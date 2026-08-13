"""
tests/unit/test_mech_primitive_composition.py — G3a (Master Guide,
"G3/G4. Hierarchical parallel build + validate", Level 0->1
deterministic-first primitive composition): covers the new pure
functions added to agents/hardware_speccer.py --

  - the three Level-0 primitive templates (box/cylinder/cone)
  - _parse_mount_spec()'s three grammars (rect/cc/thread) + garbage input
  - _mount_hole_primitives()'s clamping behavior
  - _apply_primitive_composition()'s gating (dimensions_mm present vs
    absent) and its "shape defaults to box" fallback
  - _resize_mount_parts_from_mount_spec()'s resize/recenter side effect,
    and its "no sibling mount" / "thread pattern" no-op cases

No LLM calls, no FreeCAD -- these are all deterministic, synchronous
functions, so no mock_llm/fake_bus fixtures are needed beyond the
autouse ones tests/conftest.py already provides for the whole tree.
"""
import agents.hardware_speccer as hs


# ---------------------------------------------------------------------------
# Level-0 primitive templates
# ---------------------------------------------------------------------------

def test_box_template_spans_full_bounding_box():
    primitives = hs._box_primitive_template(55, 28, 13)
    assert len(primitives) == 1
    p = primitives[0]
    assert p["shape"] == "box"
    assert p["offset"] == {"x": 0, "y": 0, "z": 0}
    assert p["size"] == {"w": 55, "h": 28, "d": 13}
    assert p["color_role"] == "primary"


def test_cylinder_template_spans_full_bounding_box():
    primitives = hs._cylinder_primitive_template(28, 19, 19)
    assert len(primitives) == 1
    p = primitives[0]
    assert p["shape"] == "cylinder"
    assert p["size"] == {"w": 28, "h": 19, "d": 19}


def test_cone_template_splits_shaft_and_dome():
    primitives = hs._cone_primitive_template(12, 10, 12)
    assert len(primitives) == 2
    shaft, dome = primitives
    assert shaft["shape"] == "box"
    assert dome["shape"] == "cone"
    # Shaft starts at the part's own base.
    assert shaft["offset"] == {"x": 0, "y": 0, "z": 0}
    # Dome sits directly atop the shaft -- no gap, no overlap.
    assert dome["offset"]["y"] == shaft["size"]["h"]
    # Combined heights reconstruct the part's own overall h.
    assert shaft["size"]["h"] + dome["size"]["h"] == 10
    # Both primitives share the part's own w/d footprint.
    assert shaft["size"]["w"] == dome["size"]["w"] == 12
    assert shaft["size"]["d"] == dome["size"]["d"] == 12


def test_cone_template_never_produces_a_zero_height_piece():
    # A very short part (h=1) shouldn't produce a 0-height shaft or dome
    # -- both clamped to at least 1mm.
    primitives = hs._cone_primitive_template(6, 1, 6)
    shaft, dome = primitives
    assert shaft["size"]["h"] >= 1
    assert dome["size"]["h"] >= 1


# ---------------------------------------------------------------------------
# _parse_mount_spec
# ---------------------------------------------------------------------------

def test_parse_mount_spec_rectangular_pattern():
    parsed = hs._parse_mount_spec("4-hole 48x18mm rectangular pattern, M2")
    assert parsed == {
        "pattern": "rect",
        "hole_count": 4,
        "span_x": 48.0,
        "span_y": 18.0,
        "thread": "M2",
    }


def test_parse_mount_spec_center_to_center():
    parsed = hs._parse_mount_spec("2-hole 35mm c-c, M3")
    assert parsed == {
        "pattern": "cc",
        "hole_count": 2,
        "span": 35.0,
        "thread": "M3",
    }


def test_parse_mount_spec_center_to_center_no_thread():
    parsed = hs._parse_mount_spec("2-hole 7.6mm c-c")
    assert parsed["pattern"] == "cc"
    assert parsed["span"] == 7.6
    assert parsed["thread"] is None


def test_parse_mount_spec_thread_only():
    parsed = hs._parse_mount_spec("M3 thread")
    assert parsed == {"pattern": "thread", "thread": "M3"}


def test_parse_mount_spec_rejects_garbage_and_empty():
    assert hs._parse_mount_spec(None) is None
    assert hs._parse_mount_spec("") is None
    assert hs._parse_mount_spec("   ") is None
    assert hs._parse_mount_spec("zip-tied to the enclosure wall") is None
    assert hs._parse_mount_spec(42) is None


# ---------------------------------------------------------------------------
# _mount_hole_primitives
# ---------------------------------------------------------------------------

def test_mount_hole_primitives_thread_pattern_gives_one_centered_hole():
    parsed = {"pattern": "thread", "thread": "M3"}
    holes = hs._mount_hole_primitives(20, 20, 5, parsed)
    assert len(holes) == 1
    hole = holes[0]
    assert hole["color_role"] == "accent"
    assert hole["shape"] == "cylinder"
    # Centered: offset + radius == 10 (half of w/h).
    radius = hole["size"]["w"] / 2
    assert round(hole["offset"]["x"] + radius, 2) == 10
    assert round(hole["offset"]["y"] + radius, 2) == 10


def test_mount_hole_primitives_rect_pattern_gives_four_holes():
    parsed = {"pattern": "rect", "span_x": 48.0, "span_y": 18.0, "thread": "M2"}
    holes = hs._mount_hole_primitives(55, 28, 13, parsed)
    assert len(holes) == 4
    for hole in holes:
        assert hole["size"]["d"] == 13
        # Every hole's footprint stays inside the part's own w/h.
        assert 0 <= hole["offset"]["x"]
        assert hole["offset"]["x"] + hole["size"]["w"] <= 55
        assert 0 <= hole["offset"]["y"]
        assert hole["offset"]["y"] + hole["size"]["h"] <= 28


def test_mount_hole_primitives_cc_span_wider_than_part_gets_clamped():
    # The 28BYJ-48 case: 35mm c-c spec on a 28mm-diameter body -- the
    # span must be pulled inside the part's own w, not drawn outside it.
    parsed = {"pattern": "cc", "span": 35.0, "thread": "M3"}
    holes = hs._mount_hole_primitives(28, 19, 19, parsed)
    assert len(holes) == 2
    for hole in holes:
        assert hole["offset"]["x"] >= 0
        assert hole["offset"]["x"] + hole["size"]["w"] <= 28


def test_mount_hole_primitives_empty_when_unparsed():
    assert hs._mount_hole_primitives(10, 10, 5, None) == []


# ---------------------------------------------------------------------------
# _apply_primitive_composition
# ---------------------------------------------------------------------------

def test_apply_primitive_composition_gates_on_dimensions_mm():
    parts = [
        {"id": "mcu_1", "dimensions_mm": {"w": 55, "h": 28, "d": 13}},
        {"id": "unmatched_1"},  # no dimensions_mm at all
    ]
    spec = {
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 55, "h": 28, "d": 13, "shape": "box"},
                {"part_id": "unmatched_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 15, "d": 8},
            ]
        }
    }
    hs._apply_primitive_composition(spec, parts)
    placements_by_id = {p["part_id"]: p for p in spec["mech"]["placements"]}
    assert "primitives" in placements_by_id["mcu_1"]
    assert "primitives" not in placements_by_id["unmatched_1"]


def test_apply_primitive_composition_defaults_to_box_with_no_shape():
    # A G1b (DigiKey/Mouser) hit has dimensions_mm but no shape at all.
    parts = [{"id": "part_1", "dimensions_mm": {"w": 10, "h": 10, "d": 10}}]
    spec = {
        "mech": {
            "placements": [
                {"part_id": "part_1", "x": 0, "y": 0, "z": 0, "w": 10, "h": 10, "d": 10},
            ]
        }
    }
    hs._apply_primitive_composition(spec, parts)
    primitives = spec["mech"]["placements"][0]["primitives"]
    assert len(primitives) == 1
    assert primitives[0]["shape"] == "box"


def test_apply_primitive_composition_adds_mount_holes_when_mount_spec_parses():
    parts = [{
        "id": "mcu_1",
        "dimensions_mm": {"w": 55, "h": 28, "d": 13},
        "mount_spec": "4-hole 48x18mm rectangular pattern, M2",
    }]
    spec = {
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 55, "h": 28, "d": 13, "shape": "box"},
            ]
        }
    }
    hs._apply_primitive_composition(spec, parts)
    primitives = spec["mech"]["placements"][0]["primitives"]
    # 1 body primitive + 4 mounting holes.
    assert len(primitives) == 5
    assert sum(1 for p in primitives if p["color_role"] == "accent") == 4


def test_apply_primitive_composition_noop_on_missing_placements():
    spec = {"mech": {}}
    hs._apply_primitive_composition(spec, [])  # should not raise
    assert "placements" not in spec["mech"]


# ---------------------------------------------------------------------------
# _resize_mount_parts_from_mount_spec
# ---------------------------------------------------------------------------

def _base_spec():
    return {
        "mech": {
            "enclosure": {"w": 100, "h": 60, "d": 40},
            "placements": [
                {"part_id": "mcu_1", "x": 10, "y": 10, "z": 5, "w": 55, "h": 28, "d": 13},
                {"part_id": "mount_mcu_1", "x": 8, "y": 8, "z": 0, "w": 60, "h": 30, "d": 5},
            ],
        }
    }


def test_resize_mount_parts_rect_pattern_resizes_and_recenters():
    parts = [{
        "id": "mcu_1",
        "mount_spec": "4-hole 48x18mm rectangular pattern, M2",
    }]
    spec = _base_spec()
    hs._resize_mount_parts_from_mount_spec(spec, parts)
    placements_by_id = {p["part_id"]: p for p in spec["mech"]["placements"]}
    mount = placements_by_id["mount_mcu_1"]
    # span_x(48) + 2*margin(6) = 60, span_y(18) + 2*margin(6) = 30.
    assert mount["w"] == 60.0
    assert mount["h"] == 30.0
    # Recentered under mcu_1's own center: (10+55/2, 10+28/2) = (37.5, 24).
    assert mount["x"] == 37.5 - 30.0
    assert mount["y"] == 24.0 - 15.0


def test_resize_mount_parts_cc_pattern_only_resizes_width():
    parts = [{"id": "mcu_1", "mount_spec": "2-hole 35mm c-c, M3"}]
    spec = _base_spec()
    original_h = spec["mech"]["placements"][1]["h"]
    hs._resize_mount_parts_from_mount_spec(spec, parts)
    placements_by_id = {p["part_id"]: p for p in spec["mech"]["placements"]}
    mount = placements_by_id["mount_mcu_1"]
    assert mount["w"] == 35.0 + 2 * 6
    assert mount["h"] == original_h  # untouched -- cc spec has no h span


def test_resize_mount_parts_thread_pattern_is_a_noop():
    parts = [{"id": "mcu_1", "mount_spec": "M3 thread"}]
    spec = _base_spec()
    original = dict(spec["mech"]["placements"][1])
    hs._resize_mount_parts_from_mount_spec(spec, parts)
    placements_by_id = {p["part_id"]: p for p in spec["mech"]["placements"]}
    assert placements_by_id["mount_mcu_1"] == original


def test_resize_mount_parts_noop_without_sibling_mount():
    parts = [{"id": "sensor_1", "mount_spec": "4-hole 20x10mm rectangular pattern, M2"}]
    spec = {
        "mech": {
            "enclosure": {"w": 100, "h": 60, "d": 40},
            "placements": [
                {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 15, "d": 8},
                # No "mount_sensor_1" sibling in placements.
            ],
        }
    }
    hs._resize_mount_parts_from_mount_spec(spec, parts)  # should not raise
    assert len(spec["mech"]["placements"]) == 1


def test_resize_mount_parts_noop_without_mount_spec():
    parts = [{"id": "mcu_1"}]  # no mount_spec at all
    spec = _base_spec()
    original = dict(spec["mech"]["placements"][1])
    hs._resize_mount_parts_from_mount_spec(spec, parts)
    placements_by_id = {p["part_id"]: p for p in spec["mech"]["placements"]}
    assert placements_by_id["mount_mcu_1"] == original
