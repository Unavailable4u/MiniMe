"""
tests/unit/test_mech_subsection_pool.py — G3e-2 (Master Guide, "G3/G4.
Hierarchical parallel build + validate", Level 1->2 "Subsections", LLM
path): covers agents/mech_subsection_pool.py --

  - _parse_relative_offset()'s parsing/clamping and fallback-to-default
    behavior on garbage/empty/malformed LLM output
  - _default_relative_offset()'s "directly below" shape
  - _needs_llm_relative_placement()'s gating: singleton -> False,
    mount_spec-resized (rect/cc) -> False, thread-pattern or no
    mount_spec -> True, unknown BOM part -> True (don't skip what you
    can't confirm is already grounded)
  - run()'s end-to-end wiring: only in-scope subsections get an LLM
    call, mount x/y/z gets recomputed from the part's own position plus
    the proposed offset, and a no-targets spec never calls the worker
    pool at all

Uses the same mock_llm/fake_bus fixtures as the rest of this tree
(tests/conftest.py) -- no real network calls.
"""
import pytest

import agents.hardware_speccer  # noqa: F401 -- ensure importable before mech_subsection_pool's lazy imports
import agents.mech_subsection_pool as msp

# ---------------------------------------------------------------------------
# _parse_relative_offset / _default_relative_offset
# ---------------------------------------------------------------------------

def test_default_relative_offset_places_mount_directly_below():
    assert msp._default_relative_offset(20) == {"x": 0.0, "y": 20.0, "z": 0.0}


def test_parse_relative_offset_passes_through_reasonable_values():
    raw = '{"mount_offset": {"x": 1.5, "y": 12, "z": 0}}'
    offset = msp._parse_relative_offset(raw, 20, 20, 20)
    assert offset == {"x": 1.5, "y": 12.0, "z": 0.0}


def test_parse_relative_offset_falls_back_on_garbage_json():
    offset = msp._parse_relative_offset("not json at all", 10, 20, 10)
    assert offset == msp._default_relative_offset(20)


def test_parse_relative_offset_falls_back_on_missing_key():
    offset = msp._parse_relative_offset('{"something_else": 1}', 10, 20, 10)
    assert offset == msp._default_relative_offset(20)


def test_parse_relative_offset_strips_markdown_fences():
    raw = '```json\n{"mount_offset": {"x": 0, "y": 5, "z": 0}}\n```'
    offset = msp._parse_relative_offset(raw, 10, 10, 10)
    assert offset == {"x": 0.0, "y": 5.0, "z": 0.0}


def test_parse_relative_offset_clamps_runaway_values():
    raw = '{"mount_offset": {"x": 999999, "y": -999999, "z": 0}}'
    offset = msp._parse_relative_offset(raw, 10, 10, 10)
    max_extent = 10 * msp._MAX_OFFSET_MULTIPLIER
    assert offset["x"] == max_extent
    assert offset["y"] == -max_extent


# ---------------------------------------------------------------------------
# _needs_llm_relative_placement
# ---------------------------------------------------------------------------

def test_needs_llm_false_for_singleton_subsection():
    subsection = {"subsection_id": "battery_1", "member_ids": ["battery_1"]}
    assert msp._needs_llm_relative_placement(subsection, {}) is False


def test_needs_llm_true_when_no_mount_spec_at_all():
    subsection = {"subsection_id": "sensor_1", "member_ids": ["sensor_1", "mount_sensor_1"]}
    parts_by_id = {"sensor_1": {"id": "sensor_1", "category": "sensor"}}
    assert msp._needs_llm_relative_placement(subsection, parts_by_id) is True


def test_needs_llm_false_when_mount_spec_already_resized_rect_pattern():
    subsection = {"subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"]}
    parts_by_id = {"mcu_1": {"id": "mcu_1", "mount_spec": "4-hole 48x18mm rectangular pattern, M2"}}
    assert msp._needs_llm_relative_placement(subsection, parts_by_id) is False


def test_needs_llm_true_for_thread_pattern_mount_spec():
    subsection = {"subsection_id": "buzzer_1", "member_ids": ["buzzer_1", "mount_buzzer_1"]}
    parts_by_id = {"buzzer_1": {"id": "buzzer_1", "mount_spec": "M3 threaded boss"}}
    assert msp._needs_llm_relative_placement(subsection, parts_by_id) is True


def test_needs_llm_true_when_anchor_part_missing_from_bom():
    subsection = {"subsection_id": "ghost_1", "member_ids": ["ghost_1", "mount_ghost_1"]}
    assert msp._needs_llm_relative_placement(subsection, {}) is True


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------

def test_run_places_mount_relative_to_part(mock_llm):
    mock_llm.set_json_response({"mount_offset": {"x": 0, "y": 20, "z": 0}})
    spec = {"mech": {"placements": [
        {"part_id": "sensor_1", "x": 10, "y": 5, "z": 0, "w": 20, "h": 15, "d": 5},
        {"part_id": "mount_sensor_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 5, "d": 5},
        # Singleton -- never touched by this pool.
        {"part_id": "battery_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},
    ]}}
    parts = [
        {"id": "sensor_1", "category": "sensor"},
        {"id": "mount_sensor_1", "category": "3D_PRINT"},
        {"id": "battery_1", "category": "power"},
    ]

    result = msp.run(spec, parts, session_id="s1")

    placements_by_id = {p["part_id"]: p for p in result["mech"]["placements"]}
    mount = placements_by_id["mount_sensor_1"]
    # part's own x/y/z (10, 5, 0) + proposed offset (0, 20, 0)
    assert mount["x"] == 10
    assert mount["y"] == 25
    assert mount["z"] == 0
    # Untouched.
    assert placements_by_id["battery_1"]["x"] == 0
    assert placements_by_id["battery_1"]["y"] == 0


def test_run_skips_subsection_already_grounded_by_mount_spec(mock_llm):
    spec = {"mech": {"placements": [
        {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5},
        {"part_id": "mount_mcu_1", "x": 5, "y": 5, "z": 0, "w": 40, "h": 5, "d": 5},
    ]}}
    parts = [{"id": "mcu_1", "category": "mcu", "mount_spec": "4-hole 48x18mm rectangular pattern, M2"}]

    msp.run(spec, parts, session_id="s1")

    mock_llm.mock.assert_not_called()


def test_run_is_a_noop_when_only_singletons_present(mock_llm):
    spec = {"mech": {"placements": [{"part_id": "battery_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10}]}}
    parts = [{"id": "battery_1", "category": "power"}]

    result = msp.run(spec, parts, session_id="s1")

    mock_llm.mock.assert_not_called()
    assert result["mech"]["placements"][0]["x"] == 0


def test_run_falls_back_to_default_offset_when_every_provider_fails(mock_llm):
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
    spec = {"mech": {"placements": [
        {"part_id": "sensor_1", "x": 10, "y": 5, "z": 0, "w": 20, "h": 15, "d": 5},
        {"part_id": "mount_sensor_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 5, "d": 5},
    ]}}
    parts = [
        {"id": "sensor_1", "category": "sensor"},
        {"id": "mount_sensor_1", "category": "3D_PRINT"},
    ]

    result = msp.run(spec, parts, session_id="s1")

    placements_by_id = {p["part_id"]: p for p in result["mech"]["placements"]}
    mount = placements_by_id["mount_sensor_1"]
    # part's own y (5) + default fallback offset (part_h = 15)
    assert mount["y"] == 20
    assert mount["x"] == 10


# ---------------------------------------------------------------------------
# regenerate_subsection (G3e-4, this patch) -- the Level 1->2
# regenerate_node_fn eo/mech_repair.py's run_repair_loop() calls
# ---------------------------------------------------------------------------

def test_regenerate_subsection_repositions_mount_from_llm_response(mock_llm):
    mock_llm.set_json_response({"mount_offset": {"x": 0, "y": 22, "z": 0}})
    mech = {"placements": [
        {"part_id": "mcu_1", "x": 10, "y": 5, "z": 0, "w": 30, "h": 20, "d": 5},
        {"part_id": "mount_mcu_1", "x": 10, "y": 12, "z": 0, "w": 30, "h": 5, "d": 5},
    ]}
    violation = {"node_id": "mcu_1", "issue": "part and mount collide (~9.0 mm^3 overlap)"}

    msp.regenerate_subsection(mech, "mcu_1", violation, 1, key_override="fake_key", session_id="s1")

    placements_by_id = {p["part_id"]: p for p in mech["placements"]}
    mount = placements_by_id["mount_mcu_1"]
    assert mount["x"] == 10  # part's own x (10) + offset x (0)
    assert mount["y"] == 27  # part's own y (5) + offset y (22)
    assert mount["z"] == 0


def test_regenerate_subsection_feeds_violation_back_as_context(mock_llm):
    mock_llm.set_json_response({"mount_offset": {"x": 0, "y": 25, "z": 0}})
    mech = {"placements": [
        {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5},
        {"part_id": "mount_mcu_1", "x": 0, "y": 10, "z": 0, "w": 30, "h": 5, "d": 5},
    ]}
    violation = {"node_id": "mcu_1", "issue": "part and mount collide (~9.0 mm^3 overlap)"}

    msp.regenerate_subsection(mech, "mcu_1", violation, 1, key_override="fake_key", session_id="s1")

    user_content = mock_llm.mock.call_args.args[1]
    assert "part and mount collide" in user_content


def test_regenerate_subsection_raises_for_unknown_node_id(mock_llm):
    mech = {"placements": [{"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5}]}
    with pytest.raises(ValueError):
        msp.regenerate_subsection(mech, "nonexistent", {"issue": "x"}, 1, key_override="fake_key")


def test_regenerate_subsection_raises_when_no_mount_sibling(mock_llm):
    mech = {"placements": [{"part_id": "battery_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10}]}
    with pytest.raises(ValueError):
        msp.regenerate_subsection(mech, "battery_1", {"issue": "x"}, 1, key_override="fake_key")


def test_regenerate_subsection_falls_back_to_default_when_provider_fails(mock_llm):
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
    mech = {"placements": [
        {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5},
        {"part_id": "mount_mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 5, "d": 5},
    ]}
    msp.regenerate_subsection(mech, "mcu_1", {"issue": "collision"}, 1, key_override="fake_key")

    placements_by_id = {p["part_id"]: p for p in mech["placements"]}
    # part's own y (0) + default fallback offset (part_h = 20)
    assert placements_by_id["mount_mcu_1"]["y"] == 20
