"""
tests/unit/test_eo_quota_sentinel.py — Patch 7d.

eo/quota_sentinel.py had zero test coverage before this. The audit's own
framing (quota gating -- reject vs. degrade vs. queue) undersells what's
actually here: this module doesn't itself reject/degrade/queue anything
-- can_proceed()-style gating lives in utils/rate_ledger.py. What this
module does is READ the two things that gating (and Panel's quota-aware
hiring, and the /api/quota + /api/usage/history endpoints) depend on:
today's real per-account usage against QUOTA_CONFIG's verified numbers
(get_quota_snapshot), live per-minute headroom (get_rate_window_snapshot),
and historical day-by-day rollups (get_usage_history /
get_usage_history_scoped). check_and_alert() is the one function with a
side effect (emit_event), and it's the thing most likely to fail exactly
the way the audit worries about -- silently never firing (or firing on
the wrong threshold) with nothing to notice, since nothing downstream of
it raises.

Style/isolation notes:
  - eo/registry.py's AGENT_CAPABILITIES is real production config (dozens
    of entries) -- every test here monkeypatches it to a small, fixed set
    of fake accounts instead, so tests aren't coupled to whatever
    providers/models happen to be registered today. Safe to patch
    directly on the eo.registry module object: _key_id_for(),
    get_quota_snapshot(), get_rate_window_snapshot(), and
    get_usage_history() all do `from eo.registry import
    AGENT_CAPABILITIES` INSIDE the function body (a deferred import, to
    dodge a circular-import -- see this module's own _model_for()
    docstring for the sibling case), so the attribute lookup happens at
    call time and picks up whatever's on eo.registry.AGENT_CAPABILITIES
    at that moment.
  - QUOTA_CONFIG, headroom_snapshot, and emit_event are all imported at
    eo/quota_sentinel.py's OWN module top level (`from ... import ...`),
    which binds a name in ITS namespace -- per conftest.py's own
    documented gotcha with generate_text, monkeypatching
    utils.llm_client.QUOTA_CONFIG or relay.emitter.emit_event would NOT
    reach the copies quota_sentinel.py actually calls. These are patched
    as eo.quota_sentinel.QUOTA_CONFIG / eo.quota_sentinel.emit_event /
    eo.quota_sentinel.headroom_snapshot instead.
  - memory.bus.read/read_many need no mocking: tests/conftest.py's
    autouse fake_bus fixture already swaps memory.bus.redis for an
    in-memory FakeRedis before every test, and bus.write()/read_many()
    resolve that module-level `redis` name at call time regardless of
    which module imported the read/write functions -- so seeding data
    via memory.bus.write() (matching the exact key shapes
    generate_text()/log_usage() write in production) reaches
    quota_sentinel's own bus_read/bus_read_many calls correctly.
  - No date-freezing library (freezegun etc.) is available in this repo
    (checked requirements.txt directly) -- so instead of mocking
    date.today(), tests compute the SAME date.today() the code under
    test will use and build/seed keys against that, matching production
    key shapes exactly. This is safe for a normal test run and avoids
    adding a new test-only dependency for one module.
"""
from datetime import UTC, date, datetime, timedelta

import pytest

import eo.quota_sentinel as qs
from memory.bus import write as bus_write

# ---------------------------------------------------------------------
# Fixtures — small, fixed fake registry/config in place of the real one
# ---------------------------------------------------------------------

FAKE_CAPS = {
    "GROQ_TEST_KEY": {"provider": "groq"},
    "GROQ_TEST_KEY_2": {"provider": "groq"},
    "GEMINI_TEST_KEY": {"provider": "gemini"},
    "MISTRAL_TEST_KEY": {"provider": "mistral"},
    "CF_TEST_KEY": {"provider": "cloudflare", "key_id": "CF_ACCOUNT_ID_ENV"},
}

FAKE_QUOTA_CONFIG = {
    "groq": {"llama-3.3-70b-versatile": {"rpd": 1000, "rpm": 30}},
    "gemini": {"gemini-3.6-flash": {"rpd": 20, "rpm": 5}},
    "mistral": {"mistral-large-latest": {"tpm": 250000}},  # no "rpd" published
    "cloudflare": {"@cf/meta/llama-3.3-70b-instruct-fp8-fast": {"neurons_rpd": 10000}},
}

FAKE_PROVIDER_DEFAULT_MODEL = {
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-3.6-flash",
    "mistral": "mistral-large-latest",
}


@pytest.fixture(autouse=True)
def _fake_registry_and_quota(monkeypatch):
    import agents.generic_worker as generic_worker_module
    import eo.registry as registry_module

    monkeypatch.setattr(registry_module, "AGENT_CAPABILITIES", dict(FAKE_CAPS))
    monkeypatch.setattr(qs, "QUOTA_CONFIG", FAKE_QUOTA_CONFIG)
    monkeypatch.setattr(generic_worker_module, "PROVIDER_DEFAULT_MODEL", FAKE_PROVIDER_DEFAULT_MODEL)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    yield


def _seed_usage(provider, key_id, day, requests=0, tokens=0, model=None):
    record = {"requests": requests, "tokens": tokens}
    if model is not None:
        record["model"] = model
    bus_write(f"usage:{provider}:{key_id}:{day}", record)


def _seed_cooldown(provider, key_id, until_timestamp):
    bus_write(f"cooldown_until:{provider}:{key_id}", until_timestamp)


# ---------------------------------------------------------------------
# _key_id_for
# ---------------------------------------------------------------------

def test_key_id_for_uses_explicit_key_id_field():
    assert qs._key_id_for("CF_TEST_KEY", "cloudflare") == "CF_ACCOUNT_ID_ENV"


def test_key_id_for_falls_back_to_agent_key_when_field_absent():
    assert qs._key_id_for("GROQ_TEST_KEY", "groq") == "GROQ_TEST_KEY"


def test_key_id_for_falls_back_when_agent_key_not_registered_at_all():
    assert qs._key_id_for("SOME_UNKNOWN_KEY", "groq") == "SOME_UNKNOWN_KEY"


# ---------------------------------------------------------------------
# _model_for
# ---------------------------------------------------------------------

def test_model_for_cloudflare_ignores_provider_default_model_table():
    assert qs._model_for("CF_TEST_KEY", "cloudflare") == qs._CLOUDFLARE_DEFAULT_MODEL


def test_model_for_non_cloudflare_uses_provider_default_model_table():
    assert qs._model_for("GROQ_TEST_KEY", "groq") == "llama-3.3-70b-versatile"


def test_model_for_unknown_provider_returns_none():
    assert qs._model_for("SOME_KEY", "some_unlisted_provider") is None


# ---------------------------------------------------------------------
# _tavily_usage_this_month
# ---------------------------------------------------------------------

def test_tavily_usage_is_zero_with_nothing_recorded():
    assert qs._tavily_usage_this_month() == 0


def test_tavily_usage_sums_every_day_of_the_month_so_far():
    """Tavily has no daily reset (1,000/MONTH, not /day) -- this must sum
    across every day from the 1st through today, not just read "today"
    the way every other provider's snapshot does."""
    today = date.today()
    day1 = date(today.year, today.month, 1).isoformat()
    bus_write(f"usage:tavily:TAVILY_API_KEY:{day1}", {"requests": 3, "tokens": 0})
    bus_write(f"usage:tavily:TAVILY_API_KEY:{today.isoformat()}", {"requests": 4, "tokens": 0})
    assert qs._tavily_usage_this_month() == 7


def test_tavily_usage_ignores_days_outside_the_current_month():
    """A record from a previous month must not leak into this month's
    sum -- the date range is deliberately scoped to [1st of THIS month,
    today], not an unbounded lookback."""
    today = date.today()
    last_month_day = today.replace(day=1) - timedelta(days=1)
    bus_write(
        f"usage:tavily:TAVILY_API_KEY:{last_month_day.isoformat()}",
        {"requests": 999, "tokens": 0},
    )
    assert qs._tavily_usage_this_month() == 0


# ---------------------------------------------------------------------
# get_quota_snapshot
# ---------------------------------------------------------------------

def test_get_quota_snapshot_pct_from_todays_usage_record():
    today = date.today().isoformat()
    _seed_usage("groq", "GROQ_TEST_KEY", today, requests=300, model="llama-3.3-70b-versatile")
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["GROQ_TEST_KEY"]
    assert entry["used"] == 300
    assert entry["quota"] == 1000
    assert entry["pct"] == pytest.approx(0.3)
    assert entry["unit_mismatch"] is False


def test_get_quota_snapshot_falls_back_to_default_model_with_no_usage_yet():
    """No usage record at all today for this account -- model must come
    from _model_for()'s tag-driven default, not blow up on a missing
    "model" field."""
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["GEMINI_TEST_KEY"]
    assert entry["used"] == 0
    assert entry["quota"] == 20
    assert entry["pct"] == pytest.approx(0.0)


def test_get_quota_snapshot_uses_recorded_model_over_the_default_guess():
    """When a real call already landed today, the record's own "model"
    field must win over _model_for()'s best-guess default -- seed a
    model this account's default table would NOT have produced."""
    today = date.today().isoformat()
    _seed_usage("gemini", "GEMINI_TEST_KEY", today, requests=1, model="gemini-3.1-flash-lite")
    qs.QUOTA_CONFIG["gemini"]["gemini-3.1-flash-lite"] = {"rpd": 500}
    try:
        snapshot = qs.get_quota_snapshot()
        assert snapshot["GEMINI_TEST_KEY"]["quota"] == 500
    finally:
        del qs.QUOTA_CONFIG["gemini"]["gemini-3.1-flash-lite"]


def test_get_quota_snapshot_none_quota_and_pct_when_no_verified_rpd():
    """Mistral publishes RPS, not RPD -- an honest "no verified number"
    (None/None) is required here, not a fabricated percentage against
    some other field."""
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["MISTRAL_TEST_KEY"]
    assert entry["quota"] is None
    assert entry["pct"] is None
    assert entry["unit_mismatch"] is False


def test_get_quota_snapshot_cloudflare_reports_real_neuron_usage_and_pct():
    """Patch I.2 follow-up (supersedes the old
    test_get_quota_snapshot_cloudflare_reports_unit_mismatch_and_no_pct):
    now that rate_ledger's "neurons" gating mode maintains a real daily
    neuron total (fed by reserve()/release_reservation(), not a request
    count), get_quota_snapshot()'s cloudflare branch reports `used` in
    the SAME unit as `quota` -- a genuine percentage, not withheld, and
    `unit_mismatch: False` since the units now actually match. Seeds the
    neuron day-bucket directly via rate_ledger._adjust_daily_neurons(),
    matching the exact key shape reserve()/release_reservation() write
    in production (see that function's own docstring), rather than the
    old test's `usage:cloudflare:...` request-count record -- that key
    is still written by log_usage() for other purposes, but the
    cloudflare branch no longer reads it for `used`."""
    import utils.rate_ledger as rate_ledger

    rate_ledger._adjust_daily_neurons("cloudflare", "CF_ACCOUNT_ID_ENV", 4200.0)
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["CF_TEST_KEY"]
    assert entry["used"] == pytest.approx(4200.0)
    assert entry["quota"] == 10000
    assert entry["pct"] == pytest.approx(0.42)
    assert entry["unit"] == "neurons"
    assert entry["unit_mismatch"] is False
    assert entry["unmetered"] is False


def test_get_quota_snapshot_cloudflare_zero_neurons_used_with_no_bookings_yet():
    """No reserve()/release_reservation() has ever touched this
    account's neuron bucket -- must read as a real 0, not None/crash,
    same fail-open-as-0 posture _read_daily_neurons() documents."""
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["CF_TEST_KEY"]
    assert entry["used"] == 0.0
    assert entry["quota"] == 10000
    assert entry["pct"] == pytest.approx(0.0)
    assert entry["unit"] == "neurons"


def test_get_quota_snapshot_marks_unmetered_provider_distinctly(monkeypatch):
    """A provider whose QUOTA_CONFIG entry is the "unmetered_credit_pool"
    sentinel (Patch I.2's huggingface case) must report `unmetered: True`
    and a withheld quota/pct -- distinct from mistral's "nobody's
    published rpd yet" case (same None/None on the surface, different
    reason, both must be representable without colliding).

    rate_ledger.is_unmetered_provider() imports QUOTA_CONFIG from
    utils.llm_client directly (lazily, at call time -- see its own
    docstring), NOT the qs.QUOTA_CONFIG copy this file's autouse fixture
    patches for get_quota_snapshot()'s other lookups. Patching only
    qs.QUOTA_CONFIG here would leave is_unmetered_provider() reading the
    real production dict and silently miss the sentinel -- both need
    patching for this test to actually exercise the branch it claims to."""
    import eo.registry as registry_module
    import utils.llm_client as llm_client_module

    caps = dict(FAKE_CAPS)
    caps["HF_TEST_KEY"] = {"provider": "huggingface"}
    monkeypatch.setattr(registry_module, "AGENT_CAPABILITIES", caps)
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, "huggingface", "unmetered_credit_pool")

    snapshot = qs.get_quota_snapshot()
    entry = snapshot["HF_TEST_KEY"]
    assert entry["quota"] is None
    assert entry["pct"] is None
    assert entry["unmetered"] is True
    assert entry["unit_mismatch"] is False
    # Contrast: mistral's None/None is NOT unmetered -- just unpublished.
    assert snapshot["MISTRAL_TEST_KEY"]["unmetered"] is False


def test_get_quota_snapshot_cooling_down_true_for_future_timestamp():
    future = datetime.now(UTC).timestamp() + 300
    _seed_cooldown("groq", "GROQ_TEST_KEY", future)
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["GROQ_TEST_KEY"]
    assert entry["cooldown_until"] == pytest.approx(future)
    assert entry["cooling_down"] is True


def test_get_quota_snapshot_cooling_down_false_for_past_timestamp():
    past = datetime.now(UTC).timestamp() - 300
    _seed_cooldown("groq", "GROQ_TEST_KEY", past)
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["GROQ_TEST_KEY"]
    assert entry["cooling_down"] is False


def test_get_quota_snapshot_no_cooldown_record_is_falsy_and_not_cooling_down():
    snapshot = qs.get_quota_snapshot()
    entry = snapshot["GROQ_TEST_KEY"]
    assert entry["cooldown_until"] is None
    assert entry["cooling_down"] is False


def test_get_quota_snapshot_omits_tavily_when_key_not_configured():
    snapshot = qs.get_quota_snapshot()
    assert "tavily" not in snapshot


def test_get_quota_snapshot_includes_tavily_when_key_configured(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-key")
    today = date.today().isoformat()
    bus_write(f"usage:tavily:TAVILY_API_KEY:{today}", {"requests": 250, "tokens": 0})
    snapshot = qs.get_quota_snapshot()
    assert snapshot["tavily"]["used"] == 250
    assert snapshot["tavily"]["quota"] == qs.TAVILY_MONTHLY_QUOTA
    assert snapshot["tavily"]["pct"] == pytest.approx(250 / qs.TAVILY_MONTHLY_QUOTA)


# ---------------------------------------------------------------------
# get_rate_window_snapshot
# ---------------------------------------------------------------------

def test_get_rate_window_snapshot_calls_headroom_snapshot_per_account(monkeypatch):
    calls = []

    def fake_headroom_snapshot(provider, key_id, model):
        calls.append((provider, key_id, model))
        return {"gating_mode": "tokens", "sliding_window": {"pct_used": 0.1}}

    monkeypatch.setattr(qs, "headroom_snapshot", fake_headroom_snapshot)
    result = qs.get_rate_window_snapshot()

    assert set(result.keys()) == set(FAKE_CAPS.keys())
    assert ("groq", "GROQ_TEST_KEY", "llama-3.3-70b-versatile") in calls
    assert ("cloudflare", "CF_ACCOUNT_ID_ENV", qs._CLOUDFLARE_DEFAULT_MODEL) in calls


def test_get_rate_window_snapshot_resolves_model_from_todays_usage_record(monkeypatch):
    """Same model-resolution rule as get_quota_snapshot(): a real logged
    model for today must be used over the tag-driven default guess."""
    today = date.today().isoformat()
    _seed_usage("groq", "GROQ_TEST_KEY", today, requests=1, model="qwen/qwen3.6-27b")
    seen_models = {}

    def fake_headroom_snapshot(provider, key_id, model):
        seen_models[key_id] = model
        return {}

    monkeypatch.setattr(qs, "headroom_snapshot", fake_headroom_snapshot)
    qs.get_rate_window_snapshot()

    assert seen_models["GROQ_TEST_KEY"] == "qwen/qwen3.6-27b"


def test_get_rate_window_snapshot_excludes_tavily():
    """Unlike get_quota_snapshot()'s one-off tavily entry, rate-window
    gating doesn't apply to a search API at all -- confirm it's never
    added here even when the key is configured."""
    import os
    os.environ["TAVILY_API_KEY"] = "fake-key"
    try:
        result = qs.get_rate_window_snapshot()
        assert "tavily" not in result
    finally:
        del os.environ["TAVILY_API_KEY"]


# ---------------------------------------------------------------------
# check_and_alert
# ---------------------------------------------------------------------

def test_check_and_alert_fires_daily_alert_at_80_percent(monkeypatch):
    events = []
    monkeypatch.setattr(
        qs, "get_quota_snapshot",
        lambda: {"GROQ_TEST_KEY": {"used": 800, "quota": 1000, "pct": 0.8}},
    )
    monkeypatch.setattr(qs, "get_rate_window_snapshot", dict)
    monkeypatch.setattr(
        qs, "emit_event",
        lambda event_type, session_id, agent=None, payload=None: events.append(
            (event_type, session_id, agent, payload)
        ),
    )

    qs.check_and_alert(session_id="sess-1")

    assert len(events) == 1
    event_type, session_id, agent, payload = events[0]
    assert event_type == "quota_alert"
    assert session_id == "sess-1"
    assert payload["agent_key"] == "GROQ_TEST_KEY"
    assert payload["window"] == "daily"
    assert payload["pct"] == pytest.approx(0.8)


def test_check_and_alert_does_not_fire_below_80_percent(monkeypatch):
    events = []
    monkeypatch.setattr(
        qs, "get_quota_snapshot",
        lambda: {"GROQ_TEST_KEY": {"used": 799, "quota": 1000, "pct": 0.799}},
    )
    monkeypatch.setattr(qs, "get_rate_window_snapshot", dict)
    monkeypatch.setattr(qs, "emit_event", lambda *a, **k: events.append((a, k)))

    qs.check_and_alert()

    assert events == []


def test_check_and_alert_skips_accounts_with_no_verified_pct(monkeypatch):
    """pct=None (e.g. Mistral, no published rpd) must never be compared
    against the threshold -- a naive `>= 0.8` on None would raise
    TypeError and take down the whole periodic check for every OTHER
    account in the same snapshot."""
    events = []
    monkeypatch.setattr(
        qs, "get_quota_snapshot",
        lambda: {"MISTRAL_TEST_KEY": {"used": 0, "quota": None, "pct": None}},
    )
    monkeypatch.setattr(qs, "get_rate_window_snapshot", dict)
    monkeypatch.setattr(qs, "emit_event", lambda *a, **k: events.append((a, k)))

    qs.check_and_alert()  # must not raise

    assert events == []


def test_check_and_alert_fires_minute_alert_from_sliding_window(monkeypatch):
    events = []
    monkeypatch.setattr(qs, "get_quota_snapshot", dict)
    monkeypatch.setattr(
        qs, "get_rate_window_snapshot",
        lambda: {
            "GROQ_TEST_KEY": {
                "gating_mode": "tokens",
                "sliding_window": {"used_last_60s": 9500, "tpm_limit": 12000, "pct_used": 0.79},
            }
        },
    )
    monkeypatch.setattr(
        qs, "emit_event",
        lambda event_type, session_id, agent=None, payload=None: events.append(payload),
    )

    qs.check_and_alert()
    assert events == []  # 0.79 is below threshold, confirms the fixture itself is meaningful

    monkeypatch.setattr(
        qs, "get_rate_window_snapshot",
        lambda: {
            "GROQ_TEST_KEY": {
                "gating_mode": "tokens",
                "sliding_window": {"used_last_60s": 9600, "tpm_limit": 12000, "pct_used": 0.80},
            }
        },
    )
    qs.check_and_alert()

    assert len(events) == 1
    payload = events[0]
    assert payload["window"] == "minute"
    assert payload["gating_mode"] == "tokens"
    assert payload["used"] == 9600
    assert payload["quota"] == 12000


def test_check_and_alert_skips_minute_alert_when_pct_used_is_none(monkeypatch):
    events = []
    monkeypatch.setattr(qs, "get_quota_snapshot", dict)
    monkeypatch.setattr(
        qs, "get_rate_window_snapshot",
        lambda: {
            "GROQ_TEST_KEY": {
                "gating_mode": "requests",
                "sliding_window": {"used_last_60s": 5, "tpm_limit": None, "pct_used": None},
            }
        },
    )
    monkeypatch.setattr(qs, "emit_event", lambda *a, **k: events.append((a, k)))

    qs.check_and_alert()  # must not raise

    assert events == []


# ---------------------------------------------------------------------
# get_usage_history
# ---------------------------------------------------------------------

def test_get_usage_history_dates_are_oldest_to_newest_ending_today():
    result = qs.get_usage_history(days=3)
    today = date.today()
    expected = [(today - timedelta(days=i)).isoformat() for i in (2, 1, 0)]
    assert result["dates"] == expected


def test_get_usage_history_sums_per_provider_across_multiple_accounts():
    today = date.today().isoformat()
    _seed_usage("groq", "GROQ_TEST_KEY", today, requests=5, tokens=100)
    _seed_usage("groq", "GROQ_TEST_KEY_2", today, requests=7, tokens=200)

    result = qs.get_usage_history(days=1)

    assert result["providers"]["groq"]["tokens"] == [300]
    assert result["providers"]["groq"]["requests"] == [12]
    assert result["providers"]["groq"]["total_tokens"] == 300
    assert result["providers"]["groq"]["avg_tokens_per_day"] == pytest.approx(300.0)


def test_get_usage_history_per_account_breakdown_matches_provider_sum():
    today = date.today().isoformat()
    _seed_usage("gemini", "GEMINI_TEST_KEY", today, requests=2, tokens=40)

    result = qs.get_usage_history(days=1)

    assert result["accounts"]["GEMINI_TEST_KEY"]["provider"] == "gemini"
    assert result["accounts"]["GEMINI_TEST_KEY"]["tokens"] == [40]
    assert result["accounts"]["GEMINI_TEST_KEY"]["requests"] == [2]
    assert result["providers"]["gemini"]["tokens"] == [40]


def test_get_usage_history_zero_days_does_not_raise():
    """days=0 must not divide by zero computing avg_tokens_per_day."""
    result = qs.get_usage_history(days=0)
    assert result["dates"] == []
    for series in result["providers"].values():
        assert series["avg_tokens_per_day"] == 0.0


# ---------------------------------------------------------------------
# get_usage_history_scoped
# ---------------------------------------------------------------------

def test_get_usage_history_scoped_domain_only():
    today = date.today().isoformat()
    bus_write(f"usage_by_domain:coding:{today}", {"requests": 3, "tokens": 60})

    result = qs.get_usage_history_scoped(days=1, domain="coding")

    assert result["domain"]["tokens"] == [60]
    assert result["domain"]["requests"] == [3]
    assert result["domain"]["total_tokens"] == 60
    assert result["workspace"] is None


def test_get_usage_history_scoped_workspace_only():
    today = date.today().isoformat()
    bus_write(f"usage_by_workspace:ws-1:{today}", {"requests": 1, "tokens": 10})

    result = qs.get_usage_history_scoped(days=1, workspace_id="ws-1")

    assert result["workspace"]["tokens"] == [10]
    assert result["domain"] is None


def test_get_usage_history_scoped_domain_and_workspace_are_independent():
    """Both given -- must be two SEPARATE series read from their own
    keys, not intersected/combined into one number."""
    today = date.today().isoformat()
    bus_write(f"usage_by_domain:coding:{today}", {"requests": 3, "tokens": 60})
    bus_write(f"usage_by_workspace:ws-1:{today}", {"requests": 1, "tokens": 10})

    result = qs.get_usage_history_scoped(days=1, domain="coding", workspace_id="ws-1")

    assert result["domain"]["tokens"] == [60]
    assert result["workspace"]["tokens"] == [10]


def test_get_usage_history_scoped_neither_argument_returns_none_for_both():
    result = qs.get_usage_history_scoped(days=2)
    assert result["domain"] is None
    assert result["workspace"] is None
    assert len(result["dates"]) == 2


# ---------------------------------------------------------------------
# get_ledger_event_counts
# ---------------------------------------------------------------------

def test_get_ledger_event_counts_reads_seeded_counter():
    bus_write("ledger_events:sess-42", {"wait": 2, "reroute": 1, "provider_failure": 0})
    assert qs.get_ledger_event_counts("sess-42") == {
        "wait": 2, "reroute": 1, "provider_failure": 0,
    }


def test_get_ledger_event_counts_defaults_when_nothing_recorded():
    assert qs.get_ledger_event_counts("sess-never-ran") == {
        "wait": 0, "reroute": 0, "provider_failure": 0,
    }


@pytest.mark.parametrize("falsy_session_id", [None, ""])
def test_get_ledger_event_counts_falsy_session_id_short_circuits(falsy_session_id):
    """A falsy session_id must return the zero dict directly, without
    even attempting a bus read keyed on an empty/None session id."""
    assert qs.get_ledger_event_counts(falsy_session_id) == {
        "wait": 0, "reroute": 0, "provider_failure": 0,
    }
