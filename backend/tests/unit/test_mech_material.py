"""
tests/unit/test_mech_material.py — Patch E.4 (Phase E, "Material
awareness"): covers eo/mech_material.py's own resolve_material() (Patch
E.2) plus the material-aware wiring Patch E.3 adds to
eo/mech_enclosure.py (compute_housing_footprint(),
compute_baseplate_footprint(), apply_enclosure_generation()) and
eo/mech_cutouts.py (check_min_wall_thickness(), generate_cutout(),
generate_port_cutout(), apply_cutout_generation()).

Per this guide's own Patch E.4 wording: "Wearable strap -> flexible
material + correct spec override; non-wearable device -> all parts
remain on rigid default." Both halves are covered here, at the
resolve_material() level AND at the wired-into-geometry level.

No LLM, no FreeCAD -- pure data reshaping (same as eo/mech_enclosure.py's
own tests), so no mock_llm/fake_bus fixtures needed.
"""
import eo.mech_cutouts as mc
import eo.mech_device as md
import eo.mech_enclosure as me
import eo.mech_material as mm
from eo.enclosure_spec import DEFAULT_MATERIAL, ENCLOSURE_SPEC, MATERIAL_PROPERTIES

_WEARABLE = {"enclosure_mode": "full", "mobility_type": "wearable"}
_STATIC = {"enclosure_mode": "full", "mobility_type": "static"}
_WHEELED = {"enclosure_mode": "partial", "mobility_type": "wheeled"}

_STRAP_PART = {"id": "strap_1", "category": "3D_PRINT", "generic_name": "Wrist Strap", "aliases": []}
_BAND_PART = {"id": "band_1", "category": "MISC", "generic_name": "Sensor Band", "aliases": ["wearable band"]}
_HOUSING_PART = {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"}
_MCU_PART = {"id": "mcu_1", "category": "mcu", "generic_name": "ESP32 Dev Board"}


# ---------------------------------------------------------------------------
# resolve_material() (Patch E.2)
# ---------------------------------------------------------------------------

def test_strap_on_wearable_resolves_flexible():
    assert mm.resolve_material(_STRAP_PART, _WEARABLE) == "tpu_flexible"


def test_band_on_wearable_resolves_flexible_via_alias():
    # "band" keyword lives in `aliases`, not `generic_name` -- both fields
    # must be checked, per resolve_material()'s own docstring.
    assert mm.resolve_material(_BAND_PART, _WEARABLE) == "tpu_flexible"


def test_strap_on_static_stays_rigid():
    assert mm.resolve_material(_STRAP_PART, _STATIC) == DEFAULT_MATERIAL


def test_housing_on_wearable_stays_rigid():
    # Not a strap/band keyword match -- every other part/archetype
    # combination resolves to the rigid default, unchanged from today's
    # implicit behavior (this patch's own "done when").
    assert mm.resolve_material(_HOUSING_PART, _WEARABLE) == DEFAULT_MATERIAL


def test_mcu_on_wearable_stays_rigid():
    # Electrical category is never strap-eligible, regardless of
    # generic_name/aliases content.
    assert mm.resolve_material(_MCU_PART, _WEARABLE) == DEFAULT_MATERIAL


def test_strap_category_mismatch_stays_rigid():
    # "strap"/"band" keyword present, but category isn't 3D_PRINT/MISC --
    # the coarse category pre-filter still wins.
    electrical_strap = {"id": "x", "category": "sensor", "generic_name": "Strap Sensor"}
    assert mm.resolve_material(electrical_strap, _WEARABLE) == DEFAULT_MATERIAL


def test_missing_or_malformed_archetype_defaults_to_rigid():
    assert mm.resolve_material(_STRAP_PART, None) == DEFAULT_MATERIAL
    assert mm.resolve_material(_STRAP_PART, {}) == DEFAULT_MATERIAL
    assert mm.resolve_material(_STRAP_PART, "not-a-dict") == DEFAULT_MATERIAL


def test_non_dict_part_defaults_to_rigid():
    assert mm.resolve_material(None, _WEARABLE) == DEFAULT_MATERIAL
    assert mm.resolve_material("not-a-dict", _WEARABLE) == DEFAULT_MATERIAL


def test_never_mutates_inputs():
    part_snapshot = dict(_STRAP_PART)
    archetype_snapshot = dict(_WEARABLE)
    mm.resolve_material(_STRAP_PART, _WEARABLE)
    assert _STRAP_PART == part_snapshot
    assert _WEARABLE == archetype_snapshot


# ---------------------------------------------------------------------------
# estimate_print_cost_bdt() (Patch K.2, pricing-audit) -- the
# deterministic, LLM-free print-cost estimate agents/hardware_speccer.py's
# _populate_prices() and api/routes/workspace_data.py's
# refresh_part_prices() both call for 3D_PRINT-category parts instead of
# routing them through part_price_finder.py's market search.
# ---------------------------------------------------------------------------

_HOUSING_WITH_DIMS = {
    "id": "housing_1", "category": "3D_PRINT", "generic_name": "Enclosure Housing",
    "dimensions_mm": {"w": 100, "h": 60, "d": 30},
}
_HOUSING_NO_DIMS = {
    "id": "housing_2", "category": "3D_PRINT", "generic_name": "Sensor Holder",
}
_HOUSING_PARTIAL_DIMS = {
    # Cylindrical shape -- "d" legitimately null per
    # agents/component_dimension_table.py's own "not applicable to this
    # shape" convention.
    "id": "housing_3", "category": "3D_PRINT", "generic_name": "Cylindrical Mount",
    "dimensions_mm": {"w": 28, "h": 19},
}


def test_full_bounding_box_produces_volume_based_estimate():
    cost = mm.estimate_print_cost_bdt(_HOUSING_WITH_DIMS)
    # 100 * 60 * 30 = 180,000 mm^3 * 0.3 fill / 1000 = 54 cm^3
    # 54 cm^3 * 1.24 g/cm^3 = 66.96 g * 3.5 BDT/g = 234.36 BDT
    assert cost == round(100 * 60 * 30 * mm._FDM_FILL_FACTOR / 1000 * mm._PLA_DENSITY_G_PER_CM3
                          * mm.PRINT_COST_BDT_PER_GRAM, 2)
    assert cost > 0


def test_missing_dimensions_falls_back_to_flat_estimate():
    assert mm.estimate_print_cost_bdt(_HOUSING_NO_DIMS) == mm._FLAT_ESTIMATE_BDT


def test_partial_dimensions_falls_back_to_flat_estimate_not_bogus_math():
    # Only 2 of 3 axes present -- must NOT multiply just w*h and call
    # that a volume (dimensionally meaningless for a bounding box).
    assert mm.estimate_print_cost_bdt(_HOUSING_PARTIAL_DIMS) == mm._FLAT_ESTIMATE_BDT


def test_non_dict_part_falls_back_to_flat_estimate():
    assert mm.estimate_print_cost_bdt(None) == mm._FLAT_ESTIMATE_BDT
    assert mm.estimate_print_cost_bdt("not-a-dict") == mm._FLAT_ESTIMATE_BDT


def test_zero_or_negative_axis_falls_back_to_flat_estimate():
    bad_dims = {"id": "x", "category": "3D_PRINT", "dimensions_mm": {"w": 0, "h": 10, "d": 10}}
    assert mm.estimate_print_cost_bdt(bad_dims) == mm._FLAT_ESTIMATE_BDT


def test_unknown_material_falls_back_to_default_material_cost():
    default_cost = mm.estimate_print_cost_bdt(_HOUSING_WITH_DIMS, material=DEFAULT_MATERIAL)
    unknown_cost = mm.estimate_print_cost_bdt(_HOUSING_WITH_DIMS, material="not_a_real_material")
    assert unknown_cost == default_cost


def test_material_override_cost_per_gram_takes_priority_over_flat_rate(monkeypatch):
    # No MATERIAL_PROPERTIES entry defines cost_per_gram_bdt today -- prove
    # the LOOKUP itself is override-aware (not just that today's table
    # happens to lack one) via monkeypatch, same pattern
    # test_check_min_wall_thickness_reads_min_feature_via_material_override
    # above already uses for min_feature_mm.
    patched_properties = dict(MATERIAL_PROPERTIES)
    patched_properties["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched_properties["tpu_flexible"]["cost_per_gram_bdt"] = 999.0
    monkeypatch.setattr(mm, "MATERIAL_PROPERTIES", patched_properties)

    overridden = mm.estimate_print_cost_bdt(_HOUSING_WITH_DIMS, material="tpu_flexible")
    rigid = mm.estimate_print_cost_bdt(_HOUSING_WITH_DIMS, material=DEFAULT_MATERIAL)
    assert overridden > rigid  # 999 BDT/g completely dwarfs the 3.5 BDT/g flat rate
    assert overridden == round(100 * 60 * 30 * mm._FDM_FILL_FACTOR / 1000 * mm._PLA_DENSITY_G_PER_CM3 * 999.0, 2)


def test_never_makes_network_or_llm_call(monkeypatch):
    # Deliberately break generate_text so the test fails loudly if
    # estimate_print_cost_bdt() ever grows an LLM dependency -- this must
    # stay a pure, instant computation (see its own docstring).
    import utils.llm_client as llm_client_mod

    def _boom(*a, **k):
        raise AssertionError("estimate_print_cost_bdt() must never call generate_text()")

    monkeypatch.setattr(llm_client_mod, "generate_text", _boom, raising=False)
    mm.estimate_print_cost_bdt(_HOUSING_WITH_DIMS)
    mm.estimate_print_cost_bdt(_HOUSING_NO_DIMS)


# ---------------------------------------------------------------------------
# MATERIAL_PROPERTIES table itself (Patch E.1) -- sanity checks the rest
# of this file's wiring assertions lean on.
# ---------------------------------------------------------------------------

def test_pla_rigid_is_an_empty_override():
    assert MATERIAL_PROPERTIES[DEFAULT_MATERIAL] == {}


def test_tpu_flexible_overrides_wall_thickness_only():
    tpu = MATERIAL_PROPERTIES["tpu_flexible"]
    assert tpu["wall_thickness_mm"] < ENCLOSURE_SPEC["wall_thickness_mm"]
    assert "min_feature_mm" not in tpu  # print-process floor, not overridden


# ---------------------------------------------------------------------------
# Patch E.3 wiring -- eo/mech_enclosure.py
# ---------------------------------------------------------------------------

def test_compute_housing_footprint_default_material_unchanged():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    with_default = me.compute_housing_footprint(device_footprint)
    explicit_rigid = me.compute_housing_footprint(device_footprint, material="pla_rigid")
    assert with_default == explicit_rigid
    assert with_default["lid"]["d"] == ENCLOSURE_SPEC["wall_thickness_mm"]


def test_compute_housing_footprint_flexible_material_uses_thinner_wall():
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    rigid = me.compute_housing_footprint(device_footprint, material="pla_rigid")
    flexible = me.compute_housing_footprint(device_footprint, material="tpu_flexible")

    tpu_wall = MATERIAL_PROPERTIES["tpu_flexible"]["wall_thickness_mm"]
    assert flexible["lid"]["d"] == tpu_wall
    assert flexible["lid"]["d"] < rigid["lid"]["d"]
    # outer shrinks with the thinner wall; inner (clearance-only) is
    # unaffected by material at all.
    assert flexible["outer"]["w"] < rigid["outer"]["w"]
    assert flexible["inner"] == rigid["inner"]


def test_compute_baseplate_footprint_flexible_material_uses_thinner_plate():
    device_footprint = {"x": 0, "y": 0, "z": 10, "w": 80, "h": 50, "d": 20}
    rigid = me.compute_baseplate_footprint(device_footprint, material="pla_rigid")
    flexible = me.compute_baseplate_footprint(device_footprint, material="tpu_flexible")

    tpu_wall = MATERIAL_PROPERTIES["tpu_flexible"]["wall_thickness_mm"]
    assert flexible["outer"]["d"] == tpu_wall
    assert flexible["outer"]["d"] < rigid["outer"]["d"]


def _mech_with_housing(device_footprint, housing_generic_name, archetype):
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 1, "w": 1, "h": 1, "d": 1},
        ],
        "sections": [
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 2}},
        ],
        "device": {"footprint": device_footprint},
        "archetype": archetype,
    }


def test_apply_enclosure_generation_non_wearable_stays_on_rigid_default():
    # Non-wearable device -> housing resolves DEFAULT_MATERIAL, numerically
    # identical to Phase A's own full-mode regression test in
    # tests/unit/test_mech_enclosure.py.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    mech = _mech_with_housing(device_footprint, "3D-Printed Enclosure Housing", _STATIC)
    parts = [
        {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"},
        {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
    ]
    result = me.apply_enclosure_generation(mech, parts)
    assert result["lid"]["d"] == ENCLOSURE_SPEC["wall_thickness_mm"]


def test_apply_enclosure_generation_no_archetype_stays_on_rigid_default():
    # Missing archetype entirely -- same "full mode must not drift"
    # regression-safety this whole guide's Phase A section requires,
    # now also true of Phase E's own material resolution.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    mech = _mech_with_housing(device_footprint, "3D-Printed Enclosure Housing", None)
    del mech["archetype"]
    parts = [
        {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"},
        {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
    ]
    result = me.apply_enclosure_generation(mech, parts)
    assert result["lid"]["d"] == ENCLOSURE_SPEC["wall_thickness_mm"]


def test_apply_enclosure_generation_wearable_strap_housing_gets_flexible_wall():
    # Exercises the actual E.3 wiring end-to-end: a structural part whose
    # own BOM generic_name resolves to tpu_flexible (Patch E.2) produces a
    # thinner housing shell out of apply_enclosure_generation(), not just
    # out of compute_housing_footprint() called directly.
    device_footprint = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30}
    mech = _mech_with_housing(device_footprint, "Wrist Strap Housing", _WEARABLE)
    parts = [
        {"id": "housing_1", "category": "3D_PRINT", "generic_name": "Wrist Strap Housing"},
        {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
    ]
    result = me.apply_enclosure_generation(mech, parts)
    tpu_wall = MATERIAL_PROPERTIES["tpu_flexible"]["wall_thickness_mm"]
    assert result["lid"]["d"] == tpu_wall
    assert result["lid"]["d"] < ENCLOSURE_SPEC["wall_thickness_mm"]


def test_apply_enclosure_generation_partial_mode_material_wiring():
    device_footprint = {"x": 0, "y": 0, "z": 10, "w": 80, "h": 50, "d": 20}
    mech = {
        "placements": [{"part_id": "baseplate_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1}],
        "sections": [
            {"section_id": "Enclosure", "subsection_ids": ["baseplate_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1}},
        ],
        "device": {"footprint": device_footprint},
        "archetype": _WHEELED,
    }
    parts = [{"id": "baseplate_1", "category": "3D_PRINT", "generic_name": "Wheeled Chassis Baseplate"}]
    result = me.apply_enclosure_generation(mech, parts)
    # Not strap/band-flavored -- stays on the rigid default even though
    # the archetype is non-static.
    assert result["outer"]["d"] == ENCLOSURE_SPEC["wall_thickness_mm"]


# ---------------------------------------------------------------------------
# Patch E.3 wiring -- eo/mech_cutouts.py
# ---------------------------------------------------------------------------

_INNER = {"x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 40}


def test_check_min_wall_thickness_default_material_matches_unspecified_call():
    part = {"part_id": "button_1", "category": "actuator",
            "generic_name": "Tactile Push Button",
            "x": 0.5, "y": 1, "z": 10, "w": 6, "h": 6, "d": 6}
    face = mc.nearest_exterior_face(part, _INNER)
    cutout = mc.generate_cutout(part, face, "through_hole")

    without_material_arg = mc.check_min_wall_thickness(part, face, cutout, _INNER)
    with_default_material = mc.check_min_wall_thickness(part, face, cutout, _INNER, material=DEFAULT_MATERIAL)
    assert without_material_arg == with_default_material


def test_check_min_wall_thickness_reads_min_feature_via_material_override(monkeypatch):
    # tpu_flexible doesn't define its own min_feature_mm today (Patch E.1),
    # so this proves the LOOKUP itself is material-aware -- not just that
    # today's two materials happen to agree -- by monkeypatching in a
    # temporary override and confirming it actually changes the result.
    part = {"part_id": "display_1", "category": "sensor",
            "generic_name": "0.96in OLED Display",
            "x": 40, "y": 20, "z": 10, "w": 20, "h": 15, "d": 2}
    face = mc.nearest_exterior_face(part, _INNER)
    cutout = mc.generate_cutout(part, face, "window")

    baseline = mc.check_min_wall_thickness(part, face, cutout, _INNER, material="tpu_flexible")
    assert baseline["ok"] is True  # plenty of margin under today's 1.2mm floor

    patched_properties = dict(MATERIAL_PROPERTIES)
    patched_properties["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched_properties["tpu_flexible"]["min_feature_mm"] = 1000.0
    monkeypatch.setattr(mc, "MATERIAL_PROPERTIES", patched_properties)

    overridden = mc.check_min_wall_thickness(part, face, cutout, _INNER, material="tpu_flexible")
    assert overridden["ok"] is False
    assert overridden["violations"]

    # Rigid default is untouched by the patched tpu_flexible entry.
    rigid_after_patch = mc.check_min_wall_thickness(part, face, cutout, _INNER, material=DEFAULT_MATERIAL)
    assert rigid_after_patch == baseline or rigid_after_patch["ok"] is True


def test_generate_cutout_forwards_material_to_wall_thickness_check(monkeypatch):
    part = {"part_id": "display_1", "category": "sensor",
            "generic_name": "0.96in OLED Display",
            "x": 40, "y": 20, "z": 10, "w": 20, "h": 15, "d": 2}
    face = mc.nearest_exterior_face(part, _INNER)

    patched_properties = dict(MATERIAL_PROPERTIES)
    patched_properties["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched_properties["tpu_flexible"]["min_feature_mm"] = 1000.0
    monkeypatch.setattr(mc, "MATERIAL_PROPERTIES", patched_properties)

    result = mc.generate_cutout(part, face, "window", housing_inner=_INNER, material="tpu_flexible")
    assert result["wall_thickness_check"]["ok"] is False


def test_generate_port_cutout_forwards_material_to_wall_thickness_check(monkeypatch):
    part = {"part_id": "usb_1", "category": "power",
            "generic_name": "USB-C Power Connector",
            "x": 40, "y": 20, "z": 1, "w": 9, "h": 7, "d": 4}
    face = mc.nearest_exterior_face(part, _INNER)

    patched_properties = dict(MATERIAL_PROPERTIES)
    patched_properties["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched_properties["tpu_flexible"]["min_feature_mm"] = 1000.0
    monkeypatch.setattr(mc, "MATERIAL_PROPERTIES", patched_properties)

    result = mc.generate_port_cutout(part, face, housing_inner=_INNER, material="tpu_flexible")
    assert result["wall_thickness_check"]["ok"] is False


def _demo_mech_with_archetype(archetype):
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "display_1", "x": 5, "y": 5, "z": 2, "w": 25, "h": 18, "d": 3},
        ],
        "sections": [
            {"section_id": "Sensing", "subsection_ids": ["display_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 25, "h": 18, "d": 3}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
        "wiring": {"edges": []},
        "archetype": archetype,
    }


def test_apply_cutout_generation_resolves_housing_material_end_to_end(monkeypatch):
    # A wearable device whose housing part is itself strap/band-flavored
    # (contrived, but exercises the real _resolve_housing_material() join
    # against `parts`) -- patch tpu_flexible's own min_feature_mm sky-high
    # so a passing cutout flips to a violation ONLY if the pipeline really
    # threaded the resolved "tpu_flexible" material all the way through
    # apply_cutout_generation() -> generate_cutout() ->
    # check_min_wall_thickness().
    mech = _demo_mech_with_archetype(_WEARABLE)
    parts = [
        {"id": "housing_1", "category": "3D_PRINT", "generic_name": "Wrist Strap Housing"},
        {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
        {"id": "display_1", "category": "sensor", "generic_name": "0.96in OLED Display"},
    ]
    md.apply_device_merge(mech, parts)
    me.apply_enclosure_generation(mech, parts)

    patched_properties = dict(MATERIAL_PROPERTIES)
    patched_properties["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched_properties["tpu_flexible"]["min_feature_mm"] = 1000.0
    monkeypatch.setattr(mc, "MATERIAL_PROPERTIES", patched_properties)

    cutouts = mc.apply_cutout_generation(mech, parts)
    by_id = {c["part_id"]: c for c in cutouts}
    assert "display_1" in by_id
    assert by_id["display_1"]["wall_thickness_check"]["ok"] is False


def test_apply_cutout_generation_non_wearable_unaffected_by_material_patch(monkeypatch):
    mech = _demo_mech_with_archetype(_STATIC)
    parts = [
        {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing"},
        {"id": "lid_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Lid"},
        {"id": "display_1", "category": "sensor", "generic_name": "0.96in OLED Display"},
    ]
    md.apply_device_merge(mech, parts)
    me.apply_enclosure_generation(mech, parts)

    patched_properties = dict(MATERIAL_PROPERTIES)
    patched_properties["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched_properties["tpu_flexible"]["min_feature_mm"] = 1000.0
    monkeypatch.setattr(mc, "MATERIAL_PROPERTIES", patched_properties)

    # Housing is never strap/band-flavored here, so it stays on
    # DEFAULT_MATERIAL regardless of the tpu_flexible patch above -- the
    # cutout's own wall_thickness_check is unaffected.
    cutouts = mc.apply_cutout_generation(mech, parts)
    by_id = {c["part_id"]: c for c in cutouts}
    assert by_id["display_1"]["wall_thickness_check"]["ok"] is True
