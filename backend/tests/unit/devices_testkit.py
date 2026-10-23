"""Shared fakes for the S03 device-pairing tests (not collected: no test_ prefix).

  Clock         a controllable clock, so expiry and lock-out windows can be
                crossed without sleeping.
  FakeStore     an in-memory eo.devices.PgStore with the same atomic
                semantics (single collection of a credential, revocation
                checked together with the last-seen stamp, idempotent
                revoke). It is what lets the unit tests drive the WHOLE flow
                through the real routes without a database; the SQL itself
                is covered by tests/integration/test_devices_pg.py.
  TTLRedis      the slice of Redis that eo/devices.py uses (get / incr /
                expire), honouring TTLs against the Clock. tests/conftest.py's
                FakeRedis ignores TTLs, which is fine for most tests but cannot
                show a lock-out lifting.
  BrokenRedis   every call raises, with a canary in the message, to prove
                the module fails closed and never logs an error's text.
  Harness       a FastAPI app with the devices router, a TestClient, and a
                switchable "signed-in user" standing in for require_auth.
"""
import hashlib
import threading
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import require_auth
from api.routes.devices import router

SECRET = "unit-test-device-jwt-secret-0123456789-abcdefghij"      # 49 chars, test-only
SUPABASE_SECRET = "unit-test-supabase-jwt-secret-9876543210-zyxwvutsrq"
VERIFY_URL = "https://app.example.test/devices"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def advance(self, seconds):
        self.t += seconds

    def dt(self, t=None):
        return datetime.fromtimestamp(self.t if t is None else t, tz=timezone.utc)


class FakeStore:
    """Mirror of eo.devices.PgStore. Methods take the same keyword args and
    return the same shapes; one lock makes every method atomic, like a
    single SQL statement."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.pairings = {}      # device_code_hash -> dict
        self.devices = {}       # id -> dict
        self.claim_calls = 0

    # -- helpers -------------------------------------------------------
    def _by_user_code(self, user_code_hash):
        return next((p for p in self.pairings.values() if p["user_code_hash"] == user_code_hash), None)

    def _live(self, p):
        return p["expires_at"] > self.clock.t

    # -- pairing -------------------------------------------------------
    def create_pairing(self, *, device_code_hash, user_code_hash, device_name,
                       os_name, client_version, ttl_seconds):
        with self.lock:
            if device_code_hash in self.pairings or self._by_user_code(user_code_hash):
                return False
            self.pairings[device_code_hash] = {
                "device_code_hash": device_code_hash, "user_code_hash": user_code_hash,
                "device_name": device_name, "os": os_name, "client_version": client_version,
                "status": "pending", "user_id": None,
                "created_at": self.clock.t, "expires_at": self.clock.t + ttl_seconds,
            }
            return True

    def purge_expired(self):
        with self.lock:
            for key in [k for k, p in self.pairings.items() if p["expires_at"] < self.clock.t - 86400]:
                del self.pairings[key]

    def approve(self, *, user_code_hash, user_id):
        with self.lock:
            p = self._by_user_code(user_code_hash)
            if p and p["status"] == "pending" and self._live(p):
                p.update(status="approved", user_id=user_id)
            elif not (p and p["user_id"] == user_id and p["status"] in ("approved", "consumed")
                      and self._live(p)):
                return None
            return {"device_name": p["device_name"], "os": p["os"], "client_version": p["client_version"]}

    def deny(self, *, user_code_hash):
        with self.lock:
            p = self._by_user_code(user_code_hash)
            if p and p["status"] == "pending" and self._live(p):
                p["status"] = "denied"
                return True
            return False

    def claim(self, *, device_code_hash, credential_hash):
        with self.lock:
            self.claim_calls += 1
            p = self.pairings.get(device_code_hash)
            if not (p and p["status"] == "approved" and self._live(p)):
                return None
            p["status"] = "consumed"
            device_id = str(uuid.uuid4())
            self.devices[device_id] = {
                "id": device_id, "user_id": p["user_id"], "device_name": p["device_name"],
                "os": p["os"], "client_version": p["client_version"],
                "credential_hash": credential_hash, "created_at": self.clock.t,
                "last_seen_at": None, "revoked_at": None,
            }
            return {"device_id": device_id, "user_id": p["user_id"]}

    def pairing_state(self, *, device_code_hash):
        with self.lock:
            p = self.pairings.get(device_code_hash)
            return None if p is None else {"status": p["status"], "expired": not self._live(p)}

    # -- devices -------------------------------------------------------
    def touch_active_by_credential(self, *, credential_hash):
        with self.lock:
            for d in self.devices.values():
                if d["credential_hash"] == credential_hash and d["revoked_at"] is None:
                    d["last_seen_at"] = self.clock.t
                    return {"id": d["id"], "user_id": d["user_id"]}
            return None

    def is_active(self, *, user_id, device_id):
        with self.lock:
            d = self.devices.get(device_id)
            return bool(d and d["user_id"] == user_id and d["revoked_at"] is None)

    def _row(self, d):
        return {
            "id": d["id"], "device_name": d["device_name"], "os": d["os"],
            "client_version": d["client_version"], "created_at": self.clock.dt(d["created_at"]),
            "last_seen_at": self.clock.dt(d["last_seen_at"]) if d["last_seen_at"] else None,
            "revoked_at": self.clock.dt(d["revoked_at"]) if d["revoked_at"] else None,
        }

    def list_devices(self, *, user_id, include_revoked):
        with self.lock:
            mine = [d for d in self.devices.values()
                    if d["user_id"] == user_id and (include_revoked or d["revoked_at"] is None)]
            mine.sort(key=lambda d: d["created_at"], reverse=True)
            return [self._row(d) for d in mine]

    def revoke(self, *, user_id, device_id):
        with self.lock:
            d = self.devices.get(device_id)
            if not d or d["user_id"] != user_id:
                return None
            if d["revoked_at"] is None:
                d["revoked_at"] = self.clock.t
            return {"id": d["id"], "revoked_at": self.clock.dt(d["revoked_at"])}


class TTLRedis:
    def __init__(self, clock: Clock):
        self.clock, self.data, self.expiry = clock, {}, {}

    def _live(self, key):
        if key in self.expiry and self.expiry[key] <= self.clock.t:
            self.data.pop(key, None)
            self.expiry.pop(key, None)

    def get(self, key):
        self._live(key)
        return self.data.get(key)

    def incr(self, key):
        self._live(key)
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    def expire(self, key, seconds):
        if key not in self.data:
            return False
        self.expiry[key] = self.clock.t + seconds
        return True

    def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
            self.expiry.pop(k, None)


class BrokenRedis:
    """Every operation fails, with an identifiable string in the message."""
    CANARY = "CANARY-redis-exploded-key=device_pair:wrong:someone"

    def _boom(self, *a, **k):
        raise RuntimeError(self.CANARY)

    get = incr = expire = delete = set = getdel = _boom


class Harness:
    """The devices router behind a TestClient. `user` is whoever require_auth
    resolves to; set it to act as a different signed-in account."""

    def __init__(self, client_addr=("203.0.113.7", 40000), user="user-a"):
        self.user = user
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: self.user
        self.app = app
        self.client = TestClient(app, client=client_addr, raise_server_exceptions=False)

    # terminal side
    def start(self, name="alice-laptop", os="Ubuntu 24.04 (WSL2)", version="0.1.0", **extra):
        return self.client.post("/v1/code/devices/pair/start",
                                json={"device_name": name, "os": os, "client_version": version, **extra})

    def poll(self, device_code):
        return self.client.post("/v1/code/devices/pair/poll", json={"device_code": device_code})

    def token(self, credential):
        return self.client.post("/v1/code/devices/token", json={"device_credential": credential})

    # web side
    def approve(self, user_code, user=None):
        if user:
            self.user = user
        return self.client.post("/api/devices/approve", json={"user_code": user_code})

    def deny(self, user_code, user=None):
        if user:
            self.user = user
        return self.client.post("/api/devices/deny", json={"user_code": user_code})

    def list(self, user=None, **params):
        if user:
            self.user = user
        return self.client.get("/api/devices", params=params)

    def revoke(self, device_id, user=None):
        if user:
            self.user = user
        return self.client.post(f"/api/devices/{device_id}/revoke")

    def pair(self, user="user-a", **kw):
        """Run the whole pairing loop; return (credential, device_id, start_body)."""
        start = self.start(**kw)
        assert start.status_code == 200, start.text
        body = start.json()
        assert self.approve(body["user_code"], user=user).status_code == 200
        polled = self.poll(body["device_code"])
        assert polled.status_code == 200, polled.text
        data = polled.json()
        assert data["status"] == "approved", data
        return data["device_credential"], data["device_id"], body


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
