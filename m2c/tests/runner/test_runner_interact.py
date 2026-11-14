"""
m2c/tests/runner/test_runner_interact.py -- Patch L01a: ``ask_user`` and ``tasks`` through the tool runner.

Both tools run on the DEVICE and, like ``plan_exit``, have no ``run_tool`` executor: the runner handles them itself
after C06 has decided and after the argument checks every tool gets.  A fake screen (the ``question`` callable) and a
fake task sink stand in for the terminal UI that L01b writes.  Pinned here:

* a run PAUSES on a question and continues with the person's answer (an option, several, free text, both);
* a decline, a timeout, a cancel and a missing screen each end the call with a fixed message and never hang;
* ``tasks`` replaces the list through the sink, writes one LOCAL event per update and answers with counts;
* nothing the model wrote is echoed into a log, and the person's answer is redacted on its way out.
"""
from __future__ import annotations

import asyncio
import random
import string

import pytest
from runner_rig import MARKER, capture_runner_logs, finished, make_rig, run, state_changed

from m2c import runner as runner_module
from m2c.interact.question import Declined, UserAnswer, UserQuestion
from m2c.interact.tasks import EVENT_TASKS, Task
from m2c.runner import (
    MSG_DECLINED,
    MSG_NO_PERSON,
    MSG_QUESTION_CANCELLED,
    MSG_QUESTION_FAILED,
    MSG_QUESTION_TIMED_OUT,
    MSG_TASKS_NOT_SHOWN,
    Choice,
)
from m2c.interact import question as question_module
from m2c.store.events import read_events

OPTIONS = [{"label": "SQLite", "description": "one file"}, {"label": "Postgres"}, {"label": "Redis"}]
ASK = {"question": "Which database?", "options": OPTIONS}


def screen_returning(answer, seen=None):
    async def screen(request):
        if seen is not None:
            seen.append(request)
        return answer
    return screen


def tasks_in(*items):
    return {"tasks": [dict(i) for i in items]}


def item(id_, title="do it", status="pending", **extra):
    return {"id": id_, "title": title, "status": status, **extra}


def local_events(rig, event_type):
    return [e for e in read_events(rig.store, rig.session_id) if e.type == event_type]


# ---- ask_user: the run pauses, then continues with the answer -----------------------------------------------

def test_a_run_pauses_on_a_question_and_continues_with_the_answer(tmp_path):
    seen: list[UserQuestion] = []
    gate_open: dict = {}

    async def scenario():
        gate = asyncio.Event()
        started = asyncio.Event()

        async def screen(request):
            seen.append(request)
            started.set()
            await gate.wait()                                   # the person is thinking
            return UserAnswer(choices=("Postgres",))

        rig = make_rig(tmp_path, question=screen)
        rig.propose("c_ask", "ask_user", ASK)
        await started.wait()
        await asyncio.sleep(0.05)
        gate_open["before"] = list(rig.link.types())            # nothing but the approval while waiting
        gate_open["result_before"] = rig.link.for_call("c_ask", "tool_result")
        gate.set()
        await rig.runner.wait_idle()
        return rig

    rig = run(scenario())
    assert gate_open["result_before"] == [] and gate_open["before"] == ["approval"]
    assert rig.link.types() == ["approval", "tool_result"]
    assert rig.link.approval("c_ask") == {"call_id": "c_ask", "decision": "allow", "scope": "once"}
    result = rig.link.result("c_ask")
    assert result["ok"] is True and result["output"] == "The person answered.\nChose: Postgres"
    assert result["trust"] == "local_other" and result["truncated"] is False
    (request,) = seen
    assert isinstance(request, UserQuestion) and request.question == "Which database?"
    assert [o.label for o in request.options] == ["SQLite", "Postgres", "Redis"]
    assert request.options[0].description == "one file"
    assert request.allow_free_text is True and request.timeout_s == 600           # the schema defaults
    assert rig.asked == []                                                         # no approval prompt, ever


@pytest.mark.parametrize("answer,expected", [
    (UserAnswer(choices=("SQLite",)), "The person answered.\nChose: SQLite"),
    (UserAnswer(choices=("Redis", "SQLite")), "The person answered.\nChose: Redis\nChose: SQLite"),
    (UserAnswer(text="Use DuckDB"), "The person answered.\nWrote: Use DuckDB"),
    (UserAnswer(choices=("Postgres",), text="v16"), "The person answered.\nChose: Postgres\nWrote: v16"),
])
def test_one_option_several_options_free_text_and_both(tmp_path, answer, expected):
    rig = make_rig(tmp_path, question=screen_returning(answer))
    run(rig.call("c1", "ask_user", ASK))
    result = rig.link.result("c1")
    assert result["ok"] is True and result["output"] == expected, (answer, result)


def test_an_open_question_with_no_options_takes_free_text(tmp_path):
    seen = []
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text="Orion"), seen))
    run(rig.call("c1", "ask_user", {"question": "What should the project be called?"}))
    assert rig.link.result("c1")["output"] == "The person answered.\nWrote: Orion"
    assert seen[0].options == ()


def test_the_given_flags_reach_the_screen(tmp_path):
    seen = []
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(choices=("A",)), seen))
    run(rig.call("c1", "ask_user", {"question": "q", "options": [{"label": "A"}, {"label": "B"}],
                                    "allow_free_text": False, "timeout_s": 45}))
    assert seen[0].allow_free_text is False and seen[0].timeout_s == 45


def test_the_answer_is_cut_and_flagged_when_it_is_huge(tmp_path):
    big = "y" * (question_module.MAX_ANSWER_CHARS + 10)
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text=big)))
    run(rig.call("c1", "ask_user", {"question": "Paste it"}))
    result = rig.link.result("c1")
    assert result["ok"] is True and result["truncated"] is True and big not in result["output"]


# ---- ask_user: every way it can end without an answer ----------------------------------------------------------

def test_a_decline_is_ok_false_with_the_fixed_message(tmp_path):
    rig = make_rig(tmp_path, question=screen_returning(Declined()))
    run(rig.call("c1", "ask_user", ASK))
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"] == MSG_DECLINED and "declined" in MSG_DECLINED
    assert result["truncated"] is False


def test_without_a_screen_the_call_fails_fast_with_the_fixed_message(tmp_path):
    rig = make_rig(tmp_path)                                    # no question callable: headless / non-interactive
    run(rig.call("c1", "ask_user", ASK), timeout=5)
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"] == MSG_NO_PERSON
    assert "no person is available to answer" in MSG_NO_PERSON
    assert rig.link.types() == ["approval", "tool_result"]


def test_a_timeout_dismisses_the_screen_and_returns_the_fixed_message(tmp_path):
    state = {"dismissed": False}

    async def screen(request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["dismissed"] = True                           # the screen must take the question down
            raise

    rig = make_rig(tmp_path, question=screen)
    run(rig.call("c1", "ask_user", {"question": "Still there?", "timeout_s": 1}), timeout=10)
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"] == MSG_QUESTION_TIMED_OUT and state["dismissed"]


def test_the_timeout_starts_when_the_question_is_shown_not_when_it_was_proposed(tmp_path):
    # an approval prompt for another call holds the one terminal for 1.3 s; the question (timeout 1 s) must still get
    # its full second once it is shown.
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "a.txt").write_text("x\n")

    async def scenario():
        gate = asyncio.Event()
        shown_at: dict = {}

        async def approving(request):
            await gate.wait()
            return Choice("allow")

        async def screen(request):
            shown_at["t"] = asyncio.get_running_loop().time()
            return UserAnswer(text="ok")

        rig = make_rig(tmp_path, ask=["read_file(*)"], ask_fn=lambda r: approving(r), question=screen)
        rig.propose("c_read", "read_file", {"path": "a.txt"})
        await asyncio.sleep(0.05)
        rig.propose("c_ask", "ask_user", {"question": "q", "timeout_s": 1}, seq=2)
        await asyncio.sleep(1.3)
        assert "t" not in shown_at                              # one prompt at a time: the question waits its turn
        gate.set()
        await rig.runner.wait_idle()
        return rig

    rig = run(scenario(), timeout=15)
    assert rig.link.result("c_ask")["ok"] is True


def test_interrupt_releases_a_pending_question_and_sends_nothing(tmp_path):
    state = {"dismissed": False}

    async def scenario():
        started = asyncio.Event()

        async def screen(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["dismissed"] = True
                raise

        rig = make_rig(tmp_path, question=screen)
        rig.propose("c1", "ask_user", ASK)
        await started.wait()
        assert rig.runner.interrupt() == 1
        await asyncio.wait_for(rig.runner.wait_idle(), 5)
        return rig

    rig = run(scenario())
    assert state["dismissed"] is True
    assert rig.link.payloads("tool_result") == []                # the server closes a cancelled call
    assert MSG_QUESTION_CANCELLED not in str(rig.link.payloads("tool_result"))


@pytest.mark.parametrize("how", ["call_finished", "run_cancelled", "aclose"])
def test_the_server_closing_the_call_or_the_run_releases_the_question(tmp_path, how):
    state = {"dismissed": False}

    async def scenario():
        started = asyncio.Event()

        async def screen(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["dismissed"] = True
                raise

        rig = make_rig(tmp_path, question=screen)
        rig.propose("c1", "ask_user", ASK)
        await started.wait()
        if how == "call_finished":
            rig.runner.observe(finished("c1", "ask_user", "timeout"))
        elif how == "run_cancelled":
            rig.runner.observe(state_changed("cancelled"))
        else:
            await rig.runner.aclose()
        await asyncio.wait_for(rig.runner.wait_idle(), 5)
        return rig

    rig = run(scenario())
    assert state["dismissed"] is True and rig.link.payloads("tool_result") == []


def test_a_screen_that_raises_gives_the_fixed_failure_and_logs_only_the_class(tmp_path, monkeypatch):
    logs = capture_runner_logs(monkeypatch)

    async def broken(request):
        raise RuntimeError(f"boom {MARKER}")

    rig = make_rig(tmp_path, question=broken)
    run(rig.call("c1", "ask_user", {"question": f"q {MARKER}"}))
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"] == MSG_QUESTION_FAILED
    assert MARKER not in logs.text and MARKER not in result["output"]


@pytest.mark.parametrize("bad", [
    UserAnswer(choices=("Mongo",)), UserAnswer(), UserAnswer(text="  "), "Postgres", None, 5, object(),
    UserAnswer(choices=("SQLite", "SQLite")),
])
def test_an_answer_the_screen_should_never_give_is_refused_fail_closed(tmp_path, bad):
    rig = make_rig(tmp_path, question=screen_returning(bad))
    run(rig.call("c1", "ask_user", ASK))
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"] == question_module.BAD_ANSWER


def test_free_text_the_question_did_not_allow_is_refused(tmp_path):
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text="my own")))
    run(rig.call("c1", "ask_user", {"question": "q", "options": [{"label": "A"}], "allow_free_text": False}))
    assert rig.link.result("c1")["output"] == question_module.BAD_ANSWER


# ---- ask_user: arguments get the same checks as every tool ----------------------------------------------------

@pytest.mark.parametrize("args", [
    {}, {"question": 5}, {"question": "q", "options": "A"}, {"question": "q", "timeout_s": 0},
    {"question": "q", "timeout_s": 3601}, {"question": "q", "allow_free_text": "yes"}, {"question": "q", "extra": 1},
])
def test_malformed_arguments_are_denied_by_the_policy_and_the_screen_never_sees_them(tmp_path, args):
    seen = []
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text="x"), seen))
    run(rig.call("c1", "ask_user", args))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.payloads("tool_result") == [] and seen == []
    assert rig.runner.policy_denials == 1


@pytest.mark.parametrize("args,needle", [
    ({"question": "q", "options": [{"label": str(i)} for i in range(7)]}, "options"),
    ({"question": "q", "options": [{"label": "A"}, {"label": "A"}]}, "unique"),
    ({"question": "q", "options": [{"description": "no label"}]}, "option"),
    ({"question": "q", "allow_free_text": False}, "allow_free_text"),
    ({"question": "x" * 2001}, "question"),
    ({"question": "bad \x1b[31m text"}, "question"),
    ({"question": "   "}, "question"),
])
def test_nested_rules_fail_with_a_fixed_message_and_the_screen_never_sees_them(tmp_path, monkeypatch, args, needle):
    logs = capture_runner_logs(monkeypatch)
    seen = []
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text="x"), seen))
    run(rig.call("c1", "ask_user", args))
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"].startswith("error: invalid question: ") and needle in result["output"]
    assert seen == []


def test_the_models_question_text_never_reaches_a_log_or_the_audit_record(tmp_path, monkeypatch):
    logs = capture_runner_logs(monkeypatch)
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text=f"answer {MARKER}")))
    run(rig.call("c1", "ask_user", {"question": f"question {MARKER}", "options": [{"label": f"label {MARKER}"}]}))
    run(rig.call("c2", "ask_user", {"question": f"question {MARKER}", "options": [{"label": "A"}] * 2}))   # invalid
    assert MARKER not in logs.text
    dump = "\n".join(str(tuple(r)) for r in rig.store.execute("SELECT * FROM events")) + "\n" + \
           "\n".join(str(tuple(r)) for r in rig.store.execute("SELECT * FROM approvals"))
    assert MARKER not in dump                                      # nothing at rest on the device from the runner


# ---- ask_user: the person's answer is redacted like every result ----------------------------------------------

def test_a_secret_in_the_answer_is_redacted_on_the_way_out(tmp_path):
    rng = random.Random(20260101)
    fake_key = "AKIA" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    rig = make_rig(tmp_path, question=screen_returning(UserAnswer(text=f"use this key {fake_key} please")))
    run(rig.call("c1", "ask_user", {"question": "Which key?"}))
    result = rig.link.result("c1")
    assert result["ok"] is True and fake_key not in result["output"] and "please" in result["output"]


# ---- ask_user: ordering and the one terminal ------------------------------------------------------------------

def test_a_later_change_waits_for_the_answer_but_a_read_does_not(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "a.txt").write_text("read me\n")
    seen: dict = {}

    async def scenario():
        gate = asyncio.Event()
        started = asyncio.Event()

        async def screen(request):
            started.set()
            await gate.wait()
            return UserAnswer(choices=("Yes",))

        rig = make_rig(tmp_path, mode="accept-edits", question=screen)
        rig.propose("c_ask", "ask_user", {"question": "Write it?", "options": [{"label": "Yes"}],
                                          "allow_free_text": False})
        await started.wait()
        rig.propose("c_write", "write_file", {"path": "out.txt", "content": "hi\n"}, "R1", seq=2)
        rig.propose("c_read", "read_file", {"path": "a.txt"}, seq=3)
        await asyncio.sleep(0.3)
        seen["written_early"] = (rig.root / "out.txt").exists()
        seen["read_early"] = rig.link.for_call("c_read", "tool_result")
        gate.set()
        await rig.runner.wait_idle()
        return rig

    rig = run(scenario())
    assert seen["written_early"] is False and len(seen["read_early"]) == 1      # the edit waits, the read does not
    assert (rig.root / "out.txt").read_text() == "hi\n"
    assert rig.link.result("c_ask")["output"].endswith("Chose: Yes")


def test_a_question_is_never_shown_while_an_approval_prompt_is_open(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "a.txt").write_text("x\n")
    seen: dict = {"question_started": False}

    async def scenario():
        gate = asyncio.Event()

        async def approving(request):
            await gate.wait()
            return Choice("allow")

        async def screen(request):
            seen["question_started"] = True
            return UserAnswer(text="ok")

        rig = make_rig(tmp_path, ask=["read_file(*)"], ask_fn=lambda r: approving(r), question=screen)
        rig.propose("c_read", "read_file", {"path": "a.txt"})
        await asyncio.sleep(0.05)
        rig.propose("c_ask", "ask_user", {"question": "q"}, seq=2)
        await asyncio.sleep(0.3)
        seen["during"] = seen["question_started"]
        gate.set()
        await rig.runner.wait_idle()
        return rig

    rig = run(scenario())
    assert seen["during"] is False and seen["question_started"] is True
    assert rig.link.result("c_ask")["ok"] is True and rig.link.result("c_read")["ok"] is True


@pytest.mark.parametrize("mode", ["plan", "ask", "accept-edits"])
def test_both_tools_run_in_every_mode_without_an_approval_prompt(tmp_path, mode):
    sink = []
    rig = make_rig(tmp_path, mode=mode, question=screen_returning(UserAnswer(text="fine")), task_sink=sink.append)
    run(rig.call("c1", "ask_user", {"question": "q"}))
    run(rig.call("c2", "tasks", tasks_in(item("a"))))
    assert rig.link.result("c1")["ok"] is True and rig.link.result("c2")["ok"] is True
    assert rig.asked == [] and len(sink) == 1


def test_a_deny_rule_for_ask_user_blocks_it(tmp_path):
    seen = []
    rig = make_rig(tmp_path, deny=["ask_user(*)"], question=screen_returning(UserAnswer(text="x"), seen))
    run(rig.call("c1", "ask_user", {"question": "q"}))
    assert rig.link.approval("c1")["decision"] == "deny" and seen == [] and rig.link.payloads("tool_result") == []


@pytest.mark.parametrize("kwargs", [{"question": "not callable"}, {"task_sink": 42}])
def test_the_constructor_rejects_something_that_is_not_callable(tmp_path, kwargs):
    with pytest.raises(ValueError):
        make_rig(tmp_path, **kwargs)


# ---- tasks ------------------------------------------------------------------------------------------------------

def test_the_sink_receives_the_list_and_the_model_gets_counts(tmp_path):
    sink: list = []
    rig = make_rig(tmp_path, task_sink=sink.append)
    run(rig.call("c1", "tasks", tasks_in(item("t1", "Read", "done"), item("t2", "Write", "in_progress", depends_on=["t1"]),
                                         item("t3", "Ship", depends_on=["t2"]))))
    assert sink == [(Task("t1", "Read", "done", ()), Task("t2", "Write", "in_progress", ("t1",)),
                     Task("t3", "Ship", "pending", ("t2",)))]
    result = rig.link.result("c1")
    assert result["ok"] is True and result["output"] == "Task list updated: 3 tasks (1 pending, 1 in progress, 1 done, 0 blocked)."
    assert result["trust"] == "local_project" and rig.link.types() == ["approval", "tool_result"]


def test_each_call_replaces_the_whole_list_and_an_empty_list_clears_it(tmp_path):
    sink: list = []
    rig = make_rig(tmp_path, task_sink=sink.append)
    run(rig.call("c1", "tasks", tasks_in(item("a"), item("b"))))
    run(rig.call("c2", "tasks", tasks_in(item("c", status="done"))))
    run(rig.call("c3", "tasks", {"tasks": []}))
    assert [[t.id for t in snapshot] for snapshot in sink] == [["a", "b"], ["c"], []]
    assert rig.link.result("c3")["output"] == "Task list cleared."


def test_one_local_event_is_written_per_update_with_the_whole_list(tmp_path):
    rig = make_rig(tmp_path, task_sink=lambda tasks: None)
    run(rig.call("c1", "tasks", tasks_in(item("a", "First"), item("b", "Second", "blocked", depends_on=["a"]))))
    run(rig.call("c2", "tasks", {"tasks": []}))
    first, second = local_events(rig, EVENT_TASKS)
    assert first.data == {"call_id": "c1", "tasks": [
        {"id": "a", "title": "First", "status": "pending", "depends_on": []},
        {"id": "b", "title": "Second", "status": "blocked", "depends_on": ["a"]}]}
    assert second.data == {"call_id": "c2", "tasks": []}
    assert not EVENT_TASKS.startswith("server.")                   # the link's resume logic only reads "server.*" events


def test_without_a_sink_the_list_is_still_validated_and_recorded(tmp_path):
    rig = make_rig(tmp_path)
    run(rig.call("c1", "tasks", tasks_in(item("a"))))
    assert rig.link.result("c1")["ok"] is True and len(local_events(rig, EVENT_TASKS)) == 1


@pytest.mark.parametrize("raw,rule", [
    ([item("a"), item("a")], "unique"),
    ([item("a", depends_on=["ghost"])], "depends_on"),
    ([item("a", depends_on=["b"]), item("b", depends_on=["a"])], "cycle"),
    ([item(f"t{i}") for i in range(31)], "at most 30"),
    ([item("a", status="stuck")], "status"),
    ([{"id": "a", "title": "x"}], "each task"),
    ([item("a", "bad \x1b[2J title")], "title"),
])
def test_an_invalid_list_is_a_fixed_error_and_changes_nothing(tmp_path, monkeypatch, raw, rule):
    logs = capture_runner_logs(monkeypatch)
    sink: list = []
    rig = make_rig(tmp_path, task_sink=sink.append)
    run(rig.call("c1", "tasks", {"tasks": raw}))
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"].startswith("error: invalid task list: ") and rule in result["output"]
    assert sink == [] and local_events(rig, EVENT_TASKS) == []


def test_the_models_task_text_never_reaches_a_log_or_an_error(tmp_path, monkeypatch):
    logs = capture_runner_logs(monkeypatch)
    rig = make_rig(tmp_path, task_sink=lambda tasks: None)
    run(rig.call("c1", "tasks", tasks_in(item(f"id-{MARKER}", f"title {MARKER}", depends_on=[f"ghost-{MARKER}"]))))
    run(rig.call("c2", "tasks", tasks_in(item(f"id-{MARKER}", f"title {MARKER}"))))
    assert MARKER not in logs.text and MARKER not in rig.link.result("c1")["output"]
    assert MARKER in str(local_events(rig, EVENT_TASKS)[0].data)   # the model's own list IS stored locally (that is its place)


@pytest.mark.parametrize("args", [{}, {"tasks": "t1"}, {"tasks": [], "extra": 1}, {"tasks": None}])
def test_malformed_tasks_arguments_are_denied_by_the_policy(tmp_path, args):
    sink: list = []
    rig = make_rig(tmp_path, task_sink=sink.append)
    run(rig.call("c1", "tasks", args))
    assert rig.link.approval("c1")["decision"] == "deny" and sink == [] and rig.link.payloads("tool_result") == []


def test_a_sink_that_raises_gives_a_fixed_error_and_records_nothing(tmp_path, monkeypatch):
    logs = capture_runner_logs(monkeypatch)

    def sink(tasks):
        raise RuntimeError(f"screen is gone {MARKER}")

    rig = make_rig(tmp_path, task_sink=sink)
    run(rig.call("c1", "tasks", tasks_in(item("a"))))
    result = rig.link.result("c1")
    assert result["ok"] is False and result["output"] == MSG_TASKS_NOT_SHOWN
    assert local_events(rig, EVENT_TASKS) == [] and MARKER not in logs.text


def test_a_failing_event_write_does_not_fail_the_call(tmp_path, monkeypatch):
    logs = capture_runner_logs(monkeypatch)
    sink: list = []

    real_append = runner_module.append_event

    def broken_append(conn, session_id, event_type, *args, **kwargs):
        if event_type == EVENT_TASKS:                         # the audit record of the approval must still work
            raise OSError(f"disk full {MARKER}")
        return real_append(conn, session_id, event_type, *args, **kwargs)

    monkeypatch.setattr(runner_module, "append_event", broken_append)
    rig = make_rig(tmp_path, task_sink=sink.append)
    run(rig.call("c1", "tasks", tasks_in(item("a"))))
    assert rig.link.result("c1")["ok"] is True and len(sink) == 1 and MARKER not in logs.text


def test_two_updates_in_one_reply_apply_in_arrival_order(tmp_path):
    sink: list = []
    rig = make_rig(tmp_path, task_sink=sink.append)

    async def scenario():
        rig.propose("c1", "tasks", tasks_in(item("first")))
        rig.propose("c2", "tasks", tasks_in(item("second")), seq=2)
        await rig.runner.wait_idle()

    run(scenario())
    assert [[t.id for t in snapshot] for snapshot in sink] == [["first"], ["second"]]


# ---- the two tools have no run_tool executor: the runner is the only way to run them -------------------------------

def test_the_executor_table_does_not_know_them():
    from m2c import tools

    assert "ask_user" not in tools.TOOLS and "tasks" not in tools.TOOLS
