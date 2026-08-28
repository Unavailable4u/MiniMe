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
from memory.bus import read, write

# Starting default — a named constant so it's easy to tune later from
# real chat-tab usage data (per §3.4), without hunting for a magic
# number buried in eo/executor.py's dispatch loop.
DEFAULT_TOOL_CALL_BUDGET = 40


def increment(session_id: str) -> int:
    """Bumps this session's tool-call counter by one and returns the new
    total. Call once per tool/role dispatch a caller wants counted
    toward the budget — see eo/executor.py's _run_loop() for the one
    real call site today (once per completed role-step)."""
    count = read(f"tool_call_budget:{session_id}", default=0) + 1
    write(f"tool_call_budget:{session_id}", count)
    return count


def over_threshold(session_id: str, threshold: int = DEFAULT_TOOL_CALL_BUDGET) -> bool:
    """Reads (never increments) this session's current count and
    compares it against `threshold`. Split from increment() on purpose:
    a caller checks this immediately after incrementing, at whatever
    checkpoint it wants the budget enforced -- the counter itself
    doesn't know or care when/whether it's being enforced."""
    return read(f"tool_call_budget:{session_id}", default=0) >= threshold


def reset(session_id: str) -> None:
    """Zeroes this session's counter. Not called anywhere in this patch
    (a fresh session_id already starts at an implicit 0 via read()'s
    default), but exposed for a future caller that wants to reuse the
    same session_id across logically separate budget windows rather
    than minting a new session_id each time."""
    write(f"tool_call_budget:{session_id}", 0)
