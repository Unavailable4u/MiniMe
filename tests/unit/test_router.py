"""
tests/unit/test_router.py — Part 5 checklist item 2, rebuilt around the
current eo/router.py. build_execution_graph_from_hires() now returns a
3-tuple (agent_names, role_names, key_overrides) -- role_names was added
by Migration Part 2 §2.6 for parallel-group support (see
sanitize_parallel_groups()'s docstring) -- and key_overrides is keyed by
ROLE NAME (Part 11 §0 fix), not resolved module name.
"""
from eo.router import build_execution_graph_from_hires, validate_registry_coverage


def test_duplicate_role_hires_produce_a_list_of_key_overrides():
    hires = [
        {"role": "implementer", "agent_key": "CEREBRAS_CODE_1", "brief": "part A"},
        {"role": "implementer", "agent_key": "CEREBRAS_CODE_2", "brief": "part B"},
        {"role": "verifier", "agent_key": "GROQ_API_KEY", "brief": "check it"},
    ]
    agent_names, role_names, key_overrides = build_execution_graph_from_hires(hires)

    assert agent_names == ["code_writers", "code_writers", "reviewer"]
    assert role_names == ["implementer", "implementer", "verifier"]
    assert isinstance(key_overrides["implementer"], list)
    assert key_overrides["implementer"] == ["CEREBRAS_CODE_1", "CEREBRAS_CODE_2"]
    assert key_overrides["verifier"] == "GROQ_API_KEY"  # single hire -> plain string


def test_three_way_duplicate_accumulates_in_hire_order():
    hires = [
        {"role": "implementer", "agent_key": "K1", "brief": ""},
        {"role": "implementer", "agent_key": "K2", "brief": ""},
        {"role": "implementer", "agent_key": "K3", "brief": ""},
    ]
    _, role_names, overrides = build_execution_graph_from_hires(hires)
    assert overrides["implementer"] == ["K1", "K2", "K3"]
    assert role_names == ["implementer", "implementer", "implementer"]


def test_single_hire_key_override_stays_a_plain_string():
    hires = [{"role": "fixer", "agent_key": "SOLO_KEY", "brief": ""}]
    _, _, overrides = build_execution_graph_from_hires(hires)
    assert overrides["fixer"] == "SOLO_KEY"
    assert not isinstance(overrides["fixer"], list)


def test_key_overrides_are_keyed_by_role_not_by_resolved_module():
    # "verifier" resolves to module "reviewer" -- key_overrides must
    # still use the role name "verifier" as its key (Part 11 §0 fix),
    # not the resolved module name, so distinct roles that happen to
    # share a module never clobber each other's account choice.
    hires = [{"role": "verifier", "agent_key": "GROQ_API_KEY", "brief": ""}]
    agent_names, role_names, overrides = build_execution_graph_from_hires(hires)
    assert agent_names == ["reviewer"]
    assert role_names == ["verifier"]
    assert "verifier" in overrides
    assert "reviewer" not in overrides


def test_execution_order_collapses_a_multi_member_group():
    hires = [
        {"role": "writer_a", "agent_key": "K1", "brief": ""},
        {"role": "writer_b", "agent_key": "K2", "brief": ""},
        {"role": "implementer", "agent_key": "K3", "brief": ""},
    ]
    execution_order = [["writer_a", "writer_b"], "implementer"]
    agent_names, role_names, overrides = build_execution_graph_from_hires(
        hires, execution_order=execution_order
    )
    assert agent_names == ["generic_worker", "code_writers"]
    assert role_names == [["writer_a", "writer_b"], "implementer"]
    assert overrides["writer_a"] == "K1"
    assert overrides["writer_b"] == "K2"


def test_execution_order_group_with_only_one_member_staffed_degrades_to_single_slot():
    # writer_b was never actually hired -- a "group of one" is just a
    # normal single-role slot, not a concurrent group.
    hires = [{"role": "writer_a", "agent_key": "K1", "brief": ""}]
    execution_order = [["writer_a", "writer_b"]]
    agent_names, role_names, overrides = build_execution_graph_from_hires(
        hires, execution_order=execution_order
    )
    assert agent_names == ["generic_worker"]
    assert role_names == ["writer_a"]


def test_hires_not_mentioned_in_execution_order_go_to_the_end():
    hires = [
        {"role": "forgotten_role", "agent_key": "K1", "brief": ""},
        {"role": "implementer", "agent_key": "K2", "brief": ""},
    ]
    execution_order = ["implementer"]
    _, role_names, _ = build_execution_graph_from_hires(hires, execution_order=execution_order)
    assert role_names == ["implementer", "forgotten_role"]


def test_registry_coverage_has_no_gaps():
    validate_registry_coverage()  # raises AssertionError on any gap
