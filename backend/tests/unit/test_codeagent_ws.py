"""S04 (device socket, WS /v1/code/device): the handshake, authentication for
the life of the connection, validation, limits, and the protocol fixtures.

The real endpoint (codeagent/ws.py), the real registry (codeagent/connections.py),
the real eo/devices.py logic and the real protocol/v1 schemas run here; only the
device table is faked (devices_testkit.FakeStore, via codeagent_testkit.WsFakeStore).
Every frame the server sends is also validated against protocol/v1, strictly, so
the server cannot drift from the contract it is built against.

Maps to the work package's "Done when":
  * tests replay the protocol fixtures against a fake device ..... test_replay_fixture_*
  * an unknown, malformed or revoked credential is refused ........ test_unknown_*, test_malformed_*,
                                                                    test_revoked_credential_*
  * a device revoked while connected is disconnected at the next
    re-check ...................................................... test_device_revoked_while_connected_*
  * messages that fail schema validation get invalid_message and
    do not crash the socket ...................................... test_invalid_*, test_every_invalid_example_*

Skipped as a module when jsonschema is not installed. A backend install has it
transitively (guardrails-ai, litellm) though requirements.txt does not pin it -- see
codeagent/ws.py, "Runtime needs". The endpoint fails closed without it, which
test_codeagent_ws_failclosed.py checks and which does not need it.
"""
import json
import logging
import time

import jwt
import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed (a backend install gets it via guardrails-ai); "
           "install protocol/requirements.txt to run the device-socket tests "
           "(see codeagent/ws.py, 'Runtime needs')",
)

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import deps  # noqa: E402
from codeagent import connections, ws as ws_module  # noqa: E402
from eo import devices  # noqa: E402
from tests.unit.codeagent_testkit import (  # noqa: E402
    CLIENT_TYPES,
    FIXTURE_CREDENTIAL,
    FIXTURES,
    Rig,
    ScriptedHandler,
    envelope,
    hello,
    load_jsonl,
    ping,
    user_message,
)
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET  # noqa: E402

PROTOCOL = ws_module.load_protocol()      # the production loader: the same reader the server uses


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


@pytest.fixture
def rig(monkeypatch):
    r = Rig(monkeypatch)
    r.check = assert_server_frame_is_valid
    return r


@pytest.fixture
def device(rig):
    """(credential, device_id) of a live device owned by user-a."""
    return rig.store.add_device("user-a")


def assert_server_frame_is_valid(msg):
    """Whatever the server sends must satisfy protocol/v1, with the strict reader
    (the tolerant one is for what the server RECEIVES)."""
    issues = PROTOCOL.validate_message(msg)
    assert issues == [], [str(i) for i in issues]


def welcome_of(rig, credential, **hello_kwargs):
    with rig.session(credential, **hello_kwargs) as (_dev, welcome):
        return welcome


# ============================================================ handshake: welcome

def test_hello_is_answered_with_a_valid_welcome(rig, device):
    credential, _ = device
    with rig.session(credential) as (_dev, welcome):
        assert_server_frame_is_valid(welcome)
        p = welcome["payload"]
        assert p["protocol"] == 1 and p["min_protocol"] == 1
        assert p["limits"]["max_parallel_runs"] >= 1
        assert p["limits"]["max_message_bytes"] == ws_module.SETTINGS.max_message_bytes
        assert p["server_version"]
        assert "run_id" not in welcome and "seq" not in welcome


def test_welcome_ttl_is_the_device_jwt_ttl_and_is_only_advertised(rig, device):
    """token_ttl_s is S03's DEVICE_JWT_TTL_SECONDS: the advertised authentication
    lifetime. It is informational; the periodic re-check is what enforces revocation
    (the connection is NOT dropped when the advertised time passes)."""
    credential, _ = device
    assert welcome_of(rig, credential)["payload"]["token_ttl_s"] == devices.DEVICE_JWT_TTL_SECONDS
    rig.configure(revalidate_interval_s=60.0)
    with rig.session(credential) as (dev, welcome):
        assert welcome["payload"]["token_ttl_s"] == 900
        dev.ping_pong()                                   # still connected, nothing refreshed


def test_server_picks_the_highest_common_version(rig, device):
    credential, _ = device
    for versions in ([1], [1, 2, 7], [9, 1], [7, 1, 2]):
        assert welcome_of(rig, credential, versions=versions)["payload"]["protocol"] == 1


def test_no_common_version_is_protocol_unsupported_in_v1_shape(rig, device):
    credential, _ = device
    with rig.connect() as dev:
        dev.send(hello(credential, versions=[2, 3]))
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["t"] == "error"
        assert reply["payload"]["code"] == "protocol_unsupported"
        assert reply["payload"]["details"] == {"min_protocol": 1, "max_protocol": 1}
        assert dev.expect_close() == ws_module.CLOSE_PROTOCOL_UNSUPPORTED
    assert rig.store.touch_calls == 0                    # a stale client never costs a lookup
    assert rig.limiter.retry_after("203.0.113.7") == 0   # and is not counted as an attack


def test_hello_envelope_is_always_v1_so_a_v2_envelope_is_just_invalid(rig, device):
    credential, _ = device
    msg = hello(credential)
    msg["v"] = 2
    with rig.connect() as dev:
        dev.send(msg)
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_BAD_HANDSHAKE


# ============================================================ handshake: credentials

def test_valid_credential_registers_the_device_and_stamps_last_seen(rig, device):
    credential, device_id = device
    assert rig.store.devices[device_id]["last_seen_at"] is None
    with rig.session(credential, client_version="9.9.9-test", capabilities=["acp"]):
        assert rig.store.devices[device_id]["last_seen_at"] == rig.store_clock.t
        conn = rig.registry.get("user-a", device_id)
        assert conn is not None and conn.user_id == "user-a" and conn.device_id == device_id
        assert conn.protocol == 1 and conn.client_version == "9.9.9-test"
        assert conn.capabilities == ("acp",)
    assert rig.registry.get("user-a", device_id) is None   # gone after disconnect


def test_unknown_credential_is_unauthorized(rig):
    with rig.connect() as dev:
        dev.send(hello("a" * 43))                         # right shape, nobody's credential
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["payload"]["code"] == "unauthorized"
        assert reply["payload"]["ref_id"]
        assert dev.expect_close() == ws_module.CLOSE_UNAUTHORIZED
    assert rig.registry.count() == 0


@pytest.mark.parametrize("credential", [
    "x" * 16,                       # passes hello.json's pattern, fails eo.devices' shape (min 20)
    "has.a.dot." + "a" * 20,        # dots are legal in hello.json, never in a device credential
    "a" * 200,                      # longer than any credential S03 mints
    "+/=" * 10,
])
def test_malformed_credential_is_unauthorized_without_a_lookup(rig, credential):
    with rig.connect() as dev:
        dev.send(hello(credential))
        assert dev.recv_error()["code"] == "unauthorized"
        assert dev.expect_close() == ws_module.CLOSE_UNAUTHORIZED
    assert rig.store.touch_calls == 0


def test_credential_that_fails_the_hello_schema_is_invalid_message(rig):
    with rig.connect() as dev:
        dev.send(hello("short"))                          # < 16 characters: hello.json's pattern
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_BAD_HANDSHAKE


def test_revoked_credential_is_device_revoked(rig, device):
    credential, device_id = device
    devices.revoke_device("user-a", device_id)
    with rig.connect() as dev:
        dev.send(hello(credential))
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["payload"]["code"] == "device_revoked"
        assert dev.expect_close() == ws_module.CLOSE_DEVICE_REVOKED
    assert rig.registry.count() == 0
    assert rig.store.devices[device_id]["last_seen_at"] is None   # a revoked device is not stamped


def test_a_supabase_user_token_is_never_accepted_as_a_credential(rig, device):
    """A user's access token is not a device credential, even from a user who owns devices."""
    token = jwt.encode({"sub": "user-a", "aud": "authenticated", "exp": int(time.time()) + 600,
                        "iat": int(time.time())}, SUPABASE_SECRET, algorithm="HS256")
    with rig.connect() as dev:
        dev.send(hello(token))
        assert dev.recv_error()["code"] == "unauthorized"
        assert dev.expect_close() == ws_module.CLOSE_UNAUTHORIZED
    assert rig.registry.count() == 0


def test_a_device_jwt_is_not_a_credential_either(rig, device):
    credential, _ = device
    token = jwt.encode({"sub": "user-a", "device_id": device[1], "token_type": "device",
                        "aud": devices.DEVICE_JWT_AUDIENCE, "iat": int(time.time()),
                        "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")
    with rig.connect() as dev:
        dev.send(hello(token))
        assert dev.recv_error()["code"] == "unauthorized"
        assert dev.expect_close() == ws_module.CLOSE_UNAUTHORIZED


def test_require_auth_accepts_neither_a_device_jwt_nor_a_device_credential(monkeypatch, rig, device):
    """The other direction: the web API's auth never takes what the socket takes.
    The positive control proves the check would have let a real user token in."""
    monkeypatch.setattr(deps, "SUPABASE_JWT_SECRET", SUPABASE_SECRET)
    app = FastAPI()

    @app.get("/whoami")
    def whoami(user_id: str = Depends(deps.require_auth)):
        return {"user_id": user_id}

    client = TestClient(app)
    credential, device_id = device
    now = int(time.time())
    device_jwt = jwt.encode({"sub": "user-a", "device_id": device_id, "token_type": "device",
                             "aud": devices.DEVICE_JWT_AUDIENCE, "iat": now, "exp": now + 600},
                            SECRET, algorithm="HS256")
    user_jwt = jwt.encode({"sub": "user-a", "aud": "authenticated", "iat": now, "exp": now + 600},
                          SUPABASE_SECRET, algorithm="HS256")
    for bearer in (device_jwt, credential):
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bearer}"}).status_code == 401
    ok = client.get("/whoami", headers={"Authorization": f"Bearer {user_jwt}"})
    assert ok.status_code == 200 and ok.json() == {"user_id": "user-a"}


def test_store_failure_during_authentication_fails_closed_as_internal(rig, device):
    credential, _ = device
    rig.store.fail_touch = RuntimeError("CANARY-db-password=hunter2")
    with rig.connect() as dev:
        dev.send(hello(credential))
        reply = dev.recv_error()
        assert reply["code"] == "internal"
        assert "CANARY" not in json.dumps(reply)
        assert dev.expect_close() == ws_module.CLOSE_INTERNAL
    assert rig.registry.count() == 0


def test_revoked_lookup_failure_fails_closed_too(rig):
    rig.store.fail_revoked_lookup = RuntimeError("boom")
    with rig.connect() as dev:
        dev.send(hello("b" * 43))
        assert dev.recv_error()["code"] == "internal"
        assert dev.expect_close() == ws_module.CLOSE_INTERNAL


# ============================================================ handshake: first message rules

@pytest.mark.parametrize("first", [
    ping("p"),
    user_message("hi"),
    envelope("cancel", {}, run_id="run_testkit_0001"),
])
def test_first_message_must_be_hello(rig, device, first):
    with rig.connect() as dev:
        dev.send(first)
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["payload"]["code"] == "invalid_message"
        assert reply["payload"]["ref_id"] == first["id"]
        assert dev.expect_close() == ws_module.CLOSE_BAD_HANDSHAKE


@pytest.mark.parametrize("raw", ["not json at all", "[1, 2, 3]", "null", "42", '"hello"', "{"])
def test_garbage_first_frame_is_invalid_message(rig, raw):
    with rig.connect() as dev:
        dev.send_text(raw)
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_BAD_HANDSHAKE


def test_binary_first_frame_is_invalid_message(rig):
    with rig.connect() as dev:
        dev.send_bytes(b"\x00\x01\x02")
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_BAD_HANDSHAKE


def test_hello_must_arrive_within_the_timeout(monkeypatch):
    rig = Rig(monkeypatch, hello_timeout_s=0.2)
    rig.check = assert_server_frame_is_valid
    started = time.monotonic()
    with rig.connect() as dev:
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["payload"]["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_HANDSHAKE_TIMEOUT
    assert time.monotonic() - started < 5
    assert rig.store.touch_calls == 0


def test_default_hello_timeout_is_short():
    assert 0 < ws_module.SETTINGS.hello_timeout_s <= 15


def test_oversized_first_message_is_refused_before_it_is_parsed(monkeypatch):
    rig = Rig(monkeypatch, max_message_bytes=2048)
    rig.check = assert_server_frame_is_valid
    with rig.connect() as dev:
        dev.send_text(json.dumps(hello("a" * 43)) + " " * 4096)
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_TOO_BIG
    assert rig.store.touch_calls == 0


def test_a_second_hello_is_invalid_and_the_credential_is_not_re_read(rig, device):
    credential, _ = device
    with rig.session(credential) as (dev, _welcome):
        before = rig.store.touch_calls
        again = hello(credential)
        dev.send(again)
        err = dev.recv_error()
        assert err["code"] == "invalid_message" and err["ref_id"] == again["id"]
        dev.ping_pong()                                   # the socket is fine
    assert rig.store.touch_calls == before


# ============================================================ failed-handshake rate limit

def test_repeated_failed_handshakes_from_one_address_are_rate_limited(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    rig.limiter.limit = 3
    for _ in range(3):
        with rig.connect() as dev:
            dev.send(hello("c" * 43))
            assert dev.recv_error()["code"] == "unauthorized"
            dev.expect_close()
    lookups = rig.store.touch_calls
    with rig.connect() as dev:                            # fourth: refused up front
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["payload"]["code"] == "rate_limited"
        assert 1 <= reply["payload"]["details"]["retry_after_s"] <= 60
        assert dev.expect_close() == ws_module.CLOSE_RATE_LIMITED
    assert rig.store.touch_calls == lookups               # and it cost no credential lookup


def test_a_good_device_at_a_limited_address_is_refused_then_welcome_after_the_window(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    rig.limiter.limit = 2
    credential, _ = rig.store.add_device("user-a")
    for _ in range(2):
        with rig.connect() as dev:
            dev.send(hello("d" * 43))
            dev.recv()
            dev.expect_close()
    with rig.connect() as dev:
        assert dev.recv_error()["code"] == "rate_limited"
        dev.expect_close()
    rig.limiter_clock.advance(61)
    with rig.session(credential) as (dev, _):
        dev.ping_pong()


def test_the_limit_is_per_address_and_ignores_forwarded_headers(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    rig.limiter.limit = 2
    for _ in range(2):
        with rig.connect() as dev:
            dev.send(hello("e" * 43))
            dev.recv()
            dev.expect_close()
    # Another peer is not limited ...
    other = rig.client_from(("198.51.100.9", 50000))
    with rig.connect(other) as dev:
        dev.send(hello("e" * 43))
        assert dev.recv_error()["code"] == "unauthorized"
        dev.expect_close()
    # ... and a header the client controls does not buy a fresh budget.
    spoofing = TestClient(rig.app, client=("203.0.113.7", 40001),
                          headers={"X-Forwarded-For": "192.0.2.77"})
    with rig.connect(spoofing) as dev:
        assert dev.recv_error()["code"] == "rate_limited"
        dev.expect_close()


def test_revoked_and_unsupported_handshakes_do_not_count_as_attacks(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    rig.limiter.limit = 2
    credential, device_id = rig.store.add_device("user-a")
    devices.revoke_device("user-a", device_id)
    for _ in range(5):
        with rig.connect() as dev:                        # a revoked device that keeps retrying
            dev.send(hello(credential))
            assert dev.recv_error()["code"] == "device_revoked"
            dev.expect_close()
        with rig.connect() as dev:                        # an old client that keeps retrying
            dev.send(hello(credential, versions=[5]))
            assert dev.recv_error()["code"] == "protocol_unsupported"
            dev.expect_close()
    assert rig.limiter.retry_after("203.0.113.7") == 0


def test_successful_handshakes_are_never_limited(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    credential, _ = rig.store.add_device("user-a")
    for _ in range(25):
        with rig.session(credential):
            pass


def test_malformed_hello_and_timeouts_count_against_the_address(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    rig.limiter.limit = 2
    with rig.connect() as dev:
        dev.send_text("garbage")
        dev.recv()
        dev.expect_close()
    rig.configure(hello_timeout_s=0.1)
    with rig.connect() as dev:
        dev.recv()
        dev.expect_close()
    with rig.connect() as dev:
        assert dev.recv_error()["code"] == "rate_limited"
        dev.expect_close()


# ============================================================ re-validation (revocation)

def test_default_recheck_interval_is_at_most_30_seconds():
    assert ws_module.REVALIDATE_INTERVAL_SECONDS <= 30
    assert ws_module.SETTINGS.revalidate_interval_s == ws_module.REVALIDATE_INTERVAL_SECONDS


def test_device_revoked_while_connected_is_disconnected_at_the_next_recheck(monkeypatch):
    rig = Rig(monkeypatch, revalidate_interval_s=0.05)
    rig.check = assert_server_frame_is_valid
    credential, device_id = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        dev.ping_pong()
        devices.revoke_device("user-a", device_id)        # the web app's revoke button
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["t"] == "error" and reply["payload"]["code"] == "device_revoked"
        assert "ref_id" not in reply["payload"]
        assert dev.expect_close() == ws_module.CLOSE_DEVICE_REVOKED
    assert rig.registry.count() == 0


def test_an_active_device_survives_many_rechecks(monkeypatch):
    rig = Rig(monkeypatch, revalidate_interval_s=0.03)
    rig.check = assert_server_frame_is_valid
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _):
        time.sleep(0.4)
        dev.ping_pong()
    assert rig.store.active_calls >= 3                    # it really did keep checking


def test_revoking_one_device_leaves_the_users_other_device_connected(monkeypatch):
    rig = Rig(monkeypatch, revalidate_interval_s=0.05)
    rig.check = assert_server_frame_is_valid
    cred_a, id_a = rig.store.add_device("user-a", name="laptop")
    cred_b, _id_b = rig.store.add_device("user-a", name="desktop")
    with rig.session(cred_a) as (dev_a, _), rig.session(cred_b) as (dev_b, _):
        devices.revoke_device("user-a", id_a)
        assert dev_a.recv_error()["code"] == "device_revoked"
        dev_a.expect_close()
        time.sleep(0.2)
        dev_b.ping_pong()


def test_the_recheck_failing_for_infrastructure_reasons_fails_closed_but_is_not_a_revocation(monkeypatch):
    """A database fault must not be reported as 'revoked' (the client would tell the
    user to log in again), but it must not leave the device authenticated either."""
    rig = Rig(monkeypatch, revalidate_interval_s=0.05)
    rig.check = assert_server_frame_is_valid
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _):
        rig.store.fail_is_active = RuntimeError("CANARY-connection-refused host=10.0.0.5")
        reply = dev.recv_error()
        assert reply["code"] == "internal"
        assert "CANARY" not in json.dumps(reply)
        assert dev.expect_close() == ws_module.CLOSE_INTERNAL
    assert rig.registry.count() == 0


def test_the_recheck_checks_this_user_and_this_device(monkeypatch):
    rig = Rig(monkeypatch, revalidate_interval_s=0.05)
    rig.check = assert_server_frame_is_valid
    credential, device_id = rig.store.add_device("user-a")
    seen = []
    real = rig.store.is_active

    def spy(*, user_id, device_id):
        seen.append((user_id, device_id))
        return real(user_id=user_id, device_id=device_id)

    rig.store.is_active = spy
    with rig.session(credential):
        time.sleep(0.2)
    assert seen and set(seen) == {("user-a", device_id)}


# ============================================================ registry

def test_registry_tracks_devices_per_user(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    cred_a1, id_a1 = rig.store.add_device("user-a", name="a1")
    cred_a2, id_a2 = rig.store.add_device("user-a", name="a2")
    cred_b, id_b = rig.store.add_device("user-b", name="b1")
    with rig.session(cred_a1), rig.session(cred_a2), rig.session(cred_b):
        assert {c.device_id for c in rig.registry.for_user("user-a")} == {id_a1, id_a2}
        assert [c.device_id for c in rig.registry.for_user("user-b")] == [id_b]
        assert rig.registry.count() == 3
        assert rig.registry.get("user-a", id_b) is None     # a device id is scoped to its user
    assert rig.registry.count() == 0


def test_a_new_connection_from_the_same_device_supersedes_the_old_one(monkeypatch):
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    credential, device_id = rig.store.add_device("user-a")
    with rig.session(credential) as (first, _):
        with rig.session(credential) as (second, _):
            assert first.expect_close() == ws_module.CLOSE_SUPERSEDED
            second.ping_pong()                            # the newcomer is untouched
            assert rig.registry.count() == 1
            assert rig.registry.get("user-a", device_id) is not None
    assert rig.registry.count() == 0               # and the old socket's cleanup removed nobody else


# ============================================================ messages after the handshake

def test_ping_is_answered_with_a_pong_that_echoes_the_nonce(rig, device):
    credential, _ = device
    with rig.session(credential) as (dev, _):
        msg = ping("n-1")
        dev.send(msg)
        pong = dev.recv()
        assert_server_frame_is_valid(pong)
        assert pong["t"] == "pong" and pong["payload"] == {"nonce": "n-1"}
        assert pong["id"] != msg["id"]
        dev.send(ping())
        bare = dev.recv()
        assert bare["t"] == "pong" and bare["payload"] == {}


def test_run_messages_are_handed_to_the_installed_handler_in_order(rig, device):
    credential, device_id = device
    handler = rig.use_handler(ScriptedHandler())
    msgs = [user_message(f"m{i}") for i in range(5)]
    with rig.session(credential) as (dev, _):
        for m in msgs:
            dev.send(m)
        dev.ping_pong()                                   # a barrier: everything before it was handled
    assert [m["id"] for m in handler.received] == [m["id"] for m in msgs]
    assert handler.disconnected == [("user-a", device_id)]


def test_without_a_run_manager_run_messages_get_a_clear_internal_error(rig, device):
    credential, _ = device
    with rig.session(credential) as (dev, _):
        msg = user_message("hi")
        dev.send(msg)
        reply = dev.recv()
        assert_server_frame_is_valid(reply)
        assert reply["payload"]["code"] == "internal" and reply["payload"]["ref_id"] == msg["id"]
        dev.ping_pong()


def test_a_handler_that_raises_does_not_take_the_socket_down(rig, device, caplog):
    credential, _ = device
    rig.use_handler(ScriptedHandler(fail_on="user_message"))
    caplog.set_level(logging.DEBUG)
    with rig.session(credential) as (dev, _):
        msg = user_message("hi")
        dev.send(msg)
        err = dev.recv_error()
        assert err["code"] == "internal" and err["ref_id"] == msg["id"]
        assert "CANARY" not in json.dumps(err)
        dev.ping_pong()
    assert "CANARY" not in caplog.text                    # an exception's text can carry content


@pytest.mark.parametrize("raw", ["not json", "[]", "null", "12", '"x"', "{"])
def test_garbage_after_the_handshake_is_invalid_message_and_the_socket_lives(rig, device, raw):
    credential, _ = device
    with rig.session(credential) as (dev, _):
        dev.send_text(raw)
        assert dev.recv_error()["code"] == "invalid_message"
        dev.ping_pong()


def test_binary_frames_after_the_handshake_are_invalid_message(rig, device):
    credential, _ = device
    with rig.session(credential) as (dev, _):
        dev.send_bytes(b"\x01\x02")
        assert dev.recv_error()["code"] == "invalid_message"
        dev.ping_pong()


def test_the_server_types_are_not_something_a_device_may_send(rig, device):
    credential, _ = device
    first_of_each = {}                                   # one per type: the streak limit is a different test
    for name in ("01_plain_chat", "02_edit_with_approval", "04_quota_error"):
        for m in load_jsonl(FIXTURES / f"{name}.jsonl"):
            if m["t"] not in CLIENT_TYPES:
                first_of_each.setdefault(m["t"], m)
    sent = list(first_of_each.values())
    assert {m["t"] for m in sent} >= {"welcome", "run_event", "tool_proposed", "budget_warning",
                                       "error", "pong"}
    with rig.session(credential) as (dev, _):
        for m in sent:
            dev.send(m)
            err = dev.recv_error()
            assert err["code"] == "invalid_message" and err["ref_id"] == m["id"]
        dev.ping_pong()


def test_an_unknown_message_type_is_invalid_message(rig, device):
    credential, _ = device
    with rig.session(credential) as (dev, _):
        msg = envelope("token.refresh", {})
        dev.send(msg)
        err = dev.recv_error()
        assert err["code"] == "invalid_message" and err["ref_id"] == msg["id"]


def test_there_is_no_token_refresh_message(rig):
    """P00 deliberately left it out; S04 must not add one (protocol/v1 is unchanged)."""
    envelope_schema = json.loads((FIXTURES.parent / "v1" / "envelope.json").read_text("utf-8"))
    assert not [t for t in envelope_schema["properties"]["t"]["enum"] if "refresh" in t or "token" in t]


def test_ref_id_is_left_out_when_the_offending_id_is_not_a_uuid(rig, device):
    credential, _ = device
    with rig.session(credential) as (dev, _):
        msg = ping("n")
        msg["id"] = "not-a-uuid"
        dev.send(msg)
        err = dev.recv_error()
        assert err["code"] == "invalid_message" and "ref_id" not in err
        dev.ping_pong()


def _tolerated(case):
    """An example whose only defect is an unknown optional property outside tool
    input: the tolerant reader (README section 8) deliberately lets it through."""
    return (case["expect"]["validator"] == "additionalProperties"
            and not case["expect"]["path"].startswith("payload/input")
            and case["message"]["t"] in CLIENT_TYPES)


INVALID = load_jsonl(FIXTURES / "invalid_examples.jsonl")


@pytest.mark.parametrize("case", INVALID, ids=[c["case"] for c in INVALID])
def test_every_invalid_example_is_rejected_without_crashing_the_socket(rig, device, case):
    credential, _ = device
    handler = rig.use_handler(ScriptedHandler())
    msg = case["message"]
    with rig.session(credential) as (dev, _):
        dev.send(msg)
        if _tolerated(case):                              # accepted: unknown optional fields are ignored
            if msg["t"] == "ping":
                assert dev.recv()["t"] == "pong"          # the socket answers pings itself
                dev.ping_pong()
                assert handler.received == []
            else:
                dev.ping_pong()                           # a barrier: the handler has run by now
                assert len(handler.received) == 1
        else:
            assert dev.recv_error()["code"] == "invalid_message"
            dev.ping_pong()
            assert handler.received == []                 # nothing invalid reaches the run manager


def test_too_many_invalid_messages_in_a_row_close_the_connection(monkeypatch):
    rig = Rig(monkeypatch, max_consecutive_invalid=3)
    rig.check = assert_server_frame_is_valid
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _):
        for _ in range(2):
            dev.send_text("garbage")
            assert dev.recv_error()["code"] == "invalid_message"
        dev.ping_pong()                                   # a valid message resets the streak
        for _ in range(3):
            dev.send_text("garbage")
            assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_POLICY


def test_messages_over_the_advertised_limit_are_refused_and_the_socket_closes(monkeypatch):
    rig = Rig(monkeypatch, max_message_bytes=2048)
    rig.check = assert_server_frame_is_valid
    credential, _ = rig.store.add_device("user-a")
    handler = rig.use_handler(ScriptedHandler())
    with rig.session(credential) as (dev, welcome):
        assert welcome["payload"]["limits"]["max_message_bytes"] == 2048
        dev.send(user_message("x" * 5000))
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_TOO_BIG
    assert handler.received == []


def test_the_limit_counts_bytes_not_characters_and_is_inclusive(monkeypatch):
    rig = Rig(monkeypatch, max_message_bytes=2048)
    rig.check = assert_server_frame_is_valid
    credential, _ = rig.store.add_device("user-a")
    handler = rig.use_handler(ScriptedHandler())

    msg = user_message("a" * 1500)
    raw = json.dumps(msg)
    exact = raw + " " * (2048 - len(raw.encode("utf-8")))   # trailing whitespace is legal JSON
    assert len(exact.encode("utf-8")) == 2048
    with rig.session(credential) as (dev, _):
        dev.send_text(exact)                              # exactly the limit: accepted
        dev.ping_pong()
    assert [m["id"] for m in handler.received] == [msg["id"]]

    with rig.session(credential) as (dev, _):
        dev.send_text(exact + " ")                        # one byte over: refused
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_TOO_BIG

    multibyte = json.dumps(user_message("\u00e9" * 1100), ensure_ascii=False)   # 1100 characters, > 2048 bytes
    assert len(multibyte) < 2048 < len(multibyte.encode("utf-8"))
    with rig.session(credential) as (dev, _):
        dev.send_text(multibyte)
        assert dev.recv_error()["code"] == "invalid_message"
        assert dev.expect_close() == ws_module.CLOSE_TOO_BIG


def test_a_large_valid_message_below_the_limit_is_validated_and_delivered(rig, device):
    credential, _ = device
    handler = rig.use_handler(ScriptedHandler())
    big = user_message("y" * 190_000)                     # under bulk_text's 200000 and the 1 MiB default
    with rig.session(credential) as (dev, _):
        dev.send(big)
        dev.ping_pong()
    assert handler.received[0]["payload"]["text"] == "y" * 190_000


# ============================================================ fixtures (golden transcripts)

SERVER_SELF_TYPES = {"welcome", "pong"}            # answered by the socket itself, not a run manager


def _connections(lines):
    conns, cur = [], None
    for m in lines:
        if m["t"] == "hello":
            cur = []
            conns.append(cur)
        cur.append(m)
    return conns


def _steps(conn_lines):
    """[(client message, [expected server lines])] for one connection."""
    steps = []
    for m in conn_lines:
        if m["t"] in CLIENT_TYPES:
            steps.append((m, []))
        else:
            steps[-1][1].append(m)
    return steps


def _comparable(m):
    out = {"t": m["t"], "payload": dict(m["payload"])}
    for k in ("run_id", "seq"):
        if k in m:
            out[k] = m[k]
    if m["t"] == "welcome":
        out["payload"].pop("server_version", None)    # the server states its own
    return out


@pytest.mark.parametrize("name", ["01_plain_chat", "02_edit_with_approval",
                                  "03_disconnect_resume", "04_quota_error"])
def test_replay_fixture_against_a_fake_device(monkeypatch, name):
    """The README's server-test recipe: feed a transcript's client lines to the socket in
    order and compare what comes back. hello/welcome and ping/pong are the socket's own
    work; everything else the transcript says in reply is played by a stand-in run
    manager, which proves the socket delivers each client message to it unchanged and
    carries each server line to the device verbatim."""
    lines = load_jsonl(FIXTURES / f"{name}.jsonl")
    rig = Rig(monkeypatch)
    rig.check = assert_server_frame_is_valid
    rig.store.add_device("fixture-user", credential=FIXTURE_CREDENTIAL)
    replies = {m["id"]: [] for m in lines if m["t"] in CLIENT_TYPES}
    delivered = []
    for conn in _connections(lines):
        for client_msg, expected in _steps(conn):
            replies[client_msg["id"]] = [e for e in expected if e["t"] not in SERVER_SELF_TYPES]
            delivered.append(client_msg)
    handler = rig.use_handler(ScriptedHandler(replies))

    for conn in _connections(lines):
        with rig.connect() as dev:
            for client_msg, expected in _steps(conn):
                dev.send(client_msg)
                for want in expected:
                    got = dev.recv()
                    assert_server_frame_is_valid(got)
                    assert _comparable(got) == _comparable(want), (client_msg["t"], got, want)
    run_side = [m for m in delivered if m["t"] not in {"hello", "ping"}]
    assert [m["id"] for m in handler.received] == [m["id"] for m in run_side]
    assert handler.received == run_side                   # unchanged, in order, across connections
    assert len(handler.disconnected) == len(_connections(lines))


def test_every_client_message_type_reaches_the_handler_unchanged(rig, device):
    """The fixtures do not use every type (no cancel, no tool_stream), so say it directly:
    hello and ping are the socket's own; each of the other seven is delivered as sent."""
    credential, _ = device
    handler = rig.use_handler(ScriptedHandler())
    run = "run_testkit_0001"
    sent = [
        envelope("run.start", {"profile": "code", "mode": "plan", "cwd_label": "proj",
                               "instructions": "", "rules": [], "memory_index": "",
                               "tool_catalog_version": "v0", "model_pref": "auto"}, run_id=run),
        user_message("hi", run),
        envelope("tool_result", {"call_id": "c1", "ok": True, "output": "x", "truncated": False,
                                 "trust": "local_project"}, run_id=run),
        envelope("tool_stream", {"call_id": "c1", "stream": "stdout", "chunk": "line\n"}, run_id=run),
        envelope("approval", {"call_id": "c1", "decision": "deny", "scope": "once"}, run_id=run),
        envelope("cancel", {}, run_id=run),
        envelope("run.resume", {"last_seq": 0}, run_id=run),
    ]
    assert {m["t"] for m in sent} == CLIENT_TYPES - {"hello", "ping"}
    with rig.session(credential) as (dev, _):
        for m in sent:
            dev.send(m)
        dev.ping_pong()
    assert handler.received == sent


# ============================================================ logging: no content

def test_no_message_content_or_credential_is_logged(monkeypatch, caplog):
    """Log device id, user id, message type and error code only. A canary in every
    place content could ride -- the credential, run text, tool output, a bad message,
    an exception raised while handling one -- must never reach a log record."""
    rig = Rig(monkeypatch, max_message_bytes=4096)
    rig.check = assert_server_frame_is_valid
    credential, device_id = rig.store.add_device("user-a")
    rig.use_handler(ScriptedHandler(fail_on="tool_result"))
    caplog.set_level(logging.DEBUG)
    canary = "CANARY-SECRET-CONTENT-7f3a"

    with rig.connect() as dev:                            # a failed handshake carrying a canary credential
        dev.send(hello(canary + "x" * 20))
        dev.recv()
        dev.expect_close()
    with rig.session(credential) as (dev, _):
        dev.send(user_message(f"please read /home/me/{canary}/id_rsa"))
        dev.send(envelope("tool_result", {"call_id": "c1", "ok": True, "output": canary,
                                          "truncated": False, "trust": "local_project"},
                          run_id="run_testkit_0001"))
        dev.recv()                                        # the internal error for the raising handler
        dev.send(envelope("run.start", {"profile": "code", "mode": "ask", "cwd_label": "p",
                                        "instructions": canary, "rules": [], "memory_index": "",
                                        "tool_catalog_version": "v0", "model_pref": "auto",
                                        "bogus_field_" + canary: 1}, run_id="run_testkit_0001"))
        dev.send_text(json.dumps({"t": "user_message", "text": canary}))      # schema-invalid, with content
        dev.recv()
        dev.send_text(f'{{"truncated json {canary}')
        dev.recv()
        dev.send_text("x" * 5000 + canary)                # over the limit
        dev.recv()
        dev.expect_close()
    assert canary not in caplog.text
    assert credential not in caplog.text
    assert device_id in caplog.text                       # what IS logged: ids ...
    assert "user-a" in caplog.text
