"""
eo/tool_budget.py — Patch B6 (CLI-as-Internal-Interface plan, §3.4):
a small, reusable per-task tool-call counter.

Deliberately NOT chat-tab-specific in its own code — this module has no
idea what a "tab" is at all. The chat-tab scoping the plan asks for
(§3.4) lives entirely in the caller (eo/executor.py's _run_loop()),
which only consults over_threshold() when it already knows this run
came from the chat tab. Any other caller (a future CLI-originated task,
a template run, whatever) can reuse this exact counter with zero
changes here.

Storage: one memory.bus counter per session_id, namespaced the same way
every other per-session bus key in this codebase already is (see
eo/loop_controller.py's "prev_critical_issues:{session_id}" for the
established pattern this follows).
"""
import os

from memory.bus import incr, read, write

# Starting default — a named constant so it's easy to tune later from
# real chat-tab usage data (per §3.4), without hunting for a magic
# number buried in eo/executor.py's dispatch loop.
#
# CALIBRATION FOLLOW-UP (perf audit item #6): 40 was always a guess,
# never a measured number. Lowered to 15 as the new test-environment
# default so we actually see budget_exceeded pauses fire at a
# realistic chat-tab session length instead of only in pathological
# runs — 40 was high enough that almost nothing ever hit it, which
# means the "let the user choose to continue" pause/resume plumbing
# (already built end-to-end in eo/executor.py) was effectively never
# exercised in practice. Overridable via env var so this can be raised
# back up (or tuned further) per-environment without a code change —
# same convention conversation_memory.py's CONVERSATION_TTL/filter
# toggles already use.
DEFAULT_TOOL_CALL_BUDGET = int(os.getenv("DEFAULT_TOOL_CALL_BUDGET", "15"))

_STATS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — matches eo/sga.py's _SGA_STATS_TTL_SECONDS
_BUDGET_HIT_STATS_KEY = "tool_budget_stats:hits"
_BUDGET_INCREMENT_STATS_KEY = "tool_budget_stats:increments"


def increment(session_id: str) -> int:
    """Bumps this session's tool-call counter by one and returns the new
    total. Call once per tool/role dispatch a caller wants counted
    toward the budget — see eo/executor.py's _run_loop() for the one
    real call site today (once per completed role-step)."""
    count = read(f"tool_call_budget:{session_id}", default=0) + 1
    write(f"tool_call_budget:{session_id}", count)
    # Perf audit item #6 follow-up: a global "how many role-steps have
    # we counted, total, across every session" counter. On its own
    # this isn't very informative, but paired with _BUDGET_HIT_STATS_KEY
    # below it gives a rough "hit rate" (what fraction of counted steps
    # actually land on a paused run) without needing a per-session
    # breakdown or a new storage shape. Best-effort — a missed
    # increment here must never block the real counter above from
    # advancing.
    try:
        incr(_BUDGET_INCREMENT_STATS_KEY, ex=_STATS_TTL_SECONDS)
    except Exception as exc:
        print(f"  [Tool Budget] increment stat failed (non-fatal): {exc}")
    return count


def over_threshold(session_id: str, threshold: int = DEFAULT_TOOL_CALL_BUDGET) -> bool:
    """Reads (never increments) this session's current count and
    compares it against `threshold`. Split from increment() on purpose:
    a caller checks this immediately after incrementing, at whatever
    checkpoint it wants the budget enforced -- the counter itself
    doesn't know or care when/whether it's being enforced.

    NEW — perf audit item #6: records a hit every time this actually
    resolves True, i.e. every time a caller is *about* to pause a run
    on the budget (eo/executor.py only reaches "reason =
    budget_exceeded" when this returns True). This is the number the
    calibration pass at DEFAULT_TOOL_CALL_BUDGET's docstring above
    exists to watch — see get_tool_budget_stats()."""
    exceeded = read(f"tool_call_budget:{session_id}", default=0) >= threshold
    if exceeded:
        try:
            incr(_BUDGET_HIT_STATS_KEY, ex=_STATS_TTL_SECONDS)
        except Exception as exc:
            print(f"  [Tool Budget] hit stat failed (non-fatal): {exc}")
    return exceeded


def get_tool_budget_stats() -> dict:
    """Returns {"increments", "hits", "hit_rate", "current_default"} —
    the numbers to pull before raising or lowering
    DEFAULT_TOOL_CALL_BUDGET any further. "increments" is every
    completed role-step counted toward *any* session's budget;
    "hits" is how many of those checks actually crossed the
    threshold and triggered (or would have triggered, for a
    non-chat-tab run — see eo/executor.py's budget_exceeded gating)
    a pause. hit_rate is None (not 0.0) with no data yet, same
    "don't confuse cold with zero" convention
    eo/sga.py's get_sga_stats() and this module's sibling
    get_conversation_memory_store_stats() already use.

    A hit_rate near 0 after real chat-tab usage means the budget is
    still comfortably above what real sessions need — safe to leave,
    or worth raising if the pause is felt as an interruption more
    than a safety net. A hit_rate that's uncomfortably high (most
    sessions running into it) means 15 is now too low and should come
    back up — this is exactly the manual read-the-numbers step,
    not something this module should auto-tune."""
    increments = read(_BUDGET_INCREMENT_STATS_KEY, default=0) or 0
    hits = read(_BUDGET_HIT_STATS_KEY, default=0) or 0
    return {
        "increments": increments,
        "hits": hits,
        "hit_rate": (hits / increments) if increments else None,
        "current_default": DEFAULT_TOOL_CALL_BUDGET,
    }


def reset(session_id: str) -> None:
    """Zeroes this session's counter. Not called anywhere in this patch
    (a fresh session_id already starts at an implicit 0 via read()'s
    default), but exposed for a future caller that wants to reuse the
    same session_id across logically separate budget windows rather
    than minting a new session_id each time."""
    write(f"tool_call_budget:{session_id}", 0)
