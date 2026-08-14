"""
eo/mech_supports.py — Phase 2, Patch 2.2 of the Mech/Enclosure
implementation guide: the pure, deterministic standoff-projection
function that gives a mounted part real physical support inside the
shell Phase 1 already sized, instead of leaving it floating in a
hollow cavity (Master Guide, Phase 2 "Goal": "Parts stop floating
inside a hollow shell; anything meant to be mounted gets physical
support").

Same build-order reasoning every earlier "pure function first" patch in
this tree already established (eo/mech_enclosure.py's own
compute_housing_footprint() before apply_enclosure_generation(), eo/
mech_device.py's own plan_device_layout() before apply_device_merge()):
land the mechanical, side-effect-free projection logic on its own,
testable with plain dict inputs, before Patch 2.3 adds the bored
screw-boss variant and Patch 2.4 wires this into the pipeline's
mutate-in-place convention.

Input: `section_members`, a list of dicts each shaped like one
eo/mech_subsections.py members_for_subsection() placement dict --
{"part_id", "x", "y", "z", "w", "h", "d", ...} -- PLUS a "category" key
this module reads directly off each dict. Member dicts read straight
off `mech["placements"]` don't carry category on their own -- Patch
2.4's own apply_supports_generation() is what actually joins each
placement with its BOM part's category before calling this function;
this pure function never reads `parts` or does that join itself, same
"reads a shape, doesn't go fetch related data" boundary eo/
mech_enclosure.py's own compute_housing_footprint() already holds for
`device_footprint`.

Support-eligible categories: eo/enclosure_spec.py's own
SUPPORT_CATEGORIES (Patch 2.1) -- {"mcu", "power", "module"} in this
codebase's real category enum (see that module's own docstring on why
this differs from the Master Guide's literal {"mcu","battery","pcb",
"module"} wording). A member whose "category" isn't in that set is
skipped entirely -- this function never emits a standoff for it.

Projection rule (Master Guide, Phase 2 "Design" section):
    for each support-eligible member, project N=4 corner points (the
    plan-view corners of its own x/y/w/h footprint) down to the housing
    floor (z=0) and emit one standoff primitive at each -- same
    "read a footprint, emit a primitive at an offset" pattern
    eo/mech_device.py already uses for translation, just adding new
    geometry here instead of moving existing geometry.

Standoff height = the member's own "z" (so a standoff spans exactly
from the housing floor up to the part's own placed height, giving it a
rigid seat at z=0 rather than floating with nothing underneath) --
literal Master Guide wording: "Standoff height = part's own z (so its
base lands exactly at the part's placed height)." Standoff diameter
comes from ENCLOSURE_SPEC["standoff_dia_mm"] (Patch 2.1), never a
per-call guess.

Four corners, not fewer: Phase 2's own "Definition of done" requires
"every part with category in the support set has >=3 contact points to
the housing floor" -- 4 (one per rectangle corner) clears that with one
spare, and a rectangular footprint's four corners are also the simplest
points that are always well-defined regardless of the part's own w/h
(unlike, say, edge midpoints, which add no extra stability for a rigid
standoff post and are never used elsewhere in this tree).

No mounting-hole/bore logic in compute_standoffs() itself -- every
primitive IT returns is a solid post. compute_screw_bosses() below
(Patch 2.3) is the bored variant, kept in a separate function (not a
flag on compute_standoffs()) so a bug in bore geometry can never affect
compute_standoffs()'s already-verified solid-post output, mirroring
this same package's own Patch 1.2/1.3 split (pure geometry landed and
tested on its own before anything downstream depends on it).

Pure function: never mutates `section_members` or any member dict
inside it, never touches `mech`, never does I/O. Two calls with the
same input always return the same output -- same "idempotent by
construction" property this whole tree already holds itself to at
every pure-function layer (eo/mech_enclosure.py's own
compute_housing_footprint(), eo/mech_device.py's own
plan_device_layout()).
"""

from eo.enclosure_spec import ENCLOSURE_SPEC, SUPPORT_CATEGORIES

# Corner offsets (as (dx, dy) fractions of a member's own w/h) each
# standoff sits at -- fixed at the plan-view rectangle's four corners,
# see module docstring's "Four corners, not fewer" note. Order is
# stable (not sorted/randomized) so two calls over the same input always
# emit standoffs in the same sequence, matching this module's own
# "idempotent by construction" contract down to list ordering, not just
# set membership.
_CORNER_FRACTIONS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]

# The housing floor every standoff is projected down to -- literal
# Master Guide wording ("project ... down to the housing floor (z=0
# plane)"), same fixed-floor assumption agents/hardware_speccer.py's own
# SYSTEM_PROMPT already made for the housing's own placement before
# Patch 1.4 stopped asking the LLM to author it ("span the full
# enclosure footprint starting at z: 0").
_FLOOR_Z = 0.0


def _corner_primitives(member: dict, extra_fields: dict) -> list:
    """Shared corner-projection loop both compute_standoffs() and
    compute_screw_bosses() (Patch 2.3) reduce to -- the same four
    plan-view corners, projected down to the housing floor, differing
    only in which extra fields (a plain "diameter", or a boss's
    "diameter" + "bore_diameter") get attached to each primitive. Kept
    private and un-exported: neither public function's own contract
    changes shape depending on how the other is implemented, this is
    purely an internal de-duplication of the identical loop, not a new
    piece of this module's public surface.
    """
    part_id = member.get("part_id")
    x = float(member.get("x") or 0)
    y = float(member.get("y") or 0)
    z = float(member.get("z") or 0)
    w = float(member.get("w") or 0)
    h = float(member.get("h") or 0)

    primitives = []
    for corner_index, (fx, fy) in enumerate(_CORNER_FRACTIONS):
        primitive = {
            "part_id": part_id,
            "corner_index": corner_index,
            "x": round(x + fx * w, 3),
            "y": round(y + fy * h, 3),
            "z": _FLOOR_Z,
            "height": round(z - _FLOOR_Z, 3),
        }
        primitive.update(extra_fields)
        primitives.append(primitive)
    return primitives


def compute_standoffs(section_members: list) -> list:
    """Returns one standoff primitive dict per corner (4 per
    support-eligible member) --
    {"part_id", "corner_index", "x", "y", "z", "height", "diameter"} --
    for every member in `section_members` whose "category" is in
    SUPPORT_CATEGORIES. See module docstring for the full projection
    rule and the reasoning behind height/diameter.

    `part_id` on each returned primitive traces back to the member it
    supports (a support-eligible member emits 4 primitives sharing one
    `part_id`, distinguished by `corner_index` 0-3) -- Patch 2.4's own
    apply_supports_generation() needs this to stash results grouped by
    owning part, and Phase 6's future collision checker needs it to
    report which part's support a violation belongs to.

    A member missing x/y/z/w/h (defaults to 0 for each, same "tolerant
    of a partial dict" posture eo/mech_enclosure.py's own
    compute_housing_footprint() already takes toward a partial
    `device_footprint`) still produces 4 primitives, just degenerately
    stacked at one point -- this function never raises on incomplete
    input, same fail-safe posture every pure-planning function in this
    tree already holds itself to.

    Non-dict entries in `section_members`, and entries whose "category"
    isn't in SUPPORT_CATEGORIES (including entries with no "category"
    key at all), are silently skipped -- never crash, never
    default-eligible.

    Returns a solid post for EVERY support-eligible member, regardless
    of whether it also has declared mounting holes -- Patch 2.3's
    compute_screw_bosses() below is the separate, ADDITIONAL bored
    variant for the subset that does; this function's own output never
    changes shape based on that (see this module's own docstring on why
    the two are kept as separate functions rather than one with a
    bore-or-not flag).
    """
    diameter = ENCLOSURE_SPEC["standoff_dia_mm"]
    standoffs = []

    for member in section_members or []:
        if not isinstance(member, dict):
            continue
        if member.get("category") not in SUPPORT_CATEGORIES:
            continue
        standoffs.extend(_corner_primitives(member, {"diameter": diameter}))

    return standoffs


def compute_screw_bosses(section_members: list) -> list:
    """Patch 2.3: the bored variant of compute_standoffs() above, for
    parts that ADDITIONALLY have declared mounting holes -- Master
    Guide, Phase 2 "Design" step 4: "Screw bosses (for parts with
    mounting holes) reuse the same primitive with a bore added." Same
    eligibility gate as compute_standoffs() (member's "category" must be
    in SUPPORT_CATEGORIES) PLUS a non-empty "mount_spec" string on the
    member -- a member that clears the category gate but has no
    mount_spec gets a plain standoff from compute_standoffs() and
    nothing from this function; a member with neither gets nothing from
    either. Patch 2.4's own apply_supports_generation() is what decides
    how the two functions' outputs combine for a given member (e.g.
    preferring this function's bossed primitives over compute_standoffs()'s
    plain ones wherever both would apply) -- this function only computes
    the bossed geometry, it doesn't resolve that precedence itself.

    "Declared mounting holes" here means only that `member["mount_spec"]`
    is present and non-empty -- this function deliberately does NOT parse
    its grammar the way agents/hardware_speccer.py's own
    _parse_mount_spec()/_mount_hole_primitives() do (that machinery
    exists to render small cosmetic hole cylinders sized to a specific
    thread on the PART's own body, and lives in agents/ for exactly that
    reason -- this eo/ package never imports agents/, same boundary
    every other module in this tree already holds). A structural screw
    boss's own bore is always sized off ENCLOSURE_SPEC["screw_boss_dia_mm"]
    regardless of the part's actual thread size (literal Master Guide
    wording: "diameter from ENCLOSURE_SPEC['screw_boss_dia_mm']", not
    "per the part's own thread") -- so there is nothing to gain from
    parsing mount_spec's grammar here, only from checking it's declared
    at all. `member["mount_spec"]` itself is not this function's own
    invention: Patch 2.4's join step copies it over from the BOM part
    the same way it already copies over "category" for
    compute_standoffs() above.

    Each returned primitive has the same shape as compute_standoffs()'s
    own output, plus one extra key: "bore_diameter" (from
    ENCLOSURE_SPEC["screw_boss_dia_mm"]) alongside "diameter" (the
    boss's own OUTER diameter, from ENCLOSURE_SPEC["standoff_dia_mm"] --
    see that constant's own docstring in eo/enclosure_spec.py for why it
    was sized specifically to leave real wall material around this exact
    bore).
    """
    boss_diameter = ENCLOSURE_SPEC["standoff_dia_mm"]
    bore_diameter = ENCLOSURE_SPEC["screw_boss_dia_mm"]
    bosses = []

    for member in section_members or []:
        if not isinstance(member, dict):
            continue
        if member.get("category") not in SUPPORT_CATEGORIES:
            continue
        mount_spec = member.get("mount_spec")
        if not isinstance(mount_spec, str) or not mount_spec.strip():
            continue
        bosses.extend(_corner_primitives(
            member, {"diameter": boss_diameter, "bore_diameter": bore_diameter}
        ))

    return bosses
