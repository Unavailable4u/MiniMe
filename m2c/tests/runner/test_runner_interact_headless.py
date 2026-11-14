"""
m2c/tests/runner/test_runner_interact_headless.py -- Patch L01a: ``m2c -p`` and the two interactive tools.

Headless mode has nobody to ask.  With the runner installed as C10's tool hooks, ``ask_user`` must come back as the
fixed "no person is available to answer" result (``ok`` false) and the run must go on to finish: it never hangs.
``tasks`` still works (there may be no screen, but the list is validated, recorded locally and confirmed).

Until the runner is wired into ``m2c -p`` for real (not part of this patch), a headless run with NO hooks installed
denies every tool call, these two included, exactly as before; that is pinned too.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HEADLESS_TESTS = str(Path(__file__).resolve().parents[1] / "headless")
if _HEADLESS_TESTS not in sys.path:
    sys.path.insert(0, _HEADLESS_TESTS)

from headless_rig import *  # noqa: E402,F401,F403  (the autouse fixtures are re-exported on purpose)
from headless_rig import ThreadedServer, call_headless, expect, scripted  # noqa: E402
from runner_rig import make_layers  # noqa: E402

from m2c import headless  # noqa: E402
from m2c.interact.tasks import Task  # noqa: E402
from m2c.policy import Context  # noqa: E402
from m2c.runner import MSG_NO_PERSON, ToolRunner  # noqa: E402
from m2c.store import open_db  # noqa: E402
from m2c.tools import ToolContext  # noqa: E402

CALL = "call_h_ask_1"
ASK = {"question": "Which database?", "options": [{"label": "SQLite"}, {"label": "Postgres"}]}


def adapter(project: Path, **kwargs) -> ToolRunner:
    layers = make_layers()
    return ToolRunner(None, run_id=None, tool_context=ToolContext(project),
                      policy_context=lambda: Context(mode="ask", layers=layers, project_root=str(project), project_trusted=True),
                      store=open_db(), replay_settle_s=0.0, **kwargs)


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


def test_headless_answers_ask_user_with_the_fixed_error_and_the_run_finishes(project):
    runner = adapter(project)                                           # no question callable: nobody can answer
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    with ThreadedServer(serve("ask_user", ASK, "R0", "tool_result")) as srv:
        out = call_headless(srv, "ask me something", output_format="json")
    assert out.code == 0, out.err                                       # it finished: no hang, no policy failure
    (result,) = srv.messages("tool_result")
    assert result["payload"]["call_id"] == CALL
    assert result["payload"]["ok"] is False and result["payload"]["output"] == MSG_NO_PERSON
    assert "no person is available to answer" in result["payload"]["output"]
    assert srv.messages("approval") == []                               # C10: an auto-allowed call has no approval
    assert runner.would_ask == 0 and runner.policy_denials == 0


def test_headless_runs_tasks_through_the_sink_and_confirms(project):
    sink: list = []
    runner = adapter(project, task_sink=sink.append)
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    tasks = {"tasks": [{"id": "t1", "title": "Read", "status": "done"},
                       {"id": "t2", "title": "Write", "status": "pending", "depends_on": ["t1"]}]}
    with ThreadedServer(serve("tasks", tasks, "R0", "tool_result")) as srv:
        out = call_headless(srv, "plan it", output_format="json")
    assert out.code == 0, out.err
    (result,) = srv.messages("tool_result")
    assert result["payload"]["ok"] is True
    assert result["payload"]["output"] == "Task list updated: 2 tasks (1 pending, 0 in progress, 1 done, 0 blocked)."
    assert sink == [(Task("t1", "Read", "done", ()), Task("t2", "Write", "pending", ("t1",)))]


def test_headless_reports_a_bad_list_as_a_fixed_error_and_goes_on(project):
    sink: list = []
    runner = adapter(project, task_sink=sink.append)
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    cycle = {"tasks": [{"id": "a", "title": "A", "status": "pending", "depends_on": ["b"]},
                       {"id": "b", "title": "B", "status": "pending", "depends_on": ["a"]}]}
    with ThreadedServer(serve("tasks", cycle, "R0", "tool_result")) as srv:
        out = call_headless(srv, "plan it", output_format="json")
    assert out.code == 0, out.err
    (result,) = srv.messages("tool_result")
    assert result["payload"]["ok"] is False and "cycle" in result["payload"]["output"] and sink == []


def test_a_malformed_ask_user_is_a_policy_denial_in_headless_mode(project):
    runner = adapter(project)
    headless.install_tool_hooks(decide=runner.decide, execute=runner.execute)
    with ThreadedServer(serve("ask_user", {"question": 5}, "R0", "approval")) as srv:
        out = call_headless(srv, "ask me something", output_format="json")
    assert out.code == 3 and srv.messages("tool_result") == [] and runner.policy_denials == 1


def test_with_no_hooks_installed_headless_still_denies_every_tool_including_these(project):
    # the base state: the runner is not wired into ``m2c -p`` yet, so nothing runs and the run fails closed
    for tool, args in (("ask_user", ASK), ("tasks", {"tasks": []})):
        with ThreadedServer(serve(tool, args, "R0", "approval")) as srv:
            out = call_headless(srv, "go", output_format="json")
        assert out.code == 3 and srv.messages("tool_result") == [], tool
