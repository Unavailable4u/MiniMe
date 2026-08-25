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

    # Patch 2.1 (Phase 2, "Ribs / standoffs"): outer diameter of a plain
    # (borehole-free) standoff post -- distinct from screw_boss_dia_mm
    # above, which is the BORE diameter drilled INTO a boss for an M3
    # heat-set insert. A screw boss reuses this same post primitive with
    # that bore added (see Phase 2's own design section), so it needs
    # real wall material left over once the bore is cut out of it, not
    # just an outer diameter equal to the bore itself. Sized as
    # screw_boss_dia_mm (4.5) plus min_feature_mm (1.2) of solid wall on
    # each side of the bore -- 4.5 + 2*1.2 = 6.9 -- rounded up to 7.0 for
    # ordinary FDM print margin, same manufacturability-floor reasoning
    # min_feature_mm itself already documents above, not a fresh
    # independent guess.
    "standoff_dia_mm": 7.0,
}

# Patch 2.1: which BOM part categories get standoff/screw-boss support at
# all -- Phase 2's own design section iterates "every placed member whose
# category needs mechanical support" against exactly this set. The Master
# Guide's own worked example for this set is {"mcu", "battery", "pcb",
# "module"}, but "battery" and "pcb" are not real values of this
# codebase's actual part-category enum: agents/hardware_speccer.py's
# SYSTEM_PROMPT_PARTS (see its own "category" is one of: ..." line) and
# its _ELECTRICAL_CATEGORIES constant both fix that enum at exactly
# {"mcu", "sensor", "actuator", "power", "module"} -- a battery is always
# categorized "power" in this codebase, and there is no standalone "pcb"
# category at all (a bare PCB, if ever added as its own BOM line, would
# most likely land under "module" or "mcu" depending on what it carries).
# Using the guide's literal wording verbatim here would silently match
# nothing for the two categories that don't exist, so this set instead
# uses the real enum values covering the same intent -- "mcu" and "power"
# (batteries/battery holders) are the parts whose weight and mounting-hole
# footprint most need physical standoff support, and "module" (radios,
# breakout boards) commonly ships with its own mounting holes too.
# "sensor"/"actuator" are deliberately excluded -- per Phase 2's own
# design section, standoff support is for parts meant to be RIGIDLY
# mounted, not every electrical part generally; most sensors/actuators in
# a typical BOM are small enough to sit in a form-fit pocket or dangle on
# a wire lead instead.
SUPPORT_CATEGORIES = {"mcu", "power", "module"}

# ---------------------------------------------------------------------------
# Patch 5.1 (Phase 5, "Cutouts"): category -> cutout-type table + the
# coarse electrical-category pre-filter Phase 5's own nearest-exterior-
# face/generate_cutout patches (5.2/5.3/5.4) will gate on before ever
# consulting this table. Config only -- no logic, per this patch's own
# sizing note in the breakdown ("Config only"), same "pure data, zero
# behavior change" posture Patch 1.1/2.1 above already established for
# this same module.
#
# Keyed by KEYWORD, not by the BOM's own "category" field -- deliberate
# deviation from the Master Guide's literal "category -> cutout type"
# table wording, for the same reason Patch 2.1's own SUPPORT_CATEGORIES
# comment above already had to deviate from the guide's literal
# SUPPORT_CATEGORIES wording: the Guide's cutout rows ("display",
# "buzzer / mic", "button", "usb / power connector", "led / indicator")
# are not values of this codebase's real 7-value category enum
# (agents/hardware_speccer.py's own SYSTEM_PROMPT_PARTS "category" line:
# "mcu", "sensor", "actuator", "power", "module", "3D_PRINT", "MISC").
# A display and a plain analog sensor are BOTH just "sensor"; a button
# and a relay are BOTH "actuator"; an LED indicator and a full USB-power
# breakout are BOTH "module" or "power" depending on how it was BOM'd --
# the coarse enum alone genuinely cannot tell these apart, so category
# can only ever be Phase 5's pre-filter, never its dispatch key.
#
# This codebase already has real prior art for exactly this problem:
# agents/hardware_speccer.py's own _SHAPE_TO_PRIMITIVE (G1c) faces the
# identical "the categorical field I have isn't fine-grained enough"
# situation and solves it the same way -- keyword-matching against a
# free-text field (there, the curated-table's own "shape" string; here,
# a part's own "generic_name") rather than the blunt category enum.
# Patch 5.2/5.3 (not this patch) are what actually lowercase-substring-
# match a part's "generic_name" against this table's own keys -- this
# patch only supplies the keyword->descriptor data they'll match into,
# same "pure data first, matching logic in the patch that consumes it"
# split every other config-then-consumer patch pair in this tree already
# follows (Patch 1.1 before 1.2, Patch 2.1 before 2.2).
#
# "shape" below reuses the three-way vocabulary Phase 5's own design
# section already implies -- "rectangular" (display window),
# "circular" (vent/through-hole/light-pipe -- all round openings, same
# primitive, different diameter), and "port" (USB/power -- the one
# shape needing a connector envelope, not just a primitive hole, which
# is exactly why Patch 5.4 splits it into its own function rather than
# handling it inside Patch 5.3's simple-shape generator).
CUTOUT_TABLE = {
    # Rectangular window sized to the part's own footprint minus a
    # bezel margin -- literal Master Guide wording ("rectangular
    # window, sized to part footprint minus bezel margin"). 2.0mm
    # matches ENCLOSURE_SPEC["wall_thickness_mm"] itself -- a bezel
    # roughly one wall-thickness wide is enough to hide a display
    # module's own non-active border without eating meaningfully into
    # the visible area.
    "display": {
        "cutout_type": "window",
        "shape": "rectangular",
        "bezel_margin_mm": 2.0,
    },

    # Circular vent hole(s) -- literal Master Guide wording ("circular
    # vent hole(s), optionally with a mesh-clearance ring"). Buzzers
    # (louder, want more total open area) get more, smaller holes than
    # a single mic port; mesh_clearance_mm is the extra radius left
    # around the hole pattern for an adhesive mesh/foam dust screen,
    # 0 meaning "no ring, bare hole(s)" is a valid, common case this
    # patch still models explicitly rather than omitting the key.
    "buzzer": {
        "cutout_type": "vent",
        "shape": "circular",
        "hole_diameter_mm": 1.5,
        "hole_count": 4,
        "mesh_clearance_mm": 1.0,
    },
    "mic": {
        "cutout_type": "vent",
        "shape": "circular",
        "hole_diameter_mm": 1.2,
        "hole_count": 1,
        "mesh_clearance_mm": 0.5,
    },

    # Through-hole matching the actuator's own diameter -- literal
    # Master Guide wording ("through-hole matching actuator diameter").
    # No hole_diameter_mm of its own here: unlike vent/light-pipe holes
    # (which are always this table's own fixed size regardless of the
    # part), a button's cutout diameter is read off the PART's own
    # w/d footprint by Patch 5.3, not guessed here -- clearance_mm is
    # the only table-owned number, added to that part-reported diameter
    # so the actuator's shaft isn't a forced press-fit against the
    # housing wall.
    "button": {
        "cutout_type": "through_hole",
        "shape": "circular",
        "clearance_mm": 0.3,
    },

    # Port-shaped cutout at the part's own footprint -- literal Master
    # Guide wording ("port-shaped cutout at part's footprint"). Same
    # "read the real footprint, don't guess a size here" reasoning as
    # "button" above; clearance_mm is this table's only owned number,
    # sized more generously than button's own (0.3) since a USB/power
    # connector's mating plug needs to physically slide in and out
    # repeatedly, not just clear a static shaft.
    "usb": {
        "cutout_type": "port",
        "shape": "port",
        "clearance_mm": 0.4,
    },
    "power_connector": {
        "cutout_type": "port",
        "shape": "port",
        "clearance_mm": 0.4,
    },

    # Small circular light-pipe hole -- literal Master Guide wording
    # ("small circular light-pipe hole"). "led"/"indicator" are kept as
    # two separate keys (not aliased to one) because Patch 5.2/5.3's own
    # keyword match is a plain substring test against generic_name, and
    # a part BOM'd as e.g. "Status Indicator LED" should match on
    # whichever word actually appears -- two small literal entries here
    # cost nothing and remove any ordering-dependence from how that
    # substring match gets implemented downstream.
    "led": {
        "cutout_type": "light_pipe",
        "shape": "circular",
        "hole_diameter_mm": 3.0,
    },
    "indicator": {
        "cutout_type": "light_pipe",
        "shape": "circular",
        "hole_diameter_mm": 3.0,
    },
}

# ---------------------------------------------------------------------------
# Patch D.1 (Phase D, "Access mechanisms"): the access-type enum + per-type
# geometry constants every Phase D generator (eo/mech_access.py's
# generate_hinge()/generate_snap_latch()/generate_slide(), Patch D.2/D.3/D.4)
# reads its sizing numbers from -- same "config here, logic in the module
# that consumes it" split Patch 1.1/2.1/5.1 above already established for
# ENCLOSURE_SPEC/SUPPORT_CATEGORIES/CUTOUT_TABLE respectively.
#
# Per this guide's own stated assumption (access mechanisms are scoped
# PER-SECTION, not device-wide -- see the implementation guide's "Assumptions"
# section): a single device can mix a "fastened" main body with one
# "hinged"/"snap_latch"/"slide" sub-region (e.g. a slide-out battery hatch on
# an otherwise screwed-shut housing). "fastened" is, and remains, the default
# for any section that doesn't explicitly declare otherwise -- today's only
# behavior is completely unchanged unless a section opts into one of the
# other three.
ACCESS_TYPES = {"fastened", "hinged", "snap_latch", "slide"}
DEFAULT_ACCESS_TYPE = "fastened"

# Per-type geometry, one sub-dict per non-default ACCESS_TYPES member --
# "fastened" has no entry here because it's a no-op relative to today's
# existing screw-boss/standoff geometry (eo/mech_supports.py, Phase 2),
# not a new primitive this phase generates.
ACCESS_GEOMETRY = {
    # Knuckle/pin pair (Patch D.2's generate_hinge()). Alternating knuckles
    # on the housing/lid halves of the section boundary, joined by one
    # continuous pin -- the standard FDM-printable "print-in-place-clearance"
    # hinge, not a living hinge (this codebase's rigid pla_rigid default
    # material, Phase E, can't flex enough for a living hinge to survive
    # repeated cycles).
    "hinged": {
        # Outer diameter of each knuckle barrel. Sized well above
        # min_feature_mm so the knuckle wall around the pin bore stays
        # printable at typical FDM layer widths.
        "knuckle_dia_mm": 6.0,
        # Axial length of each individual knuckle segment.
        "knuckle_length_mm": 8.0,
        # Number of knuckle segments across the hinge span -- odd count so
        # the two halves alternate evenly (housing gets the two outer
        # knuckles, lid gets the middle one, or vice versa).
        "knuckle_count": 3,
        # Pin diameter -- sized to leave real wall material inside the
        # knuckle bore (knuckle_dia_mm minus 2x this, minus min_feature_mm
        # of margin, still clears min_feature_mm on each side).
        "pin_dia_mm": 2.5,
        # Radial clearance between the pin and its bore so the assembled
        # hinge actually rotates instead of binding as a single fused part.
        "pin_clearance_mm": 0.3,
    },

    # Cantilever hook + catch pair (Patch D.3's generate_snap_latch()).
    "snap_latch": {
        # Free length of the cantilever beam, base to hook tip.
        "cantilever_length_mm": 12.0,
        # Beam width (in-plane, perpendicular to the flex direction).
        "cantilever_width_mm": 4.0,
        # Beam thickness (the flexing dimension) -- thin enough to deflect
        # by hand on FDM-printed pla_rigid, thick enough not to snap on
        # the first cycle; same "printable and durable" balance
        # wall_thickness_mm strikes for the shell itself.
        "cantilever_thickness_mm": 1.5,
        # How far the hook overhangs past the catch's retaining edge --
        # the engagement depth that actually holds the latch closed.
        "catch_depth_mm": 1.2,
        # Lead-in overhang on the catch's engagement face, so the hook can
        # cam over it on insertion without needing to be pried open first.
        "catch_overhang_mm": 0.8,
    },

    # Channel + stop pair (Patch D.4's generate_slide()) -- e.g. a
    # slide-out battery hatch running in a printed rail.
    "slide": {
        # Radial clearance between the sliding part and its channel walls
        # -- same dimensional-tolerance-stack-up role clearance_mm plays
        # for a static part against the housing wall above, but slide
        # fit needs it to move smoothly, not once, so it's tracked as its
        # own number rather than reusing clearance_mm's static-fit value.
        "channel_clearance_mm": 0.4,
        # How deep the channel is cut into the housing wall.
        "channel_depth_mm": 2.5,
        # Length of the end-stop that keeps the slide from over-traveling
        # and coming free of its channel.
        "stop_length_mm": 3.0,
        # How far the stop projects into the channel -- must stay below
        # channel_depth_mm or it blocks the slide from ever seating.
        "stop_height_mm": 1.5,
    },
}

# Coarse pre-filter: only these five categories are ever wired/mounted
# electrical parts in the first place (3D_PRINT/MISC -- housing, lid,
# mounts, fasteners -- are purely mechanical and never a cutout
# candidate, same reasoning eo/mech_sections.py's own _CATEGORY_TO_SECTION
# already documents for why those two land in Enclosure, not a
# functional section). Duplicated here as a local constant, matching
# eo/mech_subsections.py's own documented precedent of never importing
# agents/hardware_speccer.py from this package (see that module's own
# "Dependency shape" docstring section) -- agents/hardware_speccer.py's
# own _ELECTRICAL_CATEGORIES is the same five values, this is not a new
# judgment call, just the same enum re-declared on this side of that
# boundary. Patch 5.2/5.3 apply this FIRST (cheap, exact-match category
# check) before ever running CUTOUT_TABLE's own keyword scan against a
# part's generic_name -- a 3D_PRINT/MISC part is never cutout-eligible
# regardless of what its generic_name happens to contain.
CUTOUT_ELIGIBLE_CATEGORIES = {"mcu", "sensor", "actuator", "power", "module"}

# ---------------------------------------------------------------------------
# Patch E.1 (Phase E, "Material awareness"): the material-property override
# table every later Phase E module reads from -- Patch E.2's own
# eo/mech_material.py resolve_material() (which part/archetype combination
# resolves to which material name), and Patch E.3's targeted edits to eo/
# mech_enclosure.py / eo/mech_cutouts.py (which ENCLOSURE_SPEC-shaped values
# a resolved material actually overrides). Same "config here, logic in the
# module/patch that consumes it" split Patch 1.1/2.1/5.1/D.1 above already
# established for ENCLOSURE_SPEC/SUPPORT_CATEGORIES/CUTOUT_TABLE/
# ACCESS_GEOMETRY respectively -- deliberately zero resolution logic in this
# table itself, same "pure data, matching/resolving logic lives in the patch
# that consumes it" posture Patch 5.1's own CUTOUT_TABLE docstring already
# spells out for itself.
#
# "pla_rigid" is, and remains, this codebase's implicit material today --
# every structural part up to this phase has been treated as one rigid,
# generic 3D-printable plastic (Part 1, item 6's own "not modeled yet" gap
# this phase closes), sized off ENCLOSURE_SPEC's own wall_thickness_mm/
# min_feature_mm directly. Rather than re-declaring those same two numbers
# a second time under a "pla_rigid" key (which would create exactly the
# "numbers drift out of sync between modules" failure mode ENCLOSURE_SPEC's
# own module docstring says this whole file exists to prevent), pla_rigid's
# own entry below is an EMPTY override dict: Patch E.3's material-aware
# lookup falls through to ENCLOSURE_SPEC's own baseline value for any key a
# material doesn't explicitly override, so pla_rigid parts stay numerically
# identical to today's un-overridden ENCLOSURE_SPEC reads, by construction,
# not by two tables happening to agree.
DEFAULT_MATERIAL = "pla_rigid"

# Flex behavior is a material property this phase newly introduces --
# ENCLOSURE_SPEC has no corresponding baseline key of its own for it to
# "fall through" to the way wall_thickness_mm/min_feature_mm can, so it
# gets its own explicit default here instead, read by any material entry
# (like pla_rigid's empty dict below) that doesn't override it.
DEFAULT_FLEX_BEHAVIOR = "rigid"

# Keyed on material name. Each value is a PARTIAL override dict -- only the
# ENCLOSURE_SPEC keys (plus the new "flex_behavior" key, which has no
# ENCLOSURE_SPEC counterpart) a given material actually changes, per this
# patch's own "overriding relevant ENCLOSURE_SPEC values (min wall
# thickness, flex behavior) per material" wording -- not a full restated
# copy of every ENCLOSURE_SPEC key for every material.
MATERIAL_PROPERTIES = {
    # The rigid FDM default every other phase already assumes (see this
    # table's own docstring above) -- no overrides.
    "pla_rigid": {},

    # Strap/band material (Patch E.2 resolves this specifically for
    # wearable strap-category parts -- see that patch's own docstring).
    # Genuinely different print/behavior profile from pla_rigid, not a
    # thinner version of the same rigid part:
    "tpu_flexible": {
        # A flexible strap needs to actually bend repeatedly in normal
        # use, which a wall sized for pla_rigid's own rigid-shell
        # thickness (2.0mm, ENCLOSURE_SPEC["wall_thickness_mm"]) would
        # resist rather than flex -- thinned down to a cross-section that
        # bends comfortably by hand while still printing as one solid
        # wall (not so thin it falls below FDM's own reliable minimum
        # extrusion width for a flexible filament).
        "wall_thickness_mm": 1.2,
        # min_feature_mm is unchanged from ENCLOSURE_SPEC's own rigid-
        # material floor (1.2mm) -- that number is a print-process
        # limit (nozzle/under-extrusion), not a material-stiffness one,
        # so TPU doesn't get its own separate value here; only keys this
        # material genuinely changes appear in its override dict, per
        # this table's own "partial override" convention above.
        "flex_behavior": "flexible",
    },
}


# ---------------------------------------------------------------------------
# Patch G.1 (Phase G, Mech View standalone implementation guide): the
# single global build-plate size every future Phase G module reads from --
# Patch G.2's own eo/mech_dfm.py check_bed_fit() (which final housing/
# baseplate dimensions get compared against) and Patch G.3's own auto-split
# logic (which decides whether a split is even needed). Same "config here,
# logic in the module/patch that consumes it" split Patch 1.1/2.1/5.1/D.1/
# E.1's own tables above already establish for ENCLOSURE_SPEC/
# SUPPORT_CATEGORIES/CUTOUT_TABLE/ACCESS_GEOMETRY/MATERIAL_PROPERTIES
# respectively -- deliberately zero fit-checking logic in this constant
# itself.
#
# A typical consumer FDM printer's own usable bed area (e.g. a Bambu Lab
# A1/P1S- or Prusa MK4-class 220x220mm bed, 250mm of vertical Z clearance)
# -- literal Patch G.1 wording ("a typical consumer-printer default").
# Deliberately NOT per-project configurable, per this guide's own stated
# preference for simplicity here ("not per-project configurable, per this
# guide's stated preference for simplicity here") -- a real project
# targeting a genuinely different printer is a future config surface, not
# something this phase's own bed-fit check needs to solve on its first
# pass.
PRINT_BED_MM = {"x": 220, "y": 220, "z": 250}


