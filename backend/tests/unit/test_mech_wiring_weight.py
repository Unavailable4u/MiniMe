"""
tests/unit/test_mech_wiring_weight.py — Patch 4.1 (Phase 4, "Wiring-
weighted placement"): covers eo/mech_wiring_weight.py --

  - build_section_adjacency_weights(): part_id->section_id resolution
    via the same section->subsection->member two-hop eo/mech_supports.py
    already uses, symmetric/canonicalized keys, same-section edges
    excluded, unresolvable parts excluded, non-dict/empty-input
    fail-safes, purity
  - section_pair_weight(): canonicalization-agnostic lookup, 0 default
    for an unknown/absent pair

No LLM, no FreeCAD -- pure data reshaping, so no mock_llm/fake_bus
fixtures needed.
"""
import eo.mech_wiring_weight as mw


def _mech(edges, sections=None):
    return {
        "placements": [
            {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
            {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
            {"part_id": "sensor_2", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
            {"part_id": "button_1", "x": 0, "y": 0, "z": 0, "w": 1, "h": 1, "d": 1},
        ],
        "sections": sections if sections is not None else [
            {"section_id": "Compute", "subsection_ids": ["mcu_1"]},
            {"section_id": "Sensing", "subsection_ids": ["sensor_1", "sensor_2"]},
            {"section_id": "Actuation", "subsection_ids": ["button_1"]},
        ],
        "wiring": {"edges": edges},
    }


# ---------------------------------------------------------------------------
# build_section_adjacency_weights
# ---------------------------------------------------------------------------

def test_single_edge_between_two_sections_counts_once():
    mech = _mech([{"from": "mcu_1", "to": "sensor_1"}])
    weights = mw.build_section_adjacency_weights(mech)
    assert weights == {("Compute", "Sensing"): 1}


def test_multiple_edges_between_same_pair_accumulate():
    mech = _mech([
        {"from": "mcu_1", "to": "sensor_1"},
        {"from": "mcu_1", "to": "sensor_2"},
        {"from": "sensor_2", "to": "mcu_1"},
    ])
    weights = mw.build_section_adjacency_weights(mech)
    assert weights == {("Compute", "Sensing"): 3}


def test_keys_are_canonicalized_regardless_of_edge_direction():
    forward = mw.build_section_adjacency_weights(
        _mech([{"from": "mcu_1", "to": "button_1"}]))
    backward = mw.build_section_adjacency_weights(
        _mech([{"from": "button_1", "to": "mcu_1"}]))
    assert forward == backward == {("Actuation", "Compute"): 1}


def test_edge_within_same_section_excluded():
    mech = _mech([{"from": "sensor_1", "to": "sensor_2"}])
    assert mw.build_section_adjacency_weights(mech) == {}


def test_edge_to_unresolvable_part_excluded():
    mech = _mech([{"from": "mcu_1", "to": "ghost_1"}])
    assert mw.build_section_adjacency_weights(mech) == {}


def test_multiple_section_pairs_tracked_independently():
    mech = _mech([
        {"from": "mcu_1", "to": "sensor_1"},
        {"from": "mcu_1", "to": "button_1"},
        {"from": "mcu_1", "to": "button_1"},
    ])
    weights = mw.build_section_adjacency_weights(mech)
    assert weights == {
        ("Compute", "Sensing"): 1,
        ("Actuation", "Compute"): 2,
    }


def test_non_dict_edges_silently_skipped():
    mech = _mech(["not_a_dict", None, {"from": "mcu_1", "to": "sensor_1"}])
    assert mw.build_section_adjacency_weights(mech) == {("Compute", "Sensing"): 1}


def test_no_wiring_key_returns_empty_dict():
    mech = _mech([])
    del mech["wiring"]
    assert mw.build_section_adjacency_weights(mech) == {}


def test_empty_edges_list_returns_empty_dict():
    assert mw.build_section_adjacency_weights(_mech([])) == {}


def test_no_sections_yet_returns_empty_dict():
    mech = _mech([{"from": "mcu_1", "to": "sensor_1"}], sections=[])
    assert mw.build_section_adjacency_weights(mech) == {}


def test_non_dict_mech_returns_empty_dict_without_raising():
    assert mw.build_section_adjacency_weights(None) == {}
    assert mw.build_section_adjacency_weights("not_a_dict") == {}


def test_pure_function_never_mutates_input():
    mech = _mech([{"from": "mcu_1", "to": "sensor_1"}])
    import copy
    snapshot = copy.deepcopy(mech)
    mw.build_section_adjacency_weights(mech)
    assert mech == snapshot


def test_idempotent_same_input_same_output():
    mech = _mech([{"from": "mcu_1", "to": "sensor_1"}])
    first = mw.build_section_adjacency_weights(mech)
    second = mw.build_section_adjacency_weights(mech)
    assert first == second


# ---------------------------------------------------------------------------
# section_pair_weight
# ---------------------------------------------------------------------------

def test_pair_weight_matches_either_query_direction():
    weights = mw.build_section_adjacency_weights(
        _mech([{"from": "mcu_1", "to": "sensor_1"}] * 2))
    assert mw.section_pair_weight(weights, "Compute", "Sensing") == 2
    assert mw.section_pair_weight(weights, "Sensing", "Compute") == 2


def test_pair_weight_defaults_to_zero_for_unknown_pair():
    weights = mw.build_section_adjacency_weights(
        _mech([{"from": "mcu_1", "to": "sensor_1"}]))
    assert mw.section_pair_weight(weights, "Compute", "Actuation") == 0


def test_pair_weight_zero_for_empty_or_none_weights():
    assert mw.section_pair_weight({}, "Compute", "Sensing") == 0
    assert mw.section_pair_weight(None, "Compute", "Sensing") == 0
