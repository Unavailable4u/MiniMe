"""
tests/unit/test_mech_subsections.py — G3e-1 (Master Guide, "G3/G4.
Hierarchical parallel build + validate", Level 1->2 "Subsections",
deterministic grouping): covers eo/mech_subsections.py --

  - group_into_subsections()'s part+mount pairing off the "mount_"
    naming convention
  - singleton subsections for parts with no mount sibling
  - orphaned "mount_x" placements (no matching "x" part) still becoming
    their own singleton subsection, nothing dropped
  - placements with no part_id skipped entirely
  - members_for_subsection()'s lookup-back-to-placements helper
  - apply_subsection_grouping()'s mutate-and-return wrapper

No LLM, no FreeCAD -- this module is pure data reshaping, so these tests
need no mock_llm/fake_bus fixtures at all.
"""
import eo.mech_subsections as ms

# ---------------------------------------------------------------------------
# group_into_subsections
# ---------------------------------------------------------------------------

def test_groups_part_with_its_mount_sibling():
    mech = {"placements": [
        {"part_id": "mcu_1", "w": 30, "h": 20, "d": 5},
        {"part_id": "mount_mcu_1", "w": 30, "h": 5, "d": 5},
    ]}
    subsections = ms.group_into_subsections(mech)
    assert subsections == [
        {"subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"]},
    ]


def test_part_with_no_mount_becomes_singleton_subsection():
    mech = {"placements": [{"part_id": "battery_1", "w": 20, "h": 10, "d": 10}]}
    subsections = ms.group_into_subsections(mech)
    assert subsections == [{"subsection_id": "battery_1", "member_ids": ["battery_1"]}]


def test_orphaned_mount_becomes_its_own_singleton_subsection():
    # "mount_ghost_1" exists but "ghost_1" itself does not -- nothing
    # should silently vanish from Level 2.
    mech = {"placements": [{"part_id": "mount_ghost_1", "w": 10, "h": 10, "d": 5}]}
    subsections = ms.group_into_subsections(mech)
    assert subsections == [{"subsection_id": "mount_ghost_1", "member_ids": ["mount_ghost_1"]}]


def test_mixed_bom_produces_expected_grouping():
    mech = {"placements": [
        {"part_id": "mcu_1"},
        {"part_id": "mount_mcu_1"},
        {"part_id": "battery_1"},
        {"part_id": "sensor_1"},
        {"part_id": "mount_sensor_1"},
        {"part_id": "housing_1"},
    ]}
    subsections = ms.group_into_subsections(mech)
    by_id = {s["subsection_id"]: s["member_ids"] for s in subsections}
    assert by_id == {
        "mcu_1": ["mcu_1", "mount_mcu_1"],
        "battery_1": ["battery_1"],
        "sensor_1": ["sensor_1", "mount_sensor_1"],
        "housing_1": ["housing_1"],
    }
    # Every subsection_id is a real anchor part -- never a "mount_"-
    # prefixed id used as its own anchor.
    assert all(not sid.startswith(ms.MOUNT_ID_PREFIX) for sid in by_id)


def test_placements_missing_part_id_are_skipped():
    mech = {"placements": [{"w": 10, "h": 10, "d": 10}, {"part_id": "mcu_1"}]}
    subsections = ms.group_into_subsections(mech)
    assert subsections == [{"subsection_id": "mcu_1", "member_ids": ["mcu_1"]}]


def test_empty_or_missing_placements_returns_empty_list():
    assert ms.group_into_subsections({}) == []
    assert ms.group_into_subsections({"placements": []}) == []
    assert ms.group_into_subsections(None) == []


def test_non_dict_placements_are_ignored():
    mech = {"placements": ["not a dict", None, {"part_id": "mcu_1"}]}
    subsections = ms.group_into_subsections(mech)
    assert subsections == [{"subsection_id": "mcu_1", "member_ids": ["mcu_1"]}]


def test_does_not_mutate_input_mech():
    mech = {"placements": [{"part_id": "mcu_1"}, {"part_id": "mount_mcu_1"}]}
    original = {"placements": list(mech["placements"])}
    ms.group_into_subsections(mech)
    assert mech == original
    assert "subsections" not in mech


# ---------------------------------------------------------------------------
# members_for_subsection
# ---------------------------------------------------------------------------

def test_members_for_subsection_resolves_placements_in_order():
    mech = {"placements": [
        {"part_id": "mount_mcu_1", "w": 30, "h": 5, "d": 5},
        {"part_id": "mcu_1", "w": 30, "h": 20, "d": 5},
    ]}
    subsection = {"subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"]}
    members = ms.members_for_subsection(mech, subsection)
    assert [m["part_id"] for m in members] == ["mcu_1", "mount_mcu_1"]
    assert members[0]["h"] == 20
    assert members[1]["h"] == 5


def test_members_for_subsection_skips_unresolvable_member_id():
    mech = {"placements": [{"part_id": "mcu_1"}]}
    subsection = {"subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"]}
    members = ms.members_for_subsection(mech, subsection)
    assert [m["part_id"] for m in members] == ["mcu_1"]


def test_members_for_subsection_handles_empty_subsection():
    assert ms.members_for_subsection({"placements": []}, {}) == []
    assert ms.members_for_subsection({"placements": []}, None) == []


# ---------------------------------------------------------------------------
# apply_subsection_grouping
# ---------------------------------------------------------------------------

def test_apply_subsection_grouping_mutates_and_returns_same_list():
    mech = {"placements": [{"part_id": "mcu_1"}, {"part_id": "mount_mcu_1"}]}
    returned = ms.apply_subsection_grouping(mech)
    assert mech["subsections"] == returned
    assert returned == [{"subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"]}]


def test_apply_subsection_grouping_is_idempotent():
    mech = {"placements": [{"part_id": "mcu_1"}, {"part_id": "mount_mcu_1"}]}
    first = ms.apply_subsection_grouping(mech)
    second = ms.apply_subsection_grouping(mech)
    assert first == second
