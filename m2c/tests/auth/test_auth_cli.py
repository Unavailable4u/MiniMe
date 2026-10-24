"""
m2c/tests/auth/test_auth_cli.py -- Patch C03: ``m2c login`` and ``m2c logout``
as the person at the terminal sees them (exit statuses, what goes to stdout
and stderr, the help text).  The flow itself is tested in test_auth_login.py.
"""
from __future__ import annotations

from auth_fixtures import *  # noqa: F401,F403  (fixtures; see auth_fixtures.py)

import types
from pathlib import Path

import pytest

from m2c import cli
from m2c.auth import credentials as creds
from m2c.auth import login as login_mod
from m2c.auth.credentials import Credential, Secret
from mock_server import (
    CREDENTIAL,
    DEVICE_CODE,
    DEVICE_ID,
    DROP,
    POLL_PATH,
    START_PATH,
    USER_CODE,
    VERIFICATION_URL,
    approved,
    assert_no_secrets,
    denied,
    error,
    expired,
    pending,
    start_reply,
)


@pytest.fixture(autouse=True)
def _fake_time_for_login(monkeypatch, fake_time):
    """cli.main() calls login with its default sleep and clock.  Replace the
    ``time`` module as login.py sees it (only there: other code, like socket
    timeouts, keeps the real one) with the fake clock, so a login that waits
    for a code to expire takes no real time."""
    monkeypatch.setattr(
        login_mod,
        "time",
        types.SimpleNamespace(sleep=lambda s: fake_time.sleep(s), monotonic=lambda: fake_time.clock()),
    )
    return fake_time


@pytest.fixture
def configured(server, monkeypatch):
    monkeypatch.setenv("M2C_SERVER_URL", server.url)
    return server


def stored_credential(url):
    creds.save_credential(Credential(Secret(CREDENTIAL), DEVICE_ID, url))


# --------------------------------------------------------------------------
# help text: the stubs' promises are now true
# --------------------------------------------------------------------------
def test_help_no_longer_calls_login_or_logout_unimplemented(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "not implemented" not in out
    assert "M2C_SERVER_URL" in out


def test_logout_help_does_not_claim_to_revoke_the_device(capsys):
    """S03 has no device-authenticated revoke endpoint, so logout is local."""
    assert cli.main(["--help"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("logout"))
    assert "revoke this device" not in line
    assert "local" in line or "delete" in line


def test_login_help_describes_the_flow_and_the_device_name_option(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["login", "--help"])
    out = capsys.readouterr().out
    assert "--device-name" in out and "code" in out and "M2C_SERVER_URL" in out


def test_logout_help_names_the_web_devices_page(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["logout", "--help"])
    assert "Devices page" in capsys.readouterr().out


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------
def test_login_success_exits_zero_and_tells_the_person(configured, capsys):
    configured.script(START_PATH, start_reply())
    configured.script(POLL_PATH, pending(), approved())
    assert cli.main(["login", "--device-name", "laptop"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert USER_CODE in captured.out and VERIFICATION_URL in captured.out
    assert "Signed in" in captured.out
    assert_no_secrets(captured.out, captured.err)
    assert creds.load_credential().device_credential.reveal() == CREDENTIAL


def test_login_without_a_server_address_is_a_usage_error_and_sends_nothing(server, capsys, monkeypatch):
    monkeypatch.delenv("M2C_SERVER_URL", raising=False)
    assert cli.main(["login"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "M2C_SERVER_URL" in err and "Traceback" not in err
    assert server.requests == []


def test_login_with_a_bad_server_address_is_a_usage_error(capsys, monkeypatch):
    monkeypatch.setenv("M2C_SERVER_URL", "http://api.example.test")
    assert cli.main(["login"]) == cli.EXIT_USAGE
    assert "M2C_SERVER_URL" in capsys.readouterr().err


def test_login_with_a_bad_device_name_is_a_usage_error_and_sends_nothing(configured, capsys):
    assert cli.main(["login", "--device-name", "bad\x1bname"]) == cli.EXIT_USAGE
    assert "device name" in capsys.readouterr().err
    assert configured.requests == []


@pytest.mark.parametrize(
    "replies, word",
    [([denied()], "denied"), ([expired()], "expired"), ([error(403)], "403")],
)
def test_login_failures_exit_with_the_auth_status_and_a_one_line_reason(configured, capsys, replies, word):
    configured.script(START_PATH, start_reply())
    configured.script(POLL_PATH, *replies)
    assert cli.main(["login"]) == cli.EXIT_AUTH == 4
    captured = capsys.readouterr()
    assert word in captured.err
    assert captured.err.startswith("m2c login:")
    assert "Traceback" not in captured.err
    assert_no_secrets(captured.out, captured.err)


def test_login_network_failure_exits_with_the_auth_status(configured, capsys):
    configured.script(START_PATH, start_reply(expires_in=20))
    configured.script(POLL_PATH, DROP, repeat_last=True)
    assert cli.main(["login"]) == cli.EXIT_AUTH
    assert "reach the server" in capsys.readouterr().err


def test_ctrl_c_during_login_exits_130_quietly(configured, capsys, monkeypatch):
    configured.script(START_PATH, start_reply())

    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(login_mod.time, "sleep", interrupted)
    assert cli.main(["login"]) == 130
    err = capsys.readouterr().err
    assert "Cancelled" in err and "Traceback" not in err
    with pytest.raises(creds.NotLoggedInError):
        creds.load_credential()


def test_an_unexpected_error_prints_only_its_type(configured, capsys, monkeypatch):
    """Whatever goes wrong inside, the terminal gets a type name, never the
    exception's text (which could echo a secret) and never a traceback."""

    def explode(*args, **kwargs):
        raise RuntimeError(f"surprise {DEVICE_CODE} {CREDENTIAL}")

    monkeypatch.setattr(login_mod, "login", explode)
    assert cli.main(["login"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "Traceback" not in err
    assert_no_secrets(err)


def test_a_config_directory_problem_is_reported_plainly(configured, capsys, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    monkeypatch.setenv("M2C_HOME", str(blocker))
    assert cli.main(["login"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "not a directory" in err and "Traceback" not in err
    assert configured.requests == []


def test_a_loose_credentials_file_does_not_block_a_fresh_login(configured, capsys):
    """login replaces the file atomically, so it repairs what load refuses."""
    stored_credential(configured.url)
    path = creds.credentials_path()
    path.chmod(0o644)
    configured.script(START_PATH, start_reply())
    configured.script(POLL_PATH, approved())
    assert cli.main(["login"]) == cli.EXIT_OK
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------
# logout
# --------------------------------------------------------------------------
def test_logout_deletes_the_credential_and_points_at_the_devices_page(server, capsys):
    stored_credential(server.url)
    assert cli.main(["logout"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "deleted" in out and "Devices page" in out
    assert not creds.credentials_path().exists()
    assert server.requests == []


def test_logout_when_not_signed_in_is_a_calm_success(capsys):
    assert cli.main(["logout"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "Not signed in" in captured.out
    assert "Devices page" not in captured.out
    assert captured.err == ""


def test_logout_creates_nothing_when_there_is_nothing_to_delete(tmp_path, monkeypatch):
    target = tmp_path / "must-not-exist"
    monkeypatch.setenv("M2C_HOME", str(target))
    cli.main(["logout"])
    assert not target.exists()


def test_logout_removes_a_file_it_would_refuse_to_read(server, capsys):
    stored_credential(server.url)
    creds.credentials_path().chmod(0o644)
    assert cli.main(["logout"]) == cli.EXIT_OK
    assert not creds.credentials_path().exists()


def test_logout_failure_is_reported_and_never_claims_success(fake_keyring, capsys):
    stored_credential("https://api.example.test")
    fake_keyring.store[("m2c", creds._keyring_username())] = '{"x":1}'
    fake_keyring.fail_delete = RuntimeError(f"nope {CREDENTIAL}")
    assert cli.main(["logout"]) == cli.EXIT_AUTH
    captured = capsys.readouterr()
    assert "deleted" not in captured.out
    assert "could not" in captured.err
    assert_no_secrets(captured.out, captured.err)


def test_logout_empties_the_in_memory_token(server, fake_time):
    """Behavioural check: the same credential stored again after logout must
    fetch a fresh token instead of being served the old one from memory."""
    from mock_server import TOKEN_PATH, token_reply

    stored_credential(server.url)
    server.script(TOKEN_PATH, token_reply(jwt="aaa.bbb.one"), token_reply(jwt="aaa.bbb.two"))
    assert login_mod.get_access_token(clock=fake_time.clock).reveal() == "aaa.bbb.one"
    assert cli.main(["logout"]) == cli.EXIT_OK
    stored_credential(server.url)
    assert login_mod.get_access_token(clock=fake_time.clock).reveal() == "aaa.bbb.two"


def test_the_exit_statuses_are_documented_in_the_epilog(capsys):
    cli.main(["--help"])
    out = capsys.readouterr().out
    assert "4" in out and "sign-in" in out
