"""
eo/quota_sentinel.py — reads the usage data utils/llm_client.py ALREADY
tracks (via generate_text() -> log_usage()), rather than maintaining a
separate counter. This module adds exactly two things on top of what
already exists: an 80%-threshold alert, and a snapshot function for the
Panel's quota-aware hiring (Part 6) and the GET /api/quota endpoint
(Part 4 §7.5/§8.2).

Migration Part 8 §2 — this REPLACES the earlier version, which built a
parallel, incompatible tracking system (a flat, never-date-scoped
usage:{key_env} counter in a separate cache Redis, with its own
DAILY_QUOTA_ESTIMATES numbers that diverged from utils/llm_client.py's
real, verified QUOTA_CONFIG) and was called via record_usage() ALONGSIDE
generate_text()'s own logging -- double-counting every single call. That
call has been removed from llm_client.py's log_usage() as part of this
same fix; see llm_client.py's own comment at that call site.
"""
import os
import sys
from datetime import UTC, date, datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read as bus_read
from memory.bus import read_many as bus_read_many
from relay.emitter import emit_event
from utils.llm_client import QUOTA_CONFIG
from utils.rate_ledger import daily_neurons_used, headroom_snapshot, is_unmetered_provider

TAVILY_MONTHLY_QUOTA = 1000  # Tavily's free tier: 1,000 searches/MONTH, not
# daily like every other provider in QUOTA_CONFIG. Deliberately NOT added
# to utils/llm_client.py's QUOTA_CONFIG -- that dict's own docstring
# commits it to daily free-tier limits only, and folding a monthly cap
# in there would make get_usage_history()'s date-scoped reads silently
# wrong for this one provider. Tracked separately below instead.
def _tavily_usage_this_month() -> int:
    """Sums usage:tavily:TAVILY_API_KEY:{date} requests for every day
    from the 1st of the current calendar month through today -- Tavily
    has no daily reset, so reading just "today" (like get_quota_snapshot()
    does for every other provider) would understate real usage against
    its actual 1,000/month cap. One bus_read_many() round trip, same
    "don't turn N days into N network calls" discipline get_usage_history()
    already uses above."""
    today = date.today()
    dates = [date(today.year, today.month, d).isoformat() for d in range(1, today.day + 1)]
    keys = [f"usage:tavily:TAVILY_API_KEY:{d}" for d in dates]
    records = bus_read_many(keys, default={"requests": 0, "tokens": 0})
    return sum(records[k].get("requests", 0) for k in keys)


def _key_id_for(agent_key: str, provider: str) -> str:
    """For groq/cerebras/github/mistral, log_usage()'s key_id IS the
    key_env string itself. For cloudflare, it's the account_id_env
    string specifically (see llm_client.py's generate_text(): the
    cloudflare branch sets `key_id = account_id_env`). AGENT_CAPABILITIES
    entries need a "key_id" field for cloudflare accounts that differs
    from their key_env — add one if it's missing; for every other
    provider, key_id already equals the account entry's own dict key."""
    from eo.registry import AGENT_CAPABILITIES
    info = AGENT_CAPABILITIES.get(agent_key, {})
    return info.get("key_id", agent_key)


# Quota-reality fix, §1: cloudflare's model isn't resolvable the same way
# as the other providers (it's CHAIN-hardcoded per agent file, not a
# PROVIDER_DEFAULT_MODEL entry) -- this is what every cloudflare CHAIN
# step calls except reviewer.py's outlier ("@cf/meta/llama-3.1-8b-instruct",
# which has no QUOTA_CONFIG entry yet -- see llm_client.py's QUOTA_CONFIG
# note on this). Used by _model_for() only as the "no call logged yet
# today" fallback; once a real call lands, the record's own "model" field
# (log_usage()'s new model= param) takes over.
_CLOUDFLARE_DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def _model_for(agent_key: str, provider: str) -> str | None:
    """Resolves which model an account/agent_key actually calls, for
    accounts with no usage record yet today (get_quota_snapshot() prefers
    the record's own "model" field -- the real, logged model from an
    actual call -- and only falls back to this when that's absent).

    AGENT_CAPABILITIES entries don't currently carry a "model" field
    (checked directly against eo/registry.py) -- tag-driven roles get
    their model from PROVIDER_DEFAULT_MODEL[provider]
    (agents/generic_worker.py), CHAIN-based roles hardcode it per step
    and aren't resolvable from AGENT_CAPABILITIES alone (e.g.
    performance_reviewer.py uses two different Gemini models on the same
    key -- there's no single "the" model for that key_env without also
    knowing which CHAIN step matched). This returns the tag-driven
    default as the best available guess; it's deliberately not meant to
    be exact for CHAIN-based accounts before their first real call of
    the day.

    `agents.generic_worker` is imported inside this function, not at
    module level: that module imports eo.quota_sentinel.get_quota_snapshot
    at its own top level, so a module-level import here would be a
    circular import (same reasoning as _key_id_for()'s deferred
    eo.registry import above, and eo.panel's deferred eo.registry
    import)."""
    if provider == "cloudflare":
        return _CLOUDFLARE_DEFAULT_MODEL
    from agents.generic_worker import PROVIDER_DEFAULT_MODEL
    return PROVIDER_DEFAULT_MODEL.get(provider)


def get_quota_snapshot() -> dict:
    """Returns {agent_key: {"used": int|float, "quota": int|None,
    "pct": float|None, "unit": str, "unit_mismatch": bool,
    "unmetered": bool, "cooldown_until": float|None, "cooling_down": bool}}
    for every account in AGENT_CAPABILITIES, reading TODAY's real usage
    from the exact keys generate_text() already writes.

    Quota-reality fix, §1 -- this used to compare TOKEN usage against a
    number documented (and used) as a REQUEST-per-day ceiling (§1a), read
    from a flat per-provider number that couldn't represent reality
    anyway (§1b/§1c). Now: resolve the real model this account/key_id
    actually used today (the usage record's own "model" field when a
    call has landed today, else _model_for()'s best-guess default),
    look up QUOTA_CONFIG[provider][model]["rpd"], and compare against
    `requests` -- not `tokens`. quota/pct are None where QUOTA_CONFIG has
    no verified daily figure for that provider/model (e.g. mistral,
    which only publishes RPS) — an honest "no verified number" rather
    than a guess.

    Cloudflare gets its own branch (reliability guide §10, corrected by
    Patch I.2's follow-up): originally this compared a REQUEST count
    against the neuron ceiling (`unit_mismatch: True`, `pct` withheld,
    since a request count and a neuron ceiling aren't the same unit).
    Now that rate_ledger's "neurons" gating mode maintains a real running
    daily neuron total (rate_ledger.daily_neurons_used(), fed from
    reserve()'s pre-flight estimate and trued up by release_reservation()
    against actual usage when Cloudflare's response happens to include
    one), `used` here IS a neuron figure, the same unit as `quota` --
    `unit_mismatch` is correctly False and `pct` is a real percentage,
    not withheld. IMPORTANT CAVEAT this snapshot cannot express in a
    single boolean: `used` is the ledger's tracked estimate, not a number
    Cloudflare's dashboard independently confirms -- see
    rate_ledger._estimate_neurons()'s own docstring for why it's a
    worst-case (max_tokens-ceiling-based) figure that only gets trued up
    to something more exact on the minority of calls whose response
    includes a usable prompt/completion split. Structurally the same
    "tracked usage is our best estimate, verify big anomalies against the
    provider's own dashboard" caveat every OTHER provider's `used` in
    this snapshot already carries (they're all self-tracked, none of
    them poll the provider's dashboard live) -- cloudflare just makes it
    more visible because its number was previously a request count, an
    obviously different order of magnitude from a neuron figure.

    `unit` (new): "neurons" for cloudflare, "requests" for every other
    metered provider, so the frontend can label a row correctly without
    hardcoding a provider name to guess it (see
    frontend/app/components/tabs/TokenUsageTab.jsx, updated alongside
    this to read the field instead of assuming "requests" everywhere).

    `unmetered` (Patch I.2): True for a provider whose QUOTA_CONFIG entry
    is the explicit "unmetered_credit_pool" sentinel (currently just
    huggingface) -- `quota`/`pct` are None here too, but for a different
    reason than mistral's "nobody's published a number yet": there
    genuinely isn't a request/token ceiling to hit, it's a monthly credit
    pool instead. Every other provider gets `unmetered: False` so the
    frontend has one consistent field to check rather than needing a
    provider-name special case (same reasoning `unit_mismatch` already
    documented before this fix, extended to cover this second axis).

    Fix B (reliability guide, §3 "Fix B"): also reads back
    cooldown_until:{provider}:{key_id} — the UTC timestamp
    utils/llm_client.py's generate_text() writes whenever a call to that
    account fails with a transient (429/5xx/timeout) error, parsed from
    the provider's own retry-after signal where one is available. This
    is a SEPARATE constraint from daily quota usage (`pct` above): an
    account can be well under its 80% daily cutoff and still be
    mid-cooldown from a recent rate-limit response, or vice versa. Both
    are surfaced here so eo/panel.py's _best_match() can check them
    independently instead of conflating "out of quota for the day"
    with "briefly rate-limited a moment ago." Read in the SAME MGET
    round trip as the usage keys below — same "don't turn N accounts
    into N network calls" discipline get_usage_history() already uses.
    """
    from eo.registry import AGENT_CAPABILITIES
    today = date.today().isoformat()
    agent_infos = [
        (agent_key, info.get("provider"), _key_id_for(agent_key, info.get("provider")))
        for agent_key, info in AGENT_CAPABILITIES.items()
    ]
    usage_keys = [f"usage:{provider}:{key_id}:{today}" for _, provider, key_id in agent_infos]
    cooldown_keys = [f"cooldown_until:{provider}:{key_id}" for _, provider, key_id in agent_infos]
    usage_records = bus_read_many(usage_keys, default={"requests": 0, "tokens": 0})
    cooldown_records = bus_read_many(cooldown_keys, default=None)
    now = datetime.now(UTC).timestamp()

    snapshot = {}
    for agent_key, provider, key_id in agent_infos:
        record = usage_records[f"usage:{provider}:{key_id}:{today}"]
        model = record.get("model") or _model_for(agent_key, provider)
        cooldown_until = cooldown_records.get(f"cooldown_until:{provider}:{key_id}")
        cooling_down = bool(cooldown_until and cooldown_until > now)

        if provider == "cloudflare":
            # Patch I.2 follow-up: real neuron accounting, not the
            # request-count proxy this branch used before -- see this
            # function's own docstring for the full "why" and its caveat.
            limits = QUOTA_CONFIG.get("cloudflare", {}).get(model, {})
            neuron_quota = limits.get("neurons_rpd")
            used_neurons = daily_neurons_used(provider, key_id)
            pct = (used_neurons / neuron_quota) if neuron_quota else None
            snapshot[agent_key] = {
                "used": used_neurons, "quota": neuron_quota, "pct": pct,
                "unit": "neurons",
                "unit_mismatch": False,  # units now genuinely match
                "unmetered": False,
                "cooldown_until": cooldown_until, "cooling_down": cooling_down,
            }
            continue

        # Patch I.2: providers whose QUOTA_CONFIG entry is the explicit
        # "unmetered_credit_pool" sentinel (currently just huggingface)
        # get their own branch, same reasoning as cloudflare's above --
        # `QUOTA_CONFIG.get(provider, {}).get(model, {})` would raise on
        # a bare string, and even guarded against that, `quota: None`
        # here would render identically to a provider nobody's sourced
        # real numbers for yet. `unmetered: True` lets the frontend tell
        # the two apart instead of both showing as an unlabeled dash.
        if is_unmetered_provider(provider):
            used_requests = record.get("requests", 0)
            snapshot[agent_key] = {
                "used": used_requests, "quota": None, "pct": None,
                "unit": "requests",
                "unit_mismatch": False, "unmetered": True,
                "cooldown_until": cooldown_until, "cooling_down": cooling_down,
            }
            continue

        limits = QUOTA_CONFIG.get(provider, {}).get(model, {})
        quota = limits.get("rpd")  # None where not published (e.g. mistral)
        used = record.get("requests", 0)  # FIX (§1a): requests, not tokens
        pct = (used / quota) if quota else None
        snapshot[agent_key] = {
            "used": used, "quota": quota, "pct": pct,
            "unit": "requests",
            "unit_mismatch": False,
            "unmetered": False,
            "cooldown_until": cooldown_until, "cooling_down": cooling_down,
        }

    # Search-API keys aren't LLM agents, so they have no AGENT_CAPABILITIES
    # entry to loop over above -- added as a one-off extra entry instead,
    # and only when the key is actually configured, so an unused feature
    # doesn't clutter the dashboard with a permanent phantom 0/1000 row.
    # Monthly cap, not daily -- see _tavily_usage_this_month()'s docstring.
    # check_and_alert() below needs no changes to cover this: it already
    # iterates whatever get_quota_snapshot() returns.
    if os.environ.get("TAVILY_API_KEY"):
        used = _tavily_usage_this_month()
        snapshot["tavily"] = {
            "used": used,
            "quota": TAVILY_MONTHLY_QUOTA,
            "pct": used / TAVILY_MONTHLY_QUOTA,
        }

    return snapshot


def get_rate_window_snapshot() -> dict:
    """Phase 8a — {agent_key: headroom_snapshot()-shaped dict} for every
    account in AGENT_CAPABILITIES, read straight from rate_ledger.py's
    live gating state rather than the once-a-day usage: records
    get_quota_snapshot() above reads. This is the "how close is this
    org to its ceiling right now" companion to that function's "how
    much of today's quota is left" -- Phase 8's GET /api/quota goal.

    Deliberately a separate function/key (`rate_windows`) rather than
    folded into get_quota_snapshot()'s own per-agent dict: that dict's
    {used, quota, pct, unit_mismatch, cooldown_until, cooling_down}
    shape is a real, depended-on contract (Panel's quota-aware hiring
    reads it directly), and headroom_snapshot()'s token-vs-request
    gating_mode split doesn't fit cleanly into those existing fields
    without conditionals leaking into every caller of that function.

    Same model-resolution rule as get_quota_snapshot(): today's usage
    record's own "model" field when a call has landed today, else
    _model_for()'s best-guess default -- headroom_snapshot() needs a
    concrete model to resolve gating_mode and the right QUOTA_CONFIG
    limits, same as get_quota_snapshot()'s QUOTA_CONFIG lookup does.

    Tavily is skipped here (unlike get_quota_snapshot()'s one-off extra
    entry): it isn't an LLM agent, has no AGENT_CAPABILITIES entry, and
    rate_ledger.py's token-bucket/sliding-window gating doesn't apply to
    it at all -- its only quota concept is the monthly figure
    get_quota_snapshot() already reports.

    Read pattern mirrors get_quota_snapshot()'s own MGET-first-then-loop
    discipline for the usage records (one round trip for `today`'s
    per-account records, purely to resolve `model`); headroom_snapshot()
    itself does its own reads per account since it pulls from several
    distinct bus keys per (provider, key_id, model) and Phase 2 didn't
    expose a batched form of that read.
    """
    from eo.registry import AGENT_CAPABILITIES
    today = date.today().isoformat()
    agent_infos = [
        (agent_key, info.get("provider"), _key_id_for(agent_key, info.get("provider")))
        for agent_key, info in AGENT_CAPABILITIES.items()
    ]
    usage_keys = [f"usage:{provider}:{key_id}:{today}" for _, provider, key_id in agent_infos]
    usage_records = bus_read_many(usage_keys, default={"requests": 0, "tokens": 0})

    rate_windows = {}
    for agent_key, provider, key_id in agent_infos:
        record = usage_records[f"usage:{provider}:{key_id}:{today}"]
        model = record.get("model") or _model_for(agent_key, provider)
        rate_windows[agent_key] = headroom_snapshot(provider, key_id, model)

    return rate_windows


def check_and_alert(session_id: str = None) -> None:
    """Call this periodically (or after each generate_text() call, if you
    want it real-time) to fire quota_alert for anything that's crossed
    80%. Deliberately separate from get_quota_snapshot() so reading a
    snapshot for hiring decisions (Part 6) never has an alerting side
    effect.

    Phase 8b: alongside the existing daily-quota check (unchanged below,
    now tagged "window": "daily" so a listener can tell the two apart),
    this also walks get_rate_window_snapshot()'s per-account minute-
    window state and fires the same quota_alert event, tagged "window":
    "minute", the moment sliding_window.pct_used crosses ~80% -- before
    can_proceed() actually starts blocking requests for that account.
    Uses sliding_window.pct_used directly rather than re-deriving it, so
    this fires correctly for both gating_mode shapes (tpm-limited
    providers and OpenRouter-style rpm-limited ones) without a
    token-vs-request branch here -- headroom_snapshot() already picked
    the right limit and unit for whichever mode the account is in.
    pct_used is None (skipped, same as the daily check below) wherever
    QUOTA_CONFIG has no verified per-minute figure for that
    provider/model -- an honest "no verified number" rather than a
    fabricated percentage, same posture as get_quota_snapshot()'s own
    unit_mismatch/pct handling."""
    snapshot = get_quota_snapshot()
    for agent_key, info in snapshot.items():
        if info["pct"] is not None and info["pct"] >= 0.8:
            emit_event("quota_alert", session_id, agent="quota_sentinel",
                       payload={"agent_key": agent_key, "used": info["used"],
                                "quota": info["quota"], "pct": round(info["pct"], 3),
                                "window": "daily"})

    rate_windows = get_rate_window_snapshot()
    for agent_key, window in rate_windows.items():
        sliding = window.get("sliding_window") or {}
        pct = sliding.get("pct_used")
        if pct is not None and pct >= 0.8:
            emit_event("quota_alert", session_id, agent="quota_sentinel",
                       payload={"agent_key": agent_key,
                                "used": sliding.get("used_last_60s"),
                                "quota": sliding.get("tpm_limit"),
                                "pct": round(pct, 3),
                                "gating_mode": window.get("gating_mode"),
                                "window": "minute"})


def get_usage_history(days: int = 7) -> dict:
    """
    Cross-session, persisted day-by-day usage — the GET /api/usage/history
    candidate flagged in the Part 17 guide. Reads the exact same
    usage:{provider}:{key_id}:{date} keys get_quota_snapshot() reads for
    "today", just repeated across the last `days` calendar dates. No new
    storage, no new write path -- this is a pure read rollup over data
    utils/llm_client.py's log_usage() already writes on every real call.

    Returns:
    {
      "dates": ["2026-07-01", ..., "2026-07-07"],   # oldest -> newest
      "providers": {
        "groq": {"tokens": [d0, d1, ...], "requests": [d0, d1, ...],
                  "total_tokens": int, "avg_tokens_per_day": float},
        ...
      },
      "accounts": {
        "EO_INSPECTOR_GROQ_KEY_1": {"provider": "groq",
                                     "tokens": [d0, d1, ...]},
        ...
      }
    }

    Provider-level series SUM every account under that provider for each
    day -- mirrors how get_quota_snapshot()'s pct is already a
    per-account number, but a dashboard comparing "Groq vs Cerebras vs
    Mistral" wants one line per provider, not one per account. The
    per-account breakdown is kept too (under "accounts"), for a drill-
    down view or per-key debugging, without a second round of reads.
    """
    from eo.registry import AGENT_CAPABILITIES

    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    agent_infos = []
    for agent_key, info in AGENT_CAPABILITIES.items():
        provider = info.get("provider")
        key_id = _key_id_for(agent_key, provider)
        agent_infos.append((agent_key, provider, key_id))

    # Fix — this used to be a nested loop calling bus_read() once per
    # (account, date) pair, sequentially: accounts * days blocking round
    # trips in a row, each one a full HTTPS request (bus.py talks to
    # Upstash Redis over REST). That's what was turning a handful of
    # accounts x 7 days into dozens of sequential network calls and the
    # 30s+ wait on this endpoint. Every (account, date) key is
    # independent, so fetch them all in ONE round trip via MGET instead.
    all_keys = [
        f"usage:{provider}:{key_id}:{d}"
        for agent_key, provider, key_id in agent_infos
        for d in dates
    ]
    records = bus_read_many(all_keys, default={"requests": 0, "tokens": 0})

    results_by_agent = {agent_key: {} for agent_key, _, _ in agent_infos}
    for agent_key, provider, key_id in agent_infos:
        for d in dates:
            record = records[f"usage:{provider}:{key_id}:{d}"]
            results_by_agent[agent_key][d] = (record.get("tokens", 0), record.get("requests", 0))

    providers = {}
    accounts = {}

    for agent_key, provider, key_id in agent_infos:
        tokens_series = [results_by_agent[agent_key][d][0] for d in dates]
        requests_series = [results_by_agent[agent_key][d][1] for d in dates]

        accounts[agent_key] = {"provider": provider, "tokens": tokens_series, "requests": requests_series}

        if provider not in providers:
            providers[provider] = {"tokens": [0] * days, "requests": [0] * days}
        providers[provider]["tokens"] = [
            a + b for a, b in zip(providers[provider]["tokens"], tokens_series)
        ]
        providers[provider]["requests"] = [
            a + b for a, b in zip(providers[provider]["requests"], requests_series)
        ]

    for provider, series in providers.items():
        total = sum(series["tokens"])
        series["total_tokens"] = total
        series["avg_tokens_per_day"] = round(total / days, 1) if days else 0.0

    return {"dates": dates, "providers": providers, "accounts": accounts}


def get_usage_history_scoped(days: int = 7, domain: str = None, workspace_id: str = None) -> dict:
    """New in Part 2 §2.6 -- the "per project or per section" breakdown
    the blueprint asked for and the original TokenUsageTab.jsx view (per
    provider / per account, above) didn't cover. Reads the
    usage_by_domain:{domain}:{date} / usage_by_workspace:{workspace_id}:{date}
    keys utils/llm_client.py's log_usage() now writes (Part 2 §2.6)
    alongside its existing per-account key -- same MGET-in-one-round-trip
    approach as get_usage_history() above, same reasoning: don't turn a
    handful of dates into that many sequential network calls.

    Deliberately a SEPARATE function rather than new params bolted onto
    get_usage_history() above: that function's {dates, providers,
    accounts} return shape is a real, depended-on contract (the
    UsageHistoryPanel component already reads it), and a domain/workspace
    query has no "providers"/"accounts" breakdown to offer -- the
    secondary keys are pure aggregates, not tagged by provider/account.
    Changing that function's shape conditionally would make it harder to
    reason about for every existing caller; a new function with its own
    shape is the honest choice.

    domain and workspace_id, if both given, are read as two INDEPENDENT
    series, not intersected -- log_usage() doesn't write a joint
    domain+workspace key (e.g. "coding tasks in workspace X" specifically),
    since there's no caller asking for that specific cut yet; add a joint
    key later if one shows up. Passing neither returns both series as
    None rather than raising, so a caller can be lazy about the condition.

    Returns:
    {
      "dates": ["2026-07-01", ..., "2026-07-07"],
      "domain": {"tokens": [...], "requests": [...],
                 "total_tokens": int, "avg_tokens_per_day": float} | None,
      "workspace": {"tokens": [...], "requests": [...],
                    "total_tokens": int, "avg_tokens_per_day": float} | None,
    }
    """
    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    def _series_for(prefix: str, scope_id: str):
        if not scope_id:
            return None
        keys = [f"{prefix}:{scope_id}:{d}" for d in dates]
        records = bus_read_many(keys, default={"requests": 0, "tokens": 0})
        tokens_series = [records[f"{prefix}:{scope_id}:{d}"].get("tokens", 0) for d in dates]
        requests_series = [records[f"{prefix}:{scope_id}:{d}"].get("requests", 0) for d in dates]
        total = sum(tokens_series)
        return {
            "tokens": tokens_series,
            "requests": requests_series,
            "total_tokens": total,
            "avg_tokens_per_day": round(total / days, 1) if days else 0.0,
        }

    return {
        "dates": dates,
        "domain": _series_for("usage_by_domain", domain),
        "workspace": _series_for("usage_by_workspace", workspace_id),
    }


def get_ledger_event_counts(session_id: str) -> dict:
    """Phase 8c -- reads back the ledger_events:{session_id} counter
    utils/llm_client.py's _record_ledger_event() writes during the run,
    for api/task_runner.py's end-of-run_task() per-task summary. Pure
    read, no side effect (mirrors get_quota_snapshot()/
    get_rate_window_snapshot()'s own "no alerting/mutation on a plain
    read" posture).

    Returns {"wait": int, "reroute": int, "provider_failure": int} --
    "wait"/"reroute" are ledger-caused gating decisions (no call went
    out, or a call went out and hit a rate-limit response that the
    ledger then routed around); "provider_failure" is a call that
    actually went out and came back with a transient provider error.
    Keeping them apart is the whole point of this counter: a run that's
    slow because the ledger is being cautious is a very different
    signal from a run that's slow because providers are actually
    erroring.

    Zero counts (rather than missing) for a session_id with no calls
    yet, or where session_id is falsy -- same "empty dict/default
    rather than raising" posture bus_read's own default= param already
    gives every other reader in this module."""
    if not session_id:
        return {"wait": 0, "reroute": 0, "provider_failure": 0}
    return bus_read(f"ledger_events:{session_id}",
                     default={"wait": 0, "reroute": 0, "provider_failure": 0})