"""
tests/test_eo_router.py — Part 11 of the v5 Master Blueprint's testing
plan: "every entry in DIRECTED_TASK_MAP resolves to real, importable
agent callables; every TIERS[n]["agents"] entry does too."
Also the specific check Stage 4.1 of the roadmap calls for: the Router
reproduces today's exact 19-agent tier-3 sequence, unchanged.
Run standalone:
    python -m pytest tests/test_eo_router.py -v
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.router import TIERS, DIRECTED_TASK_MAP, build_execution_graph, validate_registry_coverage
from eo.registry import REGISTRY, resolve
# Hand-verified against loop.py's run_one_cycle() call order, line by
# line, as of this commit. If this test ever fails, the fix is almost
# always in router.py (to match loop.py) — not the other way around,
# unless loop.py itself changed on purpose.
EXPECTED_TIER3_ORDER = [
    "memory_search",
    "idea_planner",
    "prompt_writer",
    "code_writers",
    "dependency_mapper",
    "test_writer",
    "reviewer",
    "duplication_checker",
    "fixer_pool",
    "sandbox_tester",
    "structure_architect",
    "security_scanner",
    "security_aggregator",
    "file_manager",
    "documentation_agent",
    "changelog_writer",
    "report_writer",
    "final_qa",
    "gatekeeper",
]
def test_tier3_matches_loop_py_exactly():
    assert TIERS[3]["agents"] == EXPECTED_TIER3_ORDER
    assert build_execution_graph(tier=3) == EXPECTED_TIER3_ORDER
def test_every_tier3_agent_resolves_to_a_real_callable():
    for name in TIERS[3]["agents"]:
        fn = resolve(name)
        assert callable(fn), f"{name} did not resolve to a callable"
def test_every_directed_task_map_agent_resolves_to_a_real_callable():
    for task_type, agent_names in DIRECTED_TASK_MAP.items():
        for name in agent_names:
            fn = resolve(name)
            assert callable(fn), f"{task_type} -> {name} did not resolve"
def test_registry_covers_every_referenced_agent_name():
    # Doesn't raise == passes.
    validate_registry_coverage()
def test_unknown_agent_name_raises_keyerror_not_silent_none():
    try:
        resolve("not_a_real_agent")
        assert False, "expected KeyError"
    except KeyError:
        pass
def test_tier2_directed_task_routing():
    assert build_execution_graph(tier=2, directed_task_type="review") == ["reviewer"]
    assert build_execution_graph(tier=2, directed_task_type="debug") == [
        "reviewer", "fixer_pool", "sandbox_tester", "file_manager_writeback",
    ]
    # add_tests: covers the tier-2 persistence-gap fix -- test_writer's
    # generated test_code now gets written to disk via
    # file_manager_test_writeback, not silently dropped.
    assert build_execution_graph(tier=2, directed_task_type="add_tests") == [
        "test_writer", "sandbox_tester", "file_manager_test_writeback",
    ]
def test_tier2_missing_directed_task_type_raises():
    try:
        build_execution_graph(tier=2)
        assert False, "expected ValueError"
    except ValueError:
        pass
def test_tier2_unknown_directed_task_type_raises():
    try:
        build_execution_graph(tier=2, directed_task_type="not_a_real_task")
        assert False, "expected KeyError"
    except KeyError:
        pass
def test_explain_code_routes_to_responder():
    # Stage 4 step 2: responder.py exists now, so explain_code routes for
    # real instead of raising NotImplementedError.
    assert build_execution_graph(tier=2, directed_task_type="explain_code") == ["responder"]

def test_tier_0_routes_to_responder():
    # Stage 4 step 4.
    assert build_execution_graph(tier=0) == ["responder"]

def test_tier_1_routes_to_lean_pipeline():
    assert build_execution_graph(tier=1) == [
        "prompt_writer_lean", "code_writer_lean", "reviewer_fixer_lean",
    ]

def test_tier_1_run_tests_appends_sandbox_tester_lean():
    graph = build_execution_graph(tier=1, run_tests=True)
    assert graph[-1] == "sandbox_tester_lean"
    assert build_execution_graph(tier=1, run_tests=False) == build_execution_graph(tier=1)

def test_every_tier0_and_tier1_agent_resolves_to_a_real_callable():
    for name in build_execution_graph(tier=0) + build_execution_graph(tier=1, run_tests=True):
        fn = resolve(name)
        assert callable(fn), f"{name} did not resolve"
def test_unknown_tier_raises_value_error():
    try:
        build_execution_graph(tier=99)
        assert False, "expected ValueError"
    except ValueError:
        pass
if __name__ == "__main__":
    # Allow `python tests/test_eo_router.py` without pytest, matching the
    # style of the other standalone test scripts in this repo.
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)