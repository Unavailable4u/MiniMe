"""
Patch I01 -- the walking skeleton against a REAL Postgres.

The end-to-end stack of test_hello_run_e2e.py (real device socket, S14, hello.py, the real C04
client and REPL) with the two database-backed stores swapped for the real thing: the `runs` table
(S06, migration 0014) and the usage ledger (S01, migration 0012). Redis stays the in-memory twin
and the model stays S05's fake provider; device authentication stays the in-memory device store.

Same gating and pattern as test_usage_ledger_pg.py / test_runs_pg.py: skipped unless
MINIME_TEST_DATABASE_URL is set, pointing at a scratch database with every migration through 0014
applied (the root README's "Real-database tests" section), connecting AS the app role so grants and
row-level security are enforced. Every test uses its own random user, so nothing needs cleaning up.

    MINIME_TEST_DATABASE_URL="postgresql://minime_app:pw@localhost:5432/minime_test" \\
        pytest tests/integration/test_hello_run_pg.py

What it checks (work package I01): the run row, the ledger row (surface 'm2c', the right user,
credits equal to S02's figure, numbers only), the pool going down, and that NO row in any table
the app role can read contains the message or the reply.
"""
import asyncio
import contextlib
import os
import uuid

import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")

if DSN:
    from tests.integration import hello_testkit as k
    from tests.integration.hello_testkit import Session
    from tests.unit.test_codeagent_adapter import text_reply


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def _app_role_on_a_migrated_schema():
    import psycopg
    problems = []
    with psycopg.connect(DSN) as conn:
        who, is_super, bypass = conn.execute(
            "select current_user, r.rolsuper, r.rolbypassrls from pg_roles r where r.rolname = current_user"
        ).fetchone()
        if who != "minime_app":
            problems.append(f"connected as {who!r}, not 'minime_app'")
        if is_super or bypass:
            problems.append("the role is a superuser or has BYPASSRLS (grants and RLS not enforced)")
        have = {r[0] for r in conn.execute(
            "select tablename from pg_tables where schemaname = 'public' "
            "and tablename in ('runs', 'usage_ledger', 'devices')")}
        for table in ("runs", "usage_ledger", "devices"):
            if table not in have:
                problems.append(f"table {table!r} is missing (migrations 0012 to 0014)")
    if problems:
        pytest.fail("MINIME_TEST_DATABASE_URL must connect as the unprivileged app role on a database built "
                    "per the README through 0014: " + "; ".join(problems), pytrace=False)


@pytest.fixture
def cursor():
    import psycopg
    from psycopg.rows import dict_row

    @contextlib.contextmanager
    def cur_ctx(user_id=None, trusted=False):
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

    return cur_ctx


@pytest.fixture
def user():
    return f"pg_user_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def pgstack(monkeypatch, tmp_path, cursor, user):
    s = k.new_stack(monkeypatch, tmp_path, db_cursor=cursor, user_id=user)
    try:
        yield s
    finally:
        s.close()


def rows_mentioning(cursor, user, needle):
    """(tables scanned, [(table, count)] of tables with a row whose text contains `needle`), over every
    public table the app role may SELECT, read as the user and as the trusted server."""
    with cursor(user_id=user, trusted=True) as cur:
        cur.execute("select table_name from information_schema.role_table_grants "
                    "where grantee = current_user and privilege_type = 'SELECT' and table_schema = 'public' "
                    "group by table_name order by table_name")
        tables = [r["table_name"] for r in cur.fetchall()]
    hits, scanned = [], 0
    for table in tables:
        try:
            with cursor(user_id=user, trusted=True) as cur:
                cur.execute(f'select count(*) as n from public."{table}" t where t::text like %s', (f"%{needle}%",))
                n = cur.fetchone()["n"]
        except Exception:
            continue                                    # a table the role cannot read in full
        scanned += 1
        if n:
            hits.append((table, n))
    return scanned, hits


def test_a_hello_run_leaves_a_run_row_a_ledger_row_and_no_content(pgstack, cursor, user):
    from codeagent import runs
    from eo import credits as credits_module, usage_ledger

    pgstack.provider.push(text_reply(k.REPLY_TEXT_CANARY, prompt=300, completion=40, cached=100))
    before = usage_ledger.usage_summary(user)

    async def scenario():
        async with Session(pgstack) as s:
            s.say(k.USER_TEXT_CANARY)
            await s.reply_count(1)
            assert s.repl.replies == [k.REPLY_TEXT_CANARY]
            run_id = s.repl.run_id
            # while it runs: the run row, the one ledger row, the pool
            row = runs.get_run(user, run_id)
            assert (row.state, row.profile, row.mode, row.device_id) == ("running", "code", "ask", pgstack.device_id)
            assert row.error_code is None and row.ended_at is None
            await s.leave()
            return run_id

    run_id = go(scenario())

    with cursor(user_id=user) as cur:
        cur.execute("select * from usage_ledger where user_id = %s", (user,))
        ledger = cur.fetchall()
    (entry,) = ledger                                                      # exactly ONE row
    assert entry["surface"] == "m2c" and entry["user_id"] == user and entry["run_id"] == run_id
    assert (entry["input_tokens"], entry["cached_input_tokens"], entry["output_tokens"]) == (300, 100, 40)
    expected = credits_module.credits_for(entry["provider"], entry["model"], 300, 100, 40)
    assert float(entry["credits"]) == pytest.approx(expected, abs=1e-6) and expected > 0
    for column, value in entry.items():                                    # numbers and identifiers only
        assert not isinstance(value, str) or len(value) < 200, f"{column} is suspiciously long"
        assert k.USER_TEXT_CANARY not in str(value) and k.REPLY_TEXT_CANARY not in str(value)

    after = usage_ledger.usage_summary(user)                               # the pool decreased by that figure
    assert after["by_surface"]["m2c"] == pytest.approx(expected, abs=1e-6)
    assert after["remaining"] == pytest.approx(before["remaining"] - expected, abs=1e-6)

    final = runs.get_run(user, run_id)                                     # cancel ended it; the counter matches
    assert final.state == "cancelled" and final.ended_at is not None
    assert float(final.credits) == pytest.approx(expected, abs=1e-6)

    for canary in (k.USER_TEXT_CANARY, k.REPLY_TEXT_CANARY):
        scanned, hits = rows_mentioning(cursor, user, canary)
        assert scanned >= 2, "the scan did not reach the runs and usage_ledger tables"
        assert hits == [], f"message or reply text found in rows of {hits}"


def test_quota_exhausted_parks_the_run_in_the_real_table(pgstack, cursor, user):
    """Use up the pool with real rows, send one message through the raw link: the model is not called,
    the error goes out, and `runs` shows paused_budget with error_code quota_exhausted (the real CHECK
    constraints and the state machine accept it)."""
    from codeagent import runs
    from eo import plans, usage_ledger
    from m2c.link import BackoffPolicy, DeviceLink, LinkCallbacks

    usage_ledger.commit(user_id=user, surface="web", provider="groq", model="openai/gpt-oss-120b",
                        credits=plans.pool_for("free") + 1)
    assert usage_ledger.check(user).allowed is False

    async def scenario():
        errors = []
        store = pgstack.open_store("quota-home")
        link = DeviceLink(store, credential_loader=pgstack.credential,
                          callbacks=LinkCallbacks(on_error=errors.append),
                          backoff=BackoffPolicy(base_s=0.05, cap_s=0.2, stable_after_s=0.1),
                          keepalive_interval_s=None)
        link.start()
        try:
            await link.wait_connected(15)
            run_id = await link.start_run(project_path=str(pgstack.cwd), mode="ask", cwd_label="my-project",
                                          instructions="", profile="code")
            await link.send_user_message(run_id, k.USER_TEXT_CANARY)
            await k.await_for(lambda: errors and runs.get_run(user, run_id).state == "paused_budget",
                              what="paused_budget")
            row = runs.get_run(user, run_id)
            assert row.error_code == "quota_exhausted" and row.ended_at is None
            assert [e.code for e in errors] == ["quota_exhausted"]
            await link.send_cancel(run_id)
        finally:
            await link.aclose()
            store.close()

    go(scenario())
    assert pgstack.provider.requests == []
    scanned, hits = rows_mentioning(cursor, user, k.USER_TEXT_CANARY)
    assert scanned >= 2 and hits == []


def test_the_scan_finds_text_when_it_is_there(cursor, user, monkeypatch):
    """Guards the guard: the 'no row contains the text' checks above must be able to fail."""
    from eo import db as eo_db, usage_ledger
    planted = f"planted-{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(eo_db, "cursor", cursor)
    usage_ledger.commit(user_id=user, surface="web", provider="groq", model=planted, credits=0.0)
    scanned, hits = rows_mentioning(cursor, user, planted)
    assert scanned >= 2 and ("usage_ledger", 1) in hits
