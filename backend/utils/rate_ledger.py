"""
utils/rate_ledger.py — Phase 2 of the reliability overhaul (see
reliability_overhaul_plan.md §PHASE 2).

Live, minute-granularity headroom tracking for the scope providers like
Groq actually enforce a TPM/RPM ceiling against. Backed by the existing
`bus` (the same mechanism utils/llm_client.py's cooldown_until:{provider}:
{key_id} already uses -- see _set_cooldown()/_is_cooling_down()), so every
concurrent process/worker reads and writes the same shared state instead
of each guessing in isolation from its own memory.

SCOPING NOTE: the plan (and providers' own docs) describe this as
"(organization, model)" scoping. This codebase has no separate "org"
concept -- a `key_id` (an API key / account, see llm_client.py's own
key_id derivation for groq/cerebras/mistral/cloudflare) IS the org-scoped
unit here, identical to what cooldown_until:{provider}:{key_id} already
uses. So every function below takes (provider, key_id, model) rather than
(org, model) -- key_id fills the "org" role, consistent with the existing
cooldown convention rather than inventing a second, parallel identity for
the same thing.

Two signals, provider-reported preferred (Algorithm, plan §PHASE 2):

1. Provider-reported headroom (record_headroom) -- authoritative when
   present. Groq/OpenAI-compatible APIs expose x-ratelimit-remaining-
   tokens / x-ratelimit-remaining-requests / x-ratelimit-reset-tokens (or
   whatever the SDK response object surfaces) on every response, success
   or error. Cheapest and most accurate signal available; write it
   straight to the bus after every call that has it.

2. Self-tracked sliding window (record_usage / can_proceed's fallback) --
   used both as the fallback once provider headroom data is stale/absent,
   and as the PRE-FLIGHT gate before any provider-reported data exists at
   all for this (provider, key_id, model) yet. A real sliding window (5s
   slices, sum of the trailing 60s), not a naive fixed-window reset --
   fixed windows let you burst 2x the limit right at the boundary.

Every write function here follows the same "never raise" contract
_set_cooldown()/log_usage() already use elsewhere in this codebase: a
ledger bookkeeping failure must never take down the real LLM call that
triggered it. Every read function fails open (treats the account as
having headroom) on a bad/missing read, for the same reason
_is_cooling_down() fails open -- a false "no headroom" would incorrectly
skip/delay a perfectly good account, while failing open costs at most one
wasted attempt if the account genuinely is out of headroom (it'll just
fail again and record_headroom() will correct the picture immediately
after).
"""
import sys
import os
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read as bus_read, write as bus_write

# Sliding-window tuning. 5-second slices, 60-second trailing sum -- matches
# the plan's own numbers (§PHASE 2 "Algorithm", point 2).
_SLICE_SECONDS = 5
_WINDOW_SECONDS = 60
# Both bus records are short-lived by nature (a stale headroom reading or
# a >60s-old usage slice is useless for gating), so both are written with
# a bounded TTL rather than left to accumulate in Redis forever. Generous
# buffer over the window/reset itself so a slightly-delayed read still
# sees the record.
_HEADROOM_DEFAULT_TTL = 120
_WINDOW_TTL = _WINDOW_SECONDS + 60


def _headroom_key(provider: str, key_id: str, model: str) -> str:
    return f"rate_ledger:{provider}:{key_id}:{model}:headroom"


def _window_key(provider: str, key_id: str, model: str) -> str:
    return f"rate_ledger:{provider}:{key_id}:{model}:window"


def _tpm_limit_for(provider: str, model: str):
    """Looks up QUOTA_CONFIG[provider][model]["tpm"] -- the fallback
    sliding window's own ceiling. Imported lazily (not at module level):
    Phase 3 wires this module INTO utils/llm_client.py (generate_text()
    calls can_proceed()/record_headroom()/record_usage()), so a
    module-level `from utils.llm_client import QUOTA_CONFIG` here would
    be a circular import the moment that wiring lands -- same reasoning
    eo/quota_sentinel.py's _key_id_for()/_model_for() already documented
    for their own deferred eo.registry / agents.generic_worker imports.
    Returns None if this provider/model has no verified tpm figure (e.g.
    mistral only publishes rps in some entries) -- callers treat that as
    "can't gate on the sliding window, fail open" rather than fabricating
    a number.
    """
    from utils.llm_client import QUOTA_CONFIG
    return QUOTA_CONFIG.get(provider, {}).get(model, {}).get("tpm")


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _prune_window(slices: dict, now: float) -> dict:
    """Drops any slice whose start is older than the trailing window.
    Keys are stored as strings (JSON object keys can't be numbers) --
    cast back to float to compare."""
    cutoff = now - _WINDOW_SECONDS
    return {k: v for k, v in slices.items() if float(k) >= cutoff}


def record_headroom(provider: str, key_id: str, model: str,
                     remaining_tokens: "int | None",
                     remaining_requests: "int | None",
                     reset_seconds: "float | None") -> None:
    """Called after every response (success or error) that carries
    rate-limit headers, per Phase 3's wiring. Stores an absolute
    `reset_at` timestamp (now + reset_seconds) rather than the raw
    relative reset_seconds -- a relative number goes stale the instant
    it's written, an absolute one stays correct however long it sits in
    the bus before the next read.
    """
    try:
        now = _now()
        reset_at = (now + reset_seconds) if reset_seconds is not None else None
        record = {
            "remaining_tokens": remaining_tokens,
            "remaining_requests": remaining_requests,
            "reset_at": reset_at,
            "recorded_at": now,
        }
        ttl = int(reset_seconds) + 30 if reset_seconds else _HEADROOM_DEFAULT_TTL
        bus_write(_headroom_key(provider, key_id, model), record, ex=max(ttl, 5))
    except Exception as write_exc:
        print(f"  [rate_ledger] record_headroom write failed (non-fatal): {write_exc}")


def record_usage(provider: str, key_id: str, model: str, tokens_used: int) -> None:
    """Self-tracked fallback -- called after every call whose actual
    token usage is known from the response body (log_usage() in
    llm_client.py already computes this; Phase 3 just also feeds it
    here). Read-modify-write of the sliding window's slice dict, pruning
    anything that's aged out of the trailing window on every write so
    the record never grows unbounded.
    """
    try:
        now = _now()
        slice_start = str(int(now // _SLICE_SECONDS) * _SLICE_SECONDS)
        key = _window_key(provider, key_id, model)
        record = bus_read(key, default=None) or {"slices": {}}
        slices = _prune_window(record.get("slices", {}), now)
        slices[slice_start] = slices.get(slice_start, 0) + tokens_used
        bus_write(key, {"slices": slices}, ex=_WINDOW_TTL)
    except Exception as write_exc:
        print(f"  [rate_ledger] record_usage write failed (non-fatal): {write_exc}")


def can_proceed(provider: str, key_id: str, model: str, estimated_tokens: int) -> "tuple[bool, float]":
    """Returns (ok, suggested_wait_seconds). ok=True means send now.
    ok=False means the estimated request would exceed remaining headroom
    in the current window; wait_seconds is how long until enough headroom
    is expected to free up.

    Prefers provider-reported headroom (rule 1) when a record exists and
    isn't obviously stale; falls back to the self-tracked sliding window
    (rule 2) otherwise -- including the common case where no provider
    headroom has ever been recorded yet for this (provider, key_id,
    model), e.g. the very first call of a process's lifetime.

    Fails open (True, 0.0) on any lookup error or when neither signal is
    available (no headroom record AND no verified QUOTA_CONFIG tpm figure
    to gate the sliding window against) -- see the module docstring for
    why failing open is the correct default here.
    """
    try:
        now = _now()
        headroom = bus_read(_headroom_key(provider, key_id, model), default=None)
        if headroom is not None and headroom.get("remaining_tokens") is not None:
            remaining = headroom["remaining_tokens"]
            if estimated_tokens <= remaining:
                return True, 0.0
            reset_at = headroom.get("reset_at")
            wait = max(0.0, reset_at - now) if reset_at is not None else 5.0
            return False, wait

        # Fallback: self-tracked sliding window, gated against
        # QUOTA_CONFIG's verified tpm figure. No verified tpm number ->
        # nothing to gate against -> fail open.
        tpm_limit = _tpm_limit_for(provider, model)
        if tpm_limit is None:
            return True, 0.0

        window_record = bus_read(_window_key(provider, key_id, model), default=None) or {"slices": {}}
        slices = _prune_window(window_record.get("slices", {}), now)
        current_usage = sum(slices.values())
        if current_usage + estimated_tokens <= tpm_limit:
            return True, 0.0

        # Not enough headroom right now -- wait until the oldest slice in
        # the window ages out, which is the earliest point usage can
        # possibly drop. Slight positive floor so callers never busy-loop
        # on a wait_seconds of 0 from this branch.
        if slices:
            oldest_slice_start = min(float(k) for k in slices.keys())
            wait = max(0.5, (oldest_slice_start + _WINDOW_SECONDS) - now)
        else:
            wait = _WINDOW_SECONDS
        return False, wait
    except Exception as read_exc:
        print(f"  [rate_ledger] can_proceed read failed, failing open (non-fatal): {read_exc}")
        return True, 0.0


def headroom_snapshot(provider: str, key_id: str, model: str) -> dict:
    """For Phase 5 (chain ordering) and Phase 8 (dashboard) to read
    current state without mutating it. Returns both signals side by side
    so a caller can see which one is actually driving can_proceed()'s
    decision:

    {
      "provider_reported": {"remaining_tokens": int|None,
                             "remaining_requests": int|None,
                             "reset_at": float|None,
                             "recorded_at": float|None} | None,
      "sliding_window": {"used_last_60s": int, "tpm_limit": int|None,
                          "pct_used": float|None},
    }

    `pct_used` is None when tpm_limit is None (no verified QUOTA_CONFIG
    figure for this provider/model) -- same "honest missing number rather
    than a fabricated percentage" posture eo/quota_sentinel.py's
    get_quota_snapshot() already takes for unit_mismatch/pct.
    """
    try:
        now = _now()
        headroom = bus_read(_headroom_key(provider, key_id, model), default=None)

        tpm_limit = _tpm_limit_for(provider, model)
        window_record = bus_read(_window_key(provider, key_id, model), default=None) or {"slices": {}}
        slices = _prune_window(window_record.get("slices", {}), now)
        used_last_60s = sum(slices.values())

        return {
            "provider_reported": headroom,
            "sliding_window": {
                "used_last_60s": used_last_60s,
                "tpm_limit": tpm_limit,
                "pct_used": (used_last_60s / tpm_limit) if tpm_limit else None,
            },
        }
    except Exception as read_exc:
        print(f"  [rate_ledger] headroom_snapshot read failed (non-fatal): {read_exc}")
        return {
            "provider_reported": None,
            "sliding_window": {"used_last_60s": 0, "tpm_limit": None, "pct_used": None},
        }
