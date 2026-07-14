"""
eo/executor.py — runs an execution graph built by eo/router.py.

Execution navigates by ROLE, not by resolved module name: generic_worker
runs for many different roles in the same plan, so a "next_destination:
<role>" value from the Dispatcher needs to disambiguate which
generic_worker SLOT in agent_names it means (multiple slots can all
literally be the string "generic_worker"). eo/dispatcher.py's next_step()
returns an INDEX into role_names; this module resolves agent_names[idx]
separately at the moment of calling.

The "instant"/"direct"/"fixed" paths' static graphs (build_execution_graph())
have no separate role concept — role IS the module name there, so
role_names defaults to a copy of agent_names. Hires-driven ("adaptive"
path / Panel) calls always pass role_names explicitly, via
eo.router.build_execution_graph_from_hires().

Two agent names are entry points that need the raw task text passed in
directly the first time:
  - "responder"          (path "instant" — the only agent in its graph)
  - "prompt_writer_lean" (path "direct" — the first agent in its graph)
Every other agent name reads its input from memory.bus, since the agent
before it in the same graph already wrote it there.

Each step fires agent_start/agent_done (and error, on failure) through
relay/emitter.py. session_id defaults to None, which makes emit_event() a
no-op, so callers that don't pass a session_id are unaffected.

key_overrides maps a ROLE name to the specific key_env(s) the Panel hired
for it (eo.router.build_execution_graph_from_hires()). Defaults to {}, so
each agent module falls back to its own internal default key selection.

Human-in-the-loop checkpoints (approval_roles): execute_graph() is split
into a thin entry point plus a shared _run_loop() helper, so resume_graph()
can re-enter the same dispatch/escalation/pause logic from a persisted
snapshot instead of duplicating it. See _run_loop() and resume_graph()
below.

Scoped memory per agent (no_conversation_context_roles): Part 2 §2.6. A
role name in this set is dispatched through generic_worker.run() with
include_conversation_context=False, so it doesn't get the full
conversation-memory transcript prepended ahead of context it wasn't
scoped to see. Defaults to an empty set (today's exact behavior — every
role sees the full transcript) everywhere it's threaded through below.
"""
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import resolve, resolve_role, list_known_roles
from eo.structure import PATH_TO_TIER
from eo.errors import MissingDependencyError
from relay.emitter import emit_event

TASK_TEXT_ENTRYPOINTS = {"responder", "prompt_writer_lean"}

# An agent can raise MissingDependencyError instead of hard-failing the
# whole task when a prerequisite role's output isn't in memory yet (see
# eo/errors.py). MAX_AUTO_INSERTS_PER_STEP guards against an unsatisfiable
# or circular dependency looping forever (a role requesting itself, or two
# roles each requesting the other). Keyed per (role, requested_role) pair,
# so one step's retries don't eat another step's budget.
MAX_AUTO_INSERTS_PER_STEP = 2

# Guards resume_graph()'s "reject_redo" path — a human can send a role
# back for a redo, but not forever. Keyed per-role since a reject_redo
# always targets the exact role that just paused.
MAX_STAGE_REVISITS = 2

# These agents take `tier` (int) rather than `path` (str), and don't fit
# any of the other dispatch cases below (they'd otherwise fall into the
# generic `else: fn()` branch and run with zero context). PATH_TO_TIER
# translates the current `path` back to the `tier` int they expect.
UNSCOPED_TIER_AGENTS = {
    "dependency_mapper", "documentation_agent", "duplication_checker",
    "memory_search",
    # Part 3 §3.6 — same (session_id, tier, domain) signature as the four
    # above; its real input is KEYS["extraction_table"], read straight
    # off the bus, not task_text/path.
    "contradiction_prefilter",
    # Part 3 §3.8 — same signature again; real input is
    # KEYS["academic_search_report"].
    "source_quality_flagger",
    # Part 3 — same signature; read-only, no bus-key input at all beyond
    # KEYS["academic_search_report"] and eo/graph_edges.py's list_edges().
    "citation_graph_builder",
}
# "structure_architect" is deliberately NOT in the set above — its
# no-code planning path needs task_text (to plan a folder/file scaffold
# when there's no fixed_code/submitted_code to organize yet), which the
# four agents above don't need. It gets its own dispatch case instead.


def _apply_recheck_retry(key_overrides: dict, role_names: list, next_idx, reason: str) -> None:
    """Migration Part 2 §2.6 — escalation logic's one genuine gap.

    Every other escalation path already existed (SGA's three-stage
    relay, Panel escalation, dispatcher-level "escalate"/prerequisite
    auto-insertion). What was missing: automatically retrying a role on
    a DIFFERENT, stronger/different account purely because its own
    output looked weak (dispatcher reason == "recheck"), without the
    agent having to self-report via a NEXT: tag naming some other role.

    A "recheck" from eo/dispatcher.py's next_step() means role_names[next_idx]
    is a role already run earlier in this plan, being revisited. Left
    alone, key_overrides still points that role at the exact same
    account that just produced the output weak enough to trigger the
    recheck in the first place — this forces a different one via
    eo.panel._best_match()'s new `exclude` param, mutating key_overrides
    in place so the next iteration of the loop picks it up naturally
    through the existing `override = key_overrides.get(role)` line.

    No-op for every other reason ("plan"/"escalate") and for a role's
    very first run (no prior override to exclude yet, so there's
    nothing to switch away from)."""
    if reason != "recheck" or next_idx is None:
        return
    from eo.panel import _best_match
    from eo.quota_sentinel import get_quota_snapshot

    retry_role = role_names[next_idx]
    last_key = key_overrides.get(retry_role)
    new_key = _best_match(retry_role, get_quota_snapshot(),
                           exclude={last_key} if last_key else None)
    if new_key:
        key_overrides[retry_role] = new_key


def _flatten_role_names(role_names: list) -> set:
    """Migration Part 2 §2.6: role_names[idx] may now be a list (a
    concurrent group — see _run_concurrent_group() below) instead of a
    plain role-name string. Every place that used to do a bare
    `set(role_names)` breaks the moment ANY position in the plan is a
    group — not just while that position is being processed — since a
    list isn't hashable. This flattens either shape into a plain set of
    role-name strings, for next_step()'s known_roles argument."""
    flat = set()
    for entry in role_names:
        if isinstance(entry, list):
            flat.update(entry)
        else:
            flat.add(entry)
    return flat


def _merge_group_next_destinations(votes: list):
    """Identical merge rule to agents/reviewer.py's own
    _merge_next_destinations(): majority vote wins; on a tie or no
    majority, the first non-None vote by member order wins; if every
    member said DONE (None) or gave no parseable tag, the merged result
    is None. Kept as its own small copy here rather than imported from
    agents/reviewer.py — that module's version is tupled together with
    its own review-specific logic, and importing agents/reviewer.py into
    eo/executor.py for one shared function isn't worth the new
    dependency edge."""
    from collections import Counter
    cast = [v for v in votes if v]
    if not cast:
        return None
    counts = Counter(cast)
    top_count = max(counts.values())
    winners = {v for v in cast if counts[v] == top_count}
    for v in cast:
        if v in winners:
            return v
    return None


def _run_concurrent_group(group_roles: list, role_names: list, idx: int, results: dict,
                            task_text: str, session_id: str, path: str, key_overrides: dict,
                            next_step, no_conversation_context_roles: set = None,
                            domain: str = None) -> tuple:
    """Migration Part 2 §2.6 — parallel execution control's real gap.

    The Panel-decided execution_order that generic_worker steps through
    is strictly sequential today, unlike the Code Writer/Reviewer/Fixer
    pools (which already run genuinely in parallel, but only inside
    their own dedicated real-action modules). role_names[idx] being a
    list rather than a str is what marks a concurrent group — produced
    ONLY when a workflow template author explicitly nested roles that
    way (eo/structure.py's save_workflow_template()); the Inspector/
    Panel's own automatic classification never produces one, so an
    ordinary run is completely unaffected.

    Runs every role in group_roles through generic_worker at once, via
    the identical ThreadPoolExecutor primitive agents/reviewer.py's
    worker pool already uses. Each member reads the SAME input_keys
    (every role that ran at any EARLIER position in the plan,
    role_names[:idx] flattened so an earlier group's members are each
    individually visible) but NOT each other's output — they're peers
    running at once, not a sequential hand-off, the same relationship
    reviewer.py's 3 workers already have to each other.

    Each member's own call to generic_worker.run() already writes its
    own stage_output:{session_id}:{role} key internally (keyed by its
    own role name) — nothing extra to persist here for that. What DOES
    need merging: each member's own next_destination vote, since
    eo/dispatcher.py's next_step() expects ONE result dict to reason
    about, not N — merged via the identical majority-vote rule
    agents/reviewer.py already uses for its own worker pool.

    no_conversation_context_roles (Part 2 §2.6, same set _run_loop()
    receives): each member is dispatched with
    include_conversation_context=(member_role not in this set), so the
    scoped-memory opt-out applies the same way inside a concurrent group
    as it does to a single sequential role.

    domain (Part 2 §2.6, cost-tracking gap): forwarded to each member's
    generic_worker.run() call the same way session_id already is, so
    every role in the group gets the same per-project/per-section usage
    attribution a sequential role gets. Defaults to None -- unaffected
    unless a caller (execute_graph()/_run_loop()) actually has one.

    Known v1 limitation, flagged rather than silently unsupported: a
    group does not currently support approval_roles pausing or a
    MissingDependencyError self-heal for any of its members. Both would
    need the pause/resume snapshot shape and the auto-insert bookkeeping
    to understand "idx currently covers N roles running together, not
    one" — real additional work, left for later if a group member ever
    actually needs either.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    no_conversation_context_roles = no_conversation_context_roles or set()
    fn = resolve("generic_worker")
    flat_input_keys = list(_flatten_role_names(role_names[:idx]))

    started_at = {}
    for member_role in group_roles:
        started_at[member_role] = _time.monotonic()
        emit_event("agent_start", session_id=session_id, agent=f"generic:{member_role}", path=path,
                    payload={"label": member_role})

    member_results = {}
    with ThreadPoolExecutor(max_workers=len(group_roles)) as pool:
        futures = {
            pool.submit(fn, role=member_role, task_text=task_text,
                        input_keys=flat_input_keys, session_id=session_id,
                        key_override=key_overrides.get(member_role),
                        include_conversation_context=member_role not in no_conversation_context_roles,
                        domain=domain,
                        ): member_role
            for member_role in group_roles
        }
        for future in as_completed(futures):
            member_results[futures[future]] = future.result()

    votes = []
    for member_role in group_roles:
        result = member_results[member_role]
        results[member_role] = result
        duration_ms = int((_time.monotonic() - started_at[member_role]) * 1000)
        emit_event("agent_done", session_id=session_id, agent=f"generic:{member_role}", path=path,
                    payload={"summary": _summarize(result, role=member_role), "duration_ms": duration_ms})
        votes.append(result.get("next_destination") if isinstance(result, dict) else None)

    merged_next = _merge_group_next_destinations(votes)
    next_idx, reason = next_step(
        {"next_destination": merged_next}, role_names, idx, session_id=session_id,
        known_roles=set(list_known_roles()) | _flatten_role_names(role_names),
    )
    return next_idx, reason


def _summarize(result, role: str = None, limit: int = 9000) -> str:
    """Best-effort human-readable summary for an agent_done payload.
    Results vary in shape (str, dict, ...) across the agent roster —
    eo/result_render.py's render_agent_result() knows every shape
    (generic_worker's {"text"}, reviewer's {"issues"}, fixer_pool's
    {"fixed_code"}, code_writers'/test_writer's flat {module: code} map,
    content_adapter_pool's flat {platform: content} map,
    content_calendar_builder's {date, platform, content_ref} row list,
    ...) and turns each into markdown instead of a raw dict repr.

    role (Part 6): forwarded to render_agent_result() so its
    content_adapter_pool branch can be gated on WHICH role produced
    `result`, not shape alone — a flat {platform: content} map is
    otherwise indistinguishable from code_writers'/test_writer's flat
    {module: code} map. Defaults to None (today's exact behavior for
    every call site that doesn't pass one).

    limit defaults to 9000: Pusher enforces a hard ~10KB payload limit
    per event, and the event envelope around this string costs a few
    hundred bytes, so 9000 is the most that fits with headroom. Only
    genuinely oversized results (e.g. a full multi-module code
    submission) still get cut, and now say so explicitly."""
    from eo.result_render import render_agent_result
    return render_agent_result(result, role=role, limit=limit)


# 4000 chars leaves room alongside _summarize()'s own text within
# Pusher's ~10KB-per-event limit -- the two share one payload. A result
# with an "image" key over this (e.g. agents/citation_graph_builder.py's
# SVG, for a large graph) still has it in the KEYS[...] bus value for
# anyone reading the bus directly; it's only the live Pusher stream that
# drops it, same "skip rather than corrupt the event" posture
# _summarize()'s own truncation already takes toward oversized text.
MAX_IMAGE_DATA_URI_CHARS = 4000


def _extract_image(result) -> str | None:
    """Any REAL_ACTION_ROLES module can opt in by putting a data-URI
    string under result["image"] -- generic by key, not by role name, so
    a future module gets this for free without another executor.py edit."""
    if isinstance(result, dict) and isinstance(result.get("image"), str):
        image = result["image"]
        if 0 < len(image) <= MAX_IMAGE_DATA_URI_CHARS:
            return image
    return None


def execute_graph(agent_names: list, role_names: list = None, task_text: str = None,
                   cycle_num: int = None, session_id: str = None, path: str = None,
                   mode: str = None, key_overrides: dict = None,
                   project_unique_name: str = None, approval_roles: set = None,
                   no_conversation_context_roles: set = None, domain: str = None) -> dict:
    """Fresh-start entry point. `approval_roles` defaults to None (today's
    full-auto behavior) — every existing call site that doesn't pass it is
    unaffected.

    `no_conversation_context_roles` (Part 2 §2.6) defaults to None (today's
    exact behavior — every role sees the full conversation-memory prepend).
    A caller dispatching a saved workflow template passes
    template["no_conversation_context_roles"] here, exactly the same
    wiring pattern approval_roles already uses (see
    eo/structure.py's classification_from_template()).

    `domain` (Part 2 §2.6, cost-tracking gap): the classification domain
    this run belongs to (e.g. "coding"/"simulate" — api/task_runner.py's
    _run_tier3_hires() already has this as decision.get("domain") and
    passes it down through eo/loop_controller.py's run_with_looping()).
    Forwarded to every generic_worker dispatch (single-role and
    concurrent-group) so utils/llm_client.py's log_usage() can tag each
    call for the per-project/per-section usage breakdown. Defaults to
    None — unaffected for every call site that doesn't pass one.

    Returns either the finished {role: output} results dict, or, if
    execution pauses at a role in approval_roles,
    {"status": "paused", "paused_at_role": role} — see _run_loop()'s
    docstring for who's responsible for not treating that as a completed
    answer."""
    from eo.dispatcher import next_step

    # Reserve-account worker pools only activate under Expert/Beast mode.
    # `mode=None` stays the default so existing callers that never pass
    # mode keep working — `(mode or "auto")` avoids crashing on a bare
    # .lower() call against None.
    expanded = (mode or "auto").lower() in ("expert", "beast")

    key_overrides = key_overrides or {}

    # role_names may get appended to on escalation (a genuinely new role
    # named via next_destination), and agent_names must grow in lockstep
    # right after — so both need to be local mutable copies, never the
    # caller's original list object.
    role_names = list(role_names) if role_names is not None else list(agent_names)
    agent_names = list(agent_names)
    approval_roles = set(approval_roles) if approval_roles else set()
    no_conversation_context_roles = set(no_conversation_context_roles) if no_conversation_context_roles else set()

    return _run_loop(
        agent_names=agent_names, role_names=role_names, idx=0, results={},
        auto_inserted={}, stage_revisits={}, task_text=task_text,
        session_id=session_id, path=path, mode=mode, key_overrides=key_overrides,
        project_unique_name=project_unique_name, expanded=expanded,
        approval_roles=approval_roles, next_step=next_step,
        no_conversation_context_roles=no_conversation_context_roles,
        domain=domain,
    )


def _run_loop(agent_names, role_names, idx, results, auto_inserted, stage_revisits,
              task_text, session_id, path, mode, key_overrides, project_unique_name,
              expanded, approval_roles, next_step, no_conversation_context_roles=None,
              domain=None) -> dict:
    """The actual step-dispatch loop, factored out of execute_graph() so
    resume_graph() below can re-enter it from a persisted mid-run
    snapshot instead of duplicating every dispatch case, the
    MissingDependencyError self-heal branch, and the escalation-growth
    bookkeeping. execute_graph() calls this once, at idx=0 with empty
    results/auto_inserted/stage_revisits; resume_graph() calls it
    starting from wherever the snapshot left off.

    Every list/dict argument is mutated in place, exactly as the original
    inline loop always did — callers are expected to pass their own local
    copies (execute_graph() already does; resume_graph() rebuilds fresh
    copies from the snapshot before calling in), so nothing leaks across
    sessions.

    no_conversation_context_roles (Part 2 §2.6): defaults to None here
    (normalized to an empty set immediately below) rather than requiring
    every caller to pass one — resume_graph() reconstructs this from its
    snapshot the same way it reconstructs approval_roles.

    domain (Part 2 §2.6, cost-tracking gap): defaults to None, forwarded
    unchanged to every generic_worker dispatch (single-role and
    concurrent-group) and carried into the pause snapshot so a resumed
    run keeps tagging usage under the same domain it started with — same
    carry-through pattern no_conversation_context_roles already uses.

    Pause behavior: checked immediately after a step's normal agent_done
    emission and results[role] write, and BEFORE next_step() is called.
    This means the Dispatcher never sees this step's result at all until
    a human resumes it — no route_trace entry for "what happens after
    this role" gets written, no escalation logic runs, nothing advances.
    On a pause, this function returns
    {"status": "paused", "paused_at_role": role} instead of the results
    dict. Callers up the stack (eo/loop_controller.py's run_with_looping(),
    and anything calling execute_graph() directly) must check for this
    sentinel before treating the return value as finished output."""
    no_conversation_context_roles = no_conversation_context_roles or set()

    while idx is not None and idx < len(agent_names):
        # Migration Part 2 §2.6: a group (role_names[idx] is a list, not
        # a str) is handled entirely separately from the single-role
        # dispatch below — see _run_concurrent_group()'s own docstring
        # for what it does and does not support yet (no approval_roles
        # pausing, no MissingDependencyError self-heal, for any member).
        if isinstance(role_names[idx], list):
            next_idx, reason = _run_concurrent_group(
                role_names[idx], role_names, idx, results, task_text,
                session_id, path, key_overrides, next_step,
                no_conversation_context_roles=no_conversation_context_roles,
                domain=domain,
            )
            _apply_recheck_retry(key_overrides, role_names, next_idx, reason)
            if next_idx is not None and next_idx >= len(agent_names):
                agent_names.append(resolve_role(role_names[next_idx]))
            idx = next_idx
            continue

        current_name = agent_names[idx]
        role = role_names[idx]
        fn = resolve(current_name)
        # key_overrides is always keyed by ROLE name, not resolved
        # agent/module name.
        override = key_overrides.get(role)

        print(f"  [Executor] running: {current_name} (role={role})")
        emit_event("agent_start", session_id=session_id, agent=current_name, path=path,
                    payload={"label": role})
        started = time.monotonic()
        try:
            if current_name == "prompt_writer_lean" and task_text:
                result = fn(task_text, session_id=session_id, path=path, domain=domain)
            elif current_name in TASK_TEXT_ENTRYPOINTS and task_text:
                # (prompt_writer_lean is also in TASK_TEXT_ENTRYPOINTS but
                # never reaches this branch — it's caught by the dedicated
                # `if` above, which already passes both session_id/path.)
                result = fn(task_text, key_override=override, session_id=session_id, path=path,
                            domain=domain)
            elif current_name == "code_writer_lean":
                result = fn(session_id=session_id, path=path, domain=domain)
            elif current_name == "reviewer_fixer_lean":
                result = fn(session_id=session_id, path=path, domain=domain)
            elif current_name == "code_writers":
                # Needs task_text as a fallback seed for its own
                # module_specs synthesis when hired without
                # "prompt_writer" ahead of it in the plan (see
                # agents/code_writers.py's _derive_specs_from_task_text()).
                result = fn(session_id=session_id, path=path, expanded=expanded,
                            key_override=override, task_text=task_text, domain=domain)
            elif current_name == "content_adapter_pool":
                # Part 6 §6.2 — needs task_text as a fallback seed for its
                # own content_targets synthesis when hired without an
                # upstream generic_worker role having written a brief
                # first (see agents/content_adapter_pool.py's
                # _derive_brief_from_task_text()), same reasoning as
                # code_writers' task_text handling just above.
                result = fn(session_id=session_id, path=path, expanded=expanded,
                            key_override=override, task_text=task_text, domain=domain)
            elif current_name in ("reviewer", "security_scanner", "extraction_table_builder"):
                # extraction_table_builder (§3.5): no task_text needed —
                # its real input is KEYS["academic_search_report"], read
                # straight off the bus. If empty, run() raises
                # MissingDependencyError("academic_search") itself (see
                # its own docstring), letting the self-heal branch below
                # splice that step in first on the adaptive path.
                result = fn(session_id=session_id, path=path, expanded=expanded,
                            key_override=override, domain=domain)
            elif current_name == "fixer_pool":
                result = fn(session_id=session_id, path=path, key_override=override, domain=domain)
            elif current_name == "sandbox_tester_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name == "structure_architect":
                # Needs task_text (see UNSCOPED_TIER_AGENTS comment above)
                # — its no-code planning path uses it to plan a
                # folder/file scaffold when there's no code to organize.
                result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path), task_text=task_text,
                            domain=domain)
            elif current_name == "deploy_config_writer":
                # Part 7 §7.4 — same call shape as structure_architect
                # just above: reads real on-disk project state itself
                # (via get_current_app_slug()), task_text is only a minor
                # extra signal, not a hard requirement.
                result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path), task_text=task_text,
                            domain=domain)
            elif current_name in ("architecture_diagrammer", "schema_diagrammer", "handoff_packager"):
                result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path), task_text=task_text, domain=domain)
            elif current_name == "academic_search":
                # Needs task_text as the search query (no other bus key
                # holds it yet — this IS the first data-gathering step)
                # and tier for write_node()'s usage logging.
                result = fn(task_text=task_text, session_id=session_id,
                            tier=PATH_TO_TIER.get(path), domain=domain)
            elif current_name == "dataset_analyst":
                # Needs task_text as the analysis request, same reasoning
                # as academic_search above. No key_override/expanded —
                # single-pass generation + sandbox execution, not a pool.
                result = fn(task_text=task_text, session_id=session_id, path=path, domain=domain)
            elif current_name in ("file_manager", "file_manager_writeback", "file_manager_test_writeback"):
                # Kept as a three-name case rather than a single
                # "file_manager" case — dropping back to one name would
                # silently drop project_unique_name for the two writeback
                # callables.
                result = fn(project_unique_name=project_unique_name)
            elif current_name == "generic_worker":
                # `role` identifies WHICH reasoning-only role this step
                # is. input_keys is "every role earlier than this one in
                # the (possibly runtime-escalated) plan" — role_names[:idx]
                # is a plain slice since role_names is already in resolved
                # execution order.
                #
                # Part 2 §2.6: include_conversation_context is False only
                # for a role explicitly listed in
                # no_conversation_context_roles — every other role keeps
                # today's exact behavior (full conversation-memory
                # prepend). input_keys is unaffected either way, since
                # that's the separate, already-enforced per-stage scoping
                # mechanism this gap sat alongside.
                #
                # domain (Part 2 §2.6, cost-tracking gap): forwarded so
                # utils/llm_client.py's log_usage() can tag this call's
                # usage for the per-project/per-section breakdown.
                result = fn(role=role, task_text=task_text,
                            input_keys=role_names[:idx], session_id=session_id,
                            key_override=override,
                            include_conversation_context=role not in no_conversation_context_roles,
                            domain=domain)
            elif current_name in UNSCOPED_TIER_AGENTS:
                # tier=None if path itself is None/unrecognized — these
                # agents already treat a None tier as "unscoped".
                result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path), domain=domain)
            elif current_name in ("idea_planner", "prompt_writer", "test_writer", "report_writer"):
                # Migration Part 2 §2.6, cost-tracking gap's last piece:
                # these four used to fall through to the bare `else: fn()`
                # branch below and got NOTHING passed to them at all — not
                # even session_id, let alone domain. Each of these four
                # modules' own run()/run_report_writer() already accepted
                # (or, this same Part, now accepts) session_id/domain
                # kwargs that simply had no caller ever supplying them.
                # tier isn't threaded here even though prompt_writer.run()
                # accepts it — none of these four are tier-gated the way
                # UNSCOPED_TIER_AGENTS' four are, so there's no
                # PATH_TO_TIER lookup relevant to pass.
                result = fn(session_id=session_id, domain=domain)
            else:
                result = fn()
        except MissingDependencyError as dep_exc:
            # An agent asked for a specific prerequisite role instead of
            # hard-failing the task. Only attempt to self-heal on the
            # "adaptive" path — that's the only mode where role_names is
            # a Panel-decided vocabulary a new role can be spliced into;
            # on instant/direct/fixed's statically-built graphs this is a
            # real ordering bug, not a staffing gap, so it's re-raised.
            needed_role = dep_exc.required_role
            pair = (role, needed_role)
            already_ran = needed_role in role_names[:idx]
            over_budget = auto_inserted.get(pair, 0) >= MAX_AUTO_INSERTS_PER_STEP
            if path != "adaptive" or already_ran or over_budget:
                emit_event("error", session_id=session_id, agent=current_name, path=path,
                            payload={"message": f"{dep_exc.__class__.__name__}: {dep_exc}"})
                raise
            auto_inserted[pair] = auto_inserted.get(pair, 0) + 1
            print(f"  [Executor] {current_name} (role={role}) requested prerequisite "
                  f"role '{needed_role}' — inserting it and retrying.")
            emit_event("agent_requested_role", session_id=session_id, agent=current_name, path=path,
                        payload={"label": f"{role} needs '{needed_role}' first — adding it to the plan",
                                 "requested_role": needed_role})
            role_names.insert(idx, needed_role)
            agent_names.insert(idx, resolve_role(needed_role))
            continue   # re-enter the loop at the same idx, now pointing at
                       # the newly inserted prerequisite step instead of
                       # the one that raised (which got shifted to idx+1).
        except Exception as exc:
            emit_event("error", session_id=session_id, agent=current_name, path=path,
                        payload={"message": f"{exc.__class__.__name__}: {exc}"})
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        # results is keyed by ROLE, not module name — results["generic_worker"]
        # would otherwise silently overwrite itself across multiple
        # generic_worker hires in the same plan.
        results[role] = result
        print(f"  [Executor] done: {current_name}")
        image = _extract_image(result)
        # Text budget shrinks when an image rides along in the same
        # event, so the two together still fit Pusher's ~10KB cap
        # (image is capped separately at MAX_IMAGE_DATA_URI_CHARS above).
        summary_limit = 9000 - len(image) if image else 9000
        payload = {"summary": _summarize(result, role=role, limit=summary_limit), "duration_ms": duration_ms}
        if image:
            payload["image"] = image
        emit_event("agent_done", session_id=session_id, agent=current_name, path=path, payload=payload)

        # Human-in-the-loop pause point. See this function's own
        # docstring above for exactly what state has and hasn't advanced
        # by this point.
        if role in approval_roles:
            from memory.bus import write, get_current_app_slug
            snapshot = {
                "agent_names": agent_names,
                "role_names": role_names,
                "idx": idx,
                "results": results,
                "key_overrides": key_overrides,
                "auto_inserted": auto_inserted,
                "stage_revisits": stage_revisits,
                "path": path,
                "task_text": task_text,
                "project_unique_name": project_unique_name,
                "mode": mode,
                "approval_roles": list(approval_roles),
                # Part 2 §2.6: carried through so a resumed run keeps
                # applying the same scoped-memory opt-outs to every role
                # still ahead of it in the plan — without this, resuming
                # from a snapshot would silently revert every later role
                # back to include_conversation_context=True.
                "no_conversation_context_roles": list(no_conversation_context_roles),
                # Part 2 §2.6: same carry-through reasoning as
                # no_conversation_context_roles above, so usage logged
                # after a resume still attributes to the same domain the
                # run started with, instead of silently losing that tag.
                "domain": domain,
                # Captured so resume_graph() can restore the exact bus
                # namespace this run was writing under before touching
                # anything else.
                "app_slug": get_current_app_slug(),
            }
            write(f"paused_execution:{session_id}", snapshot)
            return {"status": "paused", "paused_at_role": role}

        next_idx, reason = next_step(
            result if isinstance(result, dict) else {}, role_names, idx, session_id=session_id,
            # Part 2 §2.6: role_names may contain a group (a list) at
            # ANY position now, not just idx — a bare set(role_names)
            # would raise on the unhashable list the moment one exists
            # anywhere in the plan, even while processing an unrelated
            # single-role step.
            known_roles=set(list_known_roles()) | _flatten_role_names(role_names),
        )

        # Part 2 §2.6: a "recheck" (role sent back to itself) retries on
        # a different account than the one that just produced weak
        # output, instead of silently repeating the identical hire.
        _apply_recheck_retry(key_overrides, role_names, next_idx, reason)

        # next_step() may have appended a genuinely new role to role_names
        # (escalation to a role that wasn't in the original plan at all).
        # agent_names must grow in lockstep right here, or agent_names[idx]
        # indexes past its own end next iteration.
        if next_idx is not None and next_idx >= len(agent_names):
            agent_names.append(resolve_role(role_names[next_idx]))

        idx = next_idx

    return results


def resume_graph(session_id: str, decision: dict) -> dict:
    """Reads the paused_execution:{session_id} snapshot that _run_loop()
    left behind and applies one of three human decisions, then re-enters
    _run_loop() so every later role behaves exactly as it would have in
    an un-paused run.

    decision shapes:
      {"action": "approve"}
      {"action": "edit", "text": "..."}     — overwrites the paused
          role's stored result text (both in the in-memory results dict
          this function rebuilds AND in stage_output:{session_id}:{role}
          on the memory bus, so any later generic_worker step reading
          this role's output via input_keys sees the edited version).
      {"action": "reject_redo"}             — re-runs the same role from
          scratch, guarded by MAX_STAGE_REVISITS so a reject loop can't
          run forever.

    Raises KeyError if there's no paused run for this session_id.
    Raises RuntimeError if reject_redo exceeds MAX_STAGE_REVISITS for
    this role. Both are meant to be caught at the API layer and turned
    into 404 / 409 responses respectively.

    Macro-loop continuation: this calls _run_loop() directly, not
    eo/loop_controller.py's run_with_looping() — but if the snapshot
    carries the macro_loop_num/macro_current_order/macro_results/... 
    fields that run_with_looping() writes on pause (see that function's
    docstring), finishing this one _run_loop() pass cleanly does NOT
    return straight to the caller. Instead this function re-enters a
    small variant of run_with_looping()'s own tail from the resumed
    loop_num/current_order onward — merging into macro_results, running
    the gatekeeper, and possibly starting further execute_graph()
    passes — exactly as run_with_looping() would have if the pause had
    never happened. If the resumed pass pauses again (either
    immediately, on a later role in the same pass, or in a later macro
    pass reached via the gatekeeper's CONTINUE), the macro-loop fields
    are re-attached to whatever fresh paused_execution:{session_id}
    snapshot _run_loop() just wrote, so a chain of pauses across
    multiple macro-loop passes never loses state. A snapshot with no
    macro_loop_num (the plain adaptive-pass case, still the common one)
    behaves exactly as before: _run_loop()'s result is returned as-is.
    """
    from memory.bus import read, write, delete, set_app_slug
    from eo.dispatcher import next_step

    snapshot = read(f"paused_execution:{session_id}", default=None)
    if snapshot is None:
        raise KeyError(f"No paused execution found for session_id={session_id!r}")

    # Restore this run's original bus namespace BEFORE touching anything
    # else below (the edit action's stage_output write, and every bus
    # operation _run_loop() does for the rest of the resumed pass).
    set_app_slug(snapshot.get("app_slug"))

    agent_names = list(snapshot["agent_names"])
    role_names = list(snapshot["role_names"])
    idx = snapshot["idx"]
    results = dict(snapshot["results"])
    key_overrides = snapshot["key_overrides"]
    auto_inserted = snapshot["auto_inserted"]
    stage_revisits = snapshot.get("stage_revisits", {})
    path = snapshot["path"]
    task_text = snapshot["task_text"]
    project_unique_name = snapshot["project_unique_name"]
    mode = snapshot["mode"]
    approval_roles = set(snapshot.get("approval_roles") or [])
    # Part 2 §2.6 — see _run_loop()'s snapshot-write comment above.
    no_conversation_context_roles = set(snapshot.get("no_conversation_context_roles") or [])
    domain = snapshot.get("domain")
    expanded = (mode or "auto").lower() in ("expert", "beast")

    # Macro-loop state (eo/loop_controller.py's run_with_looping()) —
    # present only if the pause happened during an expert/beast-mode
    # macro-loop. None for the plain single-pass adaptive case, which
    # is the signal used below to skip macro continuation entirely.
    macro_loop_num = snapshot.get("macro_loop_num")
    macro_current_order = snapshot.get("macro_current_order")
    macro_results = snapshot.get("macro_results")
    macro_mode = snapshot.get("macro_mode")
    macro_hires = snapshot.get("macro_hires")
    macro_execution_order = snapshot.get("macro_execution_order")
    macro_domain = snapshot.get("macro_domain")
    macro_project_unique_name = snapshot.get("macro_project_unique_name")

    role = role_names[idx]
    action = decision.get("action")

    if action == "edit":
        new_text = decision.get("text", "")
        prior = results.get(role)
        if isinstance(prior, dict) and "text" in prior:
            edited = dict(prior)
            edited["text"] = new_text
        else:
            edited = {"text": new_text}
        results[role] = edited
        write(f"stage_output:{session_id}:{role}", edited)
        action = "approve"   # same continuation path once the edit lands

    if action == "approve":
        next_idx, reason = next_step(
            results[role] if isinstance(results[role], dict) else {},
            role_names, idx, session_id=session_id,
            known_roles=set(list_known_roles()) | _flatten_role_names(role_names),
        )
        # Part 2 §2.6: same recheck-retry fix as _run_loop() above — a
        # human approving a paused step can still route into a
        # "recheck" (the dispatcher doesn't distinguish who approved the
        # step it's now reasoning about), so this path needs the same
        # different-account guarantee.
        _apply_recheck_retry(key_overrides, role_names, next_idx, reason)
        if next_idx is not None and next_idx >= len(agent_names):
            agent_names.append(resolve_role(role_names[next_idx]))
        idx = next_idx

    elif action == "reject_redo":
        visits = stage_revisits.get(role, 0)
        if visits >= MAX_STAGE_REVISITS:
            delete(f"paused_execution:{session_id}")
            raise RuntimeError(
                f"role '{role}' hit its reject/redo cap ({MAX_STAGE_REVISITS}) "
                f"for session_id={session_id!r} -- refusing to loop forever."
            )
        stage_revisits[role] = visits + 1
        # idx stays exactly where it was — re-entering _run_loop() below
        # re-executes agent_names[idx]/role_names[idx] from scratch, the
        # same "mutate the plan and continue" idiom MissingDependencyError
        # already uses above for prerequisite auto-insertion.

    else:
        raise ValueError(f"Unknown resume action: {action!r}")

    # Snapshot consumed. A fresh one gets written by _run_loop() below if
    # this run pauses again on a later approval_roles role.
    delete(f"paused_execution:{session_id}")
    emit_event("execution_resumed", session_id=session_id, path=path,
                payload={"label": role, "action": decision.get("action")})

    def _reattach_macro_state(target_loop_num, target_current_order, target_results):
        """Copies the macro-loop fields onto whatever fresh
        paused_execution:{session_id} snapshot _run_loop() (or the
        macro-continuation loop below) just wrote, so a pause that
        happens anywhere downstream of this resume — same pass, later
        role, or a later macro pass — doesn't lose the state needed to
        keep resuming correctly. No-op if nothing wrote a snapshot
        (shouldn't happen alongside a "paused" result, but guards
        against a race rather than raising)."""
        new_snapshot = read(f"paused_execution:{session_id}", default=None)
        if new_snapshot is None:
            return
        new_snapshot["macro_loop_num"] = target_loop_num
        new_snapshot["macro_current_order"] = target_current_order
        new_snapshot["macro_results"] = target_results
        new_snapshot["macro_mode"] = macro_mode
        new_snapshot["macro_hires"] = macro_hires
        new_snapshot["macro_execution_order"] = macro_execution_order
        new_snapshot["macro_domain"] = macro_domain
        new_snapshot["macro_project_unique_name"] = macro_project_unique_name
        write(f"paused_execution:{session_id}", new_snapshot)

    result = _run_loop(
        agent_names=agent_names, role_names=role_names, idx=idx, results=results,
        auto_inserted=auto_inserted, stage_revisits=stage_revisits, task_text=task_text,
        session_id=session_id, path=path, mode=mode, key_overrides=key_overrides,
        project_unique_name=project_unique_name, expanded=expanded,
        approval_roles=approval_roles, next_step=next_step,
        no_conversation_context_roles=no_conversation_context_roles,
        domain=domain,
    )

    if isinstance(result, dict) and result.get("status") == "paused":
        if macro_loop_num is not None:
            _reattach_macro_state(macro_loop_num, macro_current_order, macro_results)
        return result

    # This resumed pass finished cleanly with no further pause. If it
    # wasn't part of a macro-loop, this IS the finished shape callers
    # expect — return it unchanged, same as before this correction.
    if macro_loop_num is None:
        return result

    # It WAS part of an expert/beast-mode macro-loop (Correction 1):
    # don't treat "this one pass finished" as "the whole run finished".
    # Re-enter run_with_looping()'s own tail from here — merge into the
    # results accumulated before this pass, run the gatekeeper, and
    # possibly continue into further execute_graph() passes — the same
    # sequence run_with_looping() itself would run, just resumed from
    # loop_num/current_order instead of starting at loop_num=1.
    from eo.loop_controller import _run_gatekeeper, MAX_MACRO_LOOPS
    from eo.router import build_execution_graph_from_hires

    pass_results = result
    combined_results = dict(macro_results or {})
    combined_results.update(pass_results)
    final_role = list(pass_results.keys())[-1] if pass_results else None
    loop_num = macro_loop_num
    current_order = macro_current_order
    effective_mode = macro_mode or mode

    while True:
        if effective_mode.lower() not in ("expert", "beast") or loop_num >= MAX_MACRO_LOOPS:
            break

        gate_decision = _run_gatekeeper(combined_results, task_text, session_id, loop_num)
        if gate_decision["action"] in ("STOP", "PAUSE_FOR_HUMAN"):
            break
        loop_num += 1
        current_order = gate_decision.get("redo_roles") or macro_execution_order

        next_agent_names, next_role_names, next_key_overrides = build_execution_graph_from_hires(
            macro_hires, current_order)
        pass_results = execute_graph(
            next_agent_names, role_names=next_role_names, task_text=task_text,
            session_id=session_id, path=path, key_overrides=next_key_overrides,
            project_unique_name=macro_project_unique_name, mode=effective_mode,
            approval_roles=approval_roles,
            no_conversation_context_roles=no_conversation_context_roles,
            domain=macro_domain,
        )

        if isinstance(pass_results, dict) and pass_results.get("status") == "paused":
            # A later macro-loop pass paused too — persist macro state
            # exactly like run_with_looping() does on its own first
            # pause, so this can keep resuming across passes.
            _reattach_macro_state(loop_num, current_order, combined_results)
            return pass_results

        combined_results.update(pass_results)
        if pass_results:
            final_role = list(pass_results.keys())[-1]

    return {"results": combined_results, "final_role": final_role}


if __name__ == "__main__":
    from eo.router import build_execution_graph
    graph = build_execution_graph(tier=0)
    print(execute_graph(graph, task_text="What is the capital of France?"))