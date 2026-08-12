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
import contextlib
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import resolve, resolve_role, list_known_roles
from eo.structure import PATH_TO_TIER
from eo.errors import MissingDependencyError
from eo.tracing import get_tracer, TRACING_ENABLED
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

    # Step 7 (parallel-execution work) — observability. Fired exactly
    # once per group, right as it actually begins dispatching (this
    # function is only ever reached once a group has already cleared
    # Step 3's sanitize_parallel_groups() and Step 5's approval_roles
    # backstop in _run_loop() — see that check's own comment). One event
    # per group, not per member, since agent_start/agent_done below
    # already give per-member detail; this is the "a group ran at all"
    # signal for seeing this feature fire on real traffic and evaluating
    # whether the Panel's proposed groups are actually sensible over time.
    emit_event("parallel_group_dispatched", session_id=session_id, path=path,
               payload={"roles": sorted(group_roles), "group_size": len(group_roles),
                        "given_roles": flat_input_keys})

    started_at = {}
    for member_role in group_roles:
        started_at[member_role] = _time.monotonic()
        # NEW — Phase 8 step 8.3: same field as the main sequential loop's
        # agent_start emit (see that call's comment) -- flat_input_keys is
        # already exactly role_names[:idx] flattened, computed above for
        # generic_worker's own input_keys argument, so this is free here
        # too, not a second computation.
        emit_event("agent_start", session_id=session_id, agent=f"generic:{member_role}", path=path,
                    payload={"label": member_role, "given_roles": flat_input_keys})

    # Bug fix (2026-08-12): each group member used to independently call
    # agents/generic_worker.run() with no chain_override at all, which
    # meant every member with no explicit key_overrides entry fell
    # through to that function's own _build_fallback_chain() call --
    # ranking the ENTIRE account pool by the SAME quota snapshot, all at
    # the same instant, with no visibility into what its siblings in
    # THIS SAME group were about to pick. Two (or more) members almost
    # always compute a near-identical ordered candidate list from an
    # identical snapshot, so they'd pile onto the same top-ranked
    # account simultaneously -- tripping that one account's own
    # concurrent-request/queue limit (a burst 429, e.g. "queue_exceeded")
    # well before the real, much larger pool was anywhere near exhausted.
    # Every subsequent chain step suffered the same collision in
    # lockstep, since generate_text()'s own per-step fallback (utils/
    # llm_client.py) only reacts AFTER a 429, by which point the sibling
    # calls had usually already raced onto the exact same next account
    # too. From the outside this looked like "the entire fallback chain
    # exhausted at once" even though most of the pool was untouched.
    #
    # Fix: reserve each member a coordinated chain BEFORE dispatching,
    # the same build_fallback_chain_excluding()-based pattern agents/
    # hardware_speccer.py's _populate_prices() already uses for its own
    # parallel worker pool (see that function's own comment) -- growing
    # one shared `reserved` set as each member is assigned its chain, so
    # no two members in this group ever start on the same account, and
    # each member's own fallback steps skip every OTHER member's
    # reservation too. Members with an explicit key_overrides entry are
    # left untouched (that's the caller's own deliberate single-account
    # pin, per generic_worker.run()'s key_override semantics) but still
    # seed `reserved` up front, so an auto-reserved sibling can't
    # double-book an account a pinned sibling is already using.
    from eo.dynamic_chain import build_fallback_chain_excluding
    from eo.quota_sentinel import get_quota_snapshot

    quota_status = get_quota_snapshot()
    reserved = {v for v in key_overrides.values() if isinstance(v, str) and v} | {
        item for v in key_overrides.values() if isinstance(v, list) for item in v
    }
    chain_overrides = {}
    for member_role in group_roles:
        if key_overrides.get(member_role):
            continue  # explicit pin — generic_worker.run() handles this itself
        member_chain = build_fallback_chain_excluding(member_role, reserved, quota_status=quota_status)
        if not member_chain:
            continue  # whole pool excluded/cooling — fall through to that
                       # member's own uncoordinated _build_fallback_chain()
                       # call rather than dispatching with an empty chain
        chain_overrides[member_role] = member_chain
        # Only reserve this member's OWN starting account, not its whole
        # fallback chain -- reserving every step would starve later
        # members down to length-0 chains for no reason, since a sibling
        # only actually collides with a step it's genuinely about to try
        # at the same moment, i.e. each member's own first choice.
        first_step = member_chain[0]
        reserved.add(first_step.get("key_env") or first_step.get("account_id_env"))

    # D1 patch 3c -- pool.submit() puts each member's generic_worker.run()
    # call on its own OS thread. A raw contextvars.Context can't help
    # here even though Langfuse's OTEL context is contextvars-backed
    # under the hood: Context.run() raises if the SAME context object is
    # entered from more than one thread at once, which is exactly what N
    # members running concurrently would do. opentelemetry.context's
    # Context object is different -- it's an immutable snapshot, safe to
    # attach() from multiple threads at the same time -- so it's captured
    # ONCE here on the submitting thread (before any member is
    # dispatched) and re-attached inside each worker via
    # attach()/detach(), which is what lets _open_member_span() above
    # correctly nest under "whatever's current" on a different thread
    # than the one that opened it.
    import opentelemetry.context as otel_context
    parent_otel_ctx = otel_context.get_current()

    def _dispatch_member(member_role):
        token = otel_context.attach(parent_otel_ctx)
        try:
            with _open_member_span(member_role, group_roles, session_id, task_text, domain) as _member_span:
                result = fn(role=member_role, task_text=task_text,
                            input_keys=flat_input_keys, session_id=session_id,
                            key_override=key_overrides.get(member_role),
                            chain_override=chain_overrides.get(member_role),
                            include_conversation_context=member_role not in no_conversation_context_roles,
                            domain=domain)
                if _member_span is not None:
                    _member_span.update(output=_summarize(result, role=member_role))
                return result
        finally:
            otel_context.detach(token)

    member_results = {}
    with ThreadPoolExecutor(max_workers=len(group_roles)) as pool:
        futures = {
            pool.submit(_dispatch_member, member_role): member_role
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


@contextlib.contextmanager
def _open_session_trace(session_id: str, task_text: str, domain: str, mode: str):
    """D1 patch 3a -- the ONE root trace every role span (patch 3b),
    concurrent-group member span (patch 3c), and resumed pass (patch 3d)
    below nests under for a given session_id. This is what turns 65 flat
    generations into the actual Inspector -> Panel -> parallel-worker
    tree in Langfuse.

    Deterministic, not random: the trace_id is derived from session_id via
    get_tracer().create_trace_id(seed=session_id) -- Langfuse's own
    documented pattern for correlating an external id with a trace id
    (see langfuse.Langfuse.create_trace_id's docstring). Calling this
    again with the same session_id (which is exactly what resume_graph()
    in patch 3d does, from a different call stack/time than the original
    run) always yields the same 32-char trace_id, so a paused-and-resumed
    run reattaches to the SAME trace instead of starting a second one.
    It's also what lets patch 5's frontend link build a deep link
    straight off session_id with no new backend lookup table.

    propagate_attributes(session_id=session_id) is entered alongside the
    span so every child observation created anywhere in the `with` block
    below -- including ones added later in patches 3b/3c/4, and every
    generation utils/llm_client.py already opens via patch 2 -- carries
    session_id too, which is what makes Langfuse's Sessions view (not
    just the Traces view) show the whole run grouped together.

    Always returns a context manager, unconditionally usable via
    `with _open_session_trace(...):` -- never None, never branches the
    caller. When tracing is off or session_id is missing (nothing to
    correlate against on a resume), or if opening the real span raises
    for any reason (network error talking to Langfuse, etc.), this
    yields a plain no-op context instead -- same "a tracing failure never
    takes down the run itself" posture utils/llm_client.py's
    _traced_generation() already established in patch 2. Any exception
    raised by the code INSIDE the `with` block (i.e. a real failure in
    _run_loop()) is never swallowed here -- it's recorded on the span (if
    one is open) and re-raised exactly as raised, same as any other OTEL
    span context manager.
    """
    if not TRACING_ENABLED or not session_id:
        yield
        return

    stack = contextlib.ExitStack()
    try:
        tracer = get_tracer()
        trace_id = tracer.create_trace_id(seed=session_id)
        stack.enter_context(tracer.start_as_current_observation(
            name="execute_graph",
            as_type="span",
            trace_context={"trace_id": trace_id},
            input=task_text,
            metadata={"session_id": session_id, "domain": domain, "mode": mode},
        ))
        from langfuse import propagate_attributes
        stack.enter_context(propagate_attributes(session_id=session_id))
    except Exception as trace_exc:
        stack.close()
        print(f"  [executor] Langfuse session trace failed to open for "
              f"session_id={session_id!r} (non-fatal): {trace_exc}")
        yield
        return

    with stack:
        yield


@contextlib.contextmanager
def _open_role_span(role: str, current_name: str, session_id: str, task_text: str, domain: str):
    """D1 patch 3b -- one child span per sequential step dispatch in
    _run_loop() below, named after the ROLE (not agent_names[idx]'s
    resolved module name) -- role is the label _run_loop() already keys
    `results` by everywhere else, and is what the Inspector -> Panel
    step tree needs to read out of Langfuse. Nests under whichever
    observation is currently open on entry purely via Langfuse's own
    context-var nesting -- 3a's execute_graph trace span for an
    ordinary top-level step, or 3c's per-member span for a step that
    happens to be dispatched from inside a concurrent group -- no
    trace_id/parent_id plumbing needed here at all. The generations
    utils/llm_client.py's patch 2 already wraps inside fn() nest under
    THIS span the exact same way this span nests under 3a's.

    Same no-op-when-tracing-off / never-swallow-the-real-exception
    contract as _open_session_trace() just above, and the same
    ExitStack shape for the same reason: opening the span is the only
    thing guarded by the try/except below. The `yield span` itself
    sits outside that try, so an exception raised by the step body
    running inside the `with` block (a real step failure -- including
    the MissingDependencyError self-heal branch's `continue`, which
    exits this `with` normally, not as an error) propagates untouched
    and is never mistaken for a tracing failure.

    Returns the span object on success so the caller can attach
    output once the step's result is known (see _run_loop()'s
    `_role_span.update(...)` call below), or None when tracing is off
    or failed to open -- callers must guard on that before touching
    the returned value, same as _traced_generation()'s `if traced is
    None` guard in utils/llm_client.py.
    """
    if not TRACING_ENABLED:
        yield None
        return

    stack = contextlib.ExitStack()
    try:
        tracer = get_tracer()
        span = stack.enter_context(tracer.start_as_current_observation(
            name=role,
            as_type="span",
            input=task_text,
            metadata={"agent": current_name, "session_id": session_id, "domain": domain},
        ))
    except Exception as trace_exc:
        stack.close()
        print(f"  [executor] Langfuse role span failed to open for "
              f"role={role!r} (non-fatal): {trace_exc}")
        yield None
        return

    with stack:
        yield span


@contextlib.contextmanager
def _open_member_span(member_role: str, group_roles: list, session_id: str, task_text: str, domain: str):
    """D1 patch 3c -- one child span per concurrent-group member dispatched
    in _run_concurrent_group() below, named after the member's OWN role
    (every member in a group resolves to the same "generic_worker"
    module, so the role name is the only thing that disambiguates them —
    same reasoning as _open_role_span()'s docstring above for the
    sequential case). These come out as SIBLINGS under whatever span was
    active when the group was dispatched (3a's session trace for an
    ordinary top-level group) — this is the actual "parallel-worker"
    branching in the tree, structurally different from 3b's chain of
    single spans.

    Threading wrinkle this function's caller has to work around (the
    reason 3c isn't just "_open_role_span() called from a different call
    site"): each member's generic_worker.run() call happens on its own
    ThreadPoolExecutor worker thread, and neither OTEL context (which
    Langfuse v4 is built on) nor plain contextvars cross a thread
    boundary on their own — a worker thread starts with a blank context.
    A span opened here with no help would come out as a brand-new
    unparented root trace per member instead of a sibling. See
    _run_concurrent_group()'s otel_context.attach()/detach() around this
    context manager's own `with` block for the fix; this function itself
    doesn't need to know about that — by the time it runs,
    start_as_current_observation() just nests under "whatever's current"
    the same way 3b's does.

    Same no-op-when-tracing-off / never-swallow-the-real-exception
    contract, and the same ExitStack shape, as _open_role_span() and
    _open_session_trace() above. Returns the span object on success (so
    the caller can attach output once the member's result is known) or
    None when tracing is off or failed to open.
    """
    if not TRACING_ENABLED:
        yield None
        return

    stack = contextlib.ExitStack()
    try:
        tracer = get_tracer()
        span = stack.enter_context(tracer.start_as_current_observation(
            name=member_role,
            as_type="span",
            input=task_text,
            metadata={"agent": f"generic:{member_role}", "session_id": session_id,
                      "domain": domain, "group": sorted(group_roles)},
        ))
    except Exception as trace_exc:
        stack.close()
        print(f"  [executor] Langfuse member span failed to open for "
              f"role={member_role!r} (non-fatal): {trace_exc}")
        yield None
        return

    with stack:
        yield span


def execute_graph(agent_names: list, role_names: list = None, task_text: str = None,
                   cycle_num: int = None, session_id: str = None, path: str = None,
                   mode: str = None, key_overrides: dict = None,
                   project_unique_name: str = None, approval_roles: set = None,
                   no_conversation_context_roles: set = None, domain: str = None,
                   scope: str = None, workspace_id: str = None) -> dict:
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

    `scope` (task 13d/13e — Sources sub-tab's scope selector): the ONE
    caller-controlled value this pipeline forwards to web_researcher.run()
    ("general"/"forum"/"news"/"hackernews"). Threaded the same depth as
    domain above (all the way from api/routes/tasks.py's TaskRequest,
    through run_task()/_run_tier3_hires()/run_with_looping(), down to
    here), rather than inferred from task_text phrasing -- the UI already
    knows the scope with certainty once the person picks it, so there's
    nothing to re-derive downstream. Ignored by every role except
    web_researcher; defaults to None, which _run_loop()'s own
    web_researcher branch below then substitutes "general" for, matching
    agents/web_researcher.py's own run() default.

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

    with _open_session_trace(session_id, task_text, domain, mode):
        return _run_loop(
            agent_names=agent_names, role_names=role_names, idx=0, results={},
            auto_inserted={}, stage_revisits={}, task_text=task_text,
            session_id=session_id, path=path, mode=mode, key_overrides=key_overrides,
            project_unique_name=project_unique_name, expanded=expanded,
            approval_roles=approval_roles, next_step=next_step,
            no_conversation_context_roles=no_conversation_context_roles,
            domain=domain, scope=scope, workspace_id=workspace_id,
        )


def _run_loop(agent_names, role_names, idx, results, auto_inserted, stage_revisits,
              task_text, session_id, path, mode, key_overrides, project_unique_name,
              expanded, approval_roles, next_step, no_conversation_context_roles=None,
              domain=None, scope=None, workspace_id=None) -> dict:
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
    sentinel before treating the return value as finished output.

    Group/approval_roles backstop (Step 5, parallel-execution work): a
    concurrent group (role_names[idx] is a list) is never dispatched if
    any of its members is in approval_roles — it's degraded to plain
    sequential slots in place instead, with a logged warning, so the
    single-role branch's existing pause handling covers every member.
    This should never actually fire in practice (Steps 2/3 upstream
    already filter these out); it's a defensive backstop, not the
    primary enforcement point."""
    no_conversation_context_roles = no_conversation_context_roles or set()

    while idx is not None and idx < len(agent_names):
        # Migration Part 2 §2.6: a group (role_names[idx] is a list, not
        # a str) is handled entirely separately from the single-role
        # dispatch below — see _run_concurrent_group()'s own docstring
        # for what it does and does not support yet (no approval_roles
        # pausing, no MissingDependencyError self-heal, for any member).
        if isinstance(role_names[idx], list):
            group_roles = role_names[idx]

            # Step 5 (parallel-execution work) — belt-and-braces backstop.
            # eo/panel.py's _merge_parallel_groups() (Step 2) and
            # eo/router.py's sanitize_parallel_groups() (Step 3) are BOTH
            # already supposed to keep an approval_roles member out of any
            # group before it ever reaches here — see
            # _run_concurrent_group()'s own docstring for why a group
            # can't support the pause checkpoint (no snapshot shape for
            # "idx currently covers N roles running together, not one").
            # This is the last line of defense if either of them has a
            # bug: NEVER actually dispatch a group containing one.
            # Instead, splice this one group-slot back into N ordinary
            # sequential slots, in place, and fall through to the normal
            # single-role dispatch branch below — which already handles
            # approval_roles pausing correctly — for every member,
            # including the unsafe one, one at a time.
            unsafe_members = approval_roles.intersection(group_roles)
            if unsafe_members:
                print(f"  [Executor] WARNING: group at position {idx} contains "
                      f"approval_roles member(s) {sorted(unsafe_members)} — "
                      f"concurrent groups don't support the pause checkpoint. "
                      f"Degrading to sequential execution: {group_roles}")
                agent_names[idx:idx + 1] = [resolve_role(r) for r in group_roles]
                role_names[idx:idx + 1] = list(group_roles)
                continue

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
        # NEW — Phase 8 step 8.3 ("what it was given"). eo/executor.py has
        # no notion of source/secondary-data scope the way a Notebooks
        # generate call does (see api/server.py's NOTEBOOKS_GENERATE_TARGETS
        # for that, a completely separate, step-less code path this panel
        # never renders) -- this pipeline's real, honest equivalent of
        # "what did this step have to work with" is which earlier roles'
        # results were already on the memory bus for it to read.
        # role_names[:idx] IS exactly that set, for every dispatch branch
        # below (generic_worker actually passes it as input_keys; every
        # other branch's underlying agent reads memory.bus directly
        # instead of taking it as an argument, but the set of what's
        # THERE for it to read is identical either way -- this reports
        # availability, not a per-branch argument). _flatten_role_names
        # handles role_names containing concurrent-group sublists the
        # same way every other consumer of role_names[:idx] already does
        # (see _run_concurrent_group's own flat_input_keys just above).
        given_roles = list(_flatten_role_names(role_names[:idx]))
        emit_event("agent_start", session_id=session_id, agent=current_name, path=path,
                    payload={"label": role, "given_roles": given_roles})
        started = time.monotonic()
        # D1 patch 3b -- one child span per step, named after `role`, so
        # this step's own generation(s) (already wrapped by utils/llm_client.py's
        # patch 2) nest under a labeled node instead of showing up as a
        # flat, anonymous sibling of every other role's generations. See
        # _open_role_span()'s own docstring above for the nesting and
        # no-op contract.
        with _open_role_span(role, current_name, session_id, task_text, domain) as _role_span:
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
                elif current_name == "hardware_speccer":
                    # MiniMe Blueprint fix — same call shape as
                    # architecture_diagrammer/schema_diagrammer just above, plus
                    # workspace_id: run_hardware_speccer() hard-requires it (raises
                    # ValueError without one) since the spec is written into that
                    # workspace's workspace_facts.custom, not a bus key like its
                    # sibling diagrammers. workspace_id is threaded here all the
                    # way from api/task_runner.py's _run_task_inner(), same depth
                    # scope/domain already are.
                    result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path), task_text=task_text,
                                domain=domain, workspace_id=workspace_id)
                elif current_name == "academic_search":
                    # Needs task_text as the search query (no other bus key
                    # holds it yet — this IS the first data-gathering step)
                    # and tier for write_node()'s usage logging.
                    result = fn(task_text=task_text, session_id=session_id,
                                tier=PATH_TO_TIER.get(path), domain=domain)
                elif current_name == "web_researcher":
                    # Same call shape as academic_search directly above --
                    # task_text as the search query, tier for write_node()'s
                    # usage logging. `scope` comes from _run_loop()'s own
                    # param (see execute_graph()'s docstring for the full
                    # path it's threaded through, all the way from
                    # api/routes/tasks.py's TaskRequest.scope) -- explicit
                    # end-to-end plumbing rather than parsed out of
                    # task_text, so a UI scope selector is authoritative
                    # instead of best-effort. `or "general"` only matters
                    # for callers that never pass one (e.g. the CLI
                    # __main__ smoke test below) -- web_researcher.run()'s
                    # own default is "general" too, this just makes that
                    # explicit at the call site rather than relying on
                    # run()'s signature to quietly supply it.
                    result = fn(task_text=task_text, session_id=session_id,
                                tier=PATH_TO_TIER.get(path), scope=scope or "general")
                elif current_name == "dataset_analyst":
                    # Needs task_text as the analysis request, same reasoning
                    # as academic_search above. No key_override/expanded —
                    # single-pass generation + sandbox execution, not a pool.
                    result = fn(task_text=task_text, session_id=session_id, path=path, domain=domain)
                elif current_name == "performance_reviewer":
                    # Patch 8 (rollout guide §3) — no task_text needed, unlike
                    # dataset_analyst/academic_search just above: this role's
                    # real input is fixed_code/submitted_code + test_results,
                    # read straight off the bus (same convention
                    # sandbox_tester.py's own run_sandbox_tester() uses), not
                    # anything seeded from the task description itself. No
                    # key_override/expanded either — single-pass generation +
                    # sandbox execution on ONE selected module, not a worker
                    # pool.
                    result = fn(session_id=session_id, path=path, domain=domain)
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
                                input_keys=list(_flatten_role_names(role_names[:idx])), session_id=session_id,
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
                already_ran = needed_role in _flatten_role_names(role_names[:idx])
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
            if _role_span is not None:
                try:
                    _role_span.update(output=_summarize(result, role=role))
                except Exception as trace_exc:
                    print(f"  [executor] Langfuse role span failed to update "
                          f"output for role={role!r} (non-fatal): {trace_exc}")
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
        #
        # CO3: widened from the original approval_roles-only check to
        # also fire on an on-demand pause_requested:{session_id} flag —
        # someone hitting "Pause" mid-run, rather than a role pre-listed
        # at task start. Same checkpoint, same snapshot shape, same
        # return value either way, so resume_graph() below needs no
        # changes at all to handle either trigger.
        from memory.bus import read as bus_read, delete as bus_delete
        pause_requested = bus_read(f"pause_requested:{session_id}", default=False) if session_id else False
        if role in approval_roles or pause_requested:
            from memory.bus import write, get_current_app_slug
            if pause_requested:
                # Consume the flag now, same lifecycle as the snapshot
                # itself: a one-shot trigger, not a sticky state that
                # would otherwise re-pause every subsequent role once a
                # resumed run reaches this checkpoint again.
                bus_delete(f"pause_requested:{session_id}")
            # Bug fix, found while wiring CO3: this event type existed on
            # the frontend (WorkspaceDockContext.jsx's eventType ===
            # "awaiting_approval" handler) but was never in
            # VALID_EVENT_TYPES and never emitted here — so the "resume
            # affordance already wired" step list never actually lit up
            # live for EITHER trigger, approval_roles or on-demand. This
            # call is what completes it. `reason` lets the frontend show
            # different copy for "this role needs sign-off" vs "someone
            # hit pause" without needing a second event type.
            emit_event(
                "awaiting_approval", session_id=session_id, agent=current_name, path=path,
                payload={"role": role, "reason": "approval" if role in approval_roles else "manual_pause"},
            )
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
                # Task 13d/13e: same carry-through reasoning as domain
                # directly above, so a resumed run's web_researcher step
                # (if that's what it pauses/resumes at) still uses the
                # scope the original dispatch was given, instead of
                # silently falling back to "general" on resume.
                "scope": scope,
                # MiniMe Blueprint fix — same carry-through reasoning as
                # scope/domain above, so a resumed run's hardware_speccer
                # step (if that's what it pauses/resumes at) still has the
                # workspace_id it hard-requires, instead of raising
                # ValueError on resume.
                "workspace_id": workspace_id,
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
    scope = snapshot.get("scope")
    workspace_id = snapshot.get("workspace_id")
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
    macro_scope = snapshot.get("macro_scope")
    macro_workspace_id = snapshot.get("macro_workspace_id")
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
        new_snapshot["macro_scope"] = macro_scope
        new_snapshot["macro_workspace_id"] = macro_workspace_id
        new_snapshot["macro_project_unique_name"] = macro_project_unique_name
        write(f"paused_execution:{session_id}", new_snapshot)

    # D1 patch 3d — execute_graph() (line 660) always wraps _run_loop() in
    # _open_session_trace(), which is what makes patch 3b's role spans
    # and patch 3c's member spans nest under the session's trace instead
    # of each opening their own orphan trace via start_as_current_observation.
    # This function calls _run_loop() directly, on a fresh call
    # stack/time from the original execute_graph() call, so there is no
    # "current" Langfuse observation for those child spans to nest under
    # unless we open one here too. _open_session_trace() derives its
    # trace_id deterministically from session_id (tracer.create_trace_id
    # (seed=session_id)), so calling it again here with the same
    # session_id reattaches to the exact same trace rather than minting a
    # second one — a paused-and-resumed run shows up in Langfuse as one
    # continuous tree with a gap in it, not two separate traces.
    with _open_session_trace(session_id, task_text, domain, mode):
        result = _run_loop(
            agent_names=agent_names, role_names=role_names, idx=idx, results=results,
            auto_inserted=auto_inserted, stage_revisits=stage_revisits, task_text=task_text,
            session_id=session_id, path=path, mode=mode, key_overrides=key_overrides,
            project_unique_name=project_unique_name, expanded=expanded,
            approval_roles=approval_roles, next_step=next_step,
            no_conversation_context_roles=no_conversation_context_roles,
            domain=domain, scope=scope, workspace_id=workspace_id,
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
            domain=macro_domain, scope=macro_scope, workspace_id=macro_workspace_id,
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