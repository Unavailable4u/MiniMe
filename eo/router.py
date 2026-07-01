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
# "explain_code" is deliberately absent from this map: per Part 4's note,
# it routes to the Responder (Part 2.3) instead of any of the 19 — a
# tier-0/1-only agent that doesn't exist in this codebase yet.
#
# Not wired into anything that executes yet — Stage 4.5 in the roadmap.
# ---------------------------------------------------------------------------
DIRECTED_TASK_MAP = {
    "review":        ["reviewer"],
    "debug":         ["reviewer", "fixer_pool", "sandbox_tester"],
    "add_tests":     ["test_writer", "sandbox_tester"],
    "refactor":      ["code_writers"],
    "security_scan": ["security_scanner"],
    "write_docs":    ["documentation_agent"],
}

# Tier-0 (Responder) and tier-1 (lean pipeline: Part 2.3-2.4) aren't in
# TIERS yet — those agents don't exist in this codebase. Adding them here
# before they're built would let build_execution_graph() claim it can run
# a tier it actually can't.


def build_execution_graph(tier: int, directed_task_type: str = None) -> list:
    """
    Returns an ordered list of agent-name strings (each resolvable via
    eo.registry.resolve()) for the given tier.

    Raises ValueError for an unknown tier, NotImplementedError for a real
    but not-yet-buildable tier (0, 1), and KeyError if directed_task_type
    isn't in DIRECTED_TASK_MAP for tier 2.
    """
    if tier == 3:
        return list(TIERS[3]["agents"])

    if tier == 2:
        if not directed_task_type:
            raise ValueError("tier 2 requires a directed_task_type.")
        if directed_task_type == "explain_code":
            raise NotImplementedError(
                "explain_code routes to the Responder (Part 2.3), which "
                "isn't built yet — see the roadmap, Stage 4 steps 2-4."
            )
        if directed_task_type not in DIRECTED_TASK_MAP:
            raise KeyError(f"Unknown directed_task_type '{directed_task_type}'.")
        return list(DIRECTED_TASK_MAP[directed_task_type])

    if tier in (0, 1):
        raise NotImplementedError(
            f"Tier {tier} isn't buildable yet — it depends on the "
            f"Responder / lean pipeline agents (Part 2.3-2.4), which "
            f"aren't in this codebase. See roadmap Stage 4, steps 2-4."
        )

    raise ValueError(f"Unknown tier: {tier!r}")


def validate_registry_coverage() -> None:
    """
    Walks every agent name referenced by TIERS and DIRECTED_TASK_MAP and
    confirms it resolves in eo.registry.REGISTRY. Raises on the first
    gap. Call this in tests (see tests/test_eo_router.py) and optionally
    at process startup once loop_v4.py exists.
    """
    all_names = set(TIERS[3]["agents"])
    for names in DIRECTED_TASK_MAP.values():
        all_names.update(names)

    missing = [name for name in sorted(all_names) if name not in REGISTRY]
    if missing:
        raise AssertionError(
            f"These agent names are referenced by router.py but missing "
            f"from eo.registry.REGISTRY: {missing}"
        )
