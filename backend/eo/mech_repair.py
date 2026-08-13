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
`mech["placements"][i]["part_id"]`). Level 1->2's node_id (G3e-4, this
patch) is a `subsection_id` -- also matching eo/mech_validator.py's own
LEVEL_1_2 violation shape, and (per eo/mech_subsections.py's
group_into_subsections()) always equal to its anchor part's own
`part_id`. This module's public entry point, run_repair_loop(), is
written generically against "whatever mech_validator.validate_layout()
calls node_id" and only gets level-specific in its one private helper
that narrows `mech` down to just the retried nodes for re-validation
(_subset_for_nodes() below) -- that's the one piece of this module that
needed a sibling case added for Level 1->2, and the same spot Level
2->3/3->4 (G3f/G3g) will each add their own case to; the retry/cap/flag
loop above it does not change.

run_level_1_2_repair() (G3e-4, bottom of this module) is the actual
Level 1->2 integration: builds the `regenerate_node_fn` closure over
agents/mech_subsection_pool.py's regenerate_subsection() (this level's
"generate" half), drives run_repair_loop() with it, and -- once the loop
settles -- persists every checkable subsection's validated aggregate
footprint back onto `mech["subsections"]` so G3f has something to group
by next. This is the level's own top-level driver, the Level-1->2
counterpart of whatever eventually wires Level 0->1's run_repair_loop()
call into agents/hardware_speccer.py's own pipeline -- kept in THIS
module rather than agents/mech_subsection_pool.py because it owns the
repair loop's lifecycle (including the final full-mech footprint
recompute below), not just one subsection's regeneration.
"""

import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from relay.emitter import emit_event
from eo.mech_validator import validate_layout, LEVEL_0_1, LEVEL_1_2

# Master Guide: "Retry cap: 2 attempts per node."
DEFAULT_MAX_RETRIES = 2


def _subset_for_nodes(mech: dict, node_ids: set, level: str = LEVEL_0_1) -> dict:
    """Narrows `mech` down to just the placements the caller is about to
    re-validate, so a repair round only pays for what it just changed.

    Level 0->1 (default, and the only case prior to G3e-4): keyed off
    `mech["placements"]`/`part_id` directly, the one schema eo/
    mech_validator.py's validate_layout() originally understood.

    Level 1->2 (G3e-4, this patch): `node_ids` are `subsection_id`s, not
    `part_id`s. eo/mech_validator.py's LEVEL_1_2 path re-derives its own
    subsection grouping from `mech["placements"]` (via
    eo/mech_subsections.py's group_into_subsections()) on every call, so
    the subset this function builds must include BOTH a kept
    subsection's anchor placement AND its "mount_"-prefixed sibling --
    dropping just the mount here would silently turn every requested
    subsection back into a (mount-less) singleton for validate_layout()'s
    own re-grouping, which would never re-check the collision that
    triggered the retry in the first place. A subsection genuinely
    without a mount sibling (shouldn't reach here at all -- see agents/
    mech_subsection_pool.py's regenerate_subsection() docstring on why
    LEVEL_1_2 only ever reports a collision violation for a two-member
    subsection) just contributes no extra id, harmlessly.

    Extending this to sections/device (G3f/G3g) is a lockstep change
    alongside whatever those levels define their own `mech` subset shape
    to be -- this function is where that next branch goes, not the loop
    in run_repair_loop() below.
    """
    placements = (mech or {}).get("placements") or []
    if level == LEVEL_1_2:
        keep_ids = set()
        for node_id in node_ids:
            keep_ids.add(node_id)
            keep_ids.add(f"mount_{node_id}")
        return {
            "placements": [
                p for p in placements
                if isinstance(p, dict) and p.get("part_id") in keep_ids
            ],
        }
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
                _subset_for_nodes(mech, regen_ok_ids, level=level), level,
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


def run_level_1_2_repair(spec: dict, parts: list, session_id: str = None, path: str = None,
                          domain: str = None, max_retries: int = DEFAULT_MAX_RETRIES,
                          key_override=None) -> dict:
    """G3e-4's Level 1->2 driver -- the "wire it all together" integration
    step eo/mech_subsections.py's group_into_subsections() (G3e-1),
    agents/mech_subsection_pool.py's regenerate_subsection() (this
    level's "generate" half) and eo/mech_validator.py's LEVEL_1_2 (G3e-3's
    "validate" half) were each built standalone for. Meant to run
    synchronously from agents.hardware_speccer.run_hardware_speccer(),
    the same in-process shape agents/mech_subsection_pool.py's own run()
    already documents for itself -- called AFTER that run() (G3e-2's own
    initial proposal pass) has already populated every in-scope mount's
    x/y/z once, so this function's own repair loop only ever regenerates
    a subsection that's ALREADY been proposed at least once.

    What this does, in order:
      1. run_repair_loop(mech, LEVEL_1_2, regenerate_node_fn, ...) --
         validates every subsection, and for each collision, calls
         agents/mech_subsection_pool.py's regenerate_subsection() with
         the violation fed back as context, capped at `max_retries` per
         subsection, exactly like every other level's repair loop (see
         this module's own top docstring).
      2. ONE final validate_layout(mech, LEVEL_1_2, ...) call over the
         FULL mech -- needed because run_repair_loop()'s own per-round
         validate_layout() calls are deliberately narrowed to just the
         regenerated nodes each round (see this module's docstring on
         why), so no single call inside the loop ever holds every
         subsection's footprint at once. eo/mech_validator.py's
         persistent-sandbox-session design (G3c) makes this second
         batched call cheap once step 1 has already warmed the session --
         same "one extra batched call, not one per subsection" reasoning
         the rest of this pipeline already leans on. Skipped if step 1
         already reports a `validator_error` -- the sandbox isn't usable
         right now, so a second call would just fail the same way.
      3. Writes every subsection's footprint from that final call onto
         `mech["subsections"]` (each entry's own new `footprint` key) --
         G3f's own input, per the Master Guide's Level 2->3 description
         ("check subsections within a section don't collide and get a
         section's aggregate footprint"). A subsection the final call
         couldn't compute a footprint for (its own members' primitives
         all failed to build -- see eo/mech_validator.py's
         _check_subsection() docstring) is left without a `footprint`
         key rather than one filled with a fabricated value.

    Returns run_repair_loop()'s own result dict from step 1, unmodified --
    the footprint-persisting step is a side effect on `spec`/`mech`, not
    a change to what this function reports about repair outcomes.

    Does NOT call eo/mech_validator.py's close_session() -- same "caller
    owns the session lifecycle for the whole run" contract that module's
    own docstring already establishes; whatever ultimately drives the
    full Level 0->1 through Level 3->4 run is responsible for closing it
    once, at the very end, not this one level's driver.
    """
    from eo.mech_subsections import apply_subsection_grouping
    from eo.mech_validator import LEVEL_1_2 as _LEVEL_1_2
    from agents.mech_subsection_pool import regenerate_subsection

    mech = spec.get("mech") or {}

    def regenerate_node_fn(mech_arg, node_id, violation, attempt):
        regenerate_subsection(
            mech_arg, node_id, violation, attempt,
            key_override=key_override, session_id=session_id, path=path, domain=domain,
        )

    result = run_repair_loop(
        mech, _LEVEL_1_2, regenerate_node_fn,
        session_id=session_id, path=path, domain=domain, max_retries=max_retries,
    )

    if result.get("validator_error") is None:
        final = validate_layout(mech, _LEVEL_1_2, session_id=session_id, path=path, domain=domain)
        footprints = final.get("footprints") or {}
        subsections = apply_subsection_grouping(mech)  # also stashes onto mech["subsections"]
        for subsection in subsections:
            footprint = footprints.get(subsection.get("subsection_id"))
            if footprint is not None:
                subsection["footprint"] = footprint

    return result


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
