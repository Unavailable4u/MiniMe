"""
eo/mech_subsections.py — G3e-1 (Master Guide, "G3/G4. Hierarchical
parallel build + validate", Level 1->2 "Subsections"): the deterministic,
LLM-free half of Level 1->2 -- turns Level 1's flat mech.placements list
into Level 2's grouping, a part plus its own G0-created mount, per the
Master Guide's own definition of what a subsection even is: "Level 2's
grouping isn't invented -- it's G0's own mount-per-subsystem
decomposition (MCU + its mount are already sibling parts in the BOM), so
'subsection' just means a part and the mechanical thing that holds it, a
real, already-existing relationship."

This is the first, purely-mechanical rung of Level 1->2 -- same build-
order reasoning as G3a being the first rung of Level 0->1. No LLM call,
no FreeCAD call: this module only reshapes data that's already sitting on
`mech["placements"]` into subsection groups. G3e-2
(agents/mech_subsection_pool.py) is what actually proposes each member's
relative placement inside a group this module creates; G3e-3 (eo/
mech_validator.py's LEVEL_1_2, not built by this patch) is what checks
it; G3e-4 (not built by this patch) wires both into eo/mech_repair.py's
shared retry loop -- this module's own output shape (`subsection_id` /
`member_ids`) is written to be exactly what that loop's node-identifier
contract (`{"node_id", "issue"}`, per eo/mech_repair.py's own docstring)
will key off of once it lands: `subsection_id` doubles as Level 1->2's
`node_id`.

Grouping rule: reuses G0's own mount-naming convention exactly
(agents/hardware_speccer.py's `_MOUNT_ID_PREFIX = "mount_"` -- duplicated
here as a local constant rather than imported, matching eo/
mech_validator.py's own documented precedent of never importing agents/
hardware_speccer.py or agents/mech_primitive_pool.py from this package;
see that module's "Dependency shape" docstring section, and
agents/mech_primitive_pool.py's own module docstring on why the reverse
import direction is the circular one). A placement whose `part_id` is
NOT itself "mount_"-prefixed is a subsection's anchor; if a sibling
placement with id "mount_" + that part_id also exists, it joins the same
subsection. A part with no mount sibling becomes a singleton subsection
of one -- Level 2 still needs to name it something, and "a part with no
mount" is a perfectly valid (if trivial) "part + the mechanical thing
that holds it" case (nothing holds it but the enclosure itself). An
orphaned "mount_x" placement whose "x" part doesn't exist in this mech
(shouldn't happen given G0's own sibling-creation convention, but a part
deleted/renamed mid-pipeline shouldn't silently vanish from Level 2
either) becomes its own singleton subsection too -- same "nothing gets
dropped" fail-safe posture agents/hardware_speccer.py's own
_ensure_electrical_placements() already practices for Level 0->1
coverage.

Idempotent / side-effect-free: group_into_subsections() is a pure
function of `mech["placements"]` -- it does not mutate `mech` and can be
called repeatedly (once by G3e-2 to know what to generate for, again
later by G3e-3/G3e-4 to know what to validate/repair) without needing to
cache or invalidate anything.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mirrors agents/hardware_speccer.py's own _MOUNT_ID_PREFIX exactly (see
# module docstring on why this is a local copy, not an import).
MOUNT_ID_PREFIX = "mount_"


def group_into_subsections(mech: dict) -> list:
    """Returns Level 2's node list:
        [{"subsection_id": str, "member_ids": [part_id, ...]}, ...]

    `subsection_id` is always the anchor part's own `part_id` (never a
    synthesized "sub_"-prefixed id) -- Level 2 nodes stay named after
    real BOM parts, same "no invented identifiers" spirit as every other
    level in this tree. `member_ids` holds 1 entry (no mount found) or 2
    (part + its "mount_"-prefixed sibling), always ordered part-then-
    mount.

    Placements with no `part_id` are skipped entirely -- there's no
    identity to group or later validate them under, matching eo/
    mech_validator.py's own `_checkable_placements()` skip-don't-flag
    posture for entries that aren't ready to participate in a level yet.

    Never modifies `mech` -- read-only, same contract eo/
    mech_validator.py's validate_layout() already holds itself to.
    """
    placements = (mech or {}).get("placements") or []
    by_id = {
        p.get("part_id"): p for p in placements
        if isinstance(p, dict) and p.get("part_id")
    }

    subsections = []
    grouped_mount_ids = set()

    for placement in placements:
        if not isinstance(placement, dict):
            continue
        part_id = placement.get("part_id")
        if not part_id or part_id.startswith(MOUNT_ID_PREFIX):
            continue  # mounts are only ever visited as a sibling below, never their own anchor

        mount_id = MOUNT_ID_PREFIX + str(part_id)
        member_ids = [part_id]
        if mount_id in by_id:
            member_ids.append(mount_id)
            grouped_mount_ids.add(mount_id)
        subsections.append({"subsection_id": part_id, "member_ids": member_ids})

    # Orphaned mounts -- see module docstring.
    for placement in placements:
        if not isinstance(placement, dict):
            continue
        part_id = placement.get("part_id")
        if (part_id and part_id.startswith(MOUNT_ID_PREFIX)
                and part_id not in grouped_mount_ids):
            subsections.append({"subsection_id": part_id, "member_ids": [part_id]})

    return subsections


def members_for_subsection(mech: dict, subsection: dict) -> list:
    """Resolves a subsection's `member_ids` back to their full placement
    dicts from `mech["placements"]`, in the same order as `member_ids`.
    Shared helper -- G3e-2 needs each member's w/h/d to generate relative
    placement, G3e-3/G3e-4 (not built by this patch) will need their
    absolute x/y/z + primitives to validate/repair, and both would
    otherwise duplicate this exact lookup-by-part_id loop.

    A `member_id` with no matching placement (shouldn't happen --
    group_into_subsections() only ever emits member_ids it just read
    from `mech["placements"]` itself) is silently skipped rather than
    raising, same fail-safe posture as the rest of this module.
    """
    placements = (mech or {}).get("placements") or []
    by_id = {
        p.get("part_id"): p for p in placements
        if isinstance(p, dict) and p.get("part_id")
    }
    return [
        by_id[member_id] for member_id in (subsection or {}).get("member_ids") or []
        if member_id in by_id
    ]


def apply_subsection_grouping(mech: dict) -> list:
    """Convenience wrapper matching this pipeline's usual mutate-in-place
    call shape (agents/hardware_speccer.py's own G-series steps all work
    this way) -- computes group_into_subsections(mech) and also stashes
    it on `mech["subsections"]`, so a caller that just wants the side
    effect (e.g. a future top-level Level-1->2 driver, G3e-4) doesn't
    have to thread the return value through by hand. Still returns the
    list too, so a caller that wants the pure-function shape (G3e-2,
    tests) can ignore the mutation and just use the return value.
    """
    subsections = group_into_subsections(mech)
    if isinstance(mech, dict):
        mech["subsections"] = subsections
    return subsections


if __name__ == "__main__":
    import json

    _demo_mech = {
        "placements": [
            {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5},
            {"part_id": "mount_mcu_1", "x": 0, "y": 25, "z": 0, "w": 30, "h": 5, "d": 5},
            {"part_id": "battery_1", "x": 50, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},
        ],
    }
    print(json.dumps(group_into_subsections(_demo_mech), indent=2))
