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


# W1.2: widened _VALID_PATH_CHARS -- Next.js/SvelteKit route-folder
# conventions and filenames with spaces must now be accepted, while
# every traversal/absolute/length check from above must keep rejecting
# exactly what it always did.

@pytest.mark.parametrize("path", [
    "app/[id]/page.js",
    "app/(auth)/login.tsx",
    "app/@modal/(.)photo.tsx",
    "src/routes/+page.svelte",
    "src/my component.jsx",
])
def test_validate_file_path_accepts_nextjs_sveltekit_route_conventions(path):
    workspace_code_files._validate_file_path(path)  # no raise


def test_validate_file_path_still_rejects_dotdot_traversal_with_widened_charset():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("app/(auth)/../../etc/passwd")


def test_validate_file_path_still_rejects_embedded_dotdot_with_widened_charset():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("a/../../b")


def test_validate_file_path_rejects_null_byte():
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("src/\x00evil.py")


def test_validate_file_path_still_rejects_percent_sign():
    # % stays out of the allowlist even after widening -- see
    # _VALID_PATH_CHARS's own comment for why (it's meaningful to the
    # LIKE patterns delete_file()/move_path() build from validated paths).
    with pytest.raises(ValueError):
        workspace_code_files._validate_file_path("src/100%done.py")


# ---------------------------------------------------------------------
# _escape_like_prefix
# ---------------------------------------------------------------------

def test_escape_like_prefix_escapes_underscore_wildcard():
    assert workspace_code_files._escape_like_prefix("src/my_file") == "src/my\\_file"


def test_escape_like_prefix_escapes_percent_and_backslash():
    assert workspace_code_files._escape_like_prefix("100%\\done") == "100\\%\\\\done"


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


# W1.2: delete_file() now also deletes an entire folder subtree by
# prefix, and is idempotent (no error, empty result) when nothing
# matches.

def test_delete_file_no_match_returns_empty_list_and_still_audits(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.delete_file("ws_1", "nothing/here.py", "user_1")
    assert result == []
    workspace_code_files.write_audit.assert_called_once()


def test_delete_file_deletes_every_row_under_a_folder_prefix(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[
        {"file_path": "src/comp/a.js"}, {"file_path": "src/comp/b.js"},
    ]])
    _install_fake_cursor(monkeypatch, cursor)
    result = workspace_code_files.delete_file("ws_1", "src/comp", "user_1")
    assert result == ["src/comp/a.js", "src/comp/b.js"]


# ---------------------------------------------------------------------
# create_folder (W1.2)
# ---------------------------------------------------------------------

def test_create_folder_rejects_empty_path():
    with pytest.raises(ValueError):
        workspace_code_files.create_folder("ws_1", "", "user_1")


def test_create_folder_writes_a_gitkeep_placeholder(monkeypatch):
    # create_folder() -> write_file(): fetchone() #1 = current_row (None,
    # nothing saved yet), fetchone() #2 = the upserted row write_file()
    # returns.
    new_row = _file_row(file_path="src/newdir/.gitkeep", content="", language="text")
    new_row["version"] = 1
    cursor = FakeCursor(fetchone_results=[None, new_row])
    _install_fake_cursor(monkeypatch, cursor)

    result = workspace_code_files.create_folder("ws_1", "src/newdir", "user_1")

    assert result["file_path"] == "src/newdir/.gitkeep"


def test_create_folder_strips_a_trailing_slash_before_appending_gitkeep(monkeypatch):
    new_row = _file_row(file_path="src/newdir/.gitkeep", content="", language="text")
    new_row["version"] = 1
    cursor = FakeCursor(fetchone_results=[None, new_row])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.create_folder("ws_1", "src/newdir/", "user_1")

    # write_file()'s FOR UPDATE select is the first statement executed;
    # its file_path param is the placeholder path this function built.
    _, params = cursor.executed[0]
    assert params[1] == "src/newdir/.gitkeep"


# ---------------------------------------------------------------------
# move_path (W1.2)
# ---------------------------------------------------------------------

def test_move_path_rejects_identical_source_and_destination():
    with pytest.raises(ValueError):
        workspace_code_files.move_path("ws_1", "src/a.js", "src/a.js", "user_1")


def test_move_path_rejects_moving_a_folder_into_its_own_subtree():
    with pytest.raises(ValueError):
        workspace_code_files.move_path("ws_1", "src", "src/sub", "user_1")


def test_move_path_rejects_when_nothing_exists_at_the_source(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        workspace_code_files.move_path("ws_1", "nowhere.js", "elsewhere.js", "user_1")


def test_move_path_refuses_to_overwrite_an_existing_destination(monkeypatch):
    cursor = FakeCursor(fetchall_results=[
        [_file_row(file_path="src/old.js")],       # discovery
        [{"file_path": "src/new.js"}],             # collision check: occupied
    ])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")


def test_move_path_single_file_lands_at_the_new_path_one_version_higher(monkeypatch):
    old_row = _file_row(file_path="src/old.js", content="x")
    old_row["version"] = 3
    new_row = _file_row(file_path="src/new.js", content="x")
    new_row["version"] = 4
    cursor = FakeCursor(
        fetchall_results=[[old_row], []],  # discovery, then no collision
        fetchone_results=[new_row],        # the re-inserted live row
    )
    _install_fake_cursor(monkeypatch, cursor)

    result = workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")

    assert len(result) == 1
    assert result[0]["file_path"] == "src/new.js"
    assert result[0]["version"] == 4


def test_move_path_folder_moves_every_file_under_the_new_prefix(monkeypatch):
    r1 = _file_row(file_path="src/comp/a.js", content="a")
    r1["version"] = 1
    r2 = _file_row(file_path="src/comp/b.js", content="b")
    r2["version"] = 2
    new1 = dict(r1, file_path="lib/comp/a.js", version=2)
    new2 = dict(r2, file_path="lib/comp/b.js", version=3)
    cursor = FakeCursor(
        fetchall_results=[[r1, r2], []],
        fetchone_results=[new1, new2],
    )
    _install_fake_cursor(monkeypatch, cursor)

    result = workspace_code_files.move_path("ws_1", "src/comp", "lib/comp", "user_1")

    assert [f["file_path"] for f in result] == ["lib/comp/a.js", "lib/comp/b.js"]


def test_move_path_writes_a_single_summary_audit_entry(monkeypatch):
    old_row = _file_row(file_path="src/old.js", content="x")
    old_row["version"] = 1
    new_row = dict(old_row, file_path="src/new.js", version=2)
    cursor = FakeCursor(fetchall_results=[[old_row], []], fetchone_results=[new_row])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")

    workspace_code_files.write_audit.assert_called_once()
