"""Patch S07 -- unit tests for codeagent/loop.py (the agent loop executor v0).

A scripted fake model drives the loop; the run, its buffer, run_manager and run_guard are the REAL S06/eo modules
over their in-memory twins (run_manager_testkit.World); the broker is a recording fake except in the tests that
name the real one. Groups: the step, tool calls, malformed calls, plan mode, approvals, cancel, limits and
breakers, provider fallback, metering, disconnect/pause/resume, logging, wiring.
"""
import asyncio
import logging
import re

import pytest

from codeagent import broker as broker_module, hello, loop as loop_module, run_handlers, run_manager, runs
from codeagent.loop import (BudgetPause, LoopConfig, ModelChoice, ToolCatalog, TurnResult, find_dangling,
                            MSG_DENIED, MSG_RESUME_DISCONNECTED, MSG_NOT_RUN_BUDGET)
from codeagent.model_adapter import ModelAdapterError
from codeagent.run_handlers import RunHandlers
from tests.unit.loop_testkit import (CANARY, HANG, LIGHT, STRONG, FakeBroker, FakeBudget, FakeConn, FakeMetering,
                                     Recorder, Reply, Rig, ScriptedAdapter, bad_call, call, catalog, go, outcome,
                                     protocol_problems, user_msg)
from tests.unit.run_manager_testkit import USER
from utils.llm_errors import ErrorBucket

pytest.importorskip("jsonschema", reason="the loop validates tool arguments with jsonschema")


@pytest.fixture
def rigs(monkeypatch):
    made = []

    def make(*script, **kw):
        rig = Rig(monkeypatch, *script, **kw)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.teardown()


def tool_items(rig):
    return [i for i in rig.items() if i["role"] == "tool"]


# =============================================================================================
# The step
# =============================================================================================

def test_a_plain_answer_streams_finishes_and_leaves_the_run_running(rigs):
    rig = rigs(Reply("All done here."), metering=FakeMetering())

    async def run():
        return await rig.turn("fix it")

    result = go(run())
    assert result == TurnResult("finished", None, 1, 0)
    assert rig.rec.text() == "All done here."
    assert [p["type"] for p in rig.rec.of("text_done")] == ["text_done"]
    assert rig.rec.of("text_done")[0]["stop_reason"] == "end_turn"
    assert rig.items() == [{"role": "user", "text": "fix it"}, {"role": "assistant", "text": "All done here."}]
    assert rig.state == "running"                                   # a finished turn is NOT a completed run
    assert not [s for s in rig.rec.states() if s[0] == "completed"]
    assert rig.rec.finals == []
    assert protocol_problems(rig.rec.events) == []


def test_usage_goes_to_metering_and_the_loop_emits_nothing_about_credits(rigs):
    metering = FakeMetering()
    rig = rigs(Reply("hi"), metering=metering)
    go(rig.turn())
    assert metering.before == 1 and len(metering.after) == 1
    assert (metering.after[0].input, metering.after[0].output) == (100, 20)
    assert rig.rec.of("usage") == []


def test_a_multi_step_run_feeds_results_back_and_keeps_stream_order(rigs):
    rig = rigs(Reply("Let me look.", [call("read_file", {"path": "a.py"})]),
               Reply("It says hello."),
               broker_behaviour=lambda c: {"status": "ok", "output": "print('hello')"})
    result = go(rig.turn("what does a.py do?"))
    assert result.outcome == "finished" and result.steps == 2 and result.tool_calls == 1
    items = rig.items()
    assert [i["role"] for i in items] == ["user", "assistant", "tool", "assistant"]
    (tc,) = items[1]["tool_calls"]
    assert re.fullmatch(r"call_[0-9a-f]{24}", tc["call_id"])         # minted here, not the provider's id
    assert (tc["tool"], tc["input"]) == ("read_file", {"path": "a.py"})
    assert items[2] == {"role": "tool", "call_id": tc["call_id"], "ok": True, "output": "print('hello')",
                        "truncated": False, "trust": "local_project"}
    # the second request carries the tool message and the call it answers
    roles = [m.role for m in rig.adapter.last_messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assert rig.adapter.last_messages[2].tool_calls[0].id == tc["call_id"] == rig.adapter.last_messages[3].tool_call_id
    assert "print('hello')" in rig.adapter.last_messages[3].content
    kinds = [p["type"] for p in rig.rec.of("text_done")]
    assert rig.rec.of("text_done")[0]["stop_reason"] == "tool_use"
    assert rig.rec.of("text_done")[1]["stop_reason"] == "end_turn"
    assert len(kinds) == 2
    assert protocol_problems(rig.rec.events) == []


def test_every_request_offers_the_tools_and_the_configured_timeout(rigs):
    rig = rigs(Reply("hi"), config=LoopConfig(model_timeout_s=33.0))
    go(rig.turn())
    request = rig.adapter.requests[0]
    assert request.timeout_s == 33.0
    assert rig.adapter.tool_names() == set(catalog().names) and len(catalog().names) == 9
    assert request.messages[0].role == "system"


def test_the_instructions_rules_and_memory_reach_the_system_message(rigs):
    rig = rigs(Reply("hi"), state_over={"instructions": "use tabs", "memory_index": "likes pytest",
                                        "rules": [{"name": "py", "text": "type hints please"}]})
    go(rig.turn())
    system = rig.adapter.requests[0].messages[0].content
    assert "use tabs" in system and "likes pytest" in system and "type hints please" in system


def test_an_empty_reply_ends_the_turn_without_an_empty_assistant_message(rigs):
    rig = rigs(Reply("", usage=True))
    assert go(rig.turn()).outcome == "finished"
    assert rig.items() == [{"role": "user", "text": "please help"}]


# =============================================================================================
# Tool calls
# =============================================================================================

def test_parallel_calls_run_concurrently_and_results_keep_the_call_order(rigs):
    delays = {"a.py": 0.06, "b.py": 0.03, "c.py": 0.0}              # the first call finishes LAST

    def behaviour(c):
        return {"status": "ok", "output": "out:" + c["tool_input"]["path"], "delay": delays[c["tool_input"]["path"]]}

    rig = rigs(Reply("", [call("read_file", {"path": "a.py"}, "p1"), call("read_file", {"path": "b.py"}, "p2"),
                          call("read_file", {"path": "c.py"}, "p3")]),
               Reply("done"), broker_behaviour=behaviour)
    result = go(rig.turn())
    assert result.outcome == "finished" and result.tool_calls == 3
    assert rig.broker.max_active == 3                               # all three were in flight at once
    order = [c["tool_input"]["path"] for c in rig.broker.calls]
    assert order == ["a.py", "b.py", "c.py"]
    assert [i["output"] for i in tool_items(rig)] == ["out:a.py", "out:b.py", "out:c.py"]
    ids = [c["call_id"] for c in rig.items()[1]["tool_calls"]]
    assert [i["call_id"] for i in tool_items(rig)] == ids


def test_parallelism_is_capped_at_the_configured_number(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": f"{n}.py"}, f"p{n}") for n in range(5)]), Reply("ok"),
               broker_behaviour=lambda c: {"status": "ok", "delay": 0.02}, config=LoopConfig(max_parallel_calls=2, cancel_poll_s=0.01))
    go(rig.turn())
    assert rig.broker.max_active == 2 and len(rig.broker.calls) == 5


def test_the_broker_gets_ids_risk_and_a_deadline_that_covers_the_tool_timeout(rigs):
    rig = rigs(Reply("", [call("bash", {"command": "make", "timeout_s": 600}, "p1"),
                          call("delete_path", {"path": "x"}, "p2"), call("read_file", {"path": "y"}, "p3")]),
               Reply("ok"))
    go(rig.turn())
    by_tool = {c["tool"]: c for c in rig.broker.calls}
    assert by_tool["bash"]["risk"] == "R2" and by_tool["read_file"]["risk"] == "R0"
    assert by_tool["delete_path"]["risk"] == "R4"                   # risk_max: the device may only raise it
    assert by_tool["bash"]["timeout_s"] == 600 + LoopConfig().call_grace_s
    assert by_tool["read_file"]["timeout_s"] == LoopConfig().call_timeout_s
    for c in rig.broker.calls:
        assert (c["run_id"], c["user_id"], c["device_id"]) == (rig.run_id, USER, rig.run.device_id)


def test_a_provider_that_reuses_call_ids_gets_fresh_ones_each_step(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": "a"}, "call_0")]),
               Reply("", [call("read_file", {"path": "b"}, "call_0")]), Reply("ok"))
    go(rig.turn())
    ids = [c["call_id"] for c in rig.broker.calls]
    assert len(set(ids)) == 2


def test_a_denied_call_tells_the_model_not_to_retry_unasked(rigs):
    rig = rigs(Reply("", [call("write_file", {"path": "a", "content": "x"})]), Reply("understood"),
               broker_behaviour=lambda c: "denied")
    result = go(rig.turn())
    assert result.outcome == "finished"
    (item,) = tool_items(rig)
    assert item["ok"] is False and item["output"] == MSG_DENIED
    assert "not retry" in MSG_DENIED and "ask" in MSG_DENIED
    assert "denied" in rig.adapter.last_messages[-1].content


def test_an_error_result_is_marked_for_the_model_and_a_timeout_says_to_check(rigs):
    rig = rigs(Reply("", [call("bash", {"command": "false"}, "p1"), call("read_file", {"path": "x"}, "p2")]),
               Reply("ok"),
               broker_behaviour=lambda c: {"status": "error", "output": "exit 1"} if c["tool"] == "bash" else "timeout")
    go(rig.turn())
    bash, read = tool_items(rig)
    assert bash["ok"] is False and bash["output"] == "exit 1"
    assert "timed out" in read["output"] and "check the current state" in read["output"]
    contents = [m.content for m in rig.adapter.last_messages if m.role == "tool"]
    assert contents[0].startswith("[tool error] exit 1")


def test_a_truncated_output_is_flagged_for_the_model(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": "x"})]), Reply("ok"),
               broker_behaviour=lambda c: {"status": "ok", "output": "abc", "truncated": True})
    go(rig.turn())
    assert tool_items(rig)[0]["truncated"] is True
    assert "truncated" in [m for m in rig.adapter.last_messages if m.role == "tool"][0].content


# =============================================================================================
# Malformed calls
# =============================================================================================

@pytest.mark.parametrize("make_call, needle", [
    (lambda: bad_call("malformed_arguments"), "valid JSON"),
    (lambda: bad_call("truncated_arguments"), "cut off"),
    (lambda: bad_call("unknown_tool", "no_such_tool"), "does not exist"),
    (lambda: bad_call("missing_name", ""), "no tool name"),
])
def test_a_call_the_parser_could_not_use_is_never_sent_and_the_model_can_correct_itself(rigs, make_call, needle):
    rig = rigs(Reply("", [make_call()]), Reply("sorry, retrying"))
    result = go(rig.turn())
    assert rig.broker.calls == []
    (item,) = tool_items(rig)
    assert item["ok"] is False and needle in item["output"]
    assert CANARY not in item["output"]                              # the model's own text is not echoed back
    assert needle in rig.adapter.last_messages[-1].content
    assert result.outcome == "finished"


def test_schema_invalid_arguments_are_refused_with_paths_and_keywords_not_values(rigs):
    rig = rigs(Reply("", [call("read_file", {"start_line": "XVII-lines"}, "p1"),
                          call("read_file", {"path": "a", CANARY.replace("-", "_"): 1}, "p2"),
                          call("edit_file", {"path": "a", "old_string": "x"}, "p3")]), Reply("ok"))
    go(rig.turn())
    assert rig.broker.calls == []
    one, two, three = [i["output"] for i in tool_items(rig)]
    assert "missing required argument(s): path" in one and "start_line" in one
    assert "unexpected argument(s)" in two                           # a closed object: an unknown argument fails
    assert "new_string" in three
    for text in (one, two, three):
        assert "XVII" not in text and len(text) <= LoopConfig().error_text_max_chars


def test_a_tool_that_does_not_exist_is_refused(rigs):
    rig = rigs(Reply("", [call("rm_rf", {"path": "/"})]), Reply("ok"))
    go(rig.turn())
    assert rig.broker.calls == []
    assert "does not exist" in tool_items(rig)[0]["output"]


def test_a_good_call_next_to_a_bad_one_still_runs(rigs):
    rig = rigs(Reply("", [bad_call(), call("read_file", {"path": "ok.py"}, "p2")]), Reply("ok"))
    go(rig.turn())
    assert [c["tool"] for c in rig.broker.calls] == ["read_file"]
    bad, good = tool_items(rig)
    assert bad["ok"] is False and good["ok"] is True


# =============================================================================================
# Plan mode
# =============================================================================================

def test_plan_mode_offers_only_read_only_tools_plus_plan_exit(rigs):
    rig = rigs(Reply("a plan"), mode="plan")
    go(rig.turn())
    assert rig.adapter.tool_names() == {"read_file", "list_dir", "glob", "grep", "plan_exit"}
    assert "plan mode" in rig.adapter.requests[0].messages[0].content


def test_a_hidden_tool_in_plan_mode_is_never_forwarded_and_counts_as_a_failure(rigs):
    rig = rigs(Reply("", [call("write_file", {"path": "a", "content": "x"})]), Reply("ok"), mode="plan")
    go(rig.turn())
    assert rig.broker.calls == []                                    # the model asked for a tool it cannot see
    (item,) = tool_items(rig)
    assert item["ok"] is False and "plan mode" in item["output"]
    from eo import run_guard
    assert run_guard.record_role_outcome(rig.run_id, True) is None   # one failure so far: nothing tripped
    from tests.unit.run_manager_testkit import FakeGuardRedis  # noqa: F401
    assert int(rig.world.guard.data[run_guard._k("fail_total", rig.run_id)]) == 1


def test_the_catalog_knows_each_tools_risk_and_hides_writes_in_plan_mode():
    cat = catalog()
    assert cat.effective_risk("read_file") == "R0" and cat.effective_risk("delete_path") == "R4"
    assert {s.name for s in cat.visible("ask")} == cat.names
    assert "bash" not in {s.name for s in cat.visible("plan")}
    assert cat.validate("read_file", {"path": "a"}) == []
    assert cat.validate("read_file", "not an object")


def test_the_default_catalog_loads_from_the_protocol_directory():
    assert ToolCatalog.from_protocol().names == catalog().names


# =============================================================================================
# The advisory awaiting_approval state
# =============================================================================================

def test_a_call_above_r0_moves_the_run_to_awaiting_approval_and_back_when_the_approval_arrives(rigs):
    rig = rigs(Reply("", [call("edit_file", {"path": "a", "old_string": "x", "new_string": "y"})]), Reply("ok"),
               broker_behaviour=lambda c: {"status": "ok", "approve": True, "delay": 0.01})
    go(rig.turn())
    assert rig.broker.states_seen == ["awaiting_approval", "running"]    # back to running on the approval
    assert rig.rec.states() == [("awaiting_approval", "tool_needs_approval"), ("running", "tool_resolved")]
    assert rig.state == "running"
    assert protocol_problems(rig.rec.events) == []


def test_a_read_only_call_never_enters_awaiting_approval(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": "a"})]), Reply("ok"))
    go(rig.turn())
    assert rig.broker.states_seen == ["running"] and rig.rec.states() == []


def test_awaiting_approval_ends_when_the_result_arrives_without_an_approval(rigs):
    rig = rigs(Reply("", [call("bash", {"command": "ls"})]), Reply("ok"))      # the device auto-allowed it
    go(rig.turn())
    assert rig.rec.states() == [("awaiting_approval", "tool_needs_approval"), ("running", "tool_resolved")]


def test_the_run_stays_awaiting_until_every_call_above_r0_is_resolved(rigs):
    rig = rigs(Reply("", [call("bash", {"command": "a"}, "p1"), call("bash", {"command": "b"}, "p2")]), Reply("ok"),
               broker_behaviour=lambda c: {"status": "ok", "delay": 0.0 if c["tool_input"]["command"] == "a" else 0.05})
    go(rig.turn())
    assert rig.rec.states().count(("awaiting_approval", "tool_needs_approval")) == 1
    assert rig.rec.states().count(("running", "tool_resolved")) == 1


# =============================================================================================
# Cancel
# =============================================================================================

def test_cancel_during_streaming_stops_the_stream_cancels_the_calls_and_the_run(rigs):
    from eo import run_guard
    rig = rigs(Reply(events=[__import__("codeagent.model_adapter", fromlist=["TextDelta"]).TextDelta("partial"), HANG]))

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while rig.rec.text() != "partial":
            await asyncio.sleep(0.005)
        run_guard.request_cancel(rig.run_id, "cancelled")
        return await asyncio.wait_for(task, 5)

    result = go(run())
    assert result.outcome == "cancelled"
    assert rig.adapter.opened == rig.adapter.closed == 1             # the provider stream was closed
    assert rig.broker.cancel_all_calls == [rig.run_id]
    assert rig.state == "cancelled"
    assert rig.rec.finals == [(rig.run_id, "cancelled", "cancelled")]
    assert [i for i in rig.log if i[0] == "emit" and i[1] == "usage"] == []


def test_cancel_while_a_tool_runs_calls_cancel_all_and_ends_the_run(rigs):
    from eo import run_guard
    rig = rigs(Reply("", [call("bash", {"command": "sleep 100"})]), broker_behaviour=lambda c: {"hold": True})

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while not rig.broker.calls:
            await asyncio.sleep(0.005)
        run_guard.request_cancel(rig.run_id, "cancelled")
        return await asyncio.wait_for(task, 5)

    result = go(run())
    assert result.outcome == "cancelled"
    assert rig.broker.cancel_all_calls == [rig.run_id] and rig.state == "cancelled"
    assert len(rig.adapter.requests) == 1                            # the model was not called again


def test_cancel_before_a_step_does_not_call_the_model(rigs):
    from eo import run_guard
    rig = rigs()
    run_guard.request_cancel(rig.run_id, "cancelled")
    assert go(rig.turn()).outcome == "cancelled"
    assert rig.adapter.requests == [] and rig.state == "cancelled"


def test_s14s_cancel_reaches_the_loop_as_a_cancelled_task_and_it_cleans_up_and_reraises(rigs):
    rig = rigs(Reply(events=[__import__("codeagent.model_adapter", fromlist=["TextDelta"]).TextDelta("x"), HANG]))

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while rig.rec.text() != "x":
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    go(run())
    assert rig.adapter.opened == rig.adapter.closed == 1
    assert rig.state == "running"                                    # S14 (not the loop) moves the run then


def test_when_the_broker_cancels_the_calls_for_s14_the_loop_stops_without_touching_the_run(rigs):
    rig = rigs(Reply("", [call("bash", {"command": "x"})]), broker_behaviour=lambda c: {"hold": True})

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while not rig.broker.calls:
            await asyncio.sleep(0.005)
        await rig.broker.cancel_all(rig.run_id)                      # S14's on_cancel hook, before stop_turns
        return await asyncio.wait_for(task, 5)

    result = go(run())
    assert (result.outcome, result.reason) == ("cancelled", "external")
    assert rig.state == "running" and rig.rec.finals == []


def test_a_run_ended_elsewhere_mid_turn_ends_the_turn_quietly(rigs):
    rig = rigs(Reply(events=[lambda: run_manager.cancel_run(USER, rig.run_id), __import__(
        "codeagent.model_adapter", fromlist=["TextDelta"]).TextDelta("late")]))
    result = go(rig.turn())
    assert result.outcome in ("ended", "cancelled")
    assert rig.state == "cancelled"


# =============================================================================================
# Limits and breakers
# =============================================================================================

def _always_reads(n=10):
    return [Reply("", [call("read_file", {"path": f"f{i}.py"})]) for i in range(n)]


def test_the_step_limit_pauses_the_run_with_a_notice(rigs):
    rig = rigs(*_always_reads(), config=LoopConfig(step_limit=3, cancel_poll_s=0.01))
    result = go(rig.turn())
    assert (result.outcome, result.reason, result.steps) == ("paused_budget", "step_limit", 3)
    assert len(rig.adapter.requests) == 3
    assert rig.state == "paused_budget"
    assert rig.rec.states()[-1] == ("paused_budget", "step_limit")
    (notice,) = [n for n in rig.rec.notices() if n["code"] == "step_limit_reached"]
    assert notice["severity"] == "warning" and "3" in notice["message"]
    assert rig.rec.unbound == [rig.run_id]
    assert protocol_problems(rig.rec.events) == []


def test_finishing_exactly_at_the_step_limit_is_not_a_pause(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": "a"})]), Reply("done"), config=LoopConfig(step_limit=2, cancel_poll_s=0.01))
    assert go(rig.turn()).outcome == "finished"


def test_the_tool_call_budget_uses_tool_budget_and_the_codes_own_threshold(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": f"{n}"}, f"p{n}") for n in range(3)]),
               config=LoopConfig(tool_call_threshold=2, cancel_poll_s=0.01))
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_budget", "tool_call_budget")
    assert len(rig.broker.calls) == 2 and rig.budget.count == 3      # the third took the count above 2
    last = tool_items(rig)[-1]
    assert last["ok"] is False and last["output"] == MSG_NOT_RUN_BUDGET
    assert rig.state == "paused_budget" and rig.rec.states()[-1] == ("paused_budget", "tool_call_budget")
    assert LoopConfig().tool_call_threshold > 15                     # not the web chat's default


def test_a_counter_that_cannot_be_read_does_not_stop_the_run(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": "a"})]), Reply("ok"), budget=FakeBudget(fail=True))
    assert go(rig.turn()).outcome == "finished"


def test_three_identical_failing_calls_pause_the_run_and_the_notice_names_the_tool_only(rigs):
    same = {"path": "secret-" + CANARY}
    rig = rigs(*[Reply("", [call("read_file", same)]) for _ in range(5)],
               broker_behaviour=lambda c: {"status": "error", "output": "no such file"})
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_error", "repeated_identical_failure")
    assert len(rig.adapter.requests) == 3 and len(rig.broker.calls) == 3
    (notice,) = [n for n in rig.rec.notices() if n["code"] == "repeated_identical_failure"]
    assert "read_file" in notice["message"] and CANARY not in notice["message"]
    assert rig.state == "paused_error"
    assert rig.run.error_code is None or rig.run.error_code in runs.ERROR_CODES


def test_the_same_call_succeeding_resets_its_failure_count(rigs):
    outcomes = iter(["error", "error", "ok", "error", "error", "ok"])
    rig = rigs(*[Reply("", [call("read_file", {"path": "a"})]) for _ in range(6)], Reply("done"),
               broker_behaviour=lambda c: next(outcomes))
    assert go(rig.turn()).outcome == "finished"


def test_the_failure_breaker_trips_on_three_consecutive_failures_with_the_tool_as_role(rigs):
    from eo import run_guard
    paths = iter("abcdef")
    rig = rigs(*[Reply("", [call("read_file", {"path": next(paths)})]) for _ in range(5)],
               broker_behaviour=lambda c: "error")
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_error", "repeated_failures")
    assert len(rig.broker.calls) == 3                                # different inputs, so not "identical"
    assert "read_file" in run_guard.breaker_reason(rig.run_id)
    assert rig.state == "paused_error"


def test_a_failing_call_with_an_invented_tool_name_is_filed_under_unknown(rigs):
    from eo import run_guard
    rig = rigs(*[Reply("", [call("tool_" + CANARY.replace("-", "_") + str(n), {})]) for n in range(3)])
    go(rig.turn())
    reason = run_guard.breaker_reason(rig.run_id)
    assert reason and "unknown" in reason and CANARY.replace("-", "_") not in reason


def test_a_success_between_failures_keeps_the_breaker_quiet(rigs):
    outcomes = iter(["error", "error", "ok", "error", "error"])
    paths = iter("abcdef")
    rig = rigs(*[Reply("", [call("read_file", {"path": next(paths)})]) for _ in range(5)], Reply("done"),
               broker_behaviour=lambda c: next(outcomes))
    assert go(rig.turn()).outcome == "finished"


# =============================================================================================
# Provider errors
# =============================================================================================

def _err(bucket, code=None, emitted=False):
    return ModelAdapterError(bucket, code=code, emitted_output=emitted)


def test_a_rate_limit_falls_through_silently_to_a_choice_of_the_same_class(rigs):
    a = ScriptedAdapter(_err(ErrorBucket.RATE_LIMIT_WINDOW))
    b = ScriptedAdapter(Reply("from b"))
    rig = rigs(chain=[STRONG, ModelChoice("scripted", "model-strong-2", "strong")],
               adapters={"model-strong": a, "model-strong-2": b})
    result = go(rig.turn())
    assert result.outcome == "finished" and rig.rec.text() == "from b"
    assert rig.rec.notices() == []                                   # same class: nothing to announce
    assert (a.opened, b.opened) == (1, 1)


def test_a_fall_through_to_a_different_quality_class_is_announced_never_silent(rigs):
    a = ScriptedAdapter(_err(ErrorBucket.TRANSIENT_NETWORK))
    b = ScriptedAdapter(Reply("from light"))
    rig = rigs(chain=[STRONG, LIGHT], adapters={"model-strong": a, "model-light": b})
    go(rig.turn())
    (notice,) = rig.rec.notices()
    assert notice["code"] == "model_changed" and notice["severity"] == "warning"
    assert "strong" in notice["message"] and "light" in notice["message"]
    assert "model-strong" not in notice["message"] and "scripted" not in notice["message"]    # classes only
    assert rig.log.index(("emit", "notice", "model_changed")) < rig.log.index(("emit", "text_delta", None))
    assert protocol_problems(rig.rec.events) == []


def test_later_steps_of_the_turn_stay_on_the_choice_that_worked(rigs):
    a = ScriptedAdapter(_err(ErrorBucket.RATE_LIMIT_WINDOW))
    b = ScriptedAdapter(Reply("", [call("read_file", {"path": "a"})]), Reply("done"))
    rig = rigs(chain=[STRONG, LIGHT], adapters={"model-strong": a, "model-light": b})
    go(rig.turn())
    assert a.opened == 1 and b.opened == 2 and len(rig.rec.notices()) == 1


def test_text_already_streamed_when_a_choice_fails_makes_the_client_drop_it(rigs):
    from codeagent.model_adapter import TextDelta
    a = ScriptedAdapter(Reply(events=[TextDelta("half"), _err(ErrorBucket.TRANSIENT_NETWORK, emitted=True)]))
    b = ScriptedAdapter(Reply("whole"))
    rig = rigs(chain=[STRONG, ModelChoice("scripted", "m2", "strong")], adapters={"model-strong": a, "m2": b})
    go(rig.turn())
    assert [n["code"] for n in rig.rec.notices()] == ["reply_restarted"]
    assert rig.items()[-1] == {"role": "assistant", "text": "whole"}   # the partial text is not in the conversation


def test_an_authentication_error_does_not_fall_through(rigs):
    a = ScriptedAdapter(_err(ErrorBucket.PERMANENT_AUTH))
    b = ScriptedAdapter(Reply("never"))
    rig = rigs(chain=[STRONG, LIGHT], adapters={"model-strong": a, "model-light": b})
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_error", "model_call_failed")
    assert b.opened == 0 and rig.state == "paused_error"
    assert [e["code"] for e in rig.conn.errors] == ["internal"]


def test_every_choice_failing_answers_provider_unavailable_and_pauses_the_run(rigs):
    a = ScriptedAdapter(_err(ErrorBucket.RATE_LIMIT_WINDOW))
    b = ScriptedAdapter(_err(ErrorBucket.TRANSIENT_NETWORK))
    rig = rigs(chain=[STRONG, LIGHT], adapters={"model-strong": a, "model-light": b})
    msg = rig.message()
    result = go(rig.loop.run_turn(rig.conn, rig.run, msg))
    assert (result.outcome, result.reason) == ("paused_error", "provider_unavailable")
    (error,) = rig.conn.errors
    assert error["code"] == "provider_unavailable" and error["ref_id"] == msg["id"]
    assert rig.state == "paused_error" and rig.run.error_code == "provider_unavailable"
    assert rig.log.index(("error", "provider_unavailable")) < rig.log.index(("emit", "state_changed", "paused_error"))
    assert rig.rec.unbound == [rig.run_id]


def test_an_empty_chain_is_provider_unavailable(rigs):
    rig = rigs(chain=[])
    assert go(rig.turn()).reason == "provider_unavailable"


def test_a_bare_timeout_from_an_adapter_is_treated_as_transient(rigs):
    a = ScriptedAdapter(asyncio.TimeoutError())
    b = ScriptedAdapter(Reply("ok"))
    rig = rigs(chain=[STRONG, LIGHT], adapters={"model-strong": a, "model-light": b})
    assert go(rig.turn()).outcome == "finished"


def test_a_rejected_tool_call_is_retried_on_the_same_choice(rigs):
    rig = rigs(_err(ErrorBucket.MALFORMED_REQUEST, code="tool_use_failed"), Reply("fine now"))
    assert go(rig.turn()).outcome == "finished" and rig.adapter.opened == 2 and rig.rec.notices() == []


def test_a_malformed_request_is_not_retried_elsewhere(rigs):
    rig = rigs(_err(ErrorBucket.MALFORMED_REQUEST), chain=[STRONG, LIGHT],
               adapters=None)
    assert go(rig.turn()).reason == "model_call_failed"


def test_a_model_preference_that_cannot_be_applied_is_announced_once(rigs):
    rig = rigs(Reply("hi"), state_over={"model_pref": "some-model"})
    go(rig.turn())
    assert [n["code"] for n in rig.rec.notices()] == ["model_preference_not_applied"]


# =============================================================================================
# Metering
# =============================================================================================

def test_a_budget_pause_from_metering_stops_before_the_model_is_called(rigs):
    rig = rigs(metering=FakeMetering(pause=BudgetPause("run_ceiling")))
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_budget", "run_ceiling")
    assert rig.adapter.requests == [] and rig.state == "paused_budget"
    assert rig.rec.states()[-1] == ("paused_budget", "run_ceiling") and rig.conn.errors == []


def test_a_quota_pause_sends_the_error_first_then_the_state(rigs):
    rig = rigs(metering=FakeMetering(pause=BudgetPause("quota_exhausted", "quota_exhausted", "2026-11-01T00:00:00Z")))
    msg = rig.message()
    go(rig.loop.run_turn(rig.conn, rig.run, msg))
    (error,) = rig.conn.errors
    assert error["code"] == "quota_exhausted" and error["details"] == {"reset_at": "2026-11-01T00:00:00Z"}
    assert error["ref_id"] == msg["id"]
    assert rig.log.index(("error", "quota_exhausted")) < rig.log.index(("emit", "state_changed", "paused_budget"))
    assert rig.run.error_code == "quota_exhausted"
    assert protocol_problems(rig.rec.events) == []


def test_metering_that_breaks_fails_open(rigs):
    rig = rigs(Reply("hi"), metering=FakeMetering(fail_before=RuntimeError("x"), fail_after=RuntimeError("y")))
    assert go(rig.turn()).outcome == "finished"


def test_a_metering_factory_gives_each_run_its_own_hook(rigs):
    made = []

    def factory(run):
        made.append(run.id)
        return FakeMetering()

    rig = rigs(Reply("hi"), metering_factory=factory)
    go(rig.turn())
    assert made == [rig.run_id]


def _msg(t, payload, run_id):
    return {"v": 1, "t": t, "id": "9b1c1c52-6a55-4d3e-8f0a-2f3a0d5b7c11", "run_id": run_id,
            "ts": "2026-11-02T09:00:00.000Z", "payload": payload}


def test_with_the_real_broker_a_device_result_reaches_the_model_and_the_stream_is_valid(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("bash", {"command": "ls"})]), Reply("listed"), connected=True)

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while not rig.rec.of("tool_proposed"):
            await asyncio.sleep(0.005)
        proposed = rig.rec.of("tool_proposed")[0]
        assert (proposed["tool"], proposed["risk"], proposed["input"]) == ("bash", "R2", {"command": "ls"})
        await rig.broker.handle_tool_result(rig.conn, _msg("tool_result", {
            "call_id": proposed["call_id"], "ok": True, "output": "a.py b.py", "truncated": False,
            "trust": "local_project", "exit_code": 0}, rig.run_id))
        return await asyncio.wait_for(task, 5)

    assert go(run()).outcome == "finished"
    assert tool_items(rig)[0]["output"] == "a.py b.py"
    assert [p["status"] for p in rig.rec.of("tool_finished")] == ["ok"]
    assert protocol_problems(rig.rec.events) == []


def test_with_the_real_broker_a_denial_becomes_a_do_not_retry_message(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("write_file", {"path": "a", "content": "x"})]),
                                Reply("ok, I will ask first"), connected=True)

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while not rig.rec.of("tool_proposed"):
            await asyncio.sleep(0.005)
        call_id = rig.rec.of("tool_proposed")[0]["call_id"]
        await rig.broker.handle_approval(rig.conn, _msg("approval", {"call_id": call_id, "decision": "deny", "scope": "once"},
                                                        rig.run_id))
        return await asyncio.wait_for(task, 5)

    assert go(run()).outcome == "finished"
    assert tool_items(rig)[0]["output"] == MSG_DENIED
    assert [p["status"] for p in rig.rec.of("tool_finished")] == ["denied"]
    assert rig.rec.states() == [("awaiting_approval", "tool_needs_approval"), ("running", "tool_resolved")]
    assert protocol_problems(rig.rec.events) == []


# =============================================================================================
# Disconnect, pause, resume
# =============================================================================================

def _real_broker(rig, *, connected):
    """The real S09 broker over a fake connection (or none), emitting through the rig's recorder."""
    conn = rig.conn
    return broker_module.ToolBroker(emit=rig.rec, connection_of=(lambda u, d: conn) if connected else (lambda u, d: None),
                                    timeout_s=5.0)


def _rig_with_real_broker(rigs, *script, connected, **kw):
    rig = rigs(*script, **kw)
    rig.broker = _real_broker(rig, connected=connected)
    rig.loop._broker = rig.broker
    return rig


def test_an_offline_device_pauses_the_run_disconnected_and_leaves_the_call_unanswered(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("read_file", {"path": "a"})]), connected=False)
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_disconnected", "device_disconnected")
    assert rig.state == "paused_disconnected"
    assert rig.rec.states()[-1] == ("paused_disconnected", "device_disconnected")
    items = rig.items()
    assert [i["role"] for i in items] == ["user", "assistant"]       # no result: resume tells the model
    assert len(find_dangling(items)) == 1
    assert rig.rec.unbound == [rig.run_id]


def test_a_connection_lost_mid_call_pauses_the_run_and_the_broker_closes_the_call(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("bash", {"command": "sleep 9"})]), connected=True)

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while not rig.rec.of("tool_proposed"):
            await asyncio.sleep(0.005)
        await rig.broker.on_run_disconnect(rig.conn, rig.run)        # S14's hook, called before it pauses the run
        return await asyncio.wait_for(task, 5)

    result = go(run())
    assert result.outcome == "paused_disconnected" and rig.state == "paused_disconnected"
    (finished,) = rig.rec.of("tool_finished")
    assert finished["status"] == "disconnected"
    states = rig.rec.states()
    assert ("paused_disconnected", "device_disconnected") in states
    assert protocol_problems(rig.rec.events) == []


def test_if_s14_already_paused_the_run_the_loop_does_not_pause_or_announce_twice(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("bash", {"command": "x"})]), connected=True)

    async def run():
        task = asyncio.ensure_future(rig.turn())
        while not rig.rec.of("tool_proposed"):
            await asyncio.sleep(0.005)
        run_manager.pause_run(USER, rig.run_id, "paused_disconnected")       # what S14 does after the hooks
        await rig.broker.on_run_disconnect(rig.conn, rig.run)
        return await asyncio.wait_for(task, 5)

    result = go(run())
    assert result.outcome == "paused_disconnected"
    assert ("paused_disconnected", "device_disconnected") not in rig.rec.states()   # S14 announces it, not us


def test_resume_tells_the_model_about_the_cut_off_call_and_continues(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("read_file", {"path": "a"})]), Reply("I checked, all good."),
                                connected=False)
    go(rig.turn())
    assert rig.state == "paused_disconnected"
    result = go(rig.resume())
    assert result.outcome == "finished"
    assert rig.state == "running"
    told = [i for i in rig.items() if i["role"] == "tool"]
    assert len(told) == 1 and told[0]["ok"] is False and told[0]["output"] == MSG_RESUME_DISCONNECTED
    assert "may or may not have completed" in told[0]["output"] and "check the current state" in told[0]["output"]
    contents = [m.content for m in rig.adapter.last_messages if m.role == "tool"]
    assert contents and "may or may not have completed" in contents[0]
    assert rig.items()[-1] == {"role": "assistant", "text": "I checked, all good."}
    assert find_dangling(rig.items()) == []


def test_resume_gives_every_cut_off_call_its_own_message_exactly_once(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("read_file", {"path": "a"}, "p1"), call("read_file", {"path": "b"}, "p2")]),
                                Reply("ok"), connected=False)
    go(rig.turn())
    go(rig.resume())
    ids = [c["call_id"] for c in rig.items()[1]["tool_calls"]]
    assert [i["call_id"] for i in tool_items(rig)] == ids and len(ids) == 2


def test_resume_has_nothing_to_do_when_the_last_reply_was_final(rigs):
    rig = rigs(Reply("all done"))
    go(rig.turn())
    rig.rec.events.clear()
    result = go(rig.loop.resume(rig.conn, rig.run))
    assert (result.outcome, result.reason) == ("finished", "nothing_to_resume")
    assert len(rig.adapter.requests) == 1


def test_resume_continues_a_turn_that_was_cut_before_the_reply(rigs):
    rig = rigs(Reply("here you go"), history=[{"role": "user", "text": "hello"}])
    result = go(rig.loop.resume(rig.conn, rig.run))
    assert result.outcome == "finished" and rig.items()[-1] == {"role": "assistant", "text": "here you go"}


def test_resume_after_the_breaker_tripped_gives_it_a_fresh_chance(rigs):
    from eo import run_guard
    paths = iter("abcdefgh")
    rig = rigs(*[Reply("", [call("read_file", {"path": next(paths)})]) for _ in range(3)],
               Reply("", [call("read_file", {"path": "z"})]), Reply("done"),
               broker_behaviour=lambda c: "ok" if c["tool_input"]["path"] == "z" else "error")
    go(rig.turn())
    assert rig.state == "paused_error" and run_guard.breaker_reason(rig.run_id)
    result = go(rig.resume())
    assert result.outcome == "finished"                              # a stale trip did not stop the resumed run
    assert not run_guard.breaker_reason(rig.run_id)


def test_the_resume_hook_starts_the_continuation_as_a_task_and_returns_at_once(rigs):
    rig = _rig_with_real_broker(rigs, Reply("", [call("read_file", {"path": "a"})]), Reply("done"), connected=False)
    go(rig.turn())
    run_manager.resume_run(user_id=USER, device_id=rig.run.device_id, run_id=rig.run_id, last_seq=0)

    async def run():
        await rig.loop.on_resume(rig.conn, rig.run)                  # S14 gives hooks 10 seconds
        assert rig.loop._tasks                                       # still running as a tracked task
        task = rig.loop._tasks[rig.run_id]
        await asyncio.wait_for(task, 5)

    go(run())
    assert rig.items()[-1] == {"role": "assistant", "text": "done"} and rig.loop._tasks == {}


def test_the_cancel_hook_stops_a_continuation_the_loop_started(rigs):
    from codeagent.model_adapter import TextDelta
    rig = rigs(Reply(events=[TextDelta("x"), HANG]), history=[{"role": "user", "text": "hello"}])

    async def run():
        await rig.loop.on_resume(rig.conn, rig.run)
        while rig.rec.text() != "x":
            await asyncio.sleep(0.005)
        await rig.loop.on_cancel(rig.conn, rig.run)
        assert rig.loop._tasks == {}

    go(run())
    assert rig.adapter.opened == rig.adapter.closed == 1


def test_an_unanswered_call_is_healed_at_the_start_of_the_next_turn_too(rigs):
    rig = rigs(Reply("ok then"), history=[{"role": "user", "text": "go"}, {
        "role": "assistant", "tool_calls": [{"call_id": "call_old", "tool": "read_file", "input": {"path": "a"}}]}])
    go(rig.turn("and now?"))
    roles = [m.role for m in rig.adapter.requests[0].messages]
    assert roles == ["system", "user", "assistant", "tool", "user"]   # the provider never sees an unanswered call


def test_a_buffer_that_fails_pauses_the_run_instead_of_carrying_on(rigs):
    from codeagent import buffer
    rig = rigs(Reply("hi"))
    rig.rec.fail_on["text_done"] = buffer.BufferUnavailable("the run buffer store is unavailable")
    result = go(rig.turn())
    assert (result.outcome, result.reason) == ("paused_error", "internal")
    assert rig.state == "paused_error" and [e["code"] for e in rig.conn.errors] == ["internal"]


# =============================================================================================
# Content hygiene (O1)
# =============================================================================================

def test_no_content_reaches_the_log(rigs, caplog):
    caplog.set_level(logging.DEBUG)
    secret_args = {"path": "src/" + CANARY + ".py"}
    rig = rigs(Reply("reply " + CANARY, [call("read_file", secret_args, "p1"), bad_call()]),
               Reply("", [call("read_file", secret_args, "p2")]),
               _err(ErrorBucket.RATE_LIMIT_WINDOW), Reply("done " + CANARY),
               broker_behaviour=lambda c: {"status": "error", "output": "failed " + CANARY},
               metering=FakeMetering(fail_after=RuntimeError("meter " + CANARY)),
               budget=FakeBudget(fail=True), config=LoopConfig(cancel_poll_s=0.01, identical_failure_limit=5))
    go(rig.turn("user text " + CANARY))
    assert caplog.records, "the loop logs, so this test has teeth"
    assert CANARY not in caplog.text


def test_notice_and_error_texts_are_fixed_server_text(rigs):
    rig = rigs(*_always_reads(3), config=LoopConfig(step_limit=2, cancel_poll_s=0.01), state_over={"model_pref": "x-" + CANARY.lower()})
    go(rig.turn("text " + CANARY))
    for notice in rig.rec.notices():
        assert CANARY.lower() not in notice["message"].lower() and 1 <= len(notice["message"]) <= 500


# =============================================================================================
# Wiring (nothing in this patch calls it)
# =============================================================================================

@pytest.fixture
def handlers(monkeypatch):
    from codeagent import emitter as emitter_module
    em = emitter_module.RunEmitter()
    monkeypatch.setattr(emitter_module, "_EMITTER", em)
    h = RunHandlers(emitter=em)
    monkeypatch.setattr(run_handlers, "_HANDLERS", h)
    return h


def _a_loop():
    return loop_module.AgentLoop(chain=[STRONG], broker=FakeBroker(), emit=Recorder(), catalog=catalog())


def test_install_registers_the_runner_and_the_three_hooks_once_each(handlers, monkeypatch):
    monkeypatch.setattr(loop_module, "_LOOP", None)
    active = _a_loop()
    loop_module.install(handlers=handlers, loop=active)
    loop_module.install(handlers=handlers, loop=active)
    assert handlers._turn_runner is loop_module.run_turn
    for name, key in (("_on_resume", "on_resume"), ("_on_cancel", "on_cancel"), ("_on_run_disconnect", "on_run_disconnect")):
        assert [h for h in getattr(handlers, name) if h is active.hooks[key]] == [active.hooks[key]]


def test_install_replaces_the_walking_skeletons_runner(handlers, monkeypatch):
    monkeypatch.setattr(loop_module, "_LOOP", None)
    hello.install()
    assert handlers._turn_runner is hello.run_turn
    loop_module.install(handlers=handlers, loop=_a_loop())
    assert handlers._turn_runner is loop_module.run_turn
    with pytest.raises(run_handlers.HandlerWiringError):
        handlers.set_turn_runner(hello.run_turn)                      # replace=False would have refused


def test_install_without_replace_refuses_to_take_another_runner(handlers, monkeypatch):
    monkeypatch.setattr(loop_module, "_LOOP", None)
    hello.install()
    with pytest.raises(run_handlers.HandlerWiringError):
        loop_module.install(replace=False, handlers=handlers, loop=_a_loop())


def test_the_module_level_run_turn_is_the_s14_contract_and_uses_the_process_loop(rigs, monkeypatch):
    rig = rigs(Reply("hi"))
    monkeypatch.setattr(loop_module, "_LOOP", rig.loop)
    result = go(loop_module.run_turn(rig.conn, rig.run, rig.message()))     # fn(conn, run, msg), async
    assert result.outcome == "finished"


def test_server_py_does_not_install_the_loop_in_this_patch():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "api" / "server.py").read_text(encoding="utf-8")
    assert "codeagent import loop" not in text and "codeagent_loop" not in text
    assert "codeagent_hello.install()" in text                        # hello's runner is still the one in use


def test_config_rejects_nonsense():
    for bad in ({"step_limit": 0}, {"tool_call_threshold": -1}, {"call_timeout_s": 0}, {"max_parallel_calls": True}):
        with pytest.raises(ValueError):
            LoopConfig(**bad)
    with pytest.raises(ValueError):
        ModelChoice("p", "m", "Not A Word")
    with pytest.raises(ValueError):
        loop_module.AgentLoop(chain=[STRONG], broker=FakeBroker(), emit=Recorder(), adapter=ScriptedAdapter(),
                              adapter_factory=lambda c: None)
