"""
m2c/tests/test_store_db.py -- Patch C02: opening the local state database.

Covers where the database lives (through paths.py, honouring M2C_HOME), the
file modes (0700 directory, 0600 database and its -wal / -shm sidecars,
whatever the umask), the fail-closed refusals (symlink, not a regular file,
another owner, not a database), the connection settings that make an
interrupted write safe, the ``transaction()`` helper, and the promise that
nothing in the store touches the network.
"""
from __future__ import annotations

import ast
import os
import socket
import sqlite3
import stat
from pathlib import Path

import pytest

import m2c
from m2c import paths
from m2c.paths import ConfigDirError
from m2c.store import db, events, sessions
from m2c.store.db import StoreError

STORE_SRC = Path(m2c.__file__).resolve().parent / "store"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def conn():
    c = db.open_db()
    yield c
    c.close()


# --------------------------------------------------------------------------
# Where is the database?
# --------------------------------------------------------------------------
def test_db_path_honours_m2c_home(tmp_path, monkeypatch):
    monkeypatch.setenv("M2C_HOME", str(tmp_path / "elsewhere"))
    assert db.db_path() == tmp_path / "elsewhere" / "state.db"


def test_db_path_defaults_to_dot_m2c_under_home(monkeypatch, _hermetic_env):
    monkeypatch.delenv("M2C_HOME", raising=False)
    assert db.db_path() == _hermetic_env / ".m2c" / "state.db"


def test_db_path_accepts_an_explicit_environ_mapping(tmp_path):
    assert db.db_path({"M2C_HOME": str(tmp_path / "x")}) == tmp_path / "x" / "state.db"


def test_open_db_creates_the_database_inside_the_config_dir(conn):
    assert db.db_path().is_file()
    assert db.db_path().parent == paths.config_dir()


def test_relative_m2c_home_is_rejected_not_resolved(monkeypatch):
    monkeypatch.setenv("M2C_HOME", "relative/dir")
    with pytest.raises(ConfigDirError, match="absolute"):
        db.open_db()


def test_open_db_never_touches_the_real_home(conn, _hermetic_env):
    assert not (_hermetic_env / ".m2c").exists()


# --------------------------------------------------------------------------
# Modes: directory 0700, database and sidecars 0600
# --------------------------------------------------------------------------
def test_directory_is_0700_and_database_is_0600(conn):
    assert _mode(paths.config_dir()) == 0o700
    assert _mode(db.db_path()) == 0o600


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o277])
def test_modes_do_not_depend_on_the_umask(umask):
    old = os.umask(umask)
    try:
        c = db.open_db()
        try:
            sessions.create_session(c, project_path="/home/u/proj", mode="ask")
            wal = db.db_path().with_name("state.db-wal")
            shm = db.db_path().with_name("state.db-shm")
            # Not vacuous: the sidecars exist while the connection is open.
            assert wal.exists() and shm.exists()
            assert _mode(db.db_path()) == 0o600
            assert _mode(wal) == 0o600
            assert _mode(shm) == 0o600
            assert _mode(paths.config_dir()) == 0o700
        finally:
            c.close()
    finally:
        os.umask(old)


def test_sidecars_stay_0600_after_a_second_connection_recreates_them(conn):
    sessions.create_session(conn, project_path="/home/u/proj", mode="ask")
    conn.close()  # last connection: SQLite removes -wal and -shm
    again = db.open_db()
    try:
        sessions.create_session(again, project_path="/home/u/other", mode="ask")
        assert _mode(db.db_path().with_name("state.db-wal")) == 0o600
        assert _mode(db.db_path().with_name("state.db-shm")) == 0o600
    finally:
        again.close()


def test_preexisting_directory_with_loose_mode_is_tightened(tmp_path, monkeypatch):
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    monkeypatch.setenv("M2C_HOME", str(target))
    c = db.open_db()
    c.close()
    assert _mode(target) == 0o700


def test_preexisting_database_with_loose_mode_is_tightened(tmp_path):
    path = tmp_path / "loose.db"
    path.touch()
    os.chmod(path, 0o644)
    c = db.connect(path)
    c.close()
    assert _mode(path) == 0o600


def test_preexisting_loose_sidecar_is_tightened(tmp_path):
    path = tmp_path / "s.db"
    c = db.connect(path)
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    wal = tmp_path / "s.db-wal"
    assert wal.exists()
    os.chmod(wal, 0o666)
    c2 = db.connect(path)  # a later open re-checks the sidecars
    try:
        assert _mode(wal) == 0o600
    finally:
        c2.close()
        c.close()


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------
def test_symlink_in_place_of_the_database_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    link = tmp_path / "state.db"
    link.symlink_to(victim)
    with pytest.raises(StoreError, match="symbolic link"):
        db.connect(link)
    assert victim.read_bytes() == b""  # nothing was written through the link


def test_directory_in_place_of_the_database_is_refused(tmp_path):
    (tmp_path / "state.db").mkdir()
    with pytest.raises(StoreError):
        db.connect(tmp_path / "state.db")


def test_database_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "owned.db"
    path.touch()
    mine = path.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: mine + 1)
    with pytest.raises(StoreError, match="another user"):
        db.connect(path)


def test_mode_that_cannot_be_set_is_refused(tmp_path, monkeypatch):
    """A filesystem that ignores chmod (drvfs under /mnt/c) must not be trusted."""
    path = tmp_path / "ignored.db"
    path.touch()
    os.chmod(path, 0o644)
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)
    with pytest.raises(StoreError, match="permission"):
        db.connect(path)


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is certainly not a sqlite database\n" * 200)
    os.chmod(path, 0o600)
    with pytest.raises(StoreError):
        db.connect(path)


def test_failed_open_does_not_leak_a_connection(tmp_path, monkeypatch):
    path = tmp_path / "junk.db"
    path.write_bytes(b"x" * 4096)
    os.chmod(path, 0o600)
    opened = []
    real_connect = sqlite3.connect

    def spy(*a, **k):
        c = real_connect(*a, **k)
        opened.append(c)
        return c

    monkeypatch.setattr(sqlite3, "connect", spy)
    with pytest.raises(StoreError):
        db.connect(path)
    assert opened
    for c in opened:
        with pytest.raises(sqlite3.ProgrammingError):  # "Cannot operate on a closed database"
            c.execute("SELECT 1")


# --------------------------------------------------------------------------
# Settings that make an interrupted write safe
# --------------------------------------------------------------------------
def test_journal_mode_is_wal(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_synchronous_is_full(conn):
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_foreign_keys_are_on(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_busy_timeout_is_set(conn):
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_connection_is_in_manual_transaction_mode(conn):
    # The store issues BEGIN/COMMIT itself (db.transaction); Python must not
    # open implicit transactions behind its back.
    assert conn.isolation_level is None


def test_a_fresh_database_passes_integrity_check(conn):
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_database_survives_close_and_reopen(tmp_path):
    first = db.open_db()
    created = sessions.create_session(first, project_path="/home/u/proj", mode="ask")
    events.append_event(first, created.id, "hello", {"n": 1})
    first.close()
    second = db.open_db()
    try:
        assert sessions.get_session(second, created.id) == created
        assert [e.type for e in events.read_events(second, created.id)] == ["hello"]
    finally:
        second.close()


# --------------------------------------------------------------------------
# transaction()
# --------------------------------------------------------------------------
def test_transaction_commits_on_success(conn):
    with db.transaction(conn):
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    assert not conn.in_transaction
    assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]


def test_transaction_rolls_back_on_error(conn):
    conn.execute("CREATE TABLE t (x)")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_transaction_rolls_back_on_keyboard_interrupt(conn):
    conn.execute("CREATE TABLE t (x)")
    with pytest.raises(KeyboardInterrupt):
        with db.transaction(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise KeyboardInterrupt
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_nested_transaction_is_a_savepoint(conn):
    conn.execute("CREATE TABLE t (x)")
    with db.transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(RuntimeError):
            with db.transaction(conn):
                conn.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("inner failure")
        conn.execute("INSERT INTO t VALUES (3)")
    assert conn.execute("SELECT x FROM t ORDER BY x").fetchall() == [(1,), (3,)]


def test_transaction_takes_the_write_lock_up_front(tmp_path):
    path = tmp_path / "lock.db"
    a = db.connect(path)
    b = db.connect(path)
    try:
        b.execute("PRAGMA busy_timeout = 100")
        with db.transaction(a):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                b.execute("BEGIN IMMEDIATE")
        b.execute("BEGIN IMMEDIATE")  # free again once a has committed
        b.execute("ROLLBACK")
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------
def test_utc_now_iso_has_a_fixed_width_utc_format():
    value = db.utc_now_iso()
    assert len(value) == 27 and value.endswith("Z")
    assert value[10] == "T"


def test_normalize_ts_accepts_datetimes_and_rfc3339_strings():
    from datetime import datetime, timedelta, timezone

    aware = datetime(2026, 11, 2, 9, 0, 0, 250000, tzinfo=timezone(timedelta(hours=6)))
    assert db.normalize_ts(aware) == "2026-11-02T03:00:00.250000Z"
    assert db.normalize_ts("2026-11-02T09:00:00.250Z") == "2026-11-02T09:00:00.250Z"
    assert db.normalize_ts(None).endswith("Z")


@pytest.mark.parametrize("bad", ["yesterday", "2026-11-02 09:00:00", "2026-11-02T09:00:00", 12345])
def test_normalize_ts_rejects_malformed_values(bad):
    with pytest.raises(ValueError):
        db.normalize_ts(bad)


def test_normalize_ts_rejects_naive_datetimes():
    from datetime import datetime

    with pytest.raises(ValueError):
        db.normalize_ts(datetime(2026, 11, 2, 9, 0, 0))


# --------------------------------------------------------------------------
# Nothing here touches the network
# --------------------------------------------------------------------------
_NETWORK_MODULES = {
    "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "imaplib", "poplib",
    "telnetlib", "xmlrpc", "asyncio", "selectors", "requests", "httpx", "websockets",
}


def test_store_modules_import_no_network_module():
    offenders = []
    for py in sorted(STORE_SRC.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [f"{py.name}: {n}" for n in names if n in _NETWORK_MODULES]
    assert offenders == []


def test_a_full_store_workflow_opens_no_socket(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("the store tried to open a network socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    c = db.open_db()
    try:
        s = sessions.create_session(c, project_path="/home/u/proj", mode="ask")
        events.append_event(c, s.id, "hello", {"a": 1})
        events.read_events(c, s.id)
        events.export_jsonl(c, tmp_path / "out.jsonl", session_id=s.id)
    finally:
        c.close()
