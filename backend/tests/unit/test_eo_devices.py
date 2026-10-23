"""eo/devices.py (S03): the pieces that need no HTTP -- user-code alphabet and
normalisation, signing-secret rules, and what PgStore sends to the database.
No real database: the cursor is a recorder (same approach as
test_usage_ledger.py), so these pin down the SQL's safety properties; the
real-Postgres behaviour is in tests/integration/test_devices_pg.py.
"""
import contextlib

import pytest

from eo import devices
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET


# ---- user codes ------------------------------------------------------

def test_alphabet_is_unambiguous_and_vowel_free():
    alphabet = set(devices.USER_CODE_ALPHABET)
    assert len(alphabet) == len(devices.USER_CODE_ALPHABET) == 20
    assert not alphabet & set("AEIOU01")                  # no vowels (no words), no 0/O, 1/I look-alikes
    assert alphabet == set("BCDFGHJKLMNPQRSTVWXZ")


def test_generated_codes_use_only_the_alphabet_and_are_8_long():
    codes = {devices.new_user_code() for _ in range(2000)}
    assert len(codes) > 1990                              # random, not constant
    for code in codes:
        assert len(code) == devices.USER_CODE_LENGTH and set(code) <= set(devices.USER_CODE_ALPHABET)


def test_format_and_normalize_round_trip():
    assert devices.format_user_code("BCDFGHJK") == "BCDF-GHJK"
    for typed in ("BCDF-GHJK", "bcdf-ghjk", " bcdf ghjk ", "BCDFGHJK", "BCDF - GHJK"):
        assert devices.normalize_user_code(typed) == "BCDFGHJK"


@pytest.mark.parametrize("typed", [
    None, "", "BCDF-GHJ", "BCDF-GHJKL", "BCDF-GHJA", "BCDF-GHJ0", "BCDF_GHJK",
    "ＢＣＤＦ-ＧＨＪＫ", "ß" * 8, "B" * 65, 12345678,
])
def test_normalize_rejects_anything_that_cannot_be_a_code(typed):
    assert devices.normalize_user_code(typed) is None


# ---- signing secret --------------------------------------------------

def test_secret_rules(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    assert devices._device_jwt_secret() == SECRET
    for bad in ("", "x" * (devices.MIN_SECRET_LENGTH - 1), SUPABASE_SECRET):
        monkeypatch.setenv("DEVICE_JWT_SECRET", bad)
        with pytest.raises(devices.NotConfigured) as info:
            devices._device_jwt_secret()
        assert bad not in str(info.value) or bad == ""
    monkeypatch.delenv("DEVICE_JWT_SECRET")
    with pytest.raises(devices.NotConfigured):
        devices._device_jwt_secret()


def test_verification_url_default_is_the_web_apps_origin(monkeypatch):
    monkeypatch.delenv("DEVICE_VERIFICATION_URL", raising=False)
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.test/, http://localhost:3000")
    assert devices.verification_url() == "https://app.example.test"
    monkeypatch.setenv("ALLOWED_ORIGINS", " , ,http://127.0.0.1:3000")        # blank items are skipped (B07's parsing)
    assert devices.verification_url() == "http://127.0.0.1:3000"
    for unusable in ("", " , ", "*"):
        monkeypatch.setenv("ALLOWED_ORIGINS", unusable)
        assert devices.verification_url() == "http://localhost:3000"
    monkeypatch.setenv("DEVICE_VERIFICATION_URL", "https://app.example.test/settings")
    assert devices.verification_url() == "https://app.example.test/settings"


# ---- PgStore: what it sends ------------------------------------------

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

    def fetchall(self):
        return self.rows


def test_create_pairing_never_surfaces_a_unique_violation(monkeypatch):
    rec = Recorder(rows=[None]).install(monkeypatch)
    assert devices.PgStore().create_pairing(
        device_code_hash="d" * 64, user_code_hash="u" * 64, device_name="n",
        os_name="o", client_version="v", ttl_seconds=600) is False
    sql, params = rec.calls[0]
    assert "on conflict do nothing" in sql          # a raised IntegrityError's DETAIL would print the hash
    assert params[0] == "d" * 64 and 600 in params
    assert rec.identity == [(None, True)]


def test_claim_is_one_transaction_that_flips_then_inserts(monkeypatch):
    rec = Recorder(rows=[
        {"user_id": "u1", "device_name": "n", "os": "o", "client_version": "v"},
        {"id": "00000000-0000-4000-8000-000000000001"},
    ]).install(monkeypatch)
    out = devices.PgStore().claim(device_code_hash="d" * 64, credential_hash="c" * 64)
    assert out == {"device_id": "00000000-0000-4000-8000-000000000001", "user_id": "u1"}
    assert len(rec.identity) == 1                    # ONE cursor == one transaction
    first, second = (c[0] for c in rec.calls)
    assert "status = 'consumed'" in first and "status = 'approved'" in first and "expires_at > now()" in first
    assert first.startswith("update device_pairings") and second.startswith("insert into devices")


def test_claim_inserts_nothing_when_there_is_nothing_to_collect(monkeypatch):
    rec = Recorder(rows=[None]).install(monkeypatch)
    assert devices.PgStore().claim(device_code_hash="d" * 64, credential_hash="c" * 64) is None
    assert len(rec.calls) == 1


def test_token_exchange_checks_revocation_in_the_same_statement_as_the_stamp(monkeypatch):
    rec = Recorder(rows=[None]).install(monkeypatch)
    assert devices.PgStore().touch_active_by_credential(credential_hash="c" * 64) is None
    (sql, params), = rec.calls
    assert sql.startswith("update devices set last_seen_at = now()")
    assert "revoked_at is null" in sql and params == ("c" * 64,)


def test_user_facing_queries_run_as_that_user_so_rls_applies(monkeypatch):
    rec = Recorder(rows=[]).install(monkeypatch)
    devices.PgStore().list_devices(user_id="u1", include_revoked=False)
    devices.PgStore().revoke(user_id="u1", device_id="00000000-0000-4000-8000-000000000001")
    assert rec.identity == [("u1", False), ("u1", False)]
    assert "revoked_at is null" in rec.calls[0][0]


def test_no_statement_interpolates_a_value_into_sql(monkeypatch):
    """Secrets and ids are always bound parameters (%s), never formatted in."""
    import inspect
    source = inspect.getsource(devices.PgStore)
    assert 'f"' not in source and "f'" not in source and ".format(" not in source and "% (" not in source
