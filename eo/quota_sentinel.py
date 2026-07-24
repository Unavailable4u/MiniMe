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
from datetime import date, timedelta, datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read as bus_read, read_many as bus_read_many
from utils.llm_client import QUOTA_CONFIG
from relay.emitter import emit_event
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


def get_quota_snapshot() -> dict:
    """Returns {agent_key: {"used": int, "quota": int|None, "pct": float|None,
    "cooldown_until": float|None, "cooling_down": bool}} for every account
    in AGENT_CAPABILITIES, reading TODAY's real usage from the exact keys
    generate_text() already writes. quota/pct are None for providers
    QUOTA_CONFIG deliberately omits (cloudflare, mistral) — an honest "no
    verified number" rather than a guess.

    Fix B (reliability guide, §3 "Fix B"): also reads back
    cooldown_until:{provider}:{key_id} — the UTC timestamp
    utils/llm_client.py's generate_text() writes whenever a call to that
    account fails with a transient (429/5xx/timeout) error, parsed from
    the provider's own retry-after signal where one is available. This
    is a SEPARATE constraint from daily token usage (`pct` above): an
    account can be well under its 80% daily-token cutoff and still be
    mid-cooldown from a recent rate-limit response, or vice versa. Both
    are surfaced here so eo/panel.py's _best_match() can check them
    independently instead of conflating "out of tokens for the day"
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
    now = datetime.now(timezone.utc).timestamp()

    snapshot = {}
    for agent_key, provider, key_id in agent_infos:
        record = usage_records[f"usage:{provider}:{key_id}:{today}"]
        used = record.get("tokens", 0)
        quota = QUOTA_CONFIG.get(provider)
        pct = (used / quota) if quota else None
        cooldown_until = cooldown_records.get(f"cooldown_until:{provider}:{key_id}")
        cooling_down = bool(cooldown_until and cooldown_until > now)
        snapshot[agent_key] = {
            "used": used, "quota": quota, "pct": pct,
            "cooldown_until": cooldown_until, "cooling_down": cooling_down,
        }
    return snapshot


def check_and_alert(session_id: str = None) -> None:
    """Call this periodically (or after each generate_text() call, if you
    want it real-time) to fire quota_alert for anything that's crossed
    80%. Deliberately separate from get_quota_snapshot() so reading a
    snapshot for hiring decisions (Part 6) never has an alerting side
    effect."""
    snapshot = get_quota_snapshot()
    for agent_key, info in snapshot.items():
        if info["pct"] is not None and info["pct"] >= 0.8:
            emit_event("quota_alert", session_id, agent="quota_sentinel",
                       payload={"agent_key": agent_key, "used": info["used"],
                                "quota": info["quota"], "pct": round(info["pct"], 3)})


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