"""
eo/enclosure_spec.py — Phase 1, Patch 1.1 of the Mech/Enclosure
implementation guide: the single shared ENCLOSURE_SPEC config every
later enclosure-generation patch (1.2's compute_housing_footprint(),
1.3's apply_enclosure_generation(), Phase 2's standoff/screw-boss
sizing, Phase 5's cutout wall-thickness guard, Phase 6's
manufacturability checks) reads its numbers from, instead of each
patch hand-rolling or re-guessing its own constants the way
agents/hardware_speccer.py's SYSTEM_PROMPT currently does for housing/
lid dimensions (the exact gap Phase 1 exists to close -- see that
phase's own docstring in the implementation guide).

One dict, not scattered module-level constants: every later patch
imports ENCLOSURE_SPEC itself and reads a key off it
(`ENCLOSURE_SPEC["wall_thickness_mm"]`), so a single edit here -- e.g.
tuning wall_thickness_mm for a different printer/material -- propagates
everywhere consistently, rather than requiring the same edit to be
found and repeated across eo/mech_enclosure.py, eo/mech_supports.py,
eo/mech_cutouts.py, and eo/mech_manufacturability.py independently.

Deliberately zero logic in this module (per this patch's own sizing
note in the breakdown: "No logic, just the shared constants every
later patch reads from") -- pure data, importable with no side effects
and no dependency on any other eo/ module, same "config file with no
behavior" shape agents/component_dimension_table.py's own JSON data
file plays for G1a, just as a plain Python dict here since this table
is six fixed numbers, not a growable per-component dataset that
benefits from living outside the source tree.

Units are millimeters throughout, matching every other dimension field
already in this codebase (agents/hardware_speccer.py's own
"dimensions_mm", eo/mech_validator.py's "tolerance_mm") -- no field
here needs its own unit suffix beyond the dict-wide convention, but the
key names keep the "_mm"/"_deg" suffix anyway for the same
self-documenting reason those other modules' own field names do.
"""

# Defaults are chosen for FDM 3D printing (the eo/mech_validator.py /
# eo/mech_repair.py generate->validate->repair pipeline this module
# feeds ultimately hands off to FreeCAD, and agents/hardware_speccer.py's
# SYSTEM_PROMPT already assumes 3D-printed housing/lid parts) -- not
# injection molding or CNC, which would want different minimums and no
# draft-angle allowance at all on a single-part shell.
ENCLOSURE_SPEC = {
    # Housing/lid shell thickness. 2.0mm is the common "prints solid,
    # not brittle, in 2-3 perimeters on a 0.4mm nozzle" FDM default --
    # thin enough to stay lightweight, thick enough that
    # Phase 2's standoffs/screw bosses and Phase 5's cutouts (both cut
    # INTO this thickness) have real material to sit in without the
    # min_feature_mm guard below tripping on ordinary geometry.
    "wall_thickness_mm": 2.0,

    # Smallest wall/feature width the pipeline will accept anywhere
    # (Phase 5's minimum-wall-thickness guard checks cutout-adjacent
    # walls against exactly this number). Below ~1.2mm, FDM printers
    # commonly under-extrude, warp, or fail to bridge at all depending
    # on nozzle diameter and material -- this is a manufacturability
    # floor, not a stylistic minimum.
    "min_feature_mm": 1.2,

    # Internal clearance between a packed part's own footprint and the
    # housing's inner wall (Phase 1's housing_inner = device_footprint
    # expanded by clearance_mm only). Covers ordinary dimensional
    # tolerance stack-up (part dimension_confidence, print tolerance)
    # without being so generous the enclosure balloons past what's
    # actually packed inside it -- see Phase 1's own "no dead space
    # beyond wall_thickness + clearance" definition of done.
    "clearance_mm": 1.5,

    # Vertical wall draft, applied only in Phase 9's polish pass (fillet/
    # chamfer) -- deliberately NOT used by Phase 1-6's core shell/
    # standoff/cutout geometry, which are all straight-walled. Carried
    # here now (rather than added when Phase 9 lands) so every phase
    # reads from the same one ENCLOSURE_SPEC dict from day one, per
    # this module's own "one dict, not scattered constants" reasoning
    # above -- adding a key later would mean some patches importing an
    # ENCLOSURE_SPEC that doesn't have it yet.
    "draft_angle_deg": 1.5,

    # How far the lid overlaps into the housing's own opening (a
    # stepped lip, not a flush lid-on-top) -- keeps the lid registered
    # in x/y during assembly and gives Phase 6's manufacturability pass
    # a real feature to check for adequate contact area, rather than a
    # lid that can slide off-center before any fasteners are installed.
    "lid_lip_mm": 1.0,

    # Screw boss bore diameter for M3 heat-set inserts -- matches
    # agents/hardware_speccer.py's SYSTEM_PROMPT own worked example
    # ("M3 Heat-Set Insert and Screw") for the Enclosure section's
    # fastener parts, so Phase 2's screw-boss geometry and the BOM's
    # own fastener part stay sized for the same real hardware instead
    # of two independent guesses.
    "screw_boss_dia_mm": 4.5,
}
