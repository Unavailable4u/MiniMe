"""
tests/unit/test_mech_device.py — Patch 4.3 (Phase 4, "Wiring-weighted
placement"): covers eo/mech_device.py --

  - plan_device_layout() / apply_device_merge() baseline behavior
    (front/center/edge zone assignment, side-by-side packing within a
    zone, "nothing to merge yet" None short-circuit, idempotency) --
    no dedicated test file existed for this module before this patch,
    so this file establishes that baseline alongside the new Patch 4.2
    ordering cases, per the patch breakdown's own "extend existing
    file, don't create a new one -- keeps ordering tests next to the
    existing device-merge tests they interact with" instruction: this
    IS that file, now that it exists.
  - Patch 4.2's own weighted-ordering change: within a shared zone
    (Sensing/Actuation, both -> "edge" under _SECTION_TO_ZONE), the
    section with more wiring edges to the previously-placed neighboring
    zone packs first (closer to that zone's own boundary); ties/no
    wiring data fall back to the original _SECTION_ORDER-derived order;
    two runs of the same input still produce identical (deterministic)
    output; translation math itself is unchanged by this patch, only
    iteration order

Fixture mirrors eo/mech_device.py's own `__main__` demo block exactly
(Power/Compute/Sensing/Actuation/Enclosure, one part per section) so
these tests exercise the same shape that module already documents
itself against.

No LLM, no FreeCAD -- pure data reshaping (same as eo/mech_enclosure.py's
own tests), so no mock_llm/fake_bus fixtures needed.
"""
import eo.mech_device as md
from eo.mech_wiring_weight import build_section_adjacency_weights


def _demo_mech(wiring_edges=None):
    return {
        "placements": [
            {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30},
            {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 120, "h": 90, "d": 3},
            {"part_id": "battery_1", "x": 5, "y": 5, "z": 2, "w": 20, "h": 10, "d": 10},
            {"part_id": "mcu_1", "x": 5, "y": 5, "z": 2, "w": 30, "h": 20, "d": 5},
            {"part_id": "sensor_1", "x": 5, "y": 5, "z": 2, "w": 15, "h": 10, "d": 5},
            {"part_id": "button_1", "x": 5, "y": 5, "z": 2, "w": 10, "h": 10, "d": 8},
        ],
        "sections": [
            {"section_id": "Power", "subsection_ids": ["battery_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 20, "h": 16, "d": 10}},
            {"section_id": "Compute", "subsection_ids": ["mcu_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 30, "h": 26, "d": 5}},
            {"section_id": "Sensing", "subsection_ids": ["sensor_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 15, "h": 10, "d": 5}},
            {"section_id": "Actuation", "subsection_ids": ["button_1"],
             "footprint": {"x": 5, "y": 5, "z": 2, "w": 10, "h": 10, "d": 8}},
            {"section_id": "Enclosure", "subsection_ids": ["housing_1", "lid_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
        ],
        "wiring": {"edges": wiring_edges or []},
    }


_DEMO_PARTS = [
    {"id": "housing_1", "category": "3D_PRINT"},
    {"id": "lid_1", "category": "3D_PRINT"},
    {"id": "battery_1", "category": "power"},
    {"id": "mcu_1", "category": "mcu"},
    {"id": "sensor_1", "category": "sensor"},
    {"id": "button_1", "category": "actuator"},
]


# ---------------------------------------------------------------------------
# Baseline: plan_device_layout() / apply_device_merge()
# ---------------------------------------------------------------------------

def test_returns_none_when_enclosure_has_no_footprint_yet():
    mech = {"placements": [], "sections": [{"section_id": "Enclosure",
                                             "subsection_ids": []}]}
    assert md.plan_device_layout(mech, []) is None


def test_sections_assigned_to_documented_zones():
    mech = _demo_mech()
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    assert plan["zones"]["front"] == ["Power"]
    assert plan["zones"]["center"] == ["Compute"]
    assert set(plan["zones"]["edge"]) == {"Sensing", "Actuation"}


def test_zoned_sections_packed_side_by_side_no_overlap():
    mech = _demo_mech()
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    edge_order = plan["zones"]["edge"]
    first_id, second_id = edge_order[0], edge_order[1]

    sections_by_id = {s["section_id"]: s for s in mech["sections"]}
    first_new_x = sections_by_id[first_id]["footprint"]["x"] + plan["translations"][first_id]["dx"]
    first_w = sections_by_id[first_id]["footprint"]["w"]
    second_new_x = sections_by_id[second_id]["footprint"]["x"] + plan["translations"][second_id]["dx"]

    assert second_new_x >= first_new_x + first_w  # packed after, not overlapping


def test_plan_never_mutates_mech():
    mech = _demo_mech()
    import copy
    snapshot = copy.deepcopy(mech)
    md.plan_device_layout(mech, _DEMO_PARTS)
    assert mech == snapshot


def test_apply_device_merge_moves_member_placements_and_stashes_plan():
    mech = _demo_mech()
    plan = md.apply_device_merge(mech, _DEMO_PARTS)
    assert plan is not None
    assert mech["device"] == plan

    by_id = {p["part_id"]: p for p in mech["placements"]}
    delta = plan["translations"]["Power"]
    assert by_id["battery_1"]["x"] == 5 + delta["dx"]
    assert by_id["battery_1"]["y"] == 5 + delta["dy"]


def test_apply_device_merge_none_when_nothing_to_merge():
    mech = {"placements": [], "sections": []}
    result = md.apply_device_merge(mech, [])
    assert result is None
    assert mech["device"] is None


def test_apply_device_merge_idempotent_second_call_no_op():
    mech = _demo_mech()
    md.apply_device_merge(mech, _DEMO_PARTS)
    first_positions = {p["part_id"]: (p["x"], p["y"]) for p in mech["placements"]}

    md.apply_device_merge(mech, _DEMO_PARTS)
    second_positions = {p["part_id"]: (p["x"], p["y"]) for p in mech["placements"]}

    assert first_positions == second_positions


# ---------------------------------------------------------------------------
# Patch 4.2: wiring-weighted ordering within a shared zone
# ---------------------------------------------------------------------------

def test_section_with_more_edges_to_neighbor_zone_packs_first():
    mech = _demo_mech(wiring_edges=[
        {"from": "button_1", "to": "mcu_1"},
        {"from": "button_1", "to": "mcu_1"},
        {"from": "button_1", "to": "mcu_1"},
        {"from": "sensor_1", "to": "mcu_1"},
    ])
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    # Actuation (3 edges to Compute) outweighs Sensing (1 edge) -- packs
    # first, i.e. closer to Compute's own edge of the zone boundary.
    assert plan["zones"]["edge"] == ["Actuation", "Sensing"]
    assert plan["translations"]["Actuation"]["dx"] < plan["translations"]["Sensing"]["dx"]


def test_reversed_edge_weighting_reverses_pack_order():
    mech = _demo_mech(wiring_edges=[
        {"from": "sensor_1", "to": "mcu_1"},
        {"from": "sensor_1", "to": "mcu_1"},
    ])
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    assert plan["zones"]["edge"] == ["Sensing", "Actuation"]


def test_no_wiring_falls_back_to_original_section_order():
    mech = _demo_mech(wiring_edges=[])
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    # No adjacency data at all -- degrades to eo/mech_sections.py's own
    # _SECTION_ORDER-derived order (Sensing before Actuation), same as
    # before this patch existed.
    assert plan["zones"]["edge"] == ["Sensing", "Actuation"]


def test_tied_weight_falls_back_to_original_section_order():
    mech = _demo_mech(wiring_edges=[
        {"from": "sensor_1", "to": "mcu_1"},
        {"from": "button_1", "to": "mcu_1"},
    ])
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    assert plan["zones"]["edge"] == ["Sensing", "Actuation"]


def test_edges_within_the_same_section_do_not_affect_ordering():
    # An edge between two members of the SAME section contributes
    # nothing to cross-section adjacency (eo/mech_wiring_weight.py's own
    # build_section_adjacency_weights() skips same-section pairs) --
    # order stays at its untouched default.
    mech = _demo_mech(wiring_edges=[
        {"from": "sensor_1", "to": "sensor_1"},
    ])
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    assert plan["zones"]["edge"] == ["Sensing", "Actuation"]


def test_translation_math_unchanged_only_order_changes():
    # Same footprints, same _ZONE_CLEARANCE_MM/_ZONE_MARGIN_MM math --
    # Patch 4.2 changes WHICH section goes first, never the packing
    # formula itself, so the zone's own final right-hand boundary
    # (running_x_by_zone's cumulative sum of fw + clearance) lands in
    # the same place regardless of which order the same two widths were
    # summed in -- addition is commutative even though the per-section
    # dx VALUES themselves differ by order (see the narrower-section-
    # goes-second case below).
    unweighted = md.plan_device_layout(_demo_mech(wiring_edges=[]), _DEMO_PARTS)
    weighted = md.plan_device_layout(
        _demo_mech(wiring_edges=[{"from": "button_1", "to": "mcu_1"}] * 3),
        _DEMO_PARTS,
    )
    assert unweighted["zones"]["edge"] != weighted["zones"]["edge"]  # order DID change

    def _zone_right_edge(plan):
        sid = plan["zones"]["edge"][-1]
        section = next(s for s in mech["sections"] if s["section_id"] == sid)
        return (section["footprint"]["x"] + plan["translations"][sid]["dx"]
                + section["footprint"]["w"])

    mech = _demo_mech()
    assert _zone_right_edge(unweighted) == _zone_right_edge(weighted)


def test_deterministic_two_runs_same_wiring_same_order():
    mech = _demo_mech(wiring_edges=[
        {"from": "button_1", "to": "mcu_1"},
        {"from": "sensor_1", "to": "mcu_1"},
        {"from": "sensor_1", "to": "mcu_1"},
    ])
    first = md.plan_device_layout(mech, _DEMO_PARTS)
    second = md.plan_device_layout(mech, _DEMO_PARTS)
    assert first == second


def test_front_and_center_zones_unaffected_by_weighting():
    # Only zones with >1 section sharing them are ever reordered; front
    # (Power) and center (Compute) have exactly one section each under
    # _SECTION_TO_ZONE and are always left as-is.
    mech = _demo_mech(wiring_edges=[
        {"from": "battery_1", "to": "mcu_1"},
        {"from": "battery_1", "to": "mcu_1"},
        {"from": "battery_1", "to": "mcu_1"},
    ])
    plan = md.plan_device_layout(mech, _DEMO_PARTS)
    assert plan["zones"]["front"] == ["Power"]
    assert plan["zones"]["center"] == ["Compute"]


def test_apply_device_merge_reflects_weighted_order_in_final_positions():
    mech = _demo_mech(wiring_edges=[
        {"from": "button_1", "to": "mcu_1"},
        {"from": "button_1", "to": "mcu_1"},
        {"from": "button_1", "to": "mcu_1"},
    ])
    plan = md.apply_device_merge(mech, _DEMO_PARTS)
    assert plan["zones"]["edge"][0] == "Actuation"

    by_id = {p["part_id"]: p for p in mech["placements"]}
    # Actuation's own member (button_1) actually lands at the smaller x
    # -- the weighted order isn't just reported in `zones`, it's the
    # order apply_device_merge() actually applied translations in.
    assert by_id["button_1"]["x"] < by_id["sensor_1"]["x"]


# ---------------------------------------------------------------------------
# Sanity check against eo/mech_wiring_weight.py's own Patch 4.1 output --
# not a re-test of that module's own unit tests, just confirming
# mech_device.py is reading the same map shape Patch 4.1 documents.
# ---------------------------------------------------------------------------

def test_weights_consumed_by_plan_match_patch_4_1_output_shape():
    mech = _demo_mech(wiring_edges=[
        {"from": "button_1", "to": "mcu_1"},
        {"from": "sensor_1", "to": "mcu_1"},
    ])
    weights = build_section_adjacency_weights(mech)
    assert weights == {
        ("Actuation", "Compute"): 1,
        ("Compute", "Sensing"): 1,
    }
