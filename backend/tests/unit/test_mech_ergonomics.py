"""
tests/unit/test_mech_ergonomics.py — Mech View standalone
implementation guide, Phase H, Patch H.3: covers eo/mech_ergonomics.py's
own ERGONOMIC_PRESETS table (H.1) and the wiring Patch H.2 adds to
eo/mech_enclosure.py's own apply_enclosure_generation()
(_apply_ergonomic_preset()).

Per this guide's own literal Patch H.3 wording: "Handheld case meets
grip-dimension minimums; wearable case gets correct strap-mount
positions; static/wheeled/legged/flying archetypes produce identical
output with and without this phase applied."

No LLM, no FreeCAD -- pure data reshaping (same posture
tests/unit/test_mech_material.py's own module docstring already states
for its sibling Phase E test module), so no mock_llm/fake_bus fixtures
needed.
"""
import eo.mech_enclosure as me
from eo.enclosure_spec import ENCLOSURE_SPEC
from eo.mech_ergonomics import ERGONOMIC_PRESETS

_HANDHELD = {"enclosure_mode": "full", "mobility_type": "handheld"}
_WEARABLE = {"enclosure_mode": "full", "mobility_type": "wearable"}
_STATIC = {"enclosure_mode": "full", "mobility_type": "static"}
_WHEELED = {"enclosure_mode": "partial", "mobility_type": "wheeled"}
_LEGGED = {"enclosure_mode": "partial", "mobility_type": "legged"}
_FLYING = {"enclosure_mode": "partial", "mobility_type": "flying"}


# ---------------------------------------------------------------------------
# ERGONOMIC_PRESETS table itself (Patch H.1) -- quick sanity, not a full
# re-test of every literal constant (already this module's own literal,
# self-documenting data).
# ---------------------------------------------------------------------------

def test_only_handheld_and_wearable_have_presets():
    assert set(ERGONOMIC_PRESETS.keys()) == {"handheld", "wearable"}


def test_handheld_preset_shape():
    preset = ERGONOMIC_PRESETS["handheld"]
    assert preset["min_grip_w_mm"] > 0
    assert preset["min_grip_d_mm"] > 0
    assert preset["fillet_radius_mm"] > 0


def test_wearable_preset_shape():
    preset = ERGONOMIC_PRESETS["wearable"]
    assert preset["strap_mount_inset_mm"] > 0
    assert preset["wrist_curvature_radius_mm"] > 0


# ---------------------------------------------------------------------------
# Wiring (Patch H.2) -- eo/mech_enclosure.py's apply_enclosure_generation()
# ---------------------------------------------------------------------------

def _mech_with_device(device_footprint, archetype):
    mech = {
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
    if archetype is not None:
        mech["archetype"] = archetype
    return mech


# --- handheld: meets grip-dimension minimums -------------------------------

def test_handheld_undersized_housing_grows_to_grip_minimums():
    # w=10, d=5 -- well under both the min_grip_w_mm/min_grip_d_mm floor.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 10, "h": 100, "d": 5}
    mech = _mech_with_device(device_footprint, _HANDHELD)

    result = me.apply_enclosure_generation(mech, [])

    preset = ERGONOMIC_PRESETS["handheld"]
    assert result["outer"]["w"] >= preset["min_grip_w_mm"]
    assert result["outer"]["d"] >= preset["min_grip_d_mm"]
    assert result["outer"]["w"] == preset["min_grip_w_mm"]
    assert result["outer"]["d"] == preset["min_grip_d_mm"]


def test_handheld_growth_is_centered_not_pinned_to_one_edge():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 10, "h": 100, "d": 5}
    mech = _mech_with_device(device_footprint, _HANDHELD)
    pre_growth = me.compute_housing_footprint(device_footprint)

    result = me.apply_enclosure_generation(mech, [])

    pre_center_x = pre_growth["outer"]["x"] + pre_growth["outer"]["w"] / 2.0
    post_center_x = result["outer"]["x"] + result["outer"]["w"] / 2.0
    assert round(pre_center_x, 3) == round(post_center_x, 3)

    pre_center_z = pre_growth["outer"]["z"] + pre_growth["outer"]["d"] / 2.0
    post_center_z = result["outer"]["z"] + result["outer"]["d"] / 2.0
    assert round(pre_center_z, 3) == round(post_center_z, 3)


def test_handheld_already_oversized_housing_is_not_shrunk():
    # w=200, d=100 -- already well above both floors.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 200, "h": 100, "d": 100}
    mech = _mech_with_device(device_footprint, _HANDHELD)
    pre_growth = me.compute_housing_footprint(device_footprint)

    result = me.apply_enclosure_generation(mech, [])

    assert result["outer"]["w"] == pre_growth["outer"]["w"]
    assert result["outer"]["d"] == pre_growth["outer"]["d"]


def test_handheld_lid_stays_consistent_with_grown_outer():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 10, "h": 100, "d": 5}
    mech = _mech_with_device(device_footprint, _HANDHELD)

    result = me.apply_enclosure_generation(mech, [])

    assert result["lid"]["w"] == result["outer"]["w"]
    assert result["lid"]["x"] == result["outer"]["x"]
    assert result["lid"]["z"] == round(result["outer"]["z"] + result["outer"]["d"], 3)


def test_handheld_inner_untouched_and_still_a_subset_of_grown_outer():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 10, "h": 100, "d": 5}
    mech = _mech_with_device(device_footprint, _HANDHELD)
    pre_growth = me.compute_housing_footprint(device_footprint)

    result = me.apply_enclosure_generation(mech, [])

    assert result["inner"] == pre_growth["inner"]
    # inner still fully contained within the (now larger) outer box.
    assert result["outer"]["x"] <= result["inner"]["x"]
    assert result["outer"]["z"] <= result["inner"]["z"]
    assert result["outer"]["x"] + result["outer"]["w"] >= result["inner"]["x"] + result["inner"]["w"]
    assert result["outer"]["z"] + result["outer"]["d"] >= result["inner"]["z"] + result["inner"]["d"]


def test_handheld_gets_mandatory_fillet_radius_even_without_growth():
    # Already oversized (no grip-minimum growth needed) -- fillet is
    # still mandatory, per ERGONOMIC_PRESETS's own "mandatory fillet
    # radius" wording, not conditional on growth having happened.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 200, "h": 100, "d": 100}
    mech = _mech_with_device(device_footprint, _HANDHELD)

    result = me.apply_enclosure_generation(mech, [])

    assert result["ergonomics"] == {"fillet_radius_mm": ERGONOMIC_PRESETS["handheld"]["fillet_radius_mm"]}


def test_handheld_housing_placement_reflects_grown_dims():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 10, "h": 100, "d": 5}
    mech = _mech_with_device(device_footprint, _HANDHELD)

    result = me.apply_enclosure_generation(mech, [])

    housing_placement = next(p for p in mech["placements"] if p["part_id"] == "housing_1")
    assert housing_placement["w"] == result["outer"]["w"]
    assert housing_placement["d"] == result["outer"]["d"]
    assert mech["enclosure"]["w"] == result["outer"]["w"]
    assert mech["enclosure"]["d"] == result["outer"]["d"]


# --- wearable: correct strap-mount positions --------------------------------

def test_wearable_gets_two_strap_mount_points_at_correct_positions():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 40, "h": 20, "d": 10}
    mech = _mech_with_device(device_footprint, _WEARABLE)

    result = me.apply_enclosure_generation(mech, [])

    preset = ERGONOMIC_PRESETS["wearable"]
    outer = result["outer"]
    points = result["ergonomics"]["strap_mount_points"]
    assert len(points) == 2

    expected_y = round(outer["y"] + outer["h"] / 2.0, 3)
    expected_z = round(outer["z"] + outer["d"], 3)
    expected_x_values = {
        round(outer["x"] + preset["strap_mount_inset_mm"], 3),
        round(outer["x"] + outer["w"] - preset["strap_mount_inset_mm"], 3),
    }

    assert {p["x"] for p in points} == expected_x_values
    assert all(p["y"] == expected_y for p in points)
    assert all(p["z"] == expected_z for p in points)


def test_wearable_carries_wrist_curvature_radius():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 40, "h": 20, "d": 10}
    mech = _mech_with_device(device_footprint, _WEARABLE)

    result = me.apply_enclosure_generation(mech, [])

    assert result["ergonomics"]["wrist_curvature_radius_mm"] == \
        ERGONOMIC_PRESETS["wearable"]["wrist_curvature_radius_mm"]


def test_wearable_outer_and_lid_are_not_resized():
    # A strap's curvature is a property of the strap PART, not the rigid
    # housing shell -- outer/lid stay exactly what compute_housing_footprint()
    # alone would have produced.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 40, "h": 20, "d": 10}
    mech = _mech_with_device(device_footprint, _WEARABLE)
    pre_ergonomics = me.compute_housing_footprint(device_footprint)

    result = me.apply_enclosure_generation(mech, [])

    assert result["outer"] == pre_ergonomics["outer"]
    assert result["lid"] == pre_ergonomics["lid"]


# --- static/wheeled/legged/flying: unaffected -------------------------------

def test_static_archetype_identical_with_and_without_ergonomics_phase():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    mech_with_archetype = _mech_with_device(device_footprint, _STATIC)
    mech_no_archetype = _mech_with_device(device_footprint, None)

    with_archetype = me.apply_enclosure_generation(mech_with_archetype, [])
    no_archetype = me.apply_enclosure_generation(mech_no_archetype, [])
    direct = me.compute_housing_footprint(device_footprint)

    assert with_archetype == no_archetype == direct
    assert "ergonomics" not in with_archetype
    assert "ergonomics" not in no_archetype


def _partial_mode_mech(device_footprint, archetype):
    return {
        "placements": [{"part_id": "baseplate_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1}],
        "sections": [
            {"section_id": "Enclosure", "subsection_ids": ["baseplate_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1}},
        ],
        "device": {"footprint": device_footprint},
        "archetype": archetype,
    }


def test_wheeled_legged_flying_archetypes_unaffected_by_ergonomics_phase():
    device_footprint = {"x": 0, "y": 0, "z": 10, "w": 80, "h": 50, "d": 20}
    baseline = me.compute_baseplate_footprint(device_footprint)

    for archetype in (_WHEELED, _LEGGED, _FLYING):
        mech = _partial_mode_mech(device_footprint, archetype)
        result = me.apply_enclosure_generation(mech, [])
        assert result == baseline, f"{archetype['mobility_type']} should be unaffected"
        assert "ergonomics" not in result


def test_static_archetype_key_shape_unaffected():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    mech = _mech_with_device(device_footprint, _STATIC)
    result = me.apply_enclosure_generation(mech, [])
    assert set(result.keys()) == {"outer", "inner", "lid"}


def test_handheld_and_wearable_add_ergonomics_key_on_top_of_base_shape():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 40, "h": 20, "d": 10}
    handheld_result = me.apply_enclosure_generation(
        _mech_with_device(device_footprint, _HANDHELD), []
    )
    wearable_result = me.apply_enclosure_generation(
        _mech_with_device(device_footprint, _WEARABLE), []
    )
    assert set(handheld_result.keys()) == {"outer", "inner", "lid", "ergonomics"}
    assert set(wearable_result.keys()) == {"outer", "inner", "lid", "ergonomics"}
