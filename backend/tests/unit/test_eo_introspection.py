"""
tests/unit/test_eo_introspection.py — Patch B4.

Reuses the exact `_path_in()` helper and ALLOWED_ROOTS-relative fixture
paths test_eo_redaction_guard.py (Patch B2) already established, per
the plan's own instruction: keeps the two test files honest about
testing the same boundary, rather than each drifting toward its own
idea of what's "allowed."

Two kinds of coverage:
  1. Against the REAL repo tree (backend/eo/*.py) — confirms each
     function actually reads/lists/searches real, currently-allowed
     files, not just a synthetic fixture.
  2. Against a temporary directory monkeypatched into
     redaction_guard.ALLOWED_ROOTS — isolates the "secret-named file
     denied even inside an allowed root" and size/count-cap cases from
     the real repo tree, so these tests don't depend on (or risk
     mutating) anything actually checked in.
"""
import os

import pytest

from eo import introspection, redaction_guard
from tests.unit.test_eo_redaction_guard import _path_in

# ---------------------------------------------------------------------------
# list_directory()
# ---------------------------------------------------------------------------

def test_list_directory_denied_path_returns_readable_false_and_no_entries():
    result = introspection.list_directory("/etc")
    assert result == {"readable": False, "path": "/etc", "entries": None, "error": None}


def test_list_directory_lists_a_real_allowed_directory():
    eo_dir = _path_in("eo")
    result = introspection.list_directory(eo_dir)

    assert result["readable"] is True
    assert result["error"] is None
    names = {e["name"] for e in result["entries"]}
    assert "redaction_guard.py" in names
    assert "introspection.py" in names

    entry = next(e for e in result["entries"] if e["name"] == "redaction_guard.py")
    assert entry["type"] == "file"
    assert entry["readable"] is True


def test_list_directory_on_a_file_path_returns_not_a_directory_error():
    file_path = _path_in("eo", "redaction_guard.py")
    result = introspection.list_directory(file_path)
    assert result["readable"] is True
    assert result["entries"] is None
    assert result["error"] == "not_a_directory"


def test_list_directory_marks_secret_named_entries_unreadable_but_still_lists_name(
    tmp_path, monkeypatch,
):
    # Clean-named subdirectory, not tmp_path directly -- see the same
    # note in test_search_text_skips_secret_named_files_without_reporting_them
    # below about pytest's tmp_path picking up "secret" from this test's
    # own name.
    root_dir = tmp_path / "list_root"
    root_dir.mkdir()
    monkeypatch.setattr(redaction_guard, "ALLOWED_ROOTS",
                         redaction_guard.ALLOWED_ROOTS + [str(root_dir)])
    (root_dir / "normal.txt").write_text("hello")
    (root_dir / ".env").write_text("SECRET=1")

    result = introspection.list_directory(str(root_dir))

    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["normal.txt"]["readable"] is True
    assert by_name[".env"]["readable"] is False
    # The name is still surfaced -- existence isn't the thing being
    # protected, content is.
    assert ".env" in by_name


# ---------------------------------------------------------------------------
# read_file()
# ---------------------------------------------------------------------------

def test_read_file_denied_path_returns_readable_false_and_no_content():
    result = introspection.read_file("/etc/passwd")
    assert result["readable"] is False
    assert result["content"] is None


def test_read_file_reads_a_real_allowed_file():
    file_path = _path_in("eo", "redaction_guard.py")
    result = introspection.read_file(file_path)
    assert result["readable"] is True
    assert result["error"] is None
    assert "def is_readable" in result["content"]
    assert result["truncated"] is False


def test_read_file_on_a_directory_path_returns_not_a_file_error():
    eo_dir = _path_in("eo")
    result = introspection.read_file(eo_dir)
    assert result["readable"] is True
    assert result["content"] is None
    assert result["error"] == "not_a_file"


@pytest.mark.parametrize("filename", [".env", "credentials.json", "id_rsa"])
def test_read_file_denied_for_secret_named_file_in_an_allowed_root(
    tmp_path, monkeypatch, filename,
):
    # Clean-named subdirectory as the allowed root -- otherwise
    # pytest's own tmp_path (derived from this test's name, which
    # contains "secret") would deny the ROOT itself, making the
    # assertion pass for the wrong reason.
    root_dir = tmp_path / "read_root"
    root_dir.mkdir()
    monkeypatch.setattr(redaction_guard, "ALLOWED_ROOTS",
                         redaction_guard.ALLOWED_ROOTS + [str(root_dir)])
    target = root_dir / filename
    target.write_text("super secret content")

    result = introspection.read_file(str(target))

    assert result["readable"] is False
    assert result["content"] is None


def test_read_file_truncates_over_max_read_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(redaction_guard, "ALLOWED_ROOTS",
                         redaction_guard.ALLOWED_ROOTS + [str(tmp_path)])
    monkeypatch.setattr(introspection, "MAX_READ_BYTES", 10)
    big_file = tmp_path / "big.txt"
    big_file.write_text("x" * 100)

    result = introspection.read_file(str(big_file))

    assert result["readable"] is True
    assert result["truncated"] is True
    assert len(result["content"]) == 10


def test_read_file_binary_content_returns_not_text_error(tmp_path, monkeypatch):
    monkeypatch.setattr(redaction_guard, "ALLOWED_ROOTS",
                         redaction_guard.ALLOWED_ROOTS + [str(tmp_path)])
    binary_file = tmp_path / "image.bin"
    binary_file.write_bytes(bytes([0xFF, 0xFE, 0x00, 0x80, 0x81]))

    result = introspection.read_file(str(binary_file))

    assert result["readable"] is True
    assert result["content"] is None
    assert result["error"] == "not_text"


# ---------------------------------------------------------------------------
# search_text()
# ---------------------------------------------------------------------------

def test_search_text_denied_root_returns_readable_false_and_no_matches():
    result = introspection.search_text("anything", "/etc")
    assert result["readable"] is False
    assert result["matches"] is None


def test_search_text_finds_matches_in_a_real_allowed_root():
    eo_dir = _path_in("eo")
    result = introspection.search_text(r"class RedactionGuard|def is_readable", eo_dir)

    assert result["readable"] is True
    assert result["error"] is None
    hit_paths = {m["path"] for m in result["matches"]}
    assert any(p.endswith("redaction_guard.py") for p in hit_paths)


def test_search_text_invalid_pattern_returns_error():
    eo_dir = _path_in("eo")
    result = introspection.search_text("(unclosed[", eo_dir)
    assert result["readable"] is True
    assert result["matches"] is None
    assert result["error"] == "invalid_pattern"


def test_search_text_skips_secret_named_files_without_reporting_them(
    tmp_path, monkeypatch,
):
    # Use a clean-named subdirectory rather than tmp_path directly --
    # pytest's own tmp_path is derived from the test function's name,
    # which for this test happens to contain "secret" and would
    # otherwise trip SECRET_NAME_PATTERNS on the root itself.
    root_dir = tmp_path / "search_root"
    root_dir.mkdir()
    monkeypatch.setattr(redaction_guard, "ALLOWED_ROOTS",
                         redaction_guard.ALLOWED_ROOTS + [str(root_dir)])
    (root_dir / "normal.py").write_text("needle = 1\n")
    (root_dir / "credentials.json").write_text('{"needle": true}\n')

    result = introspection.search_text("needle", str(root_dir))

    assert result["readable"] is True
    hit_paths = {m["path"] for m in result["matches"]}
    assert any(p.endswith("normal.py") for p in hit_paths)
    assert not any(p.endswith("credentials.json") for p in hit_paths)
    # No count/skip-tracking field anywhere in the result -- the whole
    # point is that a redacted file's existence doesn't leak through
    # even as a number.
    assert set(result.keys()) == {"readable", "root", "matches", "truncated", "error"}


def test_search_text_caps_at_max_matches_and_marks_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(redaction_guard, "ALLOWED_ROOTS",
                         redaction_guard.ALLOWED_ROOTS + [str(tmp_path)])
    monkeypatch.setattr(introspection, "MAX_SEARCH_MATCHES", 3)
    (tmp_path / "many_hits.txt").write_text("needle\n" * 10)

    result = introspection.search_text("needle", str(tmp_path))

    assert result["readable"] is True
    assert len(result["matches"]) == 3
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# Same boundary as Patch B2 -- reuse test_eo_redaction_guard's own denial
# cases directly, rather than re-deriving them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    ".env", ".env.local", "prod_secrets.py", "SECRET_config.py",
    "credentials.json", "api_key.py", "id_rsa", "server.pem",
])
def test_introspection_functions_honor_the_same_secret_patterns_as_redaction_guard(
    filename,
):
    path = _path_in("eo", filename)
    assert redaction_guard.is_readable(path) is False  # sanity: same fixture as B2

    assert introspection.read_file(path)["readable"] is False
    assert introspection.list_directory(path)["readable"] is False
