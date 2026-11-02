"""
m2c/tests/runner/test_runner_ordering.py -- Patch C24: idempotency, replay, cancellation, concurrency, prompts.

The shape of the rules (protocol/README.md section 5, fixture 03):

* a ``call_id`` is executed at most once, however often it is delivered, and not at all when the server has already
  closed it (a later ``tool_finished`` in the replay, or in the local log after a restart);
* cancelling kills a running shell command; a call the server closed is not answered;
* reads run side by side, anything that changes something runs alone and in arrival order;
* the person is asked one question at a time.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from runner_rig import RUN_ID, finished, make_rig, process_running, proposed, run, state_changed

from m2c.link import LinkClosedError, LinkMessageTooLargeError, LinkNotConnectedError
from m2c.runner import Choice, ToolRunner
from m2c.store import append_event
from m2c.tools import ToolResult


def executor_that(fn):
    """An injected stand-in for ``run_tool``: ``fn(name, args)`` returns the output text."""

    def execute(context, name, args, *, call_env=None):
        return ToolResult(True, fn(name, args), False, "local_project", None, 1)

    return execute


# ---- never twice --------------------------------------------------------------------------------------------

def test_a_duplicate_proposal_for_a_call_in_flight_runs_it_once(tmp_path):
    gate, entered, calls = threading.Event(), threading.Event(), []

    def slow(name, args):
        calls.append(name)
        entered.set()
        assert gate.wait(10)
        return "done"

    rig = make_rig(tmp_path, executor=executor_that(slow))
    (rig.root / "a.txt").write_text("x")

    async def scenario():
        rig.propose("c1", "read_file", {"path": "a.txt"})
        await asyncio.to_thread(entered.wait, 10)
        rig.propose("c1", "read_file", {"path": "a.txt"})                       # delivered again while it runs
        rig.propose("c1", "read_file", {"path": "a.txt"})
        gate.set()
        await rig.runner.wait_idle()
        rig.propose("c1", "read_file", {"path": "a.txt"})                       # and again after it finished
        await rig.runner.wait_idle()
    run(scenario())
    assert calls == ["read_file"]
    assert len(rig.link.payloads("tool_result")) == 1 and len(rig.link.payloads("approval")) == 1


def test_a_replayed_proposal_is_skipped_when_a_later_tool_finished_is_in_the_replay(tmp_path):
    """Fixture 03: the replay carries tool_proposed(seq 5) and then tool_finished(disconnected, seq 6)."""
    ran = []
    rig = make_rig(tmp_path, executor=executor_that(lambda n, a: ran.append(n) or "x"))
    (rig.root / "a.txt").write_text("x")

    async def scenario():
        rig.runner.on_tool_proposed(proposed("call_grep_1", "read_file", {"path": "a.txt"}, seq=5))
        rig.runner.observe(finished("call_grep_1", "read_file", "disconnected", seq=6))     # next frame of the burst
        await rig.runner.wait_idle()
        # the retry the model makes after the resume is a NEW call and runs
        rig.propose("call_grep_2", "read_file", {"path": "a.txt"}, seq=13)
        await rig.runner.wait_idle()
    run(scenario())
    assert ran == ["read_file"]                                                  # only the retry ran
    assert rig.link.for_call("call_grep_1") == [] and rig.link.result("call_grep_2")["ok"] is True


def test_a_finished_arriving_a_moment_later_still_stops_a_call_that_has_not_started(tmp_path):
    ran = []
    rig = make_rig(tmp_path, executor=executor_that(lambda n, a: ran.append(n) or "x"), replay_settle_s=0.05)
    (rig.root / "a.txt").write_text("x")

    async def scenario():
        rig.propose("c1", "read_file", {"path": "a.txt"})
        await asyncio.sleep(0.01)
        rig.runner.observe(finished("c1", "read_file", "disconnected"))
        await rig.runner.wait_idle()
    run(scenario())
    assert ran == [] and rig.link.sent == []


def test_after_a_restart_the_local_log_says_the_server_closed_the_call(tmp_path):
    """The link stores every server event before it shows it to callbacks; a fresh runner reads them back."""
    ran = []
    rig = make_rig(tmp_path, executor=executor_that(lambda n, a: ran.append(n) or "x"))
    (rig.root / "a.txt").write_text("x")
    append_event(rig.store, RUN_ID, "server.run_event.tool_finished", {
        "server_seq": 6, "id": "e", "ts": "2026-11-02T09:00:03.250Z",
        "payload": {"type": "tool_finished", "call_id": "c_old", "tool": "read_file", "status": "disconnected"}})
    run(rig.call("c_old", "read_file", {"path": "a.txt"}))
    assert ran == [] and rig.link.sent == []


def test_after_a_restart_a_call_that_was_already_decided_is_never_run_again(tmp_path):
    ran = []
    rig = make_rig(tmp_path, mode="accept-edits", executor=executor_that(lambda n, a: ran.append(n) or "x"))
    run(rig.call("c1", "write_file", {"path": "a.txt", "content": "1"}, "R1"))
    assert ran == ["write_file"]
    fresh = ToolRunner(rig.link, run_id=RUN_ID, tool_context=rig.tool_context, policy_context=rig.runner._get_policy,
                       store=rig.store, executor=executor_that(lambda n, a: ran.append(n) or "x"), replay_settle_s=0.0)
    sent_before = len(rig.link.sent)
    fresh.on_tool_proposed(proposed("c1", "write_file", {"path": "a.txt", "content": "1"}, "R1"))
    run(fresh.wait_idle())
    assert ran == ["write_file"] and len(rig.link.sent) == sent_before


def test_a_proposal_for_another_run_or_after_the_run_ended_is_ignored(tmp_path):
    ran = []
    rig = make_rig(tmp_path, executor=executor_that(lambda n, a: ran.append(n) or "x"))

    async def scenario():
        rig.runner.on_tool_proposed(proposed("c1", "read_file", {"path": "a.txt"}, run_id="another_run_id"))
        rig.runner.observe(state_changed("completed"))
        rig.propose("c2", "read_file", {"path": "a.txt"})
        await rig.runner.wait_idle()
    run(scenario())
    assert ran == [] and rig.link.sent == []


# ---- cancellation -------------------------------------------------------------------------------------------

LONG = "sleep 29.37"


async def start_long_command(rig):
    started = asyncio.Event()
    rig.link.on_send = lambda t, p: started.set() if t == "tool_stream" and "started" in p["chunk"] else None
    rig.propose("c1", "bash", {"command": f"echo started; {LONG}"}, "R2")
    await asyncio.wait_for(started.wait(), 10)
    assert process_running(LONG)
    return time.monotonic()


def test_cancel_run_kills_a_running_shell_command(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)", "bash(sleep:*)"])

    async def scenario():
        began = await start_long_command(rig)
        assert rig.runner.cancel_run(RUN_ID) == 1
        await asyncio.wait_for(rig.runner.wait_idle(), 15)
        return time.monotonic() - began
    elapsed = run(scenario())
    assert elapsed < 12 and not process_running(LONG)
    assert rig.link.for_call("c1", "tool_result") == []                          # the run is over: nobody waits for it


@pytest.mark.parametrize("status", ["cancelled", "disconnected", "timeout"])
def test_a_tool_finished_kills_the_running_call_it_closes(tmp_path, status):
    rig = make_rig(tmp_path, allow=["bash(echo:*)", "bash(sleep:*)"])

    async def scenario():
        await start_long_command(rig)
        rig.runner.observe(finished("c1", "bash", status))
        await asyncio.wait_for(rig.runner.wait_idle(), 15)
    run(scenario())
    assert not process_running(LONG), status
    assert rig.link.for_call("c1", "tool_result") == [], status


def test_the_run_ending_cancels_what_is_running(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)", "bash(sleep:*)"])

    async def scenario():
        await start_long_command(rig)
        rig.runner.observe(state_changed("cancelled"))
        await asyncio.wait_for(rig.runner.wait_idle(), 15)
    run(scenario())
    assert not process_running(LONG)


def test_cancel_run_for_another_run_does_nothing(tmp_path):
    rig = make_rig(tmp_path)
    assert rig.runner.cancel_run("some_other_run") == 0


def test_cancel_drops_queued_changes_and_dismisses_an_open_prompt(tmp_path):
    asked = []

    async def person(request):
        asked.append(request.call_id)
        await asyncio.sleep(60)                                                  # the person never answers
        return Choice("allow")

    ran = []
    rig = make_rig(tmp_path, ask_fn=person, executor=executor_that(lambda n, a: ran.append(n) or "x"))

    async def scenario():
        rig.propose("c1", "bash", {"command": "echo one"}, "R2")
        rig.propose("c2", "bash", {"command": "echo two"}, "R2")                 # queued behind c1
        await asyncio.sleep(0.05)
        assert asked == ["c1"]
        assert rig.runner.cancel_run(RUN_ID) == 2
        await asyncio.wait_for(rig.runner.wait_idle(), 5)
    run(scenario())
    assert ran == [] and asked == ["c1"] and rig.link.sent == []


def test_the_server_closing_a_call_dismisses_its_prompt(tmp_path):
    seen = {}

    async def person(request):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen["dismissed"] = True
            raise
        return Choice("allow")

    ran = []
    rig = make_rig(tmp_path, ask_fn=person, executor=executor_that(lambda n, a: ran.append(n) or "x"))

    async def scenario():
        rig.propose("c1", "bash", {"command": "echo one"}, "R2")
        await asyncio.sleep(0.05)
        rig.runner.observe(finished("c1", "bash", "timeout"))
        await asyncio.wait_for(rig.runner.wait_idle(), 5)
    run(scenario())
    assert seen == {"dismissed": True} and ran == [] and rig.link.sent == []


def test_aclose_cancels_everything_and_leaves_no_task(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)", "bash(sleep:*)"])

    async def scenario():
        await start_long_command(rig)
        await asyncio.wait_for(rig.runner.aclose(), 15)
        assert rig.runner.in_flight == 0
    run(scenario())
    assert not process_running(LONG)


# ---- concurrency --------------------------------------------------------------------------------------------

def test_reads_run_side_by_side(tmp_path):
    barrier = threading.Barrier(2, timeout=5)                                    # both reads must be inside at once

    def both_inside(name, args):
        barrier.wait()
        return "ok"

    rig = make_rig(tmp_path, executor=executor_that(both_inside))

    async def scenario():
        rig.propose("r1", "read_file", {"path": "a.txt"})
        rig.propose("r2", "read_file", {"path": "b.txt"})
        await rig.runner.wait_idle()
    run(scenario())
    assert rig.link.result("r1")["ok"] and rig.link.result("r2")["ok"]


def test_calls_that_change_something_run_one_at_a_time_in_arrival_order(tmp_path):
    lock, state = threading.Lock(), {"active": 0, "max": 0, "order": []}

    def slowly(name, args):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            state["order"].append(args.get("path") or args.get("command"))
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return "ok"

    rig = make_rig(tmp_path, mode="accept-edits", allow=["bash(echo:*)", "delete_path(*)"], executor=executor_that(slowly))

    async def scenario():
        rig.propose("w1", "write_file", {"path": "1.txt", "content": "x"}, "R1")
        rig.propose("w2", "bash", {"command": "echo 2"}, "R2")
        rig.propose("w3", "edit_file", {"path": "3.txt", "old_string": "a", "new_string": "b"}, "R1")
        rig.propose("w4", "delete_path", {"path": "4.txt"}, "R1")
        await rig.runner.wait_idle()
    run(scenario())
    assert state["max"] == 1 and state["order"] == ["1.txt", "echo 2", "3.txt", "4.txt"]
    results = [p["call_id"] for p in rig.link.payloads("tool_result")]
    assert results == ["w1", "w2", "w3", "w4"]


def test_a_read_does_not_wait_for_a_slow_change(tmp_path):
    gate = threading.Event()

    def executor(context, name, args, *, call_env=None):
        if name == "write_file":
            assert gate.wait(10)
        return ToolResult(True, "ok", False, "local_project", None, 1)

    rig = make_rig(tmp_path, mode="accept-edits", executor=executor)

    async def scenario():
        rig.propose("w1", "write_file", {"path": "1.txt", "content": "x"}, "R1")
        rig.propose("r1", "read_file", {"path": "a.txt"})
        for _ in range(200):
            if rig.link.for_call("r1", "tool_result"):
                break
            await asyncio.sleep(0.01)
        assert rig.link.for_call("r1", "tool_result") and not rig.link.for_call("w1", "tool_result")
        gate.set()
        await rig.runner.wait_idle()
    run(scenario())
    assert rig.link.result("w1")["ok"]


def test_a_later_change_is_decided_after_the_earlier_one_finished(tmp_path):
    """plan_exit changes the mode: the edit proposed right behind it must be judged in the NEW mode."""
    rig = make_rig(tmp_path, mode="plan", ask_fn=lambda request: Choice("allow"))

    async def scenario():
        rig.propose("c1", "plan_exit", {"plan": "p"})
        rig.propose("c2", "write_file", {"path": "n.txt", "content": "x"}, "R1")
        await rig.runner.wait_idle()
    run(scenario())
    assert [r.call_id for r in rig.asked] == ["c1", "c2"]                        # c2 was asked (ask mode), not denied (plan)
    assert rig.link.result("c2")["ok"] is True


# ---- prompts, one at a time ---------------------------------------------------------------------------------

def test_prompts_are_shown_one_at_a_time_in_arrival_order(tmp_path):
    state = {"now": 0, "max": 0, "order": []}

    async def person(request):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        state["order"].append(request.call_id)
        await asyncio.sleep(0.03)
        state["now"] -= 1
        return Choice("deny")

    rig = make_rig(tmp_path, ask_fn=person)

    async def scenario():
        for n in range(4):                                                       # reads outside the project ask
            rig.propose(f"r{n}", "read_file", {"path": f"/etc/hostname{n}"})
        await rig.runner.wait_idle()
    run(scenario())
    assert state["max"] == 1 and state["order"] == ["r0", "r1", "r2", "r3"]
    assert all(rig.link.approval(f"r{n}")["decision"] == "deny" for n in range(4))


# ---- the link is down -----------------------------------------------------------------------------------------

def test_a_result_that_cannot_be_sent_because_the_link_is_down_is_dropped(tmp_path):
    rig = make_rig(tmp_path)
    (rig.root / "a.txt").write_text("x")
    rig.link.fail_on = {"tool_result"}
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    assert rig.link.attempts.count("tool_result") == 1                          # tried once, never retried later
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    assert rig.link.attempts.count("tool_result") == 1


@pytest.mark.parametrize("error", [LinkNotConnectedError(), LinkClosedError()])
def test_an_approval_that_cannot_be_sent_means_the_call_is_not_run(tmp_path, error):
    """The server will close the call as disconnected and the model will retry: running it now would run it twice."""
    ran = []
    rig = make_rig(tmp_path, mode="accept-edits", executor=executor_that(lambda n, a: ran.append(n) or "x"))
    rig.link.fail_on = {"approval"}
    rig.link.fail_with = lambda t: error
    run(rig.call("c1", "write_file", {"path": "a.txt", "content": "x"}, "R1"))
    assert ran == [] and rig.link.types() == []


def test_a_result_too_large_for_the_wire_is_replaced_by_a_short_failure(tmp_path):
    rig = make_rig(tmp_path)
    (rig.root / "a.txt").write_text("x")
    original = rig.link.send_tool_result
    calls = []

    async def picky(run_id, call_id, **kwargs):
        calls.append(kwargs["ok"])
        if kwargs["ok"]:
            raise LinkMessageTooLargeError(1024)
        return await original(run_id, call_id, **kwargs)

    rig.link.send_tool_result = picky
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    assert calls == [True, False] and rig.link.result("c1")["ok"] is False


def test_a_forwarded_stream_chunk_that_fails_does_not_break_the_call(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)"])
    rig.link.fail_on = {"tool_stream"}
    run(rig.call("c1", "bash", {"command": "echo hello"}, "R2"))
    assert "hello" in rig.link.result("c1")["output"]


def test_stream_chunks_come_from_the_worker_thread_and_arrive_before_the_result(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)"])
    run(rig.call("c1", "bash", {"command": "echo streamed-text"}, "R2"))
    kinds = [k for k, _ in rig.link.for_call("c1")]
    assert kinds[0] == "approval" and kinds[-1] == "tool_result" and "tool_stream" in kinds
    chunks = "".join(p["chunk"] for k, p in rig.link.for_call("c1", "tool_stream"))
    assert "streamed-text" in chunks
