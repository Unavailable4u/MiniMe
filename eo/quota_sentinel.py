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
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read as bus_read, read_many as bus_read_many
from utils.llm_client import QUOTA_CONFIG
from relay.emitter import emit_event


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
    """Returns {agent_key: {"used": int, "quota": int|None, "pct": float|None}}
    for every account in AGENT_CAPABILITIES, reading TODAY's real usage
    from the exact keys generate_text() already writes. quota/pct are
    None for providers QUOTA_CONFIG deliberately omits (cloudflare,
    mistral) — an honest "no verified number" rather than a guess."""
    from eo.registry import AGENT_CAPABILITIES
    today = date.today().isoformat()
    snapshot = {}
    for agent_key, info in AGENT_CAPABILITIES.items():
        provider = info.get("provider")
        key_id = _key_id_for(agent_key, provider)
        record = bus_read(f"usage:{provider}:{key_id}:{today}", default={"requests": 0, "tokens": 0})
        used = record.get("tokens", 0)
        quota = QUOTA_CONFIG.get(provider)
        pct = (used / quota) if quota else None
        snapshot[agent_key] = {"used": used, "quota": quota, "pct": pct}
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