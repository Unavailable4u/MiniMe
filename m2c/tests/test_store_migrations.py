"""
m2c/tests/test_store_migrations.py -- Patch C02: the schema and its
forward-only migrations.

Two kinds of migration test live here:

* against the real ``MIGRATIONS`` list -- version 0 to latest, and every
  pair of versions in it -- so a migration added by a later patch is covered
  the moment it is appended;
* against a *synthetic* list (the real v1 plus two throwaway migrations), so
  the "between each pair of versions", "already migrated is left alone",
  "newer database", "failed migration rolls back" and "two processes race"
  behaviours are exercised today, while the real list has a single entry.
"""
from __future__ import annotations

import sqlite3
import threading
from itertools import combinations

import pytest

from m2c import paths
from m2c.store import db, events, migrations, sessions
from m2c.store.db import StoreError
from m2c.store.migrations import MIGRATIONS, Migration, SchemaTooNewError

EXPECTED_TABLES = {"schema_version", "sessions", "events", "approvals", "checkpoints"}


def _v2(c: sqlite3.Connection) -> None:
    c.execute("ALTER TABLE sessions ADD COLUMN title TEXT")


def _v3(c: sqlite3.Connection) -> None:
    c.execute("CREATE INDEX events_by_type ON events(type)")
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")


SYNTHETIC = MIGRATIONS + (
    Migration(len(MIGRATIONS) + 1, "add session title", _v2),
    Migration(len(MIGRATIONS) + 2, "events index and notes", _v3),
)


@pytest.fixture
def raw(tmp_path):
    """A connected but unmigrated database (version 0)."""
    c = db.connect(tmp_path / "m.db")
    yield c
    c.close()


def _schema(c: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """The schema as comparable data, excluding sqlite's own tables."""
    return sorted(
        c.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


# --------------------------------------------------------------------------
# The migration list itself
# --------------------------------------------------------------------------
def test_real_migration_list_is_valid_and_starts_at_one():
    migrations.validate_migrations(MIGRATIONS)
    assert MIGRATIONS[0].version == 1
    assert migrations.LATEST_VERSION == MIGRATIONS[-1].version


@pytest.mark.parametrize(
    "bad",
    [
        (Migration(2, "starts at two", _v2),),
        (Migration(1, "a", _v2), Migration(3, "gap", _v2)),
        (Migration(1, "a", _v2), Migration(1, "duplicate", _v2)),
        (Migration(1, "a", _v2), Migration(0, "backwards", _v2)),
        (Migration(0, "zero", _v2),),
    ],
)
def test_invalid_migration_lists_are_rejected(bad):
    with pytest.raises(ValueError):
        migrations.validate_migrations(bad)


def test_empty_migration_list_is_valid_and_does_nothing(raw):
    assert migrations.migrate(raw, migrations=()) == []
    assert migrations.current_version(raw) == 0


# --------------------------------------------------------------------------
# From version 0
# --------------------------------------------------------------------------
def test_a_new_database_is_at_version_zero(raw):
    assert migrations.current_version(raw) == 0
    assert _tables(raw) == set()


def test_migrating_from_zero_reaches_the_latest_version(raw):
    applied = migrations.migrate(raw)
    assert applied == [m.version for m in MIGRATIONS]
    assert migrations.current_version(raw) == migrations.LATEST_VERSION
    assert _tables(raw) >= EXPECTED_TABLES


def test_schema_version_table_records_each_migration(raw):
    migrations.migrate(raw)
    rows = raw.execute("SELECT version, name, applied_at FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [m.version for m in MIGRATIONS]
    assert [r[1] for r in rows] == [m.name for m in MIGRATIONS]
    assert all(r[2].endswith("Z") for r in rows)


def test_open_db_migrates_a_new_database(tmp_path):
    c = db.open_db()
    try:
        assert migrations.current_version(c) == migrations.LATEST_VERSION
    finally:
        c.close()


def test_migrated_database_passes_integrity_and_foreign_key_checks(raw):
    migrations.migrate(raw)
    assert raw.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("start,end", list(combinations(range(0, len(MIGRATIONS) + 1), 2)))
def test_real_migrations_between_every_pair_of_versions(tmp_path, start, end):
    """Stepwise (start, then end) must give the same schema as going straight to end."""
    stepwise = db.connect(tmp_path / "step.db")
    direct = db.connect(tmp_path / "direct.db")
    try:
        migrations.migrate(stepwise, target=start)
        assert migrations.current_version(stepwise) == start
        migrations.migrate(stepwise, target=end)
        migrations.migrate(direct, target=end)
        assert migrations.current_version(stepwise) == end
        assert _schema(stepwise) == _schema(direct)
    finally:
        stepwise.close()
        direct.close()


# --------------------------------------------------------------------------
# Between each pair of versions, on a list long enough to have pairs
# --------------------------------------------------------------------------
@pytest.mark.parametrize("start,end", list(combinations(range(0, len(SYNTHETIC) + 1), 2)))
def test_every_pair_of_versions_keeps_data_and_matches_a_direct_migration(tmp_path, start, end):
    path = tmp_path / "pair.db"
    c = db.connect(path)
    direct = db.connect(tmp_path / "direct.db")
    try:
        migrations.migrate(c, target=start, migrations=SYNTHETIC)
        seeded = None
        if start >= 1:
            seeded = sessions.create_session(c, project_path="/home/u/proj", mode="ask")
            events.append_event(c, seeded.id, "hello", {"n": 1})

        applied = migrations.migrate(c, target=end, migrations=SYNTHETIC)
        assert applied == list(range(start + 1, end + 1))
        assert migrations.current_version(c) == end

        migrations.migrate(direct, target=end, migrations=SYNTHETIC)
        assert _schema(c) == _schema(direct)

        if seeded is not None:
            assert sessions.get_session(c, seeded.id) == seeded
            assert [e.data for e in events.read_events(c, seeded.id)] == [{"n": 1}]
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        c.close()
        direct.close()


def test_synthetic_migrations_really_change_the_schema(raw):
    migrations.migrate(raw, migrations=SYNTHETIC)
    assert "title" in _columns(raw, "sessions")
    assert "notes" in _tables(raw)


# --------------------------------------------------------------------------
# Already migrated: left alone
# --------------------------------------------------------------------------
def test_an_already_migrated_database_is_left_alone(raw):
    migrations.migrate(raw, migrations=SYNTHETIC)
    s = sessions.create_session(raw, project_path="/home/u/proj", mode="ask")
    events.append_event(raw, s.id, "hello", {"n": 1})

    schema_before = _schema(raw)
    versions_before = raw.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
    changes_before = raw.total_changes

    assert migrations.migrate(raw, migrations=SYNTHETIC) == []
    assert migrations.migrate(raw, migrations=SYNTHETIC) == []

    assert raw.total_changes == changes_before  # not a single row written
    assert _schema(raw) == schema_before
    assert raw.execute("SELECT * FROM schema_version ORDER BY version").fetchall() == versions_before
    assert [e.data for e in events.read_events(raw, s.id)] == [{"n": 1}]


def test_re_migrating_does_not_even_take_the_write_lock(tmp_path):
    path = tmp_path / "l.db"
    a = db.connect(path)
    b = db.connect(path)
    try:
        migrations.migrate(a)
        b.execute("PRAGMA busy_timeout = 100")
        a.execute("BEGIN IMMEDIATE")  # someone else is holding the write lock
        try:
            assert migrations.migrate(b) == []  # nothing pending: must not block or fail
        finally:
            a.execute("ROLLBACK")
    finally:
        a.close()
        b.close()


def test_migrating_to_the_current_version_is_a_no_op(raw):
    migrations.migrate(raw, migrations=SYNTHETIC, target=2)
    assert migrations.migrate(raw, migrations=SYNTHETIC, target=2) == []


# --------------------------------------------------------------------------
# Forward only
# --------------------------------------------------------------------------
def test_target_below_the_current_version_is_refused(raw):
    migrations.migrate(raw, migrations=SYNTHETIC)
    with pytest.raises(StoreError, match="forward-only"):
        migrations.migrate(raw, migrations=SYNTHETIC, target=1)
    assert migrations.current_version(raw) == len(SYNTHETIC)


@pytest.mark.parametrize("target", [-1, 99, True, "2"])
def test_invalid_targets_are_rejected(raw, target):
    with pytest.raises(ValueError):
        migrations.migrate(raw, migrations=SYNTHETIC, target=target)


def test_a_database_from_a_newer_client_is_refused_untouched(tmp_path):
    path = tmp_path / "new.db"
    newer = db.connect(path)
    migrations.migrate(newer, migrations=SYNTHETIC)
    before = _schema(newer)
    newer.close()

    older = db.connect(path)
    try:
        with pytest.raises(SchemaTooNewError) as info:
            migrations.migrate(older)  # the real list knows fewer versions
        assert info.value.found == len(SYNTHETIC)
        assert info.value.supported == migrations.LATEST_VERSION
        assert isinstance(info.value, StoreError)
        assert _schema(older) == before
        assert migrations.current_version(older) == len(SYNTHETIC)
    finally:
        older.close()


def test_open_db_refuses_a_newer_database():
    paths.prepare_config_dir()
    real = db.connect(db.db_path())
    migrations.migrate(real, migrations=SYNTHETIC)
    real.close()
    with pytest.raises(SchemaTooNewError):
        db.open_db()


def test_inconsistent_schema_version_table_is_refused(raw):
    migrations.migrate(raw, migrations=SYNTHETIC)
    raw.execute("DELETE FROM schema_version WHERE version = 2")
    with pytest.raises(StoreError, match="inconsistent"):
        migrations.current_version(raw)


# --------------------------------------------------------------------------
# A failing migration rolls back completely
# --------------------------------------------------------------------------
def _failing(c: sqlite3.Connection) -> None:
    c.execute("CREATE TABLE half_done (x)")
    c.execute("INSERT INTO no_such_table VALUES (1)")  # raises


def test_failed_migration_rolls_back_and_names_the_version(raw):
    broken = MIGRATIONS + (Migration(len(MIGRATIONS) + 1, "broken", _failing),)
    with pytest.raises(StoreError, match=rf"migration {len(MIGRATIONS) + 1} \(broken\) failed"):
        migrations.migrate(raw, migrations=broken)
    # the earlier migration committed on its own; the broken one left nothing behind
    assert migrations.current_version(raw) == len(MIGRATIONS)
    assert "half_done" not in _tables(raw)
    assert not raw.in_transaction


def test_failed_migration_can_be_retried_after_it_is_fixed(raw):
    broken = MIGRATIONS + (Migration(len(MIGRATIONS) + 1, "broken", _failing),)
    with pytest.raises(StoreError):
        migrations.migrate(raw, migrations=broken)
    assert migrations.migrate(raw, migrations=SYNTHETIC) == [len(MIGRATIONS) + 1, len(MIGRATIONS) + 2]


def test_unversioned_database_with_clashing_tables_fails_loudly(raw):
    raw.execute("CREATE TABLE sessions (something_else TEXT)")
    with pytest.raises(StoreError, match="migration 1"):
        migrations.migrate(raw)
    assert migrations.current_version(raw) == 0


# --------------------------------------------------------------------------
# Two openers at once
# --------------------------------------------------------------------------
def test_two_connections_migrating_at_once_apply_each_migration_once(tmp_path):
    path = tmp_path / "race.db"
    db.connect(path).close()  # create the file and switch it to WAL up front
    barrier = threading.Barrier(4)
    results: list[list[int]] = []
    errors: list[BaseException] = []

    def worker():
        c = db.connect(path)
        try:
            barrier.wait()
            results.append(migrations.migrate(c, migrations=SYNTHETIC))
        except BaseException as exc:  # noqa: BLE001 -- collected and asserted below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    applied = sorted(v for r in results for v in r)
    assert applied == list(range(1, len(SYNTHETIC) + 1))  # each version exactly once
    check = db.connect(path)
    try:
        assert migrations.current_version(check) == len(SYNTHETIC)
    finally:
        check.close()


# --------------------------------------------------------------------------
# The v1 schema: tables and columns the work package names
# --------------------------------------------------------------------------
@pytest.fixture
def conn(raw):
    migrations.migrate(raw)
    return raw


def test_sessions_columns(conn):
    assert _columns(conn, "sessions") == ["id", "project_path", "created_at", "mode", "model_pref"]


def test_events_columns_and_primary_key(conn):
    assert _columns(conn, "events") == ["session_id", "seq", "ts", "type", "json"]
    pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(events)"), key=lambda r: r[5]) if r[5]]
    assert pk == ["session_id", "seq"]


def test_approvals_columns(conn):
    assert _columns(conn, "approvals") == [
        "session_id", "seq", "call_id", "tool", "input_summary",
        "decision", "scope", "decided_by", "ts",
    ]


def test_checkpoints_columns(conn):
    assert _columns(conn, "checkpoints") == ["session_id", "step", "path", "blob_hash", "ts"]


def test_schema_version_columns(conn):
    assert _columns(conn, "schema_version") == ["version", "name", "applied_at"]


def _session(conn) -> str:
    return sessions.create_session(conn, project_path="/home/u/proj", mode="ask").id


def test_approval_needs_a_real_event(conn):
    sid = _session(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO approvals VALUES (?, 1, 'call_1', 'bash', 'ls', 'allow', 'once', 'user', ?)",
            (sid, db.utc_now_iso()),
        )


def test_approval_can_point_at_the_event_that_recorded_it(conn):
    sid = _session(conn)
    ev = events.append_event(conn, sid, "approval", {"call_id": "call_1", "decision": "allow"})
    conn.execute(
        "INSERT INTO approvals VALUES (?, ?, 'call_1', 'bash', 'ls -la', 'allow', 'once', 'user', ?)",
        (sid, ev.seq, db.utc_now_iso()),
    )
    assert conn.execute("SELECT tool, decision FROM approvals").fetchall() == [("bash", "allow")]


@pytest.mark.parametrize(
    "decision,scope,by",
    [("maybe", "once", "user"), ("allow", "forever", "user"), ("allow", "once", "robot")],
)
def test_approval_values_are_constrained_to_the_protocol_enums(conn, decision, scope, by):
    sid = _session(conn)
    ev = events.append_event(conn, sid, "approval", {})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO approvals VALUES (?, ?, 'c', 'bash', 'ls', ?, ?, ?, ?)",
            (sid, ev.seq, decision, scope, by, db.utc_now_iso()),
        )


def test_checkpoint_blob_hash_may_be_null_for_a_path_that_did_not_exist(conn):
    sid = _session(conn)
    conn.execute("INSERT INTO checkpoints VALUES (?, 1, 'src/new.py', NULL, ?)", (sid, db.utc_now_iso()))
    conn.execute("INSERT INTO checkpoints VALUES (?, 1, 'src/old.py', 'ab' || hex(zeroblob(31)), ?)", (sid, db.utc_now_iso()))
    assert conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 2


def test_checkpoint_primary_key_is_session_step_path(conn):
    sid = _session(conn)
    conn.execute("INSERT INTO checkpoints VALUES (?, 1, 'a.py', 'h1', ?)", (sid, db.utc_now_iso()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO checkpoints VALUES (?, 1, 'a.py', 'h2', ?)", (sid, db.utc_now_iso()))
    conn.execute("INSERT INTO checkpoints VALUES (?, 2, 'a.py', 'h2', ?)", (sid, db.utc_now_iso()))


def test_checkpoint_needs_a_real_session(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("INSERT INTO checkpoints VALUES ('nope', 1, 'a.py', 'h', ?)", (db.utc_now_iso(),))
