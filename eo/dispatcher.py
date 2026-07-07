"""
eo/dispatcher.py — deterministic routing engine.

Migration Part 12 §3.2: this REPLACES next_step() from Parts 2/8/9/11
entirely. This is the final version.

Why the rewrite: Part 2's next_step() matched a "next" destination
string against `agent_names` (resolved module names) to find where to
jump. That broke the moment `generic_worker` started running for many
different roles in the same plan (Part 10 §2's REAL_ACTION_ROLES split)
— with coding now unified into the same pipeline (Part 12 §0), that
includes early coding stages too (idea_planner/prompt_writer aren't
real-action roles, so they resolve to "generic_worker" same as
brainstormer/writer). A "NEXT: prompt_writer" tag had no way to
disambiguate which generic_worker SLOT in agent_names it meant, since
multiple slots are all literally the string "generic_worker".

The fix: index everything by role_names (the ordered list of ROLE names
for this run — NOT resolved module names), and only resolve to a module
name at the moment eo/executor.py actually calls the function. This
module now returns an INDEX into role_plan, not a destination string —
the caller (executor.py) resolves agent_names[next_idx] separately to
know which function to call.

Also renamed per the field this now reads: agents emit "next_destination"
(a role name), not the old "next" (a resolved module/destination name).
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, read
from relay.emitter import emit_event

# Migration Part 11 §1: nothing previously stopped a stage from being
# named as "next" over and over (reviewer -> sandbox_tester -> fixer ->
# reviewer -> sandbox_tester -> ... forever). Cap revisits per role, per
# session. Unchanged in Part 12 — still 3, still per (session_id, role).
MAX_STAGE_REVISITS = 3


def _visit_count(session_id: str, name: str) -> int:
    if not session_id:
        return 0
    return read(f"visit_counts:{session_id}", default={}).get(name, 0)


def _record_visit(session_id: str, name: str) -> None:
    if not session_id:
        return
    counts = read(f"visit_counts:{session_id}", default={})
    counts[name] = counts.get(name, 0) + 1
    write(f"visit_counts:{session_id}", counts)


def next_step(agent_result: dict, role_plan: list, idx: int, session_id: str = None,
              known_roles: set = None) -> tuple:
    """
    role_plan: the ordered list of ROLE names for this run (role_names)
        — NOT resolved module names. This is what lets a
        "next_destination: <role>" value disambiguate correctly even
        when several roles share generic_worker as their module.
        May be mutated in place (appended to) on escalation to a
        genuinely new role — callers must grow agent_names in lockstep
        (see eo/executor.py) or the next iteration will index past its
        end.
    idx: position in role_plan that just finished.
    known_roles: set of role names that have an actual staffed brief
        (already-briefed roles, e.g. from eo/registry.py's
        list_known_roles() plus the current role_plan). Used to reject
        hallucinated "next_destination" values that were never staffed
        and have no brief -- a role escalating to a name outside this
        set gets rejected rather than run brief-less. If None, no
        rejection is applied (back-compat for callers that don't pass
        it yet).

    Returns (next_idx_or_None, reason). Caller resolves
        agent_names[next_idx] separately to know which function to call.
    """
    named = agent_result.get("next_destination") if isinstance(agent_result, dict) else None

    if not named:
        nxt = idx + 1
        target_idx = nxt if nxt < len(role_plan) else None
        _log_route(session_id, role_plan[nxt] if target_idx is not None else None, "plan")
        return target_idx, "plan"

    if named not in role_plan:
        # A genuinely new role, not in the original plan at all --
        # escalate by appending it on the fly rather than dropping it.
        # BUT only if it's in the system's known-roles vocabulary
        # (already-briefed roles) -- otherwise the model just made this
        # name up on the spot, it was never passed through
        # staff_task() -> _get_or_write_role_prompt() -> add_role_prompt(),
        # and running it would produce a brief-less, dead-end step.
        if known_roles is not None and named not in known_roles:
            emit_event("hallucinated_role_rejected", session_id=session_id, agent="dispatcher",
                       payload={"attempted_role": named})
            nxt = idx + 1
            target_idx = nxt if nxt < len(role_plan) else None
            _log_route(session_id, role_plan[nxt] if target_idx is not None else None, "plan")
            return target_idx, "plan"
        role_plan.append(named)
        target_idx = len(role_plan) - 1
        reason = "escalate"
    elif named in role_plan[:idx + 1]:
        # Found earlier in (or at) the plan -- a recheck/revisit.
        target_idx = max(i for i, r in enumerate(role_plan[:idx + 1]) if r == named)
        reason = "recheck"
    else:
        target_idx = role_plan.index(named, idx + 1)
        reason = "escalate"

    if _visit_count(session_id, named) >= MAX_STAGE_REVISITS:
        emit_event("revisit_cap_reached", session_id=session_id, agent="dispatcher",
                   payload={"stage": named, "cap": MAX_STAGE_REVISITS})
        nxt = idx + 1
        target_idx = nxt if nxt < len(role_plan) else None
        _log_route(session_id, role_plan[nxt] if target_idx is not None else None, "plan")
        return target_idx, "plan"

    _record_visit(session_id, named)
    _log_route(session_id, named, reason)
    return target_idx, reason


def _log_route(session_id: str, destination: str, reason: str):
    """Appends to the route_trace:{session_id} key (blueprint §6.4) so the
    frontend's Routing Trace card can show why a run deviated from plan.
    `destination` is now a ROLE name, not a resolved module name — same
    key, same shape, just the vocabulary next_step() itself now speaks
    in (Part 12 §3.1)."""
    if not session_id or destination is None:
        return
    key = f"route_trace:{session_id}"
    trace = read(key, default=[])
    trace.append({"destination": destination, "reason": reason})
    write(key, trace)
    emit_event("dispatch_event", session_id, agent="dispatcher",
               payload={"destination": destination, "reason": reason})