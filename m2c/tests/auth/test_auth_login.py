"""
m2c/tests/auth/test_auth_login.py -- Patch C03: the device-code login flow
against a scripted mock of the S03 endpoints.

Every state the server can answer (pending, approved, denied, expired),
every way the network can fail, and what the client does with a server that
says something unexpected.  Time is fake: ``FakeTime`` advances only when the
client sleeps, so the expected sleeps are exact numbers, not "about".

Redaction (the credential never reaching a log, message or traceback) has its
own file: test_auth_redaction.py.
"""
from __future__ import annotations

from auth_fixtures import *  # noqa: F401,F403  (fixtures; see auth_fixtures.py)

import io
import os
import socket
import stat
from pathlib import Path

import pytest

import m2c
from m2c.auth import credentials as creds
from m2c.auth import login as login_mod
from m2c.auth.credentials import (
    AuthError,
    AuthNetworkError,
    AuthProtocolError,
    AuthServerError,
    CredentialStoreError,
    LoginDeniedError,
    LoginExpiredError,
    NotLoggedInError,
)
from m2c.config import ServerUrlError
from m2c.paths import ConfigDirError
from mock_server import (
    CREDENTIAL,
    DEVICE_CODE,
    DEVICE_ID,
    DROP,
    POLL_PATH,
    START_PATH,
    USER_CODE,
    VERIFICATION_URL,
    MockServer,
    approved,
    closed_port_url,
    denied,
    error,
    expired,
    ok,
    pending,
    redirect,
    start_reply,
    Reply,
)


def run_login(url, fake_time, *, device_name=None, out=None, err=None, **kwargs):
    """login() with fake time, no jitter and captured output."""
    out = io.StringIO() if out is None else out
    err = io.StringIO() if err is None else err
    result = login_mod.login(
        url,
        device_name=device_name,
        out=out,
        err=err,
        sleep=fake_time.sleep,
        clock=fake_time.clock,
        jitter=lambda delay: delay,
        **kwargs,
    )
    return result, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------
# The request the client makes, and what it shows the person
# --------------------------------------------------------------------------
def test_pair_start_sends_exactly_the_three_labels(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time, device_name="my-laptop")
    request = server.requests[0]
    assert request.json == {
        "device_name": "my-laptop",
        "os": request.json["os"],
        "client_version": m2c.__version__,
    }
    assert request.json["os"] and request.json["os"].isprintable() and len(request.json["os"]) <= 64
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"] == f"m2c/{m2c.__version__}"


def test_the_default_device_name_is_the_host_name(server, fake_time, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "wsl-box")
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time)
    assert server.requests[0].json["device_name"] == "wsl-box"


@pytest.mark.parametrize(
    "hostname, expected",
    [
        ("  spaced  ", "spaced"),
        ("bell\x07name\n", "bellname"),
        ("x" * 100, "x" * 64),
        ("", "m2c-device"),
        ("\x1b\x07", "m2c-device"),
    ],
)
def test_a_default_device_name_is_cleaned_up_to_what_the_server_accepts(monkeypatch, hostname, expected):
    monkeypatch.setattr(socket, "gethostname", lambda: hostname)
    assert login_mod.default_device_name() == expected


def test_an_explicit_device_name_is_trimmed():
    assert login_mod.normalize_device_name("  laptop  ") == "laptop"


@pytest.mark.parametrize("bad", ["", "   ", "a" * 65, "bad\x1bname", "tab\there"])
def test_an_explicit_device_name_that_the_server_would_refuse_is_an_error(bad):
    with pytest.raises(ValueError):
        login_mod.normalize_device_name(bad)


def test_login_shows_the_code_and_the_address_and_never_the_device_code(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    _, out, err = run_login(server.url, fake_time)
    assert USER_CODE in out
    assert VERIFICATION_URL in out
    assert "10 minutes" in out
    assert DEVICE_CODE not in out + err
    assert CREDENTIAL not in out + err
    assert "Ctrl+C" in out
    assert err == ""


def test_login_never_tries_to_open_a_browser(server, fake_time, monkeypatch):
    """Browsers often cannot open from WSL2; the page is shown, not launched."""
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("tried to open a browser"))
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time)


def test_no_module_launches_a_browser_or_xdg_open():
    src = Path(m2c.__file__).resolve().parent
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "webbrowser" not in text and "xdg-open" not in text, py.name


def test_a_server_address_with_a_path_prefix_is_respected(server, fake_time):
    server.script("api/" + START_PATH, start_reply())
    server.script("api/" + POLL_PATH, approved())
    run_login(server.url + "/api/", fake_time)
    assert server.count("api/" + START_PATH) == 1


def test_login_refuses_an_unsafe_server_address_before_sending_anything(fake_time):
    with pytest.raises(ServerUrlError):
        run_login("http://api.example.test", fake_time)


# --------------------------------------------------------------------------
# approved / pending polling and the interval
# --------------------------------------------------------------------------
def test_approved_on_the_first_poll_stores_the_credential(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    result, out, _ = run_login(server.url, fake_time, device_name="laptop")
    assert result.device_id == DEVICE_ID
    assert result.device_name == "laptop"
    assert result.storage == "file"
    stored = creds.load_credential()
    assert stored.device_credential.reveal() == CREDENTIAL
    assert stored.device_id == DEVICE_ID
    assert stored.server_url == server.url
    assert stat.S_IMODE(os.stat(creds.credentials_path()).st_mode) == 0o600
    assert server.bodies(POLL_PATH) == [{"device_code": DEVICE_CODE}]
    assert fake_time.sleeps == [5]
    assert "Signed in" in out and str(creds.credentials_path()) in out and "0600" in out
    assert CREDENTIAL not in out


def test_pending_is_polled_at_the_servers_interval_until_approved(server, fake_time):
    server.script(START_PATH, start_reply(interval=7))
    server.script(POLL_PATH, pending(), pending(), pending(), approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [7, 7, 7, 7]
    assert server.count(POLL_PATH) == 4


@pytest.mark.parametrize("said, used", [(0, 1), (-5, 1), (3, 3), (60, 60), (9999, 60)])
def test_the_interval_is_clamped_to_something_sane(server, fake_time, said, used):
    server.script(START_PATH, start_reply(interval=said))
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [used]


def test_the_wait_is_capped_at_the_clamped_lifetime(server, fake_time):
    server.script(START_PATH, start_reply(interval=60, expires_in=99999))
    server.script(POLL_PATH, pending(), repeat_last=True)
    with pytest.raises(LoginExpiredError):
        run_login(server.url, fake_time)
    assert sum(fake_time.sleeps) == login_mod.MAX_EXPIRES_IN == 1800


def test_login_replacing_an_existing_credential_says_so(server, fake_time):
    creds.save_credential(
        creds.Credential(creds.Secret("OLD-" + "q" * 30), DEVICE_ID, server.url)
    )
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    _, out, _ = run_login(server.url, fake_time)
    assert "replaced" in out and "Devices page" in out
    assert creds.load_credential().device_credential.reveal() == CREDENTIAL


def test_a_first_login_does_not_mention_replacing_anything(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    _, out, _ = run_login(server.url, fake_time)
    assert "replaced" not in out


def test_storage_in_the_keyring_is_reported_as_such(server, fake_time, fake_keyring):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    result, out, _ = run_login(server.url, fake_time)
    assert result.storage == "keyring"
    assert "keyring" in out
    assert not creds.credentials_path().exists()


# --------------------------------------------------------------------------
# denied, expired
# --------------------------------------------------------------------------
def test_denied_is_a_clear_failure_and_stores_nothing(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, pending(), denied())
    with pytest.raises(LoginDeniedError) as info:
        run_login(server.url, fake_time)
    assert "denied" in str(info.value) and "m2c login" in str(info.value)
    assert server.count(POLL_PATH) == 2
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


def test_expired_from_the_server_is_a_clear_failure_and_stores_nothing(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, expired())
    with pytest.raises(LoginExpiredError) as info:
        run_login(server.url, fake_time)
    assert "expired" in str(info.value) and "m2c login" in str(info.value)
    assert server.count(POLL_PATH) == 1
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


@pytest.mark.parametrize(
    "expires_in, expected_sleeps",
    [(20, [5, 5, 5, 5]), (12, [5, 5, 2]), (3, [3]), (1, [1])],
)
def test_the_code_expiring_locally_stops_the_wait_without_oversleeping(
    server, fake_time, expires_in, expected_sleeps
):
    server.script(START_PATH, start_reply(expires_in=expires_in))
    server.script(POLL_PATH, pending(), repeat_last=True)
    with pytest.raises(LoginExpiredError):
        run_login(server.url, fake_time)
    assert fake_time.sleeps == expected_sleeps
    assert server.count(POLL_PATH) == len(expected_sleeps)  # one last poll at the deadline


def test_an_approval_that_lands_at_the_deadline_is_still_collected(server, fake_time):
    server.script(START_PATH, start_reply(expires_in=10))
    server.script(POLL_PATH, pending(), approved())
    result, _, _ = run_login(server.url, fake_time)
    assert result.device_id == DEVICE_ID
    assert fake_time.sleeps == [5, 5]


# --------------------------------------------------------------------------
# network errors
# --------------------------------------------------------------------------
def test_dropped_connections_are_retried_with_growing_delays(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, DROP, DROP, approved())
    _, _, err = run_login(server.url, fake_time)
    assert fake_time.sleeps == [5, 10, 20]
    assert err.count("trouble reaching") == 1  # said once, not on every retry
    assert creds.load_credential().device_credential.reveal() == CREDENTIAL


def test_the_backoff_is_capped(server, fake_time):
    server.script(START_PATH, start_reply(expires_in=1800))
    server.script(POLL_PATH, *([DROP] * 6), approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [5, 10, 20, 40, 60, 60, 60]
    assert max(fake_time.sleeps) == login_mod.MAX_BACKOFF


def test_a_recovered_connection_resets_the_backoff(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, DROP, pending(), DROP, approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [5, 10, 5, 10]


def test_network_errors_until_the_code_expires_end_with_a_network_message(server, fake_time):
    server.script(START_PATH, start_reply(expires_in=30))
    server.script(POLL_PATH, DROP, repeat_last=True)
    with pytest.raises(AuthNetworkError) as info:
        run_login(server.url, fake_time)
    assert "reach the server" in str(info.value)
    assert "m2c login" in str(info.value)
    assert info.value.retryable is True
    assert fake_time.sleeps == [5, 10, 15]
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


def test_a_server_that_cannot_be_reached_at_all_gives_up_after_a_few_tries(fake_time):
    with pytest.raises(AuthNetworkError) as info:
        run_login(closed_port_url(), fake_time)
    assert "reach the server" in str(info.value)
    assert info.value.retryable is True
    assert fake_time.sleeps == [2, 4]  # three attempts, two waits


@pytest.mark.parametrize("failing_status", [500, 502, 503, 504])
def test_a_server_error_on_poll_is_retried(server, fake_time, failing_status):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, error(failing_status), approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [5, 10]


def test_too_many_requests_on_poll_honours_retry_after(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, error(429, **{"Retry-After": "30"}), approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [5, 30]


def test_retry_after_is_never_allowed_to_outlast_the_code(server, fake_time):
    server.script(START_PATH, start_reply(expires_in=20))
    server.script(POLL_PATH, error(429, **{"Retry-After": "3600"}), repeat_last=True)
    with pytest.raises(AuthServerError):
        run_login(server.url, fake_time)
    assert sum(fake_time.sleeps) == 20


def test_a_garbage_200_on_poll_is_treated_as_a_hiccup(server, fake_time):
    """A captive portal or proxy answering with a web page must not abort the login."""
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, Reply(200, "<html>please sign in to the wifi</html>"), ok(["not", "a", "dict"]), approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [5, 10, 20]


# --------------------------------------------------------------------------
# permanent failures are not retried
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_an_unexpected_client_error_on_poll_stops_at_once(server, fake_time, status):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, error(status))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    assert info.value.status == status
    assert info.value.retryable is False
    assert server.count(POLL_PATH) == 1


def test_poll_400_means_the_pairing_is_unknown_or_already_used(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, error(400))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    message = str(info.value)
    assert info.value.status == 400
    assert "m2c login" in message
    assert "Devices page" not in message  # nothing suggests a device was created


def test_poll_400_after_a_lost_answer_warns_that_a_device_may_have_been_created(server, fake_time):
    """S03 returns the credential exactly once.  If our poll's answer was lost
    and the retry then says 'already used', the credential may have been
    issued and never received; the person must hear about the orphan."""
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, DROP, error(400))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    message = str(info.value)
    assert "may have" in message and "Devices page" in message and "revoke" in message
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


def test_a_proven_unclaimed_code_clears_the_suspicion(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, DROP, pending(), error(400))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    assert "Devices page" not in str(info.value)


def test_an_unknown_status_word_is_a_protocol_error_not_a_retry(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, ok({"status": "mystery"}))
    with pytest.raises(AuthProtocolError):
        run_login(server.url, fake_time)
    assert server.count(POLL_PATH) == 1


# --------------------------------------------------------------------------
# start failures
# --------------------------------------------------------------------------
def test_start_429_is_reported_with_the_wait_and_not_retried(server, fake_time):
    server.script(START_PATH, error(429, **{"Retry-After": "600"}))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    assert info.value.status == 429 and info.value.retryable is True
    assert "10 minutes" in str(info.value)
    assert server.count(START_PATH) == 1
    assert fake_time.sleeps == []


def test_start_503_is_retried_then_succeeds(server, fake_time):
    server.script(START_PATH, error(503), start_reply())
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time)
    assert fake_time.sleeps == [2, 5]
    assert server.count(START_PATH) == 2


def test_start_server_errors_give_up_after_three_attempts(server, fake_time):
    server.script(START_PATH, error(500), error(500), error(500))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    assert info.value.status == 500
    assert server.count(START_PATH) == 3


def test_start_400_is_not_retried(server, fake_time):
    server.script(START_PATH, error(400))
    with pytest.raises(AuthServerError) as info:
        run_login(server.url, fake_time)
    assert info.value.status == 400 and info.value.retryable is False
    assert server.count(START_PATH) == 1


# --------------------------------------------------------------------------
# What the server is allowed to make the terminal show
# --------------------------------------------------------------------------
BAD_START_BODIES = {
    "no device_code": dict(device_code=None),
    "short device_code": dict(device_code="short"),
    "device_code with a space": dict(device_code="has a space " + "x" * 30),
    "no user_code": dict(user_code=None),
    "escape sequence in user_code": dict(user_code="\x1b[2JBCDF"),
    "newline in user_code": dict(user_code="BCDF\nGHJK"),
    "overlong user_code": dict(user_code="B" * 40),
    "no verification_url": dict(verification_url=None),
    "javascript url": dict(verification_url="javascript:alert(1)"),
    "ftp url": dict(verification_url="ftp://files.example.test/x"),
    "escape in url": dict(verification_url="https://a.example.test/\x1b[31m"),
    "newline in url": dict(verification_url="https://a.example.test/\nSudo password:"),
    "space in url": dict(verification_url="https://a.example.test/a b"),
    "overlong url": dict(verification_url="https://a.example.test/" + "a" * 600),
    "no interval": dict(interval=None),
    "string interval": dict(interval="5"),
    "float interval": dict(interval=5.5),
    "bool interval": dict(interval=True),
    "no expires_in": dict(expires_in=None),
    "zero expires_in": dict(expires_in=0),
    "negative expires_in": dict(expires_in=-1),
    "string expires_in": dict(expires_in="600"),
}


@pytest.mark.parametrize("overrides", BAD_START_BODIES.values(), ids=BAD_START_BODIES.keys())
def test_a_malformed_start_reply_is_refused_and_nothing_is_printed(server, fake_time, overrides):
    server.script(START_PATH, start_reply(**overrides))
    out = io.StringIO()
    with pytest.raises(AuthProtocolError):
        run_login(server.url, fake_time, out=out)
    assert out.getvalue() == ""  # a hostile string never reaches the terminal
    assert server.count(POLL_PATH) == 0


@pytest.mark.parametrize(
    "reply",
    [
        Reply(200, "<html>captive portal</html>"),
        ok(["a", "list"]),
        ok("a string"),
        Reply(200, b""),
        Reply(200, b"x" * 200_000),  # larger than the client will read
    ],
    ids=["html", "list", "string", "empty", "oversized"],
)
def test_a_start_reply_that_is_not_the_expected_json_is_refused(server, fake_time, reply):
    server.script(START_PATH, reply)
    with pytest.raises(AuthProtocolError):
        run_login(server.url, fake_time)


def test_a_redirect_is_never_followed(fake_time):
    """A redirect could move the pairing to another host (or downgrade it)."""
    other = MockServer().start()
    first = MockServer().start()
    try:
        first.script(START_PATH, redirect(other.url + "/" + START_PATH))
        with pytest.raises(AuthServerError) as info:
            run_login(first.url, fake_time)
        assert info.value.status == 307
        assert other.requests == []
    finally:
        first.stop()
        other.stop()


# --------------------------------------------------------------------------
# an approval that cannot be used or kept
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        dict(device_credential=None),
        dict(device_credential="short"),
        dict(device_credential="has a space " + "x" * 30),
        dict(device_id=None),
        dict(device_id="not-a-uuid"),
    ],
)
def test_an_unusable_approval_stores_nothing_and_points_at_the_devices_page(server, fake_time, overrides):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved(**overrides))
    with pytest.raises(AuthProtocolError) as info:
        run_login(server.url, fake_time)
    assert "Devices page" in str(info.value)
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


def test_a_save_failure_after_approval_tells_the_person_what_to_do(server, fake_time, monkeypatch):
    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(creds, "_write_file_atomically", boom)
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    with pytest.raises(CredentialStoreError) as info:
        run_login(server.url, fake_time)
    message = str(info.value)
    assert "approved" in message and "could not be saved" in message
    assert "Devices page" in message and "m2c login" in message


def test_an_unusable_config_directory_is_found_before_anything_is_approved(server, fake_time, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("M2C_HOME", str(blocker))
    with pytest.raises(ConfigDirError):
        run_login(server.url, fake_time)
    assert server.requests == []  # no pairing was started that could be wasted


def test_ctrl_c_while_waiting_stores_nothing(server, fake_time):
    server.script(START_PATH, start_reply())

    def interrupted(_seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        login_mod.login(
            server.url,
            out=io.StringIO(),
            err=io.StringIO(),
            sleep=interrupted,
            clock=fake_time.clock,
            jitter=lambda d: d,
        )
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


# --------------------------------------------------------------------------
# logout
# --------------------------------------------------------------------------
def test_logout_deletes_the_local_credential(server, fake_time):
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time)
    assert login_mod.logout() is True
    assert login_mod.logout() is False
    with pytest.raises(NotLoggedInError):
        creds.load_credential()


def test_logout_makes_no_network_call(server, fake_time):
    """S03 has no device-authenticated revoke endpoint, so logout is local."""
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, approved())
    run_login(server.url, fake_time)
    before = len(server.requests)
    login_mod.logout()
    assert len(server.requests) == before


def test_every_failure_here_is_an_auth_error_with_no_chained_exception(server, fake_time):
    """Callers can catch one base class, and nothing hides behind __cause__."""
    server.script(START_PATH, start_reply())
    server.script(POLL_PATH, denied())
    with pytest.raises(AuthError) as info:
        run_login(server.url, fake_time)
    assert info.value.__cause__ is None and info.value.__context__ is None
