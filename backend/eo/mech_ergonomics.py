"""
eo/mech_ergonomics.py — Phase H, Patch H.1 of the Mech View standalone
implementation guide: the canned-preset table Patch H.2 reads to apply
ergonomic constraints on top of the otherwise-unchanged housing
derivation logic Phase 1/E already establish — Part 1's own gap #8
("No ergonomic/human-contact modeling. Nothing accounts for hand size,
grip, or strap paths for handheld or wearable devices").

Per this guide's own stated Part 1 assumption ("Ergonomics (Phase H)
uses canned presets — a small fixed set of parametric rules per device
class — handheld grip dimensions, wearable wrist curvature — rather
than true anthropometric modeling, which is a substantially larger
undertaking than anything else in this guide"): this is deliberately a
small, hand-picked table of two entries, not a growable per-project
anthropometric model. Same "curated dict, not an algorithm" shape eo/
enclosure_spec.py's own MATERIAL_PROPERTIES (Patch E.1) already
establishes for the identical reason — a handful of well-known
constants, not something Patch H.2's own consumer needs to compute.

Keyed on `mobility_type` (one of Phase A's own six
classify_archetype()/resolve_ambiguous_archetype() values — see eo/
device_archetype.py's own module docstring), not `enclosure_mode`:
grip/strap ergonomics are about how a HUMAN interacts with the device,
which is exactly what `mobility_type` (not `enclosure_mode`) already
captures. In practice only `handheld` and `wearable` ever resolve to a
`full` enclosure_mode with a real housing/lid pair to apply these
constraints to (see _GROUPS in eo/device_archetype.py) — the two
entries below are exactly, and only, those two.

  - "handheld": `min_grip_w_mm` / `min_grip_d_mm` — the minimum
    housing outer "w" (x-span) / "d" (z-span, thickness) a hand can
    comfortably wrap around, per this guide's own literal "minimum
    grip width/depth range" wording. Deliberately does NOT constrain
    "h" (y-span) — a handheld device's length (button layout, screen)
    varies far more between projects than the width/thickness a palm
    actually grips, so only the two grip-relevant axes get a floor.
    `fillet_radius_mm` — this guide's own literal "mandatory fillet
    radius" — a hard 90-degree housing edge is uncomfortable to grip;
    every handheld housing gets this fillet applied, not just an
    undersized one.
  - "wearable": `strap_mount_inset_mm` — how far in from each of the
    housing outer's own two "w"-edges (x-span ends) a strap-mount
    point sits, per this guide's own literal "strap-mount point
    positions" wording. `wrist_curvature_radius_mm` — this guide's own
    literal "wrist-curvature radius approximation" — a single typical-
    adult-wrist radius constant the strap part itself is expected to
    curve to, not a per-project measurement.

No other `mobility_type` ("static", "wheeled", "legged", "flying") has
an entry — literal "No other mobility_type gets a preset (no-op)"
wording: `ERGONOMIC_PRESETS.get(mobility_type)` returns `None` for all
four, and Patch H.2's own consumer treats a `None` lookup as a no-op,
same "table miss is a considered no-op, not an error" posture every
other curated table in this package already holds itself to (eo/
mech_thermal.py's own THERMAL_TABLE/VIBRATION_TABLE, eo/
enclosure_spec.py's own MATERIAL_PROPERTIES).

Deliberately zero logic in this module, same "config file with no
behavior" shape eo/enclosure_spec.py's own module docstring already
documents for ENCLOSURE_SPEC/MATERIAL_PROPERTIES — Patch H.2 (eo/
mech_enclosure.py) is where this table actually gets read and applied.
"""

ERGONOMIC_PRESETS = {
    "handheld": {
        # A palm-and-fingers grip circumference of ~85-100mm splits
        # roughly to a ~28mm minimum width and ~20mm minimum thickness
        # for a rectangular cross-section still comfortable to wrap a
        # hand around one-handed — narrower than that and fingers
        # start to overlap the far side; thinner than that and there's
        # no real material to grip onto. Same "typical hobby-hardware
        # constant, not a per-project measurement" posture this
        # module's own docstring already sets for the whole table.
        "min_grip_w_mm": 28.0,
        "min_grip_d_mm": 20.0,
        # A sharp 90-degree housing edge concentrates pressure into a
        # hand's palm/fingers under grip; 3.0mm is a common "rounded,
        # not sharp, but still reads as a rectangular device" FDM
        # fillet — mandatory per this patch's own "mandatory fillet
        # radius" wording, not a conditional/undersized-housing-only
        # feature.
        "fillet_radius_mm": 3.0,
    },
    "wearable": {
        # How far in from each end of the housing's own width a strap
        # actually attaches — close enough to the edge that the strap
        # doesn't add meaningfully to the device's own worn footprint,
        # far enough in that the mount point sits on solid housing
        # wall rather than right at its own corner/fillet.
        "strap_mount_inset_mm": 6.0,
        # A typical adult wrist circumference (~150-180mm) implies a
        # curvature radius in roughly the 24-38mm range depending on
        # build; 32.0 sits in the middle of that range as a single
        # fixed approximation, per this guide's own "wrist-curvature
        # radius approximation" wording — the strap part itself (not
        # the rigid housing shell) is what actually curves to this
        # radius.
        "wrist_curvature_radius_mm": 32.0,
    },
}
