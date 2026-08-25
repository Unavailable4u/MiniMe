"""
eo/mech_repair.py — G3d (Master Guide, "G3/G4. Hierarchical parallel
build + validate", "Local repair on failure, capped"): the shared
generate->validate->repair driver every tree level (Level 0->1 built by
G3a/G3b/G3c, Level 1->2 by G3e, Level 2->3 by G3f, Level 3->4 by G3g --
this patch, G3g's second half, which also closes out the tree) plugs
into, instead of each level re-implementing its own retry/cap/flag
bookkeeping.

What this module does NOT do: it never proposes geometry itself. This
module's whole job is orchestration -- call eo/mech_validator.py's
validate_layout(), and when it reports violations, hand each violating
node to a caller-supplied `regenerate_node_fn` (the actual "generate"
half -- for Level 0->1 that's an LLM call in the shape agents/
mech_primitive_pool.py's _generate_primitives_for_part() already makes,
just re-scoped to one already-placed part instead of a batch of
uncovered ones and now fed the violation as context; for Level 1->2/2->3
it's the equivalent per-level regenerate_* call in agents/
mech_subsection_pool.py / agents/mech_section_pool.py), then re-validate
ONLY the nodes it just regenerated. Per the Master Guide: "A violation at
any level only re-generates that specific node ... never the whole
tree." Level 3->4 (G3g, run_level_3_4_repair() at the bottom of this
module) is the one exception to "regenerate_node_fn is an LLM call" --
see that function's own docstring on why a deterministic clip, not a
fresh LLM proposal, is the right fix at the last level.

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
`mech["placements"][i]["part_id"]`). Level 1->2's node_id (G3e-4) is a
`subsection_id` -- also matching eo/mech_validator.py's own LEVEL_1_2
violation shape, and (per eo/mech_subsections.py's
group_into_subsections()) always equal to its anchor part's own
`part_id`. Level 2->3's node_id (G3f-2) is a `section_id` -- one of eo/
mech_sections.py's fixed _SECTION_ORDER values, matching eo/
mech_validator.py's own LEVEL_2_3 violation shape. Level 3->4's node_id
(G3g, this patch) is ALSO a `section_id` -- the same _SECTION_ORDER
values minus "Enclosure" (never a checkable node at Level 3->4 -- see
eo/mech_validator.py's own module docstring), matching that module's own
LEVEL_3_4 violation shape. This module's public entry point,
run_repair_loop(), is written generically against "whatever
mech_validator.validate_layout() calls node_id" and only gets
level-specific in its one private helper that narrows `mech` down to
just the retried nodes for re-validation (_subset_for_nodes() below) --
that's the one piece of this module that needed a sibling case added for
Level 1->2, Level 2->3, and now Level 3->4; the retry/cap/flag loop above
it does not change.

run_level_1_2_repair() (G3e-4) is the Level 1->2 integration: builds the
`regenerate_node_fn` closure over agents/mech_subsection_pool.py's
regenerate_subsection() (that level's "generate" half), drives
run_repair_loop() with it, and -- once the loop settles -- persists every
checkable subsection's validated aggregate footprint back onto
`mech["subsections"]` so G3f has something to group by next.

run_level_2_3_repair() (G3f-2, bottom of this module) is the same shape
one level up: builds `regenerate_node_fn` over agents/
mech_section_pool.py's regenerate_section() (Level 2->3's own "generate"
half, G3f-1), drives run_repair_loop() with `parts` threaded through
(Level 2->3's section grouping needs it -- see eo/mech_validator.py's
_checkable_sections() docstring), and -- once the loop settles -- persists
every checkable section's validated aggregate footprint onto
`mech["sections"]` so G3g has something to merge next. Each of these
level drivers is kept in THIS module rather than its own pool module
because it owns the repair loop's lifecycle (including the final
full-mech footprint recompute), not just one node's regeneration --
same reasoning run_level_1_2_repair()'s own docstring already gives.

run_level_0_1_repair() closes a gap left open through G3i: Level 0->1's
own generate->validate->repair pass was never actually wired up, even
though eo/mech_validator.py's LEVEL_0_1 path (G3c) and run_repair_loop()
(G3d) were already generic enough to handle it. G3i's own scope was
explicitly Level 1->2 through 3->4 only, so this driver -- and agents/
mech_primitive_pool.py's regenerate_primitives(), its "generate" half --
were the piece still missing. Simpler than the two drivers above: no
`parts` requirement (Level 0->1's own checkable set is derived straight
off `mech["placements"]`, same as eo/mech_validator.py's own
_checkable_placements()) and no final footprint-persisting step (Level
0->1 has no `footprints` output for a later level to consume -- see that
function's own docstring).
"""

import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.mech_validator import (
    LEVEL_0_1,
    LEVEL_1_2,
    LEVEL_2_3,
    LEVEL_3_4,
    validate_layout,
)
from relay.emitter import emit_event

# Master Guide: "Retry cap: 2 attempts per node."
DEFAULT_MAX_RETRIES = 2


def _subset_for_nodes(mech: dict, node_ids: set, level: str = LEVEL_0_1,
                       parts: list = None) -> dict:
    """Narrows `mech` down to just the placements the caller is about to
    re-validate, so a repair round only pays for what it just changed.

    Level 0->1 (default, and the only case prior to G3e-4): keyed off
    `mech["placements"]`/`part_id` directly, the one schema eo/
    mech_validator.py's validate_layout() originally understood.

    Level 1->2 (G3e-4): `node_ids` are `subsection_id`s, not `part_id`s.
    eo/mech_validator.py's LEVEL_1_2 path re-derives its own subsection
    grouping from `mech["placements"]` (via eo/mech_subsections.py's
    group_into_subsections()) on every call, so the subset this function
    builds must include BOTH a kept subsection's anchor placement AND its
    "mount_"-prefixed sibling -- dropping just the mount here would
    silently turn every requested subsection back into a (mount-less)
    singleton for validate_layout()'s own re-grouping, which would never
    re-check the collision that triggered the retry in the first place. A
    subsection genuinely without a mount sibling (shouldn't reach here at
    all -- see agents/mech_subsection_pool.py's regenerate_subsection()
    docstring on why LEVEL_1_2 only ever reports a collision violation
    for a two-member subsection) just contributes no extra id, harmlessly.

    Level 2->3 (G3f-2, this patch): `node_ids` are `section_id`s. eo/
    mech_validator.py's LEVEL_2_3 path re-derives its own section
    grouping from `mech["placements"]` AND `parts` (via eo/
    mech_sections.py's group_into_sections()) on every call, so this
    branch needs `parts` too -- same reason eo/mech_validator.py's own
    _checkable_sections() needs it. The subset built here is every
    placement belonging to ANY subsection inside a kept section (resolved
    via eo/mech_sections.py's subsections_for_section() down to eo/
    mech_subsections.py's members_for_subsection() for each), same
    "narrow to full membership, not just the anchor" reasoning the Level
    1->2 branch above already applies one level down -- dropping a
    section's non-anchor subsection here would silently turn the
    re-validated section back into a singleton, missing exactly the
    cross-subsection collision that triggered the retry. `parts=None`
    (caller hasn't wired it through) degrades to an empty placements list
    -- see eo/mech_validator.py's own _checkable_sections() docstring on
    why that's the safe default rather than a crash.

    Level 3->4 (G3g, this patch): `node_ids` are ALSO `section_id`s (same
    vocabulary as Level 2->3, minus "Enclosure" -- see eo/mech_validator.py's
    own module docstring on why the Enclosure section is never a
    checkable node at this level), so the placements-subset half below is
    identical to Level 2->3's own -- every placement belonging to ANY
    subsection inside a kept section, resolved the same two-hop way. The
    one real difference: eo/mech_validator.py's own LEVEL_3_4 path
    (_checkable_device_sections()) ALSO needs the Enclosure section's own
    already-validated `footprint` to check every kept section's
    containment against, and that footprint is NOT re-derivable from
    `placements`+`parts` alone (it's a validated aggregate this module's
    own run_level_2_3_repair() wrote onto `mech["sections"]`, not
    something a fresh group_into_sections() call recomputes) -- so this
    branch also (a) keeps every Enclosure-section member in the
    placements subset (so a caller re-deriving section grouping from just
    the subset still finds a real Enclosure section to resolve) and
    (b) passes `mech["sections"]` through UNCHANGED under a `"sections"`
    key in the returned dict, alongside the narrowed `"placements"` -- the
    one place this function's subset shape isn't just
    `{"placements": [...]}` for any level.
    """
    placements = (mech or {}).get("placements") or []
    if level in (LEVEL_2_3, LEVEL_3_4):
        if not parts:
            return {"placements": []}
        from eo.mech_sections import group_into_sections, subsections_for_section
        from eo.mech_subsections import members_for_subsection

        sections_by_id = {
            s.get("section_id"): s for s in group_into_sections(mech, parts)
            if isinstance(s, dict) and s.get("section_id")
        }
        keep_part_ids = set()
        for node_id in node_ids:
            section = sections_by_id.get(node_id)
            if section is None:
                continue
            for subsection in subsections_for_section(mech, section):
                for member in members_for_subsection(mech, subsection):
                    if isinstance(member, dict) and member.get("part_id"):
                        keep_part_ids.add(member["part_id"])
        if level == LEVEL_3_4:
            enclosure_section = sections_by_id.get("Enclosure")
            if enclosure_section is not None:
                for subsection in subsections_for_section(mech, enclosure_section):
                    for member in members_for_subsection(mech, subsection):
                        if isinstance(member, dict) and member.get("part_id"):
                            keep_part_ids.add(member["part_id"])
            return {
                "placements": [
                    p for p in placements
                    if isinstance(p, dict) and p.get("part_id") in keep_part_ids
                ],
                "sections": mech.get("sections") if isinstance(mech, dict) else None,
            }
        return {
            "placements": [
                p for p in placements
                if isinstance(p, dict) and p.get("part_id") in keep_part_ids
            ],
        }
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
                     max_retries: int = DEFAULT_MAX_RETRIES, parts: list = None) -> dict:
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

    `parts`: only meaningful at level=LEVEL_2_3 (G3f-2) -- threaded
    straight through to both validate_layout() and _subset_for_nodes(),
    which each need it to re-derive Level 2->3's category-based section
    grouping (see eo/mech_validator.py's _checkable_sections() and this
    module's own _subset_for_nodes() docstrings). Ignored at every other
    level, same as those two functions themselves ignore it.

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

    result = validate_layout(mech, level, session_id=session_id, path=path, domain=domain, parts=parts)
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
                _subset_for_nodes(mech, regen_ok_ids, level=level, parts=parts), level,
                session_id=session_id, path=path, domain=domain, parts=parts,
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


def run_level_0_1_repair(spec: dict, parts: list, session_id: str = None, path: str = None,
                          domain: str = None, max_retries: int = DEFAULT_MAX_RETRIES,
                          key_override=None) -> dict:
    """Level 0->1's own driver -- the piece that was still missing after
    G3i: eo/mech_validator.py's LEVEL_0_1 path (G3c) and
    run_repair_loop() (G3d) were both already written generically enough
    to handle Level 0->1 with zero changes, and agents/
    mech_primitive_pool.py's regenerate_primitives() (this same patch)
    is that level's `regenerate_node_fn` -- but nothing in the codebase
    ever actually called either of them together. G3i's own scope was
    explicitly Level 1->2 through 3->4 only, so Level 0->1 kept shipping
    whatever G3a/G3b's initial composition pass produced, uncorrected,
    even when a primitive genuinely poked outside its own part's
    bounding box. This function is that missing call; meant to run
    synchronously from agents.hardware_speccer.run_hardware_speccer()
    right after G3b's mech_primitive_pool.run() (the initial "generate"
    pass this function's own repair loop then corrects), same in-process
    shape run_level_1_2_repair() already uses one level up.

    What this does, in order:
      1. run_repair_loop(mech, LEVEL_0_1, regenerate_node_fn, ...) --
         validates every placement's primitives against its own part's
         w/h/d bounding box, and for each containment violation, calls
         agents/mech_primitive_pool.py's regenerate_primitives() with
         the violation fed back as context, capped at `max_retries` per
         part, exactly like every other level's repair loop (see this
         module's own top docstring).

    Unlike run_level_1_2_repair()/run_level_2_3_repair() one level up,
    there is no step 2/3 here -- Level 0->1 has no `footprints` output
    for a later level to consume (eo/mech_validator.py's own
    validate_layout() only returns a `footprints` key for the grouped
    levels, LEVEL_1_2/LEVEL_2_3/LEVEL_3_4; see that module's own
    `is_grouped_level` branch), so there's nothing to persist onto
    `mech` beyond what `regenerate_node_fn` already wrote in place
    during the loop itself. A final full re-validate would only ever
    reconfirm what run_repair_loop()'s own return value already reports.

    Returns run_repair_loop()'s own result dict, unmodified.

    Does NOT call eo/mech_validator.py's close_session() -- same "caller
    owns the session lifecycle for the whole run" contract every other
    level driver in this module already follows; whatever ultimately
    drives the full Level 0->1 through Level 3->4 run is responsible for
    closing it once, at the very end.
    """
    from agents.mech_primitive_pool import regenerate_primitives
    from eo.mech_validator import LEVEL_0_1 as _LEVEL_0_1

    mech = spec.get("mech") or {}
    parts_by_id = {p.get("id"): p for p in (parts or []) if isinstance(p, dict)}

    def regenerate_node_fn(mech_arg, node_id, violation, attempt):
        regenerate_primitives(
            mech_arg, node_id, violation, attempt, parts_by_id,
            key_override=key_override, session_id=session_id, path=path, domain=domain,
        )

    return run_repair_loop(
        mech, _LEVEL_0_1, regenerate_node_fn,
        session_id=session_id, path=path, domain=domain, max_retries=max_retries,
    )


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
    from agents.mech_subsection_pool import regenerate_subsection
    from eo.mech_subsections import apply_subsection_grouping
    from eo.mech_validator import LEVEL_1_2 as _LEVEL_1_2

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


def run_level_2_3_repair(spec: dict, parts: list, session_id: str = None, path: str = None,
                          domain: str = None, max_retries: int = DEFAULT_MAX_RETRIES,
                          key_override=None) -> dict:
    """G3f-2's Level 2->3 integration -- the "wire it all together" step
    eo/mech_sections.py's group_into_sections() (G3f-1), agents/
    mech_section_pool.py's regenerate_section() (this level's "generate"
    half, also G3f-1) and eo/mech_validator.py's LEVEL_2_3 (G3f-2's own
    "validate" half, above) were each built standalone for -- the exact
    same shape run_level_1_2_repair() already establishes one level down,
    with `parts` now threaded through everywhere LEVEL_2_3's category-
    based grouping needs it. Meant to run synchronously from
    agents.hardware_speccer.run_hardware_speccer(), called AFTER
    agents/mech_section_pool.py's own run() (G3f-1's initial proposal
    pass) has already populated every in-scope non-anchor subsection's
    x/y/z once, so this function's own repair loop only ever regenerates
    a section that's ALREADY been proposed at least once -- and AFTER
    run_level_1_2_repair() below it, since Level 2->3 grouping and
    validation both depend on `mech["subsections"][*]["footprint"]`
    already being populated.

    What this does, in order:
      1. run_repair_loop(mech, LEVEL_2_3, regenerate_node_fn, parts=parts,
         ...) -- validates every section, and for each cross-subsection
         collision, calls agents/mech_section_pool.py's
         regenerate_section() with the violation fed back as context,
         capped at `max_retries` per section, exactly like every other
         level's repair loop (see this module's own top docstring).
      2. ONE final validate_layout(mech, LEVEL_2_3, parts=parts, ...) call
         over the FULL mech -- needed for the same reason
         run_level_1_2_repair()'s own step 2 needs it: run_repair_loop()'s
         own per-round validate_layout() calls are deliberately narrowed
         to just the regenerated nodes each round, so no single call
         inside the loop ever holds every section's footprint at once.
         eo/mech_validator.py's persistent-sandbox-session design (G3c)
         makes this second batched call cheap once step 1 has already
         warmed the session. Skipped if step 1 already reports a
         `validator_error` -- the sandbox isn't usable right now, so a
         second call would just fail the same way.
      3. Writes every section's footprint from that final call onto
         `mech["sections"]` (each entry's own new `footprint` key), via
         eo/mech_sections.py's apply_section_grouping() -- G3g's own
         input, per the Master Guide's Level 3->4 description ("places
         sections relative to each other inside the enclosure
         footprint"). A section the final call couldn't compute a
         footprint for (every member of every one of its subsections
         failed to build -- see eo/mech_validator.py's _check_section()
         docstring) is left without a `footprint` key rather than one
         filled with a fabricated value, same posture
         run_level_1_2_repair() already holds for subsections.

    Returns run_repair_loop()'s own result dict from step 1, unmodified --
    the footprint-persisting step is a side effect on `spec`/`mech`, not a
    change to what this function reports about repair outcomes.

    Does NOT call eo/mech_validator.py's close_session() -- same "caller
    owns the session lifecycle for the whole run" contract
    run_level_1_2_repair()'s own docstring already establishes; whatever
    ultimately drives the full Level 0->1 through Level 3->4 run is
    responsible for closing it once, at the very end, not this one
    level's driver.
    """
    from agents.mech_section_pool import regenerate_section
    from eo.mech_sections import apply_section_grouping
    from eo.mech_validator import LEVEL_2_3 as _LEVEL_2_3

    mech = spec.get("mech") or {}

    def regenerate_node_fn(mech_arg, node_id, violation, attempt):
        regenerate_section(
            mech_arg, node_id, violation, attempt, parts,
            key_override=key_override, session_id=session_id, path=path, domain=domain,
        )

    result = run_repair_loop(
        mech, _LEVEL_2_3, regenerate_node_fn,
        session_id=session_id, path=path, domain=domain, max_retries=max_retries,
        parts=parts,
    )

    if result.get("validator_error") is None:
        final = validate_layout(mech, _LEVEL_2_3, session_id=session_id, path=path, domain=domain, parts=parts)
        footprints = final.get("footprints") or {}
        sections = apply_section_grouping(mech, parts)  # also stashes onto mech["sections"]
        for section in sections:
            footprint = footprints.get(section.get("section_id"))
            if footprint is not None:
                section["footprint"] = footprint

    return result


def _clamp_section_into_container(mech: dict, section_id: str, parts: list) -> None:
    """Level 3->4's own "generate" half (G3g, this patch) -- see
    run_level_3_4_repair()'s own docstring below for why a deterministic
    clip, not a fresh LLM proposal, is the right fix at this level. Reads
    the Enclosure section's own validated `footprint` off
    `mech["sections"]` (run_level_2_3_repair()'s own output, the same
    source eo/mech_validator.py's own _checkable_device_sections() reads)
    as the container, and `section_id`'s own current `footprint` (also
    already on `mech["sections"]`, set by the FULL validate_layout(...,
    LEVEL_3_4) call this function's caller already made before deciding
    to regenerate) as the thing being clipped. Either footprint missing
    is treated as "nothing to clip yet," a no-op -- same fail-safe
    posture every other "not ready" branch in this tree already holds.

    Unlike eo/mech_device.py's own apply_device_merge() (which computes a
    translation from scratch, unconditionally, every call), this only
    ever nudges `section_id` back toward the container on whichever
    axis/axes it's actually hanging off of -- x if the section's
    footprint pokes past the container's left or right edge, y if it
    pokes past the top or bottom, both, or neither. A section already
    fully inside the container on x/y (its only remaining violation is a
    cross-section collision with a NEIGHBOR, not containment) gets no
    translation from this function at all -- pulling an already-contained
    section further inward wouldn't be a fix, it'd just be a different
    arbitrary position, and eo/mech_device.py's own front/center/edge
    packing already chose this one deliberately. That case, and a
    section whose OWN footprint is simply wider or taller than the
    container itself (translation alone can never fully contain it, no
    matter which direction it's nudged), both fall through to
    run_repair_loop()'s own retry-cap-then-flag path -- same "ships
    flagged in the UI rather than blocking" fail-safe philosophy the
    Master Guide specifies for every level, this function included.

    Mutates every real member placement belonging to `section_id`
    (resolved the identical two-hop way this module's own
    _subset_for_nodes() LEVEL_3_4 branch above already resolves a section
    down to its real placements) by the SAME rigid dx/dy for every
    member -- only ever translates, matching eo/mech_device.py's own
    "never resizes anything" contract exactly, so a member's own
    Level 0->1/1->2/2->3-validated internal geometry (already correct)
    is never disturbed by this level's own repair. Also shifts
    `section_id`'s own `footprint` entry on `mech["sections"]` by the
    same dx/dy, mirroring eo/mech_device.py's own apply_device_merge()
    footprint-shift step, so the NEXT validate_layout(..., LEVEL_3_4)
    call in this repair round sees a section that's actually moved.
    """
    if not parts:
        return
    sections = mech.get("sections") if isinstance(mech, dict) else None
    if not sections:
        return

    container = None
    section_entry = None
    for section in sections:
        if not isinstance(section, dict):
            continue
        if section.get("section_id") == "Enclosure":
            footprint = section.get("footprint")
            container = footprint if isinstance(footprint, dict) else None
        if section.get("section_id") == section_id:
            section_entry = section
    if container is None or section_entry is None:
        return
    footprint = section_entry.get("footprint")
    if not isinstance(footprint, dict):
        return

    cx, cy = float(container.get("x") or 0), float(container.get("y") or 0)
    cw, ch = float(container.get("w") or 0), float(container.get("h") or 0)
    cz, cd = float(container.get("z") or 0), float(container.get("d") or 0)
    fx, fy = float(footprint.get("x") or 0), float(footprint.get("y") or 0)
    fw, fh = float(footprint.get("w") or 0), float(footprint.get("h") or 0)
    fz, fd = float(footprint.get("z") or 0), float(footprint.get("d") or 0)

    dx = 0.0
    if fx < cx:
        dx = cx - fx
    elif fx + fw > cx + cw:
        dx = (cx + cw) - (fx + fw)
    dy = 0.0
    if fy < cy:
        dy = cy - fy
    elif fy + fh > cy + ch:
        dy = (cy + ch) - (fy + fh)
    dz = 0.0
    if fz < cz:
        dz = cz - fz
    elif fz + fd > cz + cd:
        dz = (cz + cd) - (fz + fd)

    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        return  # already inside the container on x/y/z -- see docstring
                 # on why a pure cross-section collision or a genuinely
                 # oversize section both fall through untouched here.

    from eo.mech_sections import group_into_sections, subsections_for_section
    from eo.mech_subsections import members_for_subsection

    sections_by_id = {
        s.get("section_id"): s for s in group_into_sections(mech, parts)
        if isinstance(s, dict) and s.get("section_id")
    }
    plan_section = sections_by_id.get(section_id)
    if plan_section is None:
        return

    for subsection in subsections_for_section(mech, plan_section):
        for member in members_for_subsection(mech, subsection):
            if not isinstance(member, dict):
                continue
            member["x"] = float(member.get("x") or 0) + dx
            member["y"] = float(member.get("y") or 0) + dy
            member["z"] = float(member.get("z") or 0) + dz

    footprint["x"] = fx + dx
    footprint["y"] = fy + dy
    footprint["z"] = fz + dz


def run_level_3_4_repair(spec: dict, parts: list, session_id: str = None, path: str = None,
                          domain: str = None, max_retries: int = DEFAULT_MAX_RETRIES) -> dict:
    """G3g's Level 3->4 integration (second half of this patch) -- the
    same shape every earlier level driver in this module already
    establishes, closing out the tree (Master Guide: "this is the last
    level"). Meant to run synchronously from
    agents.hardware_speccer.run_hardware_speccer(), called AFTER
    run_level_2_3_repair() below has already populated every checkable
    section's validated footprint onto `mech["sections"]` (Level 3->4's
    own containment check needs the Enclosure section's footprint to
    exist -- see eo/mech_validator.py's _checkable_device_sections()) AND
    after eo/mech_device.py's apply_device_merge() has already run once to
    actually position every section's real placements at their
    front/center/edge slot (this function's own repair loop only ever
    regenerates -- clips -- a section that's already been positioned once,
    same "already proposed at least once" precondition every earlier
    level driver in this module states for itself).

    One deliberate difference from run_level_1_2_repair()/
    run_level_2_3_repair() one level down: `regenerate_node_fn` here is
    NOT an LLM call into a `agents/mech_*_pool.py` sibling -- eo/
    mech_device.py's own module docstring is explicit that Level 3->4 has
    no worker-pool sibling at all ("five possible nodes... explicitly
    meant to replace an LLM call for"), and a section's own INTERNAL
    geometry was already fully validated and repaired one level down by
    run_level_2_3_repair() -- so a fresh LLM proposal here would just
    re-litigate a question that's already settled. A Level 3->4 violation
    is never "this section's own layout is wrong" -- it's "this section,
    positioned exactly where eo/mech_device.py's deterministic
    front/center/edge rule put it, doesn't actually fit the housing eo/
    mech_sections.py's Enclosure section already settled on, or overlaps
    a neighboring section." eo/mech_device.py's own docstring calls this
    exact case out: "this module deliberately doesn't paper over that
    itself" -- catching and fixing it is explicitly this function's job.
    The fix (`_clamp_section_into_container()` above) is a deterministic
    rigid nudge back toward the container, never a resize -- see that
    function's own docstring on why, and on its honestly-acknowledged
    limits (a genuinely oversize section, or a pure section-vs-section
    collision with no containment component, isn't always fixable by
    translation alone, and falls through to this repair loop's own
    retry-cap-then-flag path exactly like every other unresolved
    violation anywhere else in this tree).

    What this does, in order -- identical shape to run_level_2_3_repair()
    one level down:
      1. run_repair_loop(mech, LEVEL_3_4, regenerate_node_fn, parts=parts,
         ...) -- validates every non-Enclosure section against the
         Enclosure section's own footprint and every other section, and
         for each containment/collision violation, clips that section's
         real member placements back toward the container, capped at
         `max_retries` per section.
      2. ONE final validate_layout(mech, LEVEL_3_4, parts=parts, ...) call
         over the FULL mech -- same "narrowed per-round calls inside the
         loop, one full batched call after" reasoning run_level_2_3_repair()
         and run_level_1_2_repair() both already give; eo/mech_validator.py's
         persistent-sandbox-session design (G3c) makes this cheap once
         step 1 has already warmed the session. Skipped if step 1 already
         reports a `validator_error`.
      3. Writes every checked section's footprint from that final call
         onto `mech["sections"]` (via eo/mech_sections.py's
         apply_section_grouping(), same as run_level_2_3_repair() already
         does one level down), then calls eo/mech_device.py's
         apply_device_merge() one more time to recompute and re-stash the
         FINAL device layout plan onto `mech["device"]` -- this level's
         own equivalent of "the next level's own input," even though
         Level 3->4 is the last level in THIS tree (G3i's pipeline wiring
         and G3j's frontend badge, both outside this tree, are the actual
         consumers of `mech["device"]` now).

    Returns run_repair_loop()'s own result dict from step 1, unmodified --
    the footprint/device-plan-persisting step is a side effect on
    `spec`/`mech`, not a change to what this function reports about
    repair outcomes.

    Does NOT call eo/mech_validator.py's close_session() -- same "caller
    owns the session lifecycle for the whole run" contract every earlier
    level driver in this module already holds itself to; whatever
    ultimately drives the full Level 0->1 through Level 3->4 run (G3i) is
    responsible for closing it once, at the very end, not this last
    level's driver either.
    """
    from eo.mech_device import apply_device_merge
    from eo.mech_sections import apply_section_grouping
    from eo.mech_validator import LEVEL_3_4 as _LEVEL_3_4

    mech = spec.get("mech") or {}

    def regenerate_node_fn(mech_arg, node_id, violation, attempt):
        _clamp_section_into_container(mech_arg, node_id, parts)

    result = run_repair_loop(
        mech, _LEVEL_3_4, regenerate_node_fn,
        session_id=session_id, path=path, domain=domain, max_retries=max_retries,
        parts=parts,
    )

    if result.get("validator_error") is None:
        final = validate_layout(mech, _LEVEL_3_4, session_id=session_id, path=path, domain=domain, parts=parts)
        footprints = final.get("footprints") or {}
        sections = apply_section_grouping(mech, parts)  # also stashes onto mech["sections"]
        for section in sections:
            footprint = footprints.get(section.get("section_id"))
            if footprint is not None:
                section["footprint"] = footprint
        apply_device_merge(mech, parts)  # re-stashes the final plan onto mech["device"]

    return result


# ---------------------------------------------------------------------------
# Patch C.5 (Phase C, Mech View standalone implementation guide) --
# repair suggestion on a Patch C.4 balance-check failure.
#
# eo/mech_validator.py's check_balance() (C.4) is a PURE scan over
# eo/mech_balance.py's compute_cog()/compute_support_polygon() (C.3) --
# it never sits inside the Level 0->1..3->4 generate->validate->repair
# tree the rest of this module drives (it's not one of validate_layout()'s
# LEVEL_* paths, and run_repair_loop() above has no node/level vocabulary
# that maps onto "the whole device's CoG is off"). So this patch does NOT
# call run_repair_loop() -- there is no `regenerate_node_fn`-per-node shape
# to give it, and no `validate_layout()` call for it to drive. What it DOES
# reuse from this module's own established posture (per Patch C.5's own
# wording, "following the repair-until-cap posture (one retry, not an
# unbounded loop) already used elsewhere in mech_repair.py" -- see this
# module's own top docstring's "Retry cap... after the cap, it ships with
# the violation flagged in the UI rather than blocking" line): exactly ONE
# corrective attempt, then flag-don't-loop if that attempt didn't fix it.
#
# The fix itself -- "suggest repositioning the heaviest contributing part
# (typically the battery) toward the polygon's centroid" (literal C.5
# wording) -- is a deterministic rigid nudge, same "translate, never
# resize, never re-propose from an LLM" posture _clamp_section_into_container()
# above already holds for Level 3->4's own repair.
# ---------------------------------------------------------------------------

def _joined_balance_members(mech: dict, parts: list) -> list:
    """Every placed member across every section, each paired with its
    own looked-up mass -- the same two-hop section->subsection->member
    join eo/mech_balance.py's own `_joined_mass_members()` already
    performs for compute_cog()/compute_support_polygon(), kept as a
    SEPARATE local copy here rather than importing that module's own
    (underscore-prefixed, module-private) helper -- same "each module
    owns its own join" precedent that module's own top docstring
    already states for itself relative to eo/mech_supports.py,
    eo/mech_cutouts.py, and eo/mech_swept_volume.py.

    Returns a list of `(member_dict, mass_g)` pairs. `member_dict` is
    the LIVE placement dict from `mech["placements"]` (via
    eo/mech_subsections.py's `members_for_subsection()`, which resolves
    by reference, not a copy -- see that function's own docstring) --
    deliberately NOT shallow-copied the way eo/mech_balance.py's own
    join is, because this function's one caller (`_heaviest_member()`
    below) needs to actually mutate the winning member's `x`/`y` in
    place, the same "member dicts are the real placement dicts" access
    pattern `_clamp_section_into_container()` above already relies on.
    `mass_g` is a curated eo/mech_mass.py `lookup_mass()` hit, or
    eo/mech_balance.py's own `_DEFAULT_UNKNOWN_MASS_G` placeholder on a
    miss -- same "still contributes SOME mass rather than vanishing"
    reasoning that module's own `compute_cog()` already applies, so the
    "heaviest contributor" this function feeds into never silently
    skips an unlisted part.

    Returns `[]` for a `mech` with no sections yet, never raises.
    """
    from eo.mech_balance import _DEFAULT_UNKNOWN_MASS_G
    from eo.mech_mass import lookup_mass
    from eo.mech_sections import subsections_for_section
    from eo.mech_subsections import members_for_subsection

    if not isinstance(mech, dict) or not mech.get("sections"):
        return []

    parts_by_id = {
        p.get("id"): p for p in (parts or []) if isinstance(p, dict) and p.get("id")
    }

    joined = []
    for section in mech.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                part = parts_by_id.get(member.get("part_id"))
                generic_name = part.get("generic_name") if isinstance(part, dict) else None
                mass_entry = lookup_mass(generic_name)
                mass_g = mass_entry["mass_g"] if mass_entry else _DEFAULT_UNKNOWN_MASS_G
                joined.append((member, mass_g))
    return joined


def _heaviest_member(mech: dict, parts: list):
    """The single heaviest placed member found by `_joined_balance_members()`
    above -- literal C.5 wording "the heaviest contributing part
    (typically the battery)": a curated eo/mech_mass.py MASS_TABLE
    battery entry (45-60g) dominates every other common part type's
    own curated mass (see that module's own MASS_TABLE), so this
    generic "pick the max" rule naturally resolves to the battery on
    any layout that has one, without this function special-casing
    `generic_name` at all.

    Returns `(member_dict, mass_g)` for the max-mass entry -- ties
    (shouldn't come up given real-world mass values, but possible on a
    synthetic/test layout) resolve to the first-encountered one, the
    same section->subsection->member iteration order
    `_joined_balance_members()` above already fixes, a deterministic
    but otherwise arbitrary tie-break. Returns `(None, 0.0)` if there
    are no placed members to choose from at all.
    """
    joined = _joined_balance_members(mech, parts)
    if not joined:
        return None, 0.0
    return max(joined, key=lambda pair: pair[1])


def _polygon_centroid(support_polygon: list) -> dict:
    """The plain, UNWEIGHTED centroid of `support_polygon`'s own hull
    points (eo/mech_balance.py's own `compute_support_polygon()`
    output) -- the reposition TARGET this patch nudges the heaviest
    part toward. Deliberately not the mass-weighted device CoG
    (eo/mech_balance.py's own `compute_cog()` -- that's the value
    that's already off-center, the violation itself, not a target to
    aim at) and not any single hull vertex -- the polygon's own rough
    middle is a reasonable "pull the heavy part back toward the
    device's own footprint" target regardless of the polygon's exact
    shape.

    Returns `{"x": 0.0, "y": 0.0}` for an empty `support_polygon`,
    never raises.
    """
    if not support_polygon:
        return {"x": 0.0, "y": 0.0}
    xs = [float(p.get("x") or 0) for p in support_polygon if isinstance(p, dict)]
    ys = [float(p.get("y") or 0) for p in support_polygon if isinstance(p, dict)]
    if not xs:
        return {"x": 0.0, "y": 0.0}
    return {"x": round(sum(xs) / len(xs), 3), "y": round(sum(ys) / len(ys), 3)}


def _reposition_toward(member: dict, target: dict) -> None:
    """Mutates `member`'s own `x`/`y` (never `z` -- a balance repair
    only ever shifts weight in the ground plane, same "translate on
    exactly the axes the violation is actually about" posture
    `_clamp_section_into_container()` above already holds for its own
    x/y/z containment nudge) so its own footprint CENTER -- not its
    own min-corner -- lands exactly on `target` (`_polygon_centroid()`'s
    own `{"x", "y"}` output above). Uses `eo.mech_balance`'s own
    `_footprint_center()` for the "min-corner + half extent" convention
    every placement in this tree already shares (see that function's
    own docstring), so this stays in sync with however that convention
    is defined, rather than re-deriving it here.
    """
    from eo.mech_balance import _footprint_center

    center_x, center_y, _center_z = _footprint_center(member)
    dx = float(target.get("x") or 0) - center_x
    dy = float(target.get("y") or 0) - center_y
    member["x"] = float(member.get("x") or 0) + dx
    member["y"] = float(member.get("y") or 0) + dy


def repair_balance(mech: dict, parts: list, session_id: str = None, path: str = None,
                    domain: str = None) -> dict:
    """Patch C.5 -- the repair half of Patch C.4's
    eo/mech_validator.py `check_balance()`. Meant to run synchronously
    right after that check, on a `mech` whose archetype/mobility_type
    has already made `check_balance()` a real (non-skipped) check --
    same "already been through the check once" precondition every
    other repair entry point in this module already states for itself.

    What this does, in order:
      1. Calls `check_balance(mech, parts)`. If it's skipped (mobility
         type not wheeled/legged) or already passing, this function is
         a no-op -- returns immediately, `attempted=False`, nothing
         mutated. Matches Patch C.4's own gate exactly; this patch adds
         no new gating logic of its own.
      2. On a real violation, finds the single heaviest placed member
         via `_heaviest_member()` above and nudges its own footprint
         center to the support polygon's own centroid via
         `_polygon_centroid()`/`_reposition_toward()` above -- ONE
         attempt, mutating `mech["placements"]` in place (through the
         live member reference `_joined_balance_members()` resolves,
         same live-reference access `_clamp_section_into_container()`
         above already relies on for its own Level 3->4 nudge). A
         violation reported as `"insufficient_ground_contact_points"`
         (no real support base at all -- see `check_balance()`'s own
         docstring) or a `mech` with no placed members to choose from
         has nothing a reposition could fix, so this step is skipped
         and the ORIGINAL violation is flagged immediately,
         `attempted=False`.
      3. Re-runs `check_balance(mech, parts)` exactly once more. Per
         the Master Guide's own retry-cap philosophy this module's top
         docstring already states ("after the cap, it ships with the
         violation flagged in the UI rather than blocking") -- literal
         Patch C.5 wording, "one retry, not an unbounded loop": if the
         single reposition attempt fixed it, `repaired=True`; if not,
         the layout is left at its post-attempt position (not rolled
         back -- same "the attempt already happened, flag what's still
         wrong" posture `run_repair_loop()`'s own retry-then-flag path
         above already holds) and the SECOND call's own violations are
         what gets flagged, since those reflect the actual current
         (post-attempt) state.

    Returns `{"ok": bool, "skipped": bool, "attempted": bool,
    "repaired": bool, "violations": [...], "cog": dict or None,
    "support_polygon": list}` -- same violation/cog/support_polygon
    shape `check_balance()` itself returns, plus the two fields this
    patch's own repair posture adds (`attempted`, `repaired`).

    Pure with respect to `parts` (never mutated); mutates `mech` only
    via the single reposition in step 2, and only when a repair is
    actually attempted.
    """
    agent_name = "mech_repair"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": "Mech Repair — Balance (Phase C)"})
    started = time.monotonic()

    from eo.mech_validator import check_balance

    result = check_balance(mech, parts)
    if result.get("skipped") or result.get("ok"):
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = "skipped (mobility type not checked)" if result.get("skipped") else "balanced, no repair needed"
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": summary, "duration_ms": duration_ms})
        return {
            "ok": result.get("ok"), "skipped": result.get("skipped"),
            "attempted": False, "repaired": False,
            "violations": result.get("violations") or [],
            "cog": result.get("cog"), "support_polygon": result.get("support_polygon") or [],
        }

    member, _mass_g = _heaviest_member(mech, parts)
    support_polygon = result.get("support_polygon") or []

    if member is None or len(support_polygon) < 3:
        # Nothing a reposition could fix -- no heaviest member to move,
        # or no real support polygon to aim it at (see this function's
        # own docstring, step 2). Flag the ORIGINAL violation, unattempted.
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": "unbalanced, no reposition target available",
                            "duration_ms": duration_ms})
        return {
            "ok": False, "skipped": False, "attempted": False, "repaired": False,
            "violations": result.get("violations") or [],
            "cog": result.get("cog"), "support_polygon": support_polygon,
        }

    target = _polygon_centroid(support_polygon)
    _reposition_toward(member, target)

    retry_result = check_balance(mech, parts)
    repaired = bool(retry_result.get("ok"))

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = "repaired on first reposition attempt" if repaired else "still unbalanced after one reposition attempt, flagged"
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})

    return {
        "ok": repaired, "skipped": False, "attempted": True, "repaired": repaired,
        "violations": retry_result.get("violations") or [],
        "cog": retry_result.get("cog"), "support_polygon": retry_result.get("support_polygon") or [],
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
