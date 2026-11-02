"""
m2c/tests/runner/test_runner_checkpoints.py -- Patch C24: a checkpoint before every change, and fail closed without one.

Real C08 checkpoints, real C05 file tools.  Product guide 8.x / build plan C08: a call that changes files is
checkpointed first; if the checkpoint cannot be saved the call is refused (fail closed); a path that does not exist
yet is fine; ``bash`` cannot be checkpointed and is not.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from runner_rig import RUN_ID, make_rig, proposed, run

from m2c import checkpoints
from m2c import runner as m2c_runner
from m2c.runner import MSG_CHECKPOINT_FAILED, Choice


def steps(rig) -> list[tuple[int, str, bool]]:
    rows = rig.store.execute("SELECT step, path, blob_hash IS NOT NULL FROM checkpoints WHERE session_id = ? "
                             "ORDER BY step, path", (RUN_ID,))
    return [(step, path, bool(has_blob)) for step, path, has_blob in rows]


def test_an_edit_is_checkpointed_and_a_rewind_restores_the_exact_bytes(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits")
    target = rig.root / "notes.txt"
    original = "line one\r\nline two \u00e9\u00e8 \n".encode()                       # CRLF and non-ASCII text
    target.write_bytes(original)
    target.chmod(0o640)

    async def scenario():
        await rig.call("c1", "read_file", {"path": "notes.txt"})
        await rig.call("c2", "edit_file", {"path": "notes.txt", "old_string": "line one", "new_string": "LINE ONE"}, "R1")

    run(scenario())
    assert rig.link.result("c2")["ok"] is True
    assert target.read_bytes() != original
    assert steps(rig) == [(1, "notes.txt", True)]                               # one row, taken BEFORE the change
    result = checkpoints.rewind(RUN_ID, 1, rig.root, conn=rig.store)
    assert result.restored == ("notes.txt",)
    assert target.read_bytes() == original and (target.stat().st_mode & 0o777) == 0o640


def test_the_step_number_rises_with_every_changing_call_and_reads_take_none(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits", allow=["delete_path(*)"])
    (rig.root / "a.txt").write_text("a\n")

    async def scenario():
        await rig.call("c0", "read_file", {"path": "a.txt"})
        await rig.call("c1", "write_file", {"path": "a.txt", "content": "a2\n"}, "R1")
        await rig.call("c2", "write_file", {"path": "b.txt", "content": "b\n"}, "R1")
        await rig.call("c3", "delete_path", {"path": "b.txt"}, "R1")
    run(scenario())
    assert steps(rig) == [(1, "a.txt", True), (2, "b.txt", False), (3, "b.txt", True)]
    # rewinding to step 1 puts the project back as it was before the first change: a.txt as it was, b.txt gone
    checkpoints.rewind(RUN_ID, 1, rig.root, conn=rig.store)
    assert (rig.root / "a.txt").read_text() == "a\n" and not (rig.root / "b.txt").exists()


def test_a_new_runner_continues_the_step_numbers_of_the_session(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits")
    run(rig.call("c1", "write_file", {"path": "a.txt", "content": "1"}, "R1"))
    from m2c.runner import ToolRunner

    again = ToolRunner(rig.link, run_id=RUN_ID, tool_context=rig.tool_context, policy_context=rig.runner._get_policy,
                       store=rig.store, replay_settle_s=0.0)

    async def scenario():
        again.on_tool_proposed(proposed("c2", "write_file", {"path": "a.txt", "content": "2"}, "R1"))
        await again.wait_idle()

    run(scenario())
    assert [s for s, _, _ in steps(rig)] == [1, 2]                              # not a second "step 1" (that row would win)
    assert rig.link.result("c2")["ok"] is True


def test_a_path_that_does_not_exist_yet_is_fine(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits")
    run(rig.call("c1", "write_file", {"path": "new/dir/file.txt", "content": "hello\n"}, "R1"))
    assert rig.link.result("c1")["ok"] is True
    assert steps(rig) == [(1, "new/dir/file.txt", False)]
    checkpoints.rewind(RUN_ID, 1, rig.root, conn=rig.store)
    assert not (rig.root / "new/dir/file.txt").exists()                         # a rewind removes what the step created


def test_a_failed_snapshot_refuses_the_edit_with_a_fixed_message(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits", max_snapshot_bytes=64)         # anything above 64 bytes cannot be saved
    big = rig.root / "big.txt"
    big.write_text("x" * 500 + "\n")

    async def scenario():
        await rig.call("c1", "read_file", {"path": "big.txt"})
        await rig.call("c2", "edit_file", {"path": "big.txt", "old_string": "x" * 10, "new_string": "y" * 10}, "R1")
    run(scenario())
    result = rig.link.result("c2")
    assert result["ok"] is False and result["output"] == MSG_CHECKPOINT_FAILED
    assert big.read_text() == "x" * 500 + "\n"                                   # not run
    assert rig.link.approval("c2")["decision"] == "allow"                        # it was allowed; the checkpoint refused it
    assert "big.txt" not in result["output"]                                     # a fixed message, no path


def test_a_snapshot_that_raises_refuses_the_call(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, mode="accept-edits", allow=["delete_path(*)"])
    (rig.root / "a.txt").write_text("keep\n")

    def boom(*args, **kwargs):
        raise checkpoints.CheckpointError("the disk is full, see /secret/place")

    monkeypatch.setattr("m2c.runner.checkpoints.snapshot", boom)

    async def scenario():
        await rig.call("c1", "read_file", {"path": "a.txt"})
        await rig.call("c2", "write_file", {"path": "a.txt", "content": "lost\n"}, "R1")
        await rig.call("c3", "delete_path", {"path": "a.txt"}, "R1")
    run(scenario())
    for call_id in ("c2", "c3"):
        assert rig.link.result(call_id) == {**rig.link.result(call_id), "ok": False, "output": MSG_CHECKPOINT_FAILED}
    assert (rig.root / "a.txt").read_text() == "keep\n"


def test_a_snapshot_that_skips_the_path_refuses_the_call(tmp_path):
    """A symlink cannot be checkpointed (C08 skips it): deleting it is refused, not done without a net."""
    rig = make_rig(tmp_path, mode="accept-edits", allow=["delete_path(*)"])
    (rig.root / "real.txt").write_text("real\n")
    (rig.root / "link.txt").symlink_to("real.txt")
    run(rig.call("c1", "delete_path", {"path": "link.txt"}, "R1"))
    assert rig.link.result("c1")["output"] == MSG_CHECKPOINT_FAILED
    assert (rig.root / "link.txt").is_symlink()


def test_a_refused_call_does_not_stop_the_next_one(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits", max_snapshot_bytes=64, allow=["delete_path(*)"])
    (rig.root / "big.txt").write_text("x" * 500)

    async def scenario():
        await rig.call("c1", "delete_path", {"path": "big.txt"}, "R1")
        await rig.call("c2", "write_file", {"path": "small.txt", "content": "ok"}, "R1")
    run(scenario())
    assert rig.link.result("c1")["ok"] is False and rig.link.result("c2")["ok"] is True


def test_bash_is_not_checkpointed_and_says_so_honestly(tmp_path):
    """Nothing can say what a shell command will touch: no rows are written for it (REWIND_NOTICE is the user-facing
    statement; see the runner's docstring)."""
    rig = make_rig(tmp_path, allow=["bash(echo:*)", "bash(touch:*)"])
    run(rig.call("c1", "bash", {"command": "echo hi"}, "R2"))
    assert rig.link.result("c1")["ok"] is True and steps(rig) == []
    assert "cannot be checkpointed" in m2c_runner.__doc__


def test_a_read_and_a_denied_edit_take_no_checkpoint(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("deny"))
    (rig.root / "a.txt").write_text("a\n")

    async def scenario():
        await rig.call("c1", "read_file", {"path": "a.txt"})
        await rig.call("c2", "write_file", {"path": "a.txt", "content": "b\n"}, "R1")
    run(scenario())
    assert steps(rig) == []


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".git/config"])
def test_changes_outside_the_project_or_to_protected_paths_never_reach_the_checkpoint(tmp_path, path):
    rig = make_rig(tmp_path, mode="accept-edits", ask_fn=lambda request: Choice("allow"))
    run(rig.call("c1", "write_file", {"path": path, "content": "x"}, "R1"))
    assert steps(rig) == []
    outcomes = [k for k, _ in rig.link.for_call("c1")]
    assert "tool_result" not in outcomes or rig.link.result("c1")["ok"] is False
    assert not Path(tmp_path / "outside.txt").exists()
