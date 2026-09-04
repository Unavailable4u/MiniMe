"""
api/task_runner.py

A programmatic sibling to eo/loop_v4.py's CLI dispatch. Reuses the same
underlying decision logic (imported straight from loop_v4, not
reimplemented) but returns structured dicts instead of printing to
stdout, so api/server.py can turn a task into a JSON response.

Tiers 0, 1, 2 run their full path here. Tier 3 has two branches: a
hires-driven path (_run_tier3_hires(), below) when the Panel actually
staffed the task, and a "not_wired_yet" placeholder for the no-hires
case — loop_v4.py's CLI-only tier-3 path blocks on input() for the
cost-ceiling confirmation and hands off to loop.py, a long-running
process that a single HTTP request/response cycle can't represent.

Deliberately does NOT modify loop_v4.py. It imports loop_v4's
underscore-prefixed helpers directly (_get_decision, in particular)
rather than duplicating the Inspector/panel/override logic — that logic
must never drift between the CLI and the API.

The same Starter General Agents pre-filter (eo/sga.py) that loop_v4.py's
CLI runs is used here too, so a task submitted through the API gets the
identical SGA-first treatment instead of always going straight to the
Inspector.

project_unique_name threads through run_task() -> _run_tier2() ->
execute_graph() -> file_manager.py, so a tier-2 task can target a
registered external project (eo/project_registry.py) instead of this
system's own apps/ directory. Only tier 2 touches disk, so tiers 0/1
don't need this parameter.

run_task() is a thin wrapper around _run_task_inner(): it records the
incoming task_text as a "user" turn and the resolved response as an
"assistant" turn in this session's shared conversation transcript
(eo/conversation_memory.py), before returning that response unchanged.
Doing this as a wrapper means every one of _run_task_inner()'s early-
return points (cache hit, SGA resolved, needs_directed_task_type,
needs_app, needs_beast_mode_*, tier 0/1/2/3, paused, not_wired_yet,
unknown-tier error) gets turn-recording for free, without touching any
of them individually.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.source_manager import (
    process_upload,  # NEW — Data Layer §4a: the deterministic
)
from eo import (
    chat_workspace,
    code_loader,
    conversation_memory,
    fact_summarizer,  # NEW — Part 3, extended by Patch B2
    loop_v4,
    routing_memory,
    user_profile,  # NEW — Patch B2, profile_signals write-side
    workspace_facts,
)
from eo.executor import execute_graph
from eo.knowledge_graph import (  # NEW — bug #4 fix, notebook grounding;
    get_node,
    search_nodes,
)
from eo.loop_controller import run_with_looping
from eo.modes import apply_mode
from eo.note_candidates import (
    get_topic_related_notes,  # NEW — Step 6.11.f (6.11.e's helper)
)
from eo.output_guard import check_content_safety  # NEW — Patch 13
from eo.output_guard import validate_final_answer  # NEW — D3 Part 3
from eo.panel import staff_task
from eo.panel_content import (
    write_panel_from_role,  # NEW — chat-to-panel writes, patch 2
)

# get_node NEW — Step 6.11.f, resolving a topic's covered source ids into content
from eo.prerequisite_suggestions import (
    find_prerequisite_suggestions,  # NEW — Data Layer §9d
)
from eo.registry import update_role_prompt  # NEW — Part 2 §2.5
from eo.router import (
    EXPLAIN_CODE_ROUTE,
    build_execution_graph,
    build_execution_graph_from_hires,
    sanitize_parallel_groups,
)
from eo.semantic_cache import (
    CACHE_CLASS_DETERMINISTIC,
    CACHE_CLASS_GENERATIVE,
    check_cache,
    classify_cache_class,
    get_cached_reference,
    write_cache,
)
from eo.sga import attempt as sga_attempt
from eo.source_index import (
    get_topic_covered_sources,  # NEW — Step 6.11.f (6.11.d's helper)
)

# "attachment present" short-circuit below hires Source Manager directly,
# same entry point every upload endpoint already funnels through (§2b).
from eo.structure import (  # NEW — Part 2 §2.3/§2.6; record_template_run NEW — recent templates
    classification_from_template,
    get_workflow_template,
    record_template_run,
)
from eo.workspace_code_files import (
    write_files as write_code_files_batch,  # perf audit follow-up (registry.py N+1, part 3):
    # replaces the old per-file write_file() loop below with one
    # multi-row upsert -- see this function's own docstring and
    # write_files()'s in eo/workspace_code_files.py.
)
from relay.emitter import (  # NEW — live-refetch fix, patch 3 follow-up
    EventType,
    emit_workspace_event,
)

# NEW — bug #4 fix: chat inside a notebook never pulled the notebook's
# ingested sources into the prompt (task_text had a workspace_id for
# bookkeeping only — routing facts, cache keys, fact extraction — but the
# model answering the question never saw the actual source content, which
# is why it would say things like "I don't have access to files"). Same
# per-source truncation budget agents/mind_mapper.py's _context_for() uses,
# scaled down since this runs on every chat turn rather than a one-shot
# generation and only pulls the top_k most relevant sources, not the whole
# corpus.
MAX_NOTEBOOK_CONTEXT_CHARS_PER_SOURCE = 4000
NOTEBOOK_CONTEXT_TOP_K = 6


def _topic_scoped_task_text(workspace_id: str, topic_id: str, task_text: str,
                             session_id: str = None) -> tuple:
    """Step 6.11.f: deterministic, exact-topic grounding for a "Work
    through: <step title>" chat turn — mirrors `attachment`'s posture
    above in that a topic_id's mere presence is the signal, not
    something guessed at. A workflow step click is scoped to one known
    topic; there's no reason to make _grounded_task_text()'s similarity
    search guess that back out of a synthetic "Let's work through:
    <step title>" message, whose wording rarely echoes the source
    material anyway.

    Pulls the topic's covered sources (6.11.d's
    get_topic_covered_sources(), zero LLM calls) and any notes already
    tied to it (6.11.e's get_topic_related_notes()). Deliberately does
    NOT call agents/topic_note_writer.py — that role drafts a new,
    persisted note (an explicit "write me a note" ask), a different job
    from grounding a live turn's answer in material that already
    exists. Per the 6.11.g decision: read-only, no note_taker/
    extraction_table_builder/topic_note_writer call of its own.

    Returns (None, None) when there's nothing usable to build from
    (no workspace_id/topic_id, a lookup failure, or a real topic_id
    with zero covered sources and zero related notes) so the caller
    falls back to _grounded_task_text()'s generic search-based
    grounding — same fail-open posture that function already takes for
    a Vector hiccup. Otherwise returns (grounded_text, source_node_ids),
    matching _grounded_task_text()'s own return shape.
    """
    if not workspace_id or not topic_id:
        return None, None

    try:
        source_ids = get_topic_covered_sources(workspace_id, topic_id, session_id=session_id)
    except Exception as exc:
        print(f"  [task_runner] topic-scoped source lookup failed for topic_id={topic_id!r}, "
              f"falling back to search grounding (fail-open): {exc}")
        return None, None

    source_parts = []
    for node_id in source_ids:
        node = get_node(workspace_id, node_id)
        if not node:
            continue
        title = node.get("title") or node_id
        content = (node.get("content") or "").strip()[:MAX_NOTEBOOK_CONTEXT_CHARS_PER_SOURCE]
        if content:
            source_parts.append(f"--- {title} ---\n{content}")

    try:
        related_notes = get_topic_related_notes(workspace_id, topic_id, session_id=session_id)
    except Exception as exc:
        print(f"  [task_runner] topic-scoped note lookup failed for topic_id={topic_id!r}, "
              f"continuing with sources only (fail-open): {exc}")
        related_notes = []

    note_parts = []
    for n in related_notes:
        title = n.get("title") or n.get("node_id")
        content = (n.get("content") or "").strip()
        if content:
            note_parts.append(f"--- {title} ---\n{content}")

    if not source_parts and not note_parts:
        # A real topic_id with genuinely nothing behind it yet (no
        # ingested sources, no notes) — not an error, just nothing to
        # splice in. Caller falls back to generic grounding, which will
        # likely also come up empty, and the model answers from the
        # step title alone rather than claiming access it doesn't have.
        return None, source_ids

    sections = []
    if source_parts:
        sections.append("Source excerpts for this topic:\n\n" + "\n\n".join(source_parts))
    if note_parts:
        sections.append("Existing notes on this topic:\n\n" + "\n\n".join(note_parts))
    context = "\n\n".join(sections)

    grounded_text = (
        "This chat turn is scoped to one notebook topic. Use the "
        "topic-specific material below to answer — it's more precise "
        "for this turn than the notebook's general contents.\n\n"
        f"{context}\n\n---\n\n{task_text}"
    )
    return grounded_text, source_ids


def _grounded_task_text(workspace_id: str, task_text: str, session_id: str = None,
                         topic_id: str = None) -> tuple:
    """Retrieval-scoped notebook grounding for chat. Returns task_text
    unchanged when there's no workspace, no matching sources, or the
    search itself fails — fail-open, same posture eo/knowledge_graph.py's
    own functions already take, so a Vector hiccup degrades to "chat
    behaves like it used to" rather than breaking the turn.

    CHANGED — Data Layer §9d: now returns (grounded_text, node_ids)
    instead of just grounded_text. node_ids is the exact set of Primary
    Source node ids search_nodes() judged relevant to this turn — the
    same set _resolve_decision_and_hires() below now threads through to
    eo/prerequisite_suggestions.py's find_prerequisite_suggestions(), so
    a chat turn's proactive-suggestion pass looks at what THIS turn
    actually grounded on rather than issuing a second, separate
    retrieval call. Always a list (possibly empty), never None, on
    every return path — callers don't need to guard for that.

    topic_id: NEW — Step 6.11.f. When set, tries
    _topic_scoped_task_text()'s deterministic exact-topic path first.
    Falls through to this function's own similarity search when that
    returns nothing (bad topic_id, or a real topic with no sources/
    notes yet) — same fail-open posture as everything else here.
    """
    if topic_id:
        topic_grounded_text, topic_node_ids = _topic_scoped_task_text(
            workspace_id, topic_id, task_text, session_id=session_id)
        if topic_grounded_text:
            return topic_grounded_text, topic_node_ids or []

    if not workspace_id:
        return task_text, []
    try:
        nodes = search_nodes(workspace_id, query_text=task_text,
                              top_k=NOTEBOOK_CONTEXT_TOP_K, session_id=session_id)
    except Exception as exc:
        print(f"  [task_runner] notebook grounding search failed, skipped (fail-open): {exc}")
        return task_text, []
    if not nodes:
        return task_text, []

    node_ids = [n.get("node_id") for n in nodes if n.get("node_id")]

    parts = []
    for n in nodes:
        title = n.get("title") or n.get("node_id")
        content = (n.get("content") or "").strip()[:MAX_NOTEBOOK_CONTEXT_CHARS_PER_SOURCE]
        if not content:
            continue
        parts.append(f"--- {title} ---\n{content}")
    if not parts:
        return task_text, node_ids

    context = "\n\n".join(parts)
    grounded_text = (
        "You have access to the following excerpts from this notebook's sources. "
        "Use them to answer the user's message when relevant. These excerpts ARE "
        "the notebook's content — don't claim you can't access files or external "
        "content when the answer is right here.\n\n"
        f"{context}\n\n---\n\n{task_text}"
    )
    return grounded_text, node_ids


def _run_tier0(task_text: str, decision: dict, session_id: str, owner_id: str = None) -> dict:
    graph = build_execution_graph(tier=0)
    results = execute_graph(graph, task_text=task_text, session_id=session_id, path="instant",
                             owner_id=owner_id)   # NEW — Patch B5
    answer = results["responder"]
    routing_memory.log_outcome(task_text, decision, outcome="tier-0 responder answered directly")
    return {
        "decision": decision,
        "tier": 0,
        "session_id": session_id,
        "status": "ok",
        "result": {"answer": answer},
        "message": None,
    }


def _run_tier1(task_text: str, decision: dict, run_tests: bool, session_id: str, owner_id: str = None) -> dict:
    graph = build_execution_graph(tier=1, run_tests=run_tests)
    # owner_id (Patch B5): forwarded for signature consistency with
    # _run_tier0/_run_tier3_hires, but a no-op here in practice — tier 1's
    # lean pipeline (prompt_writer_lean/code_writer_lean/reviewer_fixer_lean)
    # never dispatches through generic_worker.run()/responder.run(), the
    # only two readers of eo/user_profile.py's default_format_hint().
    results = execute_graph(graph, task_text=task_text, session_id=session_id, path="direct",
                             owner_id=owner_id)
    fixed = results["reviewer_fixer_lean"]

    test_results = None
    if run_tests and "sandbox_tester_lean" in results:
        raw = results["sandbox_tester_lean"]
        test_results = {
            name: ("passed" if r.get("passed") else "failed")
            for name, r in raw.items()
        }

    outcome = "tier-1 lean pipeline completed" + (" and tested" if run_tests else "")
    routing_memory.log_outcome(task_text, decision, outcome=outcome)
    return {
        "decision": decision,
        "tier": 1,
        "session_id": session_id,
        "status": "ok",
        "result": {
            "module_name": fixed.get("name"),
            "code": fixed.get("code"),
            "issues_found": fixed.get("issues_found") or [],
            "test_results": test_results,
        },
        "message": None,
    }


def _run_tier3_hires(task_text: str, decision: dict, session_id: str, hires: list,
                      project_unique_name: str = None, mode: str = "auto",
                      approval_roles: set = None,
                      no_conversation_context_roles: set = None,
                      app_slug: str = None, scope: str = None, workspace_id: str = None,
                      owner_id: str = None, tab: str = None) -> dict:
    """
    Routes through eo/loop_controller.py's run_with_looping() rather than
    calling execute_graph() directly, so the adaptive-looping machinery
    (macro-loop gatekeeper, hard safety caps) fires here too. This path
    has a real session_id to pass through (the CLI path doesn't).

    Not a cost-ceiling-gated or loop.py-unified path; when hires is
    empty, run_task() falls through to the not_wired_yet response
    instead of calling this at all.

    "output" below is the full role-keyed results dict rather than just
    the final role's output — run_with_looping() doesn't expose a single
    "last agent" (the execution order can change between macro-loop
    passes), so that's a necessary shape, not a stylistic one.

    approval_roles: passed straight through to run_with_looping() ->
    execute_graph(). When execution pauses at one of these roles, this
    function returns a distinct "status": "paused" response instead of
    "status": "ok" — the frontend (AgentStepList.jsx /
    RoutingTraceGraph.jsx) renders that as a paused run and offers
    Approve / Edit & Continue / Reject & Redo, which POST to
    /api/resume rather than expecting a finished answer here.

    no_conversation_context_roles: Part 2 §2.6 — passed straight through
    to run_with_looping() -> execute_graph() the same way approval_roles
    is, on every macro-loop pass. None/empty means every role sees the
    full conversation-memory transcript, today's exact default.

    scope: task 13d/13e — the Sources sub-tab's scope selector value
    ("general"/"forum"/"news"/"hackernews"), passed straight through to
    run_with_looping() -> execute_graph(), same treatment as domain
    below. Only web_researcher reads it; every other role in `hires`
    ignores it. None (no scope selector used, or a non-research task)
    matches today's behavior exactly.

    parallel_groups (Step 4 of the parallel-execution work): decision may
    now also carry a Panel-synthesized "parallel_groups" list (see
    eo/panel.py's _merge_parallel_groups(), eo/inspector.py Step 1 for
    where it first enters a vote's schema). It never reaches
    run_with_looping() directly — eo/router.py's sanitize_parallel_groups()
    (Step 3) is the hard gatekeeper that turns it, plus the flat
    execution_order, approval_roles, and the actual hires list, into the
    nested-list execution_order shape build_execution_graph_from_hires()
    already understands (Part 2 §2.6). This is the only production call
    site that changes for this work; every other caller of
    run_with_looping() is untouched.

    owner_id (Patch B5 — Output-Format Routing): the same session_id/
    owner_id pair this function's own docstring already mentions
    _write_plan_panels()/_write_code_files() using — forwarded straight
    through to run_with_looping() -> execute_graph() so generic_worker
    steps in this tier-3 roster can look up a stored output-format
    preference. None (default) is a no-op, matching every other optional
    param here.

    tab (Patch B6 — tool-call budget): same "not branched on here, just
    forwarded" treatment as owner_id above — forwarded straight through
    to run_with_looping() -> execute_graph() -> eo/executor.py's
    _run_loop(), the one place that actually reads it (to gate the
    tool-call-budget pause to the chat tab). None (default) is a no-op.
    """
    from memory.bus import set_app_slug, slugify
    # Scopes every bus key this run touches (module_specs, current_plan,
    # submitted_code, test_code, fixed_code, file_plan, file_map, ...) to
    # this session, instead of the shared process-wide app_slug global.
    # Without this, an unrelated earlier task's leftover state (or a
    # concurrent one) gets silently read by this run. session_id is
    # always a real value here (run_task() generates one if the caller
    # didn't pass one), so this is safe unconditionally. Folding in a
    # slug of the task text keeps the eventual apps/<slug>/ disk folder
    # human-readable instead of a bare opaque UUID.
    set_app_slug(app_slug or f"{slugify(task_text)}_{session_id[:8]}")

    # Step 4: fold any Panel-agreed parallel_groups into execution_order's
    # existing nested-list shape, through the hard sanitizer — never pass
    # decision.get("parallel_groups") to anything downstream unsanitized.
    # hires is the actual staffed roster (not suggested_agents), so a
    # group proposed around a role that never got an available account
    # is dropped here rather than reaching run_with_looping() at all.
    sanitized_execution_order = sanitize_parallel_groups(
        decision.get("parallel_groups") or [],
        decision.get("execution_order") or [],
        approval_roles or set(),
        hires,
    )

    looped = run_with_looping(
        hires, sanitized_execution_order, task_text, session_id=session_id,
        mode=mode, domain=decision.get("domain"), project_unique_name=project_unique_name,
        path="adaptive",
        approval_roles=approval_roles,
        no_conversation_context_roles=no_conversation_context_roles,
        scope=scope,
        workspace_id=workspace_id,
        owner_id=owner_id,
        tab=tab,
    )

    # run_with_looping() returns a paused sentinel instead of
    # {"results": ..., "final_role": ...} when execute_graph() hit an
    # approval_roles role. Must be checked before reaching for either of
    # those keys below — neither exists on the paused shape.
    if looped.get("status") == "paused":
        routing_memory.log_outcome(
            task_text, decision,
            outcome=f"tier-3 hires-driven pipeline paused at '{looped['paused_at_role']}' for approval",
        )
        return {
            "decision": decision,
            "tier": 3,
            "session_id": session_id,
            "status": "paused",
            "result": {"paused_at_role": looped["paused_at_role"]},
            "message": (f"Run paused for approval at role '{looped['paused_at_role']}'. "
                        "POST to /api/resume with this session_id to continue."),
        }

    results = looped["results"]
    final_role = looped["final_role"]

    # NEW — Phase 6d: surface Phase 6b/6c's failure-marker dicts to the
    # API response instead of leaving them silently buried inside
    # `results`. Only tier-3's hires-driven adaptive path ever produces
    # this shape — tiers 0/1/2 don't dispatch through
    # AGENT_DEPENDENCIES-aware _run_loop() at all, so this scan is a
    # no-op (empty failed_roles) for every other path, same as today.
    failed_roles = [r["role"] for r in results.values()
                    if isinstance(r, dict) and r.get("status") == "failed"]

    # "answer" is just the final role's human-readable text — "output" is
    # kept alongside it so the agent-trace/working panel still has full
    # detail to show. render_agent_result() is the same renderer
    # eo/executor.py's _summarize() uses for the live step panel, so a
    # "fixer"/"verifier"/"implementer"-shaped final role (not just plain
    # {"text": ...}) still comes out as readable markdown here too,
    # instead of falling back to str(dict).
    from eo.result_render import render_agent_result
    final_output = results.get(final_role) if final_role else None
    answer = render_agent_result(final_output) if final_output is not None else ""

    # NEW — Phase CO, CO1 (Master Guide v2, §5): replace the "just the
    # final role's leftover text" answer above with a real synthesis pass
    # across every role's output, but ONLY for actual multi-role runs —
    # a single-role tier-3 result already has nothing to organize, and
    # running this pass there would be pure added latency for zero
    # benefit (same condition AgentTraceDisclosure used to gate its "Show
    # all N agent outputs" toggle on, in frontend/app/components/
    # MessageBubble.jsx, before CO1's frontend piece removes it).
    #
    # Fails open, not closed: if the organizer's own LLM call errors out
    # (exhausted chain, bad response, anything), fall back to the
    # final_role answer already computed above rather than failing the
    # whole run over a synthesis-pass problem — the user still gets a
    # real answer, just not the merged one this pass would have produced.
    #
    # CHANGED — CO4 patch 2: organize_final_answer() now returns
    # {"answer", "dedup_notes"} instead of a bare string — dedup_notes
    # defaults to {} here so the fail-open exception path (and any
    # single-role run that never reaches this branch) always has
    # something safe to put in the result payload below, rather than
    # the frontend needing its own None-guard on a key that may not
    # exist.
    # NEW — CO5 Finding A: snapshot the RESULT of organize_final_answer()
    # (not its inputs) so a LATER, separate HTTP request (GET
    # /api/task/{session_id}/stream, Step 3) has something to read.
    # Today role execution + synthesis happen inside this one POST
    # request -- `answer`/`dedup_notes` are local variables here and
    # vanish when this function returns, same problem
    # paused_execution:{session_id} was introduced to solve for the
    # pause/resume split. Written under the identical bus-key exemption
    # (memory/bus.py's _namespaced(), "pending_synthesis:" prefix) for
    # the identical reason: the stream endpoint's GET hasn't called
    # set_app_slug() either, so this key must not be app_slug-namespaced
    # or that later read would land in the wrong (or default) namespace.
    #
    # CHANGED — CO5 gap fix (post-audit): this used to snapshot
    # role_outputs/user_request/final_role BEFORE calling
    # organize_final_answer(), so that GET /stream could call
    # organize_final_answer_stream() and run the exact same synthesis a
    # second time. That bought nothing (this POST already blocks on the
    # synthesis below before the frontend can even open the
    # EventSource -- there's no "silent wait" left to eliminate) and
    # risked the streamed text diverging from this response's own
    # `answer` (two independent, non-deterministic LLM calls over the
    # same inputs). Snapshotting the already-computed answer instead
    # means the stream route has nothing left to generate -- it just
    # replays this exact string as chunks -- so what the user watches
    # stream in is guaranteed to be byte-for-byte what
    # conversation_memory.append_turn() below persists as this turn's
    # answer, and the LLM is called exactly once per request either way.
    #
    # Gated on the same len(results) > 1 condition as the synthesis call
    # below -- a single-role run has nothing to stream-synthesize either,
    # so writing a snapshot for it would just be a key nothing ever reads.
    # Written after the try/except below (fail-open included) so the
    # snapshot always matches whatever `answer`/`dedup_notes` this
    # response actually ships, even on the fallback path.
    # CO5 Step 7 follow-up: api/routes/tasks.py's stream_answer() now
    # deletes this key itself once it's done reading it, the same
    # "consumer deletes on its way out" pattern paused_execution already
    # uses -- so the normal, happy-path lifetime of this key is just the
    # gap between this write and that route being hit. The ex= here is
    # only a backstop for the *ab*normal path: a client that never opens
    # the SSE connection at all (tab closed, network dropped, browser
    # never got past the POST /api/task response) leaves nothing to run
    # that delete, so this key would otherwise sit in the bus forever.
    # One hour comfortably covers "user's tab is just slow to open the
    # stream" while still bounding the leak for the "never comes back"
    # case.
    dedup_notes = {}
    if len(results) > 1:
        try:
            from agents.output_organizer import organize_final_answer
            organized = organize_final_answer(results, task_text, final_role=final_role, session_id=session_id)

            # NEW — D3 Part 3: guard the merged answer before it replaces
            # the final_role fallback already sitting in `answer`. Same
            # fail-open contract as the except block below -- on a
            # failing check, leave `answer`/`dedup_notes` exactly as they
            # were pre-synthesis rather than shipping a blank, marker-
            # leaking, or structurally-broken answer to chat_store /
            # chat_workspace.
            is_valid, reason = validate_final_answer(organized["answer"])
            # NEW — Patch 13: content check, independent of the
            # structural check just above -- both run at this same
            # choke point since they can fail independently (a
            # well-formed answer can still be unsafe content, and vice
            # versa).
            is_content_safe, content_reason = check_content_safety(
                organized["answer"], label="final_answer",
            )
            if is_valid and is_content_safe:
                answer = organized["answer"]
                dedup_notes = organized["dedup_notes"]
            else:
                combined_reason = "; ".join(
                    r for r in (reason, content_reason) if r
                )
                print(f"  [task_runner] output_organizer answer failed "
                      f"output_guard validation, falling back to "
                      f"final_role's own answer (fail-open): {combined_reason}")
        except Exception as exc:
            print(f"  [task_runner] output_organizer synthesis failed, "
                  f"falling back to final_role's own answer (fail-open): {exc}")

        from memory.bus import write as bus_write
        bus_write(f"pending_synthesis:{session_id}", {
            "answer": answer,
            "dedup_notes": dedup_notes,
        }, ex=3600)

    # NEW — Phase CO, CO2 (Master Guide v2, §5): pull any interactive
    # artifacts a role attached to its own output into a flat top-level
    # list, alongside `answer` — same "extend the result payload with an
    # optional array" shape the guide specifies. No role emits one yet
    # (this patch only adds the plumbing + the html/svg frontend
    # renderer, per CO2's own cheapest-first build order), so this is
    # normally an empty list; harmless either way since
    # MessageBubble.jsx only renders anything when the array is
    # non-empty.
    from eo.result_render import collect_artifacts
    artifacts = collect_artifacts(results)

    routing_memory.log_outcome(task_text, decision, outcome="tier-3 hires-driven pipeline completed")
    return {
        "decision": decision,
        "tier": 3,
        "session_id": session_id,
        "status": "ok",
        "result": {
            "output": results,
            "answer": answer,
            "final_role": final_role,
            "artifacts": artifacts,
            "dedup_notes": dedup_notes,  # NEW — CO4 patch 2
            # NEW — Phase 6d: `status` above stays "ok" — a degraded-but-
            # completed run is still a 200, not a different top-level
            # status. `partial` is the signal the frontend keys off of
            # (e.g. "your device spec is ready, but the write-up failed —
            # retry?"); `failed_roles` names which role(s) degraded so it
            # can offer a targeted retry instead of redoing the whole task.
            "partial": bool(failed_roles),
            "failed_roles": failed_roles,
        },
        "message": None,
    }


def _extract_answer_text(response: dict) -> str:
    """Best-effort flat text for the conversation transcript — mirrors
    eo/executor.py's own _summarize() reasoning (results vary in shape
    across tiers), but doesn't truncate as aggressively since this is
    for context recall, not a UI label. Covers every run_task() return
    shape: cache/SGA/tier-0 use "answer", tier-1 uses "code", tier-2/3
    use "output"; a paused run has neither (nothing to record as "the
    answer" yet); anything else (needs_*, not_wired_yet, error) has no
    "result" payload worth recording, so falls back to "message" (or ""
    if that's also absent)."""
    if response.get("status") == "paused":
        return f"[paused for approval at role '{response.get('result', {}).get('paused_at_role')}']"
    result = response.get("result") or {}
    if "answer" in result:
        return str(result["answer"])
    if "code" in result:
        return str(result["code"])
    if "output" in result:
        return str(result["output"])
    return str(response.get("message") or "")


def _write_plan_panels(response: dict, session_id: str, owner_id: str) -> None:
    """Chat-to-panel writes, patch 2: after a tier-3 hires-driven run
    completes, push any of the six PLAN_ROLE_PANEL_MAP roles' output
    straight into its PlanTab panel (eo/panel_content.py's patch 1),
    instead of leaving it sitting only in the chat answer for someone to
    manually copy-paste.

    Only tier 3's result["output"] is the full role-keyed `results` dict
    _run_tier3_hires() hands back (see that function's own comment on
    why "output" there is the full results dict, not just the final
    role's output) — tier 0/1's "answer"/"code" and tier 2's
    single-agent "output" have no role-keyed shape for
    write_panel_from_role() to walk, so this is a deliberate no-op for
    every tier but 3, not something that needs its own guard per tier.
    A "paused" response (mid-run, awaiting approval) is skipped too —
    there's nothing finished yet to write.

    session_id/owner_id: the same pair run_task() already has in hand
    for conversation_memory.append_turn() just below this call — no new
    lookup needed to know "which chat, which caller" this response
    belongs to. workspace_for_chat() itself already no-ops (returns
    None) for a chat that isn't in a workspace at all, which is the
    common case for most chats — most chat turns simply have nothing to
    write here, not an error.

    Best-effort end to end: this must never turn a real, already-
    computed chat answer into a 500 over a panel-write hiccup. A single
    role's write failing doesn't stop the rest of the roles in this same
    response from being tried.
    """
    if response.get("status") != "ok" or response.get("tier") != 3:
        return
    if not session_id or not owner_id:
        return
    results = (response.get("result") or {}).get("output")
    if not isinstance(results, dict):
        return

    try:
        workspace = chat_workspace.workspace_for_chat(session_id, owner_id)
    except Exception as exc:
        print(f"  [task_runner] panel write-back: workspace lookup failed for "
              f"session_id={session_id!r}, skipped (fail-open): {exc}")
        return
    if not workspace:
        return

    ws_id = workspace["id"]
    for role, result in results.items():
        try:
            saved = write_panel_from_role(ws_id, role, result, owner_id)
        except Exception as exc:
            print(f"  [task_runner] panel write-back failed for role={role!r} "
                  f"ws_id={ws_id!r}, skipped (fail-open): {exc}")
            continue
        if not saved:
            continue
        # NEW — live-refetch fix (patch 3 follow-up): tell every dock/tab
        # that has this workspace open to re-fetch this panel now,
        # instead of leaving it to sit unseen until the next full page
        # reload. Fire-and-forget, same as every other emit_*_event()
        # call site — a relay hiccup here must never turn an
        # already-successful panel write into a failed chat turn.
        try:
            emit_workspace_event(
                EventType.PANEL_CONTENT_UPDATED,
                workspace_id=ws_id,
                agent=role,
                payload={"panel_key": saved.get("panel_key"), "workspace_id": ws_id},
            )
        except Exception as exc:
            print(f"  [task_runner] panel-updated event emission failed for "
                  f"role={role!r} ws_id={ws_id!r}, skipped (fail-open): {exc}")


def _write_code_files(response: dict, session_id: str, owner_id: str) -> None:
    """Code sub-tab write-back, patch 9: after a tier-3 hires-driven run
    completes, push whatever code_writers.py's Code Writer Pool (and, if
    it ran this cycle, the Fixer Pool on top of it) actually produced
    into patch 8's workspace_code_files store — same "the tab keeps a
    persisted copy of what chat already produced" pattern
    _write_plan_panels() established for the six Plan panels above,
    applied to Build's Code sub-tab instead.

    Reads the same three bus keys agents/file_manager.py's own
    run_file_manager() reads — fixed_code, submitted_code, file_map —
    with the identical fixed_code-over-submitted_code preference order
    (see that function's own comment: "prefer fixed_code (Fixer Pool's
    cleaned-up output) over submitted_code (Code Writers' raw output)
    when both exist"). This hook runs AFTER _run_task_inner() has
    already returned, i.e. after file_manager.py (if it ran this cycle)
    has already written file_map, so file_map is the authoritative
    module_name -> relative-path mapping for whatever actually landed on
    disk under apps/{app_slug}/ — reading it here instead of guessing a
    path from the module name keeps this write-back from ever
    disagreeing with what patch 11's "Download as ZIP" of that same
    app_slug's apps/ directory would contain, and with what a person
    would see if they opened the file directly on disk.

    Unlike file_manager.py's own _get_module_code() (which defaults an
    unrecognized shape to "python" because it has no file path to fall
    back on), a bare-string code value here is written with
    language=None — write_file() already infers the language from the
    file extension in file_path (see workspace_code_files._infer_language()),
    and file_map always gives us that path, so there's no need to guess.

    Only tier 3, status "ok" — tiers 0/1 never run the Code Writer Pool
    at all, and tier 2 loads an EXISTING app via code_loader.py instead,
    a deliberately separate concern per that module's own docstring (see
    its header: "NOT invoked by loop.py -- this only exists for
    eo/loop_v4.py's tier-2 path"); write-back for that path isn't part
    of this patch. A "paused" response has nothing finished yet to
    write, same as _write_plan_panels()'s own guard.

    Same fail-open posture as _write_plan_panels() throughout: this
    still must never turn an already-computed chat answer into a 500
    over a persistence hiccup.

    Perf audit follow-up (registry.py N+1, part 3): used to call
    write_file() once per file in file_map -- each call was its own
    Postgres pool checkout for the upsert PLUS a second checkout for
    write_audit(), so N files meant 2N sequential round trips on the
    hot path of every finished code task. Now builds the whole batch
    up front and writes it in one call to write_files() -- 2 pool
    checkouts total, not 2N. Trade-off: write_files() validates every
    path before running its single batch statement, so ANY file in the
    batch failing validation (or the statement itself failing) now
    fails the whole batch, caught here at batch granularity instead of
    the old per-file granularity -- see write_files()'s own docstring
    for why that's an acceptable trade-off in practice (file_map's
    paths come from file_manager.py/structure_architect.py, not raw
    user input, so a validation failure here would be a genuine
    upstream bug rather than a routine occurrence).
    """
    if response.get("status") != "ok" or response.get("tier") != 3:
        return
    if not session_id or not owner_id:
        return

    from memory.bus import KEYS, read_many
    _vals = read_many([KEYS["fixed_code"], KEYS["submitted_code"], "file_map"], default=None)
    code_source = _vals[KEYS["fixed_code"]] or _vals[KEYS["submitted_code"]]
    file_map = _vals["file_map"]
    if not code_source or not file_map:
        return

    try:
        workspace = chat_workspace.workspace_for_chat(session_id, owner_id)
    except Exception as exc:
        print(f"  [task_runner] code write-back: workspace lookup failed for "
              f"session_id={session_id!r}, skipped (fail-open): {exc}")
        return
    if not workspace:
        return

    ws_id = workspace["id"]

    # Build the batch first -- one entry per file_map member whose code
    # is non-empty, same non-empty filtering the old loop did before
    # ever reaching write_file(), just collected instead of written
    # immediately. Path validation itself happens inside write_files()
    # (see its own docstring for why that's fine here).
    pending_files = []
    for module_name, rel_path in file_map.items():
        data = code_source.get(module_name)
        if data is None:
            continue
        if isinstance(data, dict):
            code = data.get("code", "")
            language = data.get("language")   # None falls through to write_files()'s own extension guess
        else:
            code = data if isinstance(data, str) else ""
            language = None
        if not code:
            continue
        pending_files.append({"file_path": rel_path, "content": code, "language": language})

    if not pending_files:
        return

    try:
        saved_rows = write_code_files_batch(ws_id, pending_files, owner_id)
    except Exception as exc:
        # A per-file ValueError from _validate_file_path() (or any other
        # write failure) fails the WHOLE batch here -- see write_files()'s
        # own docstring for why that trade-off is acceptable in practice.
        # Still fail-open at the batch level: this must never turn an
        # already-computed chat answer into a 500.
        print(f"  [task_runner] code write-back batch failed for "
              f"{len(pending_files)} file(s), ws_id={ws_id!r}, skipped "
              f"(fail-open): {exc}")
        return

    # NEW — same live-refetch pattern as _write_plan_panels()'s
    # PANEL_CONTENT_UPDATED emission: tell every dock/tab that has this
    # workspace open to re-fetch the Code sub-tab now, instead of
    # leaving it to sit unseen until the next full page reload. Still
    # one event per file (not batched) -- unlike the DB writes above,
    # this isn't a connection-pool cost, and each event's payload
    # carries a single file_path the frontend already knows how to
    # handle; batching this is a separate, lower-priority change.
    for saved in saved_rows:
        try:
            emit_workspace_event(
                EventType.CODE_FILE_UPDATED,
                workspace_id=ws_id,
                agent="code_writers",
                payload={"file_path": saved.get("file_path"), "workspace_id": ws_id},
            )
        except Exception as exc:
            print(f"  [task_runner] code-file-updated event emission failed for "
                  f"path={saved.get('file_path')!r} ws_id={ws_id!r}, skipped "
                  f"(fail-open): {exc}")


def _quota_summary(response: dict, session_id: str) -> dict:
    """Phase 8c — per-task summary of quota-related outcomes, attached
    to every run_task() response as response["quota_summary"].

    - "roles_succeeded"/"roles_degraded"/"failed_roles": derived from
      the same {role: {"status": "failed", ...}} marker shape Phase
      6b/6c's degradation path writes into `results` (see the
      failed_roles scan in _run_tier3_hires() above). Only tier-3's
      hires-driven adaptive path ever produces that shape — every
      other tier/path (cache, SGA, tier-1 fixed/direct, tier-2) has no
      "roles" concept at all, so these come back None there rather
      than 0: "nothing to report" is a different fact from "zero roles
      degraded" and collapsing the two would make an empty run look
      like a clean one.
    - "ledger_waits"/"ledger_reroutes"/"provider_failures": from
      eo/quota_sentinel.get_ledger_event_counts(session_id) — these
      ARE meaningful for every tier/path, since every one of them
      eventually calls utils/llm_client.py's generate_text() with this
      same session_id, regardless of whether that path has a "roles"
      concept.

    Never raises — a summary this is, not a task-completion gate; any
    failure here must never take down the actual response it's
    summarizing."""
    try:
        from eo.quota_sentinel import get_ledger_event_counts
        counts = get_ledger_event_counts(session_id)
    except Exception as exc:
        print(f"  [task_runner] quota summary: ledger event read failed (non-fatal): {exc}")
        counts = {"wait": 0, "reroute": 0, "provider_failure": 0}

    output = (response.get("result") or {}).get("output")
    roles_succeeded = roles_degraded = failed_roles = None
    if isinstance(output, dict):
        failed_roles = [role for role, r in output.items()
                         if isinstance(r, dict) and r.get("status") == "failed"]
        roles_degraded = len(failed_roles)
        roles_succeeded = len(output) - roles_degraded

    return {
        "roles_succeeded": roles_succeeded,
        "roles_degraded": roles_degraded,
        "failed_roles": failed_roles,
        "ledger_waits": counts.get("wait", 0),
        "ledger_reroutes": counts.get("reroute", 0),
        "provider_failures": counts.get("provider_failure", 0),
    }


def run_task(task_text: str, tier_override: int = None, directed_task_type_override: str = None,
             app_slug: str = None, run_tests: bool = False, session_id: str = None,
             mode: str = "auto", project_unique_name: str = None,
             approval_roles: set = None,
             no_conversation_context_roles: set = None, owner_id: str = None,
             attachment: dict = None, topic_id: str = None, scope: str = None,
             tab: str = None) -> dict:
    """
    ...docstring unchanged, plus:

    owner_id: NEW — the authenticated caller's id (server.py's
    require_auth), threaded down to loop_v4._get_decision() so the
    classifier's conversation-memory lookup can pull in linked-chat
    context without violating ownership. Optional so non-HTTP callers
    (tests, scripts) keep working with linked-chat context simply
    skipped.

    attachment: NEW — Data Layer §4a, forwarded unchanged to
    _resolve_decision_and_hires() via _run_task_inner(). See that
    function's docstring for the shape and the reasoning.

    topic_id: NEW — Step 6.11.f. The Notebooks topic (if any) this turn
    is scoped to, e.g. from a "Work through: <step title>" click.
    Forwarded unchanged to _resolve_decision_and_hires() via
    _run_task_inner(), where it's consulted by _grounded_task_text()'s
    exact-topic path before falling back to similarity search. None for
    every non-Notebooks caller — identical behavior to today.

    scope: NEW — task 13d/13e. The Sources sub-tab's scope selector
    value ("general"/"forum"/"news"/"hackernews"), forwarded unchanged
    through _run_task_inner() -> _dispatch_resolved() ->
    _run_tier3_hires() -> run_with_looping() -> execute_graph(), where
    only web_researcher's dispatch branch actually reads it. None for
    every caller that doesn't set it (every existing caller, plus any
    non-research task) — identical behavior to today.

    tab: NEW — Patch B6 (§3.4). Which frontend tab this request
    originated from ("chat", "projects", "notebooks", ...) — the
    person's own open UI tab, not a routing/classification concept.
    Forwarded unchanged through _run_task_inner() -> _dispatch_resolved()
    -> _run_tier3_hires() -> run_with_looping() -> execute_graph(), where
    only eo/executor.py's _run_loop() reads it, to gate the tool-call
    budget pause to the chat tab specifically. None for every caller
    that doesn't set it (every existing caller before this patch) —
    identical behavior to today: no budget enforcement at all.
    """
    # NEW — Patch 13: content-safety guard at intake, before this
    # task_text is persisted to conversation_memory or dispatched to any
    # role. Deliberately checked before the session_id/append_turn lines
    # just below -- a flagged task never gets a turn recorded and never
    # reaches _run_task_inner()'s hire/dispatch machinery at all.
    is_safe, reason = check_content_safety(task_text, label="task_text")
    if not is_safe:
        session_id = session_id or str(uuid.uuid4())
        print(f"  [task_runner] run_task: task_text failed content-safety "
              f"guard, returning early (fail-closed): {reason}")
        return {
            "decision": {}, "tier": -1, "session_id": session_id,
            "status": "error", "result": None,
            # Deliberately vague to the end user (don't echo the
            # classifier's reasoning back to whoever's testing the
            # boundary) -- the full reason is already in the server log
            # line above if you want to look it up.
            "message": "This request couldn't be processed.",
        }

    session_id = session_id or str(uuid.uuid4())
    conversation_memory.append_turn(session_id, "user", task_text)
    response = _run_task_inner(
        task_text, tier_override=tier_override, directed_task_type_override=directed_task_type_override,
        app_slug=app_slug, run_tests=run_tests, session_id=session_id,
        mode=mode, project_unique_name=project_unique_name,
        approval_roles=approval_roles,
        no_conversation_context_roles=no_conversation_context_roles,
        owner_id=owner_id,   # FIXED
        attachment=attachment,   # NEW — Data Layer §4a
        topic_id=topic_id,   # NEW — Step 6.11.f
        scope=scope,   # NEW — task 13d/13e
        tab=tab,   # NEW — Patch B6
    )
    _write_plan_panels(response, session_id, owner_id)   # NEW — chat-to-panel writes, patch 2
    _write_code_files(response, session_id, owner_id)   # NEW — Code sub-tab write-back, patch 9
    response["quota_summary"] = _quota_summary(response, session_id)   # NEW — Phase 8c
    conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response))
    return response


def preview_task(task_text: str, tier_override: int = None, directed_task_type_override: str = None,
                  app_slug: str = None, run_tests: bool = False, session_id: str = None,
                  mode: str = "auto", project_unique_name: str = None, owner_id: str = None) -> dict:
    """...docstring unchanged, plus: owner_id — same contract as run_task()."""
    session_id = session_id or str(uuid.uuid4())
    conversation_memory.append_turn(session_id, "user", task_text)

    resolved = _resolve_decision_and_hires(task_text, tier_override, directed_task_type_override,
                                            app_slug, session_id, mode, owner_id=owner_id)   # FIXED
    if not resolved["resolved"]:
        response = resolved["response"]
        conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response))
        return response

    decision, tier, hires = resolved["decision"], resolved["tier"], resolved["hires"]

    if tier in (0, 1) or not hires:
        response = _dispatch_resolved(task_text, decision, tier, hires, app_slug, run_tests,
                                       session_id, mode, project_unique_name, approval_roles=None,
                                       owner_id=owner_id)
        conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response))
        return response

    return {
        "decision": decision, "tier": tier, "session_id": session_id,
        "status": "preview_ready",
        "result": {"hires": hires},
        "message": ("Review the hires below, then POST to /api/task/confirm with this "
                    "session_id, decision, and hires (edited or not) to dispatch."),
    }


def confirm_task(task_text: str, decision: dict, hires: list, session_id: str,
                  app_slug: str = None, mode: str = "auto", project_unique_name: str = None,
                  approval_roles: set = None,
                  no_conversation_context_roles: set = None, owner_id: str = None,
                  tab: str = None) -> dict:   
    """Part 2 §2.5 — the "confirm" half: takes the (possibly user-edited)
    hires list straight from a preview_task() response and dispatches it
    directly, WITHOUT calling staff_task() a second time (a second call
    would re-run account selection and, for any role the user didn't
    touch, potentially write a redundant brief-writer call for a role
    that's already resolved).

    Each hire may optionally carry `update_library: bool` alongside the
    normal `role`/`agent_key`/`brief` fields — set by the frontend's
    "just this once" vs "update the library" choice (2.5's design). When
    true, this reuses 2.2's update_role_prompt() to make the edited brief
    the new stored default for every future hire of that role; when
    false or absent, the edited brief is used for this one dispatch only
    and the registry entry is left untouched. `update_library` is
    stripped before the hires list is handed to the actual dispatch —
    downstream code (build_execution_graph_from_hires(), etc.) only
    knows about role/agent_key/brief, unchanged.

    tab: NEW — Patch B6, same "forwarded to _dispatch_resolved(), only
    eo/executor.py's _run_loop() reads it" treatment as run_task()'s own
    tab param — preview -> edit -> confirm is still a chat-tab flow when
    that's where it started, so this needs the same pass-through
    run_task() already has, not a second design.
    """
    for hire in hires:
        if hire.get("update_library"):
            update_role_prompt(hire["role"], hire["brief"])

    cleaned_hires = [
        {"role": h["role"], "agent_key": h["agent_key"], "brief": h["brief"]}
        for h in hires
    ]
    tier = decision.get("tier")

    if tier not in (2, 3):
        response = {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "error", "result": None,
            "message": (f"confirm_task() only supports the hires-driven tier 2/3 dispatch "
                        f"path — got tier {tier!r}. Tiers 0/1 (and hires-empty tier 2/3) are "
                        f"never returned as 'preview_ready' by preview_task() in the first place."),
        }
    else:
        # MiniMe Blueprint fix — same workspace_id lookup _run_task_inner()
        # already does for the run_task() path; confirm_task() is the other
        # real route into a tier-3 hires dispatch (preview_task() -> user
        # edits hires -> confirm_task()), so hardware_speccer needs the same
        # workspace_id here or it raises ValueError the moment it's hired
        # through this path instead of the plain run_task() one.
        workspace = chat_workspace.workspace_for_chat(session_id, owner_id) if (session_id and owner_id) else None
        workspace_id = workspace["id"] if workspace else None
        response = _dispatch_resolved(task_text, decision, tier, cleaned_hires, app_slug,
                                       run_tests=False, session_id=session_id, mode=mode,
                                       project_unique_name=project_unique_name,
                                       approval_roles=approval_roles,
                                       no_conversation_context_roles=no_conversation_context_roles,
                                       workspace_id=workspace_id, owner_id=owner_id, tab=tab)

    conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response), owner_id=owner_id)   
    return response


def run_task_from_template(template_id: str, task_text: str, session_id: str = None,
                            mode: str = "auto", project_unique_name: str = None,
                            owner_id: str = None, scope: str = None) -> dict:   
    """Part 2 §2.3/§2.6 — the entrypoint eo/structure.py's
    save_workflow_template()/classification_from_template() were built
    for but, until now, had nothing on the API side actually calling
    them: starting a new task from a saved workflow template instead of
    running the Inspector/Panel classification at all.

    Mirrors run_task()'s own turn-recording wrapper shape, but skips
    loop_v4._get_decision() entirely — classification_from_template()
    already produces the identical decision shape a real Inspector/Panel
    classification would, per that function's own docstring.

    Raises KeyError if template_id doesn't match a saved template — meant
    to be caught at the API layer and turned into a 404, same convention
    eo/executor.py's resume_graph() already uses for an unknown
    session_id.

    Always tier 3 (classification_from_template() fixes this): reachable
    through _dispatch_resolved()'s existing tier==3 branch, hires-driven,
    exactly like a normal Panel-staffed adaptive task — no new dispatch
    code needed for the two to behave identically once hires exist.

    approval_roles and no_conversation_context_roles come from the
    template itself (see save_workflow_template()'s schema in
    eo/structure.py), not from a caller-supplied argument here — a saved
    template is the single source of truth for both, the same way its
    `roles` list is already the single source of truth for
    execution_order.

    Known duplication, flagged rather than silently copied: the
    offer_beast_mode / stop_ask_beast_mode gating below is the same logic
    _resolve_decision_and_hires() applies after its own staff_task() call.
    Not reused directly because that function is written tightly around
    loop_v4._get_decision()'s classification path, which this entrypoint
    deliberately bypasses. Worth factoring into one shared helper if a
    third caller ever needs the same gating — not done here to keep this
    change to exactly what §2.6 needs.

    scope: NEW — task 13e. Closes the gap flagged when 13d landed:
    _dispatch_resolved()'s scope param was only ever reached from
    run_task() -> _run_task_inner(), so a template built around
    web_researcher would silently fall back to "general" once dispatched
    from here. Forwarded unchanged to _dispatch_resolved() below, same
    "tier-3 hires-driven branch only, no-op otherwise" scoping scope
    already has everywhere else. Still None for every template that
    doesn't hire web_researcher, and for every caller that doesn't pass
    it — no behavior change for existing templates."""
    template = get_workflow_template(template_id)
    if template is None:
        raise KeyError(f"No workflow template found for template_id={template_id!r}")
    # Recent-templates feature — stamp the moment this template is
    # actually dispatched. Deliberately here (once run_task_from_template
    # is genuinely committed to running it) rather than inside a
    # separate "did the user click run" API call, so recency reflects
    # real dispatches, not just opening the picker.
    record_template_run(template_id)

    session_id = session_id or str(uuid.uuid4())
    conversation_memory.append_turn(session_id, "user", task_text, owner_id=owner_id)   

    decision = classification_from_template(template)
    tier = decision["tier"]

    # staff_task() needs the real task text (to write a good brief if a
    # template role is genuinely new to this system) and session_id (so
    # the brief-writer's agent_start/agent_done events land on the same
    # live channel the frontend is already subscribed to) — identical
    # call shape to _resolve_decision_and_hires()'s own staff_task() call.
    hires = staff_task(decision, task_text=task_text, session_id=session_id)

    assessed_max = decision.get("agent_count_max", len(decision.get("suggested_agents", [])) or 1)
    mode_result = apply_mode(mode, hires, assessed_max)

    if mode_result["action"] == "offer_beast_mode":
        response = {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "needs_beast_mode_confirmation", "result": {"suggested_hires": len(hires)},
            "message": "This template staffs more roles than the current mode expects. Switch to beast mode?",
        }
        conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response), owner_id=owner_id)   
        return response
    elif mode_result["action"] == "stop_ask_beast_mode":
        response = {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "needs_beast_mode_choice", "result": None,
            "message": "Please choose Beast Mode explicitly for a template this large.",
        }
        conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response), owner_id=owner_id)
        return response

    approval_roles = set(decision.get("approval_roles") or [])
    no_conversation_context_roles = set(decision.get("no_conversation_context_roles") or [])

    # MiniMe Blueprint fix — same workspace_id lookup _run_task_inner()/
    # confirm_task() already do. A saved workflow template is a third real
    # route into a tier-3 hires dispatch, so a template that hires
    # hardware_speccer needs this too, or it raises ValueError the moment
    # it's dispatched from here instead of the plain run_task() path.
    workspace = chat_workspace.workspace_for_chat(session_id, owner_id) if (session_id and owner_id) else None
    workspace_id = workspace["id"] if workspace else None

    response = _dispatch_resolved(
        task_text, decision, tier, mode_result["hires"], app_slug=None,
        run_tests=False, session_id=session_id, mode=mode,
        project_unique_name=project_unique_name,
        approval_roles=approval_roles,
        no_conversation_context_roles=no_conversation_context_roles,
        scope=scope,   # NEW — task 13e
        workspace_id=workspace_id,
        owner_id=owner_id,
    )
    conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response))
    return response


# Bug fix (2026-08-27, prompt-bloat audit): _record_routing_fact() used to
# store the FULL, untruncated task_text on every single task, forever —
# unlike every other fact-writing path in this file (_SGA_FACT_TITLE_MAX /
# _SGA_FACT_SUMMARY_MAX below, fact_summarizer's own bounded summary),
# this one had no cap at all. format_facts_for_prompt() then re-injects
# every stored "text" verbatim into EVERY classify()/Inspector call for
# the workspace (via conversation_memory.get_light_context() ->
# _workspace_facts_text()), so a workspace that's been used for a while
# silently accumulates a multi-hundred-KB "decisions" section that gets
# resent on every future task regardless of how short that task is.
# Capped the same way _SGA_FACT_SUMMARY_MAX caps its sibling write path;
# this is routing/tier metadata for a human glancing at the facts panel,
# not a transcript, so a short excerpt is all it ever needed.
_ROUTING_FACT_TEXT_MAX = 300


def _record_routing_fact(workspace_id: str, tier, task_text: str, session_id: str, decision: dict = None) -> None:
    """D1 — shared by every early-return branch of
    _resolve_decision_and_hires() (cache hit, SGA-resolved) as well as
    the full-decision path below, so a workspace_facts entry gets
    written regardless of which tier a task actually resolves at.
    Previously this was inlined only after the full Inspector decision
    was computed, so the Cached and SGA fast paths — which `return`
    before that point — never reached it at all.

    decision is None for the cache/SGA fast paths (no Inspector
    decision was ever computed for those); the key/title/summary below
    fall back to tier-only labels in that case. This is still just
    routing/tier metadata, not real conversational content — that
    upgrade is D2, out of scope here."""
    if not workspace_id:
        return
    decision = decision or {}
    decision_key = ":".join([
        "routing",
        str(decision.get("tier", tier) or tier or "unknown"),
        str(decision.get("action") or "decision").lower(),
        str(decision.get("directed_task_type") or decision.get("path") or "general").lower(),
    ])
    # Bug fix (2026-08-27): truncate before storing, not just at render
    # time — see _ROUTING_FACT_TEXT_MAX's comment above. Truncating here
    # (rather than only in format_facts_for_prompt()) also keeps the
    # workspace_facts bus key itself from growing without bound.
    excerpt = (task_text or "").strip()
    if len(excerpt) > _ROUTING_FACT_TEXT_MAX:
        excerpt = excerpt[:_ROUTING_FACT_TEXT_MAX - 1].rstrip() + "…"
    workspace_facts.record_section_entry(
        workspace_id,
        "decisions",
        {
            "key": decision_key,
            "title": decision.get("directed_task_type") or decision.get("path") or decision.get("action")
                      or f"Routing decision (tier {tier})",
            "summary": decision.get("reasoning") or decision.get("action") or f"Resolved at tier {tier}",
            "text": excerpt,
            "data": decision or {"tier": tier},
        },
        source="chat_task_runner",
        source_ref=session_id,
        event="decision",
    )


def _should_extract_content_fact(tier) -> bool:
    """Part 2 (Data-bubble content work) — gates the upcoming *content*
    summarizer (Part 3) separately from D1's _record_routing_fact()
    above, which still fires unconditionally on every tier and keeps
    writing cheap routing/classifier metadata regardless of this gate.

    Only tier 2/3 reach Inspector classification + (often) Panel
    staffing of real agents; that's where a task actually carries
    durable, workspace-level content ("target this for students", "use
    TypeScript", "the login bug is in the session refresh") as opposed
    to the trivial lookups cache/SGA/tier-0 exist specifically to catch
    cheaply and at high volume. Tier 1 is a judgment call being skipped
    for now too, for the same reason — revisit if real content turns
    out to be getting missed there in practice, rather than paying the
    summarizer cost speculatively."""
    return tier in (2, 3)


def _maybe_extract_content_fact(workspace_id: str, tier, task_text: str, session_id: str,
                                 response: dict, owner_id: str = None) -> None:
    """Part 3 — the actual content summarizer, gated by
    _should_extract_content_fact() above (tier 2/3 only). Two
    independent fail-open layers, matching
    eo/workspace_facts.py's own _invalidate_facts_cache() discipline
    (try/except, print-and-continue) rather than adding a new way for
    a task to fail:

      1. eo/fact_summarizer.extract_fact() never raises — a model/
         parse error there returns None, same as a genuine
         worth_remembering: false judgment (and, since Patch B2, "and
         no profile signal either" — see that function's docstring).
      2. Both write paths below (workspace fact, profile signals) are
         still wrapped here, since a storage-layer failure is a
         different failure mode than a summarizer failure and callers
         of this function must never see either one — the task's
         actual answer has already been returned to the user by the
         time this runs.

    owner_id: NEW — Patch B2. Only needed for the profile-signal half
    (eo/user_profile.py is keyed by owner_id, not workspace_id); the
    workspace-fact half below is unaffected and still runs without it.
    Missing owner_id (e.g. a system-triggered run with no authenticated
    caller) just means profile signals are skipped for this call,
    same "degrade, don't fail" posture record_section_entry() already
    has for a missing workspace_id at the top of this function.

    Scoped to _run_task_inner()'s auto-dispatch path only for now —
    confirm_task() and run_task_from_template() are separate dispatch
    entrypoints that also reach tier 2/3 and would need this same call
    added if/when content extraction should cover them too. Not done
    here to keep this step to exactly what Part 2/3 (and now B2) need."""
    if not workspace_id or not _should_extract_content_fact(tier):
        return

    answer_text = _extract_answer_text(response)
    if not answer_text:
        return

    fact = fact_summarizer.extract_fact(task_text, answer_text, session_id=session_id)
    if not fact:
        return

    if fact["worth_remembering"]:
        section = workspace_facts.CATEGORY_TO_SECTION.get(fact["category"])
        if not section:
            pass  # unreachable in practice — extract_fact() already validates category
        else:
            try:
                workspace_facts.record_section_entry(
                    workspace_id,
                    section,
                    {
                        "title": fact["title"],
                        "summary": fact["summary"],
                        "data": {"category": fact["category"], "tier": tier},
                    },
                    source="chat_summarizer",   # distinct from D1's source="chat_task_runner"
                    source_ref=session_id,
                    event="upsert",
                )
            except Exception as exc:
                print(f"  [task_runner] content-fact write failed, skipped (fail-open): {exc}")

    _maybe_apply_profile_signals(owner_id, fact.get("profile_signals"), session_id)


def _maybe_apply_profile_signals(owner_id: str, profile_signals, session_id: str) -> None:
    """Patch B2 — writes each of extract_fact()'s already-validated
    `profile_signals` entries into eo/user_profile.py via
    apply_profile_signal(), routed by owner_id rather than
    workspace_id (a person's profile follows them across every
    workspace they touch, per that module's own docstring).

    Each signal is written independently, in its own try/except, so
    one malformed or storage-failing entry can't drop the rest of an
    otherwise-good batch — same reasoning record_section_entry()'s
    per-write isolation already has one level up in
    _maybe_extract_content_fact(), just applied per-signal instead of
    per-call since this is the one place multiple writes can happen
    off a single extraction result."""
    if not owner_id or not profile_signals:
        return

    for signal in profile_signals:
        try:
            user_profile.apply_profile_signal(owner_id, signal, source="chat_summarizer:" + str(session_id))
        except Exception as exc:
            print(f"  [task_runner] profile-signal write failed, skipped (fail-open): {exc}")


_SGA_FACT_TITLE_MAX = 80
_SGA_FACT_SUMMARY_MAX = 300


def _maybe_record_sga_fact(workspace_id: str, task_text: str, session_id: str, sga_result: dict) -> None:
    """Part 5 follow-up — the efficient answer to "run fact_summarizer
    on SGA answers too, or skip it": skip it. SGA (eo/sga.py, Part 5)
    already pays for exactly one LLM call per resolved task, and that
    same JSON response now self-reports "memorable"/"category" for
    free — running fact_summarizer.extract_fact() on top would be a
    second LLM call to re-derive a judgment SGA already made. So this
    writes directly off sga_result instead of calling the summarizer at
    all.

    Deliberately does NOT reuse _should_extract_content_fact() — that
    gate (tier 2/3 only) governs the *summarizer* call's cost, which
    doesn't apply here since there's no second call to gate. The SGA
    fast path gets its own unconditional check: only whether
    sga_result["memorable"] is true.

    title/summary are truncated directly from task_text/answer rather
    than generated — asking a model to write a good title/summary would
    just be the second LLM call this function exists to avoid. A
    slightly blunt title beats paying for a nicer one on every fast-path
    hit.

    Same fail-open discipline as _maybe_extract_content_fact(): a
    storage-layer failure here must never surface past this point, since
    the task's actual answer (sga_result["answer"]) has already been
    returned to the user by the time this runs.
    """
    if not workspace_id or not sga_result.get("memorable"):
        return

    category = sga_result.get("category")
    section = workspace_facts.CATEGORY_TO_SECTION.get(category)
    if not section:
        return  # unreachable in practice — eo/sga.py already validates category

    title = (task_text or "").strip()
    if len(title) > _SGA_FACT_TITLE_MAX:
        title = title[:_SGA_FACT_TITLE_MAX - 1].rstrip() + "…"

    summary = (sga_result.get("answer") or "").strip()
    if len(summary) > _SGA_FACT_SUMMARY_MAX:
        summary = summary[:_SGA_FACT_SUMMARY_MAX - 1].rstrip() + "…"

    try:
        workspace_facts.record_section_entry(
            workspace_id,
            section,
            {
                "title": title or category,
                "summary": summary,
                "data": {"category": category, "tier": "sga"},
            },
            source="chat_sga",   # distinct from "chat_summarizer" (Part 3) and "chat_task_runner" (D1)
            source_ref=session_id,
            event="upsert",
        )
    except Exception as exc:
        print(f"  [task_runner] SGA fact write failed, skipped (fail-open): {exc}")


def _resolve_decision_and_hires(task_text: str, tier_override: int, directed_task_type_override: str,
                                 app_slug: str, session_id: str, mode: str, owner_id: str = None,
                                 attachment: dict = None, topic_id: str = None) -> dict:
    """Part 2 §2.5: the shared first half of dispatch — semantic cache,
    SGA, Inspector/Panel classification, staff_task()'s hiring, and mode
    adjustment — factored out of _run_task_inner() so preview_task() can
    stop exactly here (before any tier actually executes) instead of
    duplicating this logic.
    ...
    owner_id: NEW — passed straight through to loop_v4._get_decision()
    so conversation_memory's linked-chat lookup can be owner-scoped.

    attachment: NEW — Data Layer §4a. When set, `{"kind": ..., "payload":
    ...}` (plus any of process_upload()'s optional ingest_kwargs, e.g.
    "fmt"/"default_title" for kind="import") describing a file/url that
    arrived attached to THIS turn. Its mere presence is the routing
    signal — deterministic, no LLM involved, checked before cache/SGA/
    the Inspector even run (same "resolved before you ever see a task"
    priority those two already have, per eo/inspector.py's own
    docstring). An attached file is never a question for the Inspector
    to guess a domain/agent-set for; it always means "hire Source
    Manager", which (via agents/source_manager.py's own §3a wiring)
    hires the Backlink Detector right after itself, unconditionally.

    topic_id: NEW — Step 6.11.f. Passed straight through to
    _grounded_task_text() below, which tries the deterministic
    exact-topic path before falling back to its own similarity search.
    Unlike attachment, this never short-circuits routing/cache/SGA/
    Inspector — it only changes what context the eventual answer is
    grounded in, so cache/SGA/classification below still see the
    original task_text exactly as before.
    """
    conv_context = conversation_memory.get_full_context(session_id)

    workspace = chat_workspace.workspace_for_chat(session_id, owner_id) if (session_id and owner_id) else None
    workspace_id = workspace["id"] if workspace else None

    if attachment:
        kind = attachment.get("kind")
        payload = attachment.get("payload")
        ingest_kwargs = {k: v for k, v in attachment.items() if k not in ("kind", "payload")}
        decision = {
            "tier": "source", "path": "source", "directed_task_type": None,
            "confidence": 1.0, "suggested_agents": ["source_manager"],
            "execution_order": ["source_manager"], "domain": None,
            "reasoning": ("attachment present on this turn — routed straight to Source "
                          "Manager, bypassing Inspector classification (§4a: deterministic, "
                          "no LLM needed to know a file means ingestion)."),
        }
        try:
            result = process_upload(kind, payload, workspace_id, session_id=session_id,
                                     created_by=owner_id or "user", **ingest_kwargs)
            status, message = "ok", None
        except Exception as exc:
            # Same "let the caller translate this into an error" posture
            # every direct upload endpoint already uses for a bad kind/
            # payload (agents/source_manager.py's own process_upload()
            # docstring) — surfaced here rather than silently falling
            # through to cache/SGA/Inspector, since a broken attachment
            # is never actually a plain-text task in disguise.
            result, status, message = None, "error", f"{exc.__class__.__name__}: {exc}"
        _record_routing_fact(workspace_id, "source", task_text, session_id, decision=decision)
        return {"resolved": False, "response": {
            "decision": decision,
            "tier": "source",
            "session_id": session_id,
            "status": status,
            "result": result,
            "message": message,
        }}

    # NEW — bug #4 fix: build once, reuse for every model-facing call below
    # (SGA fast path here, and the tier 0/1/2/3 dispatch further down via
    # the "task_text" key on the returned dict). Cache lookups/writes and
    # routing/staffing decisions deliberately keep using the *original*
    # task_text — grounding is for what the model sees when it actually
    # answers, not for cache keys or the Inspector's classification.
    # CHANGED — Data Layer §9d: _grounded_task_text() now returns the
    # grounded node ids alongside the text (see its own updated
    # docstring) so the "resolved" dict below can carry them through to
    # _run_task_inner()'s new prerequisite-suggestion pass without a
    # second retrieval call.
    grounded_task_text, grounded_node_ids = _grounded_task_text(
        workspace_id, task_text, session_id=session_id, topic_id=topic_id)   # topic_id NEW — Step 6.11.f

    # NEW — Patch B7: classify before touching the cache at all. Deterministic
    # asks keep the exact check_cache()/write_cache() replay behavior that
    # existed before this patch. Generative asks never get a literal replay —
    # get_cached_reference() below only supplies prior material to build on.
    cache_class = classify_cache_class(task_text)
    reference_answer = None
    if tier_override is None and mode != "beast":
        if cache_class == CACHE_CLASS_DETERMINISTIC:
            cached = check_cache(task_text, app_slug=app_slug, workspace_id=workspace_id,
                                 context_text=conv_context, session_id=session_id)
            if cached:
                _record_routing_fact(workspace_id, "cache", task_text, session_id)   # NEW — D1
                return {"resolved": False, "response": {
                    "decision": {},
                    "tier": "cache",
                    "session_id": session_id,
                    "status": "ok",
                    "result": {"answer": cached},
                    "message": None,
                }}
        elif cache_class == CACHE_CLASS_GENERATIVE:
            reference_answer = get_cached_reference(task_text, app_slug=app_slug, workspace_id=workspace_id)

    # NEW — Patch B7: fold the prior answer in as reference context rather
    # than replaying it. grounded_task_text (not task_text) so the model
    # still sees the grounded prompt from the bug #4 fix above.
    sga_input = grounded_task_text
    if reference_answer:
        sga_input = (
            f"{grounded_task_text}\n\n"
            f"(Reference — your previous answer to a similar ask. Build on it, "
            f"refine it, or diverge from it as this new ask calls for; don't just "
            f"repeat it verbatim.)\n{reference_answer}"
        )

    sga_result = sga_attempt(sga_input, session_id=session_id)   # CHANGED — bug #4 fix, was task_text; Patch B7, may include reference
    if sga_result["resolved"]:
        write_cache(task_text, sga_result["answer"], app_slug=app_slug, workspace_id=workspace_id,
                    context_text=conv_context, cache_class=cache_class)   # CHANGED — Patch B7, tags the entry
        _record_routing_fact(workspace_id, "sga", task_text, session_id)   # NEW — D1
        _maybe_record_sga_fact(workspace_id, task_text, session_id, sga_result)   # NEW — Part 5 follow-up
        return {"resolved": False, "response": {
                "decision": {},
                "tier": "sga",
                "session_id": session_id,
                "status": "ok",
                "result": {"answer": sga_result["answer"]},
                "message": None,
            }}

    decision = loop_v4._get_decision(task_text, tier_override, directed_task_type_override,
                                      session_id=session_id, owner_id=owner_id)   # FIXED — now passes owner_id
    tier = decision["tier"]

    _record_routing_fact(workspace_id, tier, task_text, session_id, decision=decision)   # CHANGED — D1, now shared with cache/SGA branches above
    # ... rest unchanged ...

    # staff_task() needs the original task text (to write a good brief if
    # a suggested role is genuinely new) and the session_id (so the
    # brief-writer's agent_start/agent_done events show up on the same
    # live channel the frontend is already subscribed to).
    hires = staff_task(decision, task_text=task_text, session_id=session_id)

    assessed_max = decision.get("agent_count_max", len(decision.get("suggested_agents", [])) or 1)
    mode_result = apply_mode(mode, hires, assessed_max)

    if mode_result["action"] == "offer_beast_mode":
        return {"resolved": False, "response": {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "needs_beast_mode_confirmation", "result": {"suggested_hires": len(hires)},
            "message": "The Inspector assumed it's a Beast Mode level task. Switch to beast mode?",
        }}
    elif mode_result["action"] == "stop_ask_beast_mode":
        return {"resolved": False, "response": {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "needs_beast_mode_choice", "result": None,
            "message": "Please choose Beast Mode explicitly for a task this large.",
        }}

    return {"resolved": True, "decision": decision, "tier": tier, "hires": mode_result["hires"],
            "workspace_id": workspace_id,   # NEW — Part 2, so _run_task_inner can gate content-fact extraction
            "task_text": grounded_task_text,   # NEW — bug #4 fix, so _run_task_inner dispatches the grounded text
            "grounded_node_ids": grounded_node_ids}   # NEW — Data Layer §9d, so _run_task_inner's
            # prerequisite-suggestion pass knows what this turn actually grounded on


def _dispatch_resolved(task_text: str, decision: dict, tier, hires: list, app_slug: str,
                        run_tests: bool, session_id: str, mode: str, project_unique_name: str,
                        approval_roles: set, no_conversation_context_roles: set = None,
                        scope: str = None, workspace_id: str = None, owner_id: str = None,
                        tab: str = None) -> dict:
    """The tier-branch dispatch that runs once classification + hiring
    are resolved — shared by _run_task_inner() (auto, one-shot path),
    confirm_task() (Part 2 §2.5's post-review path, where `hires` may
    have been user-edited since staff_task() first produced it), and
    run_task_from_template() (Part 2 §2.3/§2.6's template-driven path).

    no_conversation_context_roles (Part 2 §2.6) only has any effect on
    the tier-3 hires-driven branch below — tiers 0/1/2 never dispatch
    through generic_worker with a Panel/template-assigned role set the
    same way, so this is a no-op for them, same as approval_roles
    already is.

    scope (task 13d/13e) has the same "tier-3 hires-driven branch only"
    scoping as no_conversation_context_roles above — tiers 0/1/2 have no
    web_researcher dispatch path for it to reach.

    workspace_id: MiniMe Blueprint fix — same "tier-3 hires-driven branch
    only" scoping as scope/no_conversation_context_roles above. Only
    hardware_speccer's dispatch branch (eo/executor.py) actually reads
    it; every other role/tier is unaffected. Callers already compute this
    (see _resolve_decision_and_hires()'s own "workspace_id" return key)
    but it was never forwarded past this point before, so hardware_speccer
    could never actually run even once hired -- it hard-requires
    workspace_id and would raise ValueError without it.

    owner_id (Patch B5 — Output-Format Routing): unlike the params above,
    NOT tier-3-only — forwarded to every tier branch below (0, 1, and 3)
    so eo/user_profile.py's default_format_hint() has an account to look
    a stored output preference up against, regardless of which tier
    actually ends up answering. None (default) is a no-op everywhere.

    tab (Patch B6 — tool-call budget): same "tier-3 hires-driven branch
    only" scoping as scope/no_conversation_context_roles above — tiers
    0/1/2 never reach eo/executor.py's _run_loop() through
    run_with_looping(), the only place this is read. None (default) is
    a no-op."""
    if tier == 0:
        return _run_tier0(task_text, decision, session_id, owner_id=owner_id)
    elif tier == 1:
        return _run_tier1(task_text, decision, run_tests, session_id, owner_id=owner_id)
    elif tier == 2:
        return _run_tier2(task_text, decision, app_slug, session_id, hires=hires,
                           project_unique_name=project_unique_name, mode=mode)
    elif tier == 3:
        # Same "if hires: build/execute, else: fall through" pattern
        # tier 2's _run_tier2 uses, so a hires-driven task (often
        # non-coding) actually reaches generic_worker and can be tested
        # end to end. Deliberately NOT a full tier-3 replacement or a
        # unification with loop.py's 19-agent path.
        if hires:
            return _run_tier3_hires(task_text, decision, session_id, hires=hires,
                                     project_unique_name=project_unique_name, mode=mode,
                                     approval_roles=approval_roles,
                                     no_conversation_context_roles=no_conversation_context_roles,
                                     app_slug=app_slug, scope=scope, workspace_id=workspace_id,
                                     owner_id=owner_id, tab=tab)
        return {
            "decision": decision, "tier": 3, "session_id": session_id,
            "status": "not_wired_yet", "result": None,
            "message": ("Tier 3 requires the real-time relay and background "
                        "execution — not available through this endpoint yet."),
        }
    else:
        return {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "error", "result": None,
            "message": f"Unknown tier {tier!r} returned by the EO layer.",
        }


def _run_task_inner(task_text: str, tier_override: int = None, directed_task_type_override: str = None,
                     app_slug: str = None, run_tests: bool = False, session_id: str = None,
                     mode: str = "auto", project_unique_name: str = None,
                     approval_roles: set = None,
                     no_conversation_context_roles: set = None, owner_id: str = None,
                     attachment: dict = None, topic_id: str = None, scope: str = None,
                     tab: str = None) -> dict:
    """The actual routing/execution body — split out of run_task() so
    that wrapper can do turn-recording on either side without every
    early-return point needing to do it individually. session_id is
    always already resolved to a real value by the time this is called.

    owner_id: NEW — passed through to _resolve_decision_and_hires().
    attachment: NEW — Data Layer §4a, passed through unchanged.
    topic_id: NEW — Step 6.11.f, passed through unchanged.
    scope: task 13d/13e — Sources sub-tab's scope selector value, passed
    through unchanged to _dispatch_resolved() -> _run_tier3_hires(). Only
    web_researcher reads it; a no-op for every other role/tier.
    tab: Patch B6 — passed through unchanged to _dispatch_resolved() ->
    _run_tier3_hires(). Only eo/executor.py's _run_loop() reads it, to
    gate the tool-call budget pause to the chat tab."""
    resolved = _resolve_decision_and_hires(task_text, tier_override, directed_task_type_override,
                                            app_slug, session_id, mode, owner_id=owner_id,
                                            attachment=attachment, topic_id=topic_id)   # FIXED / 6.11.f
    if not resolved["resolved"]:
        return resolved["response"]
    # CHANGED — bug #4 fix: dispatch the grounded text (falls back to the
    # original task_text if the key's ever missing) so tier 0/1/2/3
    # generation actually sees notebook source content, not just task_text.
    response = _dispatch_resolved(resolved.get("task_text", task_text), resolved["decision"], resolved["tier"],
                                   resolved["hires"], app_slug, run_tests, session_id, mode, project_unique_name,
                                   approval_roles, no_conversation_context_roles=no_conversation_context_roles,
                                   scope=scope, workspace_id=resolved.get("workspace_id"), owner_id=owner_id,
                                   tab=tab)
    _maybe_extract_content_fact(resolved.get("workspace_id"), resolved["tier"], task_text, session_id, response,
                                 owner_id=owner_id)   # NEW — Part 2, owner_id threaded through for Patch B2
    _maybe_attach_prerequisite_suggestions(resolved, response, session_id)   # NEW — Data Layer §9d
    return response


def _maybe_attach_prerequisite_suggestions(resolved: dict, response: dict, session_id: str) -> None:
    """Data Layer architecture §9d: chat proactive suggestions. Mutates
    `response` in place — same "attach onto the already-built response"
    shape _maybe_extract_content_fact() (Part 2) already uses just above,
    rather than threading a new return value through every one of
    _dispatch_resolved()'s four tier branches.

    Reactive to what THIS turn actually asked about — workspace_id and
    grounded_node_ids are exactly what _resolve_decision_and_hires()
    already resolved for the turn's own notebook grounding (see
    _grounded_task_text()'s updated docstring), not a second, separate
    lookup. Gated to:
      - tier in (0, 1) — the plain chat-answer tiers. Tiers 2/3 are
        already a "do/build something" dispatch; layering a second,
        unrelated "want more?" offer onto an already-heavy multi-agent
        result would be noise, not help. The cache/SGA/attachment/
        needs_* short-circuit paths never reach here at all —
        `resolved["resolved"]` is False for every one of those, so
        _run_task_inner() returns before this function is ever called.
      - status == "ok" — an error/needs_*/paused response has nothing
        for a person to read a suggestion alongside.

    Never raises past this point — a broken suggestion pass (Secondary
    Data unreadable, workspace mid-write, whatever) must not take an
    otherwise-fine chat answer down with it, same fail-open posture
    _grounded_task_text() itself already takes for its own read.
    """
    workspace_id = resolved.get("workspace_id")
    grounded_node_ids = resolved.get("grounded_node_ids")
    if not workspace_id or not grounded_node_ids:
        return
    if response.get("status") != "ok" or response.get("tier") not in (0, 1):
        return
    try:
        suggestions = find_prerequisite_suggestions(
            workspace_id, grounded_node_ids, session_id=session_id,
        )
    except Exception as exc:
        print(f"  [task_runner] prerequisite suggestion pass failed, skipped (fail-open): {exc}")
        return
    if not suggestions:
        return
    for s in suggestions:
        # NEW — §9d: MessageBubble.jsx's Generate button needs a
        # workspace_id to call generateNotebooks(wsId, ...) with —
        # nothing else on the chat response carries one today (the
        # response's own top-level session_id is a chat/task session,
        # not a workspace).
        s["workspace_id"] = workspace_id
    # tier 0/1 always return a real dict under "result" (never None,
    # see _run_tier0()/_run_tier1() above), so this is safe unguarded.
    response["result"]["prerequisite_suggestions"] = suggestions


def _run_tier2(task_text: str, decision: dict, app_slug: str, session_id: str, hires: list = None,
               project_unique_name: str = None, mode: str = "auto") -> dict:
    directed_task_type = decision.get("directed_task_type")
    if not directed_task_type:
        return {
            "decision": decision,
            "tier": 2,
            "session_id": session_id,
            "status": "needs_directed_task_type",
            "result": None,
            "message": ("Tier 2 requires a directed_task_type, but none was set "
                        "(Inspector/panel disagreement, or a bad override). "
                        "Resubmit with an explicit directed_task_type."),
        }
    if not app_slug:
        available = code_loader.list_available_apps()
        return {
            "decision": decision,
            "tier": 2,
            "session_id": session_id,
            "status": "needs_app",
            "result": {"available_apps": available or []},
            "message": "Tier 2 needs an existing app to act on. Resubmit with app_slug set.",
        }

    code_loader.load_existing_app(app_slug)

    if directed_task_type == "explain_code":
        # Fixed single-agent, read-only route — unaffected by hires or
        # project_unique_name. Nothing here touches disk, so there's no
        # root to redirect.
        import json

        from memory.bus import KEYS, read
        submitted_code = read(KEYS["submitted_code"], default={})
        combined = (
            f"{task_text}\n\nHere is the codebase (module_name -> code):\n"
            + json.dumps(submitted_code, indent=2)
        )
        graph = list(EXPLAIN_CODE_ROUTE)
        results = execute_graph(graph, task_text=combined, session_id=session_id, path="fixed")
        output = results["responder"]
    else:
        # Build from the Panel's staffing decision when it staffed this
        # task; fall back to the fixed DIRECTED_TASK_MAP list when hires
        # is empty. project_unique_name is forwarded to execute_graph()
        # on both branches, which forwards it on to file_manager.py's
        # disk-touching calls — when None, this is the unchanged
        # behavior (writes go to apps/<app_slug>).
        if hires:
            # build_execution_graph_from_hires() returns a 3rd list
            # (role_names) and accepts the Panel's synthesized
            # execution_order, so hired non-coding roles get ordered and
            # dispatched correctly. task_text is passed through too —
            # generic_worker.run() needs the actual task text to build
            # its context.
            agent_names, role_names, key_overrides = build_execution_graph_from_hires(
                hires, execution_order=decision.get("execution_order"))
            results = execute_graph(agent_names, task_text=task_text, session_id=session_id, path="fixed",
                                     key_overrides=key_overrides, project_unique_name=project_unique_name,
                                     mode=mode, role_names=role_names, domain=decision.get("domain"))
            last_agent = role_names[-1] if role_names else agent_names[-1]
        else:
            graph = build_execution_graph(tier=2, directed_task_type=directed_task_type)
            results = execute_graph(graph, session_id=session_id, path="fixed",
                                     project_unique_name=project_unique_name, mode=mode)
            last_agent = graph[-1]
        output = results[last_agent]

    routing_memory.log_outcome(
        task_text, decision, outcome=f"tier-2 {directed_task_type} completed on {app_slug}"
    )
    return {
        "decision": decision,
        "tier": 2,
        "session_id": session_id,
        "status": "ok",
        "result": {
            "directed_task_type": directed_task_type,
            "app_slug": app_slug,
            "output": output,
        },
        "message": None,
    }