"""Real-Postgres checks for eo/workspace_code_files.py's version numbering.

The unit tests use a FakeCursor, so neither migration 0009's grants nor
the (workspace_id, file_path, version) primary key is ever exercised --
which is how "delete a file, re-create it, hit Save -> 500" shipped.

Skipped unless MINIME_TEST_DATABASE_URL is set. Point it at a scratch
database that already has migrations applied, connecting AS the app
role (minime_app), not postgres, so the grants are enforced:

    MINIME_TEST_DATABASE_URL="postgresql://minime_app@localhost/minime_test" \
        pytest tests/integration/test_workspace_code_files_pg.py
"""
import contextlib
import os
import uuid

import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")

USER = "pg_test_user"


@pytest.fixture
def wcf(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from eo import db, workspace_code_files

    @contextlib.contextmanager
    def cursor(user_id=None, trusted=False):
        conn = psycopg.connect(DSN, row_factory=dict_row)
        try:
            cur = conn.cursor()
            cur.execute(
                "select set_config('app.current_user_id', %s, true), "
                "set_config('app.trusted_internal', %s, true)",
                (user_id or "", "true" if trusted else "false"),
            )
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(db, "cursor", cursor)
    monkeypatch.setattr(workspace_code_files, "write_audit", lambda *a, **k: None)
    return workspace_code_files


@pytest.fixture
def ws(wcf):
    """A workspace the test user owns; removed afterwards (cascades)."""
    import psycopg
    ws_id = f"pgtest-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN) as conn:
        conn.execute("insert into workspaces (id, owner_id) values (%s, %s)", (ws_id, USER))
    yield ws_id
    with psycopg.connect(DSN) as conn:
        conn.execute("delete from workspaces where id = %s", (ws_id,))


def _history(wcf, ws, path):
    return sorted(h["version"] for h in wcf.get_file_history(ws, path))


def test_recreate_after_delete_then_save_does_not_collide(wcf, ws):
    wcf.write_file(ws, "a.py", "1", USER)
    wcf.write_file(ws, "a.py", "2", USER)            # live v2, history [1]
    wcf.delete_file(ws, "a.py", USER)

    assert _history(wcf, ws, "a.py") == [1, 2]       # last live version kept
    assert wcf.write_file(ws, "a.py", "new", USER, base_version=0)["version"] == 3
    assert wcf.write_file(ws, "a.py", "new2", USER)["version"] == 4   # used to be a 500
    assert _history(wcf, ws, "a.py") == [1, 2, 3]


def test_stale_editor_tab_conflicts_with_recreated_file(wcf, ws):
    wcf.write_file(ws, "a.py", "1", USER)
    wcf.delete_file(ws, "a.py", USER)                # history [1]
    wcf.write_file(ws, "a.py", "new", USER)          # live v2
    with pytest.raises(wcf.VersionConflictError):
        wcf.write_file(ws, "a.py", "stale tab", USER, base_version=1)


def test_batch_write_recreates_deleted_path_above_its_history(wcf, ws):
    wcf.write_file(ws, "b.py", "1", USER)
    wcf.delete_file(ws, "b.py", USER)
    out = wcf.write_files(ws, [{"file_path": "b.py", "content": "x"},
                               {"file_path": "c.py", "content": "y"}], USER)
    assert {f["file_path"]: f["version"] for f in out} == {"b.py": 2, "c.py": 1}
    out = wcf.write_files(ws, [{"file_path": "b.py", "content": "x2"}], USER)
    assert out[0]["version"] == 3


def test_restore_of_a_deleted_file_continues_above_history(wcf, ws):
    wcf.write_file(ws, "d.py", "keep me", USER)
    wcf.delete_file(ws, "d.py", USER)
    restored = wcf.restore_version(ws, "d.py", 1, USER)
    assert restored["content"] == "keep me" and restored["version"] == 2
    assert wcf.write_file(ws, "d.py", "again", USER)["version"] == 3


def test_move_onto_a_deleted_path_then_save(wcf, ws):
    wcf.write_file(ws, "e.py", "1", USER)
    wcf.delete_file(ws, "e.py", USER)
    wcf.write_file(ws, "f.py", "f", USER)
    wcf.move_path(ws, "f.py", "e.py", USER)
    assert wcf.write_file(ws, "e.py", "z", USER)["version"] > 2


def test_folder_delete_then_recreate(wcf, ws):
    wcf.write_files(ws, [{"file_path": f"dir/{i}.py", "content": str(i)} for i in range(3)], USER)
    assert len(wcf.delete_file(ws, "dir", USER)) == 3
    out = wcf.write_files(ws, [{"file_path": f"dir/{i}.py", "content": "n"} for i in range(3)], USER)
    assert [f["version"] for f in out] == [2, 2, 2]
    assert wcf.write_file(ws, "dir/0.py", "m", USER)["version"] == 3


# ---------------------------------------------------------------------
# move_path leaves a tombstone at the old path so its version numbers
# are never reused either (the same hole as delete-then-recreate).
# ---------------------------------------------------------------------

def test_recreate_at_the_old_path_after_a_move_continues_above_it(wcf, ws):
    for body in ("1", "2", "3"):
        wcf.write_file(ws, "a.py", body, USER)       # live v3, history [1, 2]
    wcf.move_path(ws, "a.py", "b.py", USER)

    assert _history(wcf, ws, "a.py") == [3]           # tombstone: last live version
    assert wcf.write_file(ws, "a.py", "new", USER, base_version=0)["version"] == 4
    with pytest.raises(wcf.VersionConflictError):     # stale tab still on the old file
        wcf.write_file(ws, "a.py", "stale", USER, base_version=3)
    assert wcf.write_file(ws, "a.py", "again", USER)["version"] == 5


def test_move_keeps_the_new_paths_history_complete_and_increasing(wcf, ws):
    for body in ("1", "2", "3"):
        wcf.write_file(ws, "a.py", body, USER)
    moved = wcf.move_path(ws, "a.py", "b.py", USER)

    assert moved[0]["version"] == 4
    assert _history(wcf, ws, "b.py") == [1, 2, 3]


def test_moving_a_folder_up_over_its_own_nested_namesake(wcf, ws):
    # a/b -> a: a/b/c.py goes to a/c.py while a/b/b/c.py lands on the
    # path a/b/c.py just vacated. The tombstone left at a/b/c.py must be
    # accounted for when the second file's history is shifted in.
    for body in ("1", "2"):
        wcf.write_file(ws, "a/b/c.py", body, USER)
    for body in ("1", "2", "3"):
        wcf.write_file(ws, "a/b/b/c.py", body, USER)

    moved = wcf.move_path(ws, "a/b", "a", USER)

    assert {m["file_path"] for m in moved} == {"a/c.py", "a/b/c.py"}
    versions = _history(wcf, ws, "a/b/c.py")
    assert versions == sorted(set(versions)) and versions[-1] < wcf.get_file(ws, "a/b/c.py")["version"]
    live = wcf.get_file(ws, "a/b/c.py")
    assert live["content"] == "3"
    assert wcf.write_file(ws, "a/b/c.py", "next", USER)["version"] == live["version"] + 1
