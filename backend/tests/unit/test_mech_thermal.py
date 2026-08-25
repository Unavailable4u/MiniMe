"""
tests/unit/test_mech_thermal.py — Mech View standalone implementation
guide, Phase F, Patch F.5: covers eo/mech_thermal.py's own curated
tables (F.1), eo/mech_cutouts.py's thermal-vent branch (F.3), and eo/
mech_validator.py's check_vibration_separation() (F.4).

  - lookup_thermal() / lookup_vibration() (F.1): a curated hit returns
    the table's own value; a miss (no entry, or an unusable
    generic_name) returns `None`, never a silently-defaulted "none"/
    `False` -- see eo/mech_thermal.py's own top docstring on why that
    distinction matters to every caller below.
  - generate_thermal_vent_cutout() (F.3 pure generator): fixed sizing
    off `_THERMAL_VENT_DESCRIPTOR`, independent of any CUTOUT_TABLE
    match; optional `housing_inner` wall-thickness-check plumbing,
    same additive/opt-in shape generate_cutout()/generate_port_cutout()
    already establish.
  - apply_cutout_generation() thermal branch (F.3 pipeline wiring): a
    curated-"hot" part gets its own extra thermal_vent cutout even
    though its own generic_name matches no CUTOUT_TABLE keyword; a
    curated-"warm" (not "hot") part does not; the existing Patch A.5
    full-mode gate applies to this branch exactly like every other
    cutout this function emits -- no thermal vent leaks through in
    `partial`/`none` enclosure_mode.
  - check_vibration_separation() (F.4): a vibration-source part too
    close to a vibration-sensitive part (camera/IMU/gyro keyword)
    violates; the same pair adequately separated passes; a layout with
    no vibration source, or no sensitive part, checks zero pairs and
    passes trivially; the check runs regardless of
    `mech["archetype"]["mobility_type"]` (unlike check_balance()) since
    Phase F is stated as independent of Phase A.

No LLM, no FreeCAD -- every function under test here is pure/
deterministic (same "pure scan, no mutation, no I/O, no FreeCAD"
posture tests/unit/test_mech_balance.py's own module docstring already
states for its sibling Phase C/F.4 module), and every F.3/F.4 code path
under test deliberately calls only F.1's free table lookups, never F.2's
LLM fallback -- so no mock_llm/fake_bus fixtures needed either.
"""
import eo.mech_cutouts as mc
import eo.mech_device as md
import eo.mech_enclosure as me
import eo.mech_sections as msec
import eo.mech_subsections as msub
import eo.mech_supports as msup
import eo.mech_thermal as mt
import eo.mech_validator as mv

# ---------------------------------------------------------------------------
# lookup_thermal() / lookup_vibration() (Patch F.1) -- quick sanity, not a
# full re-test of every THERMAL_TABLE/VIBRATION_TABLE entry (already this
# module's own literal, self-documenting data).
# ---------------------------------------------------------------------------

def test_lookup_thermal_curated_hit():
    assert mt.lookup_thermal("Linear Voltage Regulator") == "hot"
    assert mt.lookup_thermal("linear voltage regulator") == "hot"  # case-insensitive
    assert mt.lookup_thermal("Raspberry Pi") == "warm"


def test_lookup_thermal_miss_is_none_not_curated_none():
    # A genuinely uncurated part -- distinct from a part curated "none".
    assert mt.lookup_thermal("Tactile Push Button") is None
    assert mt.lookup_thermal(None) is None
    assert mt.lookup_thermal("") is None


def test_lookup_vibration_curated_hit_including_explicit_false():
    assert mt.lookup_vibration("Stepper Motor") is True
    assert mt.lookup_vibration("Wheel") is True
    assert mt.lookup_vibration("Caster Wheel") is False  # curated False, not a miss


def test_lookup_vibration_miss_is_none():
    assert mt.lookup_vibration("0.96in OLED Display") is None


# ---------------------------------------------------------------------------
# generate_thermal_vent_cutout() (Patch F.3, pure generator)
# ---------------------------------------------------------------------------

_INNER = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 40}


def test_generate_thermal_vent_cutout_shape_and_sizing():
    part = {"part_id": "regulator_1", "x": 40, "y": 25, "z": 1,
            "w": 8, "h": 6, "d": 3}
    face = mc.nearest_exterior_face(part, _INNER)
    result = mc.generate_thermal_vent_cutout(part, face)

    assert result["part_id"] == "regulator_1"
    assert result["face"] == face
    assert result["cutout_type"] == "thermal_vent"
    assert result["shape"] == "circular"
    assert result["keyword"] == "thermal_hot"
    assert result["diameter_mm"] == mc._THERMAL_VENT_DESCRIPTOR["hole_diameter_mm"]
    assert result["hole_count"] == mc._THERMAL_VENT_DESCRIPTOR["hole_count"]
    assert result["mesh_clearance_mm"] == mc._THERMAL_VENT_DESCRIPTOR["mesh_clearance_mm"]
    assert "wall_thickness_check" not in result  # housing_inner omitted


def test_generate_thermal_vent_cutout_wall_thickness_check_opt_in():
    # Placed hard against the -x wall -- should flag, same shape
    # check_min_wall_thickness()'s own tests already establish for
    # generate_cutout()/generate_port_cutout().
    part = {"part_id": "regulator_1", "x": 0.5, "y": 25, "z": 1,
            "w": 8, "h": 6, "d": 3}
    face = mc.nearest_exterior_face(part, _INNER)
    assert face == "-x"
    result = mc.generate_thermal_vent_cutout(part, face, housing_inner=_INNER)
    assert result["wall_thickness_check"]["ok"] is False


def test_generate_thermal_vent_cutout_never_raises_on_unmatched_part():
    # Unlike generate_cutout(), never consults CUTOUT_TABLE at all --
    # a part matching no keyword (or no category) still gets a cutout.
    part = {"part_id": "regulator_1", "x": 40, "y": 25, "z": 1,
            "w": 8, "h": 6, "d": 3}
    face = mc.nearest_exterior_face(part, _INNER)
    result = mc.generate_thermal_vent_cutout(part, face)
    assert result["cutout_type"] == "thermal_vent"


# ---------------------------------------------------------------------------
# apply_cutout_generation() thermal branch (Patch F.3, pipeline wiring)
# ---------------------------------------------------------------------------

def _thermal_demo_mech():
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "regulator_1", "x": 5, "y": 5, "z": 2, "w": 8, "h": 6, "d": 3},
            {"part_id": "mcu_1", "x": 20, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5},
        ],
        "sections": [
            {"section_id": "Power", "subsection_ids": ["regulator_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 8, "h": 6, "d": 3}},
            {"section_id": "Compute", "subsection_ids": ["mcu_1"],
             "footprint": {"x": 20, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
        "wiring": {"edges": []},
    }


_THERMAL_DEMO_PARTS = [
    {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"},
    {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
    # Curated "hot" (Patch F.1's own THERMAL_TABLE), matches no
    # CUTOUT_TABLE keyword at all -- the exact gap Patch F.3 closes.
    {"id": "regulator_1", "category": "power", "generic_name": "Linear Voltage Regulator"},
    # Curated "warm" (not "hot") -- must NOT get a thermal vent.
    {"id": "mcu_1", "category": "mcu", "generic_name": "Raspberry Pi"},
]


def _built_thermal_mech(enclosure_mode="full"):
    mech = _thermal_demo_mech()
    if enclosure_mode != "full":
        mech["archetype"] = {"enclosure_mode": enclosure_mode}
    md.apply_device_merge(mech, _THERMAL_DEMO_PARTS)
    me.apply_enclosure_generation(mech, _THERMAL_DEMO_PARTS)
    msup.apply_supports_generation(mech, _THERMAL_DEMO_PARTS)
    return mech


def test_hot_part_gets_thermal_vent_cutout_in_full_mode():
    mech = _built_thermal_mech("full")
    cutouts = mc.apply_cutout_generation(mech, _THERMAL_DEMO_PARTS)

    thermal_vents = [c for c in cutouts if c["cutout_type"] == "thermal_vent"]
    assert {c["part_id"] for c in thermal_vents} == {"regulator_1"}
    assert thermal_vents[0]["keyword"] == "thermal_hot"


def test_warm_not_hot_part_gets_no_thermal_vent():
    mech = _built_thermal_mech("full")
    cutouts = mc.apply_cutout_generation(mech, _THERMAL_DEMO_PARTS)

    # mcu_1 (Raspberry Pi, curated "warm") never gets a thermal_vent --
    # only a curated "hot" part crosses Patch F.3's own threshold.
    assert not any(c["part_id"] == "mcu_1" for c in cutouts
                   if c["cutout_type"] == "thermal_vent")


def test_hot_part_no_cutout_at_all_in_partial_mode():
    mech = _built_thermal_mech("partial")
    cutouts = mc.apply_cutout_generation(mech, _THERMAL_DEMO_PARTS)
    # The existing Patch A.5 full-mode gate (not new to F.3) applies to
    # the thermal branch exactly like every other cutout this function
    # emits -- an open frame has no wall to vent through.
    assert cutouts == []
    assert mech["cutouts"] == []


def test_hot_part_no_cutout_at_all_in_none_mode():
    mech = _built_thermal_mech("none")
    cutouts = mc.apply_cutout_generation(mech, _THERMAL_DEMO_PARTS)
    assert cutouts == []
    assert mech["cutouts"] == []


def test_thermal_vent_carries_a_wall_thickness_check():
    mech = _built_thermal_mech("full")
    cutouts = mc.apply_cutout_generation(mech, _THERMAL_DEMO_PARTS)
    thermal_vents = [c for c in cutouts if c["cutout_type"] == "thermal_vent"]
    assert thermal_vents
    assert all("wall_thickness_check" in c for c in thermal_vents)


# ---------------------------------------------------------------------------
# check_vibration_separation() (Patch F.4)
# ---------------------------------------------------------------------------

def _vibration_parts(extra=None):
    parts = [
        {"id": "motor_1", "generic_name": "Stepper Motor", "category": "actuator"},
        {"id": "camera_1", "generic_name": "Wide Angle Camera Module", "category": "sensor"},
    ]
    return parts + (extra or [])


def _vibration_mech(motor_pos, camera_pos, mobility_type="static", extra_placements=None):
    placements = [
        {"part_id": "motor_1", "w": 10, "h": 10, "d": 10, **motor_pos},
        {"part_id": "camera_1", "w": 10, "h": 10, "d": 10, **camera_pos},
    ] + (extra_placements or [])
    mech = {
        "archetype": {"enclosure_mode": "full", "mobility_type": mobility_type},
        "placements": placements,
    }
    msub.apply_subsection_grouping(mech)
    msec.apply_section_grouping(mech, _vibration_parts())
    return mech


def test_vibration_source_too_close_to_sensitive_part_violates():
    # Footprint centers (5,5,5) and (10,10,5) -- well under the 15mm floor.
    mech = _vibration_mech({"x": 0, "y": 0, "z": 0}, {"x": 5, "y": 5, "z": 0})

    result = mv.check_vibration_separation(mech, _vibration_parts())

    assert result["ok"] is False
    assert result["pairs_checked"] == 1
    violation = result["violations"][0]
    assert violation["vibration_source_part_id"] == "motor_1"
    assert violation["sensitive_part_id"] == "camera_1"
    assert violation["separation_mm"] < mv.MIN_VIBRATION_SEPARATION_MM
    assert violation["required_mm"] == mv.MIN_VIBRATION_SEPARATION_MM


def test_adequately_separated_passes():
    # Footprint centers (5,5,5) and (105,5,5) -- 100mm apart, well clear.
    mech = _vibration_mech({"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 0})

    result = mv.check_vibration_separation(mech, _vibration_parts())

    assert result["ok"] is True
    assert result["violations"] == []
    assert result["pairs_checked"] == 1


def test_no_vibration_source_checks_zero_pairs():
    parts = [{"id": "camera_1", "generic_name": "Wide Angle Camera Module", "category": "sensor"}]
    mech = {
        "archetype": {"enclosure_mode": "full", "mobility_type": "static"},
        "placements": [{"part_id": "camera_1", "x": 0, "y": 0, "z": 0, "w": 10, "h": 10, "d": 10}],
    }
    msub.apply_subsection_grouping(mech)
    msec.apply_section_grouping(mech, parts)

    result = mv.check_vibration_separation(mech, parts)
    assert result == {"ok": True, "violations": [], "pairs_checked": 0}


def test_no_sensitive_part_checks_zero_pairs():
    parts = [{"id": "motor_1", "generic_name": "Stepper Motor", "category": "actuator"}]
    mech = {
        "archetype": {"enclosure_mode": "full", "mobility_type": "static"},
        "placements": [{"part_id": "motor_1", "x": 0, "y": 0, "z": 0, "w": 10, "h": 10, "d": 10}],
    }
    msub.apply_subsection_grouping(mech)
    msec.apply_section_grouping(mech, parts)

    result = mv.check_vibration_separation(mech, parts)
    assert result == {"ok": True, "violations": [], "pairs_checked": 0}


def test_runs_regardless_of_archetype_unlike_check_balance():
    # Same too-close layout as the violation test above, but under
    # every non-wheeled/legged mobility_type -- check_balance() would
    # skip all of these; check_vibration_separation() must not, since
    # Phase F is independent of Phase A.
    for mobility_type in ("static", "handheld", "wearable", "flying", "wheeled", "legged"):
        mech = _vibration_mech({"x": 0, "y": 0, "z": 0}, {"x": 5, "y": 5, "z": 0},
                                mobility_type=mobility_type)
        result = mv.check_vibration_separation(mech, _vibration_parts())
        assert result["ok"] is False, f"mobility_type={mobility_type} should still be checked"


def test_noop_when_mech_has_no_sections_yet():
    assert mv.check_vibration_separation({}, []) == {"ok": True, "violations": [], "pairs_checked": 0}
