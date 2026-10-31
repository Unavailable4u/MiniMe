"""
m2c/tests/headless/test_headless_process.py -- Patch C10: Ctrl-C and SIGTERM, and the command line itself.

The signal tests run the REAL program as a child process (a signal cannot be tested honestly in-process) against the
C04 fake server in this process's thread, and watch what the server receives: a ``cancel`` for the run, then a normal
close.  The child must exit non-zero (130 for SIGINT, 143 for SIGTERM, the shell conventions) without a traceback, and in
json mode it must still leave exactly one JSON object on stdout.

The second half is the surface in cli.py: ``-p/--print``, ``--output-format``, ``--mode``, ``m2c --help`` documenting the
exit codes, and every existing command still working next to them.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from headless_rig import *  # noqa: F401,F403

from m2c import cli, headless

TASK = "think for a long time"
SIGNALS = [(signal.SIGTERM, 143, "SIGTERM"), (signal.SIGINT, 130, "SIGINT")]


async def _slow_reply(conn, stream, _start, _user):
    """The model starts answering and then takes for ever: the run is mid-reply when the signal arrives."""
    await stream.text("thinking...")
    await asyncio.sleep(120)


def _start_child(srv, tmp_path, project, *args):
    env_vars = child_env(tmp_path)
    sign_in_for(env_vars, srv.url)
    return subprocess.Popen([sys.executable, "-c", LAUNCHER, *args], env=env_vars, cwd=project,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_until(predicate, timeout=20.0, what="condition"):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def _signalled_run(tmp_path, project, signum, *extra):
    started = []

    async def script(conn):
        await do_handshake(conn)
        run_start = await expect(conn, "run.start")
        stream = Stream(conn, run_start["run_id"])
        await stream.state("created")
        await expect(conn, "user_message")
        await stream.state("running")
        started.append(True)
        task = asyncio.ensure_future(_slow_reply(conn, stream, run_start, None))
        try:
            await idle(conn, stream)                              # answers the cancel with the ``cancelled`` state
        finally:
            task.cancel()

    with ThreadedServer(script) as srv:
        child = _start_child(srv, tmp_path, project, "-p", TASK, *extra)
        try:
            _wait_until(lambda: started, what="the run to start")
            time.sleep(0.3)                                       # let the first delta reach the child
            child.send_signal(signum)
            stdout, stderr = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)
    return srv, child.returncode, stdout, stderr


@pytest.mark.parametrize("signum,expected,name", SIGNALS)
def test_a_signal_sends_cancel_cleans_up_and_exits_non_zero_without_a_traceback(tmp_path, project, signum, expected, name):
    srv, code, stdout, stderr = _signalled_run(tmp_path, project, signum)
    assert code == expected, (code, stderr)
    assert "Traceback" not in stderr and "KeyboardInterrupt" not in stderr
    types = srv.types()
    assert types.count("cancel") == 1 and types[-1] == "cancel", types
    (cancel,) = srv.messages("cancel")
    assert cancel["run_id"] == srv.messages("run.start")[0]["run_id"]
    assert "cancel" in stderr.lower()                              # one line saying so
    assert stdout == "thinking...\n"                               # what had streamed stays, and the line is ended
    srv.assert_clean()


@pytest.mark.parametrize("signum,expected,name", SIGNALS)
def test_a_signal_in_json_mode_still_leaves_exactly_one_json_object(tmp_path, project, signum, expected, name):
    srv, code, stdout, stderr = _signalled_run(tmp_path, project, signum, "--output-format", "json")
    assert code == expected
    assert stdout.count("\n") == 1
    doc = json.loads(stdout)
    assert doc["ok"] is False and doc["exit_code"] == expected
    assert doc["error"]["code"] == "cancelled" and doc["result"] == "thinking..."
    assert doc["run_id"] == srv.messages("run.start")[0]["run_id"]


def test_the_server_is_told_the_connection_ended_normally_after_a_signal(tmp_path, project):
    srv, code, *_ = _signalled_run(tmp_path, project, signal.SIGTERM)
    assert code == 143
    assert srv.fake.connections[0].close_code in (1000, 1005, None)   # a close frame, not a dropped socket


def test_a_second_signal_while_cleaning_up_does_not_make_it_worse(tmp_path, project):
    """A user who hammers Ctrl-C gets the same clean exit, not a traceback from the clean-up."""
    started = []

    async def script(conn):
        await do_handshake(conn)
        run_start = await expect(conn, "run.start")
        stream = Stream(conn, run_start["run_id"])
        await stream.state("created")
        await expect(conn, "user_message")
        await stream.state("running")
        started.append(True)
        while True:                                                # never answers the cancel: the clean-up has to time out
            await conn.recv(timeout=None)

    with ThreadedServer(script) as srv:
        child = _start_child(srv, tmp_path, project, "-p", TASK)
        try:
            _wait_until(lambda: started, what="the run to start")
            time.sleep(0.3)
            child.send_signal(signal.SIGTERM)
            time.sleep(0.2)
            child.send_signal(signal.SIGTERM)
            child.send_signal(signal.SIGINT)
            _stdout, stderr = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)
    assert child.returncode in (130, 143)
    assert "Traceback" not in stderr


def test_signal_handlers_are_put_back_after_a_run(project):
    """run_headless is a function other code calls: it must not leave this process's signal handling changed."""
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    with ThreadedServer(replying("ok")) as srv:
        out = call_headless(srv, "hello")
    assert out.code == 0
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def test_a_run_from_a_thread_that_cannot_install_signal_handlers_still_works(project):
    import threading

    result = {}

    def work():
        with ThreadedServer(replying("from a thread")) as srv:
            result["out"] = call_headless(srv, "hello")

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(60)
    assert result["out"].code == 0 and result["out"].out == "from a thread\n"


# ---- the command line --------------------------------------------------------------------------------------------

def test_help_documents_the_headless_options_and_every_exit_code(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for needle in ("-p", "--print", "--output-format", "--mode", "plan", "accept-edits"):
        assert needle in out, needle
    for line in ("0  success", "1  general failure", "2  usage error", "3  policy denial", "4  quota exhausted",
                 "5  not logged in"):
        assert line in out, line
    assert "M2C_HOME" in out and "login" in out                    # what was there before is still there


def test_the_exit_code_text_in_help_is_the_modules_own_documentation():
    """One table, two places: the module docstring and the help text say the same numbers."""
    doc = headless.__doc__
    for line in ("0  success", "1  general failure", "2  usage error", "3  policy denial", "4  quota exhausted",
                 "5  not logged in"):
        assert line in doc, line
    assert headless.EXIT_CODES_HELP in cli._EPILOG


def test_the_long_form_and_the_short_form_of_the_option_are_the_same(capsys, project):
    short = run_cli(capsys, ["-p", "hello"], replying("x"))
    long = run_cli(capsys, ["--print", "hello"], replying("x"))
    assert (short.code, short.out) == (long.code, long.out) == (0, "x\n")


def test_options_can_come_before_or_after_the_task(capsys, project):
    out = run_cli(capsys, ["--output-format", "json", "--mode", "plan", "-p", "hello"], replying("x"))
    assert out.code == 0 and out.json()["result"] == "x"
    assert out.server.messages("run.start")[0]["payload"]["mode"] == "plan"


@pytest.mark.parametrize("args,needle", [(["--version"], "m2c "), (["doctor", "--help"], "project")])
def test_existing_commands_still_parse(capsys, args, needle):
    assert cli.main(args) == 0
    assert needle in capsys.readouterr().out


def test_login_logout_and_doctor_still_work_with_the_new_options_around(capsys, project, monkeypatch):
    assert cli.main(["logout"]) == 0                               # nothing stored: still the old message
    assert "Not signed in" in capsys.readouterr().out
    assert cli.main(["login", "--device-name", "bad\x00name"]) == cli.EXIT_USAGE
    capsys.readouterr()
    assert cli.main(["doctor", "--project", str(project / "nope")]) == cli.EXIT_USAGE


def test_the_bare_command_still_opens_the_prompt_not_a_headless_run(monkeypatch, project):
    from m2c import repl
    from m2c.auth import credentials

    seen = {}

    async def fake_run_prompt(store, *, project_path=None, **kwargs):
        seen["called"] = True
        return 0

    monkeypatch.setattr(credentials, "load_credential", lambda *a, **k: object())
    monkeypatch.setattr(repl, "run_prompt", fake_run_prompt)
    monkeypatch.setattr(headless, "run_headless", lambda *a, **k: (_ for _ in ()).throw(AssertionError("headless ran")))
    assert cli.main([]) == cli.EXIT_OK and seen == {"called": True}


def test_cli_hands_the_parsed_options_to_headless(monkeypatch, project, capsys):
    seen = {}

    def fake(options, **kwargs):
        seen["options"] = options
        return 0

    monkeypatch.setattr(headless, "run_headless", fake)
    assert cli.main(["-p", "the task", "--output-format", "json", "--mode", "accept-edits"]) == 0
    assert (seen["options"].task, seen["options"].output_format, seen["options"].mode) == ("the task", "json", "accept-edits")


def test_headless_needs_the_websocket_package_only_when_it_runs():
    """``m2c --version`` and ``m2c doctor`` must not pay for importing the link: headless.py imports it lazily."""
    code = ("import sys; import m2c.headless; "
            "print('m2c.link' in sys.modules or 'websockets' in sys.modules)")
    proc = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(PKG_DIR.parent)},
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_headless_does_not_import_the_policy_or_tools_packages():
    """C06 and C05 are built in parallel; the only join is the injected hooks (I02 wires them)."""
    import ast

    tree = ast.parse((PKG_DIR / "headless.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            imported.update(("." * node.level) + (node.module or "") + "." + a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not [name for name in imported if any(part in ("policy", "tools") for part in name.replace("m2c.", "").split("."))]
