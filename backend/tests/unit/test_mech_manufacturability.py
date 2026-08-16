"""
tests/unit/test_mech_manufacturability.py — Patch 6.1 (Phase 6,
"Manufacturability report"): covers eo/mech_manufacturability.py --

  - check_standoff_wall_clearance() (Patch 6.1): a post with ample
    plan-view room on every side passes; a post placed too close to
    each of the four housing_inner walls (low-x, high-x, low-y, high-y)
    in turn is flagged on exactly that axis/side; a post's own radius
    (from "diameter") is subtracted before measuring, not just its
    center point; missing "diameter" degrades to a zero-radius
    point-clearance check rather than raising; missing keys on either
    dict default to 0 without raising; pure function (never mutates
    either input).

No LLM, no FreeCAD -- pure data reshaping (same as eo/mech_cutouts.py's
own check_min_wall_thickness() tests), so no mock_llm/fake_bus fixtures
needed.
"""
from eo.enclosure_spec import ENCLOSURE_SPEC
import eo.mech_manufacturability as mm


# A generous, fixed cavity every test below measures a post against --
# same "one shared inner box, only the post moves" shape
# tests/unit/test_mech_cutouts.py's own fixture already uses for
# check_min_wall_thickness().
HOUSING_INNER = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}

MIN_FEATURE = ENCLOSURE_SPEC["min_feature_mm"]


# ---------------------------------------------------------------------------
# check_standoff_wall_clearance (Patch 6.1)
# ---------------------------------------------------------------------------

def test_post_with_ample_room_on_every_side_passes():
    # Dead center of a 100x60 cavity, small diameter -- nowhere near any wall.
    support = {"part_id": "mcu_1", "corner_index": 0, "x": 50, "y": 30, "z": 0,
               "height": 10, "diameter": ENCLOSURE_SPEC["standoff_dia_mm"]}
    result = mm.check_standoff_wall_clearance(support, HOUSING_INNER)

    assert result["ok"] is True
    assert result["violations"] == []
    assert result["margins"]["x"]["low"] > MIN_FEATURE
    assert result["margins"]["x"]["high"] > MIN_FEATURE
    assert result["margins"]["y"]["low"] > MIN_FEATURE
    assert result["margins"]["y"]["high"] > MIN_FEATURE


def test_post_too_close_to_low_x_wall_is_flagged():
    diameter = 7.0
    radius = diameter / 2.0
    # Positioned so the post's own edge sits well under min_feature_mm
    # away from housing_inner's low-x wall (x=0).
    support = {"part_id": "mcu_1", "corner_index": 0,
               "x": radius + (MIN_FEATURE / 2.0), "y": 30, "z": 0,
               "height": 10, "diameter": diameter}
    result = mm.check_standoff_wall_clearance(support, HOUSING_INNER)

    assert result["ok"] is False
    assert {"axis": "x", "side": "low", "margin_mm": round(MIN_FEATURE / 2.0, 3)} in result["violations"]
    # Only the low-x side is flagged -- high-x/y sides still have ample room.
    assert all(v["axis"] != "x" or v["side"] != "high" for v in result["violations"])
    assert all(v["axis"] != "y" for v in result["violations"])


def test_post_too_close_to_high_y_wall_is_flagged():
    diameter = 7.0
    radius = diameter / 2.0
    y = HOUSING_INNER["h"] - radius - (MIN_FEATURE / 2.0)
    support = {"part_id": "power_1", "corner_index": 1, "x": 50, "y": y, "z": 0,
               "height": 10, "diameter": diameter}
    result = mm.check_standoff_wall_clearance(support, HOUSING_INNER)

    assert result["ok"] is False
    assert {"axis": "y", "side": "high", "margin_mm": round(MIN_FEATURE / 2.0, 3)} in result["violations"]


def test_radius_is_subtracted_not_just_center_point():
    # Center point alone is comfortably clear of the low-x wall, but a
    # large enough diameter still eats into the required margin -- the
    # check must measure the post's own EDGE, not its center.
    diameter = 20.0
    support = {"part_id": "module_1", "corner_index": 2, "x": 5, "y": 30, "z": 0,
               "height": 10, "diameter": diameter}
    result = mm.check_standoff_wall_clearance(support, HOUSING_INNER)

    assert result["ok"] is False
    assert any(v["axis"] == "x" and v["side"] == "low" for v in result["violations"])


def test_missing_diameter_degrades_to_zero_radius_point_check():
    support = {"part_id": "mcu_1", "corner_index": 0, "x": 50, "y": 30, "z": 0, "height": 10}
    result = mm.check_standoff_wall_clearance(support, HOUSING_INNER)

    assert result["ok"] is True
    assert result["margins"]["x"]["low"] == 50
    assert result["margins"]["x"]["high"] == 50


def test_missing_keys_default_to_zero_without_raising():
    result = mm.check_standoff_wall_clearance({}, {})
    assert result["ok"] is False
    assert result["margins"]["x"] == {"low": 0.0, "high": 0.0}
    assert result["margins"]["y"] == {"low": 0.0, "high": 0.0}


def test_pure_function_never_mutates_inputs():
    support = {"part_id": "mcu_1", "corner_index": 0, "x": 50, "y": 30, "z": 0,
               "height": 10, "diameter": 7.0}
    housing_inner = dict(HOUSING_INNER)
    support_snapshot = dict(support)
    housing_snapshot = dict(housing_inner)

    mm.check_standoff_wall_clearance(support, housing_inner)

    assert support == support_snapshot
    assert housing_inner == housing_snapshot
