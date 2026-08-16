"""
tests/unit/test_enclosure_spec_cutouts.py — Patch 5.1 (Phase 5,
"Cutouts"): covers the new CUTOUT_TABLE / CUTOUT_ELIGIBLE_CATEGORIES
config added to eo/enclosure_spec.py in this patch.

Pure config, zero logic (per this patch's own "Config only" sizing
note) -- no compute_*/apply_* function to call yet, so these are shape/
invariant checks on the data itself: every row has the fields Patch
5.2/5.3's own future keyword-match and cutout-geometry generator will
need, `shape` stays within the three-way vocabulary those patches will
dispatch on, and CUTOUT_ELIGIBLE_CATEGORIES matches the codebase's real
5-value electrical-category enum (agents/hardware_speccer.py's own
_ELECTRICAL_CATEGORIES) rather than the Master Guide's literal,
non-existent category names. Existing ENCLOSURE_SPEC/SUPPORT_CATEGORIES
keys already have their own coverage via test_mech_enclosure.py/
test_mech_supports.py and aren't re-tested here.
"""
from eo.enclosure_spec import ENCLOSURE_SPEC, CUTOUT_TABLE, CUTOUT_ELIGIBLE_CATEGORIES


_VALID_SHAPES = {"rectangular", "circular", "port"}
_EXPECTED_KEYWORDS = {"display", "buzzer", "mic", "button", "usb",
                       "power_connector", "led", "indicator"}


def test_importable_alongside_existing_spec_constants():
    assert isinstance(ENCLOSURE_SPEC, dict)
    assert isinstance(CUTOUT_TABLE, dict)
    assert isinstance(CUTOUT_ELIGIBLE_CATEGORIES, set)


def test_table_covers_every_master_guide_row():
    assert _EXPECTED_KEYWORDS <= set(CUTOUT_TABLE.keys())


def test_every_row_has_cutout_type_and_shape():
    for keyword, descriptor in CUTOUT_TABLE.items():
        assert isinstance(descriptor, dict), keyword
        assert "cutout_type" in descriptor, keyword
        assert "shape" in descriptor, keyword


def test_every_row_shape_within_known_vocabulary():
    for keyword, descriptor in CUTOUT_TABLE.items():
        assert descriptor["shape"] in _VALID_SHAPES, keyword


def test_display_row_is_rectangular_window_with_bezel_margin():
    row = CUTOUT_TABLE["display"]
    assert row["cutout_type"] == "window"
    assert row["shape"] == "rectangular"
    assert row["bezel_margin_mm"] > 0


def test_vent_rows_are_circular_with_hole_sizing():
    for keyword in ("buzzer", "mic"):
        row = CUTOUT_TABLE[keyword]
        assert row["cutout_type"] == "vent"
        assert row["shape"] == "circular"
        assert row["hole_diameter_mm"] > 0
        assert row["hole_count"] >= 1
        assert row["mesh_clearance_mm"] >= 0


def test_button_row_has_no_hardcoded_diameter():
    # Button cutout diameter is read off the actuator's own footprint by
    # Patch 5.3, not guessed here -- only a clearance allowance belongs
    # in this table for that row.
    row = CUTOUT_TABLE["button"]
    assert row["cutout_type"] == "through_hole"
    assert row["shape"] == "circular"
    assert "hole_diameter_mm" not in row
    assert row["clearance_mm"] > 0


def test_port_rows_are_port_shaped_with_clearance_not_a_fixed_size():
    for keyword in ("usb", "power_connector"):
        row = CUTOUT_TABLE[keyword]
        assert row["cutout_type"] == "port"
        assert row["shape"] == "port"
        assert row["clearance_mm"] > 0


def test_led_and_indicator_are_separate_equivalent_light_pipe_rows():
    led, indicator = CUTOUT_TABLE["led"], CUTOUT_TABLE["indicator"]
    assert led == indicator
    assert led["cutout_type"] == "light_pipe"
    assert led["shape"] == "circular"
    assert led["hole_diameter_mm"] > 0


def test_eligible_categories_match_real_electrical_enum_not_guide_wording():
    # Guide wording never used real category names ("display", "button",
    # etc.) in the first place for this set -- CUTOUT_ELIGIBLE_CATEGORIES
    # is the same 5-value electrical enum SUPPORT_CATEGORIES/
    # _ELECTRICAL_CATEGORIES already establish elsewhere in this tree.
    assert CUTOUT_ELIGIBLE_CATEGORIES == {"mcu", "sensor", "actuator", "power", "module"}


def test_eligible_categories_excludes_purely_mechanical_categories():
    assert "3D_PRINT" not in CUTOUT_ELIGIBLE_CATEGORIES
    assert "MISC" not in CUTOUT_ELIGIBLE_CATEGORIES
