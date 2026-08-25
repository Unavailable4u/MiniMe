"""
eo/mech_access.py — Phase D, Patches D.2/D.3/D.4 of the Mech/Enclosure
implementation guide: the pure, deterministic access-mechanism geometry
generators that let one Enclosure sub-region open (hinge, snap-latch, or
slide) instead of every section being permanently screwed shut, per this
guide's own Phase D scope ("No access-mechanism modeling beyond
'fastened'" -- Part 1, item 5 -- and the guide's stated per-section, not
device-wide, framing for this phase).

Same build-order reasoning every earlier "pure function first" patch in
this tree already established (eo/mech_supports.py's own
compute_standoffs() before apply_supports_generation(), eo/
mech_enclosure.py's own compute_housing_footprint() before
apply_enclosure_generation()): the three side-effect-free primitive
generators (Patch D.2/D.3/D.4) are pure and testable with plain dict
inputs on their own; Patch D.5 (this same module's
apply_access_generation(), _declared_access_regions(), and
_bounding_box()) adds the access_type-dispatching wrapper and wires it
into the pipeline's mutate-in-place convention at the Level 3->4 call
site, immediately after eo/mech_supports.py's own
apply_supports_generation() wiring point.

Input shape: each generator takes one `section` dict shaped like a
Level-3 section boundary PLUS a placed footprint --
{"section_id": str, "x", "y", "z", "w", "h", "d"} (millimeters, same
"x/y/z/w/h/d" placement vocabulary eo/mech_sections.py's own demo
placements and eo/mech_supports.py's `section_members` already use) --
the box the access mechanism is being cut into/mounted across. Patch
D.2/D.3/D.4's three generators never read `mech["sections"]` themselves
(that shape is {"section_id", "subsection_ids"}, with no footprint of
its own) -- this same module's own apply_access_generation() (Patch D.5,
below) is what actually resolves a subsection's declared `access_type`
and bounding-box footprint before calling into one of these three, same
"reads a shape, doesn't go fetch related data" boundary eo/
mech_supports.py's own compute_standoffs() already holds for
`section_members`.

Every generator here reads its sizing numbers from
eo/enclosure_spec.py's ACCESS_GEOMETRY (Patch D.1) -- no hand-rolled
constants duplicated in this module, same "one shared config, every
consumer reads from it" posture ENCLOSURE_SPEC/SUPPORT_CATEGORIES/
CUTOUT_TABLE already established for Phases 1/2/5 respectively.

Genuinely different geometry per type (pivot vs. flexure vs. linear
travel) is why this guide splits D.2/D.3/D.4 into three functions in one
module rather than one do-everything generator -- a bug in
generate_snap_latch() can't block generate_hinge() or generate_slide()
from landing/being tested independently, and each function's own
docstring below is self-contained per the Master Guide's "isolating
them" rationale.

Idempotent / side-effect-free: none of the three functions below ever
mutate `section` -- same read-only contract every other pure-geometry
function in this package already holds itself to (eo/mech_supports.py's
compute_standoffs(), eo/mech_cutouts.py's cutout generators).
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.enclosure_spec import ACCESS_GEOMETRY, ACCESS_TYPES, DEFAULT_ACCESS_TYPE
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection


def _section_box(section: dict) -> dict:
    """Shared footprint read -- every generator below needs the same
    boundary box, defaulting missing fields to 0 rather than raising, same
    fail-safe posture eo/mech_supports.py's own _corner_primitives() holds
    for a member dict with missing fields.
    """
    return {
        "x": section.get("x", 0),
        "y": section.get("y", 0),
        "z": section.get("z", 0),
        "w": section.get("w", 0),
        "h": section.get("h", 0),
        "d": section.get("d", 0),
    }


def generate_hinge(section: dict) -> dict:
    """Knuckle/pin pair primitive at `section`'s own boundary (Patch D.2).

    Places `knuckle_count` knuckle barrels evenly spaced along the
    section's width (its `w` edge, running along x at the section's own y),
    alternating which half of the joint each knuckle belongs to (even
    index -> "housing", odd index -> "lid") so the assembled hinge
    interleaves correctly, plus one continuous `hinge_pin` primitive
    spanning the same length that every knuckle's bore is sized to accept.

    Returns:
        {"section_id": str, "access_type": "hinged",
         "primitives": [ {"type": "hinge_knuckle", "member": "housing"|"lid",
                           "x", "y", "z", "diameter_mm", "length_mm",
                           "bore_diameter_mm"}, ...,
                          {"type": "hinge_pin", "x", "y", "z",
                           "diameter_mm", "length_mm"} ]}

    A section with zero width (`w` == 0, e.g. a malformed/unresolved
    boundary) still returns a well-formed result with an empty
    `primitives` list rather than raising or dividing by zero -- same
    fail-safe posture as the rest of this package.
    """
    geo = ACCESS_GEOMETRY["hinged"]
    box = _section_box(section)
    section_id = section.get("section_id")

    primitives = []
    knuckle_count = geo["knuckle_count"]
    knuckle_len = geo["knuckle_length_mm"]
    knuckle_dia = geo["knuckle_dia_mm"]
    bore_dia = geo["pin_dia_mm"] + geo["pin_clearance_mm"]

    if box["w"] > 0 and knuckle_count > 0:
        # Evenly spaced along the width edge, centered within it.
        span = knuckle_count * knuckle_len
        start_x = box["x"] + (box["w"] - span) / 2.0
        for i in range(knuckle_count):
            primitives.append({
                "type": "hinge_knuckle",
                "member": "housing" if i % 2 == 0 else "lid",
                "x": start_x + i * knuckle_len,
                "y": box["y"],
                "z": box["z"],
                "diameter_mm": knuckle_dia,
                "length_mm": knuckle_len,
                "bore_diameter_mm": bore_dia,
            })

        primitives.append({
            "type": "hinge_pin",
            "x": start_x,
            "y": box["y"],
            "z": box["z"],
            "diameter_mm": geo["pin_dia_mm"],
            "length_mm": span,
        })

    return {
        "section_id": section_id,
        "access_type": "hinged",
        "primitives": primitives,
    }


def generate_snap_latch(section: dict) -> dict:
    """Cantilever hook + catch primitive pair at `section`'s own boundary
    (Patch D.3).

    Split from generate_hinge() rather than sharing a code path -- a
    cantilever flexure and a pivot knuckle are genuinely different
    geometry (see module docstring's "Why split from D.2" note), so a
    latch is one hook primitive (mounted on the moving half) plus one
    catch primitive (mounted on the fixed half), not a variant of the
    hinge's knuckle/pin shape.

    Placed centered on the section's width edge (single latch point --
    unlike the hinge's evenly-distributed knuckles, one snap point is
    the common real-world case for a small access panel; nothing here
    prevents a caller from invoking this once per additional latch point
    a larger panel might need).

    Returns:
        {"section_id": str, "access_type": "snap_latch",
         "primitives": [ {"type": "latch_hook", "x", "y", "z",
                           "length_mm", "width_mm", "thickness_mm"},
                          {"type": "latch_catch", "x", "y", "z",
                           "depth_mm", "overhang_mm"} ]}
    """
    geo = ACCESS_GEOMETRY["snap_latch"]
    box = _section_box(section)
    section_id = section.get("section_id")

    center_x = box["x"] + box["w"] / 2.0 - geo["cantilever_width_mm"] / 2.0

    primitives = [
        {
            "type": "latch_hook",
            "x": center_x,
            "y": box["y"],
            "z": box["z"],
            "length_mm": geo["cantilever_length_mm"],
            "width_mm": geo["cantilever_width_mm"],
            "thickness_mm": geo["cantilever_thickness_mm"],
        },
        {
            "type": "latch_catch",
            "x": center_x,
            "y": box["y"] + geo["cantilever_length_mm"],
            "z": box["z"],
            "depth_mm": geo["catch_depth_mm"],
            "overhang_mm": geo["catch_overhang_mm"],
        },
    ]

    return {
        "section_id": section_id,
        "access_type": "snap_latch",
        "primitives": primitives,
    }


def generate_slide(section: dict) -> dict:
    """Channel + stop primitive pair at `section`'s own boundary
    (Patch D.4) -- e.g. a slide-out battery hatch.

    One pair of parallel `slide_channel` primitives (running the section's
    full depth, `d`, along the axis the part slides in) flanking the
    section's width, plus one `slide_stop` primitive at the channel's far
    end that keeps the slide from over-traveling out of its channel.

    Returns:
        {"section_id": str, "access_type": "slide",
         "primitives": [ {"type": "slide_channel", "side": "left"|"right",
                           "x", "y", "z", "length_mm", "clearance_mm",
                           "depth_mm"},
                          {"type": "slide_stop", "x", "y", "z",
                           "length_mm", "height_mm"} ]}
    """
    geo = ACCESS_GEOMETRY["slide"]
    box = _section_box(section)
    section_id = section.get("section_id")

    primitives = [
        {
            "type": "slide_channel",
            "side": "left",
            "x": box["x"],
            "y": box["y"],
            "z": box["z"],
            "length_mm": box["d"],
            "clearance_mm": geo["channel_clearance_mm"],
            "depth_mm": geo["channel_depth_mm"],
        },
        {
            "type": "slide_channel",
            "side": "right",
            "x": box["x"] + box["w"],
            "y": box["y"],
            "z": box["z"],
            "length_mm": box["d"],
            "clearance_mm": geo["channel_clearance_mm"],
            "depth_mm": geo["channel_depth_mm"],
        },
        {
            "type": "slide_stop",
            "x": box["x"],
            "y": box["y"],
            "z": box["z"] + box["d"],
            "length_mm": geo["stop_length_mm"],
            "height_mm": geo["stop_height_mm"],
        },
    ]

    return {
        "section_id": section_id,
        "access_type": "slide",
        "primitives": primitives,
    }


# Patch D.5's own dispatch table -- one entry per non-default ACCESS_TYPES
# member, same set ACCESS_GEOMETRY (Patch D.1) already keys on. "fastened"
# has no entry, same reason it has no ACCESS_GEOMETRY entry: it's a no-op,
# never dispatched to a generator at all (see apply_access_generation()'s
# own skip below).
_GENERATORS = {
    "hinged": generate_hinge,
    "snap_latch": generate_snap_latch,
    "slide": generate_slide,
}


def _bounding_box(members: list) -> dict:
    """Reduces a subsection's own placed members (each a
    {"x","y","z","w","h","d", ...} dict, same shape
    eo/mech_subsections.py's members_for_subsection() already returns) to
    the single enclosing box a generator in this module needs as its own
    `section` argument -- the min corner across every member's own
    (x, y, z) and the max corner across every member's own
    (x + w, y + h, z + d), same axis-aligned-bounding-box reduction
    eo/mech_device.py's own device-footprint computation already applies
    one level up (there, over placements; here, over one subsection's
    members).

    An empty `members` list (a subsection with no resolvable members --
    shouldn't happen given group_into_subsections()'s own construction,
    but never assumed) returns an all-zero box rather than raising, same
    fail-safe posture _section_box() above already holds for a missing
    field on a single dict.
    """
    if not members:
        return {"x": 0, "y": 0, "z": 0, "w": 0, "h": 0, "d": 0}

    min_x = min(m.get("x", 0) for m in members)
    min_y = min(m.get("y", 0) for m in members)
    min_z = min(m.get("z", 0) for m in members)
    max_x = max(m.get("x", 0) + m.get("w", 0) for m in members)
    max_y = max(m.get("y", 0) + m.get("h", 0) for m in members)
    max_z = max(m.get("z", 0) + m.get("d", 0) for m in members)

    return {
        "x": min_x, "y": min_y, "z": min_z,
        "w": max_x - min_x, "h": max_y - min_y, "d": max_z - min_z,
    }


def _declared_access_regions(mech: dict, parts: list) -> list:
    """Resolves every Enclosure sub-region across EVERY section of `mech`
    (not just one named section -- same "don't hardcode which sections to
    check, gate on the data" posture eo/mech_supports.py's own
    _joined_section_members() already documents for itself) into
    `(subsection_id, access_type, box)` triples, one per subsection.

    A subsection's declared `access_type` lives on its own anchor BOM
    part -- eo/mech_sections.py's own docstring fixes a subsection's
    `subsection_id` as always its anchor part's `part_id`, so this
    function looks up that same part_id in `parts` and reads an
    `access_type` field off it, mirroring the exact precedent eo/
    mech_supports.py's own _joined_section_members() already set for
    reading a member's "category"/"mount_spec" off its BOM part rather
    than off the placement itself. A part with no `access_type` field at
    all, or a value outside ACCESS_TYPES (e.g. a typo, or a part predating
    this phase), falls back to DEFAULT_ACCESS_TYPE ("fastened") -- same
    "never let a bad/missing field produce anything other than today's
    unchanged default behavior" posture Patch D.1's own ACCESS_TYPES
    docstring establishes.

    Returns a NEW list -- never mutates `mech` or `parts`, same read-only
    contract every other pure `_joined_*`/`_declared_*` helper in this
    tree already holds toward its own inputs.
    """
    parts_by_id = {
        p.get("id"): p for p in (parts or []) if isinstance(p, dict) and p.get("id")
    }

    regions = []
    for section in mech.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for subsection in subsections_for_section(mech, section):
            subsection_id = subsection.get("subsection_id")
            anchor_part = parts_by_id.get(subsection_id)
            access_type = anchor_part.get("access_type") if isinstance(anchor_part, dict) else None
            if access_type not in ACCESS_TYPES:
                access_type = DEFAULT_ACCESS_TYPE

            members = members_for_subsection(mech, subsection)
            box = _bounding_box([m for m in members if isinstance(m, dict)])
            regions.append((subsection_id, access_type, box))

    return regions


def apply_access_generation(mech: dict, parts: list) -> dict:
    """Patch D.5: wires generate_hinge()/generate_snap_latch()/
    generate_slide() into the pipeline. agents/hardware_speccer.py's own
    Level 3->4 call site runs this immediately after
    apply_supports_generation() -- same "act only on placements the
    validator has already signed off on" ordering that call already
    documents for apply_supports_generation() itself sitting after
    run_level_3_4_repair(): an access mechanism cut/mounted across a
    subsection's own bounding box isn't meaningful until that subsection's
    member positions are final, and apply_supports_generation() having
    already run means mech["supports"] (which Phase 5's future
    overlap-awareness pass will want alongside mech["access"]) is already
    populated by the time this runs too.

    For each subsection resolved by _declared_access_regions() above:
      - `access_type == "fastened"` (the default, whether explicitly set
        or because no `access_type` field/an invalid one was present) is
        skipped entirely -- no entry is added to the result at all, so a
        device with every part left at today's implicit default produces
        `mech["access"] == []`, byte-for-byte the same "no access
        mechanism generated" outcome as before this phase existed.
      - any other declared type dispatches to that type's own generator
        in `_GENERATORS` above, passing `{"section_id": subsection_id,
        **box}` as the generator's own `section` argument.

    Stashes the full result list on the new `mech["access"]` key --  a
    flat list, not nested under access_type or section_id, so a consumer
    (Phase 6's future manufacturability pass, MechView.jsx) can iterate
    every generated mechanism without first knowing which subsections
    declared one, mirroring eo/mech_cutouts.py's own flat `mech["cutouts"]`
    list shape rather than eo/mech_supports.py's `{"standoffs":
    [...], "bosses": [...]}` split -- there's no analogous "two kinds of
    primitive that need to combine/de-duplicate against each other" case
    here the way bosses-vs-standoffs has, so a flat list is the simpler
    correct shape.

    Returns `[]` (and stashes that same empty result onto `mech["access"]`)
    when `mech` has no sections yet -- same "nothing to derive from yet"
    no-op posture apply_supports_generation() already takes when
    `mech["sections"]` is empty/missing.
    """
    if not isinstance(mech, dict) or not mech.get("sections"):
        result = []
        if isinstance(mech, dict):
            mech["access"] = result
        return result

    result = []
    for subsection_id, access_type, box in _declared_access_regions(mech, parts):
        if access_type == DEFAULT_ACCESS_TYPE:
            continue

        generator = _GENERATORS.get(access_type)
        if generator is None:
            continue

        section_arg = {"section_id": subsection_id, **box}
        result.append(generator(section_arg))

    mech["access"] = result
    return result


if __name__ == "__main__":
    import json

    _demo_section = {
        "section_id": "battery_hatch",
        "x": 0, "y": 0, "z": 0,
        "w": 40, "h": 5, "d": 25,
    }

    print(json.dumps({
        "hinge": generate_hinge(_demo_section),
        "snap_latch": generate_snap_latch(_demo_section),
        "slide": generate_slide(_demo_section),
    }, indent=2))
