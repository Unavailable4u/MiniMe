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
"""
from eo.registry import REGISTRY, resolve_role
# ---------------------------------------------------------------------------
# Tier 3 — full roster, Part 4 of the blueprint.
#
# This list reflects the system's actual production call order, not just
# what Part 4's table implies.
#
#   Note (corrected): agents/review_aggregator.py's aggregate_reviews()
#   IS called -- from inside agents/reviewer.py's run_reviewer(), right
#   after the 3-worker Reviewer Pool finishes, before review_notes is
#   written. It never needs its own line in this roster because it isn't
#   a standalone pipeline step -- it's an internal merge step inside the
#   "reviewer" agent itself, same as security_aggregator.py is a
#   standalone roster entry for Scanner Pool but review_aggregator.py is
#   not for Reviewer Pool.
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
    # Migration Part 27 §2: TIERS[3] (the classic, fixed 19-agent
    # roster) is retired -- confirmed dead: both live entrypoints
    # (eo/loop_v4.py, api/task_runner.py) always route tier 3 through
    # the adaptive/hires-driven path (_run_tier3_hires() ->
    # build_execution_graph_from_hires()), never through
    # build_execution_graph(tier=3). Nothing called this. The four
    # modules that were only reachable through it (dependency_mapper,
    # duplication_checker, structure_architect, memory_search) do real
    # work generic_worker can't replicate, so rather than deleting them
    # along with this dead list, they're wired into the live path
    # instead via eo/registry.py's REAL_ACTION_ROLES -- see that dict's
    # comment. gatekeeper/changelog_writer/final_qa's dedicated modules
    # really were dead weight (reasoning-only, duplicated or
    # unreachable) and were deleted outright, not migrated.
}

# Mode ceilings (blueprint §8, raised from the original Blueprint's 14/9/11
# to reflect the reserve-account capacity added in this part). Not used by
# build_execution_graph() itself — consumed by eo/modes.py (new, step 3).
MODE_CEILINGS = {
    "auto": 16,
    "simple": 10,
    "fast": 13,
    "expert": None,      # no ceiling
    "beast": None,        # sized as ~2.5x assessed max instead, see eo/modes.py
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
    "debug":         ["reviewer", "fixer_pool", "sandbox_tester", "file_manager_writeback"],
    "add_tests":     ["test_writer", "sandbox_tester", "file_manager_test_writeback"],
    "refactor":      ["code_writers", "file_manager_writeback"],
    "security_scan": ["security_scanner", "security_aggregator"],
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

    Tier 3 has no entry here anymore (Migration Part 27 §2) -- it never
    ran through this static-graph function in practice (both live
    entrypoints always go through build_execution_graph_from_hires()
    instead), so this now raises ValueError for tier 3 same as any other
    unknown tier, rather than silently returning a graph nothing ever
    executed.

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
    raise ValueError(f"Unknown tier: {tier!r}")


def build_execution_graph_from_hires(hires: list, execution_order: list = None) -> tuple:
    """
    Migration Part 5 §2.1, extended by Part 10 §3.1/§4, corrected by
    Part 11 §0.

    hires: [{"role": "implementer", "agent_key": "CEREBRAS_CODE_1", "brief": "..."}, ...]
    execution_order: the Panel's synthesized ordering (Part 10 §3) — a
        list of role name strings. Optional: omitting it (every call site
        from Parts 1-9) preserves hire order exactly as before, since the
        reorder step below only runs `if execution_order`.

    Returns a 3-tuple:

        agent_names: ["code_writers", "generic_worker", "generic_worker", ...]
        role_names: ["implementer", "brainstormer", "writer", ...] — PARALLEL
            to agent_names, same order, same length. role_names[i] is
            always the real role for agent_names[i].
        key_overrides: {"implementer": "CEREBRAS_CODE_1", "brainstormer": "...", ...}
            — keyed by ROLE NAME, not resolved agent/module name.

    Part 11 §0 fix: key_overrides used to be keyed by resolved module
    name. That broke once Part 10 introduced generic_worker as the
    shared module for many different roles — "brainstormer" and "writer"
    both resolve to the literal string "generic_worker", so a dict keyed
    that way let one hire's account choice silently clobber another's.
    It also mismatched real-action roles whose role name and module name
    were never actually identical (e.g. "verifier" resolves to module
    "reviewer") — collapsing those together conflated two distinct
    hiring decisions into one call. Keying by role name fixes both: every
    hire keeps its own distinct account choice, and only hires that
    genuinely share the same role name (a real worker-pool hire, the
    same role staffed more than once) collapse their keys into a list.

    Note: role_names is built in the exact order agent_names is, which
    IS the effective execution order after the optional reorder step
    below — so a caller (eo/executor.py) can use role_names[:idx] directly
    as "every role that already ran before this point," with no separate
    execution_order list needing to be threaded through.
    """
    if execution_order:
        order_index = {role: i for i, role in enumerate(execution_order)}
        # hires not mentioned in execution_order (Panel forgot one, or a
        # role was added after ordering) go to the end, in their
        # original hire order — never dropped
        hires = sorted(hires, key=lambda h: order_index.get(h["role"], len(execution_order)))

    agent_names, role_names = [], []
    key_overrides = {}   # keyed by ROLE now, not by resolved module name
    for hire in hires:
        agent_names.append(resolve_role(hire["role"]))
        role_names.append(hire["role"])
        existing = key_overrides.get(hire["role"])
        if existing is None:
            key_overrides[hire["role"]] = hire["agent_key"]
        elif isinstance(existing, list):
            existing.append(hire["agent_key"])
        else:
            key_overrides[hire["role"]] = [existing, hire["agent_key"]]

    return agent_names, role_names, key_overrides


def validate_registry_coverage() -> None:
    """
    Walks every agent name referenced by TIERS, DIRECTED_TASK_MAP, and
    EXPLAIN_CODE_ROUTE and confirms it resolves in eo.registry.REGISTRY.
    Raises on the first gap. Call this in tests (see
    tests/test_eo_router.py) and optionally at process startup.

    Migration Part 27 §2: no longer includes TIERS[3] -- that key was
    removed (the classic 19-agent roster is retired; nothing ever called
    build_execution_graph(tier=3)). Tier 3's real agent-name coverage now
    comes entirely from the hires-driven path (eo.registry.REAL_ACTION_ROLES
    / resolve_role()), which this function doesn't need to separately
    validate -- resolve_role() always returns either a real REGISTRY key
    or the literal "generic_worker", both of which are guaranteed present.
    """
    all_names = set(TIERS[0]["agents"]) | set(TIERS[1]["agents"])
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