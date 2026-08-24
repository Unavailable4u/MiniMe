"""
tests/unit/test_mech_supports.py — Patch 7e-S6.

eo/mech_supports.py had zero test coverage before this. Phase 2's own
"Definition of done" requires "every part with category in the support
set has >=3 contact points to the housing floor" -- these tests pin the
concrete mechanics that make that true: the four-corner projection rule
(compute_standoffs), the "only members with mount_spec get a bore"
split (compute_screw_bosses), and the precedence rule the pipeline half
(apply_supports_generation) applies when both would otherwise fire for
the same part (bossed wins, plain standoffs for that part_id are
dropped, never both).

No LLM, no FreeCAD, no db -- pure data reshaping (same as
tests/unit/test_mech_enclosure.py), so no mock_llm/fake_bus fixtures
needed for the pure-function half. apply_supports_generation() pulls
real eo/mech_sections.py + eo/mech_subsections.py grouping logic (not
mocked) -- same "build a real mech dict, let the real pipeline resolve
it" approach test_mech_enclosure.py's own _mech_with_device() fixture
already uses for apply_enclosure_generation().
"""
from eo.enclosure_spec import ENCLOSURE_SPEC, SUPPORT_CATEGORIES
import eo.mech_supports as ms


STANDOFF_DIA = ENCLOSURE_SPEC["standoff_dia_mm"]
BORE_DIA = ENCLOSURE_SPEC["screw_boss_dia_mm"]


# ---------------------------------------------------------------------
# compute_standoffs (Patch 2.2)
# ---------------------------------------------------------------------

def test_eligible_member_emits_four_corner_standoffs():
    member = {"part_id": "mcu_1", "category": "mcu",
              "x": 10, "y": 20, "z": 5, "w": 30, "h": 10, "d": 4}
    result = ms.compute_standoffs([member])

    assert len(result) == 4
    assert {p["corner_index"] for p in result} == {0, 1, 2, 3}
    assert all(p["part_id"] == "mcu_1" for p in result)
    assert all(p["diameter"] == STANDOFF_DIA for p in result)


def test_corners_sit_at_the_footprints_four_plan_view_corners():
    member = {"part_id": "mcu_1", "category": "mcu",
              "x": 10, "y": 20, "z": 5, "w": 30, "h": 10, "d": 4}
    result = ms.compute_standoffs([member])
    xy = {(p["x"], p["y"]) for p in result}
    assert xy == {(10, 20), (40, 20), (10, 30), (40, 30)}


def test_standoff_height_spans_floor_to_the_members_own_z():
    member = {"part_id": "mcu_1", "category": "mcu",
              "x": 0, "y": 0, "z": 7.5, "w": 10, "h": 10, "d": 4}
    result = ms.compute_standoffs([member])
    assert all(p["z"] == 0.0 for p in result)
    assert all(p["height"] == 7.5 for p in result)


def test_member_with_ineligible_category_is_skipped():
    member = {"part_id": "sensor_1", "category": "sensor",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    assert ms.compute_standoffs([member]) == []


def test_member_with_no_category_key_at_all_is_skipped():
    member = {"part_id": "mystery_1", "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    assert ms.compute_standoffs([member]) == []


def test_non_dict_entries_are_silently_skipped_not_raised():
    member = {"part_id": "mcu_1", "category": "mcu",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    result = ms.compute_standoffs([member, "not a dict", None, 42])
    assert len(result) == 4


def test_missing_geometry_keys_default_to_zero_without_raising():
    member = {"part_id": "mcu_1", "category": "mcu"}
    result = ms.compute_standoffs([member])
    assert len(result) == 4
    assert all(p["x"] == 0 and p["y"] == 0 and p["z"] == 0 and p["height"] == 0
               for p in result)


def test_multiple_eligible_members_each_get_their_own_four():
    members = [
        {"part_id": "mcu_1", "category": "mcu", "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4},
        {"part_id": "batt_1", "category": "power", "x": 50, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4},
    ]
    result = ms.compute_standoffs(members)
    assert len(result) == 8
    assert {p["part_id"] for p in result} == {"mcu_1", "batt_1"}


def test_empty_input_returns_empty_list():
    assert ms.compute_standoffs([]) == []
    assert ms.compute_standoffs(None) == []


def test_pure_function_never_mutates_input():
    member = {"part_id": "mcu_1", "category": "mcu",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    snapshot = dict(member)
    ms.compute_standoffs([member])
    assert member == snapshot


def test_two_calls_same_input_return_identical_output_including_order():
    members = [
        {"part_id": "mcu_1", "category": "mcu", "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4},
        {"part_id": "batt_1", "category": "power", "x": 50, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4},
    ]
    first = ms.compute_standoffs(members)
    second = ms.compute_standoffs(members)
    assert first == second


def test_all_support_categories_are_covered_and_nothing_else_is():
    for category in SUPPORT_CATEGORIES:
        member = {"part_id": f"{category}_1", "category": category,
                  "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
        assert len(ms.compute_standoffs([member])) == 4
    for category in ("sensor", "actuator", "3D_PRINT", "MISC"):
        member = {"part_id": "x", "category": category,
                  "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
        assert ms.compute_standoffs([member]) == []


# ---------------------------------------------------------------------
# compute_screw_bosses (Patch 2.3)
# ---------------------------------------------------------------------

def test_eligible_member_with_mount_spec_gets_four_bossed_primitives():
    member = {"part_id": "mcu_1", "category": "mcu", "mount_spec": "M3 x2",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    result = ms.compute_screw_bosses([member])
    assert len(result) == 4
    assert all(p["diameter"] == STANDOFF_DIA for p in result)
    assert all(p["bore_diameter"] == BORE_DIA for p in result)


def test_eligible_member_without_mount_spec_gets_nothing():
    member = {"part_id": "mcu_1", "category": "mcu",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    assert ms.compute_screw_bosses([member]) == []


def test_mount_spec_present_but_empty_string_is_treated_as_undeclared():
    member = {"part_id": "mcu_1", "category": "mcu", "mount_spec": "   ",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    assert ms.compute_screw_bosses([member]) == []


def test_mount_spec_present_but_not_a_string_is_treated_as_undeclared():
    member = {"part_id": "mcu_1", "category": "mcu", "mount_spec": ["M3"],
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    assert ms.compute_screw_bosses([member]) == []


def test_ineligible_category_with_mount_spec_still_gets_nothing():
    member = {"part_id": "sensor_1", "category": "sensor", "mount_spec": "M3",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    assert ms.compute_screw_bosses([member]) == []


def test_non_dict_entries_are_silently_skipped():
    member = {"part_id": "mcu_1", "category": "mcu", "mount_spec": "M3",
              "x": 0, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
    result = ms.compute_screw_bosses([member, "junk", None])
    assert len(result) == 4


# ---------------------------------------------------------------------
# apply_supports_generation (Patch 2.4)
# ---------------------------------------------------------------------

def _mech_for(parts):
    """One flat, mount-free subsection per part, all in one section --
    same shallow shape test_mech_enclosure.py's own fixtures use, since
    apply_supports_generation() joins mech["placements"] against `parts`
    via the real group_into_sections()/subsections_for_section()/
    members_for_subsection() pipeline, not a mocked one."""
    placements = [
        {"part_id": p["id"], "x": i * 20, "y": 0, "z": 5, "w": 10, "h": 10, "d": 4}
        for i, p in enumerate(parts)
    ]
    mech = {"placements": placements, "sections": []}
    from eo.mech_sections import apply_section_grouping
    apply_section_grouping(mech, parts)
    return mech


def test_no_sections_returns_and_stashes_empty_result():
    mech = {"placements": [], "sections": []}
    result = ms.apply_supports_generation(mech, [])
    assert result == {"standoffs": [], "bosses": []}
    assert mech["supports"] == {"standoffs": [], "bosses": []}


def test_eligible_part_without_mount_spec_gets_plain_standoffs_only():
    parts = [{"id": "mcu_1", "category": "mcu"}]
    mech = _mech_for(parts)
    result = ms.apply_supports_generation(mech, parts)

    assert len(result["standoffs"]) == 4
    assert result["bosses"] == []
    assert mech["supports"] == result


def test_eligible_part_with_mount_spec_gets_bosses_not_plain_standoffs():
    parts = [{"id": "mcu_1", "category": "mcu", "mount_spec": "M3 x2"}]
    mech = _mech_for(parts)
    result = ms.apply_supports_generation(mech, parts)

    assert result["standoffs"] == []
    assert len(result["bosses"]) == 4
    assert result["bosses"][0]["part_id"] == "mcu_1"


def test_mixed_parts_bossed_and_plain_never_double_counted():
    parts = [
        {"id": "mcu_1", "category": "mcu", "mount_spec": "M3 x2"},
        {"id": "batt_1", "category": "power"},
        {"id": "sensor_1", "category": "sensor"},
    ]
    mech = _mech_for(parts)
    result = ms.apply_supports_generation(mech, parts)

    assert {s["part_id"] for s in result["standoffs"]} == {"batt_1"}
    assert {b["part_id"] for b in result["bosses"]} == {"mcu_1"}
    assert len(result["standoffs"]) == 4
    assert len(result["bosses"]) == 4


def test_ineligible_parts_produce_no_supports_at_all():
    parts = [{"id": "sensor_1", "category": "sensor"}]
    mech = _mech_for(parts)
    result = ms.apply_supports_generation(mech, parts)
    assert result == {"standoffs": [], "bosses": []}


def test_result_is_stashed_on_mech_supports_key():
    parts = [{"id": "mcu_1", "category": "mcu"}]
    mech = _mech_for(parts)
    ms.apply_supports_generation(mech, parts)
    assert "supports" in mech
    assert mech["supports"]["standoffs"]


def test_never_mutates_placements_or_parts_lists():
    parts = [{"id": "mcu_1", "category": "mcu", "mount_spec": "M3"}]
    mech = _mech_for(parts)
    placements_snapshot = [dict(p) for p in mech["placements"]]
    parts_snapshot = [dict(p) for p in parts]

    ms.apply_supports_generation(mech, parts)

    assert mech["placements"] == placements_snapshot
    assert parts == parts_snapshot
