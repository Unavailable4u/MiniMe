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
              language="python", version=1):
    return {
        "workspace_id": workspace_id, "file_path": file_path, "content": content,
        "language": language, "version": version,
        "updated_at": _now(), "updated_by": "user_1",
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
    cursor = FakeCursor(fetchone_results=[None, _file_row(file_path="app.py", language="python")])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.write_file("ws_1", "app.py", "print(1)", "user_1")

    _, params = cursor.executed[1]  # [0] is the `for update` read, [1] the upsert
    assert params[3] == "python"  # resolved_language param


def test_write_file_explicit_language_overrides_the_guess(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None, _file_row(file_path="weird.txt", language="python")])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.write_file("ws_1", "weird.txt", "print(1)", "user_1", language="python")

    _, params = cursor.executed[1]  # [0] is the `for update` read, [1] the upsert
    assert params[3] == "python"


def test_write_file_writes_audit_and_returns_full_content_shape(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None, _file_row()])
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


# ---------------------------------------------------------------------
# move_path: grants, history carry-over, ordering
#
# The FakeCursor above never talks to Postgres, so on its own it can't
# notice a statement the database would reject. Migration 0009 REVOKEs
# UPDATE on workspace_code_file_versions from minime_app; move_path()
# used to run `update workspace_code_file_versions set file_path = ...`
# and returned a 500 on EVERY move/rename in production while every
# test here stayed green. _GrantCheckingCursor rejects exactly what the
# migration revokes, so that class of bug fails in unit tests too.
# ---------------------------------------------------------------------

import re

_REVOKED = [  # (statement regex, what migrations/0009_code_file_versions.sql says)
    (re.compile(r"^\s*update\s+workspace_code_file_versions\b", re.I | re.S),
     "permission denied for table workspace_code_file_versions (0009 revokes UPDATE)"),
]


class _GrantCheckingCursor(FakeCursor):
    def execute(self, query, params=None):
        for pattern, message in _REVOKED:
            if pattern.search(query):
                raise PermissionError(message)
        super().execute(query, params)


def _statements(cursor, needle):
    return [(q, p) for q, p in cursor.executed if needle in " ".join(q.split()).lower()]


def test_grant_checking_cursor_really_rejects_update_on_versions():
    cur = _GrantCheckingCursor()
    with pytest.raises(PermissionError):
        cur.execute("update workspace_code_file_versions set file_path = %s", ("x",))
    cur.execute("insert into workspace_code_file_versions (a) values (%s)", (1,))  # granted


def test_move_path_never_updates_the_versions_table(monkeypatch):
    old_row = _file_row(file_path="src/old.js", version=3)
    new_row = _file_row(file_path="src/new.js", version=4)
    cursor = _GrantCheckingCursor(fetchall_results=[[old_row], []], fetchone_results=[new_row])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")  # no PermissionError

    assert not _statements(cursor, "update workspace_code_file_versions")


def test_move_path_folder_never_updates_the_versions_table(monkeypatch):
    r1 = _file_row(file_path="src/c/a.js", version=2)
    r2 = _file_row(file_path="src/c/b.js", version=1)
    cursor = _GrantCheckingCursor(
        fetchall_results=[[r1, r2], []],
        fetchone_results=[dict(r1, file_path="lib/c/a.js", version=3),
                          dict(r2, file_path="lib/c/b.js", version=2)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/c", "lib/c", "user_1")

    assert not _statements(cursor, "update workspace_code_file_versions")


def test_move_path_carries_history_with_insert_select_then_delete(monkeypatch):
    old_row = _file_row(file_path="src/old.js", version=3)
    cursor = _GrantCheckingCursor(
        fetchall_results=[[old_row], []],
        fetchone_results=[_file_row(file_path="src/new.js", version=4)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")

    def norm(q):
        return " ".join(q.split()).lower()

    copy_idx = [i for i, (q, p) in enumerate(cursor.executed)
                if norm(q).startswith("insert into workspace_code_file_versions") and "select" in norm(q)]
    drop_idx = [i for i, (q, p) in enumerate(cursor.executed)
                if norm(q).startswith("delete from workspace_code_file_versions")
                and p == ("ws_1", "src/old.js")]
    assert len(copy_idx) == 1 and len(drop_idx) == 1
    # (new_path, version shift, ws_id, old_path) — no leftover history => shift 0
    assert cursor.executed[copy_idx[0]][1] == ("src/new.js", 0, "ws_1", "src/old.js")
    # copy BEFORE delete, or the old path's history is lost
    assert copy_idx[0] < drop_idx[0]


def test_move_path_snapshots_live_row_then_writes_it_one_version_higher(monkeypatch):
    old_row = _file_row(file_path="src/old.js", version=3, content="live")
    cursor = _GrantCheckingCursor(
        fetchall_results=[[old_row], []],
        fetchone_results=[_file_row(file_path="src/new.js", version=4)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")

    snapshot = [p for q, p in _statements(cursor, "insert into workspace_code_file_versions")
                if p and len(p) == 7]  # the VALUES (...) snapshot, not the INSERT ... SELECT
    assert snapshot[0][:4] == ("ws_1", "src/new.js", 3, "live")
    live_insert = _statements(cursor, "insert into workspace_code_files")[0][1]
    assert live_insert[1] == "src/new.js" and live_insert[4] == 4


def test_move_path_onto_path_with_leftover_history_shifts_incoming_versions(monkeypatch):
    # delete_file() keeps history, so a destination can have history rows
    # (v1..v5 here) with no live row. Copying versions in unchanged would
    # hit the (workspace_id, file_path, version) primary key.
    old_row = _file_row(file_path="src/old.js", version=3)
    cursor = _GrantCheckingCursor(
        fetchall_results=[
            [old_row],                                          # discovery
            [],                                                 # no LIVE collision
            [{"file_path": "src/new.js", "max_version": 5}],    # leftover history max
        ],
        fetchone_results=[_file_row(file_path="src/new.js", version=9)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")

    copy = [p for q, p in _statements(cursor, "insert into workspace_code_file_versions")
            if p and len(p) == 4]
    assert copy[0][:2] == ("src/new.js", 5)                     # versions shifted up by 5
    snapshot = [p for q, p in _statements(cursor, "insert into workspace_code_file_versions")
                if p and len(p) == 7]
    assert snapshot[0][2] == 3 + 5                              # live row's snapshot above the leftovers
    assert _statements(cursor, "insert into workspace_code_files")[0][1][4] == 3 + 5 + 1


def test_move_path_up_into_an_ancestor_moves_the_occupying_row_out_first(monkeypatch):
    # Moving a/b -> a sends a/b/b/c -> a/b/c while a/b/c -> a/c. Handled in
    # discovery order, a/b/b/c (listed first here) would try to land on
    # a/b/c while it's still live and hit a primary-key violation.
    deep = _file_row(file_path="a/b/b/c", version=1)
    shallow = _file_row(file_path="a/b/c", version=1)
    cursor = _GrantCheckingCursor(
        fetchall_results=[[deep, shallow], []],
        fetchone_results=[_file_row(file_path="a/c", version=2),
                          _file_row(file_path="a/b/c", version=2)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    result = workspace_code_files.move_path("ws_1", "a/b", "a", "user_1")

    landed = [p[1] for q, p in _statements(cursor, "insert into workspace_code_files")]
    assert landed == ["a/c", "a/b/c"]       # the occupant of a/b/c leaves before anything lands on it
    assert len(result) == 2


# ---------------------------------------------------------------------
# Item 4: a path's version numbers must never be reused, even after the
# live row is deleted (delete_file() keeps history so the file stays
# restorable). See _NEXT_VERSION_SQL and delete_file()'s docstring.
# ---------------------------------------------------------------------

def _norm(q):
    return " ".join(q.split()).lower()


def test_write_file_upsert_floors_the_version_above_existing_history(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None, _file_row(version=4)])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.write_file("ws_1", "src/app.py", "print(1)", "user_1")

    query, params = cursor.executed[1]
    assert "greatest(" in _norm(query) and "from workspace_code_file_versions" in _norm(query)
    # (ws, path, content, language, candidate, ws, path, now, user)
    assert params[4] == 1 and params[5:7] == ("ws_1", "src/app.py")


def test_write_files_upsert_floors_every_rows_version_above_history(monkeypatch):
    rows = [_file_row(file_path="a.py", version=3), _file_row(file_path="b.py", version=1)]
    cursor = FakeCursor(fetchall_results=[[], rows])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.write_files("ws_1", [
        {"file_path": "a.py", "content": "a"}, {"file_path": "b.py", "content": "b"},
    ], "user_1")

    upsert_query, flat = next((q, p) for q, p in cursor.executed if _norm(q).startswith("insert into workspace_code_files"))
    assert _norm(upsert_query).count("greatest(") == 2  # one per row
    assert len(flat) == 18  # 9 params per row


def test_delete_file_snapshots_live_rows_into_history_before_deleting(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[{"file_path": "src/app.py"}]])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.delete_file("ws_1", "src/app.py", "user_1")

    kinds = [_norm(q).split(" where")[0][:60] for q, _ in cursor.executed]
    snapshot = next(i for i, q in enumerate(cursor.executed)
                    if _norm(q[0]).startswith("insert into workspace_code_file_versions"))
    delete = next(i for i, q in enumerate(cursor.executed)
                  if _norm(q[0]).startswith("delete from workspace_code_files "))
    assert snapshot < delete, kinds
    assert "on conflict (workspace_id, file_path, version) do nothing" in _norm(cursor.executed[snapshot][0])
    # and the history of the deleted path is pruned afterwards
    assert any("row_number()" in _norm(q) for q, _ in cursor.executed[delete + 1:])


def test_delete_file_that_matches_nothing_touches_no_history(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.delete_file("ws_1", "nothing/here.py", "user_1")

    assert len(cursor.executed) == 1  # just the discovery SELECT


# ---------------------------------------------------------------------
# move_path leaves a tombstone at the old path (version never reused)
# ---------------------------------------------------------------------

def _version_inserts(cursor):
    return [(q, p) for q, p in cursor.executed
            if _norm(q).startswith("insert into workspace_code_file_versions")
            and "values (%s, %s, %s, %s, %s, %s, %s)" in _norm(q)]


def test_move_path_leaves_a_tombstone_of_the_live_version_at_the_old_path(monkeypatch):
    old_row = _file_row(file_path="src/old.js", content="live body", version=3)
    cursor = _GrantCheckingCursor(
        fetchall_results=[[old_row], []],
        fetchone_results=[_file_row(file_path="src/new.js", version=4)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "src/old.js", "src/new.js", "user_1")

    inserts = _version_inserts(cursor)  # [0] snapshot at new path, [1] tombstone at old path
    assert [p[1] for _, p in inserts] == ["src/new.js", "src/old.js"]
    tomb = inserts[1][1]
    assert tomb[2] == 3 and tomb[3] == "live body" and tomb[6] == "user"
    # ...and it lands after the old path's history was cleared, not before
    order = [_norm(q) for q, _ in cursor.executed]
    delete_idx = next(i for i, q in enumerate(order) if q.startswith("delete from workspace_code_file_versions"))
    assert order.index(_norm(inserts[1][0])) > delete_idx


def test_move_folder_up_shifts_the_second_file_above_the_first_files_tombstone(monkeypatch):
    # a/b -> a: a/b/c.py (v2) goes to a/c.py, then a/b/b/c.py (v3) lands on
    # a/b/c.py, whose pre-loop history max was 1 but now holds a tombstone v2.
    r1 = _file_row(file_path="a/b/c.py", version=2)
    r2 = _file_row(file_path="a/b/b/c.py", version=3)
    cursor = _GrantCheckingCursor(
        fetchall_results=[[r2, r1], [], [{"file_path": "a/b/c.py", "max_version": 1}]],
        fetchone_results=[_file_row(file_path="a/c.py", version=3),
                          _file_row(file_path="a/b/c.py", version=6)],
    )
    _install_fake_cursor(monkeypatch, cursor)

    workspace_code_files.move_path("ws_1", "a/b", "a", "user_1")

    copies = [(q, p) for q, p in cursor.executed if _norm(q).startswith("insert into workspace_code_file_versions")
              and "select workspace_id" in _norm(q)]
    shifts = {p[0]: p[1] for _, p in copies}  # new_path -> history_shift
    assert shifts == {"a/c.py": 0, "a/b/c.py": 2}  # 2 = the tombstone's version, not the stale 1
