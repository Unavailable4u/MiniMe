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

from fastapi import APIRouter, Depends, Query

from api.deps import require_auth
from eo.quota_sentinel import (
    get_quota_snapshot,
    get_rate_window_snapshot,
    get_usage_history,
    get_usage_history_scoped,
)

router = APIRouter()


@router.get("/api/health")
def health():
    return {"status": "ok"}


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
