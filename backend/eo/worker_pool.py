"""
eo/worker_pool.py — shared quota-aware, fairness-ranked worker selection
for any parallel-fan-out pool (Part 6 §6.2).

Extracted from agents/code_writers.py's _eligible_pool()/_select_workers(),
which were already generic over anything with a ROLE_TAG in
AGENT_CAPABILITIES's natural_roles — the only thing hardcoded to coding
was the constant ROLE_TAG = "implementer" itself. Both functions now take
role_tag as a parameter instead. No behavior change for the existing
coding pipeline: agents/code_writers.py calls these with
role_tag="implementer" and gets byte-for-byte the same selection it
always did.

Any future parallel pool (agents/content_adapter_pool.py being the first,
Part 6 §6.2) gets the exact same fairness rotation for free by calling
these with its own role_tag, instead of a copy-pasted second
implementation that could drift out of sync with this one.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot
from relay.emitter import emit_event  # NEW — CO4 patch 3


def _eligible_pool(role_tag: str) -> list:
    """Every account tagged for this role — base AND reserve accounts
    alike. Mode plays no part in who's ELIGIBLE; only in how many of
    them get used at once (see _select_workers(), below)."""
    return [key for key, info in AGENT_CAPABILITIES.items() if role_tag in info.get("natural_roles", [])]


def _select_workers(role_tag: str, worker_count: int, key_override=None,
                     session_id: str = None, agent_name: str = None) -> list:
    """Panel-driven hires (Part 5's key_override) always win outright —
    the Panel already made a specific, informed choice. Otherwise, rank
    the FULL eligible pool (base + reserve together) by today's live
    usage and take the `worker_count` least-used accounts. This is the
    fairness rotation: a reserve account with less usage than a base
    account gets picked ahead of it on a totally ordinary Simple-mode
    run — it's not gated behind Expert/Beast, only the COUNT is.

    role_tag selects which pool to rank: "implementer" for
    agents/code_writers.py, "content_writer" for
    agents/content_adapter_pool.py, and so on for any future pool. A
    role_tag can safely overlap accounts with another tag (e.g.
    "content_writer" reusing the same keys as "implementer") — the
    quota snapshot ranking spreads load across whatever's least-used
    regardless of which tag(s) an account carries.

    session_id/agent_name: NEW — CO4 patch 3, both optional. Only used
    for the "worker_pool_selection" event emitted below when the real
    quota-ranked rotation actually runs — this is the real fallback/
    rotation decision point (confirmed by reading this file directly:
    neither this function nor eo/quota_sentinel.py emitted anything
    here before this patch). Deliberately NOT emitted on the
    key_override path just below: that's the Panel's own explicit
    choice, not a rotation decision, so there's nothing about "which
    account got picked instead of another" to report there.
    emit_event() is already a no-op when session_id is None, so callers
    that don't pass one (there are several — see agents/reviewer.py,
    agents/extraction_table_builder.py etc., which have their own
    private _select_workers() and never touch this shared one at all)
    are entirely unaffected.
    """
    if key_override:
        return key_override if isinstance(key_override, list) else [key_override]
    pool = _eligible_pool(role_tag)
    if not pool:
        raise RuntimeError(f"worker_pool: no accounts tagged '{role_tag}' in AGENT_CAPABILITIES.")
    snapshot = get_quota_snapshot()
    ranked = sorted(pool, key=lambda k: (snapshot.get(k) or {}).get("pct") or 0.0)
    selected = ranked[:worker_count]
    emit_event("worker_pool_selection", session_id=session_id, agent=agent_name,
               payload={"role_tag": role_tag, "worker_count": worker_count,
                        "pool_size": len(pool), "selected": selected})
    return selected