"""
tests/unit/test_eo_router.py — rebuilt around the current eo/router.py.

Migration Part 27 §2 retired the classic, fixed 19-agent TIERS[3]
roster: neither live entrypoint ever called
build_execution_graph(tier=3) in practice (tier 3 always goes through
the adaptive/hires-driven build_execution_graph_from_hires() instead --
see test_router.py), so TIERS no longer has a 3 key at all, and
build_execution_graph(tier=3) now raises ValueError like any other
unknown tier. This file replaces the old tests/test_eo_router.py, which
asserted against the now-deleted TIERS[3]/EXPECTED_TIER3_ORDER.
"""
import pytest

from eo.router import TIERS, DIRECTED_TASK_MAP, build_execution_graph, validate_registry_coverage
from eo.registry import resolve


def test_tier3_is_no_longer_a_static_graph():
    # Migration Part 27 §2 -- confirms the retirement stuck.
    assert 3 not in TIERS
    with pytest.raises(ValueError):
        build_execution_graph(tier=3)


def test_tier_0_routes_to_responder():
    assert build_execution_graph(tier=0) == ["responder"]


def test_tier_1_routes_to_lean_pipeline():
    assert build_execution_graph(tier=1) == [
        "prompt_writer_lean", "code_writer_lean", "reviewer_fixer_lean",
    ]


def test_tier_1_run_tests_appends_sandbox_tester_lean():
    graph = build_execution_graph(tier=1, run_tests=True)
    assert graph[-1] == "sandbox_tester_lean"
    assert build_execution_graph(tier=1, run_tests=False) == build_execution_graph(tier=1)


def test_tier2_directed_task_routing():
    assert build_execution_graph(tier=2, directed_task_type="review") == ["reviewer"]
    assert build_execution_graph(tier=2, directed_task_type="debug") == [
        "reviewer", "fixer_pool", "sandbox_tester", "file_manager_writeback",
    ]
    assert build_execution_graph(tier=2, directed_task_type="add_tests") == [
        "test_writer", "sandbox_tester", "file_manager_test_writeback",
    ]
    assert build_execution_graph(tier=2, directed_task_type="refactor") == [
        "code_writers", "file_manager_writeback",
    ]
    assert build_execution_graph(tier=2, directed_task_type="security_scan") == [
        "security_scanner", "security_aggregator",
    ]
    assert build_execution_graph(tier=2, directed_task_type="write_docs") == [
        "documentation_agent",
    ]


def test_tier2_missing_directed_task_type_raises():
    with pytest.raises(ValueError):
        build_execution_graph(tier=2)


def test_tier2_unknown_directed_task_type_raises():
    with pytest.raises(KeyError):
        build_execution_graph(tier=2, directed_task_type="not_a_real_task")


def test_explain_code_routes_to_responder():
    # explain_code is deliberately NOT in DIRECTED_TASK_MAP (it's
    # read-only, kept in its own EXPLAIN_CODE_ROUTE constant) but still
    # reachable through build_execution_graph(tier=2, ...).
    assert "explain_code" not in DIRECTED_TASK_MAP
    assert build_execution_graph(tier=2, directed_task_type="explain_code") == ["responder"]


def test_unknown_tier_raises_value_error():
    with pytest.raises(ValueError):
        build_execution_graph(tier=99)


def test_every_tier0_tier1_and_directed_task_agent_resolves_to_a_real_callable():
    names = set(build_execution_graph(tier=0))
    names.update(build_execution_graph(tier=1, run_tests=True))
    names.update(build_execution_graph(tier=2, directed_task_type="explain_code"))
    for task_type in DIRECTED_TASK_MAP:
        names.update(build_execution_graph(tier=2, directed_task_type=task_type))
    for name in names:
        fn = resolve(name)
        assert callable(fn), f"{name} did not resolve to a callable"


def test_registry_covers_every_referenced_agent_name():
    validate_registry_coverage()  # raises AssertionError on any gap


def test_validate_registry_coverage_raises_on_a_real_gap(monkeypatch):
    # The happy-path test above only proves today's roster is fully
    # covered -- it can never fail loudly again if a future edit
    # reintroduces a gap, since an AssertionError would just look like
    # any other test failure rather than confirming this function's own
    # detection actually works. Force a gap by adding a bogus agent name
    # to TIERS[0] that REGISTRY has never heard of, and confirm the
    # function raises with that exact name surfaced in the message.
    import eo.router as router_module
    patched_tiers = {
        0: {"agents": list(TIERS[0]["agents"]) + ["totally_unregistered_agent_xyz"]},
        1: TIERS[1],
    }
    monkeypatch.setattr(router_module, "TIERS", patched_tiers)
    with pytest.raises(AssertionError, match="totally_unregistered_agent_xyz"):
        validate_registry_coverage()
