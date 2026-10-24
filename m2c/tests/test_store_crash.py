"""
m2c/tests/test_store_crash.py -- Patch C02: an interrupted write leaves a
valid database.

Each test starts a real child process that talks to the store and kills it
with SIGKILL -- no ``finally``, no ``atexit``, no chance to roll back or
close the connection -- then reopens the database in this process.

* ``test_kill_with_an_open_uncommitted_transaction``: the child begins a
  transaction and writes enough that SQLite spills uncommitted pages to the
  WAL file; after the kill, ``PRAGMA integrity_check`` is ``ok`` and none of
  those rows is visible.
* ``test_kill_in_the_middle_of_a_stream_of_commits``: the child commits
  events one by one and reports each seq on stdout *after* the commit
  returned; after the kill, every reported seq is present and the log has no
  gaps.  (SIGKILL does not lose data the kernel already has, so an
  acknowledged commit must survive; power loss is what ``synchronous=FULL``
  is for and cannot be simulated here.)
"""
from __future__ import annotations

import os
import queue
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import m2c
from m2c.store import db, events, migrations, sessions

SRC_ROOT = Path(m2c.__file__).resolve().parent.parent  # .../m2c/src

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"), reason="SIGKILL is POSIX-only; M2C supports Linux and WSL2"
)

CHILD_UNCOMMITTED = """
import sys, time
from m2c.store import db, events
conn = db.connect(sys.argv[1])
session_id = sys.argv[2]
conn.execute("PRAGMA cache_size = 16")      # small cache: force uncommitted pages out to the WAL
conn.execute("BEGIN IMMEDIATE")
blob = "x" * 65536
for i in range(64):
    events.append_event(conn, session_id, "uncommitted", {"i": i, "blob": blob})
print("ready", flush=True)
time.sleep(120)                              # killed long before this ends
"""

CHILD_STREAM = """
import sys
from m2c.store import db, events
conn = db.connect(sys.argv[1])
session_id = sys.argv[2]
i = 0
while True:
    e = events.append_event(conn, session_id, "tick", {"i": i})
    print(e.seq, flush=True)                 # only after the commit returned
    i += 1
"""


class Child:
    """A python subprocess whose stdout lines can be awaited with a timeout."""

    def __init__(self, code: str, *args: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_ROOT), env.get("PYTHONPATH")]))
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)  # EOF

    def next_line(self, timeout: float = 30.0) -> str:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.kill()
            pytest.fail("child produced no output in time")
        if line is None:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            pytest.fail(f"child exited early: {stderr[-2000:]}")
        return line

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=30)
        self._thread.join(timeout=30)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()


@pytest.fixture
def prepared(tmp_path):
    """A migrated database holding one session and three committed events."""
    path = tmp_path / "crash.db"
    c = db.connect(path)
    migrations.migrate(c)
    s = sessions.create_session(c, project_path="/home/u/proj", mode="ask")
    for i in range(3):
        events.append_event(c, s.id, "committed", {"i": i})
    c.close()
    return path, s.id


def _assert_database_is_healthy(path: Path) -> None:
    c = db.connect(path)
    try:
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    finally:
        c.close()


def test_kill_with_an_open_uncommitted_transaction(prepared):
    path, sid = prepared
    child = Child(CHILD_UNCOMMITTED, str(path), sid)
    try:
        assert child.next_line() == "ready"
        wal = Path(str(path) + "-wal")
        # Not vacuous: uncommitted frames really are sitting in the WAL file.
        assert wal.exists() and wal.stat().st_size > 1_000_000
    finally:
        child.kill()
    assert child.proc.returncode == -signal.SIGKILL

    _assert_database_is_healthy(path)

    c = db.connect(path)
    try:
        got = events.read_events(c, sid)
        assert [e.type for e in got] == ["committed"] * 3  # the 64 big rows are gone
        assert c.execute("SELECT count(*) FROM events WHERE type = 'uncommitted'").fetchone()[0] == 0
        # The dead process's write lock is gone: appending works and continues the seq.
        assert events.append_event(c, sid, "after", {}).seq == 4
        # Recovery did not loosen any mode.
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                assert stat.S_IMODE(side.stat().st_mode) == 0o600
    finally:
        c.close()


def test_kill_in_the_middle_of_a_stream_of_commits(prepared):
    path, sid = prepared
    child = Child(CHILD_STREAM, str(path), sid)
    acknowledged = 0
    try:
        for _ in range(150):
            acknowledged = int(child.next_line())
    finally:
        child.kill()  # the child is still committing as the signal lands
    assert child.proc.returncode == -signal.SIGKILL
    assert acknowledged >= 150

    _assert_database_is_healthy(path)

    c = db.connect(path)
    try:
        got = events.read_events(c, sid)
        seqs = [e.seq for e in got]
        assert seqs == list(range(1, len(seqs) + 1))  # contiguous: no gap, no duplicate
        assert len(seqs) >= acknowledged  # every acknowledged commit survived
        assert [e.type for e in got[:3]] == ["committed"] * 3
        assert all(e.type == "tick" for e in got[3:])
        assert events.append_event(c, sid, "after", {}).seq == len(seqs) + 1
    finally:
        c.close()


def test_kill_while_holding_the_migration_lock_leaves_a_usable_database(tmp_path):
    """The database is created and migrated by a process that is killed at once."""
    path = tmp_path / "fresh.db"
    code = (
        "import sys\n"
        "from m2c.store import db, migrations\n"
        "c = db.connect(sys.argv[1])\n"
        "migrations.migrate(c)\n"
        "print('migrated', flush=True)\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "print('ready', flush=True)\n"
        "import time; time.sleep(120)\n"
    )
    child = Child(code, str(path))
    try:
        assert child.next_line() == "migrated"
        assert child.next_line() == "ready"
    finally:
        child.kill()
    _assert_database_is_healthy(path)
    c = db.connect(path)
    try:
        assert migrations.current_version(c) == migrations.LATEST_VERSION
        assert migrations.migrate(c) == []
    finally:
        c.close()
