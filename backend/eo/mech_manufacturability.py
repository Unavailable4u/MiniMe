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

Patch 6.2 (not this patch) is expected to add the report-shape
aggregator -- {"passed": bool, "violations": [...]} across every check
this module ends up holding, per Phase 6's own Design step 2 -- and
Patch 6.3 the agents/hardware_speccer.py wiring + handoff-package
surfacing, mirroring Phase 5's own 5.2..5.5 (pure checks) -> 5.6
(wiring) shape.
"""

from eo.enclosure_spec import ENCLOSURE_SPEC


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
