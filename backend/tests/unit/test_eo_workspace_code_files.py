"""
tests/unit/test_eo_workspace_code_files.py — Patch 7e-S4.

eo/workspace_code_files.py had zero test coverage before this.
Priorities, worst-silent-failure first:

  1. _validate_file_path()'s shape gate — this is the module's actual
     security boundary (no traversal, no absolute paths, no bad chars,
     bounded length), unlike panel_content.py's allowlist gate. Every
     public function that touches a path must route through it.
  2. list_files() vs get_file()'s shape split — list_files() must NEVER
     leak `content` into its metadata dict (that's the whole reason it
     exists separately from get_file()).
  3. write_file()'s upsert + language-inference default, and that an
     explicitly-passed language always wins over the guess.
  4. build_zip_archive()'s "no rows -> None" contract and that it zips
     every file's real content under its own file_path as the arcname.

Isolation follows the same FakeCursor/FakeCursorContext convention as
test_eo_panel_content.py / test_eo_chat_workspace.py. write_audit is
patched as `workspace_code_files.write_audit` for the same "already
bound into this module's own namespace" reason those files document.
"""
import io
import zipfile
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from eo import workspace_code_files


class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed = []
        self._fetchone_queue = list(fetchone_results or [])
        self._fetchall_queue = list(fetchall_results or [])

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        if not self._fetchone_queue:
            return None
        return self._fetchone_queue.pop(0)

    def fetchall(self):
        if not self._fetchall_queue:
            return []
        return self._fetchall_queue.pop(0)


class FakeCursorContext:
    def __init__(self, cursor, calls_log, **kwargs):
        self.cursor = cursor
        self.calls_log = calls_log
        self.kwargs = kwargs

    def __enter__(self):
        self.calls_log.append(self.kwargs)
        return self.cursor

    def __exit__(self, *exc_info):
        return False


def _install_fake_cursor(monkeypatch, cursor, calls_log=None):
    calls_log = calls_log if calls_log is not None else []
    monkeypatch.setattr(
        workspace_code_files.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _now():
    return datetime(2026, 1, 1, tzinfo=UTC)


def _file_row(workspace_id="ws_1", file_path="src/app.py", content="print(1)",
              language="python"):
    return {
        "workspace_id": workspace_id, "file_path": file_path, "content": content,
        "language": language, "updated_at": _now(), "updated_by": "user_1",
    }


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch):
    monkeypatch.setattr(workspace_code_files, "write_audit", MagicMock())


# ---------------------------------------------------------------------
# _validate_file_path
# ---------------------------------------------------------------------

def test_validate_file_path_rejects_empty():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("")


def test_validate_file_path_rejects_whitespace_only():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("   ")


def test_validate_file_path_rejects_too_long():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("a" * 513)


def test_validate_file_path_rejects_absolute_unix_path():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("/etc/passwd")


def test_validate_file_path_rejects_absolute_windows_style_path():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("\\windows\\system32")


def test_validate_file_path_rejects_dotdot_traversal():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("../../etc/passwd")


def test_validate_file_path_rejects_embedded_dotdot_segment():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("src/../../etc/passwd")


def test_validate_file_path_rejects_backslash_dotdot_traversal():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("src\\..\\..\\etc")


def test_validate_file_path_rejects_invalid_characters():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("src/app;rm -rf.py")


def test_validate_file_path_accepts_a_normal_relative_path():
    workspace_code_files._validate_file_path("src/components/App.jsx")  # no raise


def test_validate_file_path_accepts_a_bare_filename():
    workspace_code_files._validate_file_path("README.md")  # no raise


# ---------------------------------------------------------------------
# _infer_language
# ---------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("app.py", "python"),
    ("index.jsx", "javascript"),
    ("types.ts", "typescript"),
    ("data.json", "json"),
    ("README.md", "markdown"),
    ("index.html", "html"),
    ("style.css", "css"),
    ("config.yml", "yaml"),
    ("schema.sql", "sql"),
    ("run.sh", "bash"),
    ("config.env", "dotenv"),
])
def test_infer_language_known_extensions(path, expected):
    assert workspace_code_files._infer_language(path) == expected


def test_infer_language_unknown_extension_defaults_to_text():
    assert workspace_code_files._infer_language("data.bin") == "text"


def test_infer_language_is_case_insensitive():
    assert workspace_code_files._infer_language("App.PY") == "python"


# ---------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------

def test_list_files_keys_by_file_path(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[
        _file_row(file_path="a.py"), _file_row(file_path="b.py", content="x" * 10),
    ]])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.list_files("ws_1")
    assert set(result.keys()) == {"a.py", "b.py"}


def test_list_files_never_includes_content_key(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[_file_row()]])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.list_files("ws_1")
    assert "content" not in result["src/app.py"]


def test_list_files_size_reflects_content_length(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[_file_row(content="0123456789")]])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.list_files("ws_1")
    assert result["src/app.py"]["size"] == 10


def test_list_files_size_zero_when_content_missing(monkeypatch):
    row = _file_row()
    row["content"] = None
    cursor = FakeCursor(fetchall_results=[[row]])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.list_files("ws_1")
    assert result["src/app.py"]["size"] == 0


def test_list_files_uses_trusted_cursor(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    calls = _install_fake_cursor(monkeypatch, cursor)
    workspace_code_files.list_files("ws_1")
    assert calls[0] == {"trusted": True}


# ---------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------

def test_get_file_rejects_invalid_path():
    with pytest.raises(ValueError):
        workspace_code_files.get_file("ws_1", "../../etc/passwd")


def test_get_file_returns_empty_shape_when_nothing_saved(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.get_file("ws_1", "src/app.py")
    assert result["content"] == ""
    assert result["updated_at"] is None


def test_get_file_returns_saved_content_when_present(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_file_row()])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.get_file("ws_1", "src/app.py")
    assert result["content"] == "print(1)"
    assert result["language"] == "python"


# ---------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------

def test_write_file_rejects_invalid_path():
    with pytest.raises(ValueError):
        workspace_code_files.write_file("ws_1", "/etc/passwd", "content", "user_1")


def test_write_file_infers_language_when_not_provided(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_file_row(file_path="app.py", language="python")])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.write_file("ws_1", "app.py", "print(1)", "user_1")

    _, params = cursor.executed[0]
    assert params[3] == "python"  # resolved_language param


def test_write_file_explicit_language_overrides_the_guess(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_file_row(file_path="weird.txt", language="python")])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.write_file("ws_1", "weird.txt", "print(1)", "user_1", language="python")

    _, params = cursor.executed[0]
    assert params[3] == "python"


def test_write_file_writes_audit_and_returns_full_content_shape(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_file_row()])
    _install_fake_cursor(monkeypatch, cursor)

    result = workspace_code_files.write_file("ws_1", "src/app.py", "print(1)", "user_1")

    assert result["content"] == "print(1)"
    workspace_code_files.write_audit.assert_called_once()


# ---------------------------------------------------------------------
# build_zip_archive
# ---------------------------------------------------------------------

def test_build_zip_archive_returns_none_when_no_files(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    assert workspace_code_files.build_zip_archive("ws_1") is None


def test_build_zip_archive_zips_every_file_under_its_own_path(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[
        {"file_path": "src/app.py", "content": "print(1)"},
        {"file_path": "README.md", "content": "# Hello"},
    ]])
    _install_fake_cursor(monkeypatch, cursor)

    archive_bytes = workspace_code_files.build_zip_archive("ws_1")

    assert archive_bytes is not None
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        names = set(zf.namelist())
        assert names == {"src/app.py", "README.md"}
        assert zf.read("src/app.py").decode() == "print(1)"
        assert zf.read("README.md").decode() == "# Hello"


def test_build_zip_archive_treats_none_content_as_empty_string(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[{"file_path": "empty.py", "content": None}]])
    _install_fake_cursor(monkeypatch, cursor)

    archive_bytes = workspace_code_files.build_zip_archive("ws_1")

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        assert zf.read("empty.py") == b""


# ---------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------

def test_delete_file_rejects_invalid_path():
    with pytest.raises(ValueError):
        workspace_code_files.delete_file("ws_1", "../escape.py", "user_1")


def test_delete_file_writes_audit(monkeypatch):
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)
    workspace_code_files.delete_file("ws_1", "src/app.py", "user_1")
    workspace_code_files.write_audit.assert_called_once()
