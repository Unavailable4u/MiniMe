"""
api/routes/system.py

B6, piece 2 — the small, low-risk stuff: health check, quota snapshot,
usage history. No shared state with any other route group, which is
exactly why this was the second piece split out (after tasks.py) —
good practice for the split-and-verify pattern before tackling the
bigger, more entangled domains (workspaces, notebooks, etc.) later.

Deliberately NOT included here: /api/capabilities. That route shares
CAPABILITIES_MANIFEST and _capability_label() with the not-yet-split
notebooks routes still in api/server.py — moving it alone would either
duplicate that manifest or force an awkward import back into
api.server (circular). It moves later, alongside the notebooks split.
"""

import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query

from api.deps import require_auth
from eo import chat_page_cache, db
from eo.quota_sentinel import (
    get_quota_snapshot,
    get_rate_window_snapshot,
    get_usage_history,
    get_usage_history_scoped,
)
from memory import bus

router = APIRouter()

# Perf audit item #4.4/#7 follow-up: how many round trips each backend
# gets timed over for /api/system/backend-latency-probe below. 5 is
# enough to see min/max spread (e.g. one slow outlier vs. a
# consistently slow backend) without turning the endpoint itself into
# a slow request. Not meant to be a rigorous benchmark -- just enough
# real numbers to answer "is Redis actually faster than Postgres here,
# or is this cache paying REST overhead for nothing."
_LATENCY_PROBE_SAMPLES = 5


def _redact_host(raw_url: str | None) -> str | None:
    """Returns just the host from a connection URL/DSN -- e.g.
    'db.xyz.supabase.co' or 'us1-xxxx.upstash.io' -- with no
    credentials, path, or query string, so this is safe to return in
    an API response. The host is also the thing that actually answers
    "which region is this in": Upstash and Supabase/RDS-style hosts
    both encode region in the hostname (e.g. an 'us-east-1' or 'us1'
    segment), so comparing these two strings directly answers the
    open question from the perf audit without needing any extra
    provider-specific API calls. None if the env var isn't set."""
    if not raw_url:
        return None
    return urlparse(raw_url).hostname


def _timed_calls(fn, samples: int) -> dict:
    """Runs fn() `samples` times back-to-back, timing each call with
    perf_counter(), and returns {"avg_ms", "min_ms", "max_ms",
    "samples_ms"}. Sequential on purpose (not concurrent) -- this is
    measuring the per-call latency a single request pays, which is
    exactly the cost the perf audit's before_seq page-cache path pays
    sequentially today, not this backend's max throughput."""
    times_ms = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        times_ms.append((time.perf_counter() - start) * 1000)
    return {
        "avg_ms": round(sum(times_ms) / len(times_ms), 2),
        "min_ms": round(min(times_ms), 2),
        "max_ms": round(max(times_ms), 2),
        "samples_ms": [round(t, 2) for t in times_ms],
    }


def _postgres_ping() -> None:
    # trusted=True: this touches no RLS'd table and has no acting
    # user, same category db.cursor()'s own docstring documents for
    # audit_log-style internal reads -- see that docstring for why
    # trusted=True is the right call here rather than inventing a
    # user_id this probe doesn't have.
    with db.cursor(trusted=True) as cur:
        cur.execute("select 1")
        cur.fetchone()


@router.get("/api/health")
def health():
    return {"status": "ok"}


@router.get("/api/system/backend-latency-probe", dependencies=[Depends(require_auth)])
def backend_latency_probe():
    # Perf audit item #4.4/#7 follow-up: answers the open question
    # from the audit -- "is Redis's per-call REST round trip actually
    # faster than Postgres's for this workload, or could a co-location
    # mismatch mean the page cache is paying MORE latency than the
    # Postgres query it exists to avoid, independent of hit rate."
    # Reports both backends' host (for a quick visual region check)
    # and real, timed round-trip latency (the number that actually
    # answers the question, since region strings alone can mislead --
    # e.g. same-continent-different-metro still adds real latency).
    return {
        "postgres": {
            "host": _redact_host(db.DATABASE_URL),
            "latency_ms": _timed_calls(_postgres_ping, _LATENCY_PROBE_SAMPLES),
        },
        "redis": {
            "host": _redact_host(bus.UPSTASH_REDIS_REST_URL),
            "latency_ms": _timed_calls(bus.ping, _LATENCY_PROBE_SAMPLES),
        },
    }


@router.get("/api/system/chat-page-cache-stats", dependencies=[Depends(require_auth)])
def chat_page_cache_stats():
    # Perf audit item #3: surfaces the global hit/miss counters
    # eo/chat_store.py's before_seq branch now records on every
    # cache-eligible lookup (see eo/chat_page_cache.py's
    # record_cache_result()/get_cache_stats()). This is the number to
    # pull before deciding whether item #7's Redis page cache is worth
    # keeping enabled, given B4's per-call REST overhead -- a low
    # hit_rate here means most before_seq page loads are paying for a
    # Redis round trip that never turns into a served hit. Just a
    # snapshot read, no auth-scoping needed beyond "logged in" since
    # these are aggregate counters, not per-user or per-chat data.
    return chat_page_cache.get_cache_stats()


@router.get("/api/system/db-pool-stats", dependencies=[Depends(require_auth)])
def db_pool_stats():
    # Perf audit §4.6 / priority #9 (part 3): surfaces the global
    # getconn()-outcome counters eo/db.py's _getconn_with_retry() records
    # on every checkout (see that function's own docstring and
    # get_pool_stats()). This is the number to pull before raising
    # DB_POOL_MAX / api/server.py's APP_THREAD_POOL_SIZE any further --
    # a nonzero "exhausted" means real requests got a 503 today, not a
    # hypothetical one. Same "aggregate counters, no per-user scoping
    # needed beyond logged-in" reasoning as chat_page_cache_stats above.
    return db.get_pool_stats()



@router.get("/api/quota", dependencies=[Depends(require_auth)])
def quota():
    # Phase 8a — today's daily-quota figures (existing) plus a
    # `rate_windows` section keyed by the same agent_key, giving the
    # dashboard the live minute/daily headroom state from rate_ledger.py
    # (token-based providers and OpenRouter-style request-based
    # providers both covered, per get_rate_window_snapshot()'s
    # docstring) alongside the once-a-day usage figures already here.
    return {
        **get_quota_snapshot(),
        "rate_windows": get_rate_window_snapshot(),
    }


@router.get("/api/usage/history", dependencies=[Depends(require_auth)])
def usage_history(
    days: int = Query(7, ge=1, le=90),
    domain: str | None = Query(None),
    workspace_id: str | None = Query(None),
):
    # Cross-session, persisted day-by-day usage -- reads the same
    # usage:{provider}:{key_id}:{date} records /api/quota already reads
    # for today, just repeated across the last `days` calendar dates.
    # See eo/quota_sentinel.py's get_usage_history() docstring for the
    # exact response shape.
    #
    # Part 2 §2.6 -- when domain and/or workspace_id is given, this
    # branches to get_usage_history_scoped() instead, returning
    # {dates, domain, workspace} (see that function's docstring) rather
    # than {dates, providers, accounts}. Same route, response shape
    # depends on query params -- exactly the way `days` already changes
    # this endpoint's window without becoming a separate route.
    if domain or workspace_id:
        return get_usage_history_scoped(days=days, domain=domain, workspace_id=workspace_id)
    return get_usage_history(days=days)
