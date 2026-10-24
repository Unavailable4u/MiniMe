"""
m2c/tests/auth/test_auth_token.py -- Patch C03: load_credential() for the
device link, and get_access_token() for later HTTP calls.

The token helper exchanges the stored device credential for a short-lived JWT
(POST /v1/code/devices/token -> {jwt, expires_in}), keeps it in memory and
refreshes it a safety margin before it expires.  A revoked device is a
specific, non-retryable error; transient problems are retryable ones.  Nothing
in Wave 2 or 3 calls this yet, so the tests are what pin its behaviour.
"""
from __future__ import annotations

from auth_fixtures import *  # noqa: F401,F403  (fixtures; see auth_fixtures.py)

import json
import os
import threading

import pytest

from m2c.auth import credentials as creds
from m2c.auth import login as login_mod
from m2c.auth.credentials import (
    AuthNetworkError,
    AuthProtocolError,
    AuthServerError,
    Credential,
    DeviceRevokedError,
    InsecureCredentialFileError,
    NotLoggedInError,
    Secret,
    ServerMismatchError,
)
from m2c.auth import get_access_token as exported_get_access_token
from m2c.auth import load_credential as exported_load_credential
from mock_server import (
    CREDENTIAL,
    DEVICE_ID,
    DROP,
    JWT,
    TOKEN_PATH,
    FakeTime,
    MockServer,
    error,
    ok,
    token_reply,
)


def store(url: str, secret: str = CREDENTIAL) -> Credential:
    cred = Credential(Secret(secret), DEVICE_ID, url)
    creds.save_credential(cred)
    return cred


def fetch(fake_time: FakeTime, **kwargs) -> Secret:
    return login_mod.get_access_token(clock=fake_time.clock, **kwargs)


def test_the_helpers_are_exported_from_the_package():
    """C04 imports these two by their package-level names."""
    assert exported_load_credential is creds.load_credential
    assert exported_get_access_token is login_mod.get_access_token


# --------------------------------------------------------------------------
# the call itself
# --------------------------------------------------------------------------
def test_the_credential_is_exchanged_for_a_jwt(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply())
    token = fetch(fake_time)
    assert isinstance(token, Secret)
    assert token.reveal() == JWT
    assert server.bodies(TOKEN_PATH) == [{"device_credential": CREDENTIAL}]
    assert server.requests[0].headers["content-type"] == "application/json"


def test_no_stored_credential_is_a_clear_error_and_no_request(server, fake_time):
    with pytest.raises(NotLoggedInError):
        fetch(fake_time)
    assert server.requests == []


def test_a_loose_credentials_file_stops_everything_before_any_request(server, fake_time):
    store(server.url)
    os.chmod(creds.credentials_path(), 0o644)
    with pytest.raises(InsecureCredentialFileError):
        fetch(fake_time)
    assert server.requests == []


# --------------------------------------------------------------------------
# caching and the refresh margin
# --------------------------------------------------------------------------
def test_the_token_is_cached_in_memory(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply())
    first = fetch(fake_time)
    fake_time.advance(100)
    second = fetch(fake_time)
    assert first.reveal() == second.reveal() == JWT
    assert server.count(TOKEN_PATH) == 1


def test_the_token_is_refreshed_a_minute_before_it_expires(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply(900, jwt="aaa.bbb.one"), token_reply(900, jwt="aaa.bbb.two"))
    assert fetch(fake_time).reveal() == "aaa.bbb.one"
    fake_time.advance(839)  # 900 - 60 margin = 840: one second early
    assert fetch(fake_time).reveal() == "aaa.bbb.one"
    fake_time.advance(1)
    assert fetch(fake_time).reveal() == "aaa.bbb.two"
    assert server.count(TOKEN_PATH) == 2
    assert login_mod.TOKEN_REFRESH_MARGIN == 60


def test_a_very_short_lived_token_is_still_cached_briefly(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply(30, jwt="aaa.bbb.one"), token_reply(30, jwt="aaa.bbb.two"))
    fetch(fake_time)
    fake_time.advance(14)  # margin is half the lifetime here: 15
    assert fetch(fake_time).reveal() == "aaa.bbb.one"
    fake_time.advance(1)
    assert fetch(fake_time).reveal() == "aaa.bbb.two"


def test_an_absurd_lifetime_is_not_trusted(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply(10**9, jwt="aaa.bbb.one"), token_reply(900, jwt="aaa.bbb.two"))
    fetch(fake_time)
    fake_time.advance(login_mod.MAX_TOKEN_LIFETIME)  # an hour
    assert fetch(fake_time).reveal() == "aaa.bbb.two"


def test_a_different_credential_never_gets_the_cached_token(server, fake_time):
    store(server.url, secret=CREDENTIAL)
    server.script(TOKEN_PATH, token_reply(jwt="aaa.bbb.one"), token_reply(jwt="aaa.bbb.two"))
    assert fetch(fake_time).reveal() == "aaa.bbb.one"
    store(server.url, secret="OTHER-" + "z" * 30)  # e.g. logout then login as someone else
    assert fetch(fake_time).reveal() == "aaa.bbb.two"
    assert server.bodies(TOKEN_PATH)[1] == {"device_credential": "OTHER-" + "z" * 30}


def test_logout_empties_the_cache(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply())
    fetch(fake_time)
    login_mod.logout()
    with pytest.raises(NotLoggedInError):
        fetch(fake_time)


def test_concurrent_callers_share_one_request(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply())
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            results.append(fetch(fake_time).reveal())
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert results == [JWT] * 8
    assert server.count(TOKEN_PATH) == 1


# --------------------------------------------------------------------------
# a revoked device, and other failures
# --------------------------------------------------------------------------
def test_a_revoked_device_is_a_specific_non_retryable_error(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, error(401, {"detail": "Invalid or revoked device credential."}))
    with pytest.raises(DeviceRevokedError) as info:
        fetch(fake_time)
    assert info.value.retryable is False
    assert "revoked" in str(info.value) and "m2c login" in str(info.value)


def test_revocation_clears_the_cache_and_does_not_delete_the_credential(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, token_reply(), error(401))
    fetch(fake_time)
    fake_time.advance(900)  # past expiry, so the next call goes to the server
    with pytest.raises(DeviceRevokedError):
        fetch(fake_time)
    assert creds.credentials_path().exists()  # leaving it is the person's call (m2c logout)
    server.script(TOKEN_PATH, error(401))
    with pytest.raises(DeviceRevokedError):
        fetch(fake_time)  # not served from a stale cache


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_server_trouble_is_a_retryable_error_and_is_not_retried_here(server, fake_time, status):
    """Retrying belongs to the caller (the link client has its own backoff)."""
    store(server.url)
    server.script(TOKEN_PATH, error(status))
    with pytest.raises(AuthServerError) as info:
        fetch(fake_time)
    assert info.value.status == status and info.value.retryable is True
    assert server.count(TOKEN_PATH) == 1
    assert fake_time.sleeps == []


def test_an_unexpected_4xx_is_not_retryable(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, error(403))
    with pytest.raises(AuthServerError) as info:
        fetch(fake_time)
    assert info.value.retryable is False


def test_a_dropped_connection_is_a_retryable_network_error(server, fake_time):
    store(server.url)
    server.script(TOKEN_PATH, DROP)
    with pytest.raises(AuthNetworkError) as info:
        fetch(fake_time)
    assert info.value.retryable is True


@pytest.mark.parametrize(
    "reply",
    [
        ok({"expires_in": 900}),
        ok({"jwt": JWT}),
        ok({"jwt": "", "expires_in": 900}),
        ok({"jwt": "not a jwt", "expires_in": 900}),
        ok({"jwt": "a.b", "expires_in": 900}),
        ok({"jwt": "a.b.c\nInjected: header", "expires_in": 900}),
        ok({"jwt": "a" * 9000 + ".b.c", "expires_in": 900}),
        ok({"jwt": JWT, "expires_in": 0}),
        ok({"jwt": JWT, "expires_in": -5}),
        ok({"jwt": JWT, "expires_in": "900"}),
        ok({"jwt": JWT, "expires_in": True}),
        ok({"jwt": 12345, "expires_in": 900}),
        ok(["not", "a", "dict"]),
    ],
)
def test_a_malformed_token_reply_is_refused_and_not_cached(server, fake_time, reply):
    store(server.url)
    server.script(TOKEN_PATH, reply, token_reply())
    with pytest.raises(AuthProtocolError):
        fetch(fake_time)
    assert fetch(fake_time).reveal() == JWT  # nothing bad was cached
    assert server.count(TOKEN_PATH) == 2


# --------------------------------------------------------------------------
# the credential only ever goes to the server it was issued by
# --------------------------------------------------------------------------
def test_a_credential_is_not_sent_to_a_different_server_than_issued_it(server, fake_time, monkeypatch):
    attacker = MockServer().start()
    try:
        store(server.url)
        monkeypatch.setenv("M2C_SERVER_URL", attacker.url)
        with pytest.raises(ServerMismatchError) as info:
            fetch(fake_time)
        assert attacker.requests == [] and server.requests == []
        assert "m2c logout" in str(info.value)
        assert CREDENTIAL not in str(info.value)
    finally:
        attacker.stop()


def test_an_explicit_server_argument_is_held_to_the_same_rule(server, fake_time):
    store(server.url)
    with pytest.raises(ServerMismatchError):
        fetch(fake_time, server_url="https://elsewhere.example.test")
    assert server.requests == []


def test_the_same_server_written_differently_is_the_same_server(server, fake_time, monkeypatch):
    store(server.url)
    server.script(TOKEN_PATH, token_reply())
    monkeypatch.setenv("M2C_SERVER_URL", server.url.upper().replace("HTTP", "http", 1) + "/")
    assert fetch(fake_time).reveal() == JWT


def test_with_no_setting_the_credentials_own_server_is_used(server, fake_time, monkeypatch):
    monkeypatch.delenv("M2C_SERVER_URL", raising=False)
    store(server.url)
    server.script(TOKEN_PATH, token_reply())
    assert fetch(fake_time).reveal() == JWT


def test_the_credential_is_loadable_for_the_device_link():
    """C04 reads the credential for the socket's hello message."""
    store("https://api.example.test")
    loaded = creds.load_credential()
    assert loaded.device_credential.reveal() == CREDENTIAL
    assert json.loads(json.dumps({"device_credential": loaded.device_credential.reveal()})) == {
        "device_credential": CREDENTIAL
    }
