"""
tests/unit/test_mech_primitive_pool.py — G3b (Master Guide, "G3/G4.
Hierarchical parallel build + validate", Level 0->1 primitive
composition, LLM path): covers agents/mech_primitive_pool.py --

  - _clamp_primitive()'s validation/clamping of one LLM-proposed
    primitive against its part's own bounding box
  - _parse_primitives()'s fallback-to-box behavior on garbage/empty/
    malformed LLM output, and its _MAX_PRIMITIVES cap
  - _needs_llm_primitives()'s gating: electrical + no dimensions_mm +
    no existing primitives -> True; anything else -> False
  - run()'s end-to-end wiring: only uncovered placements get a
    `primitives` key, everything else is left untouched, and a
    no-uncovered-parts spec never calls the worker pool at all

Uses the same mock_llm/fake_bus fixtures as the rest of this tree
(tests/conftest.py) -- no real network/E2B/FreeCAD calls.
"""
import pytest

import agents.hardware_speccer  # noqa: F401 -- ensure importable before mech_primitive_pool's lazy imports
import agents.mech_primitive_pool as mpp


# ---------------------------------------------------------------------------
# _clamp_primitive
# ---------------------------------------------------------------------------

def test_clamp_primitive_passes_through_when_already_in_bounds():
    raw = {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 10, "h": 10, "d": 10},
           "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}
    clamped = mpp._clamp_primitive(raw, 20, 20, 20)
    assert clamped["size"] == {"w": 10, "h": 10, "d": 10}
    assert clamped["shape"] == "cylinder"
    assert clamped["color_role"] == "primary"


def test_clamp_primitive_pulls_oversized_size_back_inside_bounding_box():
    raw = {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 999, "h": 999, "d": 999}}
    clamped = mpp._clamp_primitive(raw, 20, 15, 10)
    assert clamped["size"]["w"] <= 20
    assert clamped["size"]["h"] <= 15
    assert clamped["size"]["d"] <= 10


def test_clamp_primitive_pulls_negative_and_overshooting_offset_back_inside():
    raw = {"offset": {"x": -5, "y": 500, "z": 0}, "size": {"w": 5, "h": 5, "d": 5}}
    clamped = mpp._clamp_primitive(raw, 20, 20, 20)
    assert clamped["offset"]["x"] >= 0
    assert clamped["offset"]["y"] + clamped["size"]["h"] <= 20 + 1e-9


def test_clamp_primitive_defaults_invalid_shape_and_color_role():
    raw = {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 5, "h": 5, "d": 5},
           "shape": "sphere", "color_role": "loud"}
    clamped = mpp._clamp_primitive(raw, 20, 20, 20)
    assert clamped["shape"] == "box"
    assert clamped["color_role"] == "primary"


def test_clamp_primitive_returns_none_for_missing_offset_or_size():
    assert mpp._clamp_primitive({"shape": "box"}, 20, 20, 20) is None
    assert mpp._clamp_primitive("not a dict", 20, 20, 20) is None


# ---------------------------------------------------------------------------
# _parse_primitives
# ---------------------------------------------------------------------------

def test_parse_primitives_falls_back_to_box_on_garbage_json():
    primitives = mpp._parse_primitives("not json at all", 10, 10, 10)
    assert len(primitives) == 1
    assert primitives[0]["shape"] == "box"
    assert primitives[0]["size"] == {"w": 10, "h": 10, "d": 10}


def test_parse_primitives_falls_back_to_box_on_empty_primitives_list():
    primitives = mpp._parse_primitives('{"primitives": []}', 8, 8, 8)
    assert len(primitives) == 1
    assert primitives[0]["shape"] == "box"


def test_parse_primitives_strips_markdown_fences():
    raw = '```json\n{"primitives": [{"offset": {"x":0,"y":0,"z":0}, "size": {"w":4,"h":4,"d":4}, "shape": "cylinder"}]}\n```'
    primitives = mpp._parse_primitives(raw, 10, 10, 10)
    assert len(primitives) == 1
    assert primitives[0]["shape"] == "cylinder"


def test_parse_primitives_caps_at_max_primitives():
    raw_primitives = [
        {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 1, "h": 1, "d": 1}, "shape": "box"}
        for _ in range(10)
    ]
    import json
    primitives = mpp._parse_primitives(json.dumps({"primitives": raw_primitives}), 20, 20, 20)
    assert len(primitives) == mpp._MAX_PRIMITIVES


# ---------------------------------------------------------------------------
# _needs_llm_primitives
# ---------------------------------------------------------------------------

def test_needs_llm_primitives_true_for_uncovered_electrical_part():
    placement = {"part_id": "motor_1", "w": 20, "h": 20, "d": 20}
    parts_by_id = {"motor_1": {"id": "motor_1", "category": "actuator"}}
    assert mpp._needs_llm_primitives(placement, parts_by_id) is True


def test_needs_llm_primitives_false_when_dimensions_mm_already_resolved():
    placement = {"part_id": "motor_1"}
    parts_by_id = {"motor_1": {"id": "motor_1", "category": "actuator",
                                "dimensions_mm": {"w": 28, "h": 19, "d": 19}}}
    assert mpp._needs_llm_primitives(placement, parts_by_id) is False


def test_needs_llm_primitives_false_when_primitives_already_present():
    placement = {"part_id": "motor_1", "primitives": [{"shape": "box"}]}
    parts_by_id = {"motor_1": {"id": "motor_1", "category": "actuator"}}
    assert mpp._needs_llm_primitives(placement, parts_by_id) is False


def test_needs_llm_primitives_false_for_mechanical_enclosure_part():
    placement = {"part_id": "mount_mcu_1"}
    parts_by_id = {"mount_mcu_1": {"id": "mount_mcu_1", "category": "3D_PRINT"}}
    assert mpp._needs_llm_primitives(placement, parts_by_id) is False


def test_needs_llm_primitives_false_for_unknown_part_id():
    placement = {"part_id": "ghost_1"}
    assert mpp._needs_llm_primitives(placement, {}) is False


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------

def test_run_composes_only_uncovered_placements(mock_llm):
    mock_llm.set_json_response({
        "primitives": [
            {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 20, "h": 19, "d": 19},
             "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}
        ]
    })
    spec = {
        "mech": {
            "placements": [
                # Already covered by G3a -- untouched.
                {"part_id": "board_1", "w": 30, "h": 30, "d": 5, "primitives": [{"shape": "box"}]},
                # Uncovered electrical part -- gets composed.
                {"part_id": "motor_1", "w": 20, "h": 19, "d": 19},
                # Mechanical part -- never in scope for this pool.
                {"part_id": "mount_1", "w": 25, "h": 25, "d": 5},
            ]
        }
    }
    parts = [
        {"id": "board_1", "category": "mcu", "dimensions_mm": {"w": 30, "h": 30, "d": 5}},
        {"id": "motor_1", "category": "actuator", "generic_name": "DC motor"},
        {"id": "mount_1", "category": "3D_PRINT"},
    ]

    result = mpp.run(spec, parts, session_id="s1")

    placements_by_id = {p["part_id"]: p for p in result["mech"]["placements"]}
    assert placements_by_id["board_1"]["primitives"] == [{"shape": "box"}]
    assert "primitives" in placements_by_id["motor_1"]
    assert placements_by_id["motor_1"]["primitives"][0]["shape"] == "cylinder"
    assert "primitives" not in placements_by_id["mount_1"]


def test_run_is_a_noop_when_nothing_is_uncovered(mock_llm):
    spec = {"mech": {"placements": [
        {"part_id": "board_1", "w": 30, "h": 30, "d": 5, "primitives": [{"shape": "box"}]},
    ]}}
    parts = [{"id": "board_1", "category": "mcu", "dimensions_mm": {"w": 30, "h": 30, "d": 5}}]

    mpp.run(spec, parts, session_id="s1")

    # No LLM call should have happened -- everything was already covered.
    mock_llm.mock.assert_not_called()


def test_run_falls_back_to_box_when_every_provider_fails(mock_llm):
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
    spec = {"mech": {"placements": [{"part_id": "motor_1", "w": 20, "h": 19, "d": 19}]}}
    parts = [{"id": "motor_1", "category": "actuator"}]

    result = mpp.run(spec, parts, session_id="s1")

    placement = result["mech"]["placements"][0]
    assert placement["primitives"][0]["shape"] == "box"
    assert placement["primitives"][0]["size"] == {"w": 20, "h": 19, "d": 19}


# ---------------------------------------------------------------------------
# regenerate_primitives -- the Level 0->1 regenerate_node_fn
# eo/mech_repair.py's run_repair_loop() calls (closes the gap left open
# through G3i: Level 0->1's own repair pass was never actually driven).
# ---------------------------------------------------------------------------

def test_regenerate_primitives_composes_new_primitives_from_llm_response(mock_llm):
    mock_llm.set_json_response({
        "primitives": [
            {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 20, "h": 19, "d": 19},
             "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}
        ]
    })
    mech = {"placements": [
        {"part_id": "motor_1", "w": 20, "h": 19, "d": 19,
         "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 999, "h": 19, "d": 19},
                          "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]},
    ]}
    parts_by_id = {"motor_1": {"id": "motor_1", "category": "actuator", "generic_name": "DC motor"}}
    violation = {"node_id": "motor_1", "issue": "primitive(s) extend outside the part's own bounding box"}

    mpp.regenerate_primitives(mech, "motor_1", violation, 1, parts_by_id, key_override="fake_key", session_id="s1")

    placement = mech["placements"][0]
    assert placement["primitives"][0]["shape"] == "cylinder"
    assert placement["primitives"][0]["size"] == {"w": 20, "h": 19, "d": 19}


def test_regenerate_primitives_feeds_violation_back_as_context(mock_llm):
    mock_llm.set_json_response({
        "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 20, "h": 19, "d": 19},
                         "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]
    })
    mech = {"placements": [{"part_id": "motor_1", "w": 20, "h": 19, "d": 19, "primitives": [{"shape": "box"}]}]}
    parts_by_id = {"motor_1": {"id": "motor_1", "category": "actuator"}}
    violation = {"node_id": "motor_1", "issue": "primitive(s) extend outside the part's own bounding box"}

    mpp.regenerate_primitives(mech, "motor_1", violation, 1, parts_by_id, key_override="fake_key", session_id="s1")

    user_content = mock_llm.mock.call_args.args[1]
    assert "extend outside the part's own bounding box" in user_content


def test_regenerate_primitives_raises_for_unknown_node_id(mock_llm):
    mech = {"placements": [{"part_id": "motor_1", "w": 20, "h": 19, "d": 19, "primitives": [{"shape": "box"}]}]}
    with pytest.raises(ValueError):
        mpp.regenerate_primitives(mech, "nonexistent", {"issue": "x"}, 1, {}, key_override="fake_key")


def test_regenerate_primitives_falls_back_to_box_when_every_provider_fails(mock_llm):
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
    mech = {"placements": [{"part_id": "motor_1", "w": 20, "h": 19, "d": 19, "primitives": [{"shape": "box"}]}]}
    parts_by_id = {"motor_1": {"id": "motor_1", "category": "actuator"}}

    mpp.regenerate_primitives(mech, "motor_1", {"issue": "x"}, 1, parts_by_id, key_override="fake_key", session_id="s1")

    placement = mech["placements"][0]
    assert placement["primitives"][0]["shape"] == "box"
    assert placement["primitives"][0]["size"] == {"w": 20, "h": 19, "d": 19}


def test_regenerate_primitives_tolerates_unknown_part_id_in_parts_by_id(mock_llm):
    # A part missing from `parts_by_id` (shouldn't happen in practice)
    # degrades to an empty part dict rather than crashing -- same
    # fail-safe posture regenerate_subsection() already uses one level
    # up for its own lookups.
    mock_llm.set_json_response({
        "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 20, "h": 19, "d": 19},
                         "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]
    })
    mech = {"placements": [{"part_id": "motor_1", "w": 20, "h": 19, "d": 19, "primitives": [{"shape": "box"}]}]}

    mpp.regenerate_primitives(mech, "motor_1", {"issue": "x"}, 1, {}, key_override="fake_key", session_id="s1")

    assert mech["placements"][0]["primitives"][0]["shape"] == "box"
