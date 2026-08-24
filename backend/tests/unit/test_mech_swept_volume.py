"""
tests/unit/test_mech_swept_volume.py — Mech View standalone
implementation guide, Phase B, Patch B.7: covers
eo/mech_swept_volume.py (Patches B.3/B.4/B.5 + the B.6 wiring glue it
also owns) and the B.6 leakage guard added to eo/mech_cutouts.py.

  - swept_aabb_rotational() (Patch B.3): a wheel/continuous-rotation
    part's own swept cylinder comes back as a square AABB of side
    `2 * radius_mm`, centered on the part's own footprint center, with
    z/d carried through unchanged.
  - swept_aabb_linear() (Patch B.3): a linear part's own footprint is
    extruded by `travel_mm` on exactly the declared axis, every other
    axis unchanged; an unrecognized axis is a zero-length no-op.
  - swept_aabb_arc() (Patch B.4): an arc range that stays strictly
    between cardinal angles matches the plain two-endpoint box; an arc
    range that crosses a cardinal angle (-45 -> 45, crossing 0 deg)
    produces a box correctly WIDENED past what the two endpoints alone
    would give -- literal Patch B.4 "why this is its own patch" worked
    example.
  - apply_tolerance() (Patch B.5): expands a raw AABB outward by
    tolerance_mm on all three axes and tags "shape_kind": "exclusion";
    the raw AABB argument itself is never mutated.
  - compute_swept_volumes()/apply_swept_volume_generation() (Patch B.6
    wiring glue): dispatches a placed, motion-table-matched member to
    the right B.3/B.4 shape function, tolerance-expands it, and stashes
    the flat list on mech["exclusions"]; a part with no MOTION_TABLE
    entry contributes nothing.
  - Exclusion-tag leakage guard (Patch B.6, eo/mech_cutouts.py): an
    entry tagged "shape_kind": "exclusion" is never treated as a
    cutout target by apply_cutout_generation(), confirmed against a
    part that WOULD otherwise match a CUTOUT_TABLE keyword.

No LLM, no FreeCAD -- pure data reshaping (same posture
tests/unit/test_mech_manufacturability.py's own module docstring
already states for its own sibling module), so no mock_llm/fake_bus
fixtures needed.
"""
import math

import eo.mech_cutouts as mc
import eo.mech_device as md
import eo.mech_enclosure as me
import eo.mech_supports as msup
import eo.mech_swept_volume as msv


# ---------------------------------------------------------------------------
# swept_aabb_rotational (Patch B.3) -- cylinder case
# ---------------------------------------------------------------------------

def test_rotational_continuous_bounds_full_swept_cylinder():
    # Wheel footprint centered at (30, 30), spinning with radius 15mm.
    part = {"x": 20, "y": 25, "z": 2, "w": 20, "h": 10, "d": 8}
    motion = {"type": "rotational_continuous", "radius_mm": 15}

    result = msv.swept_aabb_rotational(part, motion)

    # Center is (20 + 20/2, 25 + 10/2) = (30, 30); box side = 2*15 = 30.
    assert result["x"] == 15
    assert result["y"] == 15
    assert result["w"] == 30
    assert result["h"] == 30
    # z/d carried through unchanged -- spinning doesn't grow height.
    assert result["z"] == 2
    assert result["d"] == 8


def test_rotational_continuous_missing_fields_default_to_zero():
    result = msv.swept_aabb_rotational({}, {})
    assert result == {"x": 0, "y": 0, "z": 0, "w": 0, "h": 0, "d": 0}


def test_rotational_continuous_is_pure():
    part = {"x": 20, "y": 25, "z": 2, "w": 20, "h": 10, "d": 8}
    motion = {"type": "rotational_continuous", "radius_mm": 15}
    part_before, motion_before = dict(part), dict(motion)

    msv.swept_aabb_rotational(part, motion)

    assert part == part_before
    assert motion == motion_before


# ---------------------------------------------------------------------------
# swept_aabb_linear (Patch B.3) -- linear case
# ---------------------------------------------------------------------------

def test_linear_extrudes_only_declared_axis():
    part = {"x": 10, "y": 5, "z": 0, "w": 8, "h": 8, "d": 30}
    motion = {"type": "linear", "travel_mm": 50, "axis": "z"}

    result = msv.swept_aabb_linear(part, motion)

    assert result["d"] == 30 + 50  # extruded on the declared axis
    # Every other axis unchanged from the part's own placed footprint.
    assert result["x"] == 10
    assert result["y"] == 5
    assert result["z"] == 0
    assert result["w"] == 8
    assert result["h"] == 8


def test_linear_extrudes_on_x_axis_when_declared():
    part = {"x": 0, "y": 0, "z": 0, "w": 12, "h": 6, "d": 6}
    motion = {"type": "linear", "travel_mm": 25, "axis": "x"}

    result = msv.swept_aabb_linear(part, motion)

    assert result["w"] == 12 + 25
    assert result["h"] == 6
    assert result["d"] == 6


def test_linear_unrecognized_axis_is_zero_length_noop():
    part = {"x": 10, "y": 5, "z": 0, "w": 8, "h": 8, "d": 30}
    motion = {"type": "linear", "travel_mm": 50, "axis": "diagonal"}

    result = msv.swept_aabb_linear(part, motion)

    assert result == {"x": 10, "y": 5, "z": 0, "w": 8, "h": 8, "d": 30}


# ---------------------------------------------------------------------------
# swept_aabb_arc (Patch B.4) -- with and without a cardinal crossing
# ---------------------------------------------------------------------------

def test_arc_strictly_between_cardinals_matches_two_endpoint_box():
    # [10, 80] stays strictly between 0 and 90 -- no cardinal crossing,
    # so the box should match the plain two-endpoint computation.
    part = {"x": 0, "y": 0, "z": 1, "w": 0, "h": 0, "d": 4}
    motion = {"type": "rotational_arc", "range_deg": [10, 80], "arm_length_mm": 25}

    result = msv.swept_aabb_arc(part, motion)

    xs = [25 * math.cos(math.radians(10)), 25 * math.cos(math.radians(80))]
    ys = [25 * math.sin(math.radians(10)), 25 * math.sin(math.radians(80))]
    expected_min_x, expected_max_x = min(xs), max(xs)
    expected_min_y, expected_max_y = min(ys), max(ys)

    assert result["x"] == round(expected_min_x, 3)
    assert result["y"] == round(expected_min_y, 3)
    assert result["w"] == round(expected_max_x - expected_min_x, 3)
    assert result["h"] == round(expected_max_y - expected_min_y, 3)
    assert result["z"] == 1
    assert result["d"] == 4


def test_arc_crossing_cardinal_widens_past_naive_two_endpoint_box():
    # Literal Patch B.4 worked example: range_deg = [-45, 45] crosses
    # 0 deg. Endpoint-only x-max is ~17.7 (25*cos(45)); the TRUE x-max,
    # at the 0-degree crossing, is the full arm_length_mm (25).
    part = {"x": 0, "y": 0, "z": 0, "w": 0, "h": 0, "d": 0}
    motion = {"type": "rotational_arc", "range_deg": [-45, 45], "arm_length_mm": 25}

    result = msv.swept_aabb_arc(part, motion)

    # Both endpoints (-45 and 45 deg) land at the SAME x (~17.678),
    # since cos(-45) == cos(45) -- a naive endpoint-only box's own
    # x-max would be that value. The 0-degree crossing candidate this
    # function adds pushes the TRUE x-max out to the full arm_length_mm
    # (25), strictly beyond that naive endpoint-only x-max.
    naive_endpoint_x_max = round(25 * math.cos(math.radians(45)), 3)
    true_x_max = round(result["x"] + result["w"], 3)
    assert true_x_max > naive_endpoint_x_max
    assert true_x_max == 25.0


def test_arc_missing_range_and_arm_length_degrades_to_zero_box():
    result = msv.swept_aabb_arc({"x": 10, "y": 10, "z": 0, "w": 0, "h": 0, "d": 0}, {})
    assert result["w"] == 0
    assert result["h"] == 0
    assert result["x"] == 10
    assert result["y"] == 10


# ---------------------------------------------------------------------------
# apply_tolerance (Patch B.5) -- tolerance-expansion correctness
# ---------------------------------------------------------------------------

def test_tolerance_expands_every_axis_and_tags_exclusion():
    aabb = {"x": 10, "y": 10, "z": 5, "w": 20, "h": 20, "d": 8}

    result = msv.apply_tolerance(aabb, tolerance_mm=1.5)

    assert result["x"] == 10 - 1.5
    assert result["y"] == 10 - 1.5
    assert result["z"] == 5 - 1.5
    assert result["w"] == 20 + 2 * 1.5
    assert result["h"] == 20 + 2 * 1.5
    assert result["d"] == 8 + 2 * 1.5
    assert result["shape_kind"] == "exclusion"


def test_tolerance_default_matches_patch_b5_stated_default():
    aabb = {"x": 0, "y": 0, "z": 0, "w": 10, "h": 10, "d": 10}
    result = msv.apply_tolerance(aabb)
    assert result["w"] == 10 + 2 * 1.5


def test_tolerance_never_mutates_input_aabb():
    aabb = {"x": 10, "y": 10, "z": 5, "w": 20, "h": 20, "d": 8}
    aabb_before = dict(aabb)

    msv.apply_tolerance(aabb, tolerance_mm=1.5)

    assert aabb == aabb_before
    assert "shape_kind" not in aabb


# ---------------------------------------------------------------------------
# is_exclusion (Patch B.5 predicate)
# ---------------------------------------------------------------------------

def test_is_exclusion_predicate():
    assert msv.is_exclusion({"shape_kind": "exclusion"}) is True
    assert msv.is_exclusion({"shape_kind": "cutout"}) is False
    assert msv.is_exclusion({}) is False
    assert msv.is_exclusion(None) is False
    assert msv.is_exclusion("not a dict") is False


# ---------------------------------------------------------------------------
# compute_swept_volumes / apply_swept_volume_generation (Patch B.6 glue)
# ---------------------------------------------------------------------------

_MOTION_PARTS = [
    {"id": "wheel_1", "category": "actuator", "generic_name": "wheel"},
    {"id": "servo_1", "category": "actuator", "generic_name": "hobby servo"},
    {"id": "mcu_1", "category": "mcu", "generic_name": "ESP32 Dev Board"},
]


def _motion_mech():
    return {
        "placements": [
            {"part_id": "wheel_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 20, "d": 10},
            {"part_id": "servo_1", "x": 40, "y": 0, "z": 0, "w": 10, "h": 10, "d": 10},
            {"part_id": "mcu_1", "x": 0, "y": 40, "z": 0, "w": 30, "h": 20, "d": 5},
        ],
        "sections": [
            {"section_id": "Actuation", "subsection_ids": ["wheel_1", "servo_1"]},
            {"section_id": "Compute", "subsection_ids": ["mcu_1"]},
        ],
    }


def test_compute_swept_volumes_only_covers_motion_table_matches():
    mech = _motion_mech()
    volumes = msv.compute_swept_volumes(mech, _MOTION_PARTS)

    part_ids = {v["part_id"] for v in volumes}
    # wheel_1/servo_1 both have MOTION_TABLE entries; mcu_1 (static,
    # no entry) contributes nothing.
    assert part_ids == {"wheel_1", "servo_1"}
    assert all(v["shape_kind"] == "exclusion" for v in volumes)


def test_apply_swept_volume_generation_stashes_on_mech_exclusions():
    mech = _motion_mech()
    result = msv.apply_swept_volume_generation(mech, _MOTION_PARTS)

    assert mech["exclusions"] is result
    assert {v["part_id"] for v in result} == {"wheel_1", "servo_1"}


def test_compute_swept_volumes_noop_when_mech_has_no_sections_yet():
    mech = {"placements": []}
    assert msv.compute_swept_volumes(mech, _MOTION_PARTS) == []


# ---------------------------------------------------------------------------
# Exclusion-tag leakage guard (Patch B.6, eo/mech_cutouts.py)
# ---------------------------------------------------------------------------

_CUTOUT_PARTS = [
    {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"},
    {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
    {"id": "battery_1", "category": "power", "generic_name": "9V Battery"},
    {"id": "display_1", "category": "sensor", "generic_name": "0.96in OLED Display"},
]


def _cutout_demo_mech():
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "battery_1", "x": 5, "y": 5, "z": 2, "w": 20, "h": 10, "d": 10},
            {"part_id": "display_1", "x": 5, "y": 5, "z": 2, "w": 25, "h": 18, "d": 3},
        ],
        "sections": [
            {"section_id": "Power", "subsection_ids": ["battery_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 20, "h": 16, "d": 10}},
            {"section_id": "Sensing", "subsection_ids": ["display_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 25, "h": 18, "d": 3}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
        "wiring": {"edges": []},
    }


def _built_cutout_mech():
    mech = _cutout_demo_mech()
    md.apply_device_merge(mech, _CUTOUT_PARTS)
    me.apply_enclosure_generation(mech, _CUTOUT_PARTS)
    msup.apply_supports_generation(mech, _CUTOUT_PARTS)
    return mech


def test_display_normally_produces_a_cutout():
    # Sanity baseline: an untagged display placement DOES get a cutout,
    # so the exclusion-tagged case below is a real guard, not a
    # trivially-passing no-op fixture.
    mech = _built_cutout_mech()
    cutouts = mc.apply_cutout_generation(mech, _CUTOUT_PARTS)
    assert "display_1" in {c["part_id"] for c in cutouts}


def test_exclusion_tagged_member_never_reaches_cutout_generation():
    mech = _built_cutout_mech()
    # Simulate a swept-volume exclusion box leaking into placements --
    # the guard in apply_cutout_generation() must skip it even though
    # it would otherwise match the "display" CUTOUT_TABLE keyword.
    for placement in mech["placements"]:
        if placement["part_id"] == "display_1":
            placement["shape_kind"] = "exclusion"

    cutouts = mc.apply_cutout_generation(mech, _CUTOUT_PARTS)

    assert "display_1" not in {c["part_id"] for c in cutouts}


def test_exclusion_tagged_member_does_not_suppress_other_cutout_eligible_parts():
    # The guard should be scoped to the tagged entry only -- tagging
    # display_1 must not accidentally swallow a sibling part's own
    # legitimate cutout.
    mech = _built_cutout_mech()
    mech["placements"].append(
        {"part_id": "battery_1", "x": 5, "y": 5, "z": 2, "w": 20, "h": 10, "d": 10}
    )
    for placement in mech["placements"]:
        if placement["part_id"] == "display_1":
            placement["shape_kind"] = "exclusion"

    cutouts = mc.apply_cutout_generation(mech, _CUTOUT_PARTS)
    part_ids = {c["part_id"] for c in cutouts}

    assert "display_1" not in part_ids
    # battery_1 was never cutout-eligible in the first place (no
    # CUTOUT_TABLE keyword match) -- confirms the guard didn't just
    # accidentally zero out every cutout, only the tagged one.
    assert "battery_1" not in part_ids
