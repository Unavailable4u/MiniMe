"""S03 migration 0013: invariants checked on the SQL text itself, so they hold
even where no database is available (CI). The real-Postgres behaviour of the
same properties is in tests/integration/test_devices_pg.py.

  * both tables exist, with RLS enabled AND forced;
  * grants live in the same file, are TABLE-LEVEL only (B01's grants checker,
    tests/static/test_sql_grants.py, cannot model column-level grants or DO
    blocks), go only to minime_app, and nothing grants DELETE on devices;
  * the immutability the grants cannot express is carried by BEFORE UPDATE
    (and, for pairings, BEFORE DELETE) triggers whose functions are plain
    CREATE FUNCTION statements;
  * no foreign keys, and no link to usage_ledger;
  * the file is safe to paste into the Supabase SQL editor: no psql
    meta-commands, no transaction control, re-runnable statements;
  * B01's replay of ALL migrations still works with 0013 present and derives
    the intended net privileges.
"""
import pathlib
import re

from tests.static.test_sql_grants import derive_grants, split_statements

SQL = (pathlib.Path(__file__).resolve().parents[2] / "migrations" / "0013_m2c_devices.sql").read_text()
CODE = "\n".join(line for line in SQL.splitlines() if not line.lstrip().startswith("--"))
STATEMENTS = split_statements(SQL)
TABLES = ("devices", "device_pairings")


def test_every_table_is_created_idempotently_with_rls_enabled_and_forced():
    for table in TABLES:
        assert f"create table if not exists {table} " in CODE
        assert f"alter table {table} enable row level security;" in CODE
        assert f"alter table {table} force row level security;" in CODE


def test_every_policy_is_dropped_before_it_is_created():
    created = re.findall(r"create policy (\w+) on", CODE)
    assert created
    for name in created:
        assert f"drop policy if exists {name} on" in CODE


def test_b01s_replay_of_every_migration_accepts_0013_and_derives_the_intended_grants():
    ledger = derive_grants()          # raises UnsupportedGrantSyntax on anything it cannot model
    assert ledger.effective("devices") == {"select", "insert", "update"}               # never delete
    assert ledger.effective("device_pairings") == {"select", "insert", "update", "delete"}
    assert {"devices", "device_pairings"} <= ledger.known


def test_grants_are_in_this_file_table_level_only_and_scoped():
    grants = [s for s in STATEMENTS if s.lower().startswith(("grant ", "revoke "))]
    assert grants
    for stmt in grants:
        low = stmt.lower()
        assert "(" not in low.split(" on ")[0], f"column-level privilege: {stmt}"
        assert "all tables in schema" not in low
        assert re.search(r" (?:to|from) minime_app$", low), stmt
        assert re.search(r" on (?:devices|device_pairings) ", low), stmt
    assert "grant select, insert, update on devices to minime_app" in [g.lower() for g in grants]
    assert "grant select, insert, update, delete on device_pairings to minime_app" in [g.lower() for g in grants]


def test_grants_never_precede_the_creation_of_their_table():
    for table in TABLES:
        created = next(i for i, s in enumerate(STATEMENTS) if s.lower().startswith(f"create table if not exists {table} "))
        touching = [i for i, s in enumerate(STATEMENTS)
                    if re.match(rf"(grant|revoke) .* on {table} ", s.lower())]
        assert touching and min(touching) > created


def test_no_do_blocks_no_default_privileges_no_rename():
    for stmt in STATEMENTS:
        low = stmt.lower()
        assert not low.startswith("do "), stmt[:60]
        assert not low.startswith("alter default privileges")
        assert " rename to " not in low
    assert not re.search(r"\b(grant|revoke)\b", "\n".join(
        s for s in STATEMENTS if s.lower().startswith("create or replace function")).lower())


def test_devices_can_never_be_deleted():
    assert not re.search(r"grant[^;]*\bdelete\b[^;]* on devices ", CODE)
    assert "revoke all on devices from minime_app;" in CODE
    assert "revoke all on device_pairings from minime_app;" in CODE


def test_immutability_is_enforced_by_triggers_written_as_plain_functions():
    for name in ("devices_guard_update", "device_pairings_guard_update", "device_pairings_guard_delete"):
        assert any(s.lower().startswith(f"create or replace function {name}() returns trigger") for s in STATEMENTS), name
        assert f"drop trigger if exists {name} on" in CODE
        assert re.search(rf"create trigger {name}\s+before\s+(?:update|delete)\s+on\s+\w+\s+for\s+each\s+row\s+execute\s+function\s+{name}\(\)", CODE)
    guard = next(s for s in STATEMENTS if "create or replace function devices_guard_update" in s.lower())
    for column in ("user_id", "credential_hash", "device_name", "os", "client_version", "created_at"):
        assert f"new.{column} is distinct from old.{column}" in guard
    assert "old.revoked_at is not null" in guard                  # revocation is permanent
    pairing = next(s for s in STATEMENTS if "create or replace function device_pairings_guard_update" in s.lower())
    for column in ("device_code_hash", "user_code_hash", "expires_at", "created_at"):
        assert f"new.{column} is distinct from old.{column}" in pairing
    # a trigger message must never print a row value (a pairing row holds code hashes):
    # every message is a fixed string with no format placeholder and no DETAIL/HINT
    for fn in (guard, pairing):
        messages = re.findall(r"raise exception '([^']*)'", fn)
        assert messages and all("%" not in m for m in messages)
        assert " detail " not in fn.lower() and " hint " not in fn.lower()


def test_no_foreign_keys_and_no_link_to_the_usage_ledger():
    assert not re.search(r"\breferences\s+\w+\s*\(|foreign\s+key\s*\(", CODE, re.I)
    assert "usage_ledger" not in CODE
    assert "auth.users" not in CODE


def test_secrets_are_stored_only_as_constrained_hashes():
    assert CODE.count("~ '^[0-9a-f]{64}$'") == 3             # credential, device code, user code
    for forbidden in ("credential text", "device_code text", "user_code text", " password"):
        assert forbidden not in CODE


def test_safe_to_run_by_hand():
    assert not re.search(r"^\s*\\", SQL, re.M)                # no psql meta-commands
    assert not re.search(r"\b(begin|commit|rollback)\s*;", CODE, re.I)
    assert "Supabase SQL editor" in SQL and "more than once" in SQL
    for stmt in re.findall(r"create (?:table|index) (?:if not exists )?\w+", CODE):
        assert "if not exists" in stmt
