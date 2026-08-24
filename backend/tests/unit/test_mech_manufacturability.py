"""
tests/unit/test_mech_manufacturability.py — Phase 6, "Manufacturability
report": covers eo/mech_manufacturability.py --

  - check_standoff_wall_clearance() (Patch 6.1): a post with ample
    plan-view room on every side passes; a post placed too close to
    each of the four housing_inner walls (low-x, high-x, low-y, high-y)
    in turn is flagged on exactly that axis/side; a post's own radius
    (from "diameter") is subtracted before measuring, not just its
    center point; missing "diameter" degrades to a zero-radius
    point-clearance check rather than raising; missing keys on either
    dict default to 0 without raising; pure function (never mutates
    either input).
  - build_manufacturability_report() (Patch 6.2): a clean mech (every
    support well clear of every wall, every cutout's own
    wall_thickness_check already "ok") reports passed=True with no
    violations; a standoff too close to a wall surfaces as a
    "standoff_wall_clearance" violation tagged with its own part_id; a
    cutout whose own wall_thickness_check already recorded a violation
    surfaces as a "cutout_min_wall_thickness" violation WITHOUT this
    function recomputing anything; missing housing/supports/cutouts
    degrade to "nothing to check there" rather than raising; pure
    function (never mutates mech).
  - check_standoff_cutout_clearance() / check_cutout_overlap() /
    check_feature_collisions() (Patch 6.3): a standoff well clear of a
    cutout-owning part's footprint passes; a standoff whose own circle
    (center + radius) overlaps or sits too close to that footprint is
    flagged with a "gap_mm" (negative when genuinely overlapping); two
    cutouts on the SAME face with overlapping/too-close owning-part
    footprints are flagged, tagged with both part_ids and the shared
    face; two cutouts on DIFFERENT faces are never compared even when
    their footprints would otherwise overlap; a support/cutout pair
    sharing the same part_id (a part's own standoff under its own
    cutout) is never flagged against itself; check_feature_collisions()
    output folds into build_manufacturability_report()'s own violations
    list end to end off a real, pipeline-built mech.

No LLM, no FreeCAD -- pure data reshaping (same as eo/mech_cutouts.py's
own check_min_wall_thickness() tests), so no mock_llm/fake_bus fixtures
needed.
"""
import eo.mech_cutouts as mc
import eo.mech_device as md
import eo.mech_enclosure as me
import eo.mech_manufacturability as mm
import eo.mech_supports as msup
from eo.enclosure_spec import ENCLOSURE_SPEC

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


# ---------------------------------------------------------------------------
# build_manufacturability_report (Patch 6.2)
# ---------------------------------------------------------------------------

def _clean_mech():
    # One well-clear standoff, one well-clear boss, one cutout whose own
    # wall_thickness_check already passed -- everything a real pipeline
    # run would have produced by the time this report is built.
    return {
        "housing": {"inner": dict(HOUSING_INNER)},
        "supports": {
            "standoffs": [
                {"part_id": "mcu_1", "corner_index": 0, "x": 50, "y": 30, "z": 0,
                 "height": 10, "diameter": ENCLOSURE_SPEC["standoff_dia_mm"]},
            ],
            "bosses": [
                {"part_id": "power_1", "corner_index": 1, "x": 60, "y": 20, "z": 0,
                 "height": 8, "diameter": ENCLOSURE_SPEC["standoff_dia_mm"],
                 "bore_diameter": ENCLOSURE_SPEC["screw_boss_dia_mm"]},
            ],
        },
        "cutouts": [
            {"part_id": "sensor_1", "face": "+x", "cutout_type": "vent",
             "shape": "circular", "diameter_mm": 1.5,
             "wall_thickness_check": {"ok": True, "margins": {}, "violations": []}},
        ],
    }


def test_clean_mech_reports_passed_with_no_violations():
    report = mm.build_manufacturability_report(_clean_mech())
    assert report == {"passed": True, "violations": []}


def test_standoff_too_close_to_wall_surfaces_as_violation():
    diameter = 7.0
    radius = diameter / 2.0
    mech = _clean_mech()
    mech["supports"]["standoffs"].append(
        {"part_id": "module_1", "corner_index": 2,
         "x": radius + (MIN_FEATURE / 2.0), "y": 30, "z": 0,
         "height": 10, "diameter": diameter}
    )
    report = mm.build_manufacturability_report(mech)

    assert report["passed"] is False
    assert any(
        v["check"] == "standoff_wall_clearance"
        and v["part_id"] == "module_1"
        and v["axis"] == "x" and v["side"] == "low"
        for v in report["violations"]
    )


def test_cutout_violation_is_collected_not_recomputed():
    mech = _clean_mech()
    mech["cutouts"].append({
        "part_id": "display_1", "face": "+y", "cutout_type": "window",
        "shape": "rectangular", "width_mm": 40, "height_mm": 20,
        "wall_thickness_check": {
            "ok": False, "margins": {},
            "violations": [{"axis": "w", "side": "low", "margin_mm": 0.4}],
        },
    })
    report = mm.build_manufacturability_report(mech)

    assert report["passed"] is False
    assert {
        "check": "cutout_min_wall_thickness", "part_id": "display_1",
        "axis": "w", "side": "low", "margin_mm": 0.4,
    } in report["violations"]


def test_missing_housing_inner_skips_standoff_checks_only():
    mech = _clean_mech()
    mech["housing"] = {"inner": None}
    mech["cutouts"].append({
        "part_id": "display_1",
        "wall_thickness_check": {
            "ok": False, "margins": {},
            "violations": [{"axis": "w", "side": "low", "margin_mm": 0.1}],
        },
    })
    report = mm.build_manufacturability_report(mech)

    # Standoff/boss checks can't run without housing_inner, but the
    # already-computed cutout violation still surfaces.
    assert report["passed"] is False
    assert all(v["check"] != "standoff_wall_clearance" for v in report["violations"])
    assert any(v["check"] == "cutout_min_wall_thickness" for v in report["violations"])


def test_empty_mech_reports_passed_true():
    assert mm.build_manufacturability_report({}) == {"passed": True, "violations": []}
    assert mm.build_manufacturability_report(None) == {"passed": True, "violations": []}


def test_malformed_entries_are_skipped_not_raised():
    mech = {
        "housing": {"inner": dict(HOUSING_INNER)},
        "supports": {"standoffs": ["not_a_dict"], "bosses": None},
        "cutouts": ["not_a_dict", {"part_id": "x", "wall_thickness_check": "not_a_dict"}],
    }
    assert mm.build_manufacturability_report(mech) == {"passed": True, "violations": []}


def test_pure_function_never_mutates_mech():
    mech = _clean_mech()
    import copy
    snapshot = copy.deepcopy(mech)

    mm.build_manufacturability_report(mech)

    assert mech == snapshot


# ---------------------------------------------------------------------------
# check_standoff_cutout_clearance (Patch 6.3)
# ---------------------------------------------------------------------------

# The cutout-owning part's own plan-view footprint (NOT the cutout's own
# smaller opening -- see check_standoff_cutout_clearance()'s own
# docstring on why the owning part's full footprint is used instead).
CUTOUT_FOOTPRINT = {"x": 40, "y": 20, "z": 2, "w": 20, "h": 15, "d": 5}


def test_standoff_far_from_cutout_footprint_passes():
    support = {"part_id": "mcu_1", "x": 5, "y": 5, "z": 0, "height": 10,
               "diameter": ENCLOSURE_SPEC["standoff_dia_mm"]}
    result = mm.check_standoff_cutout_clearance(support, CUTOUT_FOOTPRINT)

    assert result["ok"] is True
    assert result["violations"] == []
    assert result["gap_mm"] > MIN_FEATURE


def test_standoff_overlapping_cutout_footprint_is_flagged_with_negative_gap():
    # Center point dead inside the cutout footprint.
    support = {"part_id": "mcu_1", "x": 50, "y": 27, "z": 0, "height": 10,
               "diameter": 7.0}
    result = mm.check_standoff_cutout_clearance(support, CUTOUT_FOOTPRINT)

    assert result["ok"] is False
    assert result["gap_mm"] < 0
    assert result["violations"] == [{"gap_mm": result["gap_mm"]}]


def test_standoff_just_under_min_feature_from_footprint_edge_is_flagged():
    diameter = 7.0
    radius = diameter / 2.0
    # Just to the left of the footprint's low-x edge (x=40), edge-to-edge
    # gap deliberately under min_feature_mm.
    x = CUTOUT_FOOTPRINT["x"] - radius - (MIN_FEATURE / 2.0)
    support = {"part_id": "mcu_1", "x": x, "y": 27, "z": 0, "height": 10,
               "diameter": diameter}
    result = mm.check_standoff_cutout_clearance(support, CUTOUT_FOOTPRINT)

    assert result["ok"] is False
    assert 0 < result["gap_mm"] < MIN_FEATURE


def test_missing_diameter_and_keys_default_to_zero_without_raising():
    result = mm.check_standoff_cutout_clearance({}, {})
    assert result["ok"] is False  # post at (0,0), footprint at (0,0,0,0) -> gap 0
    assert result["gap_mm"] == 0.0


def test_check_standoff_cutout_clearance_never_mutates_inputs():
    support = {"part_id": "mcu_1", "x": 5, "y": 5, "z": 0, "height": 10, "diameter": 7.0}
    footprint = dict(CUTOUT_FOOTPRINT)
    support_snapshot = dict(support)
    footprint_snapshot = dict(footprint)

    mm.check_standoff_cutout_clearance(support, footprint)

    assert support == support_snapshot
    assert footprint == footprint_snapshot


# ---------------------------------------------------------------------------
# check_cutout_overlap (Patch 6.3)
# ---------------------------------------------------------------------------

def test_non_overlapping_well_separated_footprints_pass():
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 50, "y": 50, "w": 10, "h": 10}
    result = mm.check_cutout_overlap(a, b)

    assert result["ok"] is True
    assert result["violations"] == []


def test_truly_overlapping_footprints_flagged_with_overlap_mm():
    a = {"x": 0, "y": 0, "w": 20, "h": 20}
    b = {"x": 10, "y": 10, "w": 20, "h": 20}
    result = mm.check_cutout_overlap(a, b)

    assert result["ok"] is False
    assert "overlap_mm" in result["violations"][0]
    assert result["violations"][0]["overlap_mm"] > 0


def test_footprints_separated_by_less_than_min_feature_flagged_with_gap_mm():
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 10 + (MIN_FEATURE / 2.0), "y": 0, "w": 10, "h": 10}
    result = mm.check_cutout_overlap(a, b)

    assert result["ok"] is False
    assert "gap_mm" in result["violations"][0]
    assert 0 < result["violations"][0]["gap_mm"] < MIN_FEATURE


def test_footprints_with_ample_room_pass_with_no_violations():
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 10 + MIN_FEATURE * 5, "y": 0, "w": 10, "h": 10}
    result = mm.check_cutout_overlap(a, b)

    assert result["ok"] is True


def test_check_cutout_overlap_never_mutates_inputs():
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    b = {"x": 5, "y": 5, "w": 10, "h": 10}
    a_snapshot, b_snapshot = dict(a), dict(b)

    mm.check_cutout_overlap(a, b)

    assert a == a_snapshot
    assert b == b_snapshot


# ---------------------------------------------------------------------------
# check_feature_collisions / build_manufacturability_report end-to-end
# (Patch 6.3) -- built off the REAL Phase 1/2/5 pipeline, same posture
# tests/unit/test_mech_cutouts.py's own _built_mech() already takes.
# ---------------------------------------------------------------------------

def _demo_mech():
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "battery_1", "x": 5, "y": 5, "z": 2, "w": 20, "h": 10, "d": 10},
            {"part_id": "mcu_1", "x": 5, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5},
            {"part_id": "display_1", "x": 5, "y": 5, "z": 2, "w": 25, "h": 18, "d": 3},
            {"part_id": "button_1", "x": 5, "y": 5, "z": 2, "w": 10, "h": 10, "d": 8},
            {"part_id": "usb_1", "x": 5, "y": 5, "z": 2, "w": 9, "h": 7, "d": 4},
        ],
        "sections": [
            {"section_id": "Power", "subsection_ids": ["battery_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 20, "h": 16, "d": 10}},
            {"section_id": "Compute", "subsection_ids": ["mcu_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 30, "h": 26, "d": 5}},
            {"section_id": "Sensing", "subsection_ids": ["display_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 25, "h": 18, "d": 3}},
            {"section_id": "Actuation", "subsection_ids": ["button_1", "usb_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 19, "h": 10, "d": 8}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
        "wiring": {"edges": []},
    }


_DEMO_PARTS = [
    {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"},
    {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
    {"id": "battery_1", "category": "power", "generic_name": "9V Battery"},
    {"id": "mcu_1", "category": "mcu", "generic_name": "ESP32 Dev Board"},
    {"id": "display_1", "category": "sensor", "generic_name": "0.96in OLED Display"},
    {"id": "button_1", "category": "actuator", "generic_name": "Tactile Push Button"},
    {"id": "usb_1", "category": "power", "generic_name": "USB-C Power Connector"},
]


def _built_mech():
    """Same real-pipeline build tests/unit/test_mech_cutouts.py's own
    _built_mech() already uses, extended one step further to also run
    apply_cutout_generation() -- this module's own checks need
    mech["cutouts"] populated, not just mech["supports"]."""
    mech = _demo_mech()
    md.apply_device_merge(mech, _DEMO_PARTS)
    me.apply_enclosure_generation(mech, _DEMO_PARTS)
    msup.apply_supports_generation(mech, _DEMO_PARTS)
    mc.apply_cutout_generation(mech, _DEMO_PARTS)
    return mech


def _non_overlapping_demo_mech():
    """A variant of _demo_mech() with each part given its own,
    non-overlapping x/y footprint -- unlike the shared _demo_mech()
    fixture (deliberately stacked for cutout-generation breadth, see
    _built_mech()'s own docstring), this one is meant to be genuinely
    collision-free so a positive "no violations" case has a real
    baseline to diff against."""
    mech = _demo_mech()
    positions = {
        "battery_1": (5, 5), "mcu_1": (30, 5), "display_1": (60, 5),
        "button_1": (5, 30), "usb_1": (30, 30),
    }
    for placement in mech["placements"]:
        if placement["part_id"] in positions:
            x, y = positions[placement["part_id"]]
            placement["x"], placement["y"] = x, y
    for section in mech["sections"]:
        first_id = section["subsection_ids"][0]
        if first_id in positions:
            x, y = positions[first_id]
            section["footprint"]["x"], section["footprint"]["y"] = x, y
    return mech


def test_non_overlapping_demo_mech_has_no_feature_collisions():
    mech = _non_overlapping_demo_mech()
    md.apply_device_merge(mech, _DEMO_PARTS)
    me.apply_enclosure_generation(mech, _DEMO_PARTS)
    msup.apply_supports_generation(mech, _DEMO_PARTS)
    mc.apply_cutout_generation(mech, _DEMO_PARTS)

    assert mm.check_feature_collisions(mech) == []


def test_footprint_by_part_id_resolves_real_pipeline_geometry():
    mech = _built_mech()
    footprints = mm._footprint_by_part_id(mech)

    assert footprints["mcu_1"]["w"] == 30
    assert footprints["display_1"]["h"] == 18


def test_real_pipeline_mech_runs_clean_without_raising():
    # _demo_mech()'s own placements (shared with tests/unit/
    # test_mech_cutouts.py's fixture) deliberately stack several parts
    # at the same x/y to exercise cutout generation broadly -- so this
    # is NOT a collision-free layout, and check_feature_collisions() is
    # expected to find real violations against it. What this test
    # confirms is that a full, real pipeline output never raises and
    # always returns a well-formed, flat violation list.
    mech = _built_mech()
    violations = mm.check_feature_collisions(mech)

    assert isinstance(violations, list)
    for violation in violations:
        assert violation["check"] in ("standoff_cutout_clearance", "cutout_overlap")


def test_standoff_too_close_to_unrelated_cutout_is_flagged():
    mech = _built_mech()
    # Inject an extra standoff for mcu_1, placed dead inside display_1's
    # own footprint -- a genuine cross-part collision.
    display_footprint = mm._footprint_by_part_id(mech)["display_1"]
    mech["supports"]["standoffs"].append({
        "part_id": "mcu_1", "corner_index": 9,
        "x": display_footprint["x"] + 1, "y": display_footprint["y"] + 1, "z": 0,
        "height": 5, "diameter": 7.0,
    })
    violations = mm.check_feature_collisions(mech)

    assert any(
        v["check"] == "standoff_cutout_clearance"
        and v["standoff_part_id"] == "mcu_1"
        and v["cutout_part_id"] == "display_1"
        for v in violations
    )


def test_standoff_under_its_own_parts_cutout_is_not_flagged():
    mech = _built_mech()
    display_footprint = mm._footprint_by_part_id(mech)["display_1"]
    # display_1's OWN standoff sitting under its OWN cutout -- expected
    # geometry, never a cross-feature collision.
    mech["supports"]["standoffs"].append({
        "part_id": "display_1", "corner_index": 9,
        "x": display_footprint["x"] + 1, "y": display_footprint["y"] + 1, "z": 0,
        "height": 5, "diameter": 7.0,
    })
    violations = mm.check_feature_collisions(mech)

    assert all(v.get("cutout_part_id") != "display_1" for v in violations
               if v["check"] == "standoff_cutout_clearance")


def test_two_cutouts_on_same_face_overlapping_footprints_flagged():
    mech = _built_mech()
    cutouts = mech["cutouts"]
    assert len(cutouts) >= 2
    # Force two cutouts onto the same face with deliberately overlapping
    # owning-part footprints, bypassing the real pipeline's own
    # (non-overlapping) demo layout to exercise this check directly.
    cutouts[0]["face"] = "+x"
    cutouts[1]["face"] = "+x"
    part_a, part_b = cutouts[0]["part_id"], cutouts[1]["part_id"]
    footprints = mech["placements"]
    by_id = {p["part_id"]: p for p in footprints}
    by_id[part_a].update({"x": 0, "y": 0, "w": 20, "h": 20})
    by_id[part_b].update({"x": 5, "y": 5, "w": 20, "h": 20})

    violations = mm.check_feature_collisions(mech)
    assert any(
        v["check"] == "cutout_overlap"
        and {v["part_id_a"], v["part_id_b"]} == {part_a, part_b}
        for v in violations
    )


def test_two_cutouts_on_different_faces_never_compared_even_if_overlapping():
    mech = _built_mech()
    cutouts = mech["cutouts"]
    assert len(cutouts) >= 2
    cutouts[0]["face"] = "+x"
    cutouts[1]["face"] = "-y"  # deliberately different
    part_a, part_b = cutouts[0]["part_id"], cutouts[1]["part_id"]
    by_id = {p["part_id"]: p for p in mech["placements"]}
    by_id[part_a].update({"x": 0, "y": 0, "w": 20, "h": 20})
    by_id[part_b].update({"x": 5, "y": 5, "w": 20, "h": 20})

    violations = mm.check_feature_collisions(mech)
    assert not any(
        v["check"] == "cutout_overlap" and {v["part_id_a"], v["part_id_b"]} == {part_a, part_b}
        for v in violations
    )


def test_feature_collisions_fold_into_build_manufacturability_report():
    mech = _built_mech()
    display_footprint = mm._footprint_by_part_id(mech)["display_1"]
    mech["supports"]["standoffs"].append({
        "part_id": "mcu_1", "corner_index": 9,
        "x": display_footprint["x"] + 1, "y": display_footprint["y"] + 1, "z": 0,
        "height": 5, "diameter": 7.0,
    })
    report = mm.build_manufacturability_report(mech)

    assert report["passed"] is False
    assert any(v["check"] == "standoff_cutout_clearance" for v in report["violations"])


def test_check_feature_collisions_no_cutouts_returns_empty():
    assert mm.check_feature_collisions({"sections": [], "supports": {}, "cutouts": []}) == []
    assert mm.check_feature_collisions({}) == []
    assert mm.check_feature_collisions(None) == []
