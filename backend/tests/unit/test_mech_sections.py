"""
tests/unit/test_mech_sections.py — G3h (Master Guide, "G3/G4. Hierarchical
parallel build + validate", Level 2->3 "Sections", missing test coverage
for G3f-1): covers eo/mech_sections.py --

  - group_into_sections()'s category->section bucketing off the BOM
    `parts` list (not `mech["placements"]`)
  - the "module"->Compute and "MISC"->Enclosure special-case mappings
  - the _DEFAULT_SECTION ("Enclosure") fallback for an unknown category
    and for a subsection whose anchor part has no BOM entry at all
  - _SECTION_ORDER's fixed Power/Compute/Sensing/Actuation/Enclosure
    iteration order, and that an empty section is never emitted
  - empty/missing placements and parts inputs
  - subsections_for_section()'s lookup-back-to-Level-2 helper
  - apply_section_grouping()'s mutate-and-return wrapper

Mirrors tests/unit/test_mech_subsections.py's own shape one level down.
No LLM, no FreeCAD -- this module is pure data reshaping (same as
eo/mech_subsections.py), so these tests need no mock_llm/fake_bus
fixtures at all.
"""
import eo.mech_sections as ms


# ---------------------------------------------------------------------------
# group_into_sections
# ---------------------------------------------------------------------------

def test_groups_subsections_by_category_into_named_sections():
    mech = {"placements": [
        {"part_id": "mcu_1"},
        {"part_id": "mount_mcu_1"},
        {"part_id": "battery_1"},
        {"part_id": "sensor_1"},
    ]}
    parts = [
        {"id": "mcu_1", "category": "mcu"},
        {"id": "battery_1", "category": "power"},
        {"id": "sensor_1", "category": "sensor"},
    ]
    sections = ms.group_into_sections(mech, parts)
    by_id = {s["section_id"]: s["subsection_ids"] for s in sections}
    assert by_id == {
        "Power": ["battery_1"],
        "Compute": ["mcu_1"],
        "Sensing": ["sensor_1"],
    }


def test_module_category_maps_to_compute():
    mech = {"placements": [{"part_id": "radio_1"}]}
    parts = [{"id": "radio_1", "category": "module"}]
    sections = ms.group_into_sections(mech, parts)
    assert sections == [{"section_id": "Compute", "subsection_ids": ["radio_1"]}]


def test_misc_category_maps_to_enclosure():
    mech = {"placements": [{"part_id": "fastener_1"}]}
    parts = [{"id": "fastener_1", "category": "MISC"}]
    sections = ms.group_into_sections(mech, parts)
    assert sections == [{"section_id": "Enclosure", "subsection_ids": ["fastener_1"]}]


def test_unrecognized_category_falls_back_to_enclosure():
    mech = {"placements": [{"part_id": "weird_1"}]}
    parts = [{"id": "weird_1", "category": "some_new_category"}]
    sections = ms.group_into_sections(mech, parts)
    assert sections == [{"section_id": "Enclosure", "subsection_ids": ["weird_1"]}]


def test_anchor_part_missing_from_bom_falls_back_to_enclosure():
    # "ghost_1" is in placements but never shows up in `parts` -- nothing
    # should silently vanish from Level 3, same posture as
    # eo/mech_subsections.py's own orphaned-mount handling.
    mech = {"placements": [{"part_id": "ghost_1"}]}
    sections = ms.group_into_sections(mech, [])
    assert sections == [{"section_id": "Enclosure", "subsection_ids": ["ghost_1"]}]


def test_sections_are_emitted_in_fixed_guide_order_regardless_of_input_order():
    mech = {"placements": [
        {"part_id": "housing_1"},
        {"part_id": "sensor_1"},
        {"part_id": "battery_1"},
        {"part_id": "mcu_1"},
        {"part_id": "actuator_1"},
    ]}
    parts = [
        {"id": "housing_1", "category": "3D_PRINT"},
        {"id": "sensor_1", "category": "sensor"},
        {"id": "battery_1", "category": "power"},
        {"id": "mcu_1", "category": "mcu"},
        {"id": "actuator_1", "category": "actuator"},
    ]
    sections = ms.group_into_sections(mech, parts)
    assert [s["section_id"] for s in sections] == [
        "Power", "Compute", "Sensing", "Actuation", "Enclosure",
    ]


def test_empty_sections_are_never_emitted():
    mech = {"placements": [{"part_id": "battery_1"}]}
    parts = [{"id": "battery_1", "category": "power"}]
    sections = ms.group_into_sections(mech, parts)
    assert [s["section_id"] for s in sections] == ["Power"]


def test_multiple_subsections_in_one_section_keep_grouping_order():
    mech = {"placements": [
        {"part_id": "sensor_1"},
        {"part_id": "sensor_2"},
        {"part_id": "mount_sensor_2"},
    ]}
    parts = [
        {"id": "sensor_1", "category": "sensor"},
        {"id": "sensor_2", "category": "sensor"},
    ]
    sections = ms.group_into_sections(mech, parts)
    assert sections == [
        {"section_id": "Sensing", "subsection_ids": ["sensor_1", "sensor_2"]},
    ]


def test_empty_or_missing_placements_returns_empty_list():
    assert ms.group_into_sections({}, []) == []
    assert ms.group_into_sections({"placements": []}, []) == []
    assert ms.group_into_sections(None, []) == []


def test_missing_or_malformed_parts_list_defaults_everything_to_enclosure():
    mech = {"placements": [{"part_id": "mcu_1"}]}
    assert ms.group_into_sections(mech, None) == [
        {"section_id": "Enclosure", "subsection_ids": ["mcu_1"]},
    ]
    # Non-dict / id-less entries in `parts` are ignored, not raised on.
    assert ms.group_into_sections(mech, ["not a dict", {"category": "mcu"}]) == [
        {"section_id": "Enclosure", "subsection_ids": ["mcu_1"]},
    ]


def test_does_not_mutate_input_mech_or_parts():
    mech = {"placements": [{"part_id": "mcu_1"}, {"part_id": "mount_mcu_1"}]}
    parts = [{"id": "mcu_1", "category": "mcu"}]
    original_mech = {"placements": list(mech["placements"])}
    original_parts = [dict(p) for p in parts]
    ms.group_into_sections(mech, parts)
    assert mech == original_mech
    assert parts == original_parts
    assert "sections" not in mech


# ---------------------------------------------------------------------------
# subsections_for_section
# ---------------------------------------------------------------------------

def test_subsections_for_section_resolves_ids_back_to_subsection_dicts():
    mech = {"placements": [
        {"part_id": "mcu_1"},
        {"part_id": "mount_mcu_1"},
        {"part_id": "radio_1"},
    ]}
    section = {"section_id": "Compute", "subsection_ids": ["mcu_1", "radio_1"]}
    resolved = ms.subsections_for_section(mech, section)
    assert resolved == [
        {"subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"]},
        {"subsection_id": "radio_1", "member_ids": ["radio_1"]},
    ]


def test_subsections_for_section_skips_unresolvable_subsection_id():
    mech = {"placements": [{"part_id": "mcu_1"}]}
    section = {"section_id": "Compute", "subsection_ids": ["mcu_1", "nonexistent"]}
    resolved = ms.subsections_for_section(mech, section)
    assert [s["subsection_id"] for s in resolved] == ["mcu_1"]


def test_subsections_for_section_handles_empty_section():
    assert ms.subsections_for_section({"placements": []}, {}) == []
    assert ms.subsections_for_section({"placements": []}, None) == []


# ---------------------------------------------------------------------------
# apply_section_grouping
# ---------------------------------------------------------------------------

def test_apply_section_grouping_mutates_and_returns_same_list():
    mech = {"placements": [{"part_id": "battery_1"}]}
    parts = [{"id": "battery_1", "category": "power"}]
    returned = ms.apply_section_grouping(mech, parts)
    assert mech["sections"] == returned
    assert returned == [{"section_id": "Power", "subsection_ids": ["battery_1"]}]


def test_apply_section_grouping_is_idempotent():
    mech = {"placements": [{"part_id": "battery_1"}]}
    parts = [{"id": "battery_1", "category": "power"}]
    first = ms.apply_section_grouping(mech, parts)
    second = ms.apply_section_grouping(mech, parts)
    assert first == second
