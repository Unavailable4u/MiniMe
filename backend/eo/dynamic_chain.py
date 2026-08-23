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
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import AGENT_CAPABILITIES

# Phase 5, Patch C: loud, non-fatal "we have no redundancy for this role
# right now" signal -- same posture as eo/executor.py's _trace_logger
# (a warning, never a raise; chain building must never fail just because
# logging did). Feeds Phase 8's observability work later; for now this is
# the whole signal.
_logger = logging.getLogger("eo.dynamic_chain")

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


def _rank_by_live_headroom(candidates: list, quota_status: dict) -> list:
    """Phase 5, Patch B — the new ranking rule: live headroom (Patch A's
    _headroom_score(), ascending -- least-full account first) as the
    primary signal, daily quota fraction (eo/panel.py's own
    _usage_fraction(), ascending) as the tiebreaker for whenever live
    data is equal or absent, e.g. a fresh account with nothing recorded
    yet reads 0.0 on both and falls back to daily-quota ordering same
    as before this patch."""
    from eo.panel import _usage_fraction   # deferred — see module docstring

    return sorted(
        candidates,
        key=lambda k: (_headroom_score(k), _usage_fraction(k, quota_status)),
    )


def _candidate_pool(role: str, quota_status: dict, exclude: set) -> list:
    """Phase 5, Patch B — replaces the old per-step delegation to
    eo/panel.py's _best_match(). _best_match() only ever ranks by daily
    quota_status (see its own docstring), so it has no way to prefer an
    account with real headroom *this minute* over one that merely looks
    good on a whole-day aggregate. This builds the same natural-role-
    tagged-first-then-full-pool candidate set _best_match() would, with
    the same cooldown/exclude filtering, but leaves the ranking itself
    to _rank_by_live_headroom() above instead of _best_match()'s own
    80%-cutoff logic -- Patch B's ordering already IS the selection
    rule, so there's no separate cutoff check layered on top here.

    Returns the full ranked candidate list (best first), not just the
    winner -- Patch C needs to see the rest of the pool to find a
    second-org candidate the provider-spread loop didn't reach."""
    from eo.panel import _is_cooling_down   # deferred — see module docstring

    natural_candidates = [
        key for key, info in AGENT_CAPABILITIES.items()
        if role in info.get("natural_roles", []) and key not in exclude
        and not _is_cooling_down(key, quota_status)
    ]
    if natural_candidates:
        return _rank_by_live_headroom(natural_candidates, quota_status)

    # No natural-role match at all (as opposed to _best_match()'s other
    # fall-through case -- every natural match over-cutoff -- which no
    # longer exists now that there's no cutoff check): fall back to the
    # full account pool, same three-tier "nothing left" ladder
    # _best_match() itself uses, so a caller mid-migration off that
    # function sees identical degenerate-case behavior.
    all_candidates = [
        k for k in AGENT_CAPABILITIES.keys()
        if k not in exclude and not _is_cooling_down(k, quota_status)
    ]
    if not all_candidates:
        all_candidates = [k for k in AGENT_CAPABILITIES.keys() if not _is_cooling_down(k, quota_status)]
    if not all_candidates:
        all_candidates = list(AGENT_CAPABILITIES.keys())

    return _rank_by_live_headroom(all_candidates, quota_status)


def _rank_accounts(role: str, quota_status: dict, exclude: set, max_steps: int) -> list:
    """Shared core of build_fallback_chain()/build_fallback_chain_excluding()
    below: same provider-spreading algorithm as agents/generic_worker.py's
    _build_fallback_chain(), generalized over a caller-supplied starting
    `exclude` set so a parallel worker pool can hand each of its own
    threads a chain that also skips whichever accounts its SIBLING
    threads already claimed (see build_fallback_chain_excluding()).

    Phase 5, Patch B: the per-step candidate pick now comes from
    _candidate_pool()/_rank_by_live_headroom() (live headroom first,
    daily quota as tiebreaker) instead of delegating to eo/panel.py's
    _best_match(). The provider-spreading loop structure itself --
    prefer a fresh provider this round, fall back to a repeat provider
    only if nothing fresh is left, stop when the whole pool is
    exhausted -- is unchanged."""
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
        pool = _candidate_pool(role, quota_status, exclude=provider_exclude)
        if not pool:
            # No fresh-provider candidate left this round -- allow a
            # repeat provider rather than leaving this chain slot empty.
            pool = _candidate_pool(role, quota_status, exclude=exclude)
        if not pool:
            break  # genuinely nothing left in the whole account pool
        candidate = pool[0]
        chain_keys.append(candidate)
        exclude.add(candidate)
        used_providers.add(AGENT_CAPABILITIES[candidate].get("provider"))

    return chain_keys


def _org_for(provider: str, key: str) -> str:
    """Phase 5, Patch C — the identity used to count "genuinely distinct
    organizations" for the cross-org redundancy guarantee below.

    Default: the AGENT_CAPABILITIES key itself. Every multi-key
    provider in this pool other than OpenRouter follows the same
    "N separate accounts, N separate keys" convention OR-0 (Phase 3g)
    confirmed specifically for Cerebras ("9 separate keys, from what
    the code's comments describe as genuinely separate accounts") --
    so, absent evidence otherwise, each key here defaults to counting
    as its own org. This deliberately does NOT collapse same-provider
    keys together: the provider-spread loop in _rank_accounts() above
    already tracks provider-level diversity for its own (outage-
    avoidance) purposes, and org-scoped-cap redundancy is a related
    but separate property from provider diversity.

    OpenRouter is the one deliberate exception, and this is the whole
    reason this helper exists rather than just using the key directly:
    OR-0 (Phase 3g) has NOT yet confirmed whether OpenRouter's
    50-1000/day, 20/min free-tier limits are scoped per API key or per
    account -- ASSUMPTION FLAGGED, ungrounded either way until OR-0's
    10-minute verification check (generate 2 keys under one account,
    exhaust one, see if the other is independently usable) actually
    runs. Until then, every OPENROUTER_* key collapses to the SAME org
    identity here -- the conservative default, since crediting N
    independent orgs for N keys that might actually share one
    account-level quota would silently reproduce the exact
    single-shared-key bug this codebase already paid to fix once (see
    output_organizer.py's and report_writer.py's comments on that
    prior Cerebras incident). Revisit this the moment OR-0 lands with
    a real answer -- see OR-5's note in the plan for the update this
    function will need either way (either it stays exactly as-is,
    confirmed-conservative-and-correct, or every OPENROUTER_* key gets
    its own real per-account org id once that's known)."""
    if provider == "openrouter":
        return "openrouter"
    return key


def _ensure_cross_org_redundancy(role: str, chain_keys: list, quota_status: dict,
                                  start_exclude: set) -> list:
    """Phase 5, Patch C — post-pass on a chain _rank_accounts() already
    built: guarantee at least 2 chain entries from genuinely distinct
    orgs (per _org_for() above) whenever the pool actually allows it.

    Multiple keys within the SAME org don't provide real redundancy
    against an org-scoped cap (this module's own docstring traces the
    2026-08-12 incident this whole file exists to prevent a repeat of)
    -- so a chain that only ever reached one distinct org, even if it
    already has 2+ entries (e.g. two OPENROUTER_* keys, which _org_for()
    treats as the same org), is not actually redundant yet.

    An empty chain_keys (build_fallback_chain() found NOTHING at all
    for this role) is left untouched -- that's a total build failure,
    not a redundancy gap, and belongs to whatever the caller's own
    empty-chain handling already does, not this function's log line.

    If the pool has a second-org candidate the provider-spread loop
    in _rank_accounts() didn't reach (e.g. it stopped after max_steps,
    or every fresh-provider candidate that round happened to be
    excluded), force-append it -- deliberately allowed to push the
    chain one entry past max_steps, since real redundancy here matters
    more than the round-count heuristic. If genuinely no second-org
    candidate exists anywhere in the pool, emit a loud
    chain_redundancy_gap warning instead of silently returning a
    chain with no real redundancy -- a real signal worth surfacing at
    chain-build time (feeds Phase 8) rather than discovering it only
    when the sole org's accounts all fail together."""
    if not chain_keys:
        return chain_keys

    orgs_in_chain = {
        _org_for(AGENT_CAPABILITIES[k].get("provider"), k) for k in chain_keys
    }
    if len(orgs_in_chain) >= 2:
        return chain_keys

    exclude = set(start_exclude) | set(chain_keys)
    # Whole-pool search, not provider-restricted -- _rank_accounts()'s
    # provider-spread loop already had its shot at spreading and came
    # up short (that's how we got here); this is the belt-and-suspenders
    # pass, not a repeat of the same search with the same blind spots.
    #
    # _candidate_pool()'s own last-resort fallback tier (mirroring
    # _best_match()'s degenerate case) deliberately IGNORES `exclude`
    # as a last resort, on the theory that a repeat account beats no
    # account at all for _rank_accounts()'s own retry loop. That's the
    # wrong instinct here -- an account this function was explicitly
    # told to skip (already in the chain, or a sibling worker's claimed
    # account) must never come back via this search -- so the exclude
    # filter is re-applied explicitly rather than trusted to the pool.
    pool = [k for k in _candidate_pool(role, quota_status, exclude=exclude) if k not in exclude]
    second_org_candidate = next(
        (k for k in pool
         if _org_for(AGENT_CAPABILITIES[k].get("provider"), k) not in orgs_in_chain),
        None,
    )
    if second_org_candidate is not None:
        return chain_keys + [second_org_candidate]

    _logger.warning(
        "chain_redundancy_gap: role=%r has no second-org candidate available "
        "right now -- chain=%r is running with only ONE distinct org's worth "
        "of redundancy (orgs=%r). Every account belonging to any other org "
        "is either excluded, cooling down, or not provisioned for this role.",
        role, chain_keys, sorted(orgs_in_chain),
    )
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
    chain_keys = _ensure_cross_org_redundancy(role, chain_keys, quota_status, start_exclude=set())
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
    chain_keys = _ensure_cross_org_redundancy(role, chain_keys, quota_status,
                                               start_exclude=set(exclude_keys))
    return [chain_step_for(k) for k in chain_keys]
