"""
m2c/tests/headless/test_headless_core.py -- Patch C10: ``m2c -p "task"``, the successful runs.

Against the C04 fake server (tests/link/link_fakes.py), through the real DeviceLink and, for most tests, through
``cli.main`` itself: the text and json output formats, the task from stdin, "never prompts, no TTY needed", the
``reply_restarted`` notice, what lands in the local log, and that no content reaches a log line.
"""
from __future__ import annotations

import builtins
import io
import json
import sys

import pytest
from headless_rig import *  # noqa: F401,F403

from m2c.store import list_sessions, open_db, read_events

TASK = "summarise the repo in one line"


# ---- text mode ----------------------------------------------------------------------------------------

def test_text_mode_streams_the_reply_to_stdout_and_everything_else_to_stderr(capsys, project):
    out = run_cli(capsys, ["-p", TASK], replying("Hel", "lo"))
    assert out.code == 0
    assert out.out == "Hello\n"                                   # the text, and one final newline
    assert "Hello" not in out.err                                  # content is only ever on stdout
    assert "fake-model" in out.err                                 # the usage line is progress: stderr
    out.server.assert_clean()


def test_the_run_is_started_with_profile_code_the_mode_and_a_label_not_a_path(capsys, project):
    out = run_cli(capsys, ["-p", TASK], replying("ok"))
    (start,) = out.server.messages("run.start")
    payload = start["payload"]
    assert payload["profile"] == "code"
    assert payload["mode"] == "ask"                                # the default
    assert payload["cwd_label"] == PROJECT_NAME                    # the folder's NAME ...
    assert "/" not in payload["cwd_label"] and str(project) not in json.dumps(payload)   # ... never a path
    (message,) = out.server.messages("user_message")
    assert message["payload"] == {"text": TASK}
    assert message["run_id"] == start["run_id"]


@pytest.mark.parametrize("mode", ["plan", "ask", "accept-edits"])
def test_the_chosen_mode_goes_into_run_start(capsys, project, mode):
    out = run_cli(capsys, ["-p", TASK, "--mode", mode], replying("ok"))
    assert out.code == 0
    assert out.server.messages("run.start")[0]["payload"]["mode"] == mode


def test_a_reply_that_already_ends_in_a_newline_gets_no_second_one(capsys, project):
    out = run_cli(capsys, ["-p", TASK], replying("line one\n"))
    assert out.out == "line one\n"


def test_the_run_is_cancelled_when_the_turn_is_over_so_the_server_buffer_is_deleted(capsys, project):
    """P00 has no other way to end a run; the I01 server runner leaves it ``running`` after a reply."""
    out = run_cli(capsys, ["-p", TASK], replying("ok"))
    (cancel,) = out.server.messages("cancel")
    assert cancel["run_id"] == out.server.messages("run.start")[0]["run_id"]
    assert out.server.types()[-1] == "cancel"


def test_a_run_the_server_completes_by_itself_is_not_cancelled(capsys, project):
    async def tail(_conn, stream, _start, _user):
        await stream.text("done")
        await stream.done("end_turn")
        await stream.state("completed")                            # no usage event at all

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.code == 0
    assert out.server.messages("cancel") == []
    assert out.json()["usage"] is None                             # "usage numbers if known"


def test_a_cut_off_reply_is_still_a_success_but_says_so_on_stderr(capsys, project):
    out = run_cli(capsys, ["-p", TASK], replying("partial answ", stop_reason="max_tokens"))
    assert out.code == 0 and out.out == "partial answ\n"
    assert "cut off" in out.err


def test_notices_and_budget_warnings_are_progress_on_stderr(capsys, project):
    async def tail(_conn, stream, _start, _user):
        await stream.notice("model_changed", "Using a smaller model for this run.", "warning")
        await stream.budget_warning(80, reset_at="2026-12-01T00:00:00Z")
        await stream.reply("answer")

    out = run_cli(capsys, ["-p", TASK], scripted(tail))
    assert out.out == "answer\n"
    assert "Using a smaller model for this run." in out.err
    assert "80" in out.err


# ---- json mode ----------------------------------------------------------------------------------------

def test_json_mode_prints_exactly_one_object_and_nothing_else_on_stdout(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], replying("Hel", "lo"))
    assert out.code == 0
    doc = out.json()                                               # also asserts: one line, parses
    (start,) = out.server.messages("run.start")
    assert doc == {
        "ok": True,
        "result": "Hello",
        "session_id": start["run_id"],
        "run_id": start["run_id"],
        "usage": {"model": "fake-model", "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5,
                  "credits": 0.5, "calls": 1},
        "exit_code": 0,
        "error": None,
    }
    out.server.assert_clean()


def test_json_stdout_is_ascii_whatever_the_model_wrote(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], replying("café ✓\nsecond line"))
    assert out.out.isascii()
    assert out.json()["result"] == "café ✓\nsecond line"


def test_json_mode_keeps_notices_and_progress_off_stdout(capsys, project):
    async def tail(_conn, stream, _start, _user):
        await stream.notice("model_changed", "Using a smaller model for this run.")
        await stream.budget_warning()
        await stream.reply("answer")

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.json()["result"] == "answer"
    assert "smaller model" not in out.out


def test_usage_numbers_add_up_over_the_model_calls_of_a_run(capsys, project):
    async def tail(_conn, stream, _start, _user):
        await stream.text("a")
        await stream.done("end_turn")
        await stream.usage(input_tokens=100, cached_input_tokens=40, output_tokens=7, credits=0.25, model="m1")

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.json()["usage"] == {"model": "m1", "input_tokens": 100, "cached_input_tokens": 40,
                                   "output_tokens": 7, "credits": 0.25, "calls": 1}


def test_text_and_json_carry_the_same_reply(capsys, project):
    text = run_cli(capsys, ["-p", TASK], replying("same ", "words"))
    doc = run_cli(capsys, ["-p", TASK, "--output-format", "json"], replying("same ", "words")).json()
    assert text.out == doc["result"] + "\n"


# ---- the task: argument, stdin ------------------------------------------------------------------------

def test_the_task_can_come_from_stdin_with_dash(capsys, project, monkeypatch):
    stdin = io.StringIO("fix the failing test\nthen run it\n")
    stdin.isatty = lambda: False                                   # piped
    monkeypatch.setattr("sys.stdin", stdin)
    out = run_cli(capsys, ["-p", "-"], replying("ok"))
    assert out.code == 0
    (message,) = out.server.messages("user_message")
    assert message["payload"]["text"] == "fix the failing test\nthen run it"     # the final newline is trimmed


def test_stdin_is_never_read_when_the_task_is_an_argument(capsys, project, monkeypatch):
    class Trap(io.StringIO):
        def read(self, *a, **k):
            raise AssertionError("stdin was read")

        readline = readlines = __iter__ = read

        def isatty(self):
            raise AssertionError("stdin was probed")

    monkeypatch.setattr("sys.stdin", Trap())
    out = run_cli(capsys, ["-p", TASK], replying("ok"))
    assert out.code == 0


def test_a_headless_run_never_prompts(capsys, project, monkeypatch):
    def trap(*_a, **_k):
        raise AssertionError("input() was called")

    monkeypatch.setattr(builtins, "input", trap)
    assert run_cli(capsys, ["-p", TASK, "--output-format", "json"], replying("ok")).code == 0


def test_a_run_works_with_no_terminal_at_all(project, tmp_path):
    """The real program in a subprocess with stdin closed and both output streams pipes: no TTY anywhere."""
    import subprocess

    with ThreadedServer(replying("no tty needed")) as srv:
        env_vars = child_env(tmp_path)
        sign_in_for(env_vars, srv.url)
        proc = subprocess.run([sys.executable, "-c", LAUNCHER, "-p", TASK], env=env_vars, cwd=project,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "no tty needed\n"
    assert "Traceback" not in proc.stderr


# ---- reply_restarted -----------------------------------------------------------------------------------

async def _restarting(_conn, stream, _start, _user):
    """The connection dropped mid-reply: the server says so, then writes the whole reply again."""
    await stream.text("Hel", "lo wor")                             # the partial reply the device already got
    await stream.notice("reply_restarted", "The reply is being written again.")
    await stream.text("Hello ", "world")
    await stream.done("end_turn")
    await stream.usage()


def test_json_mode_discards_the_partial_text_after_reply_restarted(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(_restarting))
    assert out.code == 0
    assert out.json()["result"] == "Hello world"                   # only the complete reply


def test_text_mode_cannot_take_back_the_partial_text_and_says_so_on_stderr(capsys, project):
    out = run_cli(capsys, ["-p", TASK], scripted(_restarting))
    assert out.code == 0
    assert out.out == "Hello worHello world\n"                     # documented limit: partial, then the full reply
    assert "again" in out.err and "partial" in out.err


def test_the_local_log_holds_the_final_reply_once_after_a_restart(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(_restarting))
    store = open_db()
    (session,) = list_sessions(store)
    events = read_events(store, session.id)
    assistant = [e.data["text"] for e in events if e.type == "assistant.message"]
    user = [e.data["text"] for e in events if e.type == "user.message"]
    assert assistant == ["Hello world"] and user == [TASK]
    assert session.id == out.json()["session_id"] and session.mode == "ask"
    assert session.project_path == str(project)
    store.close()


# ---- content hygiene -----------------------------------------------------------------------------------

def test_neither_the_task_nor_the_reply_reaches_a_log_line(project, m2c_logs):
    task_text, reply_text = "TASK-WORDS-8f3a1c", "REPLY-WORDS-52d9e0"
    with ThreadedServer(replying(reply_text)) as srv:
        out = call_headless(srv, task_text, output_format="json")
    assert out.code == 0 and reply_text in out.out               # the user's own output is on stdout ...
    for text in log_texts(m2c_logs):                               # ... and in no log line, in any form
        assert task_text not in text and reply_text not in text
    assert task_text not in out.err and reply_text not in out.err


def test_the_credential_never_appears_in_any_output(project, m2c_logs):
    cred_text = "m2c-test-cred-" + "x" * 30         # built at run time, the same shape make_credential() uses
    with ThreadedServer(replying("ok")) as srv:
        out = call_headless(srv, TASK)
    assert cred_text not in out.out + out.err
    assert all(cred_text not in text for text in log_texts(m2c_logs))
