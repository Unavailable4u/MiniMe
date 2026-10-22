"""eo/usage_ledger.py and the llm_client hook (S01). No real database: the
cursor is a recorder, so these pin down WHAT is written and that failures
never escape. The real-Postgres checks live in
tests/integration/test_usage_ledger_pg.py.
"""
import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eo import plans, usage_context, usage_ledger
from utils import llm_client

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


class Recorder:
    """Stand-in for db.cursor(): records every execute(), serves canned rows."""
    def __init__(self, rows=None, row=None):
        self.calls, self.rows, self.row = [], rows or [], row

    def install(self, monkeypatch):
        @contextlib.contextmanager
        def cursor(user_id=None, trusted=False):
            self.last_identity = (user_id, trusted)
            yield self
        monkeypatch.setattr(usage_ledger.db, "cursor", cursor)
        monkeypatch.setattr(plans.db, "cursor", cursor)
        return self

    def execute(self, sql, params=()): self.calls.append((" ".join(sql.split()), params))
    def fetchall(self): return self.rows
    def fetchone(self): return self.row


@pytest.fixture(autouse=True)
def _config():
    plans.reload_config()
    yield
    plans.reload_config()


# ---- credits ---------------------------------------------------------

def test_priced_call_is_cost_times_credits_per_usd():
    assert usage_ledger.credits_for_call(0.002, 100, 50) == pytest.approx(2.0)


def test_unpriced_call_is_charged_a_conservative_rate_never_free():
    # 1M tokens at the configured unpriced rate ($1/M) = $1 = 1000 credits
    assert usage_ledger.credits_for_call(None, 600_000, 400_000) == pytest.approx(1000.0)


def test_call_with_nothing_measured_is_zero_credits():
    assert usage_ledger.credits_for_call(None, None, None) == 0.0


def test_a_partial_token_split_still_charges_the_known_half():
    assert usage_ledger.credits_for_call(None, None, 1_000_000) == pytest.approx(1000.0)


# ---- cached tokens ----------------------------------------------------

def test_cached_tokens_from_sdk_object_dict_and_absence():
    sdk = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=40))
    assert usage_ledger.cached_tokens_from_usage(sdk) == 40
    assert usage_ledger.cached_tokens_from_usage({"prompt_tokens_details": {"cached_tokens": 7}}) == 7
    assert usage_ledger.cached_tokens_from_usage(SimpleNamespace(prompt_tokens=5)) is None
    assert usage_ledger.cached_tokens_from_usage(None) is None
    assert usage_ledger.cached_tokens_from_usage({"cached_tokens": "x"}) is None


# ---- commit -----------------------------------------------------------

def test_commit_inserts_numbers_and_ids_only(monkeypatch):
    rec = Recorder().install(monkeypatch)
    out = usage_ledger.commit(user_id="u1", surface="web", provider="groq", model="m",
                              session_id="s1", input_tokens=10, output_tokens=5, cost_usd=0.001)
    sql, params = rec.calls[0]
    assert sql.startswith("insert into usage_ledger") and rec.last_identity == (None, True)
    assert params == ("u1", "web", "s1", None, "groq", "m", 10, None, 5, 1.0, 0.001)
    assert out == {"credits": 1.0}


@pytest.mark.parametrize("user_id,surface", [("u1", "system"), (None, "web"), ("u1", "mobile")])
def test_commit_rejects_a_malformed_user_surface_pair(user_id, surface):
    with pytest.raises(ValueError):
        usage_ledger.commit(user_id=user_id, surface=surface, provider="groq")


# ---- record_call: attribution + fail-open -----------------------------

def _record(**over):
    kw = dict(provider="groq", model="openai/gpt-oss-120b", session_id="s1",
              input_tokens=1000, output_tokens=500, cost_usd=0.0004)
    kw.update(over)
    usage_ledger.record_call(**kw)


def test_record_call_attributes_to_the_open_scope(monkeypatch):
    rec = Recorder().install(monkeypatch)
    with usage_context.scope("u1", "m2c", "s1"):
        _record()
    params = rec.calls[0][1]
    assert (params[0], params[1], params[2]) == ("u1", "m2c", "s1")


def test_record_call_with_no_owner_is_system_with_null_user(monkeypatch):
    rec = Recorder().install(monkeypatch)
    _record()
    assert rec.calls[0][1][:2] == (None, "system")


def test_record_call_never_raises_when_the_database_fails(monkeypatch, capsys):
    def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(usage_ledger.db, "cursor", boom)
    _record()                                  # must not raise
    assert "usage-ledger write failed (non-fatal)" in capsys.readouterr().out


def test_record_call_never_raises_when_attribution_fails(monkeypatch):
    monkeypatch.setattr(usage_ledger.usage_context, "resolve",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    _record()                                  # must not raise


# ---- llm_client hook --------------------------------------------------

class _Usage:
    prompt_tokens, completion_tokens, total_tokens = 1000, 500, 1500
    prompt_tokens_details = SimpleNamespace(cached_tokens=800)


def test_hook_writes_tokens_cost_and_cached_tokens(monkeypatch):
    rec = Recorder().install(monkeypatch)
    with usage_context.scope("u1", "web", "s1"):
        llm_client._record_usage_ledger(provider="groq", model="openai/gpt-oss-120b",
                                        agent_name="A", session_id="s1", usage=_Usage())
    p = rec.calls[0][1]
    assert p[0] == "u1" and p[1] == "web"
    assert p[6:9] == (1000, 800, 500)           # input, cached input, output
    assert p[10] == llm_client._compute_cost_usd("groq", "openai/gpt-oss-120b", 1000, 500)


def test_hook_survives_the_ledger_raising_anything(monkeypatch, capsys):
    monkeypatch.setattr(llm_client._usage_ledger, "record_call",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("ledger exploded")))
    llm_client._record_usage_ledger(provider="groq", model="m", agent_name="A",
                                    session_id=None, usage=_Usage())
    assert "usage-ledger logging failed (non-fatal)" in capsys.readouterr().out


def test_hook_survives_a_garbage_usage_object(monkeypatch):
    Recorder().install(monkeypatch)
    llm_client._record_usage_ledger(provider="groq", model="m", agent_name="A",
                                    session_id=None, usage=object())


# ---- summary + check --------------------------------------------------

def _rows(**by_surface):
    return [{"surface": k, "credits": v} for k, v in by_surface.items()]


def test_summary_total_is_exactly_the_sum_of_the_breakdown(monkeypatch):
    Recorder(rows=_rows(web=10.5, m2c=4.25), row=None).install(monkeypatch)
    s = usage_ledger.usage_summary("u1", NOW)
    assert s["used"] == 14.75 == sum(s["by_surface"].values())
    assert s["by_surface"] == {"web": 10.5, "m2c": 4.25, "menux": 0.0}
    assert s["pool"] == plans.pool_for("free")
    assert s["remaining"] == round(s["pool"] - 14.75, 6)
    assert s["resets_at"].startswith("2026-11-01")


def test_summary_query_is_scoped_to_the_user_and_the_period(monkeypatch):
    rec = Recorder(rows=[], row=None).install(monkeypatch)
    usage_ledger.usage_summary("u1", NOW)
    sql, params = rec.calls[-1]
    assert "where user_id = %s" in sql and "created_at >= %s" in sql and "created_at < %s" in sql
    assert params[0] == "u1" and params[1].month == 10 and params[2].month == 11


def test_overspend_clamps_remaining_to_zero(monkeypatch):
    Recorder(rows=_rows(web=10_000), row=None).install(monkeypatch)
    assert usage_ledger.usage_summary("u1", NOW)["remaining"] == 0.0


def _summary(remaining):
    return {"pool": 100.0, "used": 100.0 - remaining, "remaining": remaining,
            "resets_at": "2026-11-01T00:00:00+00:00"}


def test_check_ok_when_the_estimate_fits(monkeypatch):
    monkeypatch.setattr(usage_ledger, "usage_summary", lambda *a, **k: _summary(50))
    r = usage_ledger.check("u1", 10)
    assert (r.status, r.allowed, r.remaining, r.degraded) == ("ok", True, 50, False)


def test_check_quota_exhausted_is_structured(monkeypatch):
    monkeypatch.setattr(usage_ledger, "usage_summary", lambda *a, **k: _summary(0))
    r = usage_ledger.check("u1", 1)
    assert (r.status, r.allowed) == ("quota_exhausted", False)
    assert r.to_dict()["resets_at"] == "2026-11-01T00:00:00+00:00"


def test_check_estimate_larger_than_remaining_is_exhausted(monkeypatch):
    monkeypatch.setattr(usage_ledger, "usage_summary", lambda *a, **k: _summary(5))
    assert usage_ledger.check("u1", 6).status == "quota_exhausted"


def test_check_fails_open_when_usage_cannot_be_read(monkeypatch):
    monkeypatch.setattr(usage_ledger, "usage_summary",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    r = usage_ledger.check("u1", 1)
    assert (r.status, r.allowed, r.degraded) == ("ok", True, True)


def test_check_without_an_account_is_always_allowed():
    assert usage_ledger.check(None, 10**9).allowed is True
