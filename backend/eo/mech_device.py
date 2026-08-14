"""
eo/mech_device.py — G3g, first half (Master Guide, "G3/G4. Hierarchical
parallel build + validate", Level 3->4 "Device"): the deterministic,
LLM-free half of Level 3->4 -- takes Level 3's five functional sections
(eo/mech_sections.py's group_into_sections(), each already carrying a
validated aggregate `footprint` once eo/mech_repair.py's
run_level_2_3_repair() -- G3f-2 -- has run) and positions them relative to
each other inside the enclosure footprint, per the Master Guide's own
rule: "a small deterministic merge step positions groups relative to each
other inside the enclosure footprint (front/center/edge rule)."

Same build-order reasoning every earlier "-1" module in this tree already
established (eo/mech_subsections.py's G3e-1, eo/mech_sections.py's
G3f-1): land the pure, mechanical, LLM-free reshaping/positioning step
FIRST, on its own, so eo/mech_validator.py's future LEVEL_3_4 (global
containment + cross-section collision) and eo/mech_repair.py's future
run_level_3_4_repair() (G3g, second half, NOT this patch) each have a
real, already-testable device layout to validate against instead of a
moving target. Unlike G3f-1, there is no worker-pool sibling to this
module (no agents/mech_device_pool.py) -- Level 3->4 has only five
possible nodes (_SECTION_ORDER's own five values), a fixed small number
the Master Guide's own front/center/edge rule is explicitly meant to
replace an LLM call for, not fan one out per node the way Level 1->2 and
2->3's own per-group LLM calls do.

The container is the Enclosure section, not a synthesized box: per
agents/hardware_speccer.py's own SYSTEM_PROMPT ("The housing's placement
should span the full enclosure footprint... the lid matches the
housing -- same x/y footprint"), the housing+lid pairing already IS the
device's real outer boundary once G0 places it, and eo/mech_sections.py's
own grouping already buckets housing/lid/mounts/fasteners into the
"Enclosure" section (category "3D_PRINT"/"MISC" -> Enclosure). So this
module never invents a bounding box to merge sections into -- it reads
the Enclosure section's own already-validated `footprint` (G3f-2's own
output) and treats that as fixed ground truth. The Enclosure section
itself is never translated by this module: it's the frame every other
section gets positioned inside of, not a fifth thing competing for a
zone slot.

Zone rule: front/center/edge is applied along the container's own `h`
axis (the plan-view "into the device" dimension -- agents/
hardware_speccer.py's own SYSTEM_PROMPT documents `d` as the vertical
stacking axis the lid sits on top of via its own `z`, so `h`, not `d`, is
the correct in-plane axis left for a front-to-back functional layout; `w`
is reserved below for side-by-side placement within a zone). The
container's `h` span is split into three equal bands -- front (nearest
`y`), center (the middle third), edge (farthest third) -- and each
section is assigned to exactly one band via _SECTION_TO_ZONE below:
  - Power -> front. Batteries/switches conventionally want the easiest
    physical access, the same reasoning a real enclosure layout would
    use -- "front" as in "the side a person reaches first."
  - Compute -> center. The MCU is the electrical hub every other
    section's wiring ultimately routes through (same "Compute" bucket
    _CATEGORY_TO_SECTION in eo/mech_sections.py already gives modules/
    radios/peripherals) -- placing it centrally keeps every other zone's
    average wiring run short, not just a cosmetic choice.
  - Sensing, Actuation -> edge. Sensors and actuators are the parts most
    likely to need a real-world-facing mounting position (a button
    reachable from outside, a sensor with a line of sight) -- "edge," the
    band farthest from the front-access power zone, is the natural
    periphery slot for both. Two sections sharing one zone is expected,
    not an error -- see "Packing within a zone" below.
  - Enclosure -> not zoned at all; see above.
A section whose `section_id` isn't one of these five (shouldn't happen,
same "G0's own enum, not a new fact this module decides" reasoning eo/
mech_sections.py's own docstring already gives for _CATEGORY_TO_SECTION)
falls back to "edge" via _DEFAULT_ZONE -- same "safest fallback bucket"
posture as eo/mech_sections.py's own _DEFAULT_SECTION.

Packing within a zone: sections sharing a zone (Sensing + Actuation, most
runs) are placed side by side along the container's `w` axis, left-
aligned starting at the container's own `x` plus a fixed margin, each
offset by the running sum of its zone-siblings' own `w` plus
_ZONE_CLEARANCE_MM -- same "stack along one axis with a fixed clearance
gap" shape every other collision-avoidance step in this tree already
uses (eo/mech_validator.py's own confidence-aware tolerance buffer is the
same idea one level down). This is why sections are grouped and packed in
`mech["sections"]`'s OWN existing order (eo/mech_sections.py's own
`_SECTION_ORDER`, already fixed and deterministic per that module's
"Section ordering" docstring section) rather than a new ordering invented
here -- two runs over the same layout always pack a shared zone the same
way.

What this module does NOT do: resize anything. A section's own `w`/`h`/`d`
footprint (already validated by G3f-2) is never changed -- only
translated as a rigid whole. If the packed result doesn't actually fit
inside the Enclosure section's own footprint (e.g. five sections' combined
`w` exceeds the housing's own `w`), that is exactly the "global
containment" violation eo/mech_validator.py's future LEVEL_3_4 (G3g,
second half) is meant to catch and hand to eo/mech_repair.py's future
run_level_3_4_repair() to fix -- by regenerating the housing/lid's own
size, not by this module quietly shrinking a section to make it fit. This
module's only job is to propose where; whether "where" actually fits is
strictly a downstream question.

Translation only touches x/y, never z: this module's `h`-axis banding and
`w`-axis packing are both planar (x/y) concerns; a section's own `z`
(vertical position within the housing's `d`-axis stack, set back at
Level 0->1 by whatever placed its members originally) is left completely
alone. Re-stacking sections vertically is not part of the Master Guide's
"front/center/edge" description and this module doesn't invent a second
axis convention on top of it.

Dependency shape: imports ONLY eo/mech_sections.py and eo/
mech_subsections.py (both already peer modules in this same package) --
no import of agents/hardware_speccer.py or any *_pool.py, same "this
package never imports agents/" precedent every earlier eo/ module in
this tree already establishes. `parts` is passed straight through to eo/
mech_sections.py's own functions, which are what actually need it (see
that module's docstring) -- this module never inspects `parts` itself.

Idempotent by construction, not by special-casing: plan_device_layout()
always recomputes every zone's packed x/y from the Enclosure section's
current footprint and each candidate section's current footprint, so
calling apply_device_merge() a second time on an already-merged `mech`
computes translation deltas that land right back where the first call
already put everything (assuming nothing else moved a footprint in
between) -- {"dx": 0.0, "dy": 0.0, "dz": 0.0} for every section, a no-op
translation, not a special "already merged" branch this module has to
track separately.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.mech_sections import group_into_sections, subsections_for_section
from eo.mech_subsections import members_for_subsection

# The one section this module never assigns a zone to -- it's the
# container every zoned section gets positioned inside of. See module
# docstring's "The container is the Enclosure section" section.
_CONTAINER_SECTION_ID = "Enclosure"

# See module docstring's "Zone rule" section for the reasoning behind
# each of these five assignments.
_ZONE_ORDER = ["front", "center", "edge"]
_SECTION_TO_ZONE = {
    "Power": "front",
    "Compute": "center",
    "Sensing": "edge",
    "Actuation": "edge",
}
_DEFAULT_ZONE = "edge"

# Fixed clearance gap between two sections packed side by side in the
# same zone, and between the container's own edge and the first section
# in each band -- a device-level gap, deliberately larger than eo/
# mech_validator.py's own part-level confidence tolerance (1.5mm typical)
# since this is separating whole functional zones from each other, not
# just buffering one part's manufacturing slop.
_ZONE_CLEARANCE_MM = 8.0
_ZONE_MARGIN_MM = 8.0


def _sections_with_footprints(mech: dict, parts: list) -> list:
    """Returns the section list to plan against: `mech["sections"]` as-is
    when present (eo/mech_repair.py's run_level_2_3_repair() -- G3f-2 --
    both computes it via eo/mech_sections.py's apply_section_grouping()
    AND then annotates each entry in that exact same list with its own
    `footprint` key afterward, so `mech["sections"]` is the one place a
    footprint actually lives), falling back to a fresh, footprint-less
    eo/mech_sections.py's group_into_sections(mech, parts) call when
    `mech["sections"]` hasn't been populated yet at all. That fallback
    case still yields no footprints for anything (nothing here computes
    one -- that's G3f-2's job, not this module's), so
    _container_footprint() below still correctly reports "nothing to
    plan yet" for it; the fallback only exists so a caller mid-migration
    that hasn't wired `apply_section_grouping()`'s output onto
    `mech["sections"]` yet degrades to a clean None instead of a KeyError.
    """
    sections = mech.get("sections") if isinstance(mech, dict) else None
    if sections:
        return sections
    return group_into_sections(mech, parts)


def _container_footprint(sections: list) -> dict:
    """Returns the Enclosure section's own `footprint`, or None if the
    Enclosure section either isn't present in `sections` yet or hasn't
    been validated by eo/mech_repair.py's run_level_2_3_repair() yet
    (no `footprint` key) -- "nothing to merge against yet," same
    fail-safe posture every earlier "not ready yet" branch in this tree
    already holds, not an error.
    """
    for section in sections or []:
        if isinstance(section, dict) and section.get("section_id") == _CONTAINER_SECTION_ID:
            footprint = section.get("footprint")
            return footprint if isinstance(footprint, dict) else None
    return None


def plan_device_layout(mech: dict, parts: list) -> dict:
    """Returns Level 4's single node, or None if there's nothing to plan
    yet (see _container_footprint() above):
        {"device_id": "device",
         "container_section_id": "Enclosure",
         "zones": {"front": [section_id, ...], "center": [...], "edge": [...]},
         "translations": {section_id: {"dx": float, "dy": float, "dz": float}},
         "footprint": {"x","y","z","w","h","d"}}

    `zones` lists only the sections this call actually placed (has its
    own `footprint` already -- see below), in each zone's packing order.
    `translations` covers the same set: the dx/dy/dz apply_device_merge()
    (below) needs to move every one of that section's real placements
    from where they are now to their new front/center/edge slot. The
    Enclosure section itself never appears in either -- see module
    docstring.

    `footprint` is the union bounding box of the Enclosure section's own
    footprint and every zoned section's NEW (post-translation) footprint
    -- i.e. what the device's real extent would be after
    apply_device_merge() runs. This can be LARGER than the Enclosure
    section's own footprint (a packed zone that doesn't fit) -- that's a
    signal, not a bug; see module docstring's "What this module does NOT
    do" section.

    A section in `sections` with no `footprint` yet (G3f-2 hasn't reached
    it) is skipped from packing entirely, same "skip, don't flag, not
    ready yet" posture eo/mech_validator.py's own checkable-filtering
    functions already hold throughout this tree -- it simply doesn't
    appear in `zones`/`translations` this call, and keeps whatever x/y/z
    its members already have.

    Never modifies `mech` or `parts` -- read-only, same contract every
    other pure planning/grouping function in this package already holds
    itself to. See apply_device_merge() below for the mutate-in-place
    counterpart.
    """
    sections = _sections_with_footprints(mech, parts)
    container = _container_footprint(sections)
    if container is None:
        return None

    cx, cy = float(container.get("x") or 0), float(container.get("y") or 0)
    ch = float(container.get("h") or 0)
    band_h = ch / float(len(_ZONE_ORDER))
    band_start = {
        zone: cy + (band_h * index)
        for index, zone in enumerate(_ZONE_ORDER)
    }

    zones = {zone: [] for zone in _ZONE_ORDER}
    translations = {}
    running_x_by_zone = {zone: cx + _ZONE_MARGIN_MM for zone in _ZONE_ORDER}

    # Iterate in `sections`'s own order -- eo/mech_sections.py's
    # group_into_sections() already returns _SECTION_ORDER's fixed,
    # deterministic order (see module docstring's "Packing within a
    # zone" section on why that's what makes a shared zone pack the same
    # way on every run).
    bounds = [container["x"], container["y"], container.get("z", 0),
              container["x"] + container["w"], container["y"] + container["h"],
              container.get("z", 0) + container.get("d", 0)]

    for section in sections:
        section_id = section.get("section_id")
        if section_id == _CONTAINER_SECTION_ID:
            continue
        footprint = section.get("footprint")
        if not isinstance(footprint, dict):
            continue  # not validated by G3f-2 yet -- see docstring

        zone = _SECTION_TO_ZONE.get(section_id, _DEFAULT_ZONE)
        fw = float(footprint.get("w") or 0)
        fh = float(footprint.get("h") or 0)

        target_x = running_x_by_zone[zone]
        target_y = band_start[zone] + _ZONE_MARGIN_MM
        running_x_by_zone[zone] = target_x + fw + _ZONE_CLEARANCE_MM

        dx = target_x - float(footprint.get("x") or 0)
        dy = target_y - float(footprint.get("y") or 0)
        translations[section_id] = {"dx": dx, "dy": dy, "dz": 0.0}
        zones[zone].append(section_id)

        new_x, new_y = target_x, target_y
        new_z = float(footprint.get("z") or 0)
        fd = float(footprint.get("d") or 0)
        bounds[0] = min(bounds[0], new_x)
        bounds[1] = min(bounds[1], new_y)
        bounds[2] = min(bounds[2], new_z)
        bounds[3] = max(bounds[3], new_x + fw)
        bounds[4] = max(bounds[4], new_y + fh)
        bounds[5] = max(bounds[5], new_z + fd)

    device_footprint = {
        "x": round(bounds[0], 3), "y": round(bounds[1], 3), "z": round(bounds[2], 3),
        "w": round(bounds[3] - bounds[0], 3),
        "h": round(bounds[4] - bounds[1], 3),
        "d": round(bounds[5] - bounds[2], 3),
    }

    return {
        "device_id": "device",
        "container_section_id": _CONTAINER_SECTION_ID,
        "zones": {zone: ids for zone, ids in zones.items() if ids},
        "translations": translations,
        "footprint": device_footprint,
    }


def apply_device_merge(mech: dict, parts: list) -> dict:
    """Convenience wrapper matching this pipeline's usual mutate-in-place
    call shape (eo/mech_subsections.py's own apply_subsection_grouping(),
    eo/mech_sections.py's own apply_section_grouping()) -- computes
    plan_device_layout(mech, parts) and, unless it's None (nothing to
    merge yet), actually MOVES every zoned section's real placements:
    for each `section_id` in the plan's `translations`, resolves that
    section back to its full member placements (via eo/mech_sections.py's
    subsections_for_section() down to eo/mech_subsections.py's
    members_for_subsection() -- the same two-hop resolution eo/
    mech_repair.py's own _subset_for_nodes() LEVEL_2_3 branch already
    uses for the identical reason) and adds `dx`/`dy`/`dz` onto each
    member's own `x`/`y`/`z` in place, so a subsection's already-
    validated INTERNAL relative geometry (G3e/G3f's own job) is preserved
    exactly -- this function only ever rigid-translates a section as a
    whole, never touches a member's position relative to its siblings.

    Also shifts the section's own `footprint` entry in `mech["sections"]`
    by the same delta (only `x`/`y`/`z`; `w`/`h`/`d` unchanged) so a
    second call, or eo/mech_validator.py's future LEVEL_3_4 reading
    `mech["sections"]` afterward, sees a footprint that's already
    consistent with where the members actually are now -- see module
    docstring's "Idempotent by construction" section on why this is what
    makes a repeat call a no-op instead of double-translating.

    Stashes the plan on `mech["device"]` (None if plan_device_layout()
    itself returned None) -- same "mutate in place AND still return the
    value" convention every other apply_* function in this package
    already follows, so a caller that wants the side effect and a caller
    that wants the pure return value (tests, eo/mech_repair.py's future
    run_level_3_4_repair()) are both served by one call.
    """
    plan = plan_device_layout(mech, parts)
    if isinstance(mech, dict):
        mech["device"] = plan
    if plan is None:
        return None

    # Same source _sections_with_footprints() (called inside
    # plan_device_layout() above) already resolved -- re-reading
    # `mech["sections"]` here (rather than recomputing via
    # group_into_sections()) means this loop mutates the SAME dicts
    # `mech["sections"]` already holds, so the footprint shift below
    # lands on the object a caller reading `mech["sections"]` afterward
    # actually sees, not a disconnected copy.
    sections_by_id = {
        s.get("section_id"): s for s in _sections_with_footprints(mech, parts)
        if isinstance(s, dict) and s.get("section_id")
    }

    for section_id, delta in plan["translations"].items():
        section = sections_by_id.get(section_id)
        if section is None:
            continue
        dx, dy, dz = delta["dx"], delta["dy"], delta["dz"]

        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                member["x"] = float(member.get("x") or 0) + dx
                member["y"] = float(member.get("y") or 0) + dy
                member["z"] = float(member.get("z") or 0) + dz

        footprint = section.get("footprint")
        if isinstance(footprint, dict):
            footprint["x"] = float(footprint.get("x") or 0) + dx
            footprint["y"] = float(footprint.get("y") or 0) + dy
            footprint["z"] = float(footprint.get("z") or 0) + dz

    return plan


if __name__ == "__main__":
    import json

    _demo_mech = {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "battery_1", "x": 5, "y": 5, "z": 2, "w": 20, "h": 10, "d": 10},
            {"part_id": "mount_battery_1", "x": 5, "y": 16, "z": 2, "w": 20, "h": 5, "d": 10},
            {"part_id": "mcu_1", "x": 5, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5},
            {"part_id": "mount_mcu_1", "x": 5, "y": 26, "z": 2, "w": 30, "h": 5, "d": 5},
            {"part_id": "sensor_1", "x": 5, "y": 5, "z": 2, "w": 15, "h": 10, "d": 5},
            {"part_id": "button_1", "x": 5, "y": 5, "z": 2, "w": 10, "h": 10, "d": 8},
        ],
        "sections": [
            {"section_id": "Power", "subsection_ids": ["battery_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 20, "h": 16, "d": 10}},
            {"section_id": "Compute", "subsection_ids": ["mcu_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 30, "h": 26, "d": 5}},
            {"section_id": "Sensing", "subsection_ids": ["sensor_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 15, "h": 10, "d": 5}},
            {"section_id": "Actuation", "subsection_ids": ["button_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 10, "h": 10, "d": 8}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
    }
    _demo_parts = [
        {"id": "housing_1", "category": "3D_PRINT"},
        {"id": "lid_1", "category": "3D_PRINT"},
        {"id": "battery_1", "category": "power"},
        {"id": "mcu_1", "category": "mcu"},
        {"id": "sensor_1", "category": "sensor"},
        {"id": "button_1", "category": "actuator"},
    ]
    print(json.dumps(apply_device_merge(_demo_mech, _demo_parts), indent=2))
    print(json.dumps([p for p in _demo_mech["placements"] if p["part_id"] in
                       ("battery_1", "mcu_1", "sensor_1", "button_1")], indent=2))
