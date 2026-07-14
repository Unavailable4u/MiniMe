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


def _eligible_pool(role_tag: str) -> list:
    """Every account tagged for this role — base AND reserve accounts
    alike. Mode plays no part in who's ELIGIBLE; only in how many of
    them get used at once (see _select_workers(), below)."""
    return [key for key, info in AGENT_CAPABILITIES.items() if role_tag in info.get("natural_roles", [])]


def _select_workers(role_tag: str, worker_count: int, key_override=None) -> list:
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
    """
    if key_override:
        return key_override if isinstance(key_override, list) else [key_override]
    pool = _eligible_pool(role_tag)
    if not pool:
        raise RuntimeError(f"worker_pool: no accounts tagged '{role_tag}' in AGENT_CAPABILITIES.")
    snapshot = get_quota_snapshot()
    ranked = sorted(pool, key=lambda k: (snapshot.get(k) or {}).get("pct") or 0.0)
    return ranked[:worker_count]