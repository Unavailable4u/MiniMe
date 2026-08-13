"""
eo/mech_repair.py — G3d (Master Guide, "G3/G4. Hierarchical parallel
build + validate", "Local repair on failure, capped"): the shared
generate->validate->repair driver every tree level (Level 0->1 already
built by G3a/G3b/G3c, Level 1->2 / 2->3 / 3->4 landing with G3e/G3f/G3g)
plugs into, instead of each level re-implementing its own retry/cap/flag
bookkeeping.

What this module does NOT do: it never proposes geometry itself. This
module's whole job is orchestration -- call eo/mech_validator.py's
validate_layout(), and when it reports violations, hand each violating
node to a caller-supplied `regenerate_node_fn` (the actual "generate"
half -- for Level 0->1 that's an LLM call in the shape agents/
mech_primitive_pool.py's _generate_primitives_for_part() already makes,
just re-scoped to one already-placed part instead of a batch of
uncovered ones and now fed the violation as context; for Level 1->2/2->3/
3->4 it'll be whatever G3e/F/G's own placement-generation call looks
like), then re-validate ONLY the nodes it just regenerated. Per the
Master Guide: "A violation at any level only re-generates that specific
node ... never the whole tree."

Why re-validate only the regenerated nodes, not the whole level again:
eo/mech_validator.py's persistent-sandbox-session design (G3c) already
makes a second batched call cheap once the sandbox is warm, but a
sibling that already passed didn't change and doesn't need re-checking
-- re-validating everything on every retry round would also make a
single stubborn node's 2 retries cost 2 extra full-level FreeCAD passes
instead of 2 small ones, for zero benefit.

Retry cap, per the Master Guide verbatim: "Retry cap: 2 attempts per
node; after the cap, it ships with the violation flagged in the UI
rather than blocking, same fail-safe philosophy already used elsewhere
in this codebase (empty-but-valid, never a hard crash)." A node that
regenerates successfully partway through (attempt 1 fixes it) exits the
retry loop immediately -- the cap is a ceiling, not a quota every node
has to spend.

Failure modes this module treats as "flag, don't block" (never raises,
mirroring eo/mech_validator.py's own fail-open posture for
infrastructure problems):
  - `regenerate_node_fn` itself raises for a node -- that attempt is
    burned (still counts toward the cap) and the node is flagged once
    the cap is hit, exactly like a regeneration that ran fine but
    didn't actually fix the violation.
  - validate_layout() reports `validator_error` (FreeCAD/sandbox
    infrastructure unavailable) on a MID-REPAIR re-validation call --
    at that point this module can no longer tell whether the nodes it
    just regenerated are actually fixed or not, so it stops the repair
    loop and flags whatever was still outstanding at that moment rather
    than guessing either way.
  - validate_layout() reports `validator_error` on the very FIRST call
    (nothing was ever actually checked) -- no repair loop runs at all;
    this module returns immediately with `valid: True`, an empty
    `violations` list, and the same `validator_error` field passed
    through, matching validate_layout()'s own contract for that case
    exactly (this module adds nothing false on top of "couldn't check").

Node identification: Level 0->1's node_id is a `part_id` (matching
eo/mech_validator.py's own violation shape, `{"node_id", "issue"}`, and
`mech["placements"][i]["part_id"]`). This module's public entry point,
run_repair_loop(), is written generically against "whatever
mech_validator.validate_layout() calls node_id" and only gets
Level-0->1-specific in its one private helper that narrows `mech` down
to just the retried nodes for re-validation (_subset_for_nodes() below)
-- that helper is the one piece of this module that will need a sibling
case added once G3e/F/G define what a subsection/section "mech" subset
even looks like; the retry/cap/flag loop above it does not change.
"""

import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from relay.emitter import emit_event
from eo.mech_validator import validate_layout

# Master Guide: "Retry cap: 2 attempts per node."
DEFAULT_MAX_RETRIES = 2


def _subset_for_nodes(mech: dict, node_ids: set) -> dict:
    """Narrows `mech` down to just the placements the caller is about to
    re-validate, so a repair round only pays for what it just changed.
    Level 0->1 ONLY -- keyed off `mech["placements"]`/`part_id`, the one
    schema eo/mech_validator.py's validate_layout() currently
    understands (see its own G3c docstring). Extending this to
    subsections/sections is a lockstep change alongside whatever G3e/F/G
    define those levels' own `mech` shape to be -- this function is
    where that extra branch goes, not the loop in run_repair_loop()
    below.
    """
    placements = (mech or {}).get("placements") or []
    return {
        "placements": [
            p for p in placements
            if isinstance(p, dict) and p.get("part_id") in node_ids
        ],
    }


def run_repair_loop(mech: dict, level: str, regenerate_node_fn,
                     session_id: str = None, path: str = None, domain: str = None,
                     max_retries: int = DEFAULT_MAX_RETRIES) -> dict:
    """Validates `mech` at `level`, and for as long as violations remain
    and nodes still have retries left, regenerates just the violating
    nodes and re-validates just those, capped at `max_retries` attempts
    per node.

    `regenerate_node_fn(mech, node_id, violation, attempt)`: caller-
    supplied, mutates `mech` in place for exactly ONE node (Level 0->1:
    one placement's `primitives` list) using `violation["issue"]` as
    feedback. `attempt` is 1 on a node's first regeneration this call
    and 2 on its second (== `max_retries` at default settings) -- a
    caller that wants to phrase its regeneration prompt more forcefully
    on the last try can key off this. May raise; a raised exception is
    caught here and treated as "that attempt didn't fix it" (see module
    docstring's failure-modes section), never propagated.

    Returns:
        {"valid": bool,           # True iff nothing is left flagged
         "violations": [...],     # still-violating nodes after the cap, {"node_id","issue"}
         "attempts": {node_id: n},# regeneration attempts actually made per node
         "repaired": [node_id],   # nodes that violated at some point but passed after regeneration
         "validator_error": str}  # present only if validate_layout() itself couldn't run; see module docstring

    Never modifies `mech` itself beyond what `regenerate_node_fn` does --
    this function only reads violations and decides whether/who to ask
    to regenerate; all the actual geometry mutation is the caller's
    function's responsibility, same separation of concerns eo/
    mech_validator.py's own "never modifies the layout, only reports"
    contract already establishes for validation.
    """
    agent_name = "mech_repair"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Mech Repair — Level {level}"})
    started = time.monotonic()

    result = validate_layout(mech, level, session_id=session_id, path=path, domain=domain)
    if result.get("validator_error") is not None:
        # Nothing was actually checked -- no repair loop to run. Pass
        # the "couldn't validate" signal straight through rather than
        # inventing an empty attempts/repaired shape that implies a
        # repair pass happened.
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": f"validator unavailable: {result['validator_error']}",
                            "duration_ms": duration_ms})
        return {"valid": True, "violations": [], "attempts": {}, "repaired": [],
                "validator_error": result["validator_error"]}

    violations = result.get("violations") or []
    attempts = {}
    flagged_by_id = {}
    repaired_ids = set()

    while violations:
        retry_now = []  # [(node_id, violation)]
        for violation in violations:
            node_id = violation.get("node_id")
            count = attempts.get(node_id, 0)
            if count >= max_retries:
                flagged_by_id[node_id] = violation
                continue
            attempts[node_id] = count + 1
            retry_now.append((node_id, violation))

        if not retry_now:
            break  # everything remaining is already at/over its cap

        # A node whose regenerate_node_fn call raised didn't actually
        # change anything about that node -- re-validating it would just
        # reproduce the exact same violation FreeCAD already reported,
        # for no new information. Those nodes skip this round's
        # validate_layout() call entirely and go straight back into the
        # next round's `violations` (still counted toward the cap above,
        # exactly like a regeneration that ran but didn't help) with the
        # failure noted in their issue text. Only nodes that actually
        # got a real regeneration attempt are worth spending a
        # validate_layout() call on.
        regen_failed = {}  # node_id -> augmented violation
        regen_ok_ids = set()
        for node_id, violation in retry_now:
            try:
                regenerate_node_fn(mech, node_id, violation, attempts[node_id])
                regen_ok_ids.add(node_id)
            except Exception as exc:
                augmented = dict(violation)
                augmented["issue"] = f"{violation.get('issue')} (regeneration attempt failed: {exc})"
                regen_failed[node_id] = augmented

        next_violations = list(regen_failed.values())

        if regen_ok_ids:
            sub_result = validate_layout(
                _subset_for_nodes(mech, regen_ok_ids), level,
                session_id=session_id, path=path, domain=domain,
            )
            if sub_result.get("validator_error") is not None:
                # Can no longer tell which of this round's regenerated
                # nodes are fixed. Flag everything still outstanding
                # (both the regen_failed nodes above AND the ones we
                # couldn't re-check) and stop -- see module docstring's
                # failure-modes section on why "unknown" resolves to
                # flagged-not-blocked here, same as everywhere else.
                violation_by_id = dict(retry_now)
                for node_id in regen_ok_ids:
                    flagged_by_id[node_id] = violation_by_id[node_id]
                for node_id, violation in regen_failed.items():
                    flagged_by_id[node_id] = violation
                duration_ms = int((time.monotonic() - started) * 1000)
                emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                           payload={"summary": f"validator unavailable mid-repair: {sub_result['validator_error']}",
                                    "duration_ms": duration_ms})
                return {
                    "valid": False,
                    "violations": list(flagged_by_id.values()),
                    "attempts": attempts,
                    "repaired": sorted(repaired_ids),
                    "validator_error": sub_result["validator_error"],
                }

            still_bad_ids = {v.get("node_id") for v in (sub_result.get("violations") or [])}
            repaired_ids |= (regen_ok_ids - still_bad_ids)
            next_violations += sub_result.get("violations") or []

        violations = next_violations

    flagged = list(flagged_by_id.values())
    duration_ms = int((time.monotonic() - started) * 1000)
    summary = "no violations" if not flagged and not repaired_ids else (
        f"{len(repaired_ids)} repaired, {len(flagged)} flagged"
    )
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})

    return {
        "valid": not flagged,
        "violations": flagged,
        "attempts": attempts,
        "repaired": sorted(repaired_ids),
    }


if __name__ == "__main__":
    _demo_mech = {
        "placements": [
            {"part_id": "motor_1", "w": 28, "h": 19, "d": 19,
             "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 28, "h": 19, "d": 19},
                              "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}]},
        ],
    }

    def _demo_regenerate(mech, node_id, violation, attempt):
        print(f"    [demo] regenerating {node_id} (attempt {attempt}): {violation['issue']}")

    import json
    from eo.mech_validator import LEVEL_0_1, close_session
    try:
        print(json.dumps(run_repair_loop(_demo_mech, LEVEL_0_1, _demo_regenerate), indent=2))
    finally:
        close_session()
