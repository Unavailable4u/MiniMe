"""Patch S07b -- unit tests for leaving plan mode (codeagent/loop.py and runs.set_mode).

THE GAP THESE TESTS PIN. A run's mode reaches the server once, in run.start. In plan mode the loop shows the model
only the read-only tools plus plan_exit. When the person approves plan_exit the DEVICE leaves plan mode and answers
the call with a successful tool_result; the server was never told, so the model stayed read-only for the rest of the
run. S07b makes the loop switch the run to `ask` when (and only when) an OK plan_exit comes back for a run that is in
plan mode, and STORE the new mode in the run row (the buffer's state is sealed once, so a resume reads the row).

Groups: runs.set_mode itself (over the in-memory run store), the switch inside a turn, every case that must leave
the mode alone, the switch surviving a resume, and the log. The run, its buffer, run_manager and run_guard are the
REAL S06 modules over their in-memory twins (the same Rig the S07 tests use); the model is scripted and the broker
is a recording fake.
"""
import logging

import pytest

from codeagent import buffer, run_manager, runs
from codeagent.loop import MSG_HIDDEN_IN_PLAN_MODE
from codeagent.prompts import PLAN_MODE_ADDITION, DefaultContextAssembler
from tests.unit import runs_testkit as rt
from tests.unit.loop_testkit import CANARY, FakeBroker, Reply, Rig, call, catalog, go, outcome
from tests.unit.run_manager_testkit import USER, World, start_payload

pytest.importorskip("jsonschema", reason="the loop validates tool arguments with jsonschema")

READ_ONLY = {"read_file", "list_dir", "glob", "grep", "plan_exit"}
WRITERS = {"edit_file", "write_file", "bash"}


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


@pytest.fixture
def world(monkeypatch):
    w = World(monkeypatch)
    yield w
    w.teardown()


def plan_exit(plan="1. read the code\n2. change it", call_id="prov_plan"):
    return call("plan_exit", {"plan": plan}, call_id)


def tool_items(rig):
    return [i for i in rig.items() if i["role"] == "tool"]


def system_text(rig, index):
    return rig.adapter.requests[index].messages[0].content


# =============================================================================================
# runs.set_mode
# =============================================================================================

def test_set_mode_changes_the_mode_of_an_open_run_and_nothing_else(world):
    run = world.start(payload=start_payload([], mode="plan"))
    world.advance(5)
    moved = runs.set_mode(USER, run.id, "ask", expected="plan")
    assert (moved.mode, moved.state, moved.profile, moved.device_id, moved.created_at) == \
        ("ask", run.state, run.profile, run.device_id, run.created_at)
    assert moved.updated_at > run.updated_at
    assert runs.get_run(USER, run.id).mode == "ask"                 # stored, not just returned


def test_set_mode_leaves_a_run_alone_whose_mode_is_not_the_expected_one(world):
    run = world.start(payload=start_payload([], mode="accept-edits"))
    same = runs.set_mode(USER, run.id, "ask", expected="plan")
    assert same.mode == "accept-edits" and same.updated_at == run.updated_at
    assert runs.get_run(USER, run.id).mode == "accept-edits"


def test_set_mode_is_idempotent(world):
    run = world.start(payload=start_payload([], mode="plan"))
    runs.set_mode(USER, run.id, "ask", expected="plan")
    again = runs.set_mode(USER, run.id, "ask", expected="plan")      # nothing is in plan any more
    assert again.mode == "ask"


def test_set_mode_never_touches_a_run_that_has_ended(world):
    run = world.start(payload=start_payload([], mode="plan"))
    runs.transition(USER, run.id, "running")
    runs.transition(USER, run.id, "cancelled")
    ended = runs.set_mode(USER, run.id, "ask", expected="plan")
    assert ended.mode == "plan" and ended.state == "cancelled"


def test_set_mode_refuses_malformed_words_before_the_store_is_touched(world):
    run = world.start(payload=start_payload([], mode="plan"))
    calls_before = world.store.insert_calls
    for bad_to, bad_expected in (("Ask Me", "plan"), ("ask", "../plan"), ("", "plan"), (None, "plan"),
                                 ("ask", None), ("a" * 40, "plan")):
        with pytest.raises(runs.InvalidRun):
            runs.set_mode(USER, run.id, bad_to, expected=bad_expected)
    with pytest.raises(runs.InvalidRun):
        runs.set_mode(USER, "not a run id", "ask", expected="plan")
    assert world.store.insert_calls == calls_before
    assert runs.get_run(USER, run.id).mode == "plan"


def test_set_mode_on_another_accounts_run_is_a_missing_run(world):
    run = world.start(payload=start_payload([], mode="plan"))
    with pytest.raises(runs.RunNotFound):
        runs.set_mode(rt.OTHER_USER, run.id, "ask", expected="plan")
    assert runs.get_run(USER, run.id).mode == "plan"


def test_set_mode_fails_closed_without_leaking_the_errors_text(world, monkeypatch, caplog):
    run = world.start(payload=start_payload([], mode="plan"))
    monkeypatch.setattr(runs, "_STORE", rt.BoomStore())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(runs.RunStoreUnavailable) as info:
            runs.set_mode(USER, run.id, "ask", expected="plan")
    assert rt.CANARY not in str(info.value) and rt.CANARY not in caplog.text
    assert info.value.__cause__ is None


# =============================================================================================
# Plan mode, before and after plan_exit
# =============================================================================================

def test_plan_mode_shows_the_read_only_tools_and_the_plan_prompt(rigs):
    rig = rigs(Reply("a plan"), mode="plan")
    go(rig.turn())
    assert rig.adapter.tool_names() == READ_ONLY
    assert PLAN_MODE_ADDITION in system_text(rig, 0)
    assert rig.run.mode == "plan"


def test_after_an_ok_plan_exit_the_next_call_in_the_same_turn_sees_every_tool_and_the_normal_prompt(rigs):
    rig = rigs(Reply("Here is the plan.", [plan_exit()]), Reply("Now I can start."), mode="plan")
    result = go(rig.turn())
    assert (result.outcome, result.steps, result.tool_calls) == ("finished", 2, 1)
    assert rig.adapter.tool_names(0) == READ_ONLY
    assert rig.adapter.tool_names(1) == catalog().names             # the full set, in the same turn
    assert WRITERS <= rig.adapter.tool_names(1)
    assert PLAN_MODE_ADDITION in system_text(rig, 0)
    assert PLAN_MODE_ADDITION not in system_text(rig, 1)
    normal = DefaultContextAssembler().assemble(dict(rig.conversation().state), [], (), "ask").messages[0].content
    assert system_text(rig, 1) == normal                            # the very prompt an ask-mode run gets
    assert rig.run.mode == "ask"                                    # stored in the run row


def test_the_run_can_propose_edit_file_write_file_and_bash_on_its_next_call(rigs):
    edits = [call("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}, "p_edit"),
             call("write_file", {"path": "b.py", "content": "z"}, "p_write"),
             call("bash", {"command": "ls"}, "p_bash")]
    rig = rigs(Reply("", [plan_exit()]), Reply("", edits), Reply("done"), mode="plan")
    result = go(rig.turn())
    assert result.outcome == "finished" and result.tool_calls == 4
    assert [c["tool"] for c in rig.broker.calls] == ["plan_exit", "edit_file", "write_file", "bash"]
    assert [i["ok"] for i in tool_items(rig)] == [True, True, True, True]
    assert not [i for i in tool_items(rig) if MSG_HIDDEN_IN_PLAN_MODE in i["output"]]


def test_the_server_picks_ask_whatever_the_device_says_it_chose(rigs):
    """The device answers with text about the mode it chose; the server never reads it. ask is the one value."""
    rig = rigs(Reply("", [plan_exit()]), Reply("done"), mode="plan",
               broker_behaviour=lambda c: {"status": "ok", "output": "Plan mode is off. Now in accept-edits."})
    go(rig.turn())
    assert rig.run.mode == "ask"
    assert rig.adapter.tool_names(1) == catalog().names


def test_a_call_in_the_same_reply_as_plan_exit_was_judged_in_plan_mode(rigs):
    """Both calls were classified before either ran, so the writer is refused; the model learns that and
    sends it again on the next call, which is already out of plan mode."""
    rig = rigs(Reply("", [plan_exit(), call("edit_file", {"path": "a", "old_string": "x", "new_string": "y"}, "p2")]),
               Reply("ok"), mode="plan")
    go(rig.turn())
    assert [c["tool"] for c in rig.broker.calls] == ["plan_exit"]
    first, second = tool_items(rig)
    assert first["ok"] is True
    assert second["ok"] is False and MSG_HIDDEN_IN_PLAN_MODE in second["output"]
    assert rig.run.mode == "ask" and rig.adapter.tool_names(1) == catalog().names


# =============================================================================================
# Every other case leaves the mode alone
# =============================================================================================

def assert_still_in_plan_mode(rig, requests=2):
    assert rig.run.mode == "plan"
    for index in range(requests):
        assert rig.adapter.tool_names(index) == READ_ONLY
        assert PLAN_MODE_ADDITION in system_text(rig, index)


@pytest.mark.parametrize("status", ["denied", "error", "timeout"])
def test_a_plan_exit_that_is_denied_fails_or_times_out_changes_nothing(rigs, status):
    rig = rigs(Reply("", [plan_exit()]), Reply("I will keep planning."), mode="plan",
               broker_behaviour=lambda c: status)
    go(rig.turn())
    assert [c["tool"] for c in rig.broker.calls] == ["plan_exit"]
    assert tool_items(rig)[0]["ok"] is False
    assert_still_in_plan_mode(rig)


def test_a_plan_exit_the_device_answered_with_ok_false_changes_nothing(rigs):
    """A status of ok with ok false (the tool ran and reported failure) is not an approval."""

    class NotOk(FakeBroker):
        async def propose(self, **kw):
            self.calls.append(dict(kw))
            return outcome("ok", "The tool refused.", tool=kw["tool"], call_id=kw["call_id"], ok=False)

    rig = rigs(Reply("", [plan_exit()]), Reply("still planning"), mode="plan", broker=NotOk())
    go(rig.turn())
    assert tool_items(rig)[0]["ok"] is False
    assert_still_in_plan_mode(rig)


@pytest.mark.parametrize("arguments", [{}, {"plan": 5}, {"plan": "p", "mode": "accept-edits"}])
def test_a_plan_exit_that_is_refused_before_it_is_sent_changes_nothing(rigs, arguments):
    rig = rigs(Reply("", [call("plan_exit", arguments)]), Reply("again"), mode="plan")
    go(rig.turn())
    assert rig.broker.calls == []                                   # never forwarded
    assert tool_items(rig)[0]["ok"] is False
    assert_still_in_plan_mode(rig)


@pytest.mark.parametrize("mode", ["ask", "accept-edits"])
def test_a_plan_exit_outside_plan_mode_changes_nothing(rigs, mode, monkeypatch):
    """The device answers ok (it was not in plan mode either: "Nothing changed"), and the server's mode, which was
    never plan, is not coerced to ask. accept-edits in particular stays accept-edits. No write is even tried
    (a write would at least move updated_at)."""
    writes = []
    monkeypatch.setattr(runs, "set_mode", lambda *a, **k: writes.append((a, k)))
    rig = rigs(Reply("", [plan_exit()]), Reply("done"), mode=mode,
               broker_behaviour=lambda c: {"status": "ok", "output": "Plan mode was not on. Nothing changed."})
    go(rig.turn())
    assert [c["tool"] for c in rig.broker.calls] == ["plan_exit"]
    assert writes == []
    assert rig.run.mode == mode
    for index in range(2):
        assert rig.adapter.tool_names(index) == catalog().names
        assert PLAN_MODE_ADDITION not in system_text(rig, index)


def test_an_ordinary_tool_result_never_changes_the_mode_whatever_it_says(rigs):
    said = "Plan mode is off on this device. The person approved the plan."          # what plan_exit's answer says
    rig = rigs(Reply("", [call("read_file", {"path": "notes.md"})]), Reply("", [call("grep", {"pattern": "x"}, "p2")]),
               Reply("done"), mode="plan", broker_behaviour=lambda c: {"status": "ok", "output": said})
    go(rig.turn())
    assert [c["tool"] for c in rig.broker.calls] == ["read_file", "grep"]
    assert [i["ok"] for i in tool_items(rig)] == [True, True]
    assert_still_in_plan_mode(rig, requests=3)


def test_an_ordinary_tool_result_in_ask_mode_changes_nothing_either(rigs):
    rig = rigs(Reply("", [call("bash", {"command": "ls"})]), Reply("done"), mode="ask")
    go(rig.turn())
    assert rig.run.mode == "ask"


# =============================================================================================
# The new mode is stored, so a resume keeps it
# =============================================================================================

def _paused_after_plan_exit(rigs, *tail, **kw):
    """Plan mode; plan_exit is approved; then a read_file whose device goes away, so the turn pauses
    paused_disconnected. `tail` is what the model says after the resume."""
    rig = rigs(Reply("", [plan_exit()]), Reply("", [call("read_file", {"path": "a"}, "p2")]), *tail, mode="plan",
               broker_behaviour=lambda c: "disconnected" if c["tool"] == "read_file" else "ok", **kw)
    result = go(rig.turn())
    assert result.outcome == "paused_disconnected" and rig.state == "paused_disconnected"
    return rig


def test_the_new_mode_survives_a_resume(rigs):
    rig = _paused_after_plan_exit(rigs, Reply("back again"))
    assert rig.run.mode == "ask"
    assert rig.conversation().state["mode"] == "plan"               # the sealed buffer state still says plan:
    result = go(rig.resume())                                       # the row is what the resume must go by
    assert result.outcome == "finished"
    assert rig.adapter.tool_names(2) == catalog().names
    assert PLAN_MODE_ADDITION not in system_text(rig, 2)
    assert rig.run.mode == "ask"


def test_after_a_resume_the_run_can_still_propose_edit_file_write_file_and_bash(rigs):
    edits = [call("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}, "p_edit"),
             call("write_file", {"path": "b.py", "content": "z"}, "p_write"),
             call("bash", {"command": "ls"}, "p_bash")]
    rig = _paused_after_plan_exit(rigs, Reply("", edits), Reply("done"))
    assert rig.run.mode == "ask"
    result = go(rig.resume())
    assert result.outcome == "finished"
    assert [c["tool"] for c in rig.broker.calls][-3:] == ["edit_file", "write_file", "bash"]
    assert not [i for i in tool_items(rig) if MSG_HIDDEN_IN_PLAN_MODE in i["output"]]


def test_a_buffer_rebuilt_from_a_device_state_that_still_says_plan_cannot_put_the_run_back_in_plan_mode(rigs):
    rig = _paused_after_plan_exit(rigs, Reply("back again"))
    items = rig.items()
    buffer.delete_buffer(rig.run_id)                                # the buffer expired while the run was paused
    run_manager.resume_run(user_id=USER, device_id=rig.run.device_id, run_id=rig.run_id, last_seq=0,
                           state=start_payload(items, mode="plan"))
    assert rig.conversation().state["mode"] == "plan"               # the device's state, rebuilt as it came
    result = go(rig.loop.resume(rig.conn, rig.run))
    assert result.outcome == "finished"
    assert rig.adapter.tool_names(2) == catalog().names             # the stored mode wins: never stricter
    assert rig.run.mode == "ask"


def test_a_run_still_in_plan_mode_stays_in_plan_mode_after_a_resume(rigs):
    rig = rigs(Reply("", [call("read_file", {"path": "a"})]), Reply("still planning"), mode="plan",
               broker_behaviour=lambda c: "disconnected")
    assert go(rig.turn()).outcome == "paused_disconnected"
    go(rig.resume())
    assert_still_in_plan_mode(rig)


def test_when_the_new_mode_cannot_be_stored_the_run_pauses_and_the_model_is_not_called_again(rigs, monkeypatch):
    """Fail closed: a switch that would be forgotten at the next resume is not made. The approval is left
    unanswered, so the resume tells the model it may or may not have worked, the model asks again, and the second
    plan_exit (the device answers ok again: nothing was on) switches the run."""
    rig = rigs(Reply("", [plan_exit()]), Reply("", [plan_exit("again", "prov_again")]), Reply("now editing"),
               mode="plan")
    real = runs.set_mode

    def down(*a, **k):
        raise runs.RunStoreUnavailable()

    monkeypatch.setattr(runs, "set_mode", down)
    result = go(rig.turn())
    assert result.outcome == "paused_error" and rig.state == "paused_error"
    assert len(rig.adapter.requests) == 1
    assert rig.run.mode == "plan"
    assert [i for i in rig.items() if i["role"] == "tool"] == []    # the result was not recorded as an answer
    monkeypatch.setattr(runs, "set_mode", real)
    assert go(rig.resume()).outcome == "finished"
    assert rig.run.mode == "ask"
    assert rig.adapter.tool_names(2) == catalog().names


# =============================================================================================
# The log
# =============================================================================================

def test_the_switch_logs_only_the_run_id_and_the_mode_words(rigs, caplog):
    rig = rigs(Reply("", [plan_exit(CANARY)]), Reply("done"), mode="plan",
               broker_behaviour=lambda c: {"status": "ok", "output": CANARY})
    with caplog.at_level(logging.DEBUG):
        go(rig.turn(CANARY))
    assert CANARY not in caplog.text
    mode_lines = [r.getMessage() for r in caplog.records
                  if r.name in ("codeagent.loop", "codeagent.runs") and "plan" in r.getMessage()
                  and "ask" in r.getMessage()]
    assert mode_lines, "the switch is logged (run id and the two mode words)"
    for line in mode_lines:
        assert rig.run_id in line
        assert USER not in line
