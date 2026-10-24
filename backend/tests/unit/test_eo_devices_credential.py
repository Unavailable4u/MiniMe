"""eo/devices.py, the two public helpers S04 adds for the device socket:

    authenticate_credential(credential) -> DeviceIdentity     (handshake)
    is_device_active(user_id, device_id) -> bool              (periodic re-check)

plus the one exception (DeviceRevoked) and the one PgStore method
(credential_is_revoked) they need so the socket can tell `device_revoked` from
`unauthorized`. No hashing or revocation logic is re-implemented anywhere else:
these call S03's own SHA-256 and S03's own atomic revoked-or-not statement.

Same approach as test_api_routes_devices.py: real logic, in-memory store
(WsFakeStore extends devices_testkit.FakeStore), and a recording cursor for the SQL.
"""
import contextlib
import logging
import time

import jwt
import pytest

from eo import devices
from tests.unit.codeagent_testkit import WsFakeStore
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET, Clock, Harness


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(monkeypatch, clock):
    fake = WsFakeStore(clock)
    monkeypatch.setattr(devices, "_STORE", fake)
    return fake


# ---------------------------------------------------------------- authenticate_credential

def test_a_paired_devices_credential_authenticates_and_stamps_last_seen(store, clock):
    """Through the real pairing flow, so the credential is exactly what S03 hands out."""
    credential, device_id, _ = Harness().pair(user="user-a")
    clock.advance(5)
    identity = devices.authenticate_credential(credential)
    assert isinstance(identity, devices.DeviceIdentity)
    assert identity.user_id == "user-a" and identity.device_id == device_id
    # expires_at is the advertised authentication lifetime (informational), as for a JWT
    assert abs(identity.expires_at - (int(time.time()) + devices.DEVICE_JWT_TTL_SECONDS)) <= 2
    assert store.devices[device_id]["last_seen_at"] == clock.t


def test_unknown_credential_is_invalid_not_revoked(store):
    with pytest.raises(devices.InvalidCredential) as caught:
        devices.authenticate_credential("z" * 43)
    assert not isinstance(caught.value, devices.DeviceRevoked)


def test_revoked_credential_raises_device_revoked_which_is_still_an_invalid_credential(store):
    credential, device_id, _ = Harness().pair(user="user-a")
    devices.revoke_device("user-a", device_id)
    with pytest.raises(devices.DeviceRevoked) as caught:
        devices.authenticate_credential(credential)
    assert isinstance(caught.value, devices.InvalidCredential)       # S03's token route keeps answering 401
    assert store.devices[device_id]["last_seen_at"] is None          # and a revoked device is not stamped


def test_the_token_endpoint_is_unchanged_for_a_revoked_credential(store):
    h = Harness()
    credential, device_id, _ = h.pair(user="user-a")
    h.revoke(device_id, user="user-a")
    refused = h.token(credential)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Invalid or revoked device credential."


@pytest.mark.parametrize("bad", [None, 123, b"x" * 43, "", "short", "a" * 19, "a" * 129, "has space " + "a" * 20,
                                 "dots.are.for.jwts." + "a" * 20, "ünïcode" * 5, "a" * 43 + "\n"])
def test_malformed_credentials_are_refused_without_touching_the_store(store, bad):
    with pytest.raises(devices.InvalidCredential):
        devices.authenticate_credential(bad)
    assert store.touch_calls == 0


def test_jwts_are_not_credentials(store):
    credential, device_id, _ = Harness().pair(user="user-a")
    now = int(time.time())
    device_jwt = devices.issue_device_token(credential)["jwt"]
    supabase = jwt.encode({"sub": "user-a", "aud": "authenticated", "iat": now, "exp": now + 60},
                          SUPABASE_SECRET, algorithm="HS256")
    for token in (device_jwt, supabase):
        with pytest.raises(devices.InvalidCredential):
            devices.authenticate_credential(token)
    assert store.touch_calls == 1                                    # only issue_device_token's own


def test_authentication_needs_no_jwt_signing_secret(store, monkeypatch):
    credential, _device_id, _ = Harness().pair(user="user-a")
    monkeypatch.delenv("DEVICE_JWT_SECRET")
    assert devices.authenticate_credential(credential).user_id == "user-a"


def test_a_store_fault_propagates_so_the_caller_can_fail_closed(store):
    store.fail_touch = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        devices.authenticate_credential("q" * 43)
    store.fail_touch = None
    store.fail_revoked_lookup = RuntimeError("db down")
    with pytest.raises(RuntimeError):                                # unknown -> must look up revoked -> fault
        devices.authenticate_credential("q" * 43)


def test_no_secret_reaches_a_log_or_an_exception_message(store, caplog):
    credential, device_id, _ = Harness().pair(user="user-a")
    secret = "Z" * 43
    caplog.set_level(logging.DEBUG)
    devices.authenticate_credential(credential)
    with pytest.raises(devices.InvalidCredential) as unknown:
        devices.authenticate_credential(secret)
    devices.revoke_device("user-a", device_id)
    with pytest.raises(devices.DeviceRevoked) as revoked:
        devices.authenticate_credential(credential)
    for text in (caplog.text, str(unknown.value), str(revoked.value)):
        assert credential not in text and secret not in text


def test_it_uses_s03s_hash_not_its_own(store, monkeypatch):
    seen = []
    real = devices._sha256
    monkeypatch.setattr(devices, "_sha256", lambda v: (seen.append(v), real(v))[1])
    credential, _id, _ = Harness().pair(user="user-a")
    seen.clear()
    devices.authenticate_credential(credential)
    assert seen == [credential]


# ---------------------------------------------------------------- is_device_active

def test_is_device_active_follows_revocation(store):
    _credential, device_id, _ = Harness().pair(user="user-a")
    assert devices.is_device_active("user-a", device_id) is True
    devices.revoke_device("user-a", device_id)
    assert devices.is_device_active("user-a", device_id) is False


def test_is_device_active_is_scoped_to_the_owner(store):
    _credential, device_id, _ = Harness().pair(user="user-a")
    assert devices.is_device_active("user-b", device_id) is False


@pytest.mark.parametrize("bad_id", ["", "not-a-uuid", None, 5, "00000000-0000-0000-0000-00000000000g"])
def test_is_device_active_is_false_for_a_malformed_id_without_a_query(store, bad_id):
    assert devices.is_device_active("user-a", bad_id) is False
    assert store.active_calls == 0


@pytest.mark.parametrize("bad_user", ["", None, 7])
def test_is_device_active_is_false_without_a_user(store, bad_user):
    _credential, device_id, _ = Harness().pair(user="user-a")
    assert devices.is_device_active(bad_user, device_id) is False


def test_is_device_active_does_not_swallow_a_store_fault(store):
    """A database fault is not the same as 'revoked': the caller decides (the socket
    closes with `internal`, which is still fail-closed)."""
    _credential, device_id, _ = Harness().pair(user="user-a")
    store.fail_is_active = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        devices.is_device_active("user-a", device_id)


def test_is_device_active_does_not_stamp_last_seen(store, clock):
    _credential, device_id, _ = Harness().pair(user="user-a")
    clock.advance(100)
    devices.is_device_active("user-a", device_id)
    assert store.devices[device_id]["last_seen_at"] is None          # a 30-second poll must not write


# ---------------------------------------------------------------- PgStore.credential_is_revoked

class Recorder:
    def __init__(self, rows=None):
        self.calls, self.rows, self.identity = [], list(rows or []), []

    def install(self, monkeypatch):
        @contextlib.contextmanager
        def cursor(user_id=None, trusted=False):
            self.identity.append((user_id, trusted))
            yield self
        monkeypatch.setattr(devices.db, "cursor", cursor)
        return self

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def test_credential_is_revoked_is_one_read_by_hash_on_the_trusted_path(monkeypatch):
    rec = Recorder(rows=[{"ok": 1}]).install(monkeypatch)
    assert devices.PgStore().credential_is_revoked(credential_hash="c" * 64) is True
    (sql, params), = rec.calls
    assert sql.startswith("select ") and "from devices" in sql
    assert "credential_hash = %s" in sql and "revoked_at is not null" in sql
    assert params == ("c" * 64,)
    assert rec.identity == [(None, True)]            # no signed-in user on the socket: trusted read, like the token path


def test_credential_is_revoked_is_false_when_no_row_matches(monkeypatch):
    Recorder(rows=[]).install(monkeypatch)
    assert devices.PgStore().credential_is_revoked(credential_hash="c" * 64) is False
