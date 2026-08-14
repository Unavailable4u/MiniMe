"""
eo/mech_sections.py — G3f-1 (Master Guide, "G3/G4. Hierarchical parallel
build + validate", Level 2->3 "Sections"): the deterministic, LLM-free
half of Level 2->3 -- groups Level 2's subsections (eo/mech_subsections.py's
group_into_subsections()) into Level 3's functional sections, per the
Master Guide's own definition: "subsections grouped by function (Power/
Compute/Sensing/Actuation/Enclosure shell)."

Same build-order reasoning G3e-1 (eo/mech_subsections.py) already
established one level down, and G3a before that: land the pure,
mechanical, LLM-free reshaping step FIRST, on its own, so
agents/mech_section_pool.py (G3f-1's other half, this same patch) and
eo/mech_validator.py's LEVEL_2_3 / eo/mech_repair.py's
run_level_2_3_repair() (G3f-2, NOT this patch) each have something real
and already-testable to build against instead of a moving target.

Grouping rule: a subsection's `subsection_id` is always its anchor part's
own `part_id` (eo/mech_subsections.py's group_into_subsections() doc-
string), so this module looks that id up in the BOM `parts` list -- NOT
`mech["placements"]`, which never carries `category` -- and buckets the
subsection under whichever functional section that part's `category`
maps to, per _CATEGORY_TO_SECTION below. This mirrors agents/
hardware_speccer.py's own SYSTEM_PROMPT category enum exactly ("mcu",
"sensor", "actuator", "power", "module", "3D_PRINT", "MISC" -- see that
module's own docstring) rather than inventing a parallel classification
scheme: Level 3's five sections are a *view* over categories the BOM
already assigned at G0, not a new fact this module decides.

Two categories don't have their own named section in the Master Guide's
five ("Power/Compute/Sensing/Actuation/Enclosure shell") and need an
explicit call:
  - "module" -> Compute. A module (radio, RTC, expansion board) is a
    digital peripheral that lives and is wired alongside the MCU, not a
    sensing/actuation/power component in its own right -- the same
    grouping WiringGraph.jsx / _build_wiring_mermaid()'s own subgraph
    convention already treats "module" as its own bucket distinct from
    sensor/actuator, but this is a 5-bucket scheme, not a 6th, and
    Compute is the closest functional fit.
  - "MISC" -> Enclosure. Fasteners, gaskets, and other non-electrical
    hardware (agents/hardware_speccer.py's own SYSTEM_PROMPT examples:
    "M3 heat-set insert + screw", a gasket/seal line) are enclosure
    hardware, not a standalone function -- same bucket "3D_PRINT" (the
    housing/lid themselves) already lands in.
A subsection whose anchor part has no BOM entry at all, or a category
that isn't one of the seven recognized values (shouldn't happen given
G0's own enum, but a part deleted/renamed mid-pipeline -- same edge case
eo/mech_subsections.py's own "orphaned mount" handling exists for)
defaults to Enclosure too, via _DEFAULT_SECTION -- "goes with the shell"
is the safest fallback bucket, and (same as every other fail-safe in
this tree) it's a bucket assignment, never a dropped subsection.

Section ordering: _SECTION_ORDER fixes Power/Compute/Sensing/Actuation/
Enclosure as the canonical, deterministic iteration order for every
section that has at least one subsection in it (an empty section is
never emitted -- nothing downstream needs to reason about a section with
zero members). This isn't cosmetic: agents/mech_section_pool.py's worker-
pool fan-out (G3f-1's other half) and G3g's later front/center/edge
device-level merge both need a stable, repeatable section ordering to
stay deterministic across runs/regenerations, and "the order the Master
Guide itself lists them in" is as good a stable convention as any new
one this module could invent.

Dependency shape: imports ONLY eo/mech_subsections.py (G3e-1, already a
peer module in this same package) -- no import of agents/
hardware_speccer.py or any *_pool.py, same "this package never imports
agents/" precedent eo/mech_subsections.py's and eo/mech_validator.py's
own module docstrings already establish. `parts` is passed in by the
caller (agents/hardware_speccer.py's own run_hardware_speccer() already
has both `spec["mech"]` and `parts` on hand at the point Level 2->3 would
run) rather than this module reaching into agents/ to fetch it itself.

Idempotent / side-effect-free: group_into_sections() is a pure function
of `mech["placements"]` and `parts` -- same "callable repeatedly by
different downstream consumers without needing to cache or invalidate
anything" contract eo/mech_subsections.py's group_into_subsections()
already holds itself to.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.mech_subsections import group_into_subsections

# Master Guide, Level 3's own five named functions, in the order the
# guide itself lists them -- see module docstring's "Section ordering"
# section on why this fixed order matters downstream, not just here.
_SECTION_ORDER = ["Power", "Compute", "Sensing", "Actuation", "Enclosure"]

# See module docstring's "Grouping rule" section for the "module"->Compute
# and "MISC"->Enclosure calls specifically; the other five are a direct,
# unambiguous 1:1 with agents/hardware_speccer.py's own category enum.
_CATEGORY_TO_SECTION = {
    "power": "Power",
    "mcu": "Compute",
    "module": "Compute",
    "sensor": "Sensing",
    "actuator": "Actuation",
    "3D_PRINT": "Enclosure",
    "MISC": "Enclosure",
}
_DEFAULT_SECTION = "Enclosure"


def group_into_sections(mech: dict, parts: list) -> list:
    """Returns Level 3's node list:
        [{"section_id": str, "subsection_ids": [subsection_id, ...]}, ...]

    `section_id` is always one of _SECTION_ORDER's five values, never a
    synthesized id -- Level 3, like every other level in this tree,
    stays named after something real (here, a functional category the
    BOM already assigned) rather than an invented identifier. Only
    sections with at least one subsection are included, in
    _SECTION_ORDER's fixed order -- see module docstring.

    Never modifies `mech` or `parts` -- same read-only contract every
    other pure-grouping function in this package (eo/mech_subsections.py's
    group_into_subsections()) already holds itself to.
    """
    subsections = group_into_subsections(mech)
    parts_by_id = {
        p.get("id"): p for p in (parts or [])
        if isinstance(p, dict) and p.get("id")
    }

    subsection_ids_by_section = {name: [] for name in _SECTION_ORDER}
    for subsection in subsections:
        subsection_id = subsection.get("subsection_id")
        anchor_part = parts_by_id.get(subsection_id)
        category = anchor_part.get("category") if isinstance(anchor_part, dict) else None
        section_id = _CATEGORY_TO_SECTION.get(category, _DEFAULT_SECTION)
        subsection_ids_by_section[section_id].append(subsection_id)

    return [
        {"section_id": section_id, "subsection_ids": subsection_ids_by_section[section_id]}
        for section_id in _SECTION_ORDER
        if subsection_ids_by_section[section_id]
    ]


def subsections_for_section(mech: dict, section: dict) -> list:
    """Resolves a section's `subsection_ids` back to their full Level-2
    subsection dicts ({"subsection_id", "member_ids"}), in the same order
    as `subsection_ids`. Shared helper -- agents/mech_section_pool.py
    needs each subsection's members to look up footprints/dimensions,
    eo/mech_validator.py's future LEVEL_2_3 path (G3f-2, not this patch)
    will need the same resolution to build its own FreeCAD payload -- same
    "don't duplicate the lookup-by-id loop" reasoning eo/mech_subsections.py's
    own members_for_subsection() already documents for itself.

    A `subsection_id` with no matching Level-2 subsection (shouldn't
    happen -- group_into_sections() only ever emits subsection_ids it just
    read from group_into_subsections() itself) is silently skipped rather
    than raising, same fail-safe posture as the rest of this module.
    """
    subsections_by_id = {
        s.get("subsection_id"): s for s in group_into_subsections(mech)
        if isinstance(s, dict) and s.get("subsection_id")
    }
    return [
        subsections_by_id[subsection_id]
        for subsection_id in (section or {}).get("subsection_ids") or []
        if subsection_id in subsections_by_id
    ]


def apply_section_grouping(mech: dict, parts: list) -> list:
    """Convenience wrapper matching this pipeline's usual mutate-in-place
    call shape (eo/mech_subsections.py's own apply_subsection_grouping(),
    agents/hardware_speccer.py's G-series steps) -- computes
    group_into_sections(mech, parts) and also stashes it on
    `mech["sections"]`, so a caller that just wants the side effect
    doesn't have to thread the return value through by hand. Still
    returns the list too, so a caller that wants the pure-function shape
    (agents/mech_section_pool.py, tests) can ignore the mutation and just
    use the return value.
    """
    sections = group_into_sections(mech, parts)
    if isinstance(mech, dict):
        mech["sections"] = sections
    return sections


if __name__ == "__main__":
    import json

    _demo_mech = {
        "placements": [
            {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5},
            {"part_id": "mount_mcu_1", "x": 0, "y": 25, "z": 0, "w": 30, "h": 5, "d": 5},
            {"part_id": "battery_1", "x": 50, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},
            {"part_id": "mount_battery_1", "x": 50, "y": 15, "z": 0, "w": 20, "h": 5, "d": 10},
            {"part_id": "sensor_1", "x": 0, "y": 60, "z": 0, "w": 15, "h": 10, "d": 5},
            {"part_id": "housing_1", "x": -10, "y": -10, "z": -5, "w": 100, "h": 100, "d": 30},
            {"part_id": "fastener_1", "x": 0, "y": 0, "z": 0, "w": 5, "h": 5, "d": 5},
        ],
    }
    _demo_parts = [
        {"id": "mcu_1", "category": "mcu"},
        {"id": "battery_1", "category": "power"},
        {"id": "sensor_1", "category": "sensor"},
        {"id": "housing_1", "category": "3D_PRINT"},
        {"id": "fastener_1", "category": "MISC"},
    ]
    print(json.dumps(group_into_sections(_demo_mech, _demo_parts), indent=2))
