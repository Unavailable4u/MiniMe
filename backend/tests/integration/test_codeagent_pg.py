"""Real-Postgres checks for S04: the statements behind eo.devices.authenticate_credential
and is_device_active (including PgStore.credential_is_revoked, the one statement S04
adds), and the device socket end to end on top of them.

Same gating as test_devices_pg.py (S03): skipped unless MINIME_TEST_DATABASE_URL is set,
pointing at a scratch database built as the root README's "Real-database tests" section
says (auth.users stub, every migration 0000 through 0013) and connecting AS minime_app.
It FAILS, rather than skips, when connected as a superuser / BYPASSRLS role / owner of the
device tables or when 0013 is missing, because the checks would then pass without the
grants and row-level security being exercised.

    MINIME_TEST_DATABASE_URL="postgresql://minime_app:pw@localhost:5432/minime_test" \\
        pytest tests/integration/test_codeagent_pg.py

Every test uses its own random user ids; devices is revoke-only for minime_app by
design, so rows are not cleaned up.
"""
import contextlib
import os
import time
import uuid

import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")

SECRET = "pg-test-device-jwt-secret-0123456789-abcdefghijklmnop"


@pytest.fixture(scope="module", autouse=True)
def _connected_as_the_app_role_on_a_migrated_schema():
    import psycopg
    problems = []
    with psycopg.connect(DSN) as conn:
        who, is_super, bypass = conn.execute(
            "select current_user, r.rolsuper, r.rolbypassrls "
            "from pg_roles r where r.rolname = current_user").fetchone()
        if who != "minime_app":
            problems.append(f"connected as {who!r}, not 'minime_app'")
        if is_super or bypass:
            problems.append("the role is a superuser or has BYPASSRLS (grants and RLS not enforced)")
        owned = [r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' and tableowner = current_user "
            "and tablename in ('devices', 'device_pairings')")]
        if owned:
            problems.append(f"the role OWNS {owned} (owners bypass grants and RLS)")
        have = [r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' and tablename = 'devices'")]
        if not have:
            problems.append("migration 0013 is not applied")
    if problems:
        pytest.fail("MINIME_TEST_DATABASE_URL must connect as the unprivileged app role on a database "
                    "built per the README through 0013: " + "; ".join(problems), pytrace=False)


@pytest.fixture
def pg(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from eo import devices

    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)

    @contextlib.contextmanager
    def cursor(user_id=None, trusted=False):
        conn = psycopg.connect(DSN, row_factory=dict_row)
        try:
            cur = conn.cursor()
            cur.execute("select set_config('app.current_user_id', %s, true), "
                        "set_config('app.trusted_internal', %s, true)",
                        (user_id or "", "true" if trusted else "false"))
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(devices.db, "cursor", cursor)
    return cursor


@pytest.fixture
def user():
    return f"pg_s04_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def paired(pg, user):
    """(credential, device_id) of a device paired through the real routes and SQL."""
    from tests.unit.devices_testkit import Harness
    credential, device_id, _ = Harness(user=user).pair(user=user, name=f"laptop-{uuid.uuid4().hex[:6]}")
    return credential, device_id


# ---------------------------------------------------------------- the two helpers, for real

def test_authenticate_credential_against_postgres(pg, user, paired):
    from eo import devices
    credential, device_id = paired
    with pg(user_id=user) as cur:
        cur.execute("select last_seen_at from devices where id = %s", (device_id,))
        assert cur.fetchone()["last_seen_at"] is None
    identity = devices.authenticate_credential(credential)
    assert (identity.user_id, identity.device_id) == (user, device_id)
    with pg(user_id=user) as cur:
        cur.execute("select last_seen_at from devices where id = %s", (device_id,))
        assert cur.fetchone()["last_seen_at"] is not None


def test_unknown_and_revoked_credentials_are_told_apart(pg, user, paired):
    from eo import devices
    credential, device_id = paired
    with pytest.raises(devices.InvalidCredential) as unknown:
        devices.authenticate_credential("u" * 43)
    assert not isinstance(unknown.value, devices.DeviceRevoked)
    devices.revoke_device(user, device_id)
    with pytest.raises(devices.DeviceRevoked):
        devices.authenticate_credential(credential)
    with pg(user_id=user) as cur:                         # a revoked device is not stamped again
        cur.execute("select last_seen_at, revoked_at from devices where id = %s", (device_id,))
        row = cur.fetchone()
        assert row["revoked_at"] is not None and row["last_seen_at"] is None


def test_credential_is_revoked_runs_as_the_unprivileged_role(pg, user, paired):
    """The new statement is a plain SELECT on the trusted path: the app role is allowed
    it, and it answers for the credential's hash only."""
    from eo import devices
    from tests.unit.devices_testkit import sha256
    credential, device_id = paired
    store = devices.PgStore()
    assert store.credential_is_revoked(credential_hash=sha256(credential)) is False
    devices.revoke_device(user, device_id)
    assert store.credential_is_revoked(credential_hash=sha256(credential)) is True
    assert store.credential_is_revoked(credential_hash=sha256("n" * 43)) is False


def test_is_device_active_against_postgres(pg, user, paired):
    from eo import devices
    _credential, device_id = paired
    assert devices.is_device_active(user, device_id) is True
    assert devices.is_device_active(f"{user}-someone-else", device_id) is False
    assert devices.is_device_active(user, str(uuid.uuid4())) is False
    devices.revoke_device(user, device_id)
    assert devices.is_device_active(user, device_id) is False


# ---------------------------------------------------------------- the socket on the real store

def test_the_socket_on_the_real_store_handshakes_and_drops_a_revoked_device(monkeypatch, pg, user, paired):
    pytest.importorskip("jsonschema")
    import dataclasses

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from codeagent import connections, ws as ws_module
    from eo import devices
    from tests.unit.codeagent_testkit import FakeDevice, hello, ping

    credential, device_id = paired
    monkeypatch.setattr(connections, "_REGISTRY", connections.InMemoryRegistry())
    monkeypatch.setattr(connections, "_LIMITER", connections.HandshakeLimiter())
    monkeypatch.setattr(ws_module, "SETTINGS",
                        dataclasses.replace(ws_module.SETTINGS, revalidate_interval_s=0.2))
    app = FastAPI()
    app.include_router(ws_module.router)
    with TestClient(app).websocket_connect("/v1/code/device") as sock:
        dev = FakeDevice(sock)
        dev.send(hello(credential))
        welcome = dev.recv()
        assert welcome["t"] == "welcome" and welcome["payload"]["protocol"] == 1
        dev.send(ping("real-db"))
        assert dev.recv()["payload"] == {"nonce": "real-db"}
        assert connections.get_registry().is_online(user, device_id)

        devices.revoke_device(user, device_id)            # the web app's revoke button
        started = time.monotonic()
        err = dev.recv()
        assert err["t"] == "error" and err["payload"]["code"] == "device_revoked"
        assert dev.expect_close() == ws_module.CLOSE_DEVICE_REVOKED
        assert time.monotonic() - started < 5
    deadline = time.monotonic() + 5
    while connections.get_registry().is_online(user, device_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not connections.get_registry().is_online(user, device_id)

    with TestClient(app).websocket_connect("/v1/code/device") as sock:    # and it cannot come back
        dev = FakeDevice(sock)
        dev.send(hello(credential))
        assert dev.recv()["payload"]["code"] == "device_revoked"
        assert dev.expect_close() == ws_module.CLOSE_DEVICE_REVOKED
