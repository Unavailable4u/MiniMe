"""S03 route-level tests: device pairing, credentials and the device token.

Everything goes through the real routes (api/routes/devices.py) and the real
logic (eo/devices.py); only the two stores are faked -- Postgres by
devices_testkit.FakeStore, Redis by conftest's autouse FakeRedis (or the
TTL-honouring TTLRedis where a window has to elapse). The real SQL, grants
and RLS are covered by tests/integration/test_devices_pg.py.

Maps to the work package's "Done when":
  * the full flow works against the test client ......... test_full_pairing_flow_*
  * a revoked device can no longer get a token ........... test_revoked_device_*
  * credentials never appear in logs ..................... test_no_secret_*
  * a replayed device_code after approval fails .......... test_replayed_device_code_*
"""
import logging
import threading
import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eo import devices
from tests.unit.devices_testkit import (
    SECRET,
    SUPABASE_SECRET,
    VERIFY_URL,
    BrokenRedis,
    Clock,
    FakeStore,
    Harness,
    TTLRedis,
    sha256,
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)
    monkeypatch.setenv("DEVICE_VERIFICATION_URL", VERIFY_URL)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(monkeypatch, clock):
    fake = FakeStore(clock)
    monkeypatch.setattr(devices, "_STORE", fake)
    return fake


@pytest.fixture
def ttl_redis(monkeypatch, clock):
    """Swap in a Redis whose keys really expire against `clock`."""
    from memory import bus
    redis = TTLRedis(clock)
    monkeypatch.setattr(bus, "redis", redis)
    return redis


@pytest.fixture
def h(store):
    return Harness()


# ============================================================ full flow

def test_full_pairing_flow_on_the_shared_fake_redis(h, store):
    """start -> approve -> poll -> token -> list -> revoke, using only the
    repo's shared FakeRedis (no TTLs, no decr/nx): the module must not need
    anything fancier from Redis than get/incr/expire."""
    start = h.start(name="alice-laptop", os="Ubuntu 24.04", version="0.1.0")
    assert start.status_code == 200
    body = start.json()
    assert set(body) == {"device_code", "user_code", "verification_url", "interval", "expires_in"}
    assert body["verification_url"] == VERIFY_URL
    assert (body["interval"], body["expires_in"]) == (5, 600)

    # Before approval the terminal just waits.
    assert h.poll(body["device_code"]).json() == {"status": "pending"}

    approved = h.approve(body["user_code"], user="user-a")
    assert approved.status_code == 200
    assert approved.json() == {"status": "approved", "device_name": "alice-laptop",
                               "os": "Ubuntu 24.04", "client_version": "0.1.0"}

    polled = h.poll(body["device_code"])
    assert polled.status_code == 200
    data = polled.json()
    assert set(data) == {"status", "device_credential", "device_id"}
    assert data["status"] == "approved"

    token = h.token(data["device_credential"])
    assert token.status_code == 200
    claims = jwt.decode(token.json()["jwt"], SECRET, algorithms=["HS256"], audience="m2c-device")
    assert (claims["sub"], claims["device_id"]) == ("user-a", data["device_id"])

    listing = h.list("user-a").json()["devices"]
    assert [d["id"] for d in listing] == [data["device_id"]]
    assert listing[0]["device_name"] == "alice-laptop"
    assert listing[0]["last_seen_at"] is not None            # stamped by the token exchange

    assert h.revoke(data["device_id"]).status_code == 200
    assert h.list("user-a").json() == {"devices": []}
    assert h.token(data["device_credential"]).status_code == 401


def test_responses_carrying_secrets_are_not_cacheable(h):
    cred, _, _ = h.pair()
    fresh = h.start()
    for resp in (fresh, h.poll(fresh.json()["device_code"]), h.token(cred)):    # pending poll, then token
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
    start = h.start().json()
    h.approve(start["user_code"])
    assert h.poll(start["device_code"]).headers["cache-control"] == "no-store"   # the one carrying the credential


# ============================================================ pair/start

def test_start_validates_the_three_labels(h):
    assert h.start(name="").status_code == 422
    assert h.start(name="   ").status_code == 422
    assert h.start(name="x" * 65).status_code == 422
    assert h.start(os="y" * 65).status_code == 422
    assert h.start(version="z" * 33).status_code == 422
    assert h.start(name="bad\nname").status_code == 422
    assert h.start(name="tab\there").status_code == 422
    assert h.client.post("/v1/code/devices/pair/start", json={"device_name": "only-one"}).status_code == 422
    assert h.start(name="x" * 64, os="y" * 64, version="z" * 32).status_code == 200


def test_start_stores_the_labels_only_and_ignores_extra_fields(h, store):
    resp = h.start(name="  trimmed-name  ", prompt="write me a poem", cwd="/home/alice/secret-project")
    assert resp.status_code == 200
    (row,) = store.pairings.values()
    assert row["device_name"] == "trimmed-name"
    assert set(row) == {"device_code_hash", "user_code_hash", "device_name", "os", "client_version",
                        "status", "user_id", "created_at", "expires_at"}
    assert "poem" not in repr(row) and "secret-project" not in repr(row)


def test_start_hands_out_distinct_codes_in_the_right_shape(h):
    seen = set()
    for _ in range(6):
        body = h.start().json()
        assert len(body["device_code"]) == 43                       # 256 bits, urlsafe base64
        assert len(body["user_code"]) == 9 and body["user_code"][4] == "-"
        assert set(body["user_code"].replace("-", "")) <= set(devices.USER_CODE_ALPHABET)
        seen.add((body["device_code"], body["user_code"]))
    assert len(seen) == 6


def test_the_database_holds_hashes_never_the_codes(h, store):
    body = h.start().json()
    (key, row), = store.pairings.items()
    raw_user = body["user_code"].replace("-", "")
    assert key == sha256(body["device_code"]) and row["user_code_hash"] == sha256(raw_user)
    stored = repr(store.pairings)
    assert body["device_code"] not in stored and raw_user not in stored and body["user_code"] not in stored


def test_start_refuses_when_the_signing_secret_is_missing(h, store, monkeypatch):
    for value in ("", "too-short"):
        monkeypatch.setenv("DEVICE_JWT_SECRET", value)
        assert h.start().status_code == 503
    monkeypatch.delenv("DEVICE_JWT_SECRET")
    assert h.start().status_code == 503
    assert store.pairings == {}


def test_start_is_rate_limited_per_client_address(store, ttl_redis, clock):
    a = Harness(client_addr=("198.51.100.1", 1000))
    b = Harness(client_addr=("198.51.100.2", 1000))
    for _ in range(devices.START_LIMIT_PER_IP):
        assert a.start().status_code == 200
    blocked = a.start()
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) == devices.START_WINDOW_SECONDS
    assert b.start().status_code == 200                      # another address is unaffected
    clock.advance(devices.START_WINDOW_SECONDS + 1)
    assert a.start().status_code == 200                      # the window passed


def test_start_has_a_global_ceiling_too(store, ttl_redis, monkeypatch):
    monkeypatch.setattr(devices, "START_LIMIT_GLOBAL", 3)
    assert [Harness(client_addr=(f"198.51.100.{i}", 1)).start().status_code for i in range(1, 6)] == \
        [200, 200, 200, 429, 429]


def test_start_fails_closed_when_the_rate_limit_store_is_down(store, monkeypatch):
    from memory import bus
    monkeypatch.setattr(bus, "redis", BrokenRedis())
    resp = Harness().start()
    assert resp.status_code == 503
    assert store.pairings == {}                              # nothing was created


# ============================================================ pair/poll

def test_replayed_device_code_after_approval_fails(h, store):
    start = h.start().json()
    h.approve(start["user_code"])
    first = h.poll(start["device_code"])
    assert first.json()["status"] == "approved"

    for _ in range(3):
        again = h.poll(start["device_code"])
        assert again.status_code == 400
        assert "device_credential" not in again.text and "approved" not in again.text
    assert len(store.devices) == 1                           # no second device was minted


def test_only_one_of_many_concurrent_polls_collects_the_credential(h, store):
    start = h.start().json()
    h.approve(start["user_code"])
    results, gate = [], threading.Barrier(8)

    def poll():
        gate.wait()
        results.append(h.poll(start["device_code"]))

    threads = [threading.Thread(target=poll) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [r for r in results if r.status_code == 200 and r.json()["status"] == "approved"]
    assert len(winners) == 1 and len(store.devices) == 1
    assert all(r.status_code == 400 for r in results if r not in winners)


def test_the_credential_is_256_bits_and_only_its_sha256_is_stored(h, store):
    cred, device_id, _ = h.pair()
    assert len(cred) == 43                                   # 32 random bytes, urlsafe base64
    stored = store.devices[device_id]
    assert stored["credential_hash"] == sha256(cred)
    assert cred not in repr(store.devices) and cred not in repr(store.pairings)


def test_two_credentials_never_collide(h):
    assert len({h.pair()[0] for _ in range(5)}) == 5


def test_poll_unknown_or_malformed_device_codes_are_one_indistinguishable_400(h):
    for bad in ("", "short", "x" * 500, "has spaces and !!! chars in it which is long", "A" * 43):
        resp = h.poll(bad)
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Invalid or already-used device code."}
        assert bad not in resp.text or bad == ""


def test_poll_reports_denied_then_stays_denied(h):
    start = h.start().json()
    denied = h.deny(start["user_code"])
    assert denied.status_code == 200 and denied.json() == {"status": "denied"}
    assert h.poll(start["device_code"]).json() == {"status": "denied"}
    assert h.poll(start["device_code"]).json() == {"status": "denied"}
    assert h.approve(start["user_code"]).status_code == 404      # a denied code cannot then be approved


def test_poll_reports_expired_and_expired_codes_cannot_be_approved(h, clock):
    start = h.start().json()
    clock.advance(devices.PAIRING_TTL_SECONDS + 1)
    assert h.poll(start["device_code"]).json() == {"status": "expired"}
    assert h.approve(start["user_code"]).status_code == 404

    start2 = h.start().json()
    assert h.approve(start2["user_code"]).status_code == 200
    clock.advance(devices.PAIRING_TTL_SECONDS + 1)               # approved but never collected
    assert h.poll(start2["device_code"]).json() == {"status": "expired"}


def test_poll_does_not_burn_a_pairing_when_tokens_cannot_be_issued(h, store, monkeypatch):
    start = h.start().json()
    h.approve(start["user_code"])
    monkeypatch.delenv("DEVICE_JWT_SECRET")
    assert h.poll(start["device_code"]).status_code == 503
    assert store.claim_calls == 0
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    assert h.poll(start["device_code"]).json()["status"] == "approved"


def test_expired_pairing_rows_are_purged_on_the_next_start(h, store, clock):
    h.start()
    assert len(store.pairings) == 1
    clock.advance(devices.PAIRING_TTL_SECONDS + 86400 + 5)
    h.start()
    assert len(store.pairings) == 1                              # the old one was dropped


# ============================================================ approve / deny

def test_approve_requires_a_signed_in_user(store):
    app = FastAPI()
    from api.routes.devices import router
    app.include_router(router)                                   # no dependency override
    anon = TestClient(app)
    assert anon.post("/api/devices/approve", json={"user_code": "BCDF-GHJK"}).status_code == 401
    assert anon.post("/api/devices/deny", json={"user_code": "BCDF-GHJK"}).status_code == 401
    assert anon.get("/api/devices").status_code == 401
    assert anon.post("/api/devices/x/revoke").status_code == 401


def test_approve_binds_the_device_to_the_approving_account(h):
    _, device_id, _ = h.pair(user="user-a")
    assert [d["id"] for d in h.list("user-a").json()["devices"]] == [device_id]
    assert h.list("user-b").json()["devices"] == []


def test_user_code_is_forgiving_about_case_spaces_and_hyphens(h):
    start = h.start().json()
    raw = start["user_code"].replace("-", "")
    messy = f" {raw[:4].lower()} - {raw[4:].lower()} "
    assert h.approve(messy).status_code == 200


def test_approving_twice_as_the_same_user_is_harmless_but_not_as_someone_else(h):
    start = h.start().json()
    assert h.approve(start["user_code"], user="user-a").status_code == 200
    assert h.approve(start["user_code"], user="user-a").status_code == 200       # double click
    assert h.approve(start["user_code"], user="user-b").status_code == 404       # cannot hijack
    cred = h.poll(start["device_code"]).json()
    assert h.list("user-a").json()["devices"][0]["id"] == cred["device_id"]


def test_wrong_codes_get_one_generic_404(h):
    resp = h.approve("BCDF-GHJK")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "That code is invalid or has expired."}


def test_account_is_locked_after_repeated_wrong_codes_and_even_the_right_code_is_refused(h, ttl_redis, clock):
    start = h.start().json()
    for _ in range(devices.MAX_WRONG_ATTEMPTS):
        assert h.approve("BCDF-GHJK", user="user-a").status_code == 404
    locked = h.approve(start["user_code"], user="user-a")        # the RIGHT code, but locked
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) == devices.LOCK_SECONDS
    assert h.deny(start["user_code"], user="user-a").status_code == 429
    assert h.poll(start["device_code"]).json() == {"status": "pending"}   # nothing got approved

    assert h.approve(start["user_code"], user="user-b").status_code == 200   # lock is per account

    clock.advance(devices.LOCK_SECONDS + 1)
    assert h.approve("BCDF-GHJK", user="user-a").status_code == 404          # unlocked again (just wrong)


def test_refused_attempts_while_locked_do_not_extend_the_lock(h, ttl_redis, clock):
    for _ in range(devices.MAX_WRONG_ATTEMPTS):
        h.approve("BCDF-GHJK")
    clock.advance(devices.LOCK_SECONDS - 10)
    for _ in range(5):
        assert h.approve("BCDF-GHJK").status_code == 429
    clock.advance(11)
    assert h.approve("BCDF-GHJK").status_code == 404             # the original window ended on time


def test_codes_of_the_wrong_shape_are_rejected_without_burning_attempts(h):
    for _ in range(devices.MAX_WRONG_ATTEMPTS + 3):
        assert h.approve("not a code").status_code == 404
        assert h.approve("AEIO-0123").status_code == 404           # vowels and digits are not in the alphabet
    start = h.start().json()
    assert h.approve(start["user_code"]).status_code == 200      # still not locked


def test_approve_fails_closed_when_the_attempt_store_is_down(h, store, monkeypatch):
    start = h.start().json()
    from memory import bus
    monkeypatch.setattr(bus, "redis", BrokenRedis())
    assert h.approve(start["user_code"]).status_code == 503
    assert store.pairings[sha256(start["device_code"])]["status"] == "pending"


# ============================================================ list / revoke

def test_list_is_only_my_active_devices_unless_asked(h, clock):
    c1, d1, _ = h.pair(user="user-a", name="one")
    clock.advance(10)
    c2, d2, _ = h.pair(user="user-a", name="two")
    _, other, _ = h.pair(user="user-b", name="theirs")
    assert [d["id"] for d in h.list("user-a").json()["devices"]] == [d2, d1]      # newest first
    assert h.revoke(d1, user="user-a").status_code == 200
    assert [d["id"] for d in h.list("user-a").json()["devices"]] == [d2]
    everything = h.list("user-a", include_revoked="true").json()["devices"]
    assert {d["id"] for d in everything} == {d1, d2}
    assert [d for d in everything if d["id"] == d1][0]["revoked_at"] is not None
    assert set(h.list("user-a").json()["devices"][0]) == {
        "id", "device_name", "os", "client_version", "created_at", "last_seen_at", "revoked_at"}
    assert [d["id"] for d in h.list("user-b").json()["devices"]] == [other]


def test_a_user_cannot_revoke_someone_elses_device(h):
    cred, device_id, _ = h.pair(user="user-a")
    resp = h.revoke(device_id, user="user-b")
    assert resp.status_code == 404 and resp.json() == {"detail": "Device not found."}
    assert h.token(cred).status_code == 200                       # still alive
    assert h.revoke(device_id, user="user-b").text == h.revoke("00000000-0000-0000-0000-000000000000").text


def test_revoke_is_idempotent_and_keeps_the_first_timestamp(h, clock):
    _, device_id, _ = h.pair()
    first = h.revoke(device_id)
    clock.advance(60)
    second = h.revoke(device_id)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["id"] == device_id


def test_revoke_with_a_malformed_id_is_a_404_not_a_500(h):
    for bad in ("nope", "123", "../etc/passwd", "'; drop table devices; --"):
        assert h.client.post(f"/api/devices/{bad}/revoke").status_code in (404,)


# ============================================================ device token

def test_token_claims_and_lifetime(h):
    cred, device_id, _ = h.pair(user="user-a")
    before = int(time.time())
    body = h.token(cred).json()
    assert set(body) == {"jwt", "expires_in"} and body["expires_in"] == 900
    claims = jwt.decode(body["jwt"], SECRET, algorithms=["HS256"], audience="m2c-device")
    assert claims["sub"] == "user-a" and claims["device_id"] == device_id
    assert claims["token_type"] == "device" and claims["aud"] == "m2c-device"
    assert claims["exp"] - claims["iat"] == 900
    assert before - 2 <= claims["iat"] <= int(time.time()) + 2


def test_revoked_device_can_no_longer_get_a_token(h):
    cred, device_id, _ = h.pair()
    assert h.token(cred).status_code == 200
    assert h.revoke(device_id).status_code == 200
    resp = h.token(cred)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or revoked device credential."}


def test_a_token_issued_before_revocation_stops_verifying_after_it(h):
    cred, device_id, _ = h.pair(user="user-a")
    token = h.token(cred).json()["jwt"]
    assert devices.verify_device_token(token).device_id == device_id
    h.revoke(device_id)
    with pytest.raises(devices.DeviceAuthError):
        devices.verify_device_token(token)


def test_token_rejects_unknown_and_malformed_credentials_alike(h):
    h.pair()
    for bad in ("", "short", "A" * 43, "x" * 500, "bad chars !!! bad chars !!! bad chars"):
        resp = h.token(bad)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid or revoked device credential."}


def test_a_device_code_or_user_code_is_not_a_credential(h):
    start = h.start().json()
    h.approve(start["user_code"])
    assert h.token(start["device_code"]).status_code == 401
    assert h.token(start["user_code"]).status_code == 401


def test_token_refuses_to_issue_without_a_proper_signing_secret(h, monkeypatch):
    cred, _, _ = h.pair()
    for value in (None, "", "short-secret"):
        if value is None:
            monkeypatch.delenv("DEVICE_JWT_SECRET")
        else:
            monkeypatch.setenv("DEVICE_JWT_SECRET", value)
        resp = h.token(cred)
        assert resp.status_code == 503 and "jwt" not in resp.text
    # ... and never with the Supabase secret, even if someone configures it so
    monkeypatch.setenv("DEVICE_JWT_SECRET", SUPABASE_SECRET)
    assert h.token(cred).status_code == 503


def test_token_refresh_stamps_last_seen(h, store, clock):
    cred, device_id, _ = h.pair()
    assert store.devices[device_id]["last_seen_at"] is None
    clock.advance(120)
    h.token(cred)
    assert store.devices[device_id]["last_seen_at"] == clock.t


# ============================================================ device JWT vs user JWT

def _user_token(secret=SUPABASE_SECRET, **over):
    claims = {"sub": "user-a", "aud": "authenticated", "iat": int(time.time()),
              "exp": int(time.time()) + 3600, "role": "authenticated"}
    claims.update(over)
    return jwt.encode(claims, secret, algorithm="HS256")


def test_a_device_token_can_never_pass_require_auth(h, monkeypatch):
    import api.deps
    monkeypatch.setattr(api.deps, "SUPABASE_JWT_SECRET", SUPABASE_SECRET)
    cred, _, _ = h.pair()
    device_jwt = h.token(cred).json()["jwt"]

    app = FastAPI()
    from api.routes.devices import router
    app.include_router(router)                                    # the REAL require_auth, no override
    real = TestClient(app)
    resp = real.get("/api/devices", headers={"Authorization": f"Bearer {device_jwt}"})
    assert resp.status_code == 401
    assert real.post("/api/devices/approve", json={"user_code": "BCDF-GHJK"},
                     headers={"Authorization": f"Bearer {device_jwt}"}).status_code == 401
    # sanity: the same harness does accept a genuine user token
    genuine = real.get("/api/devices", headers={"Authorization": f"Bearer {_user_token()}"})
    assert genuine.status_code != 401


def test_a_user_token_can_never_pass_the_device_check(h):
    cred, device_id, _ = h.pair(user="user-a")
    good = h.token(cred).json()["jwt"]
    now = int(time.time())
    base = {"sub": "user-a", "device_id": device_id, "token_type": "device",
            "aud": "m2c-device", "iat": now, "exp": now + 900}

    assert devices.verify_device_token(good).user_id == "user-a"

    bad_tokens = {
        "supabase user token": _user_token(),
        "supabase token claiming device fields": _user_token(device_id=device_id, token_type="device"),
        "user audience, device key": jwt.encode({**base, "aud": "authenticated"}, SECRET, algorithm="HS256"),
        "device audience, supabase key": jwt.encode(base, SUPABASE_SECRET, algorithm="HS256"),
        "no token_type": jwt.encode({k: v for k, v in base.items() if k != "token_type"}, SECRET, algorithm="HS256"),
        "wrong token_type": jwt.encode({**base, "token_type": "user"}, SECRET, algorithm="HS256"),
        "no device_id": jwt.encode({k: v for k, v in base.items() if k != "device_id"}, SECRET, algorithm="HS256"),
        "device_id not a uuid": jwt.encode({**base, "device_id": "../../x"}, SECRET, algorithm="HS256"),
        "expired": jwt.encode({**base, "iat": now - 2000, "exp": now - 1000}, SECRET, algorithm="HS256"),
        "no exp": jwt.encode({k: v for k, v in base.items() if k != "exp"}, SECRET, algorithm="HS256"),
        "alg none": jwt.encode(base, None, algorithm="none"),
        "tampered": good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB"),
        "wrong user for the device": jwt.encode({**base, "sub": "user-b"}, SECRET, algorithm="HS256"),
        "unknown device": jwt.encode({**base, "device_id": "11111111-1111-4111-8111-111111111111"},
                                     SECRET, algorithm="HS256"),
        "garbage": "not.a.jwt",
        "empty": "",
    }
    for label, token in bad_tokens.items():
        with pytest.raises(devices.DeviceAuthError):
            devices.verify_device_token(token)
        assert label                                             # (shows up in the failure output)


def test_verify_fails_closed_when_the_secret_is_missing(h, monkeypatch):
    cred, _, _ = h.pair()
    token = h.token(cred).json()["jwt"]
    monkeypatch.delenv("DEVICE_JWT_SECRET")
    with pytest.raises(devices.DeviceAuthError):
        devices.verify_device_token(token)


# ============================================================ no secrets in logs, errors or reprs

def _secrets_of(flow):
    return {s for s in flow if s}


def test_no_secret_appears_in_logs_output_or_error_responses(h, store, caplog, capsys):
    caplog.set_level(logging.DEBUG)                 # root logger, everything
    start = h.start().json()
    wrong_user_code = "ZZZZ-ZZZZ"
    wrong_device_code = "w" * 43
    wrong_credential = "c" * 43

    responses = [
        h.poll(start["device_code"]),
        h.approve(wrong_user_code),
        h.approve(start["user_code"]),
        h.deny("not a code at all"),
        h.poll(wrong_device_code),
        h.token(wrong_credential),
    ]
    polled = h.poll(start["device_code"])
    credential = polled.json()["device_credential"]
    responses += [polled, h.poll(start["device_code"]), h.token(credential)]
    token = h.token(credential).json()["jwt"]
    device_id = polled.json()["device_id"]
    responses += [h.list(), h.revoke(device_id), h.token(credential)]

    secrets_ = _secrets_of([
        start["device_code"], start["user_code"], start["user_code"].replace("-", ""),
        wrong_user_code, wrong_user_code.replace("-", ""), wrong_device_code, wrong_credential,
        credential, token,
    ])
    log_text = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.args) for r in caplog.records]
        + [r.exc_text or "" for r in caplog.records]
    ) + caplog.text
    out = capsys.readouterr()
    assert caplog.records, "expected the flow to log something (user and device ids)"
    for secret in secrets_:
        assert secret not in log_text, "a secret reached the logs"
        assert secret not in out.out + out.err
    # ... and error responses never echo what was sent
    for resp in responses:
        if resp.status_code >= 400:
            for secret in (start["device_code"], start["user_code"], wrong_user_code,
                           wrong_device_code, wrong_credential, credential):
                assert secret not in resp.text
    # what IS logged is ids only
    assert any("device paired" in r.getMessage() and device_id in r.getMessage() for r in caplog.records)


def test_an_infrastructure_error_is_logged_by_type_only(store, monkeypatch, caplog):
    from memory import bus
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(bus, "redis", BrokenRedis())
    h = Harness()
    assert h.start().status_code == 503
    assert h.approve("BCDF-GHJK").status_code == 503
    assert caplog.records
    assert BrokenRedis.CANARY not in caplog.text and "device_pair:wrong" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_exceptions_raised_for_bad_secrets_never_contain_them(store):
    # Built at run time: a traceback prints the SOURCE line of each frame, so a
    # literal here would "appear" in it without anything having leaked.
    user_code = "R" * 4 + "-" + "R" * 4
    jwt_like = ".".join(["hdr" * 3, "payload" * 3, "sig" * 5])
    probes = [
        (lambda s: devices.poll_pairing(s), "p" * 43, devices.InvalidDeviceCode),
        (lambda s: devices.issue_device_token(s), "q" * 43, devices.InvalidCredential),
        (lambda s: devices.approve_pairing("u", s), user_code, devices.InvalidCode),
        (lambda s: devices.verify_device_token(s), jwt_like, devices.DeviceAuthError),
    ]
    import traceback
    for call, secret, exc_type in probes:
        with pytest.raises(exc_type) as info:
            call(secret)
        text = "".join(traceback.format_exception(info.value)) + repr(info.value)
        assert secret not in text and secret.replace("-", "") not in text


def test_request_models_mask_their_secrets_in_repr():
    from api.routes.devices import PairPollRequest, TokenRequest, UserCodeRequest
    assert "device-code-value" not in repr(PairPollRequest(device_code="device-code-value"))
    assert "credential-value" not in repr(TokenRequest(device_credential="credential-value"))
    assert "BCDF-GHJK" not in repr(UserCodeRequest(user_code="BCDF-GHJK")) + str(UserCodeRequest(user_code="BCDF-GHJK"))


def test_secrets_are_only_ever_in_bodies_not_urls():
    from api.routes.devices import router
    for route in router.routes:
        assert "code" not in route.path.replace("/v1/code/", "") and "credential" not in route.path


# ============================================================ wiring

def test_the_router_is_mounted_on_the_real_app():
    from api.server import app
    spec = app.openapi()["paths"]
    paths = {(method.upper(), path) for path, ops in spec.items() for method in ops}
    for expected in [
        ("POST", "/v1/code/devices/pair/start"), ("POST", "/v1/code/devices/pair/poll"),
        ("POST", "/v1/code/devices/token"), ("POST", "/api/devices/approve"),
        ("POST", "/api/devices/deny"), ("GET", "/api/devices"),
        ("POST", "/api/devices/{device_id}/revoke"),
    ]:
        assert expected in paths
