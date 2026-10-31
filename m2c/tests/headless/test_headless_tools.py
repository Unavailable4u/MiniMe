"""
m2c/tests/headless/test_headless_tools.py -- Patch C10: the non-interactive policy and the injected tool hooks.

headless.py defines two small injected interfaces -- ``decide(proposal)`` and ``execute(proposal)`` -- because the
policy engine (C06) and the tool executors (C05) are built in parallel and are not on this base; the integration
patch (I02) wires the real ones in.  These tests pin the contract the later patch relies on:

  * only an explicit ``allow`` runs a tool; ``ask``, ``deny``, an exception, a malformed answer, a missing policy
    and a missing executor all fail the run as a policy denial (exit 3), failing closed;
  * the denial is reported first, as ``approval{call_id, decision: deny, scope: once}`` and with no
    ``tool_result`` (P00: a denied call gets an approval and no tool_result), and only then is the run cancelled;
  * an allowed call is answered with a ``tool_result`` alone (P00 fixture 02: auto-allowed calls need no approval).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from headless_rig import *  # noqa: F401,F403

from m2c import headless

TASK = "read notes.txt"
CALL = "call_read_1"


async def _propose(conn, stream, _start, _user, *, tool="read_file", input=None, risk="R0", call=CALL):
    await stream.text("I'll look.")
    await stream.done("tool_use")
    await stream.usage()
    await stream.proposed(call, tool, input or {"path": "notes.txt"}, risk)


async def _denied_tail(conn, stream, start, user):
    await _propose(conn, stream, start, user, tool="bash", input={"command": "ls"}, risk="R2")
    await expect(conn, "approval")                                 # the next thing the device says


async def _answered_tail(conn, stream, start, user):
    """One tool step, then the final reply once the device has answered the call."""
    await _propose(conn, stream, start, user)
    await expect(conn, "tool_result")
    await stream.tool_finished(CALL, "read_file", "ok")
    await stream.reply("Found it.")


def _assert_denied_first_then_cancelled(srv: ThreadedServer, call: str = CALL) -> None:
    types = srv.types()
    assert types[-2:] == ["approval", "cancel"], types             # the denial is reported BEFORE the run is cancelled
    assert "tool_result" not in types                              # a denied call gets no tool_result
    (approval,) = srv.messages("approval")
    assert approval["payload"] == {"call_id": call, "decision": "deny", "scope": "once"}


# ---- fail closed ------------------------------------------------------------------------------------------

def test_with_nothing_installed_any_tool_call_fails_the_run_as_a_policy_denial(project):
    with ThreadedServer(scripted(_denied_tail)) as srv:
        out = call_headless(srv, TASK, output_format="json")
    assert out.code == 3
    doc = out.json()
    assert doc["ok"] is False and doc["exit_code"] == 3
    assert doc["error"]["code"] == "policy_denied"
    assert "bash" in doc["error"]["message"] and "policy" in doc["error"]["message"]
    assert doc["result"] == "I'll look."                           # what the model said before is kept
    _assert_denied_first_then_cancelled(srv)
    srv.assert_clean()


def test_the_denial_message_names_the_tool_but_never_its_input(project):
    async def tail(conn, stream, start, user):
        await _propose(conn, stream, start, user, tool="bash", input={"command": "cat /etc/hosts-9c1d"}, risk="R2")
        await expect(conn, "approval")

    with ThreadedServer(scripted(tail)) as srv:
        out = call_headless(srv, TASK, output_format="json")
    assert "hosts-9c1d" not in out.out + out.err


@pytest.mark.parametrize("answer", ["ask", "deny"])
def test_ask_and_deny_both_fail_the_run_with_exit_3_and_run_nothing(project, answer):
    ran = []
    with ThreadedServer(scripted(_denied_tail)) as srv:
        out = call_headless(srv, TASK, output_format="json", decide=lambda p: answer, execute=ran.append)
    assert out.code == 3 and ran == []
    message = out.json()["error"]["message"]
    assert ("approval" in message) if answer == "ask" else ("denies" in message)
    _assert_denied_first_then_cancelled(srv)


def _raises(_proposal):
    raise RuntimeError("leaky detail that must not be printed")


@pytest.mark.parametrize("decide", [_raises, lambda p: None, lambda p: "yes", lambda p: "ALLOW ", lambda p: 1,
                                    lambda p: object(), lambda p: True])
def test_an_exception_or_a_malformed_answer_from_the_policy_is_a_denial(project, decide):
    ran = []
    with ThreadedServer(scripted(_denied_tail)) as srv:
        out = call_headless(srv, TASK, output_format="json", decide=decide, execute=ran.append)
    assert out.code == 3 and ran == []
    assert "leaky detail" not in out.out + out.err and "Traceback" not in out.err
    _assert_denied_first_then_cancelled(srv)


def test_an_allowed_call_without_an_executor_is_still_a_denial(project):
    with ThreadedServer(scripted(_denied_tail)) as srv:
        out = call_headless(srv, TASK, output_format="json", decide=lambda p: "allow")
    assert out.code == 3
    assert "executor" in out.json()["error"]["message"]
    _assert_denied_first_then_cancelled(srv)


def test_the_text_mode_denial_is_one_line_on_stderr(project):
    with ThreadedServer(scripted(_denied_tail)) as srv:
        out = call_headless(srv, TASK)
    assert out.code == 3
    assert out.out == "I'll look.\n"
    assert "bash" in out.err and "Traceback" not in out.err


# ---- allow: the call runs -------------------------------------------------------------------------------------

def test_an_allowed_call_is_executed_and_answered_with_a_tool_result_alone(project):
    seen = []

    def execute(proposal):
        seen.append(proposal)
        return headless.ToolOutcome(ok=True, output="1\thello\n", exit_code=None, duration_ms=3)

    with ThreadedServer(scripted(_answered_tail)) as srv:
        out = call_headless(srv, TASK, output_format="json", decide=lambda p: "allow", execute=execute, mode="accept-edits")
    assert out.code == 0
    assert out.json()["result"] == "I'll look.\nFound it."         # the two assistant messages, one per line
    (proposal,) = seen
    assert (proposal.call_id, proposal.tool, proposal.input, proposal.risk) == (CALL, "read_file", {"path": "notes.txt"}, "R0")
    assert proposal.mode == "accept-edits"                          # the policy needs the mode ...
    assert proposal.project_path == str(project)                    # ... and the root of the project
    (result,) = srv.messages("tool_result")
    assert result["payload"] == {"call_id": CALL, "ok": True, "output": "1\thello\n", "truncated": False,
                                 "trust": "local_project", "duration_ms": 3}
    assert srv.messages("approval") == []                           # auto-allowed: no approval message
    srv.assert_clean()


def test_the_two_assistant_messages_are_separated_in_text_mode_too(project):
    with ThreadedServer(scripted(_answered_tail)) as srv:
        out = call_headless(srv, TASK, decide=lambda p: "allow", execute=lambda p: headless.ToolOutcome(True, "ok"))
    assert out.code == 0 and out.out == "I'll look.\nFound it.\n"


def test_the_policy_may_answer_with_an_object_that_has_a_decision(project):
    class Verdict:
        decision = "allow"
        reason = "a rule said so"

    with ThreadedServer(scripted(_answered_tail)) as srv:
        out = call_headless(srv, TASK, decide=lambda p: Verdict(), execute=lambda p: headless.ToolOutcome(True, "ok"))
    assert out.code == 0


def test_an_async_executor_and_a_dict_result_are_accepted(project):
    async def execute(proposal):
        await asyncio.sleep(0)
        return {"ok": True, "output": "done", "truncated": True, "trust": "local_other", "exit_code": 0, "duration_ms": 9}

    with ThreadedServer(scripted(_answered_tail)) as srv:
        out = call_headless(srv, TASK, decide=lambda p: "allow", execute=execute)
    assert out.code == 0
    (result,) = srv.messages("tool_result")
    assert result["payload"] == {"call_id": CALL, "ok": True, "output": "done", "truncated": True,
                                 "trust": "local_other", "exit_code": 0, "duration_ms": 9}


def test_a_failing_executor_is_a_failed_tool_result_not_a_failed_run(project):
    def execute(proposal):
        raise OSError("leaky detail that must not be sent")

    with ThreadedServer(scripted(_answered_tail)) as srv:
        out = call_headless(srv, TASK, output_format="json", decide=lambda p: "allow", execute=execute)
    assert out.code == 0 and out.json()["result"].endswith("Found it.")
    (result,) = srv.messages("tool_result")
    assert result["payload"]["ok"] is False and "OSError" in result["payload"]["output"]
    assert "leaky detail" not in json.dumps(srv.messages("tool_result")) + out.err


@pytest.mark.parametrize("bad", [None, "text", 42, {"ok": "yes", "output": "x"}, {"ok": True},
                                 {"ok": True, "output": "x", "trust": "somewhere"}])
def test_an_unusable_executor_result_is_a_failed_tool_result(project, bad):
    with ThreadedServer(scripted(_answered_tail)) as srv:
        out = call_headless(srv, TASK, decide=lambda p: "allow", execute=lambda p: bad)
    assert out.code == 0
    (result,) = srv.messages("tool_result")
    assert result["payload"]["ok"] is False and result["payload"]["trust"] == "local_project"


def test_calls_are_decided_in_order_once_each_and_a_closed_call_is_not_run(project):
    order = []

    async def tail(conn, stream, _start, _user):
        await stream.text("two reads")
        await stream.done("tool_use")
        await stream.usage()
        await stream.proposed("c1", "read_file", {"path": "a.txt"})
        await stream.proposed("c1", "read_file", {"path": "a.txt"})      # a re-delivery of the same call
        await stream.proposed("c2", "read_file", {"path": "b.txt"})
        await stream.tool_finished("c2", "read_file", "timeout")          # the server closed c2 before the device got to it
        await expect(conn, "tool_result")
        await stream.tool_finished("c1", "read_file", "ok")
        await stream.reply("done")

    async def execute(proposal):
        order.append(proposal.call_id)
        await asyncio.sleep(0.2)                                          # c1 is still running when c2's tool_finished arrives
        return headless.ToolOutcome(True, "ok")

    decided = []

    def decide(proposal):
        decided.append(proposal.call_id)
        return "allow"

    with ThreadedServer(scripted(tail)) as srv:
        out = call_headless(srv, TASK, decide=decide, execute=execute)
    assert out.code == 0
    assert order == ["c1"] and decided == ["c1"]                          # once; c2 was closed, so it never ran
    assert [m["payload"]["call_id"] for m in srv.messages("tool_result")] == ["c1"]


# ---- the seam used by the integration patch ----------------------------------------------------------------------

def test_installed_hooks_are_what_a_run_uses_when_none_are_passed(capsys, project):
    ran = []
    headless.install_tool_hooks(decide=lambda p: "allow", execute=lambda p: (ran.append(p.tool), headless.ToolOutcome(True, "ok"))[1])
    out = run_cli(capsys, ["-p", TASK], scripted(_answered_tail))
    assert out.code == 0 and ran == ["read_file"]
    headless.clear_tool_hooks()
    out = run_cli(capsys, ["-p", TASK], scripted(_denied_tail))
    assert out.code == 3                                                  # cleared: back to deny-everything


def test_plan_mode_proposals_carry_the_mode_and_plan_exit_is_just_another_call(capsys, project):
    """A headless plan run that ends with ``plan_exit`` needs somebody to approve leaving plan mode.  Nobody can: if
    the policy says ``ask`` the run fails as a denial (exit 3).  Whether I02 should treat plan_exit differently is a
    question for the policy engine, not for this module."""
    modes = []

    def decide(proposal):
        modes.append((proposal.mode, proposal.tool))
        return "ask"

    async def tail(conn, stream, start, user):
        await _propose(conn, stream, start, user, tool="plan_exit", input={"plan": "1. do it"}, risk="R0")
        await expect(conn, "approval")

    headless.install_tool_hooks(decide=decide)
    out = run_cli(capsys, ["-p", TASK, "--mode", "plan"], scripted(tail))
    assert out.code == 3 and modes == [("plan", "plan_exit")]
