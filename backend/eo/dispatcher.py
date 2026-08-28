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

Patch B5a (CLI-as-Internal-Interface plan, Part 2 -- "wire eo agents to
eo/capabilities.py"): grep confirms this module never imported
eo/skill_library.py or eo/mcp_registry.py directly, before or after this
patch -- next_step() is pure role-plan indexing, it never had a real
call site to redirect. The one place this module DOES already reason
about "did the agent ask for something we don't recognize" is the
hallucinated-role rejection branch below, which is the closest thing
this file has to \u00a73.2's \"an agent facing an unfamiliar request\" --
so that's where this patch wires in a real (if today mostly empty)
eo/capabilities.py call: _is_known_capability() below. This is
observability only, added to hallucinated_role_rejected's payload -- it
never changes target_idx/reason, so routing behavior is unchanged.

Patch B5b (CLI-as-Internal-Interface plan, §3.3 "the fallback path" --
join point between B4 and B5a, needs both merged first): the same
hallucinated-role branch is also this system's one real instance of
"an agent facing an unfamiliar request" (§3.2/§3.3's own framing), so
it's where the plan's fallback ORDER gets wired in for real, not just
observed. _is_known_capability() above answered "is this name known at
all" with a single, unscoped eo/capabilities.py::list_capabilities()
call. This patch replaces that with _capability_fallback_check() below,
which tries the CURRENT role's own scoped view first
(eo/capabilities.py::capabilities_for_role(), Patch B3 -- "what am I,
specifically, allowed to know about") and only reaches for a live
eo/introspection.py::search_text() read (Patch B4, via
eo/capabilities.py's re-export) if that scoped view comes back empty.
Still observability-only in the same sense B5a's version was -- the
result only enriches hallucinated_role_rejected's payload, it never
changes target_idx/reason -- but the LOOKUP itself now does real,
two-step work instead of a single unscoped list_capabilities() call.
"""
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.capabilities import capabilities_for_role, search_text
from memory.bus import read, write
from relay.emitter import emit_event

# Migration Part 11 §1: nothing previously stopped a stage from being
# named as "next" over and over (reviewer -> sandbox_tester -> fixer ->
# reviewer -> sandbox_tester -> ... forever). Cap revisits per role, per
# session. Unchanged in Part 12 — still 3, still per (session_id, role).
MAX_STAGE_REVISITS = 3

# Patch B5b: search_text()'s root for the introspection fallback below.
# Scoped to the backend's own agent/routing code (where a real role or
# capability name would actually show up in source) rather than the
# whole repo -- keeps the fallback read narrow and fast, consistent
# with introspection.py's own framing of these functions as a targeted
# last resort, not a general-purpose grep.
_FALLBACK_SEARCH_ROOT = "eo"


def _capability_fallback_check(name: str, current_role: str | None) -> dict:
    """Patch B5b: the fallback-order lookup for a hallucinated
    `next_destination`. Two steps, tried in the order the architecture
    plan specifies (§3.3):

      1. capabilities_for_role(current_role) (Patch B3) -- the scoped,
         curated answer for "what is the role that just ran allowed to
         know about." If `name` matches an entry_id/title in that
         scoped list, we're done: known_capability=True,
         fallback_source="capability_layer", and search_text() below is
         never called.
      2. Only if step 1 comes back empty (no current_role, an unscoped
         role, or a scope with no match) do we fall back to
         search_text() (Patch B4) -- a live, read-only search of the
         backend's own source for `name`, the "broader read" §3.3 calls
         the fallback of last resort. A hit there still counts as
         known_capability=True (the name is real, just not something
         the capability layer has been told about yet, e.g. a role
         that exists in code but was never seeded into
         eo/capability_entries.py) -- fallback_source="introspection".

    Defensive throughout: neither the capability-layer lookup nor the
    introspection search may ever raise into the routing path -- a
    broken store or a filesystem hiccup here degrades to "unknown,"
    the same posture eo/introspection.py's own functions take toward a
    denied/missing path, never a crash in next_step().
    """
    try:
        scoped = capabilities_for_role(current_role) if current_role else []
    except Exception:
        scoped = []

    if any(entry.get("entry_id") == name or entry.get("title") == name
           for entry in scoped):
        return {"known_capability": True, "fallback_source": "capability_layer"}

    try:
        result = search_text(pattern=re.escape(name), root=_FALLBACK_SEARCH_ROOT)
        found = bool(result.get("matches"))
    except Exception:
        found = False

    return {"known_capability": found, "fallback_source": "introspection"}


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
            current_role = role_plan[idx] if 0 <= idx < len(role_plan) else None
            fallback = _capability_fallback_check(named, current_role)
            emit_event("hallucinated_role_rejected", session_id=session_id, agent="dispatcher",
                       payload={"attempted_role": named, **fallback})
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