"""
m2c/tests/runner/test_runner_headless.py -- Patch C24: the adapter for C10's ``install_tool_hooks``.

C10 (headless mode) takes two injected callables, ``decide(proposal)`` and ``execute(proposal)``, and runs a tool only
on an explicit ``allow``.  ``ToolRunner.decide`` / ``ToolRunner.execute`` are shaped for it.  These tests use C10's
OWN hook types and its real ``run_headless`` against the fake device server of the headless tests, so "the hook types
accept them" is proved by the code that consumes them, and so is "a would-ask fails closed there".
"""
from __future__ import annotations

import sys
from pathlib import Path

_HEADLESS_TESTS = str(Path(__file__).resolve().parents[1] / "headless")
if _HEADLESS_TESTS not in sys.path:
    sys.path.insert(0, _HEADLESS_TESTS)

from headless_rig import *  # noqa: E402,F401,F403  (the autouse fixtures are re-exported on purpose)
from headless_rig import ThreadedServer, call_headless, expect, scripted  # noqa: E402
from runner_rig import make_layers, run  # noqa: E402

from m2c import headless  # noqa: E402
from m2c.policy import Context  # noqa: E402
from m2c.runner import ToolRunner  # noqa: E402
from m2c.store import open_db  # noqa: E402
from m2c.tools import ToolContext  # noqa: E402

CALL = "call_h_1"


def adapter(project: Path, *, mode="ask", allow=(), ask=(), deny=()) -> ToolRunner:
    """A runner for a headless run: no link of its own (headless owns it), a store, the project's tools."""
    layers = make_layers(allow, ask, deny)
    return ToolRunner(None, run_id=None, tool_context=ToolContext(project),
                      policy_context=lambda: Context(mode=mode, layers=layers, project_root=str(project), project_trusted=True),
                      store=open_db(), replay_settle_s=0.0)


async def _proposal_tail(tool, input, risk, expect_back):
    async def tail(conn, stream, _start, _user):
        await stream.text("working")
        await stream.done("tool_use")
        await stream.usage()
        await stream.proposed(CALL, tool, input, risk)
        await expect(conn, expect_back)
        if expect_back == "tool_result":
            await stream.tool_finished(CALL, tool, "ok")
            await stream.reply("done")
    return tail


def serve(tool, input, risk, expect_back):
    async def tail(conn, stream, _start, _user):
        await stream.text("working")
        await stream.done("tool_use")
        await stream.usage()
        await stream.proposed(CALL, tool, input, risk)
        await expect(conn, expect_back)
        if expect_back == "tool_result":
            await stream.tool_finished(CALL, tool, "ok")
            await stream.reply("done")
    return scripted(tail)


# ---- the shapes C10 accepts -----------------------------------------------------------------------------------

def test_the_hooks_are_coroutine_functions_taking_one_proposal(project):
    import inspect
    runner = adapter(project)
    assert inspect.iscoroutinefunction(runner.decide) and inspect.iscoroutinefunction(runner.execute)
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)       # C10's own installer takes them


def test_decide_answers_only_with_the_three_words_c10_knows(project):
    (project / "a.txt").write_text("x")
    runner = adapter(project, allow=["bash(echo:*)"], deny=["bash(curl:*)"])

    def verdict(tool, input, risk="R0", mode="ask"):
        return run(runner.decide(headless.ToolProposal("c" + str(abs(hash((tool, str(input))))), tool, input, risk,
                                                       mode, str(project))))

    assert verdict("read_file", {"path": "a.txt"}) == "allow"
    assert verdict("bash", {"command": "echo hi"}, "R2") == "allow"
    assert verdict("bash", {"command": "ls"}, "R2") == "ask"                        # not allowed by any rule: would ask
    assert verdict("bash", {"command": "curl http://x.invalid"}, "R2") == "deny"
    assert verdict("nope", {}) == "deny" and verdict("read_file", {"path": 1}) == "deny"


def test_decide_never_loosens_the_runner_s_mode(project):
    (project / "a.txt").write_text("x")
    runner = adapter(project, mode="plan")
    got = run(runner.decide(headless.ToolProposal("c1", "write_file", {"path": "a.txt", "content": "x"}, "R1",
                                                  "accept-edits", str(project))))
    assert got == "deny"                                                             # the stricter of the two modes wins


def test_execute_returns_a_headless_tool_outcome(project):
    (project / "a.txt").write_text("hello\n")
    runner = adapter(project)
    proposal = headless.ToolProposal("c1", "read_file", {"path": "a.txt"}, "R0", "ask", str(project))
    outcome = run(runner.execute(proposal))
    assert isinstance(outcome, headless.ToolOutcome)
    assert outcome.ok and "hello" in outcome.output and outcome.trust == "local_project"
    assert headless._coerce_outcome(outcome) is outcome                              # C10 accepts it as it is


def test_execute_refuses_what_decide_would_not_allow(project):
    runner = adapter(project)
    outcome = run(runner.execute(headless.ToolProposal("c1", "bash", {"command": "echo ran > ran.txt"}, "R2", "ask",
                                                        str(project))))
    assert outcome.ok is False and not (project / "ran.txt").exists()               # an ask is not an allow, even here


def test_execute_never_runs_a_call_id_twice(project):
    (project / "a.txt").write_text("hello\n")
    runner = adapter(project)
    proposal = headless.ToolProposal("c1", "read_file", {"path": "a.txt"}, "R0", "ask", str(project))
    first = run(runner.execute(proposal))
    second = run(runner.execute(proposal))
    assert first.ok and second.ok is False and "already" in second.output


def test_execute_without_a_bound_run_cannot_checkpoint_so_it_refuses_changes(project):
    """Fail closed: no session to record the checkpoint in means no change."""
    runner = adapter(project, mode="accept-edits")
    outcome = run(runner.execute(headless.ToolProposal("c1", "write_file", {"path": "n.txt", "content": "1"}, "R1",
                                                        "accept-edits", str(project))))
    assert outcome.ok is False and not (project / "n.txt").exists()


# ---- through C10's own run_headless ------------------------------------------------------------------------

def test_an_allowed_read_runs_through_run_headless(project):
    (project / "notes.txt").write_text("The colour of the sky is blue.\n")
    runner = adapter(project)
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    with ThreadedServer(serve("read_file", {"path": "notes.txt"}, "R0", "tool_result")) as srv:
        out = call_headless(srv, "read it", output_format="json")
    assert out.code == 0, out.err
    (result,) = srv.messages("tool_result")
    assert result["payload"]["ok"] is True and "colour of the sky" in result["payload"]["output"]
    assert result["payload"]["trust"] == "local_project"
    assert srv.messages("approval") == []                                           # C10: an auto-allowed call has no approval


def test_a_would_ask_fails_the_headless_run_closed_with_exit_3(project):
    runner = adapter(project)                                                       # no rule allows `ls`: C06 says ask
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    with ThreadedServer(serve("bash", {"command": "echo ran > ran.txt"}, "R2", "approval")) as srv:
        out = call_headless(srv, "run it", output_format="json")
    assert out.code == 3 and not (project / "ran.txt").exists()
    assert srv.types()[-2:] == ["approval", "cancel"] and srv.messages("tool_result") == []
    (approval,) = srv.messages("approval")
    assert approval["payload"] == {"call_id": CALL, "decision": "deny", "scope": "once"}
    assert "approval" in out.json()["error"]["message"]                             # C10's "would need approval" message
    assert runner.would_ask == 1 and runner.policy_denials == 1


def test_an_explicit_allow_rule_runs_a_command_through_run_headless(project):
    runner = adapter(project, allow=["bash(echo:*)"])
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    with ThreadedServer(serve("bash", {"command": "echo from-headless"}, "R2", "tool_result")) as srv:
        out = call_headless(srv, "run it", output_format="json")
    assert out.code == 0, out.err
    (result,) = srv.messages("tool_result")
    assert result["payload"]["ok"] and "from-headless" in result["payload"]["output"] and result["payload"]["trust"] == "external"
    assert result["payload"]["exit_code"] == 0


def test_a_policy_deny_fails_the_headless_run_with_exit_3(project):
    runner = adapter(project, deny=["read_file(notes*)"])
    (project / "notes.txt").write_text("secret\n")
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    with ThreadedServer(serve("read_file", {"path": "notes.txt"}, "R0", "approval")) as srv:
        out = call_headless(srv, "read it", output_format="json")
    assert out.code == 3 and srv.messages("tool_result") == [] and runner.policy_denials == 1
