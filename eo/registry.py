
"""
eo/registry.py — Stage 4, step 1 of the v5 Master Blueprint's build roadmap
(Part 10).
Single source of truth mapping an agent *name* (string, the vocabulary the
Inspector/Router speak in) to the real, importable Python callable that
does the work, plus a couple of bits of metadata router.py and the future
executor need.
This module intentionally does NOT execute anything. It only resolves
names -> callables. That keeps it safe to import from tests, from
router.py, and eventually from an executor, without any side effects
(no LLM calls, no memory writes) just from `import eo.registry`.
The production 19-agent roster (tier 3, Part 4 of the blueprint), plus
Tier 0's Responder and Tier 1's lean pipeline (Part 2.3-2.4, added in
Stage 4 steps 2-4 of the roadmap), are all wired up here.
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents import (
    memory_search,
    idea_planner,
    prompt_writer,
    code_writers,
    dependency_mapper,
    test_writer,
    reviewer,
    duplication_checker,
    fixer_pool,
    sandbox_tester,
    structure_architect,
    security_scanner,
    file_manager,
    documentation_agent,
    changelog_writer,
    report_writer,
    final_qa,
    gatekeeper,
    responder,
    prompt_writer_lean,
    code_writer_lean,
    reviewer_fixer_lean,
)
# name -> {"callable": fn, "needs_cycle_num": bool}
#
# "needs_cycle_num" flags the one agent (Gatekeeper) whose run function
# takes an argument instead of being a plain no-arg call — the executor
# built in a later stage needs to know this; router.py itself doesn't care.
REGISTRY = {
    "memory_search":       {"callable": memory_search.run,                "needs_cycle_num": False},
    "idea_planner":        {"callable": idea_planner.run,                 "needs_cycle_num": False},
    "prompt_writer":       {"callable": prompt_writer.run,                "needs_cycle_num": False},
    "code_writers":        {"callable": code_writers.run,                 "needs_cycle_num": False},
    "dependency_mapper":   {"callable": dependency_mapper.run,            "needs_cycle_num": False},
    "test_writer":         {"callable": test_writer.run,                  "needs_cycle_num": False},
    "reviewer":            {"callable": reviewer.run_reviewer,            "needs_cycle_num": False},
    "duplication_checker": {"callable": duplication_checker.run,          "needs_cycle_num": False},
    "fixer_pool":          {"callable": fixer_pool.run_fixer_pool,        "needs_cycle_num": False},
    "sandbox_tester":      {"callable": sandbox_tester.run_sandbox_tester,"needs_cycle_num": False},
    "structure_architect": {"callable": structure_architect.run_structure_architect, "needs_cycle_num": False},
    "security_scanner":    {"callable": security_scanner.run,             "needs_cycle_num": False},
    "file_manager":        {"callable": file_manager.run_file_manager,    "needs_cycle_num": False},
    "documentation_agent": {"callable": documentation_agent.run,          "needs_cycle_num": False},
    "changelog_writer":    {"callable": changelog_writer.run,             "needs_cycle_num": False},
    "report_writer":       {"callable": report_writer.run_report_writer,  "needs_cycle_num": False},
    "final_qa":            {"callable": final_qa.run,                    "needs_cycle_num": False},
    "gatekeeper":          {"callable": gatekeeper.run_gatekeeper,        "needs_cycle_num": True},
    # --- Tier 0 (Part 2.3) ---
    "responder":           {"callable": responder.run,                   "needs_cycle_num": False},
    # --- Tier 1 lean pipeline (Part 2.4) ---
    "prompt_writer_lean":     {"callable": prompt_writer_lean.run,          "needs_cycle_num": False},
    "code_writer_lean":       {"callable": code_writer_lean.run,            "needs_cycle_num": False},
    "reviewer_fixer_lean":    {"callable": reviewer_fixer_lean.run,         "needs_cycle_num": False},
    "sandbox_tester_lean":    {"callable": sandbox_tester.run_sandbox_tester_lean, "needs_cycle_num": False},
}
def resolve(agent_name: str):
    """Return the callable for `agent_name`, or raise KeyError with a
    clear message — never return None and let a caller silently no-op."""
    entry = REGISTRY.get(agent_name)
    if entry is None:
        raise KeyError(
            f"'{agent_name}' is not in eo.registry.REGISTRY. Either it's "
            f"misspelled, or it's a tier-0/1-only agent that hasn't been "
            f"built yet (see the module docstring)."
        )
    return entry["callable"]
