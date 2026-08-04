"""
tests/test_router.py — Part 5 checklist item 2
Run with: python -m tests.test_router  (from project root)
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.router import build_execution_graph_from_hires, validate_registry_coverage


def test_duplicate_role_hires_produce_list():
    hires = [
        {"role": "implementer", "agent_key": "CEREBRAS_CODE_1", "brief": "part A"},
        {"role": "implementer", "agent_key": "CEREBRAS_CODE_2", "brief": "part B"},
        {"role": "verifier", "agent_key": "GROQ_API_KEY", "brief": "check it"},
    ]
    agent_names, key_overrides = build_execution_graph_from_hires(hires)

    assert agent_names == ["code_writers", "code_writers", "reviewer"], agent_names
    assert isinstance(key_overrides["code_writers"], list), key_overrides["code_writers"]
    assert key_overrides["code_writers"] == ["CEREBRAS_CODE_1", "CEREBRAS_CODE_2"]
    assert key_overrides["reviewer"] == "GROQ_API_KEY"  # single hire -> plain string
    print("PASS: duplicate-role hires produce a list under key_overrides")


def test_three_way_duplicate_accumulates_in_order():
    hires = [
        {"role": "implementer", "agent_key": "K1", "brief": ""},
        {"role": "implementer", "agent_key": "K2", "brief": ""},
        {"role": "implementer", "agent_key": "K3", "brief": ""},
    ]
    _, overrides = build_execution_graph_from_hires(hires)
    assert overrides["code_writers"] == ["K1", "K2", "K3"], overrides["code_writers"]
    print("PASS: 3+ duplicate hires accumulate in hire order")


def test_single_hire_stays_plain_string():
    hires = [{"role": "fixer", "agent_key": "SOLO_KEY", "brief": ""}]
    _, overrides = build_execution_graph_from_hires(hires)
    assert overrides["fixer_pool"] == "SOLO_KEY"
    assert not isinstance(overrides["fixer_pool"], list)
    print("PASS: single hire stays a plain string, not a 1-item list")


def test_registry_coverage_has_no_gaps():
    validate_registry_coverage()  # raises AssertionError on any gap
    print("PASS: validate_registry_coverage() found no gaps")


if __name__ == "__main__":
    test_duplicate_role_hires_produce_list()
    test_three_way_duplicate_accumulates_in_order()
    test_single_hire_stays_plain_string()
    test_registry_coverage_has_no_gaps()
    print("\nAll Part 5 checklist item 2 tests passed.")