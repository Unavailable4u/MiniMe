"""
m2c/tests/auth/test_auth_redaction.py -- Patch C03: the device code, the
device credential and the JWT never appear in logs, tracebacks, exception
messages, reprs, stdout or stderr.

This is the "Done when" item that matters most, so it is attacked from
several sides:

* every outcome of a login (success and each failure) is run with the raw log
  records captured, and everything is searched for the three secrets;
* errors are *forced* in the places that hold secrets -- including a server
  that echoes the secrets back in its error bodies, and approvals whose
  secret is malformed in a way that keeps the secret text inside the bad
  value -- and the exceptions are rendered every way an error tracker or
  ``pytest --showlocals`` would (str, repr, vars, traceback, traceback WITH
  local variables);
* the real program is run in a subprocess and its stderr is searched;
* the source is scanned so a future edit cannot quietly add a logging call.
"""
from __future__ import annotations

from auth_fixtures import *  # noqa: F401,F403  (fixtures; see auth_fixtures.py)

import ast
import copy
import io
import json
import logging
import os
import pickle
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

import m2c
from m2c import cli
from m2c.auth import credentials as creds
from m2c.auth import login as login_mod
from m2c.auth.credentials import AuthError, Credential, Secret
from mock_server import (
    CREDENTIAL,
    DEVICE_CODE,
    DEVICE_ID,
    DROP,
    JWT,
    POLL_PATH,
    SECRETS,
    START_PATH,
    TOKEN_PATH,
    Reply,
    approved,
    assert_no_secrets,
    denied,
    error,
    exception_texts,
    expired,
    log_texts,
    ok,
    pending,
    start_reply,
    token_reply,
)
from test_auth_login import run_login

AUTH_SRC = Path(m2c.__file__).resolve().parent / "auth"


def attempt_login(server, fake_time, out, err):
    """Run a login; return the AuthError it raised (or None if it succeeded)."""
    try:
        run_login(server.url, fake_time, out=out, err=err)
    except AuthError as exc:
        return exc
    return None


# --------------------------------------------------------------------------
# Every login outcome, with logs, stdout, stderr and the exception searched
# --------------------------------------------------------------------------
ECHO = {"detail": f"echoed {DEVICE_CODE} {CREDENTIAL} {JWT}"}

SCENARIOS = {
    "approved": [pending(), approved()],
    "denied": [pending(), denied()],
    "expired by the server": [expired()],
    "dropped connections then success": [DROP, DROP, approved()],
    "server error echoing the secrets": [error(500, ECHO), approved()],
    "client error echoing the secrets": [error(403, ECHO)],
    "400 echoing the secrets": [error(400, ECHO)],
    "429 echoing the secrets": [error(429, ECHO, **{"Retry-After": "3"}), approved()],
    "html page echoing the secrets": [Reply(200, f"<html>{DEVICE_CODE} {CREDENTIAL}</html>"), approved()],
    "unknown status word": [ok({"status": f"mystery-{DEVICE_CODE}"})],
    # The bad secret CONTAINS the real one, so any path that echoes the bad
    # value or leaves it in a local variable shows up.
    "approval with a malformed credential": [approved(device_credential=CREDENTIAL + " not valid")],
    "approval with a malformed device id": [approved(device_id=CREDENTIAL)],
    "approval with no device id": [approved(device_id=None)],
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_secret_reaches_any_output_in_any_login_outcome(server, fake_time, m2c_logs, name):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, *SCENARIOS[name])
    out, err = io.StringIO(), io.StringIO()
    exc = attempt_login(server, fake_time, out, err)

    texts = [out.getvalue(), err.getvalue(), *log_texts(m2c_logs)]
    if exc is not None:
        texts += exception_texts(exc)
    # The device code and the JWT are never shown to the person at all.
    assert_no_secrets(*texts, secrets=(DEVICE_CODE, JWT))
    # The credential is stored, never displayed.
    assert_no_secrets(*texts, secrets=(CREDENTIAL,))


# Scenarios in which the login ends happily (so there is no exception to inspect).
_RECOVERING = {
    "approved",
    "dropped connections then success",
    "server error echoing the secrets",
    "429 echoing the secrets",
    "html page echoing the secrets",
}


@pytest.mark.parametrize("name", [n for n in SCENARIOS if n not in _RECOVERING])
def test_a_failed_login_leaves_the_secrets_out_of_the_exception_chain(server, fake_time, name):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, *SCENARIOS[name])
    exc = attempt_login(server, fake_time, io.StringIO(), io.StringIO())
    assert exc is not None, "this scenario was expected to fail"
    # Nothing hides in __cause__/__context__, where a tracker would look next.
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert_no_secrets(*exception_texts(exc))


def test_the_server_start_reply_is_not_left_in_a_traceback(server, fake_time):
    """Start replies hold the device code; a rejected one must not echo it."""
    server.script(START_PATH, start_reply(user_code="\x1b[2JBCDF"))
    exc = attempt_login(server, fake_time, io.StringIO(), io.StringIO())
    assert exc is not None
    assert_no_secrets(*exception_texts(exc), secrets=(DEVICE_CODE,))


def test_network_failures_whose_message_contains_a_secret_are_reduced_to_a_type(
    server, fake_time, m2c_logs, monkeypatch
):
    """Simulate a library that echoes what it was sending."""
    import urllib.request

    def leaky(self, *args, **kwargs):
        raise urllib.error.URLError(f"connection to host failed while sending {DEVICE_CODE} {CREDENTIAL}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", leaky)
    out, err = io.StringIO(), io.StringIO()
    exc = attempt_login(server, fake_time, out, err)
    assert exc is not None
    assert_no_secrets(out.getvalue(), err.getvalue(), *log_texts(m2c_logs), *exception_texts(exc))


@pytest.mark.parametrize("raised", [OSError, ValueError, TimeoutError, ConnectionResetError])
def test_other_failure_types_from_the_transport_are_reduced_too(server, fake_time, m2c_logs, monkeypatch, raised):
    import urllib.request

    def leaky(self, *args, **kwargs):
        raise raised(f"{DEVICE_CODE} {CREDENTIAL}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", leaky)
    exc = attempt_login(server, fake_time, io.StringIO(), io.StringIO())
    assert exc is not None
    assert_no_secrets(*log_texts(m2c_logs), *exception_texts(exc))


def test_a_save_failure_after_approval_does_not_leak_the_credential(server, fake_time, m2c_logs, monkeypatch):
    def boom(path, data):
        raise OSError(f"cannot write {CREDENTIAL}")

    monkeypatch.setattr(creds, "_write_file_atomically", boom)
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    exc = attempt_login(server, fake_time, io.StringIO(), io.StringIO())
    assert exc is not None
    assert_no_secrets(*log_texts(m2c_logs), *exception_texts(exc))


def test_a_keyring_error_that_contains_the_credential_is_not_surfaced(server, fake_time, m2c_logs, fake_keyring):
    fake_keyring.fail_set = RuntimeError(f"locked: {CREDENTIAL}")
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    out, err = io.StringIO(), io.StringIO()
    run_login(server.url, fake_time, out=out, err=err)  # falls back to the file
    assert_no_secrets(out.getvalue(), err.getvalue(), *log_texts(m2c_logs))


# --------------------------------------------------------------------------
# The token helper
# --------------------------------------------------------------------------
TOKEN_FAILURES = {
    "revoked, echoing": error(401, ECHO),
    "server error, echoing": error(500, ECHO),
    "malformed jwt that contains the real one": ok({"jwt": JWT + " not valid", "expires_in": 900}),
    "malformed lifetime": ok({"jwt": JWT, "expires_in": "900"}),
    "html page": Reply(200, f"<html>{JWT}</html>"),
    "dropped connection": DROP,
}


@pytest.mark.parametrize("name", TOKEN_FAILURES)
def test_token_failures_leak_nothing(server, fake_time, m2c_logs, name):
    creds.save_credential(Credential(Secret(CREDENTIAL), DEVICE_ID, server.url))
    server.script(TOKEN_PATH, TOKEN_FAILURES[name])
    with pytest.raises(AuthError) as info:
        login_mod.get_access_token(clock=fake_time.clock)
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert_no_secrets(*log_texts(m2c_logs), *exception_texts(info.value))


def test_a_successful_token_fetch_logs_nothing_secret(server, fake_time, m2c_logs):
    creds.save_credential(Credential(Secret(CREDENTIAL), DEVICE_ID, server.url))
    server.script(TOKEN_PATH, token_reply())
    token = login_mod.get_access_token(clock=fake_time.clock)
    assert token.reveal() == JWT
    assert_no_secrets(*log_texts(m2c_logs), repr(token), str(token))


def test_the_jwt_comes_back_wrapped_so_a_stray_print_cannot_leak_it(server, fake_time):
    creds.save_credential(Credential(Secret(CREDENTIAL), DEVICE_ID, server.url))
    server.script(TOKEN_PATH, token_reply())
    token = login_mod.get_access_token(clock=fake_time.clock)
    assert JWT not in f"{token} {token!r} {[token]} {{'t': token}}"


# --------------------------------------------------------------------------
# Secret and Credential cannot be serialised by accident
# --------------------------------------------------------------------------
def test_secret_cannot_be_pickled_or_json_encoded():
    s = Secret(CREDENTIAL)
    with pytest.raises(TypeError) as info:
        pickle.dumps(s)
    assert CREDENTIAL not in str(info.value)
    with pytest.raises(TypeError) as info:
        json.dumps({"x": s})
    assert CREDENTIAL not in str(info.value)


def test_secret_has_no_instance_dict_to_dump():
    with pytest.raises((TypeError, AttributeError)):
        vars(Secret(CREDENTIAL))


def test_secret_can_still_be_copied_because_it_is_immutable():
    s = Secret(CREDENTIAL)
    assert copy.copy(s).reveal() == CREDENTIAL
    assert copy.deepcopy(s).reveal() == CREDENTIAL


def test_a_secret_in_a_log_call_is_redacted_by_the_logging_setup_and_by_itself():
    stream = io.StringIO()
    from m2c.logging_setup import build_handler

    logger = logging.getLogger("m2c.test_secret")
    handler = build_handler(stream)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("credential is %s", Secret(CREDENTIAL))
    finally:
        logger.removeHandler(handler)
    assert CREDENTIAL not in stream.getvalue()
    # ...and even a handler WITHOUT the content-free filter prints nothing sensitive
    assert CREDENTIAL not in ("%s" % Secret(CREDENTIAL))


# --------------------------------------------------------------------------
# The real program
# --------------------------------------------------------------------------
def _run_program(args, tmp_path, server_url):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "M2C_HOME": str(tmp_path / "proc-home"),
        "M2C_SERVER_URL": server_url,
        "M2C_LOG_LEVEL": "DEBUG",
        "NO_PROXY": "*",
        "PYTHONPATH": str(Path(m2c.__file__).resolve().parent.parent),
    }
    return subprocess.run(
        [sys.executable, "-m", "m2c.cli", *args], env=env, capture_output=True, text=True, timeout=60
    )


def test_process_login_denied_prints_no_secret_and_no_traceback(server, tmp_path):
    server.script(START_PATH, start_reply(interval=1))
    server.script(POLL_PATH, denied())
    proc = _run_program(["login"], tmp_path, server.url)
    assert proc.returncode == cli.EXIT_AUTH
    assert "denied" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert_no_secrets(proc.stdout, proc.stderr)


def test_process_login_success_stores_the_credential_and_prints_no_secret(server, tmp_path):
    server.script(START_PATH, start_reply(interval=1))
    server.script(POLL_PATH, approved())
    proc = _run_program(["login", "--device-name", "ci-box"], tmp_path, server.url)
    assert proc.returncode == 0, proc.stderr
    assert_no_secrets(proc.stdout, proc.stderr)
    stored = json.loads((tmp_path / "proc-home" / "credentials.json").read_text())
    assert stored["device_credential"] == CREDENTIAL
    assert oct(os.stat(tmp_path / "proc-home" / "credentials.json").st_mode & 0o777) == "0o600"
    assert server.bodies(START_PATH)[0]["device_name"] == "ci-box"


# --------------------------------------------------------------------------
# The source cannot grow a leak unnoticed
# --------------------------------------------------------------------------
def _auth_modules():
    return sorted(AUTH_SRC.glob("*.py"))


def test_the_auth_package_exists_with_the_planned_modules():
    assert {p.name for p in _auth_modules()} >= {"__init__.py", "login.py", "credentials.py"}


def test_auth_code_logs_only_through_log_event():
    """logging_setup.log_event only prints allowlisted, content-free fields; a
    plain logger.info(f"...") in this package could print anything."""
    offenders = []
    for py in _auth_modules():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"logger", "logging", "log"}
                and node.func.attr != "getLogger"
            ):
                offenders.append(f"{py.name}:{node.lineno} {node.func.value.id}.{node.func.attr}")
    assert offenders == []


def test_auth_code_never_passes_log_event_a_field_outside_the_allowlist():
    from m2c.logging_setup import SAFE_KEYS

    offenders = []
    for py in _auth_modules():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "log_event":
                for kw in node.keywords:
                    if kw.arg is not None and kw.arg not in SAFE_KEYS:
                        offenders.append(f"{py.name}:{node.lineno} {kw.arg}")
    assert offenders == []


def test_auth_code_prints_only_to_an_explicit_stream():
    offenders = []
    for py in _auth_modules():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
                if not any(kw.arg == "file" for kw in node.keywords):
                    offenders.append(f"{py.name}:{node.lineno}")
    assert offenders == []


def test_auth_code_never_interpolates_exceptions_into_messages():
    """``f"... {exc}"`` and ``str(exc)`` are how a library's text (which may
    echo a request) ends up in our messages.  Only the type name is allowed."""
    offenders = []
    for py in _auth_modules():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        handler_names = {
            h.name for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and h.name
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in handler_names and isinstance(node.ctx, ast.Load):
                parent_ok = False
                for outer in ast.walk(tree):
                    # allowed shapes: type(exc).__name__ and exc.<status/code/headers attribute>
                    if isinstance(outer, ast.Call) and getattr(outer.func, "id", None) == "type" and outer.args and outer.args[0] is node:
                        parent_ok = True
                    if isinstance(outer, ast.Attribute) and outer.value is node and outer.attr in {"code", "headers", "close"}:
                        parent_ok = True
                if not parent_ok:
                    offenders.append(f"{py.name}:{node.lineno} uses exception {node.id!r}")
    assert offenders == []
