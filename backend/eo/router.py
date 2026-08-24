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

# Step 3 of the parallel-execution work (see eo/panel.py's
# _merge_parallel_groups() for how a group makes it this far, and
# sanitize_parallel_groups() below for where this is enforced). A cap
# against a degenerate/adversarial "everyone in one group" proposal --
# unrelated to MODE_CEILINGS above, which bounds total roster size, not
# how many of those roles can be told to run at once.
MAX_PARALLEL_GROUP_SIZE = 4
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


def _flatten_role_names(execution_order: list) -> set:
    """Every role name appearing anywhere in an execution_order list,
    whether as a flat entry or inside an existing nested group (e.g. one
    a saved workflow template already carries in from a PRIOR pass
    through this same sanitizer, or from Part 2 §2.6's template-authored
    grouping — this sanitizer has to be safe to run on either shape)."""
    flat = set()
    for entry in execution_order or []:
        if isinstance(entry, list):
            flat.update(r for r in entry if isinstance(r, str))
        elif isinstance(entry, str):
            flat.add(entry)
    return flat


def sanitize_parallel_groups(parallel_groups: list, execution_order: list,
                              approval_roles: list, hires: list) -> list:
    """
    Step 3 of the parallel-execution work — the HARD gatekeeper. Everything
    upstream of this function is a proposal, not a guarantee:
      - eo/inspector.py Step 1 lets a single model free-associate
        `parallel_groups` into its raw JSON output.
      - eo/panel.py's _merge_parallel_groups() (Step 2) only enforces
        "at least 2 of 3 panel members agreed" — it has no idea what was
        actually hired, which roles are approval checkpoints, or what
        eo/executor.py's own concurrency mechanism can safely support.

    This is the one place that decides a group is safe enough to become a
    real nested-list entry in execution_order (the shape
    build_execution_graph_from_hires() above already understands, per its
    own Part 2 §2.6 docstring — this function's job is producing that
    shape safely, not inventing a new one). Malformed or adversarial model
    output must degrade to the existing flat, sequential execution_order,
    never raise and never silently do something unsafe.

    A candidate group survives only if ALL of:
      - every member names a role that was ACTUALLY hired (present in
        `hires`) — a group can't run something that was never staffed.
      - every member also appears somewhere in `execution_order` itself —
        if a group references a role the final order doesn't even know
        about, something upstream disagrees with itself; drop rather than
        guess where it belongs.
      - no member is in `approval_roles` — eo/executor.py's
        _run_concurrent_group() has no support for pausing mid-group for
        the human-approval checkpoint (its own documented limitation), so
        a checkpoint role can never be folded into a concurrent group.
      - it doesn't overlap any role already claimed by an earlier-accepted
        group — no role runs in two places, and a partially-safe group
        (some members clean, one member reused) is dropped WHOLESALE
        rather than partially honored, same as any other failed check.
      - it has at least 2 members after dedup — a "group" of one isn't a
        concurrency claim, it's just a normal sequential slot.
      - it has at most MAX_PARALLEL_GROUP_SIZE members — a cap against a
        degenerate or adversarial "everyone in one group" proposal.

    Candidates are processed in the order given, so callers that want a
    deterministic winner among overlapping candidates should pass
    `parallel_groups` pre-sorted (eo/panel.py's _merge_parallel_groups()
    already returns a deterministically-ordered list for exactly this
    reason).

    Returns a NEW list in execution_order's own shape: every surviving
    group's members collapsed into one nested sublist at the position of
    that group's earliest-appearing member in `execution_order`; every
    other entry (whether it was already flat or already a pre-existing
    nested group untouched by this pass) is left exactly where it was, in
    its original relative order. Roles present in `hires` but absent from
    `execution_order` are intentionally NOT appended here —
    build_execution_graph_from_hires() already does that itself (see its
    own docstring), so duplicating it here would just be two places that
    could drift out of sync.
    """
    hired_roles = {h["role"] for h in (hires or [])}
    approval_set = set(approval_roles or [])
    order_role_set = _flatten_role_names(execution_order)

    accepted, used = [], set()
    for raw_group in parallel_groups or []:
        if not isinstance(raw_group, list):
            continue
        # Dedup within the candidate itself first — a group can't overlap
        # itself, and this keeps the size check below honest.
        role_set, seen = [], set()
        for r in raw_group:
            if isinstance(r, str) and r not in seen:
                seen.add(r)
                role_set.append(r)

        if len(role_set) < 2 or len(role_set) > MAX_PARALLEL_GROUP_SIZE:
            continue
        if any(r not in hired_roles for r in role_set):
            continue
        if any(r not in order_role_set for r in role_set):
            continue
        if any(r in approval_set for r in role_set):
            continue
        if any(r in used for r in role_set):
            # Overlaps a group already accepted earlier in this pass —
            # drop this whole candidate rather than trim it down to the
            # non-overlapping remainder, which would silently turn into a
            # DIFFERENT group than anything the Panel actually proposed.
            continue

        accepted.append(role_set)
        used.update(role_set)

    if not accepted:
        # Nothing survived — hand back execution_order completely
        # unmodified rather than an equivalent-but-rebuilt copy, so a
        # caller doing identity/equality checks in a test sees exactly
        # what it passed in.
        return list(execution_order or [])

    role_to_group = {r: tuple(g) for g in accepted for r in g}
    result, emitted = [], set()
    for entry in execution_order or []:
        if isinstance(entry, list):
            # A pre-existing nested group this pass didn't touch (e.g.
            # every one of its members failed a check, or it came in
            # already grouped from a template) — passed through as-is.
            result.append(entry)
            continue
        if entry in emitted:
            # Already emitted as part of an accepted group below.
            continue
        group_key = role_to_group.get(entry)
        if group_key is None:
            result.append(entry)
            continue
        result.append(list(group_key))
        emitted.update(group_key)

    return result


def build_execution_graph_from_hires(hires: list, execution_order: list = None) -> tuple:
    """
    Migration Part 5 §2.1, extended by Part 10 §3.1/§4, corrected by
    Part 11 §0. Migration Part 2 §2.6: execution_order may now contain
    nested groups (see below).

    hires: [{"role": "implementer", "agent_key": "CEREBRAS_CODE_1", "brief": "..."}, ...]
        — always a FLAT list, one entry per role actually staffed by
        staff_task(). Grouping (below) is purely an execution-order
        concept; every role, grouped or not, still needs its own
        individual hire (its own account and brief) exactly as before.
    execution_order: the Panel's synthesized ordering (Part 10 §3), OR a
        saved workflow template's `roles` (Part 2 §2.3) — a list of role
        name strings, where a top-level entry may ALSO be a nested list
        of role name strings marking a group its author said is safe to
        run concurrently (Part 2 §2.6). Optional: omitting it (every
        call site from Parts 1-9) preserves hire order exactly as
        before, since the reorder step below only runs `if
        execution_order`.

    Returns a 3-tuple:

        agent_names: ["code_writers", "generic_worker", "generic_worker", ...]
            — a position that's a concurrent group is "generic_worker"
            here too (every group member is, by construction, a
            reasoning-only role that resolves to generic_worker anyway
            — see eo/structure.py's save_workflow_template() docstring),
            since eo/executor.py only needs agent_names[idx] to say
            "there's something to resolve/dispatch here"; it branches on
            role_names[idx]'s actual shape (str vs list) to tell a group
            apart from a single hire.
        role_names: ["implementer", "brainstormer", ["writer_a", "writer_b"], ...]
            — PARALLEL to agent_names, same length. role_names[i] is
            either the single role for agent_names[i], or, for a
            collapsed group, a list of every role in that group that was
            ACTUALLY staffed (see below — a group with only one member
            actually hired collapses back down to a plain single-role
            slot, not a "group of one").
        key_overrides: {"implementer": "CEREBRAS_CODE_1", "brainstormer": "...", ...}
            — keyed by ROLE NAME, not resolved agent/module name, one
            entry per individual hire regardless of grouping.

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
    execution_order list needing to be threaded through. (For a group at
    an earlier position, that's a nested list inside the slice —
    eo/executor.py's own _flatten_role_names() handles that.)

    Migration Part 2 §2.6 — grouping mechanics:
    order_index maps each role name to the position of its own top-level
    slot in execution_order — every member of a group shares its group's
    single position, so sorting hires by this key (Python's sort is
    stable) always leaves group members contiguous with each other,
    without disturbing their order relative to everything else. A second
    pass then walks the now-sorted hires once, collapsing each
    contiguous run of same-group hires into one role_names/agent_names
    slot. Only roles BOTH marked as a group in execution_order AND
    actually present in hires end up in the collapsed slot — a group
    with 2+ members hired becomes a real concurrent slot; a group with
    only 1 member actually staffed (the other candidate(s) never got an
    available account, e.g. staff_task()'s own `_best_match() is None`
    skip) quietly degrades to an ordinary single-role slot rather than
    running "a group of one" or referencing a role that was never
    staffed at all.
    """
    role_to_group = {}
    if execution_order:
        order_index = {}
        for i, entry in enumerate(execution_order):
            if isinstance(entry, list):
                group_key = tuple(entry)
                for role in entry:
                    order_index[role] = i
                    role_to_group[role] = group_key
            else:
                order_index[entry] = i
        # hires not mentioned in execution_order (Panel forgot one, or a
        # role was added after ordering) go to the end, in their
        # original hire order — never dropped
        hires = sorted(hires, key=lambda h: order_index.get(h["role"], len(execution_order)))

    agent_names, role_names = [], []
    key_overrides = {}   # keyed by ROLE now, not by resolved module name

    def _record_key_override(role: str, agent_key: str) -> None:
        existing = key_overrides.get(role)
        if existing is None:
            key_overrides[role] = agent_key
        elif isinstance(existing, list):
            existing.append(agent_key)
        else:
            key_overrides[role] = [existing, agent_key]

    i = 0
    while i < len(hires):
        role = hires[i]["role"]
        group_key = role_to_group.get(role)

        if group_key is None:
            agent_names.append(resolve_role(role))
            role_names.append(role)
            _record_key_override(role, hires[i]["agent_key"])
            i += 1
            continue

        # Collect every hire that belongs to this same group and is
        # contiguous here — the stable sort above guarantees group
        # members always end up adjacent to each other.
        group_roles_present = []
        while i < len(hires) and role_to_group.get(hires[i]["role"]) == group_key:
            member_role = hires[i]["role"]
            group_roles_present.append(member_role)
            _record_key_override(member_role, hires[i]["agent_key"])
            i += 1

        if len(group_roles_present) == 1:
            # Only one member of this group actually got staffed —
            # nothing to run concurrently WITH, so this is just a
            # normal single-role slot, not a group of one.
            agent_names.append(resolve_role(group_roles_present[0]))
            role_names.append(group_roles_present[0])
        else:
            agent_names.append("generic_worker")
            role_names.append(group_roles_present)

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