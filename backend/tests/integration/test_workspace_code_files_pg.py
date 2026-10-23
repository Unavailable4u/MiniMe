"""Real-Postgres checks for eo/workspace_code_files.py's version numbering.

The unit tests use a FakeCursor, so neither migration 0009's grants nor
the (workspace_id, file_path, version) primary key is ever exercised --
which is how "delete a file, re-create it, hit Save -> 500" shipped.

Skipped unless MINIME_TEST_DATABASE_URL is set. Point it at a scratch
database that already has migrations applied, connecting AS the app
role (minime_app), not postgres, so the grants are enforced:

    MINIME_TEST_DATABASE_URL="postgresql://minime_app@localhost/minime_test" \
        pytest tests/integration/test_workspace_code_files_pg.py

B01: the module-level guard below FAILS (it does not skip) when that URL
connects as a superuser, a BYPASSRLS role, or the owner of the code-file
tables, or when the database still lets minime_app UPDATE the version-history
table. In any of those cases every test here would pass without the grants
being exercised, and a reintroduced `UPDATE workspace_code_file_versions`
would go unnoticed. The workspace fixture also needs the test user to exist in
auth.users (README, "Real-database tests").
"""
import contextlib
import os
import uuid

import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")

# workspaces.owner_id is a uuid with a foreign key to auth.users(id) (the real
# schema, see migrations/0000_base_schema.sql), so the test user must be a valid
# UUID AND already exist in auth.users. The app role cannot write the auth
# schema, so an admin seeds it once per scratch database (README, "Real-database
# tests"):  insert into auth.users (id) values ('00000000-0000-4000-8000-00000000b001');
USER = "00000000-0000-4000-8000-00000000b001"


@pytest.fixture(scope="module", autouse=True)
def _connected_as_the_app_role():
    """Fail loudly unless DSN is the unprivileged app role on a fully migrated schema.

    Grants are the whole point of this file. A superuser (or BYPASSRLS role, or a
    table owner) is not subject to them, so tests would pass vacuously. Likewise a
    scratch database where migration 0009's REVOKE UPDATE was never applied."""
    import psycopg
    problems = []
    with psycopg.connect(DSN) as conn:
        who, is_super, bypass = conn.execute(
            "select current_user, r.rolsuper, r.rolbypassrls "
            "from pg_roles r where r.rolname = current_user").fetchone()
        if who != "minime_app":
            problems.append(f"connected as {who!r}, not 'minime_app'")
        if is_super:
            problems.append("the role is a superuser (grants and RLS are not enforced)")
        if bypass:
            problems.append("the role has BYPASSRLS")
        owned = [r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' "
            "and tableowner = current_user "
            "and tablename in ('workspaces', 'workspace_code_files', 'workspace_code_file_versions')")]
        if owned:
            problems.append(f"the role OWNS {owned} (owners bypass grants and RLS)")

        def can(table, priv):
            return conn.execute("select has_table_privilege(current_user, %s, %s)",
                                (table, priv)).fetchone()[0]

        if can("workspace_code_file_versions", "UPDATE"):
            problems.append("the role can UPDATE workspace_code_file_versions "
                            "(migration 0009's REVOKE was not applied)")
        for table, priv in (("workspace_code_files", "UPDATE"),
                            ("workspace_code_file_versions", "INSERT"),
                            ("workspace_code_file_versions", "DELETE")):
            if not can(table, priv):
                problems.append(f"the role lacks {priv} on {table} (migrations not fully applied?)")
    if problems:
        pytest.fail(
            "MINIME_TEST_DATABASE_URL must connect as the unprivileged app role on a database "
            "with every migration applied: " + "; ".join(problems), pytrace=False)


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
    """A workspace the test user owns; removed afterwards (cascades).

    Both the insert and the cleanup run with app.current_user_id set, because
    RLS applies to minime_app: without it the insert is rejected, and (worse)
    the cleanup delete silently matches zero rows and leaks the workspace and
    every code file under it."""
    import psycopg
    ws_id = f"pgtest-{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(DSN) as conn:
            conn.execute("select set_config('app.current_user_id', %s, true)", (USER,))
            conn.execute("insert into workspaces (id, name, owner_id) values (%s, %s, %s)",
                         (ws_id, "pgtest", USER))
    except psycopg.errors.ForeignKeyViolation:
        pytest.fail(
            f"auth.users has no row for the test user {USER}. As an admin on the scratch "
            f"database run:  insert into auth.users (id) values ('{USER}');  (see README, "
            f"'Real-database tests')")
    yield ws_id
    with psycopg.connect(DSN) as conn:
        conn.execute("select set_config('app.current_user_id', %s, true)", (USER,))
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


# ---------------------------------------------------------------------
# B01 additions: the scenarios the audit found missing, and a direct,
# behavioural proof that the grants are enforced on this connection.
# ---------------------------------------------------------------------

def test_the_app_role_is_refused_update_on_version_history_and_row_locks(wcf, ws):
    """The behavioural twin of the guard fixture: ask the database, as the app role,
    to do the things migration 0009 forbids. `UPDATE ... SET file_path` is exactly
    what move_path() once ran (-> 500 on every move); the row lock is the same
    privilege through a different door."""
    import psycopg
    wcf.write_file(ws, "g.py", "1", USER)
    wcf.write_file(ws, "g.py", "2", USER)            # leaves one history row to aim at
    for sql in (
        "update workspace_code_file_versions set file_path = 'x.py' where workspace_id = %s",
        "select 1 from workspace_code_file_versions where workspace_id = %s for update",
    ):
        conn = psycopg.connect(DSN)
        try:
            conn.execute("select set_config('app.current_user_id', %s, true)", (USER,))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql, (ws,))
        finally:
            conn.rollback()
            conn.close()


def test_save_snapshots_every_overwritten_version(wcf, ws):
    first = wcf.write_file(ws, "s.py", "one", USER)
    assert (first["version"], first["content"]) == (1, "one")
    assert wcf.write_file(ws, "s.py", "two", USER, base_version=1)["version"] == 2
    assert wcf.write_file(ws, "s.py", "three", USER, base_version=2)["version"] == 3

    live = wcf.get_file(ws, "s.py")
    assert (live["content"], live["version"]) == ("three", 3)
    assert wcf.list_files(ws)["s.py"]["version"] == 3
    # each overwritten version is kept with the content it had; the live one is not duplicated
    assert {h["version"]: h["content"] for h in wcf.get_file_history(ws, "s.py")} == {1: "one", 2: "two"}

    with pytest.raises(wcf.VersionConflictError):    # a stale tab is refused...
        wcf.write_file(ws, "s.py", "stale", USER, base_version=1)
    assert wcf.get_file(ws, "s.py")["content"] == "three"   # ...and changes nothing


def test_moving_a_folder_carries_every_files_history_to_the_new_paths(wcf, ws):
    for body in ("1", "2"):
        wcf.write_file(ws, "src/a.py", body, USER)   # live v2, history [1]
    wcf.write_file(ws, "src/sub/b.py", "b", USER)    # live v1
    wcf.write_file(ws, "keep/k.py", "k", USER)       # outside the move

    moved = wcf.move_path(ws, "src", "lib", USER)

    assert {m["file_path"]: m["version"] for m in moved} == {"lib/a.py": 3, "lib/sub/b.py": 2}
    assert set(wcf.list_files(ws)) == {"lib/a.py", "lib/sub/b.py", "keep/k.py"}   # old paths are gone
    assert wcf.get_file(ws, "lib/a.py")["content"] == "2"
    assert wcf.get_file(ws, "lib/sub/b.py")["content"] == "b"
    assert _history(wcf, ws, "lib/a.py") == [1, 2]
    assert _history(wcf, ws, "lib/sub/b.py") == [1]
    assert wcf.get_file(ws, "keep/k.py")["version"] == 1
    # tombstones at the old paths hold each file's last live version
    assert _history(wcf, ws, "src/a.py") == [2]
    assert _history(wcf, ws, "src/sub/b.py") == [1]
    # and a save at a moved path carries on above its history
    assert wcf.write_file(ws, "lib/a.py", "3", USER)["version"] == 4


def test_restoring_an_older_version_of_a_live_file_writes_it_as_a_new_version(wcf, ws):
    for body in ("v1", "v2", "v3"):
        wcf.write_file(ws, "r.py", body, USER)       # live v3, history [1, 2]

    restored = wcf.restore_version(ws, "r.py", 1, USER)

    assert (restored["content"], restored["version"]) == ("v1", 4)
    # nothing is lost: v3 was snapshotted before being replaced, so the restore itself is undoable
    assert {h["version"]: h["content"] for h in wcf.get_file_history(ws, "r.py")} == {
        1: "v1", 2: "v2", 3: "v3"}
    undone = wcf.restore_version(ws, "r.py", 3, USER)
    assert (undone["content"], undone["version"]) == ("v3", 5)
    with pytest.raises(ValueError):                  # a version that never existed
        wcf.restore_version(ws, "r.py", 99, USER)
