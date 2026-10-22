"""Real-Postgres checks for S01 (migration 0012): the ledger, the plan pool,
RLS, and GET /api/usage.

The unit tests use a recording cursor, so neither the migration's grants nor
its RLS policies nor its CHECK constraints are ever exercised. This file does.

Skipped unless MINIME_TEST_DATABASE_URL is set. Point it at a scratch database
that already has migration 0012 applied, connecting AS the app role
(minime_app), not postgres, so grants and RLS are enforced:

    MINIME_TEST_DATABASE_URL="postgresql://minime_app:pw@localhost/minime_test" \\
        pytest tests/integration/test_usage_ledger_pg.py

Every test uses its own random user ids, so runs do not collide and need no
cleanup (usage_ledger is append-only for minime_app by design).
"""
import contextlib
import os
import uuid
from datetime import datetime, timezone

import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")


@pytest.fixture
def pg(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from eo import plans, usage_ledger

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

    monkeypatch.setattr(usage_ledger.db, "cursor", cursor)
    monkeypatch.setattr(plans.db, "cursor", cursor)
    plans.reload_config()
    return cursor


@pytest.fixture
def user():
    return f"pg_user_{uuid.uuid4().hex[:10]}"


def _commit(user_id, surface="web", credits=1.0, **kw):
    from eo import usage_ledger
    usage_ledger.commit(user_id=user_id, surface=surface, provider="groq",
                        model="openai/gpt-oss-120b", credits=credits, **kw)


def test_commit_then_summary_matches_the_sum_of_the_rows(pg, user):
    from eo import plans, usage_ledger
    _commit(user, "web", 3.5, session_id="s1", input_tokens=100, output_tokens=50, cost_usd=0.0035)
    _commit(user, "web", 1.25)
    _commit(user, "m2c", 2.0, run_id="run-1")

    s = usage_ledger.usage_summary(user)
    assert s["by_surface"] == {"web": 4.75, "m2c": 2.0, "menux": 0.0}
    assert s["used"] == 6.75
    assert s["pool"] == plans.pool_for("free")
    assert s["remaining"] == round(s["pool"] - 6.75, 6)

    with pg(user_id=user) as cur:                       # the rows themselves
        cur.execute("select sum(credits) as c, count(*) as n from usage_ledger")
        row = cur.fetchone()
    assert (float(row["c"]), row["n"]) == (6.75, 3)


def test_pool_decreases_as_calls_are_committed(pg, user):
    from eo import usage_ledger
    before = usage_ledger.usage_summary(user)["remaining"]
    _commit(user, credits=10)
    assert usage_ledger.usage_summary(user)["remaining"] == round(before - 10, 6)


def test_a_user_reads_only_their_own_rows(pg, user):
    other = f"pg_user_{uuid.uuid4().hex[:10]}"
    _commit(user, credits=1)
    _commit(other, credits=99)
    with pg(user_id=user) as cur:
        cur.execute("select distinct user_id from usage_ledger")
        assert [r["user_id"] for r in cur.fetchall()] == [user]
    with pg() as cur:                                   # no identity -> nothing
        cur.execute("select count(*) as n from usage_ledger")
        assert cur.fetchone()["n"] == 0


def test_system_rows_are_invisible_to_users_and_visible_to_trusted(pg, user):
    from eo import usage_ledger
    marker = f"sys-{uuid.uuid4().hex[:8]}"
    usage_ledger.commit(user_id=None, surface="system", provider="groq",
                        session_id=marker, credits=0.5)
    with pg(user_id=user) as cur:
        cur.execute("select count(*) as n from usage_ledger where session_id = %s", (marker,))
        assert cur.fetchone()["n"] == 0
    with pg(trusted=True) as cur:
        cur.execute("select user_id, surface from usage_ledger where session_id = %s", (marker,))
        assert [(r["user_id"], r["surface"]) for r in cur.fetchall()] == [(None, "system")]


def test_the_app_role_cannot_rewrite_history_or_insert_untrusted(pg, user):
    import psycopg
    _commit(user, credits=1)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(trusted=True) as cur:
            cur.execute("update usage_ledger set credits = 0 where user_id = %s", (user,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(trusted=True) as cur:
            cur.execute("delete from usage_ledger where user_id = %s", (user,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(user_id=user) as cur:                   # not trusted -> RLS blocks the INSERT
            cur.execute("insert into usage_ledger (user_id, surface, provider, credits) "
                        "values (%s, 'web', 'groq', 1)", (user,))


def test_check_constraints_reject_bad_rows(pg, user):
    import psycopg
    for user_id, surface in ((user, "system"), (None, "web"), (user, "mobile")):
        with pytest.raises(psycopg.errors.CheckViolation):
            with pg(trusted=True) as cur:
                cur.execute("insert into usage_ledger (user_id, surface, provider, credits) "
                            "values (%s, %s, 'groq', 1)", (user_id, surface))
    with pytest.raises(psycopg.errors.CheckViolation):    # negative credits
        with pg(trusted=True) as cur:
            cur.execute("insert into usage_ledger (user_id, surface, provider, credits) "
                        "values (%s, 'web', 'groq', -1)", (user,))


def test_a_user_plans_row_overrides_the_default_pool(pg, user):
    from eo import usage_ledger
    with pg(trusted=True) as cur:
        cur.execute(
            "insert into user_plans (user_id, plan_key, credit_pool, period_start, period_end) "
            "values (%s, 'basic', 500, %s, %s)",
            (user, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2099, 1, 1, tzinfo=timezone.utc)))
    s = usage_ledger.usage_summary(user)
    assert (s["plan_key"], s["pool"]) == ("basic", 500.0)


def test_a_user_cannot_write_their_own_plan(pg, user):
    import psycopg
    with pytest.raises(psycopg.Error):
        with pg(user_id=user) as cur:
            cur.execute(
                "insert into user_plans (user_id, plan_key, credit_pool, period_start, period_end) "
                "values (%s, 'ultimate', 99999, now(), now() + interval '1 month')", (user,))


def test_check_reports_quota_exhausted_from_real_rows(pg, user):
    from eo import plans, usage_ledger
    _commit(user, credits=plans.pool_for("free") + 1)      # overspend the free pool
    r = usage_ledger.check(user, 0.001)
    assert (r.status, r.allowed, r.remaining) == ("quota_exhausted", False, 0.0)


def test_get_usage_route_matches_the_rows(pg, user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.deps import require_auth
    from api.routes.usage import router

    _commit(user, "web", 2.5)
    _commit(user, "menux", 1.5)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: user
    body = TestClient(app).get("/api/usage").json()
    assert body["used"] == 4.0
    assert body["by_surface"] == {"web": 2.5, "m2c": 0.0, "menux": 1.5}
    assert set(body) == {"plan_key", "status", "pool", "used", "remaining",
                         "period_start", "resets_at", "by_surface"}


def test_a_broken_ledger_does_not_break_the_llm_call(pg, user, monkeypatch):
    """With a real (and then broken) database behind it: calls keep working."""
    from eo import usage_context, usage_ledger
    with usage_context.scope(user, "web", "s-pg"):
        usage_ledger.record_call(provider="groq", model="openai/gpt-oss-120b", session_id="s-pg",
                                 input_tokens=10, output_tokens=5, cost_usd=0.0001)
    assert usage_ledger.usage_summary(user)["by_surface"]["web"] > 0

    monkeypatch.setattr(usage_ledger.db, "cursor",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("pg gone")))
    usage_ledger.record_call(provider="groq", model="m", session_id=None,
                             input_tokens=1, output_tokens=1, cost_usd=0.1)   # must not raise
