"""
tests/unit/test_mech_section_pool.py — G3h (Master Guide, "G3/G4.
Hierarchical parallel build + validate", Level 2->3 "Sections", LLM path,
missing test coverage for G3f-1): covers agents/mech_section_pool.py --

  - _default_offset_for_index()'s "line up beside the anchor along x"
    fallback shape
  - _parse_section_offsets()'s parsing/clamping and per-subsection
    fallback-to-default behavior on garbage/missing/malformed LLM output
  - _pick_anchor()'s "largest footprint volume wins, ties broken
    alphabetically" selection
  - run()'s end-to-end wiring: only sections with 2+ checkable
    subsections get an LLM call, every non-anchor subsection's members
    get shifted by (anchor's absolute footprint origin + proposed
    offset) - its own current absolute position, and a spec with no
    multi-subsection section never calls the worker pool at all
  - regenerate_section() (G3f-1's already-built `regenerate_node_fn` for
    Level 2->3, not yet wired into eo/mech_repair.py by G3f-2): re-derives
    the section from `mech`+`parts`, feeds the violation back as context,
    and raises for an unknown node_id or a section with fewer than 2
    checkable subsections

Mirrors tests/unit/test_mech_subsection_pool.py's own shape one level up.
Uses the same mock_llm/fake_bus fixtures as the rest of this tree
(tests/conftest.py) -- no real network calls.
"""
import pytest

import agents.hardware_speccer  # noqa: F401 -- ensure importable before mech_section_pool's lazy imports
import agents.mech_section_pool as msp

# ---------------------------------------------------------------------------
# _default_offset_for_index
# ---------------------------------------------------------------------------

def test_default_offset_for_index_lines_subsections_up_along_x():
    anchor_footprint = {"w": 20}
    assert msp._default_offset_for_index(0, anchor_footprint) == {"x": 20.0, "y": 0.0, "z": 0.0}
    assert msp._default_offset_for_index(1, anchor_footprint) == {"x": 40.0, "y": 0.0, "z": 0.0}


def test_default_offset_for_index_falls_back_when_anchor_has_no_width():
    assert msp._default_offset_for_index(0, {}) == {"x": 10.0, "y": 0.0, "z": 0.0}
    assert msp._default_offset_for_index(0, None) == {"x": 10.0, "y": 0.0, "z": 0.0}


# ---------------------------------------------------------------------------
# _parse_section_offsets
# ---------------------------------------------------------------------------

def test_parse_section_offsets_passes_through_reasonable_values():
    raw = '{"subsection_offsets": {"sensor_2": {"x": 30, "y": 5, "z": 0}}}'
    footprints_by_id = {"sensor_1": {"w": 10, "h": 10, "d": 10}, "sensor_2": {"w": 10, "h": 10, "d": 10}}
    offsets = msp._parse_section_offsets(raw, ["sensor_2"], footprints_by_id, {"w": 10, "h": 10, "d": 10})
    assert offsets == {"sensor_2": {"x": 30.0, "y": 5.0, "z": 0.0}}


def test_parse_section_offsets_falls_back_per_subsection_on_garbage_json():
    anchor_footprint = {"w": 10, "h": 10, "d": 10}
    offsets = msp._parse_section_offsets("not json at all", ["a"], {}, anchor_footprint)
    assert offsets == {"a": msp._default_offset_for_index(0, anchor_footprint)}


def test_parse_section_offsets_falls_back_when_subsection_offsets_key_missing():
    anchor_footprint = {"w": 10, "h": 10, "d": 10}
    offsets = msp._parse_section_offsets('{"something_else": 1}', ["a"], {}, anchor_footprint)
    assert offsets == {"a": msp._default_offset_for_index(0, anchor_footprint)}


def test_parse_section_offsets_falls_back_only_for_the_missing_subsection():
    # Two non-anchor subsections; the LLM response only covers one of
    # them -- the other still gets its own positional default, the whole
    # response isn't discarded.
    raw = '{"subsection_offsets": {"a": {"x": 5, "y": 5, "z": 0}}}'
    anchor_footprint = {"w": 10, "h": 10, "d": 10}
    footprints_by_id = {"a": {"w": 10, "h": 10, "d": 10}, "b": {"w": 10, "h": 10, "d": 10}}
    offsets = msp._parse_section_offsets(raw, ["a", "b"], footprints_by_id, anchor_footprint)
    assert offsets["a"] == {"x": 5.0, "y": 5.0, "z": 0.0}
    assert offsets["b"] == msp._default_offset_for_index(1, anchor_footprint)


def test_parse_section_offsets_strips_markdown_fences():
    raw = '```json\n{"subsection_offsets": {"a": {"x": 0, "y": 5, "z": 0}}}\n```'
    footprints_by_id = {"a": {"w": 10, "h": 10, "d": 10}}
    offsets = msp._parse_section_offsets(raw, ["a"], footprints_by_id, {"w": 10, "h": 10, "d": 10})
    assert offsets == {"a": {"x": 0.0, "y": 5.0, "z": 0.0}}


def test_parse_section_offsets_clamps_runaway_values():
    raw = '{"subsection_offsets": {"a": {"x": 999999, "y": -999999, "z": 0}}}'
    footprints_by_id = {"a": {"w": 10, "h": 10, "d": 10}}
    offsets = msp._parse_section_offsets(raw, ["a"], footprints_by_id, {"w": 10, "h": 10, "d": 10})
    max_extent = 10 * msp._MAX_OFFSET_MULTIPLIER
    assert offsets["a"]["x"] == max_extent
    assert offsets["a"]["y"] == -max_extent


# ---------------------------------------------------------------------------
# _pick_anchor
# ---------------------------------------------------------------------------

def test_pick_anchor_picks_largest_footprint_volume():
    footprints_by_id = {
        "sensor_1": {"w": 10, "h": 10, "d": 5},
        "sensor_2": {"w": 30, "h": 10, "d": 5},
    }
    assert msp._pick_anchor(["sensor_1", "sensor_2"], footprints_by_id) == "sensor_2"


def test_pick_anchor_breaks_ties_alphabetically():
    footprints_by_id = {
        "a": {"w": 10, "h": 10, "d": 10},
        "b": {"w": 10, "h": 10, "d": 10},
    }
    assert msp._pick_anchor(["b", "a"], footprints_by_id) == "b"


def test_pick_anchor_treats_missing_footprint_as_zero_volume():
    footprints_by_id = {"sensor_1": {"w": 10, "h": 10, "d": 5}}
    assert msp._pick_anchor(["sensor_1", "sensor_2"], footprints_by_id) == "sensor_1"


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------

def _two_sensor_spec():
    return {"mech": {
        "placements": [
            {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5},
            {"part_id": "sensor_2", "x": 100, "y": 100, "z": 0, "w": 15, "h": 10, "d": 5},
        ],
        "subsections": [
            {"subsection_id": "sensor_1", "member_ids": ["sensor_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5}},
            {"subsection_id": "sensor_2", "member_ids": ["sensor_2"],
             "footprint": {"x": 100, "y": 100, "z": 0, "w": 15, "h": 10, "d": 5}},
        ],
    }}


def _two_sensor_parts():
    return [{"id": "sensor_1", "category": "sensor"}, {"id": "sensor_2", "category": "sensor"}]


def test_run_shifts_non_anchor_subsection_members_by_proposed_offset(mock_llm):
    # sensor_2 wins the anchor tie-break (equal volume, alphabetically
    # last) -- see test_pick_anchor_breaks_ties_alphabetically above --
    # so sensor_1 is the one placed relative to it.
    mock_llm.set_json_response({"subsection_offsets": {"sensor_1": {"x": 40, "y": 0, "z": 0}}})
    spec = _two_sensor_spec()

    result = msp.run(spec, _two_sensor_parts(), session_id="s1", key_override="fake_key")

    placements_by_id = {p["part_id"]: p for p in result["mech"]["placements"]}
    # anchor's footprint origin (100, 100, 0) + proposed offset (40, 0, 0)
    assert placements_by_id["sensor_1"]["x"] == 140.0
    assert placements_by_id["sensor_1"]["y"] == 100.0
    assert placements_by_id["sensor_1"]["z"] == 0.0
    # Anchor itself is untouched.
    assert placements_by_id["sensor_2"]["x"] == 100


def test_run_skips_sections_with_fewer_than_two_checkable_subsections(mock_llm):
    spec = {"mech": {
        "placements": [{"part_id": "battery_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10}],
        "subsections": [{"subsection_id": "battery_1", "member_ids": ["battery_1"],
                          "footprint": {"x": 0, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10}}],
    }}
    parts = [{"id": "battery_1", "category": "power"}]

    result = msp.run(spec, parts, session_id="s1", key_override="fake_key")

    mock_llm.mock.assert_not_called()
    assert result["mech"]["placements"][0]["x"] == 0


def test_run_skips_subsections_with_no_footprint_yet(mock_llm):
    # Level 1->2 repair hasn't settled a footprint for sensor_2 yet --
    # this pass has nothing safe to place it relative to, so the section
    # is treated as not-yet-checkable, same as the single-subsection case.
    spec = {"mech": {
        "placements": [
            {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5},
            {"part_id": "sensor_2", "x": 100, "y": 100, "z": 0, "w": 15, "h": 10, "d": 5},
        ],
        "subsections": [
            {"subsection_id": "sensor_1", "member_ids": ["sensor_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5}},
            {"subsection_id": "sensor_2", "member_ids": ["sensor_2"]},
        ],
    }}

    msp.run(spec, _two_sensor_parts(), session_id="s1", key_override="fake_key")

    mock_llm.mock.assert_not_called()


def test_run_falls_back_to_default_offset_when_every_provider_fails(mock_llm):
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
    spec = _two_sensor_spec()

    result = msp.run(spec, _two_sensor_parts(), session_id="s1", key_override="fake_key")

    placements_by_id = {p["part_id"]: p for p in result["mech"]["placements"]}
    # anchor's footprint origin (100, 100, 0) + default fallback offset
    # for index 0 against a 15mm-wide anchor (15.0, 0, 0)
    assert placements_by_id["sensor_1"]["x"] == 115.0
    assert placements_by_id["sensor_1"]["y"] == 100.0


def test_run_is_a_noop_when_mech_has_no_placements_list(mock_llm):
    spec = {"mech": {}}
    result = msp.run(spec, [], session_id="s1", key_override="fake_key")
    mock_llm.mock.assert_not_called()
    assert result == spec


# ---------------------------------------------------------------------------
# regenerate_section (G3f-1's already-built Level 2->3 regenerate_node_fn,
# not yet wired into eo/mech_repair.py by G3f-2)
# ---------------------------------------------------------------------------

def test_regenerate_section_repositions_non_anchor_subsection_from_llm_response(mock_llm):
    mock_llm.set_json_response({"subsection_offsets": {"sensor_1": {"x": 40, "y": 0, "z": 0}}})
    mech = _two_sensor_spec()["mech"]
    violation = {"issue": "sensor_1 and sensor_2 bounding boxes overlap"}

    msp.regenerate_section(mech, "Sensing", violation, 1, _two_sensor_parts(),
                            key_override="fake_key", session_id="s1")

    placements_by_id = {p["part_id"]: p for p in mech["placements"]}
    assert placements_by_id["sensor_1"]["x"] == 140.0  # anchor x (100) + offset x (40)
    assert placements_by_id["sensor_1"]["y"] == 100.0
    assert placements_by_id["sensor_2"]["x"] == 100  # anchor untouched


def test_regenerate_section_feeds_violation_back_as_context(mock_llm):
    mock_llm.set_json_response({"subsection_offsets": {"sensor_1": {"x": 40, "y": 0, "z": 0}}})
    mech = _two_sensor_spec()["mech"]
    violation = {"issue": "sensor_1 and sensor_2 bounding boxes overlap"}

    msp.regenerate_section(mech, "Sensing", violation, 1, _two_sensor_parts(),
                            key_override="fake_key", session_id="s1")

    user_content = mock_llm.mock.call_args.args[1]
    assert "bounding boxes overlap" in user_content


def test_regenerate_section_raises_for_unknown_node_id(mock_llm):
    mech = _two_sensor_spec()["mech"]
    with pytest.raises(ValueError):
        msp.regenerate_section(mech, "nonexistent", {"issue": "x"}, 1, _two_sensor_parts(),
                                key_override="fake_key")


def test_regenerate_section_raises_when_fewer_than_two_checkable_subsections(mock_llm):
    mech = {
        "placements": [{"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5}],
        "subsections": [{"subsection_id": "mcu_1", "member_ids": ["mcu_1"],
                          "footprint": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5}}],
    }
    parts = [{"id": "mcu_1", "category": "mcu"}]
    with pytest.raises(ValueError):
        msp.regenerate_section(mech, "Compute", {"issue": "x"}, 1, parts, key_override="fake_key")


def test_regenerate_section_falls_back_to_default_when_provider_fails(mock_llm):
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
    mech = _two_sensor_spec()["mech"]

    msp.regenerate_section(mech, "Sensing", {"issue": "collision"}, 1, _two_sensor_parts(),
                            key_override="fake_key", session_id="s1")

    placements_by_id = {p["part_id"]: p for p in mech["placements"]}
    # anchor's footprint origin (100) + default fallback offset (15.0)
    assert placements_by_id["sensor_1"]["x"] == 115.0
