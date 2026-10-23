"""Real-Postgres checks for S03 (migration 0013): the device-code flow end to
end through the real routes and the real SQL, plus RLS, grants and CHECK
constraints.

The unit tests run the same flow against an in-memory store; this file is what
proves PgStore's statements, the atomic single collection of a credential, the
column-level grants and the RLS policies actually behave.

Skipped unless MINIME_TEST_DATABASE_URL is set. Point it at a scratch database
built exactly as the root README's "Real-database tests" section says (auth.users
stub, then every file in backend/migrations in order, 0000 through 0013),
connecting AS the app role (minime_app), not postgres:

    MINIME_TEST_DATABASE_URL="postgresql://minime_app:pw@localhost:5432/minime_test" \\
        pytest tests/integration/test_devices_pg.py

Like test_workspace_code_files_pg.py (B01) it FAILS, rather than skips, when the
URL connects as a superuser, a BYPASSRLS role, or the owner of the device tables,
or when 0013 is missing or mis-granted: in each case every test below would pass
without grants, RLS or triggers being exercised. The device tables have no
foreign key to auth.users (user ids are plain text), so no auth.users row needs
seeding.

Every test uses its own random user ids and names, so runs do not collide.
devices is revoke-only for minime_app by design, so rows are not cleaned up.
"""
import contextlib
import os
import secrets
import threading
import uuid

import jwt
import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")

SECRET = "pg-test-device-jwt-secret-0123456789-abcdefghijklmnop"


@pytest.fixture(scope="module", autouse=True)
def _connected_as_the_app_role_on_a_migrated_schema():
    """Fail loudly unless DSN is the unprivileged app role and 0013 is applied."""
    import psycopg
    problems = []
    with psycopg.connect(DSN) as conn:
        who, is_super, bypass = conn.execute(
            "select current_user, r.rolsuper, r.rolbypassrls "
            "from pg_roles r where r.rolname = current_user").fetchone()
        if who != "minime_app":
            problems.append(f"connected as {who!r}, not 'minime_app'")
        if is_super:
            problems.append("the role is a superuser (grants and RLS are not enforced)")
        if bypass:
            problems.append("the role has BYPASSRLS")
        owned = [r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' and tableowner = current_user "
            "and tablename in ('devices', 'device_pairings')")]
        if owned:
            problems.append(f"the role OWNS {owned} (owners bypass grants and RLS)")
        exists = [r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' "
            "and tablename in ('devices', 'device_pairings')")]
        if sorted(exists) != ["device_pairings", "devices"]:
            problems.append("migration 0013 is not applied")
        else:
            def can(table, priv):
                return conn.execute("select has_table_privilege(current_user, %s, %s)",
                                    (table, priv)).fetchone()[0]
            if can("devices", "DELETE"):
                problems.append("the role can DELETE FROM devices (0013's grants are wrong or were edited)")
            for table, priv in (("devices", "UPDATE"), ("devices", "INSERT"),
                                ("device_pairings", "UPDATE"), ("device_pairings", "DELETE")):
                if not can(table, priv):
                    problems.append(f"the role lacks {priv} on {table} (0013 not fully applied?)")
            forced = [r[0] for r in conn.execute(
                "select relname from pg_class where relname in ('devices', 'device_pairings') "
                "and relrowsecurity and relforcerowsecurity")]
            if sorted(forced) != ["device_pairings", "devices"]:
                problems.append("RLS is not enabled and forced on both device tables")
    if problems:
        pytest.fail(
            "MINIME_TEST_DATABASE_URL must connect as the unprivileged app role on a database "
            "built per the README with migrations through 0013: " + "; ".join(problems), pytrace=False)


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
            cur.execute(
                "select set_config('app.current_user_id', %s, true), "
                "set_config('app.trusted_internal', %s, true)",
                (user_id or "", "true" if trusted else "false"),
            )
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
def users():
    return f"pg_a_{uuid.uuid4().hex[:10]}", f"pg_b_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def h(pg, users):
    from tests.unit.devices_testkit import Harness
    return Harness(user=users[0])


# ============================================================ the flow, for real

def test_full_flow_against_postgres(h, pg, users):
    user_a, _ = users
    name = f"laptop-{uuid.uuid4().hex[:6]}"
    cred, device_id, start = h.pair(user=user_a, name=name)

    claims = jwt.decode(h.token(cred).json()["jwt"], SECRET, algorithms=["HS256"], audience="m2c-device")
    assert (claims["sub"], claims["device_id"]) == (user_a, device_id)

    (listed,) = h.list(user_a).json()["devices"]
    assert listed["id"] == device_id and listed["device_name"] == name
    assert listed["last_seen_at"] is not None and listed["revoked_at"] is None

    assert h.revoke(device_id, user=user_a).status_code == 200
    assert h.token(cred).status_code == 401
    assert h.list(user_a).json()["devices"] == []
    assert [d["id"] for d in h.list(user_a, include_revoked="true").json()["devices"]] == [device_id]


def test_a_revoked_devices_existing_jwt_stops_verifying(h, users):
    from eo import devices
    cred, device_id, _ = h.pair(user=users[0])
    token = h.token(cred).json()["jwt"]
    assert devices.verify_device_token(token).device_id == device_id
    h.revoke(device_id, user=users[0])
    with pytest.raises(devices.DeviceAuthError):
        devices.verify_device_token(token)


def test_replayed_device_code_fails_and_mints_no_second_device(h, pg, users):
    start = h.start().json()
    h.approve(start["user_code"], user=users[0])
    assert h.poll(start["device_code"]).json()["status"] == "approved"
    for _ in range(3):
        assert h.poll(start["device_code"]).status_code == 400
    with pg(user_id=users[0]) as cur:
        cur.execute("select count(*) as n from devices where user_id = %s", (users[0],))
        assert cur.fetchone()["n"] == 1


def test_concurrent_polls_collect_exactly_one_credential(h, pg, users):
    start = h.start().json()
    h.approve(start["user_code"], user=users[0])
    results, gate = [], threading.Barrier(8)

    def poll():
        gate.wait()
        results.append(h.poll(start["device_code"]))

    threads = [threading.Thread(target=poll) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [r for r in results if r.status_code == 200]
    assert len(winners) == 1 and winners[0].json()["status"] == "approved"
    with pg(user_id=users[0]) as cur:
        cur.execute("select count(*) as n from devices where user_id = %s", (users[0],))
        assert cur.fetchone()["n"] == 1


def test_credential_is_stored_only_as_its_sha256(h, pg, users):
    from tests.unit.devices_testkit import sha256
    cred, device_id, _ = h.pair(user=users[0])
    with pg(user_id=users[0]) as cur:
        cur.execute("select credential_hash from devices where id = %s", (device_id,))
        assert cur.fetchone()["credential_hash"] == sha256(cred)
    with pg(trusted=True) as cur:
        cur.execute("select count(*) as n from device_pairings where device_code_hash = %s or user_code_hash = %s",
                    (cred, cred))
        assert cur.fetchone()["n"] == 0


def test_database_clock_decides_expiry_and_dead_rows_are_purged(h, pg, users):
    from eo import devices
    from tests.unit.devices_testkit import sha256
    code, user_code = secrets.token_urlsafe(32), devices.new_user_code()   # random: rows persist across runs
    with pg(trusted=True) as cur:                       # a pairing that expired 10 minutes ago
        cur.execute(
            "insert into device_pairings (device_code_hash, user_code_hash, device_name, os, client_version,"
            " created_at, expires_at) values (%s, %s, 'n', 'o', 'v',"
            " now() - interval '20 minutes', now() - interval '10 minutes')",
            (sha256(code), sha256(user_code)))
    assert h.poll(code).json() == {"status": "expired"}
    assert h.approve(user_code, user=users[0]).status_code == 404

    old = secrets.token_urlsafe(32)
    with pg(trusted=True) as cur:                       # long dead: swept by the next start
        cur.execute(
            "insert into device_pairings (device_code_hash, user_code_hash, device_name, os, client_version,"
            " created_at, expires_at) values (%s, %s, 'n', 'o', 'v',"
            " now() - interval '3 days', now() - interval '2 days')",
            (sha256(old), sha256(devices.new_user_code())))
    assert h.start().status_code == 200
    assert h.poll(old).status_code == 400
    assert devices.PgStore().pairing_state(device_code_hash=sha256(code)) is not None   # fresh-dead is kept


def test_a_user_code_hash_collision_is_reported_not_raised(pg):
    from eo import devices
    store, base = devices.PgStore(), uuid.uuid4().hex * 2
    args = dict(device_name="n", os_name="o", client_version="v", ttl_seconds=600)
    assert store.create_pairing(device_code_hash=base, user_code_hash=base[::-1], **args) is True
    assert store.create_pairing(device_code_hash=uuid.uuid4().hex * 2, user_code_hash=base[::-1], **args) is False


def test_lockout_and_ownership_through_postgres(h, users):
    user_a, user_b = users
    cred, device_id, start = h.pair(user=user_a)
    assert h.revoke(device_id, user=user_b).status_code == 404
    assert h.token(cred).status_code == 200
    assert h.list(user_b).json()["devices"] == []
    nxt = h.start().json()
    for _ in range(5):
        assert h.approve("BCDF-GHJK", user=user_b).status_code == 404
    assert h.approve(nxt["user_code"], user=user_b).status_code == 429


# ============================================================ RLS

def test_a_user_sees_only_their_own_devices_in_sql(h, pg, users):
    user_a, user_b = users
    _, dev_a, _ = h.pair(user=user_a)
    _, dev_b, _ = h.pair(user=user_b)
    with pg(user_id=user_a) as cur:
        cur.execute("select id::text as id from devices")
        visible = {r["id"] for r in cur.fetchall()}
        assert dev_a in visible and dev_b not in visible
    with pg(user_id=user_a) as cur:
        cur.execute("select distinct user_id from devices")
        assert [r["user_id"] for r in cur.fetchall()] == [user_a]
    with pg() as cur:                                   # no identity -> nothing
        cur.execute("select count(*) as n from devices")
        assert cur.fetchone()["n"] == 0


def test_pairings_are_invisible_and_untouchable_without_the_trusted_flag(h, pg, users):
    import psycopg
    h.start()
    for kw in ({}, {"user_id": users[0]}):
        with pg(**kw) as cur:
            cur.execute("select count(*) as n from device_pairings")
            assert cur.fetchone()["n"] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg(**kw) as cur:
                cur.execute("insert into device_pairings (device_code_hash, user_code_hash, device_name, os,"
                            " client_version, expires_at) values (%s, %s, 'n', 'o', 'v', now() + interval '1 minute')",
                            ("a" * 64, "b" * 64))


def test_a_user_cannot_revoke_another_users_device_in_sql(h, pg, users):
    user_a, user_b = users
    _, dev_a, _ = h.pair(user=user_a)
    with pg(user_id=user_b) as cur:
        cur.execute("update devices set revoked_at = now() where id = %s::uuid", (dev_a,))
        assert cur.rowcount == 0                        # RLS hides the row from the UPDATE
    with pg(user_id=user_a) as cur:
        cur.execute("select revoked_at from devices where id = %s::uuid", (dev_a,))
        assert cur.fetchone()["revoked_at"] is None


def test_devices_cannot_be_inserted_by_an_untrusted_session(pg, users):
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(user_id=users[0]) as cur:
            cur.execute("insert into devices (user_id, device_name, os, client_version, credential_hash)"
                        " values (%s, 'n', 'o', 'v', %s)", (users[0], "a" * 64))


# ============================================================ grants and constraints

def test_the_app_role_has_only_the_table_privileges_the_code_needs(pg):
    with pg(trusted=True) as cur:
        cur.execute("select has_table_privilege('minime_app', 'devices', 'DELETE') as d, "
                    "has_table_privilege('minime_app', 'devices', 'TRUNCATE') as t, "
                    "has_table_privilege('minime_app', 'device_pairings', 'TRUNCATE') as t2")
        row = cur.fetchone()
    assert (row["d"], row["t"], row["t2"]) == (False, False, False)


def test_devices_cannot_be_deleted(h, pg, users):
    import psycopg
    _, dev, _ = h.pair(user=users[0])
    for kw in ({"trusted": True}, {"user_id": users[0]}):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg(**kw) as cur:
                cur.execute("delete from devices where id = %s::uuid", (dev,))


def test_a_devices_identity_columns_are_immutable_for_every_caller(h, pg, users):
    """No column grants (B01's checker cannot model them): the BEFORE UPDATE
    trigger refuses a change to anything but last_seen_at / revoked_at, even on
    the trusted internal path and even for the row's own user."""
    import psycopg
    _, dev, _ = h.pair(user=users[0])
    changes = [
        "user_id = 'someone-else'", "credential_hash = '" + "c" * 64 + "'", "device_name = 'renamed'",
        "os = 'other'", "client_version = '9.9.9'", "created_at = now() - interval '1 year'",
        "id = gen_random_uuid()",
    ]
    for change in changes:
        for kw in ({"trusted": True}, {"user_id": users[0]}):
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as info:
                with pg(**kw) as cur:
                    cur.execute(f"update devices set {change} where id = %s::uuid", (dev,))
            assert "c" * 64 not in str(info.value)               # messages never print row values
    with pg(user_id=users[0]) as cur:                            # the two mutable columns still work
        cur.execute("update devices set last_seen_at = now() where id = %s::uuid", (dev,))
        assert cur.rowcount == 1


def test_revocation_is_permanent(h, pg, users):
    import psycopg
    _, dev, _ = h.pair(user=users[0])
    assert h.revoke(dev, user=users[0]).status_code == 200
    for sql in ("update devices set revoked_at = null where id = %s::uuid",
                "update devices set revoked_at = now() + interval '1 day' where id = %s::uuid"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg(user_id=users[0]) as cur:
                cur.execute(sql, (dev,))
    assert h.revoke(dev, user=users[0]).status_code == 200       # the route stays idempotent


def test_pairing_codes_labels_and_expiry_are_immutable_and_status_only_moves_forward(h, pg, users):
    import psycopg
    from tests.unit.devices_testkit import sha256
    start = h.start().json()
    key = sha256(start["device_code"])

    def attempt(set_clause, *params):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg(trusted=True) as cur:
                cur.execute(f"update device_pairings set {set_clause} where device_code_hash = %s", (*params, key))

    attempt("user_code_hash = %s", "d" * 64)
    attempt("device_code_hash = %s", "e" * 64)
    attempt("expires_at = now() + interval '1 year'")
    attempt("created_at = now() - interval '1 year'")
    attempt("device_name = 'x'")
    attempt("client_version = 'x'")
    attempt("status = 'consumed'")                               # pending -> consumed skips approval
    attempt("user_id = %s", users[0])                            # a user cannot be attached without approving

    assert h.approve(start["user_code"], user=users[0]).status_code == 200
    attempt("status = 'pending'")                                # no going back
    attempt("status = 'denied'")
    attempt("user_id = 'someone-else'")                          # approver cannot be swapped
    assert h.poll(start["device_code"]).json()["status"] == "approved"
    attempt("status = 'approved'")                               # consumed is terminal
    attempt("status = 'pending'")


def test_live_pairings_cannot_be_deleted_only_long_dead_ones(h, pg):
    import psycopg
    from tests.unit.devices_testkit import sha256
    start = h.start().json()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(trusted=True) as cur:
            cur.execute("delete from device_pairings where device_code_hash = %s", (sha256(start["device_code"]),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):    # even just-expired rows are kept a day
        with pg(trusted=True) as cur:
            cur.execute("delete from device_pairings where expires_at < now() + interval '5 minutes'")


def test_check_constraints_reject_bad_rows(pg, users):
    import psycopg
    good = dict(u=users[0], n="n", o="o", v="v", h="a" * 64)
    cases = [
        dict(h="not-hex"), dict(h="A" * 64), dict(h="a" * 63), dict(n=""), dict(n="x" * 65),
        dict(o=""), dict(o="x" * 65), dict(v=""), dict(v="x" * 33),
    ]
    for override in cases:
        row = {**good, **override}
        with pytest.raises(psycopg.errors.CheckViolation):
            with pg(trusted=True) as cur:
                cur.execute("insert into devices (user_id, device_name, os, client_version, credential_hash)"
                            " values (%s, %s, %s, %s, %s)", (row["u"], row["n"], row["o"], row["v"], row["h"]))
    with pytest.raises(psycopg.errors.CheckViolation):  # approved pairing must name its user
        with pg(trusted=True) as cur:
            cur.execute("insert into device_pairings (device_code_hash, user_code_hash, device_name, os,"
                        " client_version, status, expires_at) values (%s, %s, 'n', 'o', 'v', 'approved',"
                        " now() + interval '1 minute')", (uuid.uuid4().hex * 2, uuid.uuid4().hex * 2))
    with pytest.raises(psycopg.errors.CheckViolation):  # unknown status
        with pg(trusted=True) as cur:
            cur.execute("insert into device_pairings (device_code_hash, user_code_hash, device_name, os,"
                        " client_version, status, expires_at) values (%s, %s, 'n', 'o', 'v', 'bogus',"
                        " now() + interval '1 minute')", (uuid.uuid4().hex * 2, uuid.uuid4().hex * 2))


def test_the_two_tables_have_no_foreign_keys(pg):
    with pg(trusted=True) as cur:
        cur.execute("select count(*) as n from information_schema.table_constraints"
                    " where table_name in ('devices', 'device_pairings') and constraint_type = 'FOREIGN KEY'")
        assert cur.fetchone()["n"] == 0
