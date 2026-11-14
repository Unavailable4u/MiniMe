"""
m2c/tests/headless/test_headless_wiring_process.py -- Patch C10b: Ctrl-C and SIGTERM while a command is running.

The real ``m2c -p`` in a child process, the fake server in a thread, and a real ``sleep`` as the long-running command.
When the signal arrives the child must: kill the command (not leave it running), tell the server ``cancel``, exit
non-zero (143 for SIGTERM, 130 for SIGINT, the shell conventions) without a traceback, and never send a tool_result for
the command it killed.  ``--sandbox off`` keeps the child independent of whether bubblewrap is installed here.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiring_rig import *  # noqa: E402,F401,F403
from wiring_rig import TASK  # noqa: E402

SLEEP = "sleep 47113"                      # a number nothing else on the machine has in its command line
SIGNALS = [(signal.SIGTERM, 143), (signal.SIGINT, 130)]


def _running(marker: str) -> bool:
    """True while some process (other than the m2c child, whose own arguments name the command) has ``marker`` in its
    command line (a /proc scan: no pgrep needed)."""
    needle = marker.encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                text = handle.read().replace(b"\x00", b" ")
            if needle in text and b"--allow" not in text:   # the m2c child itself has the marker in its --allow argument
                return True
        except OSError:
            continue
    return False


def _wait_until(predicate, timeout=20.0, what="condition"):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _signalled_run(tmp_path, project, signum, *extra):
    async def tail(conn, stream, _start, _user):
        await stream.text("starting")
        await stream.done("tool_use")
        await stream.usage()
        await stream.proposed("b1", "bash", {"command": SLEEP}, "R2")

    with ThreadedServer(scripted(tail)) as srv:
        env_vars = child_env(tmp_path)
        sign_in_for(env_vars, srv.url)
        child = subprocess.Popen(
            [sys.executable, "-c", LAUNCHER, "-p", TASK, "--mode", "ask", "--sandbox", "off",
             "--allow", f"bash({SLEEP})", *extra],
            env=env_vars, cwd=project, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _wait_until(lambda: _running(SLEEP), what="the command to start")
            child.send_signal(signum)
            stdout, stderr = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(10)
    return srv, child.returncode, stdout, stderr


@pytest.mark.parametrize("signum,expected", SIGNALS)
def test_a_signal_kills_the_running_command_cancels_the_run_and_exits_cleanly(tmp_path, project, signum, expected):
    srv, code, stdout, stderr = _signalled_run(tmp_path, project, signum)
    try:
        assert code == expected, (code, stderr)
        assert "Traceback" not in stderr and "KeyboardInterrupt" not in stderr
        assert not _running(SLEEP), "the command was left running"
        types = srv.types()
        assert types.count("cancel") == 1 and "tool_result" not in types, types
        assert "cancel" in stderr.lower()
        srv.assert_clean()
    finally:
        # never leave the marker process behind even if an assertion failed
        if _running(SLEEP):
            subprocess.run(["pkill", "-f", SLEEP], check=False)


@pytest.mark.parametrize("signum,expected", SIGNALS)
def test_a_signal_in_json_mode_leaves_one_object_that_counts_the_killed_call(tmp_path, project, signum, expected):
    srv, code, stdout, stderr = _signalled_run(tmp_path, project, signum, "--output-format", "json")
    assert code == expected, (code, stderr)
    assert stdout.count("\n") == 1
    doc = json.loads(stdout)
    assert doc["ok"] is False and doc["exit_code"] == expected and doc["error"]["code"] == "cancelled"
    assert doc["tool_calls"] == {"ok": 0, "failed": 0, "denied": 0, "cancelled": 1}
    assert doc["files_changed"] == []
    assert not _running(SLEEP)
