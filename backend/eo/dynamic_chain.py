"""
eo/dynamic_chain.py — shared quota-aware, cooldown-aware, multi-provider
fallback-chain builder for REAL-ACTION agents.

Background: agents/generic_worker.py already solved this problem for
every role it routes (Fix A / Fix 4a in that file: _build_fallback_chain()
/ _chain_step_for() / _dynamic_max_chain_steps()) -- it ranks candidate
accounts by live quota, skips anything currently cooling down
(eo/panel.py's _is_cooling_down()), and spreads the chain across
DIFFERENT providers so one provider-wide outage/rate-limit event can't
take out the whole chain.

But three REAL_ACTION_ROLES modules never got this: agents/
hardware_speccer.py, agents/architecture_diagrammer.py, and agents/
part_price_finder.py each call utils.llm_client.generate_text() with
their own hardcoded, length-1 (or length-2) module-level CHAIN pointing
at one specific key_env. None of them are tagged in eo/registry.py's
AGENT_CAPABILITIES either, so even if they wanted to use
_best_match()/_build_fallback_chain(), there was nothing for those
functions to select from. Root-caused 2026-08-12: a single exhausted
GROQ_API_KEY took down hardware_speccer's price lookups AND
architecture_diagrammer's own generation in the same run, back to back,
because both were quietly sharing that one unmonitored, un-fallback-able
key.

This module is that same logic, extracted so any real-action agent can
reuse it without duplicating (or drifting out of sync with)
generic_worker.py's copy. generic_worker.py itself is left untouched --
it already works -- this is additive.

IMPORTANT -- import this LAZILY (inside a function, not at module level)
from any agents/*.py file that eo/registry.py imports at load time. This
module imports eo.registry.AGENT_CAPABILITIES at ITS OWN module level, so
a module-level import from an eagerly-imported agent module would close
the exact circular loop agents/generic_worker.py's own docstring already
flags and avoids the same way:
    eo.registry -> agents.<some_agent> -> eo.dynamic_chain -> eo.registry
By the time an agent's run_*() function is actually CALLED (not just
imported), eo.registry has always finished loading, so a deferred,
inside-the-function import is safe.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import AGENT_CAPABILITIES

# Kept in sync with agents/generic_worker.py's own PROVIDER_DEFAULT_MODEL
# (see that file for the full per-provider pinning history/reasoning).
# Duplicated on purpose, not imported -- this module has zero dependency
# on agents/generic_worker.py in either direction, so neither file's
# import order can ever create a cycle with the other.
PROVIDER_DEFAULT_MODEL = {
    # llama-3.3-70b-versatile decommissioned by Groq; migrated to
    # openai/gpt-oss-120b, same single-value pick and reasoning as
    # agents/generic_worker.py's own copy of this dict (see that file's
    # comment for the full trace) -- kept in sync deliberately.
    "groq": "openai/gpt-oss-120b",
    "cerebras": "gpt-oss-120b",
    # OR-3f: kept in sync with agents/generic_worker.py's own addition of
    # this entry (see that file for the full reasoning) -- needed for the
    # same reason there: this module's own _chain_step_for()-equivalent
    # looks up PROVIDER_DEFAULT_MODEL.get(provider, "") for whatever
    # AGENT_CAPABILITIES-selected account it lands on, and OpenRouter
    # accounts are now a real possibility in that pool.
    "openrouter": "openrouter/free",
    "mistral": "mistral-large-latest",
    "gemini": "gemini-3.1-flash-lite",
    "huggingface": "openai/gpt-oss-120b:fastest",
    "cloudflare": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
}

# Same reasoning as generic_worker.py's own constants: 3 accounts deep is
# enough to survive one exhausted account plus one full provider-wide
# outage; the dynamic ceiling avoids turning a large multi-provider
# roster into a dozen sequential HTTP round-trips before giving up.
MAX_CHAIN_STEPS = 3
MAX_CHAIN_STEPS_CEILING = 6


def _headroom_score(key: str) -> float:
    """Phase 5, Patch A — pure read helper, no behavior change yet:
    _rank_accounts() doesn't consult this until Patch B wires it in.

    Returns 0.0 (fully open) .. 1.0 (no headroom left) for how close
    THIS account is, right now, to whichever ceiling
    rate_ledger.can_proceed()/reserve() would actually gate it against
    for its default model (PROVIDER_DEFAULT_MODEL above) — the "live
    headroom" signal Phase 5's plan calls for, as opposed to the
    static/daily quota_status figure eo/panel.py's _best_match() and
    _usage_fraction() already rank by.

    Derives (provider, key_id, model) the same way chain_step_for() and
    eo/concurrency_gate.py's _candidate_identity() both already do:
    key_id is the AGENT_CAPABILITIES key itself (key_env) for every
    provider except cloudflare, where it's that account's own
    account_id_env (info["key_id"], falling back to the key itself if
    somehow absent).

    Precedence mirrors rate_ledger.can_proceed()'s own rule ordering
    (provider-reported over the self-tracked window): an exact
    remaining_tokens/remaining_requests == 0 from a live provider
    response is the strongest possible "no headroom" signal available,
    so that short-circuits straight to 1.0 before falling back to the
    sliding window's own pct_used, then ("requests" gating mode only)
    the daily pct_used. Any provider/model with no verified
    QUOTA_CONFIG limit to compute a pct against, or with no usage
    recorded for it yet at all, reads as 0.0 -- fully open -- matching
    rate_ledger.py's own fail-open posture everywhere else in that
    module's family, and keeping an unconfigured/never-used account
    from being unfairly penalized just for having no history.
    """
    from utils import rate_ledger  # deferred — see module docstring

    info = AGENT_CAPABILITIES.get(key)
    if info is None:
        return 0.0
    provider = info.get("provider")
    if provider is None:
        return 0.0
    model = PROVIDER_DEFAULT_MODEL.get(provider, "")
    key_id = info.get("key_id", key) if provider == "cloudflare" else key

    snapshot = rate_ledger.headroom_snapshot(provider, key_id, model)

    reported = snapshot.get("provider_reported")
    if reported is not None:
        remaining = (reported.get("remaining_requests")
                     if snapshot.get("gating_mode") == "requests"
                     else reported.get("remaining_tokens"))
        if remaining is not None and remaining <= 0:
            return 1.0

    window_pct = (snapshot.get("sliding_window") or {}).get("pct_used")
    if window_pct is not None:
        return max(0.0, min(1.0, window_pct))

    daily = snapshot.get("daily")
    if daily is not None and daily.get("pct_used") is not None:
        return max(0.0, min(1.0, daily["pct_used"]))

    return 0.0


def _cloudflare_token_env_for(account_id_env: str) -> str:
    """Same derivation as agents/generic_worker.py's own
    _cloudflare_token_env_for() -- mirrored, not imported, for the same
    lazy-import-safety reason as PROVIDER_DEFAULT_MODEL above."""
    if account_id_env.startswith("CLOUDFLARE_ACCOUNT_ID_"):
        n = account_id_env.rsplit("_", 1)[-1]
        return f"CLOUDFLARE_API_KEY_{n}"
    if account_id_env.startswith("CF_SCANNER_RESERVE_") and account_id_env.endswith("_ACCOUNT_ID"):
        n = account_id_env[len("CF_SCANNER_RESERVE_"):-len("_ACCOUNT_ID")]
        return f"CF_SCANNER_RESERVE_{n}_API_TOKEN"
    raise ValueError(f"Don't know how to derive a token_env for account_id_env {account_id_env!r} "
                     f"— add its naming pattern to _cloudflare_token_env_for().")


def chain_step_for(agent_key: str) -> dict:
    """agent_key (an AGENT_CAPABILITIES key) -> a ready-to-use
    generate_text() chain step dict."""
    info = AGENT_CAPABILITIES[agent_key]
    provider = info["provider"]
    step = {"provider": provider, "model": PROVIDER_DEFAULT_MODEL.get(provider, ""), "key_env": agent_key}
    if provider == "cloudflare":
        account_id_env = info.get("key_id", agent_key)
        step = {"provider": provider, "model": PROVIDER_DEFAULT_MODEL.get(provider, ""),
                 "account_id_env": account_id_env,
                 "token_env": _cloudflare_token_env_for(account_id_env)}
    return step


def _dynamic_max_chain_steps(quota_status: dict) -> int:
    from eo.panel import _is_cooling_down   # deferred — see module docstring

    live_providers = {
        info.get("provider")
        for key, info in AGENT_CAPABILITIES.items()
        if not _is_cooling_down(key, quota_status)
    }
    return max(MAX_CHAIN_STEPS, min(MAX_CHAIN_STEPS_CEILING, len(live_providers)))


def _rank_accounts(role: str, quota_status: dict, exclude: set, max_steps: int) -> list:
    """Shared core of build_fallback_chain()/build_fallback_chain_excluding()
    below: same provider-spreading algorithm as agents/generic_worker.py's
    _build_fallback_chain(), generalized over a caller-supplied starting
    `exclude` set so a parallel worker pool can hand each of its own
    threads a chain that also skips whichever accounts its SIBLING
    threads already claimed (see build_fallback_chain_excluding())."""
    from eo.panel import _best_match   # deferred — see module docstring

    chain_keys = []
    used_providers = {
        AGENT_CAPABILITIES[k].get("provider") for k in exclude if k in AGENT_CAPABILITIES
    }
    exclude = set(exclude)

    for _ in range(max_steps):
        provider_exclude = exclude | {
            key for key, info in AGENT_CAPABILITIES.items()
            if info.get("provider") in used_providers
        }
        candidate = _best_match(role, quota_status, exclude=provider_exclude)
        if candidate is None:
            # No fresh-provider candidate left this round -- allow a
            # repeat provider rather than leaving this chain slot empty.
            candidate = _best_match(role, quota_status, exclude=exclude)
        if candidate is None:
            break  # genuinely nothing left in the whole account pool
        chain_keys.append(candidate)
        exclude.add(candidate)
        used_providers.add(AGENT_CAPABILITIES[candidate].get("provider"))

    return chain_keys


def build_fallback_chain(role: str, quota_status: dict = None, max_steps: int = None) -> list:
    """Returns a ready-to-use generate_text() chain (list of step dicts),
    quota-ranked and spread across providers, cooldown-aware. This is the
    fix for agents/hardware_speccer.py and agents/architecture_diagrammer.py's
    old length-1 hardcoded CHAIN: instead of one fixed key_env, this picks
    the least-used, not-currently-cooling-down account tagged for `role`
    (falling back to the whole pool if nothing is tagged), then adds up
    to `max_steps` more accounts on OTHER providers so a single exhausted
    key or a provider-wide outage no longer ends the call.

    role: the AGENT_CAPABILITIES natural_roles tag to prefer (e.g.
        "hardware_speccer", "architecture_diagrammer"). If nothing is
        tagged for this role yet, _best_match() already degrades
        gracefully to the full account pool ranked by quota alone -- see
        eo/panel.py's own docstring.
    quota_status: pass eo.quota_sentinel.get_quota_snapshot() if you
        already fetched it this call (saves a second snapshot fetch);
        None fetches a fresh one.
    max_steps: None computes it dynamically from how many providers
        currently have a live account (see _dynamic_max_chain_steps),
        same as generic_worker.py's own Fix 4a.

    Returns [] only if literally every configured account is excluded/
    cooling down -- callers should fall back to their own static CHAIN
    in that case (belt-and-suspenders, should be very rare).
    """
    from eo.quota_sentinel import get_quota_snapshot   # deferred — see module docstring

    if quota_status is None:
        quota_status = get_quota_snapshot()
    if max_steps is None:
        max_steps = _dynamic_max_chain_steps(quota_status)

    chain_keys = _rank_accounts(role, quota_status, exclude=set(), max_steps=max_steps)
    return [chain_step_for(k) for k in chain_keys]


def build_fallback_chain_excluding(role: str, exclude_keys, quota_status: dict = None) -> list:
    """Same as build_fallback_chain(), but starting from a caller-supplied
    `exclude_keys` set/list -- for parallel worker pools (see
    agents/hardware_speccer.py's _populate_prices()) that already handed
    out N distinct accounts to N worker threads and want each thread's
    OWN fallback chain to skip the accounts its sibling threads are
    already using (and their providers, same provider-spreading logic),
    not just the one account that thread started on.
    """
    from eo.quota_sentinel import get_quota_snapshot   # deferred — see module docstring

    if quota_status is None:
        quota_status = get_quota_snapshot()
    max_steps = _dynamic_max_chain_steps(quota_status)

    chain_keys = _rank_accounts(role, quota_status, exclude=set(exclude_keys), max_steps=max_steps)
    return [chain_step_for(k) for k in chain_keys]
