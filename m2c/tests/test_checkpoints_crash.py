"""
m2c/tests/test_checkpoints_crash.py -- Patch C08: a process that is killed in
the middle of a snapshot or a rewind.

tests/test_checkpoints.py injects failures inside one process.  That proves the
rollback paths; it cannot prove what a SIGKILL leaves on disk, because nothing
runs after one: no ``finally``, no ``atexit``, no rollback, no temp-file
clean-up.  So here a real child process is stopped at the interesting moment
and killed, and this process then looks at what is left.

* ``test_kill_after_the_blobs_before_the_index``: the child has written every
  blob and is about to open the index transaction.  After the kill: the
  database is healthy, the step has no rows at all (not even a partial set),
  the orphan blobs are there and harmless, the same step can be recorded again,
  and ``gc_orphans`` clears the orphans.
* ``test_kill_in_the_middle_of_a_rewind``: the child has restored one file and
  dies.  After the kill, running the same rewind in this process finishes the
  job and ends in exactly the state a clean rewind would have.

The kill points use the same two seams as the in-process tests
(``_insert_rows`` and ``_apply_item``), set inside the child, so no test hook
exists in the production code path.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import m2c
from m2c import checkpoints as cp
from m2c import paths as m2c_paths
from m2c.store import db, events, sessions

SRC_ROOT = Path(m2c.__file__).resolve().parent.parent  # .../m2c/src

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"), reason="SIGKILL is POSIX-only; M2C supports Linux and WSL2"
)

CHILD_SNAPSHOT = """
import sys, time
from m2c import checkpoints as cp
root, sid = sys.argv[1], sys.argv[2]

def stall(*args, **kwargs):          # every blob is on disk; the index transaction has not begun
    print("ready", flush=True)
    time.sleep(120)                  # killed long before this ends

cp._insert_rows = stall
cp.snapshot(sid, 1, ["a.bin", "b.bin", "absent.bin"], root)
"""

CHILD_REWIND = """
import os, signal, sys
from m2c import checkpoints as cp
root, sid = sys.argv[1], sys.argv[2]
real = cp._apply_item
done = []

def die_after_the_first(*args, **kwargs):
    out = real(*args, **kwargs)
    done.append(1)
    if len(done) == 1:               # one file is back on disk; nothing else has happened
        print("ready", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    return out

cp._apply_item = die_after_the_first
cp.rewind(sid, 1, root, truncate_conversation=True)
"""


def run_until_ready_then_kill(code: str, *args: str) -> int:
    """Start the child, wait for its "ready" line, make sure it is dead; return its exit status."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_ROOT), env.get("PYTHONPATH")]))
    proc = subprocess.Popen(
        [sys.executable, "-c", code, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    watchdog = threading.Timer(60, proc.kill)  # a hung child must fail the test, not hang the suite
    watchdog.start()
    try:
        line = proc.stdout.readline().strip()
        if line != "ready":
            proc.wait(timeout=30)
            pytest.fail(f"child did not reach the kill point: {proc.stderr.read()[-2000:]}")
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
        return proc.wait(timeout=30)
    finally:
        watchdog.cancel()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def conn():
    c = db.open_db()
    try:
        yield c
    finally:
        c.close()


def _healthy(conn) -> None:
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_kill_after_the_blobs_before_the_index(conn, proj):
    sid = sessions.create_session(conn, project_path=proj, mode="ask").id
    (proj / "a.bin").write_bytes(b"\xff\x00 first file")
    (proj / "b.bin").write_bytes(b"\xfe\x00 second file")

    status = run_until_ready_then_kill(CHILD_SNAPSHOT, str(proj), sid)

    assert status == -signal.SIGKILL
    _healthy(conn)
    assert conn.execute("SELECT count(*) FROM checkpoints WHERE session_id = ?", (sid,)).fetchone()[0] == 0
    directory = m2c_paths.config_dir() / "checkpoints" / sid
    blobs = [n for n in os.listdir(directory) if len(n) == 64]
    assert len(blobs) == 2                                    # the orphans exist, and they are only files
    assert [n for n in os.listdir(directory) if n.startswith(".m2c-ckpt")] == []  # no half-written temp

    # The step can simply be taken again ...
    res = cp.snapshot(sid, 1, ["a.bin", "b.bin", "absent.bin"], proj, conn=conn)
    assert res.recorded == ("a.bin", "b.bin") and res.absent == ("absent.bin",)
    assert res.blobs_reused == 2 and res.blobs_written == 0   # ... and finds the orphans already in place
    assert cp.gc_orphans(sid, conn=conn, min_age_s=0).blobs_removed == 0  # now they are referenced

    (proj / "a.bin").write_bytes(b"edited")
    cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.bin").read_bytes() == b"\xff\x00 first file"


def test_a_killed_snapshot_is_cleaned_by_gc(conn, proj):
    sid = sessions.create_session(conn, project_path=proj, mode="ask").id
    (proj / "a.bin").write_bytes(b"\xff\x00 first file")
    (proj / "b.bin").write_bytes(b"\xfe\x00 second file")
    run_until_ready_then_kill(CHILD_SNAPSHOT, str(proj), sid)
    assert cp.gc_orphans(sid, conn=conn, min_age_s=0).blobs_removed == 2
    directory = m2c_paths.config_dir() / "checkpoints" / sid
    assert [n for n in os.listdir(directory)] == []


def test_kill_in_the_middle_of_a_rewind(conn, proj):
    sid = sessions.create_session(conn, project_path=proj, mode="ask").id
    old = {n: bytes([0xFF, 0x00, i]) * 50 for i, n in enumerate(["a.bin", "b.bin", "c.bin"])}
    for n, data in old.items():
        (proj / n).write_bytes(data)
    cp.snapshot(sid, 1, list(old), proj, conn=conn)
    for n in old:
        (proj / n).write_bytes(b"edited " + n.encode())

    status = run_until_ready_then_kill(CHILD_REWIND, str(proj), sid)

    assert status == -signal.SIGKILL
    _healthy(conn)
    assert (proj / "a.bin").read_bytes() == old["a.bin"]               # the one file that was done
    assert (proj / "b.bin").read_bytes() == b"edited b.bin"            # the rest are as the edits left them
    assert conn.execute("SELECT count(*) FROM checkpoints WHERE session_id = ?", (sid,)).fetchone()[0] == 3
    assert events.read_events(conn, sid) == []                         # no marker: the rewind did not finish
    assert [n for n in os.listdir(proj) if n.startswith(".m2c")] == []

    result = cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)  # run it again

    for n, data in old.items():
        assert (proj / n).read_bytes() == data
    assert result.unchanged == ("a.bin",) and result.restored == ("b.bin", "c.bin")
    assert conn.execute("SELECT count(*) FROM checkpoints WHERE session_id = ?", (sid,)).fetchone()[0] == 0
    assert [e.type for e in events.read_events(conn, sid)] == ["rewind"]
