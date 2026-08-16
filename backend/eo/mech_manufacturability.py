"""
eo/mech_manufacturability.py — Phase 6, Patch 6.1 of the Mech/Enclosure
implementation guide: the first landed check of the manufacturability
report (Master Guide, Phase 6 "Goal": "Catch bad geometry before it
reaches FreeCAD/STL export, with readable warnings instead of silent
bad output").

Same build-order reasoning every earlier phase in this tree already
established (eo/mech_enclosure.py's compute_housing_footprint() before
apply_enclosure_generation(); eo/mech_cutouts.py's
check_min_wall_thickness() (Patch 5.5) landed as its own pure function
before apply_cutout_generation() (Patch 5.6) wired the whole pipeline
together): Phase 6's own Design step 1 lists four separate checks --

    - Minimum wall thickness violated anywhere?
    - Any unsupported overhang beyond draft_angle_deg?
    - Any screw boss/standoff closer than min_feature_mm to a wall or
      cutout?
    - Any cutout overlapping a standoff or another cutout?

-- and this patch lands exactly ONE of them as a standalone, pure,
testable function, not all four at once and not yet wired into
agents/hardware_speccer.py's own G3g call chain. Landing order picked by
which check already has real geometry to run against:

    - The MINIMUM WALL THICKNESS check already exists --
      eo/mech_cutouts.py's own check_min_wall_thickness() (Patch 5.5)
      already runs per-cutout, whenever generate_cutout()/
      generate_port_cutout() are called with `housing_inner`, and its
      result already rides along on each cutout dict's own
      "wall_thickness_check" key. Nothing new to compute here; Patch
      6.2 (aggregation, see below) just needs to collect those existing
      per-cutout results into one report.
    - OVERHANG BEYOND draft_angle_deg has NO real geometry to check
      against yet, anywhere in this tree. eo/enclosure_spec.py's own
      ENCLOSURE_SPEC["draft_angle_deg"] docstring is explicit that
      draft/chamfer is "applied only in Phase 9's polish pass... NOT
      used by Phase 1-6's core shell/standoff/cutout geometry, which
      are all straight-walled." A straight-walled shell, straight
      vertical standoff posts, and through-hole cutouts have no drafted
      surface for "unsupported beyond N degrees" to mean anything about
      -- so this bullet is deliberately deferred to whichever patch
      lands alongside Phase 9's actual drafted geometry, not faked here
      against geometry that doesn't exist. A later Phase 6 patch may
      still land a no-op placeholder for report-shape completeness, but
      that's an explicit later decision, not something this patch
      invents a fake check for.
    - SCREW BOSS/STANDOFF TOO CLOSE TO A WALL is the one this patch
      lands: eo/mech_supports.py's own compute_standoffs()/
      compute_screw_bosses() (Patch 2.2/2.3) already emit real
      {"x","y","diameter",...} primitives, and eo/mech_enclosure.py's
      own compute_housing_footprint() (Patch 1.2) already emits a real
      `housing_inner` box -- both already fully populated by Phases 1/2
      with no re-joining or re-deriving needed, the same "read two
      already-computed structures, don't invent a third" posture every
      pure function in this tree already holds itself to. See
      check_standoff_wall_clearance() below.
    - SCREW BOSS/STANDOFF TOO CLOSE TO A CUTOUT, and CUTOUT OVERLAPPING
      A STANDOFF OR ANOTHER CUTOUT, both need a cutout's own real x/y
      plan-view position -- which mech["cutouts"] entries don't carry
      directly (only "part_id"/"face" plus sizing fields; Patch 5.3/5.4
      center each cutout within its owning part's own footprint, so
      recovering a real x/y needs rejoining against mech["placements"]
      the same way eo/mech_supports.py's own _joined_section_members()
      already does for standoffs). Deferred to a later Phase 6 patch
      that lands that join, rather than folded into this one.

Patch 6.2 landed the report-shape aggregator, build_manufacturability_report()
-- {"passed": bool, "violations": [...]} across every check this module
held at the time (6.1's own check_standoff_wall_clearance(), run fresh
per mech["supports"] entry, plus the wall_thickness_check results eo/
mech_cutouts.py's own Patch 5.5 already computed per mech["cutouts"]
entry, merely collected there, not recomputed).

Patch 6.3 (this patch) lands the two checklist items 6.1 explicitly
deferred: "screw boss/standoff closer than min_feature_mm to ... a
cutout" and "cutout overlapping a standoff or another cutout." Both
needed a cutout's own real plan-view position, which mech["cutouts"]
entries still don't carry directly -- this patch lands the deferred
join (_footprint_by_part_id(), reusing the cutout's OWNING PART's own
placement footprint as a conservative stand-in for the cutout's own
smaller opening -- see check_standoff_cutout_clearance()'s own
docstring for why that approximation is safe) and the two new pure
checks that consume it (check_standoff_cutout_clearance(),
check_cutout_overlap()), wired together by check_feature_collisions()
and folded into build_manufacturability_report()'s own violation list.

Patch 6.4 lands the last piece: wiring build_manufacturability_report()
into agents/hardware_speccer.py's own G3g call chain (run last, right
after apply_cutout_generation(), once mech["housing"]/mech["supports"]/
mech["cutouts"] are ALL populated) and stashing its result onto
mech["manufacturability"] -- which reaches the workspace-facts "custom"
handoff dict for free, since custom["mech"] = spec.get("mech", {})
already carries the whole mech dict (housing/supports/cutouts included)
into that handoff surface. NOT agents/handoff_packager.py, despite this
module's own earlier drafts assuming that name -- that module is the
unrelated plan-domain PRD-to-coding-domain handoff (Part 5 §5.6), never
touched by (or aware of) the mech/hardware pipeline at all.

Still no OVERHANG/draft-angle check -- deliberately deferred to Phase
9, per the reasoning above (no drafted geometry exists yet anywhere in
this tree to check an angle against); Phase 6 is otherwise complete.
"""

import math

from eo.enclosure_spec import ENCLOSURE_SPEC
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection


def check_standoff_wall_clearance(support: dict, housing_inner: dict) -> dict:
    """Checks whether `support` (one standoff or screw-boss primitive
    dict, as returned by eo/mech_supports.py's own compute_standoffs()/
    compute_screw_bosses() -- {"part_id","corner_index","x","y","z",
    "height","diameter"[,"bore_diameter"]}) leaves a plan-view wall
    segment thinner than ENCLOSURE_SPEC["min_feature_mm"] between its
    own outer edge and `housing_inner`'s own x/y boundary -- literal
    Master Guide wording: "Any screw boss/standoff closer than
    min_feature_mm to a wall."

    Only the two PLAN-VIEW axes (x, y) are checked, against
    `housing_inner`'s own "x"/"y"/"w"/"h" -- a standoff/boss is a
    vertical post standing on the housing floor (z=0, per
    eo/mech_supports.py's own _FLOOR_Z), so "a wall" here always means
    one of the housing's four vertical side walls, never the floor or
    (open) ceiling a lid later closes over; there is no z-axis wall to
    measure a post's height against.

    Margin math, per axis, mirrors eo/mech_cutouts.py's own
    check_min_wall_thickness() shape one level up (a post's OWN radius
    stands in for that function's "half of any bezel/clearance slack"
    term, since a post's x/y already IS its own center by construction
    -- see eo/mech_supports.py's own _corner_primitives(), which places
    each primitive's "x"/"y" directly at one plan-view corner of the
    member it supports, not at a separately-tracked center):
      - low-side margin  = (post_x - radius) - housing_inner_x
      - high-side margin = (housing_inner_x + housing_inner_w) -
                            (post_x + radius)
      -- and the same shape again for y/h.

    Returns {"ok": bool, "margins": {"x": {"low": mm, "high": mm},
    "y": {"low": mm, "high": mm}}, "violations": [{"axis": "x"|"y",
    "side": "low"|"high", "margin_mm": mm}, ...]}. `"ok"` is `True` iff
    `"violations"` is empty -- same "flag, never raise" contract
    check_min_wall_thickness() itself documents, for the identical
    reason: a caller (Patch 6.2's future aggregator) decides what to do
    with a violation, this function only measures and reports one.

    Missing "x"/"y"/"diameter" on `support`, or missing "x"/"y"/"w"/"h"
    on `housing_inner`, default to 0 -- same "tolerant of a partial
    dict, never raises" posture every pure function in this tree already
    holds itself to (see e.g. eo/mech_enclosure.py's own
    compute_housing_footprint() docstring). A `support` with no
    "diameter" at all degrades to a zero-radius point-clearance check
    rather than crashing.

    Pure function: never mutates `support` or `housing_inner`.
    """
    min_feature = ENCLOSURE_SPEC["min_feature_mm"]
    radius = float((support or {}).get("diameter") or 0) / 2.0

    post_x = float((support or {}).get("x") or 0)
    post_y = float((support or {}).get("y") or 0)
    inner_x = float((housing_inner or {}).get("x") or 0)
    inner_y = float((housing_inner or {}).get("y") or 0)
    inner_w = float((housing_inner or {}).get("w") or 0)
    inner_h = float((housing_inner or {}).get("h") or 0)

    margins = {}
    violations = []
    for axis, post_pos, inner_pos, inner_extent in (
        ("x", post_x, inner_x, inner_w),
        ("y", post_y, inner_y, inner_h),
    ):
        low_margin = round((post_pos - radius) - inner_pos, 3)
        high_margin = round((inner_pos + inner_extent) - (post_pos + radius), 3)
        margins[axis] = {"low": low_margin, "high": high_margin}

        if low_margin < min_feature:
            violations.append({"axis": axis, "side": "low", "margin_mm": low_margin})
        if high_margin < min_feature:
            violations.append({"axis": axis, "side": "high", "margin_mm": high_margin})

    return {"ok": not violations, "margins": margins, "violations": violations}


def _footprint_by_part_id(mech: dict) -> dict:
    """Patch 6.3 helper: {part_id: member_dict} across EVERY section of
    `mech`, same two-hop section->subsection->member resolution every
    other _joined_*() helper in this tree already uses (eo/
    mech_supports.py's own _joined_section_members(), eo/mech_cutouts.py's
    own _joined_cutout_members()) -- but WITHOUT joining against `parts`
    (the BOM list), since this module only ever needs a member's own raw
    geometry ("x"/"y"/"z"/"w"/"h"/"d"), never its "category"/"generic_name"
    (those two functions' own reason for needing the BOM join at all).
    `members_for_subsection()` already returns that geometry directly off
    `mech["placements"]`, so no `parts` argument is needed here -- this
    keeps build_manufacturability_report()'s own signature at just
    `(mech)`, matching Patch 6.1/6.2's already-shipped shape.

    Last member wins on a duplicate part_id (should not happen in
    practice -- every placement traces back to exactly one part -- but
    never assumed or raised on). Returns `{}` for a `mech` with no
    sections yet, never raises.

    Pure function: never mutates `mech`.
    """
    footprints = {}
    for section in (mech or {}).get("sections") or []:
        if not isinstance(section, dict):
            continue
        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                part_id = member.get("part_id")
                if part_id is None:
                    continue
                footprints[part_id] = member
    return footprints


def check_standoff_cutout_clearance(support: dict, cutout_footprint: dict) -> dict:
    """Patch 6.3: the "screw boss/standoff closer than min_feature_mm to
    a ... cutout" half of Phase 6's own checklist that Patch 6.1 left
    for later (its own docstring: "Deferred to a later Phase 6 patch
    that lands that join"). `cutout_footprint` is the CUTOUT-OWNING
    PART's own plan-view rectangle ({"x","y","w","h",...}, as returned
    by `_footprint_by_part_id()` above) -- not the cutout's own
    (usually smaller) drilled opening, which `mech["cutouts"]` entries
    don't carry a real x/y for at all (see Patch 6.1's own docstring on
    why). Using the owning part's full footprint instead is a
    deliberate, documented conservative approximation: it can never
    UNDER-flag a genuine standoff/cutout collision, at the cost of
    occasionally flagging a standoff that is near the PART but not
    actually near where that part's own opening was drilled.

    Measures plain circle-to-rectangle clearance: the shortest distance
    from `support`'s own center (x, y) to the nearest point on
    `cutout_footprint`'s own rectangle, minus `support`'s own radius
    (from "diameter") -- zero or negative when the post's own body
    already overlaps the rectangle, same "measure the post's own EDGE,
    not its center" posture check_standoff_wall_clearance() above
    already holds itself to.

    Returns {"ok": bool, "gap_mm": mm, "violations": [{"gap_mm": mm}]}
    (violations is `[]` when ok). Missing "x"/"y"/"diameter" on
    `support`, or missing "x"/"y"/"w"/"h" on `cutout_footprint`, default
    to 0 -- same "tolerant of a partial dict" posture every pure
    function in this module already holds itself to.

    Pure function: never mutates `support` or `cutout_footprint`.
    """
    min_feature = ENCLOSURE_SPEC["min_feature_mm"]
    radius = float((support or {}).get("diameter") or 0) / 2.0
    post_x = float((support or {}).get("x") or 0)
    post_y = float((support or {}).get("y") or 0)

    rect_x = float((cutout_footprint or {}).get("x") or 0)
    rect_y = float((cutout_footprint or {}).get("y") or 0)
    rect_w = float((cutout_footprint or {}).get("w") or 0)
    rect_h = float((cutout_footprint or {}).get("h") or 0)

    closest_x = min(max(post_x, rect_x), rect_x + rect_w)
    closest_y = min(max(post_y, rect_y), rect_y + rect_h)
    center_distance = math.hypot(post_x - closest_x, post_y - closest_y)
    gap_mm = round(center_distance - radius, 3)

    violations = [{"gap_mm": gap_mm}] if gap_mm < min_feature else []
    return {"ok": not violations, "gap_mm": gap_mm, "violations": violations}


def check_cutout_overlap(footprint_a: dict, footprint_b: dict) -> dict:
    """Patch 6.3: the "cutout overlapping ... another cutout" half of
    Phase 6's own checklist. Same conservative-approximation posture as
    check_standoff_cutout_clearance() above: compares the two CUTOUT-
    OWNING PARTS' own plan-view rectangles, not their (usually smaller)
    drilled openings. Callers (see check_feature_collisions() below)
    are expected to only compare cutouts on the SAME housing face --
    two openings on different walls are never physically overlapping
    regardless of their in-plane x/y, so this function itself takes no
    "face" argument and trusts its caller has already filtered to a
    coplanar pair.

    Axis-aligned-rectangle overlap: `overlap_x`/`overlap_y` are each
    positive exactly when the two rectangles overlap on that axis, and
    a true 2D overlap is `overlap_x > 0 and overlap_y > 0`
    simultaneously (reported as `{"overlap_mm": mm}`, the smaller of
    the two axis overlaps). When the rectangles don't overlap, the
    exact minimum edge-to-edge distance between two axis-aligned boxes
    is `hypot(gap_x, gap_y)` where each `gap_*` is the positive
    separation on that axis (0 when they still overlap on that one
    axis) -- standard AABB-to-AABB distance, flagging
    `{"gap_mm": mm}` when that distance is under
    `ENCLOSURE_SPEC["min_feature_mm"]`.

    Returns {"ok": bool, "violations": [...]} (0 or 1 entries -- either
    an "overlap_mm" violation or a "gap_mm" violation, never both).
    Missing "x"/"y"/"w"/"h" on either footprint default to 0, same
    "tolerant of a partial dict" posture this whole module holds
    itself to.

    Pure function: never mutates either input.
    """
    min_feature = ENCLOSURE_SPEC["min_feature_mm"]

    ax = float((footprint_a or {}).get("x") or 0)
    ay = float((footprint_a or {}).get("y") or 0)
    aw = float((footprint_a or {}).get("w") or 0)
    ah = float((footprint_a or {}).get("h") or 0)
    bx = float((footprint_b or {}).get("x") or 0)
    by = float((footprint_b or {}).get("y") or 0)
    bw = float((footprint_b or {}).get("w") or 0)
    bh = float((footprint_b or {}).get("h") or 0)

    overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
    overlap_y = min(ay + ah, by + bh) - max(ay, by)

    violations = []
    if overlap_x > 0 and overlap_y > 0:
        violations.append({"overlap_mm": round(min(overlap_x, overlap_y), 3)})
    else:
        gap_x = max(0.0, -overlap_x)
        gap_y = max(0.0, -overlap_y)
        gap_mm = round(math.hypot(gap_x, gap_y), 3)
        if gap_mm < min_feature:
            violations.append({"gap_mm": gap_mm})

    return {"ok": not violations, "violations": violations}


def check_feature_collisions(mech: dict) -> list:
    """Patch 6.3: wires check_standoff_cutout_clearance()/
    check_cutout_overlap() above into `mech` -- the pipeline-facing
    "does the data I already have collide with itself" pass Patch 6.1's
    own docstring deferred pending "a later Phase 6 patch that lands
    [the placements] join." That join is `_footprint_by_part_id()`
    above; this function is the one that actually consumes it.

    Two passes:
      1. STANDOFF/BOSS vs CUTOUT: every primitive in
         `mech["supports"]["standoffs"]`/`["bosses"]` against every
         entry in `mech["cutouts"]` (via that cutout's own owning-part
         footprint) -- skipping a support/cutout pair that share the
         same `part_id` (a part's own standoff sitting directly under
         its own cutout opening is expected geometry, not a collision
         between two DIFFERENT features).
      2. CUTOUT vs CUTOUT: every pair of `mech["cutouts"]` entries that
         share the same `"face"` (see check_cutout_overlap()'s own
         docstring for why cross-face pairs are never compared),
         excluding a part paired against itself.

    A cutout whose own `part_id` has no resolvable footprint (missing
    from `_footprint_by_part_id(mech)`'s own output -- should not
    happen in practice, but never assumed) is skipped entirely from
    both passes rather than defaulting to a zero-sized footprint, which
    would falsely report every such cutout as colliding with
    everything near the origin.

    Returns a flat list of violation dicts, each tagged `"check"`
    ("standoff_cutout_clearance" or "cutout_overlap") plus the
    part_id(s) involved -- same flat, mixed, attributable-back shape
    build_manufacturability_report() already established for Patch
    6.1/6.2's own violations. Returns `[]` (never raises) for a `mech`
    with no cutouts, no supports, or neither.

    Pure function: never mutates `mech`.
    """
    violations = []
    if not isinstance(mech, dict):
        return violations

    cutouts = [c for c in (mech.get("cutouts") or []) if isinstance(c, dict)]
    if not cutouts:
        return violations

    footprints = _footprint_by_part_id(mech)

    supports = mech.get("supports")
    if isinstance(supports, dict):
        for group in ("standoffs", "bosses"):
            for support in supports.get(group) or []:
                if not isinstance(support, dict):
                    continue
                support_part_id = support.get("part_id")
                for cutout in cutouts:
                    cutout_part_id = cutout.get("part_id")
                    if cutout_part_id == support_part_id:
                        continue
                    footprint = footprints.get(cutout_part_id)
                    if not isinstance(footprint, dict):
                        continue
                    check = check_standoff_cutout_clearance(support, footprint)
                    for violation in check["violations"]:
                        violations.append({
                            "check": "standoff_cutout_clearance",
                            "standoff_part_id": support_part_id,
                            "cutout_part_id": cutout_part_id,
                            **violation,
                        })

    comparable = [c for c in cutouts if isinstance(footprints.get(c.get("part_id")), dict)]
    for i in range(len(comparable)):
        for j in range(i + 1, len(comparable)):
            cutout_a, cutout_b = comparable[i], comparable[j]
            part_a, part_b = cutout_a.get("part_id"), cutout_b.get("part_id")
            if part_a == part_b or cutout_a.get("face") != cutout_b.get("face"):
                continue
            check = check_cutout_overlap(footprints[part_a], footprints[part_b])
            for violation in check["violations"]:
                violations.append({
                    "check": "cutout_overlap",
                    "part_id_a": part_a,
                    "part_id_b": part_b,
                    "face": cutout_a.get("face"),
                    **violation,
                })

    return violations


def build_manufacturability_report(mech: dict) -> dict:
    """Patch 6.2: the report-shape aggregator this module's own Patch 6.1
    docstring already promised -- {"passed": bool, "violations": [...]}
    across every check this module ends up holding, per Phase 6's own
    Design step 2. Deliberately does NOT invent any new geometry
    checking here; it only RUNS/COLLECTS checks whose real geometry
    already exists elsewhere in `mech`, same distinction Patch 6.1's own
    docstring draws between "the one this patch lands" and "no real
    geometry to check against yet, anywhere in this tree":

      - STANDOFF/BOSS WALL CLEARANCE: runs check_standoff_wall_clearance()
        (Patch 6.1) fresh, once per primitive in `mech["supports"]`
        (both the "standoffs" and "bosses" lists eo/mech_supports.py's
        own apply_supports_generation() (Patch 2.4) already populates)
        against `mech["housing"]["inner"]` (Phase 1's own
        compute_housing_footprint() output) -- this check has never
        been run before this patch, since Patch 6.1 only landed the
        pure function, not a call site.
      - CUTOUT MINIMUM WALL THICKNESS: does NOT recompute anything --
        collects the "wall_thickness_check" result already riding along
        on each `mech["cutouts"]` entry (Patch 5.5, run inline by
        eo/mech_cutouts.py's own generate_cutout()/generate_port_cutout()
        at cutout-generation time), exactly per Patch 6.1's own
        docstring: "Patch 6.2 (aggregation, see below) just needs to
        collect those existing per-cutout results into one report."
      - FEATURE COLLISIONS (Patch 6.3, folded in here rather than a
        third revision of this docstring): runs
        check_feature_collisions() -- standoff/boss-vs-cutout clearance
        and cutout-vs-cutout overlap, the two checklist items Patch 6.1
        itself deferred pending the placements join Patch 6.3 lands.

    Each violation entry is the underlying check's own per-axis
    violation dict ({"axis"/"side"/"margin_mm"} for a standoff, or
    whatever shape check_min_wall_thickness() already returns for a
    cutout) with two extra keys merged in so a flat, mixed list stays
    attributable back to what produced it: `"check"` (one of
    "standoff_wall_clearance" / "cutout_min_wall_thickness") and
    `"part_id"` (the owning support's or cutout's own "part_id", not
    re-derived). Missing/unpopulated `mech["housing"]["inner"]`,
    `mech["supports"]`, or `mech["cutouts"]` are each treated as "no
    checks to run for that category" rather than an error -- same
    "nothing to derive from yet is a no-op, not a failure" posture
    apply_supports_generation()/apply_cutout_generation() themselves
    already hold toward their own missing inputs, not something this
    aggregator invents freshly for itself.

    `"passed"` is `True` iff `"violations"` is empty -- same "ok iff no
    violations" contract every pure check in this tree already holds,
    named "passed" instead of "ok" here only because this is the
    report level (Phase 6 "Design" step 2's own field name), not one
    more individual check.

    Never raises: a malformed/missing `mech`, or malformed entries
    inside its "supports"/"cutouts" lists, are skipped rather than
    crashing the whole report -- one bad entry should not hide every
    other real violation from view.

    Pure function: never mutates `mech`.
    """
    violations = []

    housing = (mech or {}).get("housing") if isinstance(mech, dict) else None
    housing_inner = housing.get("inner") if isinstance(housing, dict) else None

    if isinstance(housing_inner, dict):
        supports = mech.get("supports") if isinstance(mech, dict) else None
        if isinstance(supports, dict):
            for group in ("standoffs", "bosses"):
                for support in supports.get(group) or []:
                    if not isinstance(support, dict):
                        continue
                    check = check_standoff_wall_clearance(support, housing_inner)
                    for violation in check["violations"]:
                        violations.append({
                            "check": "standoff_wall_clearance",
                            "part_id": support.get("part_id"),
                            **violation,
                        })

    cutouts = mech.get("cutouts") if isinstance(mech, dict) else None
    for cutout in cutouts or []:
        if not isinstance(cutout, dict):
            continue
        check = cutout.get("wall_thickness_check")
        if not isinstance(check, dict):
            continue
        for violation in check.get("violations") or []:
            violations.append({
                "check": "cutout_min_wall_thickness",
                "part_id": cutout.get("part_id"),
                **violation,
            })

    violations.extend(check_feature_collisions(mech))

    return {"passed": not violations, "violations": violations}
