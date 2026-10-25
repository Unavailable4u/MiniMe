"""
m2c/tests/link/test_link_handshake.py -- Patch C04: the hello/welcome handshake
(protocol/v1/hello.json and welcome.json, as S04 implements them), and which
failures end the reconnect loop.

The rule under test: unauthorized, device_revoked and protocol_unsupported are
FINAL -- the link stops, raises a specific exception, and never connects
again ("a loop that keeps retrying a revoked device is a bug").  Everything
else is retried with backoff.
"""
from __future__ import annotations

import json
import re

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

from m2c.auth.credentials import NotLoggedInError, ServerMismatchError
from m2c.link import (
    LinkClosedError,
    LinkDeviceRevokedError,
    LinkProtocolUnsupportedError,
    LinkSupersededError,
    LinkUnauthorizedError,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")


# --------------------------------------------------------------------------
# What the client says
# --------------------------------------------------------------------------
def test_hello_has_exactly_the_p00_fields_and_no_jwt():
    store = open_db()
    cred_text = "m2c-test-cred-" + "q" * 30

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            url = srv.server_url
            link = make_link(store, srv, credential=make_credential(url, cred_text),
                             capabilities=("sandbox", "acp"), client_version="9.8.7")
            link.start()
            await link.wait_connected(5)
            await link.aclose()
        return srv

    srv = run_async(scenario())
    srv.assert_clean()
    hello = srv.messages(0)[0]
    assert set(hello) == {"v", "t", "id", "ts", "payload"}            # no run_id, no seq
    assert hello["v"] == 1 and hello["t"] == "hello"
    assert UUID_RE.match(hello["id"]) and TS_RE.match(hello["ts"])
    assert hello["payload"] == {
        "protocol_versions": [1],
        "device_credential": cred_text,
        "client_version": "9.8.7",
        "capabilities": ["sandbox", "acp"],
    }
    # P00: the socket is authenticated by the credential itself; there is no JWT anywhere.
    lowered = json.dumps(hello).lower()
    assert "jwt" not in lowered and "access_token" not in lowered and "bearer" not in lowered


def test_default_client_version_is_the_package_version_and_capabilities_default_to_none():
    import m2c

    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.aclose()
        return srv

    srv = run_async(scenario())
    payload = srv.messages(0)[0]["payload"]
    assert payload["client_version"] == m2c.__version__
    assert payload["capabilities"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capabilities": ("Has Spaces",)},
        {"capabilities": ("a", "a")},
        {"capabilities": tuple(f"c{i}" for i in range(33))},
        {"client_version": ""},
        {"client_version": "v" * 65},
    ],
)
def test_a_hello_the_server_would_refuse_is_refused_locally(kwargs):
    """A bad hello is not retryable; fail at construction instead of looping."""
    store = open_db()
    with pytest.raises(ValueError):
        make_link(store, "http://127.0.0.1:1", **kwargs)


def test_the_client_waits_for_welcome_and_exposes_what_it_says():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script(welcome_env(max_message_bytes=4096))) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            assert link.state == "idle" and link.limits is None
            link.start()
            await link.wait_connected(5)
            assert link.state == "connected"
            info = (link.limits, link.server_version, link.protocol)
            await link.aclose()
            return info, rec.states

    (limits, version, protocol), states = run_async(scenario())
    assert limits == {"max_parallel_runs": 3, "max_message_bytes": 4096}
    assert version == "fake-1" and protocol == 1
    assert states[:2] == ["connecting", "connected"] and states[-1] == "closed"


# --------------------------------------------------------------------------
# Final errors: stop, raise, never retry
# --------------------------------------------------------------------------
FATAL_REPLIES = [
    ("unauthorized", 4401, LinkUnauthorizedError, None),
    ("device_revoked", 4403, LinkDeviceRevokedError, None),
    ("protocol_unsupported", 4426, LinkProtocolUnsupportedError, {"min_protocol": 2, "max_protocol": 3}),
]


@pytest.mark.parametrize("code, close_code, exc_type, details", FATAL_REPLIES)
def test_a_final_error_in_reply_to_hello_stops_the_link_for_good(code, close_code, exc_type, details):
    store = open_db()

    async def scenario():
        # repeat_last: if the client wrongly came back, it would find a server to talk to.
        async with FakeServer(reject_script(code, close_code, details=details), repeat_last=True) as srv:
            link = make_link(store, srv)
            link.start()
            with pytest.raises(exc_type) as raised:
                await link.wait_connected(5)
            with pytest.raises(exc_type):
                await link.wait_closed()
            await asyncio.sleep(0.1)                       # a buggy loop would reconnect by now
            return srv, link, raised.value

    srv, link, exc = run_async(scenario())
    assert len(srv.connections) == 1
    assert link.state == "failed" and link.failure is exc
    assert link.test_time.sleeps == []                     # it did not even back off: it stopped
    assert exc.code == code
    if details:
        assert (exc.min_protocol, exc.max_protocol) == (2, 1)     # the server's minimum; the newest this client speaks
    srv.assert_clean()


@pytest.mark.parametrize("code, close_code, exc_type, details", FATAL_REPLIES)
def test_the_close_code_alone_is_enough_when_the_error_frame_is_lost(code, close_code, exc_type, details):
    store = open_db()

    async def scenario():
        async with FakeServer(close_only_script(close_code), repeat_last=True) as srv:
            link = make_link(store, srv)
            link.start()
            with pytest.raises(exc_type):
                await link.wait_closed()
            return srv

    assert len(run_async(scenario()).connections) == 1


@pytest.mark.parametrize("code", ["device_revoked", "unauthorized"])
def test_revoked_or_unauthorized_at_any_time_is_final(code):
    """No token refresh exists: the server re-validates the device itself and
    ends the socket with this error (S04, every <=30 s)."""
    store = open_db()

    async def scenario():
        async def script(conn):
            await do_handshake(conn)
            await asyncio.sleep(0.05)
            await conn.send(error_env(code))
            await conn.close(4403 if code == "device_revoked" else 4401)

        async with FakeServer(script, repeat_last=True) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises((LinkDeviceRevokedError, LinkUnauthorizedError)) as raised:
                await link.wait_closed()
            await asyncio.sleep(0.1)
            return srv, link, raised.value

    srv, link, exc = run_async(scenario())
    assert exc.code == code
    assert len(srv.connections) == 1 and link.state == "failed"


def test_being_superseded_by_a_newer_connection_is_final():
    """S04: close code 4409 means the same device connected again and the newer
    one wins.  Reconnecting would start a tug of war between two processes."""
    store = open_db()

    async def scenario():
        async def script(conn):
            await do_handshake(conn)
            await conn.close(4409, "superseded")

        async with FakeServer(script, repeat_last=True) as srv:
            link = make_link(store, srv)
            link.start()
            with pytest.raises(LinkSupersededError):
                await link.wait_closed()
            await asyncio.sleep(0.1)
            return srv

    assert len(run_async(scenario()).connections) == 1


# --------------------------------------------------------------------------
# min_protocol
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "welcome",
    [
        welcome_env(protocol=2, min_protocol=2),     # a server that moved on
        welcome_env(protocol=1, min_protocol=2),     # min above anything we speak, even if it claims v1
        welcome_env(protocol=2, min_protocol=1),     # it chose a version we never offered
    ],
    ids=["moved-on", "min-too-high", "unoffered-version"],
)
def test_a_server_whose_minimum_protocol_is_too_high_is_refused_clearly(welcome):
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script(welcome), repeat_last=True) as srv:
            link = make_link(store, srv)
            link.start()
            with pytest.raises(LinkProtocolUnsupportedError) as raised:
                await link.wait_closed()
            await asyncio.sleep(0.05)
            return srv, raised.value

    srv, exc = run_async(scenario())
    assert len(srv.connections) == 1
    assert exc.max_protocol == 1
    assert "update" in str(exc).lower()


# --------------------------------------------------------------------------
# Retried failures
# --------------------------------------------------------------------------
def test_rate_limited_waits_at_least_as_long_as_the_server_asked():
    store = open_db()

    async def scenario():
        async with FakeServer(
            reject_script("rate_limited", 4429, details={"retry_after_s": 7}), idle_script()
        ) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            sleeps = list(link.test_time.sleeps)
            await link.aclose()
            return srv, sleeps

    srv, sleeps = run_async(scenario())
    assert len(srv.connections) == 2
    assert sleeps and sleeps[0] >= 7


def test_internal_error_is_retried_with_backoff():
    store = open_db()

    async def scenario():
        async with FakeServer(reject_script("internal", 1011), idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            sleeps = list(link.test_time.sleeps)
            await link.aclose()
            return srv, sleeps

    srv, sleeps = run_async(scenario())
    assert len(srv.connections) == 2 and sleeps == [0.5]      # rng() = 0.5 of a 1 s ceiling


@pytest.mark.parametrize(
    "bad_welcome",
    [
        "not json at all",
        json.dumps([1, 2, 3]),
        {"v": 1, "t": "welcome", "id": "x", "ts": "x", "payload": {}},
        welcome_env(limits={}),
        welcome_env(token_ttl_s="soon"),
        {**welcome_env(), "v": 2},
        env("pong", {}),                                      # right shape, wrong message
    ],
    ids=["text", "list", "empty-payload", "no-max-parallel", "ttl-type", "wrong-v", "not-a-welcome"],
)
def test_a_malformed_welcome_is_not_trusted_and_the_connection_is_retried(bad_welcome):
    store = open_db()

    async def scenario():
        async with FakeServer(raw_welcome_script(bad_welcome), idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.aclose()
            return srv

    srv = run_async(scenario())
    assert len(srv.connections) == 2


def test_no_welcome_within_the_timeout_is_retried():
    store = open_db()

    async def scenario():
        async with FakeServer(silent_script(), idle_script()) as srv:
            link = make_link(store, srv, handshake_timeout_s=0.2)
            link.start()
            await link.wait_connected(5)
            await link.aclose()
            return srv

    assert len(run_async(scenario()).connections) == 2


def test_connection_refused_backs_off_exponentially_then_recovers():
    store = open_db()
    attempts = {"n": 0}

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            from websockets.asyncio.client import connect as real_connect

            async def flaky_connect(*args, **kwargs):
                attempts["n"] += 1
                if attempts["n"] <= 4:
                    raise ConnectionRefusedError()
                return await real_connect(*args, **kwargs)

            link = make_link(store, srv, connector=flaky_connect)
            link.start()
            await link.wait_connected(5)
            sleeps = list(link.test_time.sleeps)
            await link.aclose()
            return sleeps

    sleeps = run_async(scenario())
    assert sleeps == [0.5, 1.0, 2.0, 4.0]                     # half of 1, 2, 4, 8 s ceilings


def test_backoff_is_reset_by_a_connection_that_stayed_up_but_not_by_a_flapping_one():
    store = open_db()

    async def scenario():
        fake_time = FastTime()
        refused = {"n": 0}

        def drop_after(seconds):
            async def script(conn):
                await do_handshake(conn)
                await asyncio.sleep(0.05)        # let the client note when it connected
                fake_time.advance(seconds)
                await conn.kill()

            return script

        # Two refused connects first, so the backoff is already at step 3 when
        # the first connection succeeds.  That connection stays up "40 s"
        # (stable), the next two stay up "1 s" (flapping), then a good one.
        async with FakeServer(drop_after(40), drop_after(1), drop_after(1), idle_script()) as srv:
            from websockets.asyncio.client import connect as real_connect

            async def connector(*args, **kwargs):
                refused["n"] += 1
                if refused["n"] <= 2:
                    raise ConnectionRefusedError()
                return await real_connect(*args, **kwargs)

            link = make_link(store, srv, fake_time=fake_time, connector=connector)
            link.start()
            await link.wait_connected(5)
            await until(lambda: len(srv.connections) == 4 and link.state == "connected")
            sleeps = list(fake_time.sleeps)
            await link.aclose()
            return sleeps

    sleeps = run_async(scenario())
    # refused twice: 0.5, 1.0.  The stable connection resets the sequence: 0.5 again, not 2.0.
    # Each flapping connection does not reset it: 1.0, then 2.0.
    assert sleeps == [0.5, 1.0, 0.5, 1.0, 2.0]


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------
def test_not_logged_in_is_final_and_nothing_is_ever_sent():
    store = open_db()

    def not_logged_in():
        raise NotLoggedInError("Not signed in. Run 'm2c login' to sign in.")

    async def scenario():
        async with FakeServer(idle_script(), repeat_last=True) as srv:
            link = make_link(store, srv, credential_loader=not_logged_in)
            link.start()
            with pytest.raises(NotLoggedInError):
                await link.wait_closed()
            return srv

    assert run_async(scenario()).connections == []


def test_a_credential_is_only_sent_to_the_server_that_issued_it():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script(), repeat_last=True) as srv:
            link = make_link(store, srv, server_url="http://127.0.0.1:9")   # not where the credential came from
            link.start()
            with pytest.raises(ServerMismatchError):
                await link.wait_closed()
            return srv

    assert run_async(scenario()).connections == []


def test_the_credential_is_reloaded_on_every_connection_so_a_logout_stops_the_loop():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script(), idle_script(), repeat_last=True) as srv:
            first = make_credential(srv.server_url, "m2c-test-cred-" + "a" * 30)
            second = make_credential(srv.server_url, "m2c-test-cred-" + "b" * 30)
            served = [first, second]

            def loader():
                if not served:
                    raise NotLoggedInError("Not signed in. Run 'm2c login' to sign in.")
                return served.pop(0)

            link = make_link(store, srv, credential_loader=loader)
            link.start()
            await link.wait_connected(5)
            srv.connections[0].ws.transport.abort()                     # connection lost
            await until(lambda: len(srv.connections) == 2, what="the reconnect")
            await until(lambda: link.state == "connected", what="connected again")
            srv.connections[1].ws.transport.abort()                     # lost again; now the user has logged out
            with pytest.raises(NotLoggedInError):
                await link.wait_closed()
            return srv

    srv = run_async(scenario())
    assert len(srv.connections) == 2
    assert srv.messages(0)[0]["payload"]["device_credential"].endswith("a" * 30)
    assert srv.messages(1)[0]["payload"]["device_credential"].endswith("b" * 30)


def test_waiting_on_a_closed_link_says_so():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.aclose()
            with pytest.raises(LinkClosedError):
                await link.wait_connected(1)

    run_async(scenario())
