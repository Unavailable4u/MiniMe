"""
tests/unit/test_mech_cutouts.py — Patch 5.7 (Phase 5, "Cutouts"):
covers eo/mech_cutouts.py --

  - nearest_exterior_face() (Patch 5.2): each of the six walls picked
    correctly, negative/overlap gaps clamped to 0, deterministic tie
    resolution, tolerant-of-missing-keys input
  - generate_cutout() (Patch 5.3): one eligible part per simple shape
    (display->window, buzzer->vent, mic->vent with its own distinct
    sizing, button->through_hole, led/indicator->light_pipe), category
    pre-filter rejection, keyword-mismatch rejection, "port" refused
    (routes to generate_port_cutout() instead)
  - generate_port_cutout() (Patch 5.4): usb/power_connector -> port
    envelope, non-port part rejected
  - check_min_wall_thickness() / the optional `housing_inner` plumbing
    on both generators (Patch 5.5): a cutout placed close to a housing
    edge is flagged (not raised); a cutout with room to spare passes;
    omitting `housing_inner` entirely omits the check (unchanged,
    Patch 5.3/5.4-era return shape)
  - apply_cutout_generation() (Patch 5.6): end-to-end wiring off a
    device-merged/enclosure-generated `mech` -- one cutout per eligible
    part, a non-cutout-eligible category (3D_PRINT housing/lid, or a
    generic_name that matches no CUTOUT_TABLE keyword) produces no
    cutout, "nothing to derive from yet" no-op when `mech["sections"]`
    or `mech["housing"]["inner"]` isn't populated

No LLM, no FreeCAD -- pure data reshaping (same as eo/mech_enclosure.py's
own tests), so no mock_llm/fake_bus fixtures needed.
"""
import eo.mech_cutouts as mc
import eo.mech_device as md
import eo.mech_enclosure as me
import eo.mech_supports as msup
from eo.enclosure_spec import ENCLOSURE_SPEC

# A generous, fixed cavity every nearest_exterior_face()/generate_*()
# test below measures a part against -- same "one shared inner box,
# only the part moves" shape eo/mech_enclosure.py's own tests already
# use for compute_housing_footprint()'s inputs.
_INNER = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 40}


# ---------------------------------------------------------------------------
# nearest_exterior_face (Patch 5.2)
# ---------------------------------------------------------------------------

def test_face_nearest_low_x_wall():
    part = {"x": 1, "y": 30, "z": 10, "w": 10, "h": 10, "d": 10}
    assert mc.nearest_exterior_face(part, _INNER) == "-x"


def test_face_nearest_high_x_wall():
    part = {"x": 88, "y": 30, "z": 10, "w": 10, "h": 10, "d": 10}
    assert mc.nearest_exterior_face(part, _INNER) == "+x"


def test_face_nearest_low_y_wall():
    part = {"x": 50, "y": 1, "z": 10, "w": 6, "h": 6, "d": 6}
    assert mc.nearest_exterior_face(part, _INNER) == "-y"


def test_face_nearest_high_y_wall():
    part = {"x": 50, "y": 53, "z": 10, "w": 6, "h": 6, "d": 6}
    assert mc.nearest_exterior_face(part, _INNER) == "+y"


def test_face_nearest_low_z_wall():
    part = {"x": 50, "y": 30, "z": 1, "w": 6, "h": 6, "d": 6}
    assert mc.nearest_exterior_face(part, _INNER) == "-z"


def test_face_nearest_high_z_wall():
    part = {"x": 50, "y": 30, "z": 33, "w": 6, "h": 6, "d": 6}
    assert mc.nearest_exterior_face(part, _INNER) == "+z"


def test_face_negative_gap_clamped_not_negative_infinity():
    # Part footprint already extends past the -x wall (pre-repair
    # geometry) -- the clamped 0 gap on -x must still win over every
    # other wall's own strictly-positive gap, not produce a nonsensical
    # comparison against an unclamped negative number.
    part = {"x": -5, "y": 30, "z": 10, "w": 10, "h": 10, "d": 10}
    assert mc.nearest_exterior_face(part, _INNER) == "-x"


def test_face_tie_resolves_via_fixed_face_order():
    # A cube dead-centered in a cube-shaped cavity is equidistant from
    # all six walls -- must return a stable, deterministic winner.
    inner = {"x": 0, "y": 0, "z": 0, "w": 20, "h": 20, "d": 20}
    part = {"x": 5, "y": 5, "z": 5, "w": 10, "h": 10, "d": 10}
    assert mc.nearest_exterior_face(part, inner) == "-x"
    # Deterministic: repeated calls agree.
    assert mc.nearest_exterior_face(part, inner) == mc.nearest_exterior_face(part, inner)


def test_face_tolerant_of_missing_keys():
    assert mc.nearest_exterior_face({}, {}) in {"-x", "+x", "-y", "+y", "-z", "+z"}


# ---------------------------------------------------------------------------
# generate_cutout (Patch 5.3) -- one eligible part per simple shape
# ---------------------------------------------------------------------------

def test_display_generates_rectangular_window():
    part = {"part_id": "display_1", "category": "sensor",
            "generic_name": "0.96in OLED Display",
            "x": 1, "y": 30, "z": 10, "w": 20, "h": 15, "d": 2}
    face = mc.nearest_exterior_face(part, _INNER)
    result = mc.generate_cutout(part, face, "window")

    assert result["part_id"] == "display_1"
    assert result["face"] == face
    assert result["cutout_type"] == "window"
    assert result["shape"] == "rectangular"
    assert result["keyword"] == "display"

    dim1, dim2 = mc._in_plane_extents(part, face)
    from eo.enclosure_spec import CUTOUT_TABLE
    bezel_margin = CUTOUT_TABLE["display"]["bezel_margin_mm"]
    assert result["width_mm"] == round(max(dim1 - 2 * bezel_margin, 0.0), 3)
    assert result["height_mm"] == round(max(dim2 - 2 * bezel_margin, 0.0), 3)


def test_buzzer_and_mic_both_vent_but_sized_differently():
    buzzer = {"part_id": "buzzer_1", "category": "actuator",
              "generic_name": "Piezo Buzzer Module",
              "x": 1, "y": 30, "z": 10, "w": 12, "h": 12, "d": 5}
    mic = {"part_id": "mic_1", "category": "sensor",
           "generic_name": "Electret Microphone",
           "x": 1, "y": 30, "z": 10, "w": 8, "h": 8, "d": 3}

    buzzer_face = mc.nearest_exterior_face(buzzer, _INNER)
    mic_face = mc.nearest_exterior_face(mic, _INNER)
    buzzer_cutout = mc.generate_cutout(buzzer, buzzer_face, "vent")
    mic_cutout = mc.generate_cutout(mic, mic_face, "vent")

    assert buzzer_cutout["keyword"] == "buzzer"
    assert mic_cutout["keyword"] == "mic"
    assert buzzer_cutout["shape"] == mic_cutout["shape"] == "circular"
    # Same cutout_type, genuinely different sizing/hole_count -- proves
    # the match is on the specific keyword's own descriptor, not just
    # the coarser cutout_type.
    assert buzzer_cutout["diameter_mm"] != mic_cutout["diameter_mm"]
    assert buzzer_cutout["hole_count"] == 4
    assert mic_cutout["hole_count"] == 1


def test_button_through_hole_diameter_derives_from_part_footprint():
    part = {"part_id": "button_1", "category": "actuator",
            "generic_name": "Tactile Push Button",
            "x": 50, "y": 1, "z": 10, "w": 6, "h": 8, "d": 6}
    face = mc.nearest_exterior_face(part, _INNER)
    result = mc.generate_cutout(part, face, "through_hole")

    assert result["shape"] == "circular"
    assert result["keyword"] == "button"
    # min(in-plane extents) + clearance_mm, per this cutout_type's own
    # "matching actuator diameter" sizing rule.
    dim1, dim2 = mc._in_plane_extents(part, face)
    from eo.enclosure_spec import CUTOUT_TABLE
    clearance = CUTOUT_TABLE["button"]["clearance_mm"]
    assert result["diameter_mm"] == round(min(dim1, dim2) + clearance, 3)


def test_led_and_indicator_both_light_pipe_fixed_size():
    led = {"part_id": "led_1", "category": "sensor", "generic_name": "Status LED",
           "x": 50, "y": 30, "z": 10, "w": 5, "h": 5, "d": 5}
    indicator = {"part_id": "ind_1", "category": "sensor",
                 "generic_name": "Power Indicator", "x": 50, "y": 30, "z": 10,
                 "w": 5, "h": 5, "d": 5}

    led_face = mc.nearest_exterior_face(led, _INNER)
    ind_face = mc.nearest_exterior_face(indicator, _INNER)
    led_cutout = mc.generate_cutout(led, led_face, "light_pipe")
    ind_cutout = mc.generate_cutout(indicator, ind_face, "light_pipe")

    assert led_cutout["keyword"] == "led"
    assert ind_cutout["keyword"] == "indicator"
    assert led_cutout["diameter_mm"] == ind_cutout["diameter_mm"] == 3.0


def test_generate_cutout_rejects_part_matching_no_keyword():
    part = {"part_id": "mcu_1", "category": "mcu",
            "generic_name": "ESP32 Dev Board",
            "x": 50, "y": 30, "z": 10, "w": 30, "h": 20, "d": 5}
    face = mc.nearest_exterior_face(part, _INNER)
    try:
        mc.generate_cutout(part, face, "window")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_generate_cutout_rejects_part_failing_category_prefilter():
    # A "housing" generic_name coincidentally contains no cutout
    # keyword, but even a contrived part whose category is outside
    # CUTOUT_ELIGIBLE_CATEGORIES must never match, regardless of its
    # own generic_name wording.
    part = {"part_id": "housing_1", "category": "3D_PRINT",
            "generic_name": "3D-Printed Enclosure Housing With Display Cutout",
            "x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    face = mc.nearest_exterior_face(part, _INNER)
    try:
        mc.generate_cutout(part, face, "window")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_generate_cutout_rejects_cutout_type_mismatch():
    part = {"part_id": "button_1", "category": "actuator",
            "generic_name": "Tactile Push Button",
            "x": 50, "y": 1, "z": 10, "w": 6, "h": 6, "d": 6}
    face = mc.nearest_exterior_face(part, _INNER)
    try:
        mc.generate_cutout(part, face, "window")  # button is through_hole, not window
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_generate_cutout_refuses_port_type():
    part = {"part_id": "usb_1", "category": "power",
            "generic_name": "USB-C Power Connector",
            "x": 50, "y": 30, "z": 1, "w": 9, "h": 7, "d": 4}
    face = mc.nearest_exterior_face(part, _INNER)
    try:
        mc.generate_cutout(part, face, "port")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# generate_port_cutout (Patch 5.4)
# ---------------------------------------------------------------------------

def test_usb_and_power_connector_generate_port_envelope():
    usb = {"part_id": "usb_1", "category": "power",
           "generic_name": "USB-C Power Connector",
           "x": 50, "y": 30, "z": 1, "w": 9, "h": 7, "d": 4}
    # CUTOUT_TABLE's own "power_connector" key is matched as a literal
    # (underscore-and-all) substring of generic_name -- see
    # _match_cutout_descriptor()'s own docstring ("appears as a
    # case-insensitive substring") -- so the fixture's generic_name has
    # to actually contain "power_connector" verbatim to exercise that
    # keyword specifically, distinct from "usb" above.
    barrel = {"part_id": "power_1", "category": "power",
              "generic_name": "DC Barrel Power_Connector Jack",
              "x": 50, "y": 30, "z": 1, "w": 11, "h": 9, "d": 5}

    usb_face = mc.nearest_exterior_face(usb, _INNER)
    barrel_face = mc.nearest_exterior_face(barrel, _INNER)
    usb_cutout = mc.generate_port_cutout(usb, usb_face)
    barrel_cutout = mc.generate_port_cutout(barrel, barrel_face)

    for cutout, part, face in ((usb_cutout, usb, usb_face), (barrel_cutout, barrel, barrel_face)):
        assert cutout["cutout_type"] == cutout["shape"] == "port"
        dim1, dim2 = mc._in_plane_extents(part, face)
        from eo.enclosure_spec import CUTOUT_TABLE
        clearance = CUTOUT_TABLE[cutout["keyword"]]["clearance_mm"]
        assert cutout["width_mm"] == round(dim1 + 2 * clearance, 3)
        assert cutout["height_mm"] == round(dim2 + 2 * clearance, 3)
        assert cutout["connector_envelope_mm"] == {
            "width": cutout["width_mm"], "height": cutout["height_mm"],
        }

    assert usb_cutout["keyword"] == "usb"
    assert barrel_cutout["keyword"] == "power_connector"


def test_generate_port_cutout_rejects_non_port_part():
    part = {"part_id": "button_1", "category": "actuator",
            "generic_name": "Tactile Push Button",
            "x": 50, "y": 1, "z": 10, "w": 6, "h": 6, "d": 6}
    face = mc.nearest_exterior_face(part, _INNER)
    try:
        mc.generate_port_cutout(part, face)
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# check_min_wall_thickness / optional housing_inner plumbing (Patch 5.5)
# ---------------------------------------------------------------------------

def test_wall_thickness_check_omitted_without_housing_inner():
    part = {"part_id": "display_1", "category": "sensor",
            "generic_name": "0.96in OLED Display",
            "x": 40, "y": 20, "z": 10, "w": 20, "h": 15, "d": 2}
    face = mc.nearest_exterior_face(part, _INNER)
    result = mc.generate_cutout(part, face, "window")
    assert "wall_thickness_check" not in result


def test_wall_thickness_check_passes_when_room_to_spare():
    part = {"part_id": "display_1", "category": "sensor",
            "generic_name": "0.96in OLED Display",
            "x": 40, "y": 20, "z": 10, "w": 20, "h": 15, "d": 2}
    face = mc.nearest_exterior_face(part, _INNER)
    result = mc.generate_cutout(part, face, "window", housing_inner=_INNER)
    check = result["wall_thickness_check"]
    assert check["ok"] is True
    assert check["violations"] == []


def test_wall_thickness_check_flags_violation_not_raises():
    # Button placed hard against the -x wall on its own w-axis, with a
    # through-hole diameter that has no bezel-style slack to absorb the
    # closeness -- must trip a real min_feature_mm violation.
    part = {"part_id": "button_1", "category": "actuator",
            "generic_name": "Tactile Push Button",
            "x": 0.5, "y": 1, "z": 10, "w": 6, "h": 6, "d": 6}
    face = mc.nearest_exterior_face(part, _INNER)
    assert face == "-x"
    result = mc.generate_cutout(part, face, "through_hole", housing_inner=_INNER)
    check = result["wall_thickness_check"]

    assert check["ok"] is False
    assert len(check["violations"]) >= 1
    assert all(v["margin_mm"] < ENCLOSURE_SPEC["min_feature_mm"] for v in check["violations"])
    # Never raised -- the cutout itself is still returned, fully formed.
    assert "diameter_mm" in result


def test_wall_thickness_check_wired_through_port_cutout_too():
    part = {"part_id": "usb_1", "category": "power",
            "generic_name": "USB-C Power Connector",
            "x": 0.5, "y": 30, "z": 1, "w": 9, "h": 7, "d": 4}
    face = mc.nearest_exterior_face(part, _INNER)
    assert face == "-x"
    result = mc.generate_port_cutout(part, face, housing_inner=_INNER)
    assert result["wall_thickness_check"]["ok"] is False


# ---------------------------------------------------------------------------
# apply_cutout_generation (Patch 5.6) -- end-to-end pipeline wiring
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
    """Runs Phase 1/2's own real pipeline (apply_device_merge ->
    apply_enclosure_generation -> apply_supports_generation) against
    `_demo_mech()`/`_DEMO_PARTS` so apply_cutout_generation() is
    exercised against a real, previously-validated `mech["housing"]`
    rather than a hand-faked one -- same "build off the real upstream
    output" posture eo/mech_wiring_weight.py's own tests already take
    toward eo/mech_device.py's zone assignment.
    """
    mech = _demo_mech()
    md.apply_device_merge(mech, _DEMO_PARTS)
    me.apply_enclosure_generation(mech, _DEMO_PARTS)
    msup.apply_supports_generation(mech, _DEMO_PARTS)
    return mech


def test_one_cutout_per_eligible_part():
    mech = _built_mech()
    cutouts = mc.apply_cutout_generation(mech, _DEMO_PARTS)

    by_part = {c["part_id"]: c for c in cutouts}
    assert set(by_part) == {"display_1", "button_1", "usb_1"}
    assert by_part["display_1"]["cutout_type"] == "window"
    assert by_part["button_1"]["cutout_type"] == "through_hole"
    assert by_part["usb_1"]["cutout_type"] == "port"
    assert mech["cutouts"] is cutouts


def test_non_eligible_category_produces_no_cutout():
    mech = _built_mech()
    cutouts = mc.apply_cutout_generation(mech, _DEMO_PARTS)
    part_ids = {c["part_id"] for c in cutouts}
    # housing_1/lid_1 (3D_PRINT) and battery_1/mcu_1 (no matching
    # CUTOUT_TABLE keyword in their own generic_name) never get a
    # cutout, regardless of being otherwise valid, placed parts.
    assert "housing_1" not in part_ids
    assert "lid_1" not in part_ids
    assert "battery_1" not in part_ids
    assert "mcu_1" not in part_ids


def test_every_cutout_carries_a_wall_thickness_check():
    mech = _built_mech()
    cutouts = mc.apply_cutout_generation(mech, _DEMO_PARTS)
    assert cutouts  # sanity: fixture actually produced cutouts
    assert all("wall_thickness_check" in c for c in cutouts)


def test_noop_when_mech_has_no_sections_yet():
    mech = {"placements": []}
    assert mc.apply_cutout_generation(mech, _DEMO_PARTS) == []
    assert mech["cutouts"] == []


def test_noop_when_housing_not_populated_yet():
    # Sections exist, but apply_enclosure_generation() hasn't run yet
    # (no mech["housing"]["inner"] to project a face against).
    mech = _demo_mech()
    md.apply_device_merge(mech, _DEMO_PARTS)
    assert mc.apply_cutout_generation(mech, _DEMO_PARTS) == []
    assert mech["cutouts"] == []


def test_idempotent_across_two_calls():
    mech = _built_mech()
    first = mc.apply_cutout_generation(mech, _DEMO_PARTS)
    second = mc.apply_cutout_generation(mech, _DEMO_PARTS)
    assert first == second
