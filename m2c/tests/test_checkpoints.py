"""
m2c/tests/test_checkpoints.py -- Patch C08: checkpoints and rewind.

A bug in this module can destroy the user's files, so most of what is here is
a failure path: corrupt blobs, symlinks swapped in after the snapshot, paths
that try to leave the project, an index that was edited by hand, a snapshot or
a rewind that is interrupted half way.  The happy path comes first and is
short: edit, edit, rewind, and the bytes are exactly what they were.

Fault injection
---------------
Two small, private functions of ``m2c.checkpoints`` are replaced by tests as
seams, because there is no other way to make a real failure land at exactly
that point:

* ``_insert_rows``  -- the one database transaction that records a step; a
  test makes it fail (or stall, in tests/test_checkpoints_crash.py) *after*
  every blob has been written and *before* any row is.
* ``_apply_item``   -- restores or deletes one file during a rewind; a test
  makes it fail after the first file.

The failure in the *middle* of the transaction needs no seam: it is a
database trigger that aborts the insert of one particular path.

Nothing here looks like a secret: file contents are random bytes or short
marker words, and every hash is computed at run time.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import random
import re
import stat
from pathlib import Path

import pytest

from m2c import checkpoints as cp
from m2c import paths as m2c_paths
from m2c.store import db, events, sessions
from m2c.tools import Caps

pytestmark = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "geteuid"),
    reason="M2C supports Linux and WSL2: POSIX file semantics are required",
)

NOT_ROOT = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def binary(n: int, seed: int) -> bytes:
    """Reproducible random bytes that are certainly not valid UTF-8 (and hold a NUL)."""
    return b"\xff\xfe\x80\x00" + random.Random(seed).randbytes(n)


def blob_dir(sid: str) -> Path:
    return m2c_paths.config_dir() / "checkpoints" / sid


def blob_names(sid: str) -> list[str]:
    d = blob_dir(sid)
    return sorted(n for n in os.listdir(d) if HEX64.match(n)) if d.is_dir() else []


def blob_path(sid: str, data: bytes) -> Path:
    return blob_dir(sid) / sha(data)


def rows(conn, sid: str) -> list[tuple]:
    return conn.execute(
        "SELECT step, path, blob_hash, mode FROM checkpoints WHERE session_id = ? ORDER BY step, path",
        (sid,),
    ).fetchall()


def edit_step(sid, n, root, edits, **kw):
    """One agent step: snapshot the paths first, then apply the edits (None deletes)."""
    result = cp.snapshot(sid, n, list(edits), root, **kw)
    for rel, data in edits.items():
        target = root / rel
        if data is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return result


def listing(root: Path) -> list[str]:
    """Every file name under root, to prove nothing stray (temp files) is left."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def outside(tmp_path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_bytes(b"OUTSIDE-ORIGINAL")
    return d.resolve()


@pytest.fixture
def conn():
    c = db.open_db()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def faults():
    """A MonkeyPatch of its own, so ``faults.undo()`` removes the injected failure and NOTHING else.

    (``monkeypatch.undo()`` would also revert the autouse fixture's M2C_HOME and HOME, and the
    "re-run after the failure" half of a test would then look in another config directory.)
    """
    mp = pytest.MonkeyPatch()
    try:
        yield mp
    finally:
        mp.undo()


@pytest.fixture
def sid(conn, proj) -> str:
    return sessions.create_session(conn, project_path=proj, mode="ask").id


# --------------------------------------------------------------------------
# The two "done when" items, then the basic semantics
# --------------------------------------------------------------------------
def test_edit_edit_rewind_restores_exact_bytes(conn, sid, proj):
    original = binary(4096, seed=1)
    with pytest.raises(UnicodeDecodeError):
        original.decode("utf-8")
    (proj / "data.bin").write_bytes(original)
    (proj / "empty.bin").write_bytes(b"")

    edit_step(sid, 1, proj, {"data.bin": binary(300, 2), "empty.bin": b"no longer empty"}, conn=conn)
    edit_step(sid, 2, proj, {"data.bin": binary(50, 3), "empty.bin": binary(7, 4)}, conn=conn)

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert (proj / "data.bin").read_bytes() == original
    assert (proj / "empty.bin").read_bytes() == b""
    assert (proj / "empty.bin").exists()
    assert result.restored == ("data.bin", "empty.bin")
    assert result.deleted == () and result.skipped == () and result.failed == ()
    assert result.to_step == 1 and result.complete


def test_a_zero_length_file_has_a_blob_and_a_row(conn, sid, proj):
    (proj / "empty.bin").write_bytes(b"")
    res = cp.snapshot(sid, 1, ["empty.bin"], proj, conn=conn)
    assert res.recorded == ("empty.bin",)
    mode = stat.S_IMODE((proj / "empty.bin").stat().st_mode)
    assert rows(conn, sid) == [(1, "empty.bin", sha(b""), mode)]
    assert blob_path(sid, b"").read_bytes() == b""


def test_a_file_created_by_a_step_is_deleted_on_rewind(conn, sid, proj):
    (proj / "keep.txt").write_bytes(b"keep")
    res = edit_step(sid, 1, proj, {"new.txt": b"created by the step", "keep.txt": b"changed"}, conn=conn)
    assert res.absent == ("new.txt",) and res.recorded == ("keep.txt",)
    assert rows(conn, sid)[1] == (1, "new.txt", None, None)  # "did not exist": no blob, no mode

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert not (proj / "new.txt").exists()
    assert (proj / "keep.txt").read_bytes() == b"keep"
    assert result.deleted == ("new.txt",)
    assert result.restored == ("keep.txt",)


def test_rewind_deletes_the_created_file_but_leaves_its_new_directories(conn, sid, proj):
    edit_step(sid, 1, proj, {"newdir/sub/x.py": b"print(1)"}, conn=conn)
    assert (proj / "newdir/sub/x.py").exists()
    result = cp.rewind(sid, 1, proj, conn=conn)
    assert result.deleted == ("newdir/sub/x.py",)
    assert not (proj / "newdir/sub/x.py").exists()
    assert (proj / "newdir/sub").is_dir()  # directories are never removed: they may hold other things


def test_a_file_deleted_by_a_step_is_restored(conn, sid, proj):
    data = binary(500, 5)
    (proj / "old.bin").write_bytes(data)
    edit_step(sid, 1, proj, {"old.bin": None}, conn=conn)
    assert not (proj / "old.bin").exists()

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert (proj / "old.bin").read_bytes() == data
    assert result.restored == ("old.bin",)


def test_a_file_whose_whole_directory_was_deleted_is_restored_with_its_directory(conn, sid, proj):
    (proj / "d").mkdir()
    (proj / "d" / "a.txt").write_bytes(b"inside d")
    cp.snapshot(sid, 1, ["d/a.txt"], proj, conn=conn)
    (proj / "d" / "a.txt").unlink()
    (proj / "d").rmdir()

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert (proj / "d" / "a.txt").read_bytes() == b"inside d"
    assert result.restored == ("d/a.txt",)


@pytest.mark.parametrize("mode", [0o644, 0o755, 0o640, 0o400])
def test_the_file_mode_is_preserved(conn, sid, proj, mode):
    f = proj / "script.sh"
    f.write_bytes(b"#!/bin/sh\n")
    os.chmod(f, mode)
    cp.snapshot(sid, 1, ["script.sh"], proj, conn=conn)
    os.chmod(f, 0o600)
    f.write_bytes(b"changed")

    cp.rewind(sid, 1, proj, conn=conn)

    assert f.read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE(f.stat().st_mode) == mode


def test_the_mode_is_restored_even_when_the_file_was_deleted(conn, sid, proj):
    f = proj / "tool"
    f.write_bytes(b"x")
    os.chmod(f, 0o750)
    cp.snapshot(sid, 1, ["tool"], proj, conn=conn)
    f.unlink()
    cp.rewind(sid, 1, proj, conn=conn)
    assert stat.S_IMODE(f.stat().st_mode) == 0o750


def test_a_rewind_to_the_middle_step_leaves_earlier_steps_intact(conn, sid, proj):
    versions = [binary(64, 10 + i) for i in range(4)]
    (proj / "a.bin").write_bytes(versions[0])
    for n in (1, 2, 3):
        edit_step(sid, n, proj, {"a.bin": versions[n]}, conn=conn)

    cp.rewind(sid, 2, proj, conn=conn)  # the state just before step 2 ran
    assert (proj / "a.bin").read_bytes() == versions[1]
    assert [r[0] for r in rows(conn, sid)] == [1]  # step 1 is still there, steps 2 and 3 are consumed

    cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.bin").read_bytes() == versions[0]
    assert rows(conn, sid) == []


def test_two_steps_touching_the_same_file_restore_to_the_right_state(conn, sid, proj):
    v0, v1, v2, v3 = (binary(32, 20 + i) for i in range(4))
    (proj / "a.bin").write_bytes(v0)
    (proj / "b.bin").write_bytes(b"b0")
    edit_step(sid, 1, proj, {"a.bin": v1}, conn=conn)          # a: v0 -> v1
    edit_step(sid, 2, proj, {"b.bin": b"b1"}, conn=conn)       # a untouched
    edit_step(sid, 3, proj, {"a.bin": v2}, conn=conn)          # a: v1 -> v2
    (proj / "a.bin").write_bytes(v3)                           # and something else changed it afterwards

    cp.rewind(sid, 2, proj, conn=conn)                         # before step 2: a is v1, b is b0
    assert (proj / "a.bin").read_bytes() == v1
    assert (proj / "b.bin").read_bytes() == b"b0"

    cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.bin").read_bytes() == v0


def test_a_second_snapshot_of_the_same_path_in_the_same_step_keeps_the_first(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"before the step")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    (proj / "a.txt").write_bytes(b"half way through the step")
    again = cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)  # e.g. two edits in one step

    assert again.already_recorded == ("a.txt",) and again.recorded == ()
    (proj / "a.txt").write_bytes(b"end of the step")
    cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.txt").read_bytes() == b"before the step"  # the pre-STEP state, not a mid-step one


def test_an_unchanged_file_is_reported_as_unchanged_and_not_rewritten(conn, sid, proj):
    f = proj / "a.txt"
    f.write_bytes(b"same")
    cp.snapshot(sid, 1, ["a.txt", "later.txt"], proj, conn=conn)
    before = f.stat()

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert result.unchanged == ("a.txt", "later.txt") and result.restored == () and result.deleted == ()
    after = f.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_rewind_with_nothing_recorded_is_a_quiet_no_op(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"x")
    result = cp.rewind(sid, 5, proj, conn=conn)
    assert (result.restored, result.deleted, result.unchanged, result.skipped) == ((), (), (), ())
    assert result.conversation_truncated is False
    assert (proj / "a.txt").read_bytes() == b"x"


def test_snapshot_of_no_paths_records_nothing(conn, sid, proj):
    res = cp.snapshot(sid, 1, [], proj, conn=conn)
    assert res.recorded == () and res.complete
    assert rows(conn, sid) == []


def test_the_module_opens_its_own_connection_when_none_is_given(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"one")
    cp.snapshot(sid, 1, ["a.txt"], proj)  # no conn=
    (proj / "a.txt").write_bytes(b"two")
    cp.rewind(sid, 1, proj)
    assert (proj / "a.txt").read_bytes() == b"one"


def test_paths_may_be_absolute_pathlike_and_are_deduplicated(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"a")
    res = cp.snapshot(sid, 1, ["a.txt", "./a.txt", ".//a.txt", proj / "a.txt", str(proj / "a.txt")], proj, conn=conn)
    assert res.recorded == ("a.txt",)
    assert [r[1] for r in rows(conn, sid)] == ["a.txt"]


def test_a_bare_string_is_not_treated_as_a_list_of_characters(conn, sid, proj):
    with pytest.raises(ValueError):
        cp.snapshot(sid, 1, "a.txt", proj, conn=conn)


@pytest.mark.parametrize("bad_step", [-1, True, 1.5, "1", None])
def test_a_bad_step_is_rejected(conn, sid, proj, bad_step):
    with pytest.raises(ValueError):
        cp.snapshot(sid, bad_step, ["a.txt"], proj, conn=conn)
    with pytest.raises(ValueError):
        cp.rewind(sid, bad_step, proj, conn=conn)


# --------------------------------------------------------------------------
# Symlinks: never snapshot or restore through one
# --------------------------------------------------------------------------
def test_symlinks_are_skipped_by_snapshot_and_reported(conn, sid, proj, outside):
    (proj / "real.txt").write_bytes(b"real")
    os.symlink("real.txt", proj / "in_project_link")
    os.symlink(outside / "secret.txt", proj / "out_link")
    os.symlink("missing-target", proj / "dangling")

    res = cp.snapshot(sid, 1, ["in_project_link", "out_link", "dangling", "real.txt"], proj, conn=conn)

    assert res.recorded == ("real.txt",)
    assert sorted(res.skipped) == sorted(
        [cp.Skipped("in_project_link", cp.SKIP_SYMLINK), cp.Skipped("out_link", cp.SKIP_SYMLINK),
         cp.Skipped("dangling", cp.SKIP_SYMLINK)]
    )
    assert not res.complete
    assert [r[1] for r in rows(conn, sid)] == ["real.txt"]       # no row for a link
    assert blob_names(sid) == [sha(b"real")]                      # and its target's bytes were never read
    assert (outside / "secret.txt").read_bytes() == b"OUTSIDE-ORIGINAL"


def test_a_target_that_became_a_symlink_is_not_written_through(conn, sid, proj, outside):
    (proj / "a.txt").write_bytes(b"original")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    (proj / "a.txt").unlink()
    os.symlink(outside / "secret.txt", proj / "a.txt")

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert result.skipped == (cp.Skipped("a.txt", cp.SKIP_SYMLINK),)
    assert result.restored == ()
    assert (outside / "secret.txt").read_bytes() == b"OUTSIDE-ORIGINAL"  # nothing went through the link
    assert (proj / "a.txt").is_symlink()                                    # and the link itself was not replaced


def test_a_created_file_that_became_a_symlink_is_not_deleted_through_it(conn, sid, proj, outside):
    cp.snapshot(sid, 1, ["new.txt"], proj, conn=conn)  # did not exist
    os.symlink(outside / "secret.txt", proj / "new.txt")

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert result.skipped == (cp.Skipped("new.txt", cp.SKIP_SYMLINK),)
    assert result.deleted == ()
    assert (outside / "secret.txt").exists() and (proj / "new.txt").is_symlink()


def test_a_directory_that_became_a_symlink_is_not_written_through(conn, sid, proj, outside):
    (proj / "d").mkdir()
    (proj / "d" / "a.txt").write_bytes(b"original")
    cp.snapshot(sid, 1, ["d/a.txt"], proj, conn=conn)
    (proj / "d" / "a.txt").unlink()
    (proj / "d").rmdir()
    os.symlink(outside, proj / "d")  # d now leads outside the project
    (outside / "a.txt").write_bytes(b"OUTSIDE-A")

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert result.skipped == (cp.Skipped("d/a.txt", cp.SKIP_SYMLINKED_DIR),)
    assert (outside / "a.txt").read_bytes() == b"OUTSIDE-A"
    assert sorted(os.listdir(outside)) == ["a.txt", "secret.txt"]


def test_a_symlinked_directory_inside_the_project_is_skipped_not_followed(conn, sid, proj):
    (proj / "real").mkdir()
    (proj / "real" / "f.txt").write_bytes(b"f")
    os.symlink("real", proj / "alias")

    res = cp.snapshot(sid, 1, ["alias/f.txt"], proj, conn=conn)

    assert res.skipped == (cp.Skipped("alias/f.txt", cp.SKIP_SYMLINKED_DIR),)
    assert rows(conn, sid) == [] and blob_names(sid) == []


# --------------------------------------------------------------------------
# Paths that leave the project are refused, and nothing is recorded
# --------------------------------------------------------------------------
def test_paths_outside_the_root_are_refused(conn, sid, proj, outside):
    (proj / "ok.txt").write_bytes(b"ok")
    for bad in ("../outside/secret.txt", str(outside / "secret.txt"), "/etc/hostname", "a/../../x"):
        with pytest.raises(cp.PathRefusedError):
            cp.snapshot(sid, 1, ["ok.txt", bad], proj, conn=conn)
    # all or nothing: the good path in the same call was not recorded either
    assert rows(conn, sid) == [] and blob_names(sid) == []


def test_a_sibling_directory_with_the_same_prefix_is_not_inside_the_root(conn, sid, proj, tmp_path):
    sibling = tmp_path / "proj-evil"
    sibling.mkdir()
    (sibling / "x.txt").write_bytes(b"x")
    with pytest.raises(cp.PathRefusedError):
        cp.snapshot(sid, 1, [str(sibling / "x.txt")], proj, conn=conn)


def test_a_path_that_escapes_through_a_symlinked_directory_is_refused(conn, sid, proj, outside):
    os.symlink(outside, proj / "linkdir")
    (proj / "ok.txt").write_bytes(b"ok")
    with pytest.raises(cp.PathRefusedError):
        cp.snapshot(sid, 1, ["ok.txt", "linkdir/secret.txt"], proj, conn=conn)
    assert rows(conn, sid) == [] and blob_names(sid) == []
    assert (outside / "secret.txt").read_bytes() == b"OUTSIDE-ORIGINAL"


@pytest.mark.parametrize("bad", ["", ".", "./", "a/", "\0", "a\0b", "x" * 5000, None, 5, b"bytes"])
def test_malformed_paths_are_refused(conn, sid, proj, bad):
    with pytest.raises(cp.PathRefusedError):
        cp.snapshot(sid, 1, [bad], proj, conn=conn)


def test_dotdot_is_refused_even_when_the_path_would_stay_inside(conn, sid, proj):
    """'..' is ambiguous next to a symlink (the OS and a lexical clean-up disagree), so it is not accepted."""
    (proj / "sub").mkdir()
    (proj / "b.txt").write_bytes(b"b")
    with pytest.raises(cp.PathRefusedError):
        cp.snapshot(sid, 1, ["sub/../b.txt"], proj, conn=conn)


def test_the_root_itself_cannot_be_snapshotted(conn, sid, proj):
    with pytest.raises(cp.PathRefusedError):
        cp.snapshot(sid, 1, [str(proj)], proj, conn=conn)


def test_directories_and_special_files_are_skipped_not_hung_on_or_followed(conn, sid, proj):
    (proj / "adir").mkdir()
    os.mkfifo(proj / "pipe")
    res = cp.snapshot(sid, 1, ["adir", "pipe"], proj, conn=conn)  # must not block on the FIFO
    assert sorted(res.skipped) == [cp.Skipped("adir", cp.SKIP_NOT_REGULAR), cp.Skipped("pipe", cp.SKIP_NOT_REGULAR)]
    assert rows(conn, sid) == []


def test_a_parent_that_is_a_file_is_skipped(conn, sid, proj):
    (proj / "f").write_bytes(b"i am a file")
    res = cp.snapshot(sid, 1, ["f/child.txt"], proj, conn=conn)
    assert res.skipped == (cp.Skipped("f/child.txt", cp.SKIP_PARENT_NOT_DIR),)


def test_a_directory_in_the_way_at_rewind_time_is_not_replaced(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"was a file")
    cp.snapshot(sid, 1, ["a.txt", "new.txt"], proj, conn=conn)
    (proj / "a.txt").unlink()
    (proj / "a.txt").mkdir()
    (proj / "a.txt" / "keep.txt").write_bytes(b"precious")
    (proj / "new.txt").mkdir()

    result = cp.rewind(sid, 1, proj, conn=conn)

    assert sorted(result.skipped) == [cp.Skipped("a.txt", cp.SKIP_IS_DIRECTORY), cp.Skipped("new.txt", cp.SKIP_IS_DIRECTORY)]
    assert (proj / "a.txt" / "keep.txt").read_bytes() == b"precious"
    assert (proj / "new.txt").is_dir()


@NOT_ROOT
def test_an_unreadable_file_is_skipped(conn, sid, proj):
    f = proj / "secret.txt"
    f.write_bytes(b"x")
    os.chmod(f, 0)
    try:
        res = cp.snapshot(sid, 1, ["secret.txt"], proj, conn=conn)
    finally:
        os.chmod(f, 0o600)
    assert res.skipped == (cp.Skipped("secret.txt", cp.SKIP_UNREADABLE),)


def test_a_file_that_changes_while_it_is_read_is_skipped_not_recorded_torn(conn, sid, proj, monkeypatch):
    (proj / "a.txt").write_bytes(b"x" * 1000)
    signatures = iter([(1, 1, 1, 1), (2, 2, 2, 2)])  # "before" and "after" differ
    monkeypatch.setattr(cp, "_stat_signature", lambda fd: next(signatures))

    res = cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)

    assert res.skipped == (cp.Skipped("a.txt", cp.SKIP_CHANGED),)
    assert rows(conn, sid) == [] and blob_names(sid) == []  # and no blob was committed either


def test_a_hard_linked_file_is_snapshotted_by_content(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"linked")
    os.link(proj / "a.txt", proj / "b.txt")
    res = cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    assert res.recorded == ("a.txt",)
    (proj / "a.txt").unlink()  # an edit tool replaces the name; the other link keeps the old inode
    (proj / "a.txt").write_bytes(b"edited")
    cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.txt").read_bytes() == b"linked"


# --------------------------------------------------------------------------
# The root must be the session's own project
# --------------------------------------------------------------------------
def test_a_root_that_is_not_the_sessions_project_is_refused(conn, sid, proj, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.txt").write_bytes(b"belongs to someone else")
    (proj / "a.txt").write_bytes(b"mine")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    (proj / "a.txt").write_bytes(b"mine, edited")

    with pytest.raises(cp.RootError):
        cp.snapshot(sid, 2, ["a.txt"], other.resolve(), conn=conn)
    with pytest.raises(cp.RootError):
        cp.rewind(sid, 1, other.resolve(), conn=conn)

    assert (other / "a.txt").read_bytes() == b"belongs to someone else"  # the wrong tree was not touched
    assert len(rows(conn, sid)) == 1


def test_the_root_may_be_reached_through_a_symlink_to_the_project(conn, sid, proj, tmp_path):
    link = tmp_path / "alias-of-proj"
    os.symlink(proj, link)
    (proj / "a.txt").write_bytes(b"one")
    cp.snapshot(sid, 1, ["a.txt"], link, conn=conn)  # resolved once, then walked without following
    (proj / "a.txt").write_bytes(b"two")
    cp.rewind(sid, 1, link, conn=conn)
    assert (proj / "a.txt").read_bytes() == b"one"


@pytest.mark.parametrize("make", ["missing", "file", "slash"])
def test_an_unusable_root_is_refused(conn, sid, proj, tmp_path, make):
    if make == "missing":
        bad = tmp_path / "does-not-exist"
    elif make == "file":
        bad = tmp_path / "a-file"
        bad.write_bytes(b"x")
    else:
        bad = Path("/")
    with pytest.raises(cp.RootError):
        cp.snapshot(sid, 1, ["a.txt"], bad, conn=conn)
    with pytest.raises(cp.RootError):
        cp.rewind(sid, 1, bad, conn=conn)


def test_a_root_inside_m2cs_own_state_directory_is_refused(conn, tmp_path):
    inside = m2c_paths.prepare_config_dir() / "somewhere"
    inside.mkdir()
    s = sessions.create_session(conn, project_path=inside, mode="ask")
    with pytest.raises(cp.RootError):
        cp.snapshot(s.id, 1, ["a.txt"], inside, conn=conn)


def test_an_unknown_session_is_refused_and_leaves_nothing_behind(conn, proj):
    (proj / "a.txt").write_bytes(b"x")
    with pytest.raises(sessions.UnknownSessionError):
        cp.snapshot("no-such-session", 1, ["a.txt"], proj, conn=conn)
    with pytest.raises(sessions.UnknownSessionError):
        cp.rewind("no-such-session", 1, proj, conn=conn)
    assert not (m2c_paths.config_dir() / "checkpoints").exists()


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "x" * 65, "short", "bad id!!", None, 5])
def test_a_session_id_that_could_be_a_path_is_rejected_before_anything_is_touched(conn, proj, bad):
    with pytest.raises(ValueError):
        cp.snapshot(bad, 1, ["a.txt"], proj, conn=conn)
    with pytest.raises(ValueError):
        cp.rewind(bad, 1, proj, conn=conn)
    assert not (m2c_paths.config_dir() / "checkpoints").exists()


# --------------------------------------------------------------------------
# Blobs: content-addressed, deduplicated, private
# --------------------------------------------------------------------------
def test_blobs_are_content_addressed_and_deduplicated(conn, sid, proj):
    same = binary(1000, 30)
    (proj / "one.bin").write_bytes(same)
    (proj / "two.bin").write_bytes(same)                       # same content, other name
    first = cp.snapshot(sid, 1, ["one.bin", "two.bin"], proj, conn=conn)
    assert first.blobs_written == 1 and first.blobs_reused == 1
    assert blob_names(sid) == [sha(same)]
    assert blob_path(sid, same).read_bytes() == same

    second = cp.snapshot(sid, 2, ["one.bin"], proj, conn=conn)  # the same content at another step
    assert second.blobs_written == 0 and second.blobs_reused == 1
    assert blob_names(sid) == [sha(same)]
    assert [r[2] for r in rows(conn, sid)] == [sha(same)] * 3   # three rows, one blob

    (proj / "one.bin").write_bytes(b"different")
    cp.snapshot(sid, 3, ["one.bin"], proj, conn=conn)
    assert blob_names(sid) == sorted([sha(same), sha(b"different")])


def test_blobs_are_kept_per_session(conn, proj):
    a = sessions.create_session(conn, project_path=proj, mode="ask").id
    b = sessions.create_session(conn, project_path=proj, mode="ask").id
    (proj / "f.txt").write_bytes(b"shared content")
    cp.snapshot(a, 1, ["f.txt"], proj, conn=conn)
    cp.snapshot(b, 1, ["f.txt"], proj, conn=conn)
    assert blob_names(a) == blob_names(b) == [sha(b"shared content")]
    assert blob_path(a, b"shared content") != blob_path(b, b"shared content")


def test_an_existing_corrupt_blob_is_replaced_when_the_same_content_is_snapshotted_again(conn, sid, proj):
    data = binary(200, 40)
    (proj / "a.bin").write_bytes(data)
    cp.snapshot(sid, 1, ["a.bin"], proj, conn=conn)
    blob_path(sid, data).write_bytes(b"rotted on disk")        # same name, wrong bytes

    res = cp.snapshot(sid, 2, ["a.bin"], proj, conn=conn)       # dedup must verify, not trust the name

    assert res.blobs_written == 1 and res.blobs_reused == 0
    assert blob_path(sid, data).read_bytes() == data


def test_the_blob_directories_are_0700_and_the_blobs_0600_whatever_the_umask(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"secret-ish project content")
    old = os.umask(0)
    try:
        cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    finally:
        os.umask(old)
    top = m2c_paths.config_dir() / "checkpoints"
    assert stat.S_IMODE(top.stat().st_mode) == 0o700
    assert stat.S_IMODE(blob_dir(sid).stat().st_mode) == 0o700
    for name in blob_names(sid):
        assert stat.S_IMODE((blob_dir(sid) / name).stat().st_mode) == 0o600
    assert [n for n in os.listdir(blob_dir(sid)) if not HEX64.match(n)] == []  # no temp file left


def test_a_looser_existing_directory_is_tightened(conn, sid, proj):
    d = blob_dir(sid)
    d.mkdir(parents=True)
    os.chmod(d, 0o755)
    os.chmod(d.parent, 0o755)
    (proj / "a.txt").write_bytes(b"x")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(d.parent.stat().st_mode) == 0o700


def test_a_symlink_in_place_of_the_session_directory_is_refused(conn, sid, proj, outside):
    top = m2c_paths.prepare_config_dir() / "checkpoints"
    top.mkdir(mode=0o700)
    os.symlink(outside, top / sid)
    (proj / "a.txt").write_bytes(b"x")
    with pytest.raises(cp.CheckpointError):
        cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    assert sorted(os.listdir(outside)) == ["secret.txt"]  # nothing was written through the link
    assert rows(conn, sid) == []


# --------------------------------------------------------------------------
# The size cap
# --------------------------------------------------------------------------
def test_the_default_cap_covers_everything_the_edit_tools_can_edit_or_write():
    caps = Caps()
    assert cp.MAX_SNAPSHOT_BYTES >= caps.max_file_bytes
    assert cp.MAX_SNAPSHOT_BYTES >= caps.max_write_bytes


def test_a_file_above_the_cap_is_skipped_with_a_reason_the_caller_can_act_on(conn, sid, proj):
    (proj / "small.bin").write_bytes(b"x" * 10)
    (proj / "big.bin").write_bytes(b"y" * 11)
    res = cp.snapshot(sid, 1, ["small.bin", "big.bin"], proj, conn=conn, max_file_bytes=10)

    assert res.recorded == ("small.bin",)                       # exactly at the cap is fine
    assert res.skipped == (cp.Skipped("big.bin", cp.SKIP_TOO_LARGE),)
    assert res.complete is False                                # the caller can refuse the edit or warn
    assert [r[1] for r in rows(conn, sid)] == ["small.bin"]
    assert blob_names(sid) == [sha(b"x" * 10)]


def test_a_file_that_grows_past_the_cap_while_being_read_is_skipped(conn, sid, proj, monkeypatch):
    """The size is checked up front *and* while streaming: a file that claims to be small is still bounded."""
    (proj / "grow.bin").write_bytes(b"z" * 8)
    monkeypatch.setattr(cp, "_stat_signature", lambda fd: (2, 1, 1, 1))  # the up-front check sees "2 bytes"
    res = cp.snapshot(sid, 1, ["grow.bin"], proj, conn=conn, max_file_bytes=4)
    assert res.skipped == (cp.Skipped("grow.bin", cp.SKIP_TOO_LARGE),)
    assert blob_names(sid) == [] and rows(conn, sid) == []


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10"])
def test_a_bad_cap_is_rejected(conn, sid, proj, bad):
    with pytest.raises(ValueError):
        cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn, max_file_bytes=bad)


# --------------------------------------------------------------------------
# A corrupt or tampered checkpoint is refused BEFORE anything is written
# --------------------------------------------------------------------------
def _two_files_edited(conn, sid, proj):
    a, b = binary(300, 50), binary(300, 51)
    (proj / "a.bin").write_bytes(a)
    (proj / "b.bin").write_bytes(b)
    edit_step(sid, 1, proj, {"a.bin": b"a edited", "b.bin": b"b edited"}, conn=conn)
    return a, b


def _nothing_was_written(proj, conn, sid):
    assert (proj / "a.bin").read_bytes() == b"a edited"
    assert (proj / "b.bin").read_bytes() == b"b edited"
    assert len(rows(conn, sid)) == 2                             # the index was not pruned
    assert not [n for n in os.listdir(proj) if n.startswith(".m2c")]  # and no temp file was left


@pytest.mark.parametrize("damaged", ["a.bin", "b.bin"])
def test_a_corrupt_blob_is_detected_and_nothing_is_written(conn, sid, proj, damaged):
    """Either file may be the damaged one.  When it is b.bin (restored LAST), a rewind without the
    read-only preflight would already have put a.bin back: this is what proves the preflight."""
    originals = dict(zip(["a.bin", "b.bin"], _two_files_edited(conn, sid, proj)))
    data = originals[damaged]
    blob_path(sid, data).write_bytes(bytes([data[0] ^ 1]) + data[1:])     # one flipped bit, same size

    with pytest.raises(cp.CorruptCheckpointError) as info:
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)

    assert damaged in str(info.value)
    _nothing_was_written(proj, conn, sid)                        # the intact file was not restored either
    assert events.read_events(conn, sid) == []                   # and no marker


@pytest.mark.parametrize("damaged", ["a.bin", "b.bin"])
def test_a_truncated_blob_is_detected(conn, sid, proj, damaged):
    data = dict(zip(["a.bin", "b.bin"], _two_files_edited(conn, sid, proj)))[damaged]
    blob_path(sid, data).write_bytes(data[:10])
    with pytest.raises(cp.CorruptCheckpointError):
        cp.rewind(sid, 1, proj, conn=conn)
    _nothing_was_written(proj, conn, sid)


@pytest.mark.parametrize("damaged", ["a.bin", "b.bin"])
def test_a_missing_blob_is_detected(conn, sid, proj, damaged):
    data = dict(zip(["a.bin", "b.bin"], _two_files_edited(conn, sid, proj)))[damaged]
    blob_path(sid, data).unlink()
    with pytest.raises(cp.CorruptCheckpointError, match="missing"):
        cp.rewind(sid, 1, proj, conn=conn)
    _nothing_was_written(proj, conn, sid)


def test_a_blob_replaced_by_a_symlink_is_not_followed(conn, sid, proj, outside):
    a, _b = _two_files_edited(conn, sid, proj)
    target = blob_path(sid, a)
    target.unlink()
    (outside / "planted").write_bytes(a)                         # even with the right bytes behind the link
    os.symlink(outside / "planted", target)
    with pytest.raises(cp.CorruptCheckpointError):
        cp.rewind(sid, 1, proj, conn=conn)
    _nothing_was_written(proj, conn, sid)


@pytest.mark.parametrize(
    "column,value",
    [
        ("blob_hash", "../../outside/secret.txt"),
        ("blob_hash", "ABCDEF"),
        ("blob_hash", "0" * 63),
        ("path", "../escape.txt"),
        ("path", "/etc/escape.txt"),
        ("path", "a/../../escape.txt"),
        ("path", ""),
        ("path", "a//b"),
        ("mode", None),
        ("mode", -1),
        ("mode", 0o20000),
        ("mode", "rw"),
    ],
)
def test_a_tampered_index_row_is_refused_before_anything_is_written(conn, sid, proj, tmp_path, column, value):
    _two_files_edited(conn, sid, proj)
    conn.execute(f"UPDATE checkpoints SET {column} = ? WHERE session_id = ? AND path = 'a.bin'", (value, sid))
    with pytest.raises(cp.CorruptCheckpointError):
        cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.bin").read_bytes() == b"a edited"
    assert (proj / "b.bin").read_bytes() == b"b edited"  # the intact row was not applied either
    assert not (tmp_path / "escape.txt").exists() and not Path("/etc/escape.txt").exists()


def test_a_blob_that_goes_bad_after_the_check_is_caught_when_it_is_copied(conn, sid, proj, monkeypatch):
    """The preflight and the copy both verify: a blob damaged in between is never written."""
    a, _b = _two_files_edited(conn, sid, proj)
    monkeypatch.setattr(cp, "_verify_blobs", lambda *args, **kw: None)  # skip the preflight
    blob_path(sid, a).write_bytes(b"x" * len(a))
    with pytest.raises(cp.CorruptCheckpointError):
        cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.bin").read_bytes() == b"a edited"
    assert not [n for n in os.listdir(proj) if n.startswith(".m2c")]    # the temp file was removed


# --------------------------------------------------------------------------
# An interrupted snapshot leaves no half state
# --------------------------------------------------------------------------
def test_failure_after_the_blobs_and_before_the_index_leaves_no_half_state(conn, sid, proj, faults):
    a, b = binary(100, 60), binary(100, 61)
    (proj / "a.bin").write_bytes(a)
    (proj / "b.bin").write_bytes(b)

    def boom(*args, **kwargs):
        raise RuntimeError("injected: the process dies after the blobs, before the index")

    faults.setattr(cp, "_insert_rows", boom)
    with pytest.raises(RuntimeError, match="injected"):
        cp.snapshot(sid, 1, ["a.bin", "b.bin", "absent.bin"], proj, conn=conn)

    assert rows(conn, sid) == []                                  # nothing recorded for the step
    assert sorted(blob_names(sid)) == sorted([sha(a), sha(b)])    # the orphan blobs are there ...
    assert cp.gc_orphans(sid, conn=conn, min_age_s=0).blobs_removed == 2  # ... and are harmless: gc removes them
    assert blob_names(sid) == []

    faults.undo()
    res = cp.snapshot(sid, 1, ["a.bin", "b.bin", "absent.bin"], proj, conn=conn)  # the same step can be retried
    assert res.recorded == ("a.bin", "b.bin") and res.absent == ("absent.bin",)
    assert len(rows(conn, sid)) == 3


def test_failure_in_the_middle_of_the_index_transaction_rolls_the_whole_step_back(conn, sid, proj):
    for name in ("a.txt", "b.txt", "c.txt"):
        (proj / name).write_bytes(name.encode())
    conn.execute(
        "CREATE TRIGGER fail_on_b BEFORE INSERT ON checkpoints WHEN NEW.path = 'b.txt' "
        "BEGIN SELECT RAISE(ABORT, 'injected: the second row cannot be written'); END"
    )

    with pytest.raises(cp.CheckpointError, match="injected"):
        cp.snapshot(sid, 1, ["a.txt", "b.txt", "c.txt"], proj, conn=conn)

    assert rows(conn, sid) == []                                  # a.txt's row (written first) was rolled back too
    assert not conn.in_transaction

    conn.execute("DROP TRIGGER fail_on_b")
    assert cp.snapshot(sid, 1, ["a.txt", "b.txt", "c.txt"], proj, conn=conn).complete
    assert [r[1] for r in rows(conn, sid)] == ["a.txt", "b.txt", "c.txt"]


def test_a_snapshot_inside_the_callers_transaction_commits_or_rolls_back_with_it(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"x")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
            events.append_event(conn, sid, "edit", {"path": "a.txt"})
            raise RuntimeError("the caller's transaction fails")
    assert rows(conn, sid) == [] and events.read_events(conn, sid) == []


def test_no_blob_directory_or_file_is_left_when_every_path_is_absent(conn, sid, proj):
    res = cp.snapshot(sid, 1, ["nothing.txt"], proj, conn=conn)
    assert res.absent == ("nothing.txt",)
    assert not blob_dir(sid).exists()


# --------------------------------------------------------------------------
# An interrupted rewind can be re-run to completion
# --------------------------------------------------------------------------
def _three_files(conn, sid, proj):
    old = {n: binary(64, 70 + i) for i, n in enumerate(["a.bin", "b.bin", "c.bin"])}
    for n, data in old.items():
        (proj / n).write_bytes(data)
    edit_step(sid, 1, proj, {n: b"edited " + n.encode() for n in old}, conn=conn)
    return old


def _assert_all_old(proj, old):
    for n, data in old.items():
        assert (proj / n).read_bytes() == data


def test_an_interrupted_rewind_can_be_re_run_to_completion(conn, sid, proj, faults):
    old = _three_files(conn, sid, proj)
    real = cp._apply_item
    done = []

    def dies_after_the_first(root, store, item):
        if done:
            raise RuntimeError("injected: interrupted after the first file")
        out = real(root, store, item)
        done.append(item.path)
        return out

    faults.setattr(cp, "_apply_item", dies_after_the_first)
    with pytest.raises(RuntimeError, match="injected"):
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)

    assert (proj / "a.bin").read_bytes() == old["a.bin"]          # one file is back ...
    assert (proj / "b.bin").read_bytes() == b"edited b.bin"       # ... the others are not
    assert len(rows(conn, sid)) == 3                              # the index is untouched, so a re-run has everything
    assert events.read_events(conn, sid) == []                    # and no marker was written
    assert not [n for n in os.listdir(proj) if n.startswith(".m2c")]

    faults.undo()
    result = cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)

    _assert_all_old(proj, old)
    assert result.unchanged == ("a.bin",) and result.restored == ("b.bin", "c.bin")
    assert rows(conn, sid) == []
    assert [e.type for e in events.read_events(conn, sid)] == ["rewind"]


def test_rewind_is_idempotent(conn, sid, proj):
    old = _three_files(conn, sid, proj)
    first = cp.rewind(sid, 1, proj, conn=conn)
    assert first.restored == ("a.bin", "b.bin", "c.bin")
    second = cp.rewind(sid, 1, proj, conn=conn)                   # again: nothing left to do, same state
    assert (second.restored, second.deleted, second.skipped) == ((), (), ())
    _assert_all_old(proj, old)


def test_an_io_error_on_one_file_restores_the_others_and_keeps_the_checkpoints(conn, sid, proj, faults):
    old = _three_files(conn, sid, proj)
    real = cp._apply_item

    def b_cannot_be_written(root, store, item):
        if item.path == "b.bin":
            raise OSError(errno.EIO, "injected I/O error")
        return real(root, store, item)

    faults.setattr(cp, "_apply_item", b_cannot_be_written)
    with pytest.raises(cp.RewindIncompleteError) as info:
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)

    partial = info.value.result
    assert partial.restored == ("a.bin", "c.bin")
    assert partial.failed == (cp.Skipped("b.bin", cp.FAIL_IO),)
    assert not partial.complete
    assert (proj / "b.bin").read_bytes() == b"edited b.bin"
    assert len(rows(conn, sid)) == 3 and events.read_events(conn, sid) == []   # nothing pruned, no marker

    faults.undo()                                             # "the disk is fixed": run it again
    cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)
    _assert_all_old(proj, old)
    assert rows(conn, sid) == [] and [e.type for e in events.read_events(conn, sid)] == ["rewind"]


def test_checkpoints_that_changed_during_the_rewind_are_not_pruned(conn, sid, proj, faults):
    old = _three_files(conn, sid, proj)
    real = cp._apply_item

    def another_writer_snapshots_meanwhile(root, store, item):
        out = real(root, store, item)
        if item.path == "a.bin":
            cp.snapshot(sid, 2, ["c.bin"], proj, conn=conn)       # the loop was still running
        return out

    faults.setattr(cp, "_apply_item", another_writer_snapshots_meanwhile)
    with pytest.raises(cp.CheckpointConflictError):
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)
    assert len(rows(conn, sid)) == 4 and events.read_events(conn, sid) == []

    faults.undo()
    cp.rewind(sid, 1, proj, conn=conn)                             # re-running converges
    _assert_all_old(proj, old)


# --------------------------------------------------------------------------
# The conversation marker
# --------------------------------------------------------------------------
def test_truncate_conversation_appends_a_rewind_event(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"v0")
    events.append_event(conn, sid, "user_message", {"text": "do it"})
    edit_step(sid, 2, proj, {"a.txt": b"v1"}, conn=conn)
    events.append_event(conn, sid, "assistant_message", {"text": "done"})
    before = events.last_seq(conn, sid)

    result = cp.rewind(sid, 2, proj, conn=conn, truncate_conversation=True)

    assert result.conversation_truncated is True
    got = events.read_events(conn, sid)
    assert [e.type for e in got] == ["user_message", "assistant_message", "rewind"]
    assert got[-1].seq == before + 1 and got[-1].data == {"to_step": 2}


def test_without_truncate_conversation_no_event_is_written(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"v0")
    edit_step(sid, 1, proj, {"a.txt": b"v1"}, conn=conn)
    result = cp.rewind(sid, 1, proj, conn=conn)
    assert result.conversation_truncated is False
    assert events.read_events(conn, sid) == []


def test_the_marker_is_written_even_when_no_file_needed_restoring(conn, sid, proj):
    result = cp.rewind(sid, 3, proj, conn=conn, truncate_conversation=True)
    assert result.conversation_truncated is True
    assert [e.data for e in events.read_events(conn, sid)] == [{"to_step": 3}]


def test_pruning_and_the_marker_commit_together(conn, sid, proj, faults):
    (proj / "a.txt").write_bytes(b"v0")
    edit_step(sid, 1, proj, {"a.txt": b"v1"}, conn=conn)

    def boom(*args, **kwargs):
        raise RuntimeError("injected: the event log cannot be written")

    faults.setattr(events, "append_event", boom)
    with pytest.raises(RuntimeError, match="injected"):
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)
    assert len(rows(conn, sid)) == 1                               # the prune was rolled back with it
    assert not conn.in_transaction

    faults.undo()
    second = cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)
    assert second.unchanged == ("a.txt",)                          # the files were already back; nothing re-done
    assert rows(conn, sid) == [] and [e.type for e in events.read_events(conn, sid)] == ["rewind"]


def test_a_failed_rewind_writes_no_marker(conn, sid, proj):
    a, _b = _two_files_edited(conn, sid, proj)
    blob_path(sid, a).unlink()
    with pytest.raises(cp.CorruptCheckpointError):
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)
    assert events.read_events(conn, sid) == []


def test_after_a_rewind_the_same_step_numbers_can_be_used_again(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"v0")
    edit_step(sid, 1, proj, {"a.txt": b"v1"}, conn=conn)
    cp.rewind(sid, 1, proj, conn=conn)
    edit_step(sid, 1, proj, {"a.txt": b"v1 again, different"}, conn=conn)   # a new timeline, step 1 again
    (proj / "a.txt").write_bytes(b"and more")
    cp.rewind(sid, 1, proj, conn=conn)
    assert (proj / "a.txt").read_bytes() == b"v0"                  # the stale first-timeline row did not win


# --------------------------------------------------------------------------
# gc_orphans
# --------------------------------------------------------------------------
def test_gc_removes_only_blobs_no_row_refers_to(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"referenced")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    orphan = b"nobody refers to this"
    (blob_dir(sid) / sha(orphan)).write_bytes(orphan)
    (blob_dir(sid) / "notes.txt").write_bytes(b"not a blob: left alone")

    res = cp.gc_orphans(sid, conn=conn, min_age_s=0)

    assert res.blobs_removed == 1 and res.bytes_freed == len(orphan)
    assert blob_names(sid) == [sha(b"referenced")]
    assert (blob_dir(sid) / "notes.txt").exists()
    (proj / "a.txt").write_bytes(b"edited")
    cp.rewind(sid, 1, proj, conn=conn)                              # the kept blob still works
    assert (proj / "a.txt").read_bytes() == b"referenced"


def test_gc_leaves_a_young_orphan_alone_so_it_cannot_race_a_snapshot_in_flight(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"x")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    young = b"written a moment ago, row not inserted yet"
    (blob_dir(sid) / sha(young)).write_bytes(young)
    assert cp.gc_orphans(sid, conn=conn).blobs_removed == 0         # the default grace period protects it
    assert sha(young) in blob_names(sid)
    old_time = os.stat(blob_dir(sid) / sha(young)).st_mtime - cp.GC_MIN_AGE_S - 5
    os.utime(blob_dir(sid) / sha(young), (old_time, old_time))
    assert cp.gc_orphans(sid, conn=conn).blobs_removed == 1


def test_a_dedup_hit_refreshes_the_blob_age(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"content")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    p = blob_path(sid, b"content")
    os.utime(p, (1, 1))                                             # ancient
    (proj / "b.txt").write_bytes(b"content")
    cp.snapshot(sid, 2, ["b.txt"], proj, conn=conn)                 # reuses it
    assert p.stat().st_mtime > 1_000_000                            # touched: gc will not take it mid-snapshot


def test_gc_after_a_rewind_frees_only_what_the_pruned_steps_alone_used(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"v0")
    edit_step(sid, 1, proj, {"a.txt": b"v1"}, conn=conn)            # blob(v0) for step 1
    edit_step(sid, 2, proj, {"a.txt": b"v2"}, conn=conn)            # blob(v1) for step 2
    cp.rewind(sid, 2, proj, conn=conn)                              # step 2's row is pruned
    assert cp.gc_orphans(sid, conn=conn, min_age_s=0).blobs_removed == 1
    assert blob_names(sid) == [sha(b"v0")]


def test_gc_without_a_session_covers_every_session_directory(conn, proj):
    a = sessions.create_session(conn, project_path=proj, mode="ask").id
    b = sessions.create_session(conn, project_path=proj, mode="ask").id
    for s, content in ((a, b"in a"), (b, b"in b")):
        (proj / "f.txt").write_bytes(content)
        cp.snapshot(s, 1, ["f.txt"], proj, conn=conn)
    conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (b,))   # b's rows are gone
    ghost = "ghost-session-with-a-dir"
    (m2c_paths.config_dir() / "checkpoints" / ghost).mkdir(mode=0o700)
    (m2c_paths.config_dir() / "checkpoints" / ghost / sha(b"x")).write_bytes(b"x")

    res = cp.gc_orphans(conn=conn, min_age_s=0)

    assert res.blobs_removed == 2                                   # b's blob and the ghost's
    assert blob_names(a) == [sha(b"in a")] and blob_names(b) == []


def test_gc_never_follows_a_symlink_in_a_blob_directory(conn, sid, proj, outside):
    (proj / "a.txt").write_bytes(b"x")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    os.symlink(outside / "secret.txt", blob_dir(sid) / sha(b"planted"))
    cp.gc_orphans(sid, conn=conn, min_age_s=0)
    assert (outside / "secret.txt").read_bytes() == b"OUTSIDE-ORIGINAL"


def test_gc_removes_stale_temp_files_but_not_a_live_ones(conn, sid, proj):
    (proj / "a.txt").write_bytes(b"x")
    cp.snapshot(sid, 1, ["a.txt"], proj, conn=conn)
    dead = blob_dir(sid) / ".m2c-ckpt.999999999.0123456789abcdef.tmp"
    live = blob_dir(sid) / f".m2c-ckpt.{os.getpid()}.fedcba9876543210.tmp"
    dead.write_bytes(b"left by a killed process")
    live.write_bytes(b"being written right now")
    res = cp.gc_orphans(sid, conn=conn)
    assert res.temps_removed == 1 and not dead.exists() and live.exists()


def test_gc_with_no_checkpoint_directory_at_all_is_a_no_op(conn):
    res = cp.gc_orphans(conn=conn)
    assert (res.blobs_removed, res.temps_removed, res.bytes_freed) == (0, 0, 0)


# --------------------------------------------------------------------------
# What the user is told, and what is never logged
# --------------------------------------------------------------------------
def test_the_rewind_notice_is_exactly_the_agreed_text():
    assert cp.REWIND_NOTICE == (
        "Rewind restores files in this project only. It does not undo network requests, "
        "installed packages, database changes, git pushes or anything else outside these files."
    )


def test_the_result_carries_the_notice_for_the_ui(conn, sid, proj):
    assert cp.rewind(sid, 1, proj, conn=conn).notice == cp.REWIND_NOTICE


def test_every_skip_reason_has_text_a_person_can_read():
    for reason in (
        cp.SKIP_SYMLINK, cp.SKIP_SYMLINKED_DIR, cp.SKIP_TOO_LARGE, cp.SKIP_NOT_REGULAR, cp.SKIP_CHANGED,
        cp.SKIP_PARENT_NOT_DIR, cp.SKIP_IS_DIRECTORY, cp.SKIP_UNREADABLE, cp.FAIL_IO,
    ):
        text = str(cp.Skipped("some/file.txt", reason))
        assert "some/file.txt" in text and len(text) > len("some/file.txt") + 10


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(f"{record.getMessage()} {record.args!r}")


def test_file_contents_and_names_are_never_logged(conn, sid, proj):
    secret_content = b"MARKER_CONTENT_SHOULD_NEVER_BE_LOGGED"
    (proj / "marker_file_name.txt").write_bytes(secret_content)
    logger = logging.getLogger("m2c")
    handler = _Collect()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        cp.snapshot(sid, 1, ["marker_file_name.txt", "marker_new_name.txt"], proj, conn=conn)
        (proj / "marker_file_name.txt").write_bytes(b"edited")
        cp.rewind(sid, 1, proj, conn=conn, truncate_conversation=True)
        cp.gc_orphans(sid, conn=conn, min_age_s=0)
        with pytest.raises(cp.PathRefusedError):
            cp.snapshot(sid, 2, ["../marker_escape"], proj, conn=conn)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    joined = "\n".join(handler.messages)
    assert "checkpoints." in joined                                 # the test is not vacuous: events were logged
    for forbidden in ("MARKER_CONTENT", "marker_file_name", "marker_new_name", "marker_escape", str(proj)):
        assert forbidden not in joined
