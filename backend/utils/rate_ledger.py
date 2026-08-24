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

OR-1d (reliability_overhaul_plan.md, OpenRouter's OR-1 header check --
see test_openrouter.py): OpenRouter never sends x-ratelimit-* headers on
any call (confirmed live, every model tried), so signal 1 above never
populates for it, and its documented limits (20 rpm; 50/day at $0
balance, up to 1000/day after a one-time >=$10 credit purchase) are a
REQUEST-COUNT ceiling, not a token ceiling -- a tpm-shaped sliding window
can't express "20 requests per minute" since tokens-per-request varies
call to call. Every function below now branches on _gating_mode_for():

- "tokens" (unchanged, existing behavior) -- QUOTA_CONFIG[provider][model]
  has a "tpm" figure; gates on summed tokens in the sliding window.
- "requests" (new) -- QUOTA_CONFIG[provider][model] has an "rpm" and/or
  "rpd" figure instead; gates on a summed REQUEST COUNT in the same
  60s sliding window for rpm, plus a separate UTC-calendar-day counter
  for rpd. Every call increments the window/day counters by exactly 1,
  regardless of how many tokens that call actually used.

Providers/models with neither figure in QUOTA_CONFIG default to "tokens"
mode and immediately fail open in it (tpm_limit is None), identical to
this module's behavior before OR-1d -- so this is additive, not a
behavior change for groq/cerebras/mistral/gemini/huggingface.
"""
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read as bus_read
from memory.bus import write as bus_write

# Sliding-window tuning. 5-second slices, 60-second trailing sum -- matches
# the plan's own numbers (§PHASE 2 "Algorithm", point 2). Reused as-is for
# "requests" mode's rpm gating (OR-1d) -- same slice/window shape, the
# slices just hold a request COUNT instead of a token COUNT in that mode.
_SLICE_SECONDS = 5
_WINDOW_SECONDS = 60
# Both bus records are short-lived by nature (a stale headroom reading or
# a >60s-old usage slice is useless for gating), so both are written with
# a bounded TTL rather than left to accumulate in Redis forever. Generous
# buffer over the window/reset itself so a slightly-delayed read still
# sees the record.
_HEADROOM_DEFAULT_TTL = 120
_WINDOW_TTL = _WINDOW_SECONDS + 60
# OR-1d: rpd (requests-per-day) counter TTL buffer -- the key itself is
# already scoped to one UTC calendar day (see _day_bucket_key()), this is
# just slack so a read right at midnight UTC doesn't race the key's own
# natural expiry and see a false "no count yet" for the day that just ended.
_DAY_TTL_BUFFER_SECONDS = 300


def _headroom_key(provider: str, key_id: str, model: str) -> str:
    return f"rate_ledger:{provider}:{key_id}:{model}:headroom"


def _window_key(provider: str, key_id: str, model: str) -> str:
    return f"rate_ledger:{provider}:{key_id}:{model}:window"


def _day_bucket_key(provider: str, key_id: str, model: str, now: float = None) -> str:
    """OR-1d: one key per (provider, key_id, model) per UTC calendar day,
    e.g. ...:day:2026-08-23. Deliberately a calendar-day bucket rather than
    a rolling 24h window -- simpler to reason about and matches how
    providers like OpenRouter actually describe their daily cap ("50/day",
    reset at a fixed point), not a trailing-24h sum."""
    ts = now if now is not None else _now()
    day = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return f"rate_ledger:{provider}:{key_id}:{model}:day:{day}"


def _seconds_until_next_utc_day(now: float = None) -> float:
    ts = now if now is not None else _now()
    current = datetime.fromtimestamp(ts, tz=UTC)
    tomorrow = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (tomorrow - current).total_seconds())


def _config_for(provider: str, model: str) -> dict:
    """Single lookup point for QUOTA_CONFIG[provider][model], imported
    lazily for the same circular-import reason _tpm_limit_for() already
    documented (Phase 3 wires this module into utils/llm_client.py, which
    is where QUOTA_CONFIG itself lives). Returns {} rather than raising
    when the provider/model has no entry at all."""
    from utils.llm_client import QUOTA_CONFIG
    return QUOTA_CONFIG.get(provider, {}).get(model, {})


def _gating_mode_for(provider: str, model: str) -> str:
    """OR-1d: returns "requests" when QUOTA_CONFIG[provider][model] has an
    "rpm" and/or "rpd" figure but NO "tpm" figure (OpenRouter's shape --
    request-count is the only ceiling that actually applies), else
    "tokens" (the correct default for an unconfigured provider/model too
    -- see module docstring).

    Bugfix (same day as OR-1d landed): the first cut of this function
    checked ONLY "does rpm or rpd exist", which misclassified every
    existing groq/cerebras/gemini entry as "requests" mode -- those
    entries already carry rpm/rpd figures alongside tpm (for
    reporting/dashboard purposes -- see eo/quota_sentinel.py), they just
    were never gated on before OR-1d. Checking "tpm" first and letting it
    win, regardless of what else is in the config, restores the pre-OR-1d
    tokens-mode gating for every provider that has a verified tpm number,
    and only routes to "requests" mode for the genuinely tpm-less case
    (OpenRouter) OR-1d was actually built for."""
    config = _config_for(provider, model)
    if "tpm" in config:
        return "tokens"
    if "rpm" in config or "rpd" in config:
        return "requests"
    return "tokens"


def _tpm_limit_for(provider: str, model: str):
    """Looks up QUOTA_CONFIG[provider][model]["tpm"] -- the fallback
    sliding window's own ceiling, "tokens" gating mode only. Returns None
    if this provider/model has no verified tpm figure (e.g. mistral only
    publishes rps in some entries) -- callers treat that as "can't gate on
    the sliding window, fail open" rather than fabricating a number.
    """
    return _config_for(provider, model).get("tpm")


def _rpm_limit_for(provider: str, model: str):
    """OR-1d: QUOTA_CONFIG[provider][model]["rpm"] -- "requests" gating
    mode's per-minute ceiling. None means no verified rpm figure -> that
    half of "requests" mode fails open (rpd, if set, still applies)."""
    return _config_for(provider, model).get("rpm")


def _rpd_limit_for(provider: str, model: str):
    """OR-1d: QUOTA_CONFIG[provider][model]["rpd"] -- "requests" gating
    mode's per-day ceiling. None means no verified rpd figure -> that half
    of "requests" mode fails open (rpm, if set, still applies)."""
    return _config_for(provider, model).get("rpd")


def _read_daily_count(provider: str, key_id: str, model: str, now: float = None) -> int:
    """OR-1d: best-effort read of today's (UTC) request count. Missing key
    (first call of the day, or a read failure) reads as 0 -- consistent
    with this module's existing fail-open posture; a false "0 used today"
    costs at most one over-the-limit request before the very next
    _increment_daily_count() call corrects it."""
    try:
        record = bus_read(_day_bucket_key(provider, key_id, model, now), default=None)
        return (record or {}).get("count", 0)
    except Exception as read_exc:
        print(f"  [rate_ledger] _read_daily_count read failed, treating as 0 (non-fatal): {read_exc}")
        return 0


def _increment_daily_count(provider: str, key_id: str, model: str, now: float = None) -> None:
    """OR-1d: read-modify-write of today's (UTC) request count. Not
    atomic -- same best-effort posture as record_usage()'s read-modify-
    write of the sliding window elsewhere in this module; a lost
    increment under concurrent writers costs at most a slight undercount
    against the rpd ceiling, not a correctness failure worth adding
    cross-process locking for here."""
    try:
        ts = now if now is not None else _now()
        key = _day_bucket_key(provider, key_id, model, ts)
        record = bus_read(key, default=None) or {"count": 0}
        record["count"] = record.get("count", 0) + 1
        ttl = int(_seconds_until_next_utc_day(ts)) + _DAY_TTL_BUFFER_SECONDS
        bus_write(key, record, ex=max(ttl, 5))
    except Exception as write_exc:
        print(f"  [rate_ledger] _increment_daily_count write failed (non-fatal): {write_exc}")


def _now() -> float:
    return datetime.now(UTC).timestamp()


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

    OR-1d: in "requests" gating mode (see _gating_mode_for()), the same
    60s sliding window is reused but each call increments it by exactly
    1 -- a request-count ceiling like OpenRouter's 20/min doesn't care how
    many tokens that particular call happened to cost, so `tokens_used` is
    ignored for the window write in this mode (it's still a real, correct
    token count; there's just nothing rpm-shaped to gate it against). The
    rpd side of "requests" mode is a separate UTC-day counter, incremented
    here too via _increment_daily_count().
    """
    try:
        mode = _gating_mode_for(provider, model)
        now = _now()
        slice_start = str(int(now // _SLICE_SECONDS) * _SLICE_SECONDS)
        key = _window_key(provider, key_id, model)
        record = bus_read(key, default=None) or {"slices": {}}
        slices = _prune_window(record.get("slices", {}), now)
        increment = 1 if mode == "requests" else tokens_used
        slices[slice_start] = slices.get(slice_start, 0) + increment
        bus_write(key, {"slices": slices}, ex=_WINDOW_TTL)
        if mode == "requests":
            _increment_daily_count(provider, key_id, model, now)
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
    available (no headroom record AND no verified QUOTA_CONFIG figure to
    gate against) -- see the module docstring for why failing open is the
    correct default here.

    OR-1d: `estimated_tokens` is only meaningful in "tokens" gating mode
    (see _gating_mode_for()). In "requests" mode it's accepted but ignored
    -- a request-count ceiling like OpenRouter's costs exactly 1 "unit"
    per call regardless of size, so there's nothing to multiply/compare
    against a token estimate. Callers (llm_client.py's _ledger_gate())
    don't need to know which mode a given provider/model is in; they
    already compute an estimate for every call, it's just unused here
    when it doesn't apply.
    """
    try:
        now = _now()
        mode = _gating_mode_for(provider, model)
        headroom = bus_read(_headroom_key(provider, key_id, model), default=None)

        if mode == "requests":
            # Provider-reported remaining_requests (rule 1) -- OpenRouter
            # itself never sends this (no headers at all, confirmed by
            # OR-1's live check), but another future "requests" mode
            # provider might, so this branch isn't dead code, just
            # currently unexercised for openrouter specifically.
            if headroom is not None and headroom.get("remaining_requests") is not None:
                remaining = headroom["remaining_requests"]
                if remaining >= 1:
                    return True, 0.0
                reset_at = headroom.get("reset_at")
                wait = max(0.0, reset_at - now) if reset_at is not None else 5.0
                return False, wait

            rpm_limit = _rpm_limit_for(provider, model)
            rpd_limit = _rpd_limit_for(provider, model)
            if rpm_limit is None and rpd_limit is None:
                return True, 0.0

            # Daily cap first: it's the harder, less-forgiving ceiling
            # (resets once every 24h vs. every 60s for rpm), so there's no
            # point telling a caller "sure, send it" on the rpm check only
            # to have them slam into a day-exhausted 429 anyway.
            if rpd_limit is not None:
                daily_count = _read_daily_count(provider, key_id, model, now)
                if daily_count >= rpd_limit:
                    return False, _seconds_until_next_utc_day(now)

            if rpm_limit is not None:
                window_record = bus_read(_window_key(provider, key_id, model), default=None) or {"slices": {}}
                slices = _prune_window(window_record.get("slices", {}), now)
                current_requests = sum(slices.values())
                if current_requests + 1 <= rpm_limit:
                    return True, 0.0
                if slices:
                    oldest_slice_start = min(float(k) for k in slices.keys())
                    wait = max(0.5, (oldest_slice_start + _WINDOW_SECONDS) - now)
                else:
                    wait = _WINDOW_SECONDS
                return False, wait

            # rpm_limit is None but rpd_limit was set and had headroom
            # (didn't return False above) -- nothing left to check.
            return True, 0.0

        # mode == "tokens" -- unchanged from pre-OR-1d behavior.
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


# ---------------------------------------------------------------------------
# Phase 4 -- reservation primitive (reliability_overhaul_plan.md §PHASE 4).
#
# The race can_proceed()/record_usage() actually leaves open: can_proceed()
# only READS, and the corresponding booking (record_usage()) doesn't happen
# until the LLM call that used the headroom has already finished -- seconds,
# sometimes tens of seconds, later. Two callers racing through can_proceed()
# in that gap both see the same "headroom free" answer and both dispatch,
# because neither one's read ever caused the other's read to change.
#
# reserve()/release_reservation() fold the check and the provisional booking
# into a single function call: reserve() reads current state and, if there's
# headroom, WRITES the provisional booking back before returning -- no LLM
# call happens between the read and the write anymore. That doesn't make the
# read-modify-write atomic (this module still has no cross-process lock, by
# design -- see the module docstring's "never raise"/fail-open philosophy),
# but it shrinks the window a second caller could race into from "the full
# duration of one LLM call" down to "one bus round trip", which is the
# concrete guarantee Patch A/B/C's acceptance test (Patch E) checks.
#
# release_reservation() corrects the provisional entry once the real
# outcome is known: the true token count for tokens-mode calls (requests
# mode already costs exactly the 1 unit it reserved -- nothing to true up),
# or a full rollback if the call never actually went out at all (rerouted
# to a different chain step by _decide_ledger_action(), or the gate chose
# not to dispatch it).
# ---------------------------------------------------------------------------

# A reservation has to outlive the real call it was made for -- which can
# run considerably longer than the 60s sliding window itself (retries,
# continuations, a slow provider). 15 minutes is a generous upper bound on
# "how long a single gated call should ever legitimately take before
# something else (a timeout elsewhere in the stack) has already killed it" --
# a reservation record surviving past that just means release_reservation()
# fails open on a missing key (see its docstring) instead of correcting a
# now-stale booking, same "costs at most one wasted attempt" trade the rest
# of this module already makes.
_RESERVATION_TTL_SECONDS = 900


def _reservation_key(reservation_id: str) -> str:
    return f"rate_ledger:reservation:{reservation_id}"


def _headroom_write_ttl(headroom: dict, now: float) -> int:
    """Best-effort TTL for re-writing a headroom record reserve()/
    release_reservation() just mutated in place. bus.read() doesn't
    surface a key's remaining Redis TTL, so this recomputes one from
    reset_at the same way record_headroom() originally derived it --
    rather than either omitting `ex` (which would make the record
    non-expiring, letting a stale reservation decrement linger forever
    past the real reset) or resetting to the full _HEADROOM_DEFAULT_TTL
    (which could incorrectly extend an about-to-expire record's life)."""
    reset_at = headroom.get("reset_at")
    if reset_at is not None:
        return max(5, int(reset_at - now) + 30)
    return _HEADROOM_DEFAULT_TTL


def _bump_window(provider: str, key_id: str, model: str, amount: int, now: float) -> str:
    """Shared read-modify-write of the self-tracked sliding window,
    factored out of record_usage() so reserve() can call the identical
    increment logic for its provisional booking. Returns the slice key
    that was incremented, so the caller can store it and correct that
    EXACT slice later via _adjust_window_slice() -- release_reservation()
    can run well after the 5s slice that was originally booked has
    rolled over, so "whatever the current slice is now" would be wrong."""
    key = _window_key(provider, key_id, model)
    record = bus_read(key, default=None) or {"slices": {}}
    slices = _prune_window(record.get("slices", {}), now)
    slice_start = str(int(now // _SLICE_SECONDS) * _SLICE_SECONDS)
    slices[slice_start] = slices.get(slice_start, 0) + amount
    bus_write(key, {"slices": slices}, ex=_WINDOW_TTL)
    return slice_start


def _adjust_window_slice(provider: str, key_id: str, model: str, slice_key: "str | None", delta: int) -> None:
    """Adds `delta` (positive or negative) to one already-recorded slice,
    floored at 0. Only touches that specific slice, never "the current
    slice" -- release_reservation() is a correction of something
    reserve() already wrote, not a new increment. A missing slice
    (pruned by TTL, or the whole window record expired between reserve()
    and release) is a silent no-op: fail-open, same as everywhere else in
    this module -- the window self-corrects on the next real
    record_usage()/reserve() regardless."""
    if not slice_key or not delta:
        return
    key = _window_key(provider, key_id, model)
    record = bus_read(key, default=None)
    if record is None:
        return
    slices = record.get("slices", {})
    if slice_key not in slices:
        return
    slices[slice_key] = max(0, slices[slice_key] + delta)
    bus_write(key, {"slices": slices}, ex=_WINDOW_TTL)


def _adjust_headroom(provider: str, key_id: str, model: str, field: str, delta: int, now: float) -> None:
    """Adds `delta` to headroom[field] (field is "remaining_tokens" or
    "remaining_requests"), floored at 0. Best-effort: by the time
    release_reservation() runs, the real response has usually already
    landed and record_headroom() has already overwritten this record
    with fresh provider-reported numbers -- correcting stale data at
    that point is a no-op in spirit (the fresh write already superseded
    it) and harmless in practice, not a new risk beyond what this module
    already accepts elsewhere."""
    if not delta:
        return
    key = _headroom_key(provider, key_id, model)
    headroom = bus_read(key, default=None)
    if headroom is None or headroom.get(field) is None:
        return
    headroom[field] = max(0, headroom[field] + delta)
    bus_write(key, headroom, ex=_headroom_write_ttl(headroom, now))


def _decrement_daily_count(day_key: str, now: float) -> None:
    """Full-rollback undo for the rpd counter. Decrements the EXACT day
    bucket key reserve() incremented (passed in verbatim from the
    reservation record, never recomputed from "now") so a reservation
    made just before UTC midnight and rolled back just after still
    corrects the day it actually belongs to, instead of decrementing
    whatever the new day's counter happens to be."""
    try:
        record = bus_read(day_key, default=None)
        if record is None:
            return
        record["count"] = max(0, record.get("count", 0) - 1)
        ttl = int(_seconds_until_next_utc_day(now)) + _DAY_TTL_BUFFER_SECONDS
        bus_write(day_key, record, ex=max(ttl, 5))
    except Exception as write_exc:
        print(f"  [rate_ledger] _decrement_daily_count write failed (non-fatal): {write_exc}")


def _reserve_tokens_mode(provider: str, key_id: str, model: str, estimated_units: int, now: float):
    """Tokens-mode body of reserve(). Same decision precedence as
    can_proceed()'s "tokens" branch (provider-reported headroom first,
    self-tracked window fallback) -- but where can_proceed() only reads,
    this writes the provisional booking back immediately, in the same
    call, before returning. Returns (ok, wait_seconds, booking), where
    `booking` is None on ok=False and otherwise a dict recording exactly
    what was mutated so release_reservation() can invert it precisely:

      {"headroom_field": "remaining_tokens" | None,
       "headroom_decrement": int | None,
       "window_slice_key": str | None,
       "window_increment": int | None,
       "day_key": None}   # tokens mode has no rpd concept

    A None value for any field means "nothing was written there, don't
    touch it on release" -- e.g. headroom_field is None whenever the
    decision fell through to the self-tracked window instead.
    """
    headroom = bus_read(_headroom_key(provider, key_id, model), default=None)
    if headroom is not None and headroom.get("remaining_tokens") is not None:
        remaining = headroom["remaining_tokens"]
        if estimated_units <= remaining:
            headroom["remaining_tokens"] = remaining - estimated_units
            bus_write(_headroom_key(provider, key_id, model), headroom,
                      ex=_headroom_write_ttl(headroom, now))
            # Also fold the estimate into the self-tracked window, even
            # though the DECISION here was driven by provider-reported
            # headroom -- can_proceed()'s window fallback and
            # headroom_snapshot()'s dashboard reporting both need this to
            # stay accurate for whenever provider headroom next goes
            # stale/absent, not just while it's fresh.
            slice_key = _bump_window(provider, key_id, model, estimated_units, now)
            return True, 0.0, {
                "headroom_field": "remaining_tokens",
                "headroom_decrement": estimated_units,
                "window_slice_key": slice_key,
                "window_increment": estimated_units,
                "day_key": None,
            }
        reset_at = headroom.get("reset_at")
        wait = max(0.0, reset_at - now) if reset_at is not None else 5.0
        return False, wait, None

    tpm_limit = _tpm_limit_for(provider, model)
    if tpm_limit is None:
        # No verified figure to gate against -- fail open (unchanged from
        # can_proceed()'s behavior), but still book the estimate into the
        # window for dashboard/snapshot accuracy; there's just no ceiling
        # to check it against here.
        slice_key = _bump_window(provider, key_id, model, estimated_units, now)
        return True, 0.0, {
            "headroom_field": None, "headroom_decrement": None,
            "window_slice_key": slice_key, "window_increment": estimated_units,
            "day_key": None,
        }

    window_record = bus_read(_window_key(provider, key_id, model), default=None) or {"slices": {}}
    slices = _prune_window(window_record.get("slices", {}), now)
    current_usage = sum(slices.values())
    if current_usage + estimated_units <= tpm_limit:
        slice_key = _bump_window(provider, key_id, model, estimated_units, now)
        return True, 0.0, {
            "headroom_field": None, "headroom_decrement": None,
            "window_slice_key": slice_key, "window_increment": estimated_units,
            "day_key": None,
        }

    if slices:
        oldest_slice_start = min(float(k) for k in slices.keys())
        wait = max(0.5, (oldest_slice_start + _WINDOW_SECONDS) - now)
    else:
        wait = _WINDOW_SECONDS
    return False, wait, None


def _reserve_requests_mode(provider: str, key_id: str, model: str, now: float):
    """Requests-mode body of reserve() -- OR-1d's rpm/rpd gating, same
    precedence as can_proceed()'s "requests" branch, immediately booked
    (provider-headroom decrement, or window+day increment) instead of
    only read. See _reserve_tokens_mode() for the booking dict shape;
    requests mode never sets window_increment to anything but 1 (or
    None, when nothing was booked because rpm has no verified limit),
    since a request-count ceiling costs exactly 1 unit per call
    regardless of size -- there's no `estimated_units` magnitude to book
    here the way tokens mode has."""
    headroom_key = _headroom_key(provider, key_id, model)
    headroom = bus_read(headroom_key, default=None)
    if headroom is not None and headroom.get("remaining_requests") is not None:
        remaining = headroom["remaining_requests"]
        if remaining >= 1:
            headroom["remaining_requests"] = remaining - 1
            bus_write(headroom_key, headroom, ex=_headroom_write_ttl(headroom, now))
            return True, 0.0, {
                "headroom_field": "remaining_requests", "headroom_decrement": 1,
                "window_slice_key": None, "window_increment": None, "day_key": None,
            }
        reset_at = headroom.get("reset_at")
        wait = max(0.0, reset_at - now) if reset_at is not None else 5.0
        return False, wait, None

    rpm_limit = _rpm_limit_for(provider, model)
    rpd_limit = _rpd_limit_for(provider, model)
    if rpm_limit is None and rpd_limit is None:
        return True, 0.0, {
            "headroom_field": None, "headroom_decrement": None,
            "window_slice_key": None, "window_increment": None, "day_key": None,
        }

    if rpd_limit is not None:
        daily_count = _read_daily_count(provider, key_id, model, now)
        if daily_count >= rpd_limit:
            return False, _seconds_until_next_utc_day(now), None

    if rpm_limit is not None:
        window_record = bus_read(_window_key(provider, key_id, model), default=None) or {"slices": {}}
        slices = _prune_window(window_record.get("slices", {}), now)
        current_requests = sum(slices.values())
        if current_requests + 1 > rpm_limit:
            if slices:
                oldest_slice_start = min(float(k) for k in slices.keys())
                wait = max(0.5, (oldest_slice_start + _WINDOW_SECONDS) - now)
            else:
                wait = _WINDOW_SECONDS
            return False, wait, None

    # Every check that applied has headroom -- book it now.
    slice_key = None
    if rpm_limit is not None:
        slice_key = _bump_window(provider, key_id, model, 1, now)
    day_key = None
    if rpd_limit is not None:
        _increment_daily_count(provider, key_id, model, now)
        day_key = _day_bucket_key(provider, key_id, model, now)
    return True, 0.0, {
        "headroom_field": None, "headroom_decrement": None,
        "window_slice_key": slice_key,
        "window_increment": (1 if slice_key is not None else None),
        "day_key": day_key,
    }


def reserve(provider: str, key_id: str, model: str, estimated_units: int) -> "tuple[bool, float, str | None]":
    """Atomic-enough check-and-book, replacing a bare can_proceed() call
    at dispatch time wherever a caller needs the race closed (Patch B's
    concurrency gate; llm_client.py's _ledger_gate(), same seam). Returns
    (ok, wait_seconds, reservation_id):

      ok=True   -- headroom was confirmed AND the provisional booking has
                   already been written; reservation_id is a real id the
                   caller MUST eventually pass to release_reservation()
                   (with the real token count once known, or with no
                   actual_units at all if the call ends up not going out)
                   so the provisional entry gets corrected rather than
                   permanently overcounting.
      ok=False  -- no headroom; wait_seconds as in can_proceed().
                   reservation_id is None -- nothing was booked, nothing
                   to release.

    `estimated_units` is a token estimate in "tokens" gating mode and
    ignored (every call costs exactly 1 unit) in "requests" mode --
    identical convention to can_proceed()'s `estimated_tokens` param; see
    _gating_mode_for()'s docstring.

    Fails open (True, 0.0, None) on any error, matching can_proceed()'s
    own fail-open contract. A reservation_id of None is always safe to
    pass straight through to release_reservation() -- it's defined as a
    no-op in that case, so callers don't need to special-case a failed-
    open reserve() differently from a real one.
    """
    try:
        now = _now()
        mode = _gating_mode_for(provider, model)
        if mode == "requests":
            ok, wait, booking = _reserve_requests_mode(provider, key_id, model, now)
        else:
            ok, wait, booking = _reserve_tokens_mode(provider, key_id, model, estimated_units, now)

        if not ok:
            return False, wait, None

        reservation_id = uuid.uuid4().hex
        booking.update({
            "provider": provider,
            "key_id": key_id,
            "model": model,
            "mode": mode,
            "estimated_units": estimated_units,
            "created_at": now,
            "released": False,
        })
        bus_write(_reservation_key(reservation_id), booking, ex=_RESERVATION_TTL_SECONDS)
        return True, 0.0, reservation_id
    except Exception as write_exc:
        print(f"  [rate_ledger] reserve failed, failing open (non-fatal): {write_exc}")
        return True, 0.0, None


def release_reservation(reservation_id: "str | None", actual_units: "int | None" = None) -> None:
    """Corrects a reserve() booking once the real outcome is known.
    Replaces the plain record_usage() call at the same call site (see
    llm_client.py's _record_ledger_bookkeeping()) wherever the dispatch
    went through reserve() rather than a bare can_proceed(), so a
    reserved-then-completed call doesn't get double-booked (estimate at
    reserve() time, actual again at the old post-call record_usage()).

    - `actual_units` given: adjusts the provisional booking from its
      original estimate to the real count. Only meaningful in "tokens"
      mode (a "requests" mode reservation already cost exactly the 1 real
      unit it reserved -- see _reserve_requests_mode() -- so
      actual_units is accepted but ignored for those).
    - `actual_units` is None: full rollback. The call never actually went
      out -- rerouted to a different chain step by
      _decide_ledger_action()'s "reroute" branch, or the gate chose not
      to dispatch it at all -- so the entire provisional booking (window
      slice, headroom decrement, day counter) is undone.

    reservation_id may be None (reserve() itself failed open and never
    created one) -- a no-op, since nothing was booked in the first
    place. Also a no-op if the reservation has already expired (TTL) or
    already been released once (idempotency guard against a caller
    releasing twice, e.g. once in a `finally` and once on an explicit
    success path).

    Never raises -- same "ledger bookkeeping must never take down the
    real call" contract as every write in this module.
    """
    if reservation_id is None:
        return
    try:
        key = _reservation_key(reservation_id)
        booking = bus_read(key, default=None)
        if booking is None or booking.get("released"):
            return

        provider = booking["provider"]
        key_id = booking["key_id"]
        model = booking["model"]
        mode = booking["mode"]
        estimated_units = booking["estimated_units"]
        now = _now()

        if actual_units is None:
            # Full rollback -- undo the provisional booking entirely.
            if booking.get("window_slice_key") and booking.get("window_increment"):
                _adjust_window_slice(provider, key_id, model,
                                      booking["window_slice_key"], -booking["window_increment"])
            if booking.get("headroom_field") and booking.get("headroom_decrement"):
                _adjust_headroom(provider, key_id, model,
                                  booking["headroom_field"], booking["headroom_decrement"], now)
            if booking.get("day_key"):
                _decrement_daily_count(booking["day_key"], now)
        elif mode == "tokens":
            delta = actual_units - estimated_units
            if delta != 0:
                _adjust_window_slice(provider, key_id, model, booking.get("window_slice_key"), delta)
                if booking.get("headroom_field"):
                    _adjust_headroom(provider, key_id, model, booking["headroom_field"], -delta, now)
        # mode == "requests": actual_units, if given, is a no-op -- see
        # docstring above.

        booking["released"] = True
        bus_write(key, booking, ex=60)  # short tombstone, just enough to catch a racing double-release
    except Exception as release_exc:
        print(f"  [rate_ledger] release_reservation failed (non-fatal): {release_exc}")


def headroom_snapshot(provider: str, key_id: str, model: str) -> dict:
    """For Phase 5 (chain ordering) and Phase 8 (dashboard) to read
    current state without mutating it. Returns both signals side by side
    so a caller can see which one is actually driving can_proceed()'s
    decision:

    {
      "gating_mode": "tokens" | "requests",
      "provider_reported": {"remaining_tokens": int|None,
                             "remaining_requests": int|None,
                             "reset_at": float|None,
                             "recorded_at": float|None} | None,
      "sliding_window": {"used_last_60s": int, "tpm_limit": int|None,
                          "pct_used": float|None},
      "daily": {"used_today": int, "rpd_limit": int|None,
                "pct_used": float|None} | None,
    }

    `pct_used` is None when the relevant limit (tpm_limit / rpd_limit) is
    None (no verified QUOTA_CONFIG figure for this provider/model) -- same
    "honest missing number rather than a fabricated percentage" posture
    eo/quota_sentinel.py's get_quota_snapshot() already takes for
    unit_mismatch/pct.

    OR-1d: `sliding_window.used_last_60s` means "requests in the last 60s"
    rather than "tokens in the last 60s" when gating_mode == "requests" --
    same underlying window, different unit, per _gating_mode_for()'s
    docstring. `daily` is only populated (non-None) in "requests" mode;
    "tokens"-mode providers have no rpd concept so it stays None rather
    than reporting a meaningless 0/None pair.
    """
    try:
        now = _now()
        mode = _gating_mode_for(provider, model)
        headroom = bus_read(_headroom_key(provider, key_id, model), default=None)

        window_record = bus_read(_window_key(provider, key_id, model), default=None) or {"slices": {}}
        slices = _prune_window(window_record.get("slices", {}), now)
        used_last_60s = sum(slices.values())

        if mode == "requests":
            window_limit = _rpm_limit_for(provider, model)
            rpd_limit = _rpd_limit_for(provider, model)
            used_today = _read_daily_count(provider, key_id, model, now)
            daily = {
                "used_today": used_today,
                "rpd_limit": rpd_limit,
                "pct_used": (used_today / rpd_limit) if rpd_limit else None,
            }
        else:
            window_limit = _tpm_limit_for(provider, model)
            daily = None

        return {
            "gating_mode": mode,
            "provider_reported": headroom,
            "sliding_window": {
                "used_last_60s": used_last_60s,
                "tpm_limit": window_limit,
                "pct_used": (used_last_60s / window_limit) if window_limit else None,
            },
            "daily": daily,
        }
    except Exception as read_exc:
        print(f"  [rate_ledger] headroom_snapshot read failed (non-fatal): {read_exc}")
        return {
            "gating_mode": "tokens",
            "provider_reported": None,
            "sliding_window": {"used_last_60s": 0, "tpm_limit": None, "pct_used": None},
            "daily": None,
        }
