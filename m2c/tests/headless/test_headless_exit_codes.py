"""
m2c/tests/headless/test_headless_exit_codes.py -- Patch C10: the exit codes of ``m2c -p``.

    0 success   1 general failure   2 usage error   3 policy denial   4 quota exhausted   5 not logged in / revoked

One test per code (several for the codes that have several causes), the table that maps every protocol error
code onto them, and the json object each failure leaves on stdout.  Tool denial has its own file
(test_headless_tools.py); signals have theirs (test_headless_process.py).
"""
from __future__ import annotations

import io
import json
import socket

import pytest
from headless_rig import *  # noqa: F401,F403

import m2c
from m2c import headless
from m2c.auth.credentials import credentials_path
from m2c.store import DB_FILENAME

TASK = "do the thing"
PROTOCOL_DIR = PKG_DIR / "protocol" / "v1"


def _json_error(out: Outcome) -> dict:
    doc = out.json()
    assert doc["ok"] is False and doc["exit_code"] == out.code
    assert set(doc["error"]) == {"code", "message"} and doc["error"]["message"]
    return doc


# ---- the table ------------------------------------------------------------------------------------------

def test_the_codes_are_the_documented_numbers():
    assert (headless.EXIT_OK, headless.EXIT_FAILURE, headless.EXIT_USAGE, headless.EXIT_POLICY_DENIED,
            headless.EXIT_QUOTA_EXHAUSTED, headless.EXIT_NOT_LOGGED_IN) == (0, 1, 2, 3, 4, 5)
    assert (headless.EXIT_SIGINT, headless.EXIT_SIGTERM) == (130, 143)


def test_every_protocol_error_code_has_a_deliberate_exit_status():
    """Read from the client's copy of protocol/v1/common.json: a code added to the protocol fails this test until
    someone decides what a headless run does with it (an unknown code is treated as ``internal``: 1)."""
    codes = json.loads((PROTOCOL_DIR / "common.json").read_text(encoding="utf-8"))["$defs"]["error_code"]["enum"]
    expected = {"quota_exhausted": 4, "unauthorized": 5, "device_revoked": 5}
    for code in codes:
        assert headless.exit_code_for_error(code) == expected.get(code, 1), code
    assert set(expected) <= set(codes)
    assert headless.exit_code_for_error("a_code_from_a_newer_server") == 1


def test_the_modes_are_the_ones_of_the_run_start_schema():
    """C06's modes.py will become the long-term source; until then the protocol decides what a run may start in."""
    common = json.loads((PROTOCOL_DIR / "common.json").read_text(encoding="utf-8"))
    assert headless.MODES == tuple(common["$defs"]["mode"]["enum"]) == ("plan", "ask", "accept-edits")
    assert headless.DEFAULT_MODE == "ask"


# ---- 0 -------------------------------------------------------------------------------------------------

def test_exit_0_on_success(capsys, project):
    assert run_cli(capsys, ["-p", TASK], replying("fine")).code == 0


# ---- 1: general failure ---------------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["provider_unavailable", "rate_limited", "internal", "invalid_message", "run_not_found"])
def test_exit_1_when_the_server_answers_the_message_with_an_error(capsys, project, code):
    async def tail(_conn, stream, _start, user):
        await stream.error(code, ref_id=user["id"])

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.code == 1
    assert _json_error(out)["error"]["code"] == code
    if code != "run_not_found":                                    # (a run the server no longer knows needs no cancel)
        assert out.server.messages("cancel")                       # the run is ended on the way out


def test_exit_1_when_the_run_is_parked_after_a_model_failure(capsys, project):
    async def tail(_conn, stream, _start, _user):
        await stream.state("paused_error", "provider_unavailable")

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.code == 1 and _json_error(out)["error"]["code"] == "provider_unavailable"


@pytest.mark.parametrize("state,code", [("failed", "run_failed"), ("expired", "run_expired"), ("cancelled", "run_cancelled")])
def test_exit_1_when_the_run_ends_badly_on_the_server(capsys, project, state, code):
    async def tail(_conn, stream, _start, _user):
        await stream.text("part")
        await stream.state(state)

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.code == 1
    doc = _json_error(out)
    assert doc["error"]["code"] == code
    assert doc["result"] == "part"                                 # what arrived before the failure is kept


def test_exit_1_when_the_server_cannot_be_reached(project):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                                                  # nothing listens here now
    out = call_headless(None, TASK, output_format="json", url=f"http://127.0.0.1:{port}", connect_timeout_s=0.5)
    assert out.code == 1
    assert _json_error(out)["error"]["code"] == "unreachable"
    assert "Traceback" not in out.err


def test_exit_1_when_the_connection_is_lost_and_does_not_come_back(project):
    """A headless job must never hang for ever on a dead server: after ``reconnect_give_up_s`` it fails."""
    async def tail(conn, stream, _start, _user):
        await stream.text("partial")
        await conn.kill()

    with ThreadedServer(scripted(tail), silent_script(), repeat_last=True) as srv:
        out = call_headless(srv, TASK, output_format="json", reconnect_give_up_s=0.4)
    assert out.code == 1
    doc = _json_error(out)
    assert doc["error"]["code"] == "unreachable" and doc["result"] == "partial"


def test_exit_1_when_stdout_goes_away(project):
    class Closed(Capture):
        def write(self, _text):
            raise BrokenPipeError()

    with ThreadedServer(replying("hello")) as srv:
        out = call_headless(srv, TASK, stdout=Closed())
    assert out.code == 1
    assert "stdout" in out.err and "Traceback" not in out.err
    assert srv.messages("cancel")                                  # nobody is reading: do not keep spending credits


# ---- 2: usage errors ------------------------------------------------------------------------------------

def test_exit_2_for_an_unknown_mode_before_anything_else_happens(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--mode", "auto"], replying("never"))
    assert out.code == 2
    assert "--mode" in out.err and "plan, ask, accept-edits" in out.err
    assert out.out == ""
    assert out.server.fake.connections == []                       # no network, ...
    assert not (project.parent / "m2c-home" / DB_FILENAME).exists()          # ... no session created


def test_a_usage_error_in_json_mode_still_leaves_the_one_json_object(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--mode", "bypass", "--output-format", "json"], replying("never"))
    assert out.code == 2
    doc = _json_error(out)
    assert doc["error"]["code"] == "usage"
    assert (doc["result"], doc["session_id"], doc["run_id"], doc["usage"]) == ("", None, None, None)


@pytest.mark.parametrize("args", [
    ["-p"],                                                        # -p needs its task (argparse)
    ["-p", TASK, "--output-format", "yaml"],                       # not text or json (argparse)
    ["--mode", "plan"],                                            # headless options without -p
    ["--output-format", "json"],
    ["-p", TASK, "doctor"],                                        # -p is not a command's option
    ["-p", TASK, "login"],
])
def test_exit_2_for_the_wrong_combination_of_options(capsys, project, args):
    out = run_cli(capsys, args, replying("never"))
    assert out.code == 2
    assert out.err and out.out == ""
    assert out.server.fake.connections == []


@pytest.mark.parametrize("task", ["", "   \n"])
def test_exit_2_for_an_empty_task(capsys, project, task):
    out = run_cli(capsys, ["-p", task], replying("never"))
    assert out.code == 2 and "empty" in out.err
    assert out.server.fake.connections == []


def test_exit_2_when_stdin_is_empty(capsys, project, monkeypatch):
    stdin = io.StringIO("")
    stdin.isatty = lambda: False
    monkeypatch.setattr("sys.stdin", stdin)
    out = run_cli(capsys, ["-p", "-"], replying("never"))
    assert out.code == 2 and "empty" in out.err


def test_exit_2_when_stdin_is_a_terminal_because_a_headless_run_never_waits_for_typing(capsys, project, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

        def read(self, *a, **k):
            raise AssertionError("a terminal was read")

    monkeypatch.setattr("sys.stdin", Terminal())
    out = run_cli(capsys, ["-p", "-", "--output-format", "json"], replying("never"))
    assert out.code == 2
    assert _json_error(out)["error"]["code"] == "usage" and "terminal" in out.json()["error"]["message"]


def test_exit_2_when_stdin_is_not_text(capsys, project, monkeypatch):
    class Binary(io.StringIO):
        def isatty(self):
            return False

        def read(self, *a, **k):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("sys.stdin", Binary())
    out = run_cli(capsys, ["-p", "-"], replying("never"))
    assert out.code == 2 and "Traceback" not in out.err


def test_exit_2_for_a_task_longer_than_the_protocol_allows(capsys, project):
    out = run_cli(capsys, ["-p", "x" * (headless.MAX_TASK_CHARS + 1)], replying("never"))
    assert out.code == 2 and "too long" in out.err
    assert out.server.fake.connections == []


def test_exit_2_when_the_server_limit_is_smaller_than_the_task(project):
    """The server announces ``max_message_bytes`` in its welcome; a task above it can never be sent."""
    async def script(conn):
        await do_handshake(conn, welcome_env(max_message_bytes=1024))
        start = await expect(conn, "run.start")
        stream = Stream(conn, start["run_id"])
        await stream.state("created")
        await idle(conn, stream)

    with ThreadedServer(script) as srv:
        out = call_headless(srv, "y" * 5000, output_format="json")
    assert out.code == 2 and _json_error(out)["error"]["code"] == "usage"


# ---- 4: quota -------------------------------------------------------------------------------------------

def test_exit_4_when_the_credit_pool_is_used_up(capsys, project):
    async def tail(_conn, stream, _start, user):
        await stream.error("quota_exhausted", ref_id=user["id"], details={"reset_at": "2026-12-01T00:00:00Z"})
        await stream.state("paused_budget", "quota_exhausted")

    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], scripted(tail))
    assert out.code == 4
    doc = _json_error(out)
    assert doc["error"]["code"] == "quota_exhausted" and "2026-12-01T00:00:00Z" in doc["error"]["message"]
    assert out.server.messages("cancel")                           # a parked run takes no more messages: it is ended


def test_exit_4_also_when_the_run_is_only_parked_for_budget(capsys, project):
    async def tail(_conn, stream, _start, _user):
        await stream.text("some of the answer")
        await stream.state("paused_budget", "quota_exhausted")

    out = run_cli(capsys, ["-p", TASK], scripted(tail))
    assert out.code == 4
    assert out.out == "some of the answer\n"                       # what was streamed stays on stdout
    assert "credit" in out.err


# ---- 5: not signed in / revoked --------------------------------------------------------------------------

def test_exit_5_when_not_signed_in_and_the_network_is_never_touched(capsys, project):
    out = run_cli(capsys, ["-p", TASK, "--output-format", "json"], replying("never"), signed_in=False)
    assert out.code == 5
    assert _json_error(out)["error"]["code"] == "not_logged_in"
    assert "m2c login" in out.json()["error"]["message"]
    assert out.server.fake.connections == []
    assert not (project.parent / "m2c-home" / DB_FILENAME).exists()


def test_exit_5_when_the_credentials_file_is_readable_by_others(capsys, project):
    with ThreadedServer(replying("never")) as srv:
        sign_in(srv.url)
        credentials_path().chmod(0o644)
        code = cli.main(["-p", TASK])
    captured = capsys.readouterr()
    assert code == 5 and "Traceback" not in captured.err
    assert srv.fake.connections == []                               # the credential was never sent anywhere


@pytest.mark.parametrize("protocol_code,close_code,json_code", [("device_revoked", 4403, "device_revoked"),
                                                                ("unauthorized", 4401, "unauthorized")])
def test_exit_5_when_the_server_refuses_the_device(project, protocol_code, close_code, json_code):
    with ThreadedServer(reject_script(protocol_code, close_code)) as srv:
        out = call_headless(srv, TASK, output_format="json")
    assert out.code == 5
    assert _json_error(out)["error"]["code"] == json_code
    assert "m2c" in out.json()["error"]["message"] and "login" in out.json()["error"]["message"]


def test_exit_5_when_the_device_is_revoked_in_the_middle_of_a_run(project):
    async def tail(conn, stream, _start, _user):
        await stream.text("working")
        await stream.error("device_revoked")
        await conn.close(4403)

    with ThreadedServer(scripted(tail)) as srv:
        out = call_headless(srv, TASK, output_format="json")
    assert out.code == 5 and _json_error(out)["error"]["code"] == "device_revoked"


# ---- no traceback, ever ------------------------------------------------------------------------------------

def test_an_unexpected_bug_is_one_line_with_the_exception_type_and_exit_1(project, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("leaky detail that must not be printed")

    monkeypatch.setattr("m2c.store.open_db", boom)
    out = call_headless(None, TASK, output_format="json", url="http://127.0.0.1:1")
    assert out.code == 1
    assert _json_error(out)["error"]["code"] == "internal_error"
    assert "RuntimeError" in out.json()["error"]["message"]
    assert "leaky detail" not in out.out + out.err and "Traceback" not in out.err
