"""
tests/unit/test_mech_enclosure.py — Patch 1.5 (Phase 1, "Enclosure derived
from device footprint"): covers eo/mech_enclosure.py --

  - compute_housing_footprint() (Patch 1.2): outer/inner/lid expansion off
    ENCLOSURE_SPEC's own wall_thickness_mm/clearance_mm, lid stacking on
    top of outer via z = outer.z + outer.d, tolerant-of-missing-keys input
  - apply_enclosure_generation() (Patch 1.3): mech["housing"] stash,
    mech["enclosure"] refreshed in its existing FLAT {"w","h","d"} shape
    (never the nested result -- see that function's own docstring on why),
    housing_1/lid_1 placement entries inside the Enclosure section
    overwritten to match, "nothing to merge yet" None short-circuit when
    mech["device"] isn't populated yet
  - idempotency: two calls with an unchanged device footprint produce an
    identical housing (mirrors eo/mech_device.py's own apply_device_merge()
    idempotency guarantee)
  - no dead space beyond wall_thickness_mm + clearance_mm on any side
  - pipeline-ordering integration: apply_enclosure_generation() called
    right after eo/mech_device.py's own apply_device_merge(), same "Level
    3->4, running immediately after apply_device_merge()" slot the Phase 1
    design section of the implementation guide specifies

No LLM, no FreeCAD -- pure data reshaping (same as eo/mech_sections.py's
own tests), so no mock_llm/fake_bus fixtures needed.
"""
from eo.enclosure_spec import ENCLOSURE_SPEC
import eo.mech_enclosure as me
import eo.mech_device as md


# ---------------------------------------------------------------------------
# compute_housing_footprint (Patch 1.2)
# ---------------------------------------------------------------------------

def test_outer_and_inner_expand_by_spec_constants():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    result = me.compute_housing_footprint(device_footprint)

    clearance = ENCLOSURE_SPEC["clearance_mm"]
    wall = ENCLOSURE_SPEC["wall_thickness_mm"]

    inner = result["inner"]
    assert inner["x"] == -clearance
    assert inner["y"] == -clearance
    assert inner["w"] == 100 + 2 * clearance
    assert inner["h"] == 60 + 2 * clearance
    assert inner["d"] == 30 + 2 * clearance

    outer = result["outer"]
    pad = wall + clearance
    assert outer["x"] == -pad
    assert outer["y"] == -pad
    assert outer["w"] == 100 + 2 * pad
    assert outer["h"] == 60 + 2 * pad
    assert outer["d"] == 30 + 2 * pad


def test_lid_sits_on_top_of_outer_with_same_xy_footprint():
    device_footprint = {"x": 5, "y": 5, "z": 2, "w": 40, "h": 20, "d": 10}
    result = me.compute_housing_footprint(device_footprint)
    outer, lid = result["outer"], result["lid"]

    assert lid["x"] == outer["x"]
    assert lid["y"] == outer["y"]
    assert lid["w"] == outer["w"]
    assert lid["h"] == outer["h"]
    assert lid["z"] == round(outer["z"] + outer["d"], 3)
    assert lid["d"] == ENCLOSURE_SPEC["wall_thickness_mm"]


def test_missing_keys_default_to_zero_without_raising():
    result = me.compute_housing_footprint({})
    assert result["outer"]["w"] == round(2 * (ENCLOSURE_SPEC["wall_thickness_mm"]
                                                + ENCLOSURE_SPEC["clearance_mm"]), 3)


def test_pure_function_never_mutates_input():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    snapshot = dict(device_footprint)
    me.compute_housing_footprint(device_footprint)
    assert device_footprint == snapshot


def test_idempotent_same_input_same_output():
    device_footprint = {"x": 1, "y": 2, "z": 3, "w": 50, "h": 40, "d": 20}
    first = me.compute_housing_footprint(device_footprint)
    second = me.compute_housing_footprint(device_footprint)
    assert first == second


def test_no_dead_space_beyond_wall_plus_clearance():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 80, "h": 50, "d": 25}
    result = me.compute_housing_footprint(device_footprint)
    outer, inner = result["outer"], result["inner"]

    wall = ENCLOSURE_SPEC["wall_thickness_mm"]
    clearance = ENCLOSURE_SPEC["clearance_mm"]

    # inner cavity is exactly device_footprint + clearance on every side --
    # no extra slack beyond the spec's own clearance_mm.
    assert round(inner["x"], 3) == round(device_footprint["x"] - clearance, 3)
    assert round(inner["w"] - device_footprint["w"], 3) == round(2 * clearance, 3)

    # outer shell is exactly inner + wall_thickness_mm on every side -- no
    # extra slack beyond the spec's own wall_thickness_mm.
    assert round(outer["w"] - inner["w"], 3) == round(2 * wall, 3)
    assert round(outer["h"] - inner["h"], 3) == round(2 * wall, 3)
    assert round(outer["d"] - inner["d"], 3) == round(2 * wall, 3)


# ---------------------------------------------------------------------------
# apply_enclosure_generation (Patch 1.3)
# ---------------------------------------------------------------------------

def _mech_with_device(device_footprint):
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 1, "w": 1, "h": 1, "d": 1},
        ],
        "sections": [
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 2}},
        ],
        "device": {"footprint": device_footprint},
    }


def test_returns_none_and_stashes_none_when_device_missing():
    mech = {"placements": [], "sections": []}
    result = me.apply_enclosure_generation(mech, [])
    assert result is None
    assert mech["housing"] is None
    assert "enclosure" not in mech  # untouched -- see docstring


def test_stashes_full_breakdown_on_housing_not_enclosure():
    mech = _mech_with_device({"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30})
    result = me.apply_enclosure_generation(mech, [])
    assert result == mech["housing"]
    assert set(result.keys()) == {"outer", "inner", "lid"}


def test_enclosure_key_stays_flat_for_frontend_compatibility():
    """mech["enclosure"] must stay {"w","h","d"} (flat) -- MechView.jsx's
    PartBox/isShellPlacement/wireframe hull all read enclosure.w/h/d
    directly and would break on a nested dict. See
    apply_enclosure_generation()'s own docstring.
    """
    mech = _mech_with_device({"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30})
    me.apply_enclosure_generation(mech, [])
    enclosure = mech["enclosure"]
    assert set(enclosure.keys()) == {"w", "h", "d"}
    outer = mech["housing"]["outer"]
    assert enclosure["w"] == outer["w"]
    assert enclosure["h"] == outer["h"]
    assert enclosure["d"] == outer["d"]


def test_housing_and_lid_placements_overwritten_to_match():
    mech = _mech_with_device({"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30})
    result = me.apply_enclosure_generation(mech, [])

    by_id = {p["part_id"]: p for p in mech["placements"]}
    housing_placement = by_id["housing_1"]
    lid_placement = by_id["lid_1"]

    for key in ("x", "y", "z", "w", "h", "d"):
        assert housing_placement[key] == result["outer"][key]
        assert lid_placement[key] == result["lid"][key]


def test_housing_and_lid_matched_by_prefix_not_literal_id():
    mech = _mech_with_device({"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30})
    mech["placements"] = [
        {"part_id": "housing_2", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
        {"part_id": "lid_2", "x": 0, "y": 0, "z": 1, "w": 1, "h": 1, "d": 1},
    ]
    mech["sections"][0]["subsection_ids"] = ["housing_2", "lid_2"]

    result = me.apply_enclosure_generation(mech, [])
    by_id = {p["part_id"]: p for p in mech["placements"]}
    assert by_id["housing_2"]["w"] == result["outer"]["w"]
    assert by_id["lid_2"]["w"] == result["lid"]["w"]


def test_idempotent_two_calls_same_device_footprint_same_result():
    mech = _mech_with_device({"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30})
    first = me.apply_enclosure_generation(mech, [])
    second = me.apply_enclosure_generation(mech, [])
    assert first == second

    by_id = {p["part_id"]: p for p in mech["placements"]}
    assert by_id["housing_1"]["x"] == first["outer"]["x"]
    assert by_id["lid_1"]["x"] == first["lid"]["x"]


# ---------------------------------------------------------------------------
# Pipeline ordering: apply_enclosure_generation() right after
# eo/mech_device.py's own apply_device_merge() (Patch 1.5's own wiring,
# mirrored here rather than through agents/hardware_speccer.py's full
# LLM-backed run_hardware_speccer() -- no mock_llm fixture needed to prove
# the ordering contract holds).
# ---------------------------------------------------------------------------

def test_apply_enclosure_generation_after_apply_device_merge_end_to_end():
    mech = {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "mcu_1", "x": 5, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5},
        ],
        "sections": [
            {"section_id": "Compute", "subsection_ids": ["mcu_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
    }
    parts = [
        {"id": "housing_1", "category": "3D_PRINT"},
        {"id": "lid_1", "category": "3D_PRINT"},
        {"id": "mcu_1", "category": "mcu"},
    ]

    md.apply_device_merge(mech, parts)
    assert mech["device"] is not None  # precondition for the call below

    housing = me.apply_enclosure_generation(mech, parts)
    assert housing is not None

    by_id = {p["part_id"]: p for p in mech["placements"]}
    assert by_id["housing_1"]["w"] == housing["outer"]["w"]
    assert by_id["lid_1"]["z"] == housing["lid"]["z"]
    # housing/lid are sized off the ACTUAL packed device footprint, not the
    # placeholder 120x90 the LLM originally guessed.
    assert housing["outer"]["w"] != 120 or housing["outer"]["h"] != 90
