
"""
eo/router.py — Stage 4, step 1 of the v5 Master Blueprint's build roadmap
(Part 10, Stage 4.1):
    "Build registry.py + router.py against tier 3 only — confirm the
    Router can reproduce today's exact 19-agent sequence with zero
    behavior change, before touching routing logic."
This module only ANSWERS THE QUESTION "for this tier (and, at tier 2,
this directed_task_type), which agent names run, in what order?" It does
not call anything itself — see eo/registry.py for name->callable
resolution, and a later-stage executor for actually running the graph.
Nothing in loop.py is touched by this file. It is purely additive and
inert until something imports and calls it.
"""
from eo.registry import REGISTRY
# ---------------------------------------------------------------------------
# Tier 3 — full roster, Part 4 of the blueprint.
#
# This list is deliberately hand-copied from loop.py's actual call order
# (not "what Part 4's table implies"), because the point of this step is
# fidelity to what the system does today, not to what the table says it
# should do. Note the one real discrepancy this surfaced:
#
#   agents/review_aggregator.py exists (agent #8, "deterministic Python,
#   no LLM") but loop.py never imports or calls it — reviewer.py's output
#   goes straight to duplication_checker.py. That's a pre-existing gap
#   between the blueprint and the code, not something this router
#   introduces or silently papers over. Flagging it here rather than
#   guessing at a fix.
# ---------------------------------------------------------------------------
TIERS = {
    0: {
        # Part 2.3 — Responder. Takes task_text directly (see
        # eo/executor.py); nothing else runs at tier 0 (Part 5.1: no
        # Upstash, no E2B, no git).
        "agents": ["responder"],
    },
    1: {
        # Part 2.4 — lean pipeline. sandbox_tester_lean is appended
        # separately by build_execution_graph() only when the caller
        # says the user asked to run/test the result (Part 2.4:
        # "Sandbox Tester (optional) ... only invoked if the user asked
        # to run/test the result") -- it is NOT unconditional like the
        # other three steps.
        "agents": ["prompt_writer_lean", "code_writer_lean", "reviewer_fixer_lean"],
    },
    3: {
        "agents": [
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
            "file_manager",
            "documentation_agent",
            "changelog_writer",
            "report_writer",
            "final_qa",
            "gatekeeper",
        ],
    },
}
# ---------------------------------------------------------------------------
# Tier 2 — directed-task subsets of the SAME 19-agent roster (Part 2.5:
# "No new models — Tier 2 calls directly into the existing 19-agent
# roster's specialists, using exactly their production model
# assignments"). Built from Part 4's "Tiers that call it" column.
#
# "explain_code" routes to the Responder (Part 2.3) instead of any of the
# 19, per Part 4's note — included here now that responder.py exists
# (Stage 4 step 2). Kept in its own EXPLAIN_CODE_ROUTE constant rather
# than folded into DIRECTED_TASK_MAP so a caller can't accidentally treat
# it as "run these 19-roster agents" -- explain_code is read-only and
# doesn't touch submitted_code at all, unlike every other directed task.
# ---------------------------------------------------------------------------
DIRECTED_TASK_MAP = {
    "review":        ["reviewer"],
    "debug":         ["reviewer", "fixer_pool", "sandbox_tester"],
    "add_tests":     ["test_writer", "sandbox_tester"],
    "refactor":      ["code_writers"],
    "security_scan": ["security_scanner"],
    "write_docs":    ["documentation_agent"],
}
EXPLAIN_CODE_ROUTE = ["responder"]


def build_execution_graph(tier: int, directed_task_type: str = None, run_tests: bool = False) -> list:
    """
    Returns an ordered list of agent-name strings (each resolvable via
    eo.registry.resolve()) for the given tier.

    `run_tests` only affects tier 1 (Part 2.4: Sandbox Tester is optional,
    "only invoked if the user asked to run/test the result") — appends
    sandbox_tester_lean to the end of the tier-1 graph when True. Ignored
    for every other tier, so callers can pass it unconditionally without
    branching on tier first.

    Raises ValueError for an unknown tier or a tier-2 call missing
    directed_task_type, and KeyError if directed_task_type isn't in
    DIRECTED_TASK_MAP.
    """
    if tier == 0:
        return list(TIERS[0]["agents"])
    if tier == 1:
        graph = list(TIERS[1]["agents"])
        if run_tests:
            graph.append("sandbox_tester_lean")
        return graph
    if tier == 2:
        if not directed_task_type:
            raise ValueError("tier 2 requires a directed_task_type.")
        if directed_task_type == "explain_code":
            return list(EXPLAIN_CODE_ROUTE)
        if directed_task_type not in DIRECTED_TASK_MAP:
            raise KeyError(f"Unknown directed_task_type '{directed_task_type}'.")
        return list(DIRECTED_TASK_MAP[directed_task_type])
    if tier == 3:
        return list(TIERS[3]["agents"])
    raise ValueError(f"Unknown tier: {tier!r}")


def validate_registry_coverage() -> None:
    """
    Walks every agent name referenced by TIERS, DIRECTED_TASK_MAP, and
    EXPLAIN_CODE_ROUTE and confirms it resolves in eo.registry.REGISTRY.
    Raises on the first gap. Call this in tests (see
    tests/test_eo_router.py) and optionally at process startup.
    """
    all_names = set(TIERS[0]["agents"]) | set(TIERS[1]["agents"]) | set(TIERS[3]["agents"])
    all_names.update(EXPLAIN_CODE_ROUTE)
    all_names.add("sandbox_tester_lean")
    for names in DIRECTED_TASK_MAP.values():
        all_names.update(names)
    missing = [name for name in sorted(all_names) if name not in REGISTRY]
    if missing:
        raise AssertionError(
            f"These agent names are referenced by router.py but missing "
            f"from eo.registry.REGISTRY: {missing}"
        )
