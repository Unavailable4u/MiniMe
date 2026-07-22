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

from eo import loop_v4
from eo.modes import apply_mode
from eo.router import build_execution_graph, build_execution_graph_from_hires, EXPLAIN_CODE_ROUTE
from eo.executor import execute_graph
from eo.loop_controller import run_with_looping
from eo.sga import attempt as sga_attempt
from eo.semantic_cache import check_cache, write_cache
from eo.panel import staff_task
from eo.registry import update_role_prompt   # NEW — Part 2 §2.5
from eo.structure import get_workflow_template, classification_from_template, record_template_run   # NEW — Part 2 §2.3/§2.6; record_template_run NEW — recent templates
from eo import code_loader
from eo import routing_memory
from eo import conversation_memory
from eo import chat_workspace
from eo import workspace_facts

def _run_tier0(task_text: str, decision: dict, session_id: str) -> dict:
    graph = build_execution_graph(tier=0)
    results = execute_graph(graph, task_text=task_text, session_id=session_id, path="instant")
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


def _run_tier1(task_text: str, decision: dict, run_tests: bool, session_id: str) -> dict:
    graph = build_execution_graph(tier=1, run_tests=run_tests)
    results = execute_graph(graph, task_text=task_text, session_id=session_id, path="direct")
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
                      app_slug: str = None) -> dict:
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

    looped = run_with_looping(
        hires, decision.get("execution_order"), task_text, session_id=session_id,
        mode=mode, domain=decision.get("domain"), project_unique_name=project_unique_name,
        path="adaptive",
        approval_roles=approval_roles,
        no_conversation_context_roles=no_conversation_context_roles,
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

    routing_memory.log_outcome(task_text, decision, outcome="tier-3 hires-driven pipeline completed")
    return {
        "decision": decision,
        "tier": 3,
        "session_id": session_id,
        "status": "ok",
        "result": {"output": results, "answer": answer, "final_role": final_role},
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


def run_task(task_text: str, tier_override: int = None, directed_task_type_override: str = None,
             app_slug: str = None, run_tests: bool = False, session_id: str = None,
             mode: str = "auto", project_unique_name: str = None,
             approval_roles: set = None,
             no_conversation_context_roles: set = None, owner_id: str = None) -> dict:
    """
    ...docstring unchanged, plus:

    owner_id: NEW — the authenticated caller's id (server.py's
    require_auth), threaded down to loop_v4._get_decision() so the
    classifier's conversation-memory lookup can pull in linked-chat
    context without violating ownership. Optional so non-HTTP callers
    (tests, scripts) keep working with linked-chat context simply
    skipped.
    """
    session_id = session_id or str(uuid.uuid4())
    conversation_memory.append_turn(session_id, "user", task_text)
    response = _run_task_inner(
        task_text, tier_override=tier_override, directed_task_type_override=directed_task_type_override,
        app_slug=app_slug, run_tests=run_tests, session_id=session_id,
        mode=mode, project_unique_name=project_unique_name,
        approval_roles=approval_roles,
        no_conversation_context_roles=no_conversation_context_roles,
        owner_id=owner_id,   # FIXED
    )
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
                                       session_id, mode, project_unique_name, approval_roles=None)
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
                  no_conversation_context_roles: set = None, owner_id: str = None) -> dict:   
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
        response = _dispatch_resolved(task_text, decision, tier, cleaned_hires, app_slug,
                                       run_tests=False, session_id=session_id, mode=mode,
                                       project_unique_name=project_unique_name,
                                       approval_roles=approval_roles,
                                       no_conversation_context_roles=no_conversation_context_roles)

    conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response), owner_id=owner_id)   
    return response


def run_task_from_template(template_id: str, task_text: str, session_id: str = None,
                            mode: str = "auto", project_unique_name: str = None,
                            owner_id: str = None) -> dict:   
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
    change to exactly what §2.6 needs."""
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

    response = _dispatch_resolved(
        task_text, decision, tier, mode_result["hires"], app_slug=None,
        run_tests=False, session_id=session_id, mode=mode,
        project_unique_name=project_unique_name,
        approval_roles=approval_roles,
        no_conversation_context_roles=no_conversation_context_roles,
    )
    conversation_memory.append_turn(session_id, "assistant", _extract_answer_text(response))
    return response


def _resolve_decision_and_hires(task_text: str, tier_override: int, directed_task_type_override: str,
                                 app_slug: str, session_id: str, mode: str, owner_id: str = None) -> dict:
    """Part 2 §2.5: the shared first half of dispatch — semantic cache,
    SGA, Inspector/Panel classification, staff_task()'s hiring, and mode
    adjustment — factored out of _run_task_inner() so preview_task() can
    stop exactly here (before any tier actually executes) instead of
    duplicating this logic.
    ...
    owner_id: NEW — passed straight through to loop_v4._get_decision()
    so conversation_memory's linked-chat lookup can be owner-scoped.
    """
    conv_context = conversation_memory.get_full_context(session_id)

    workspace = chat_workspace.workspace_for_chat(session_id, owner_id) if (session_id and owner_id) else None
    workspace_id = workspace["id"] if workspace else None

    if tier_override is None and mode != "beast":
        cached = check_cache(task_text, app_slug=app_slug, workspace_id=workspace_id, context_text=conv_context)
        if cached:
            return {"resolved": False, "response": {
                "decision": {},
                "tier": "cache",
                "session_id": session_id,
                "status": "ok",
                "result": {"answer": cached},
                "message": None,
            }}

    sga_result = sga_attempt(task_text, session_id=session_id)
    if sga_result["resolved"]:
        write_cache(task_text, sga_result["answer"], app_slug=app_slug, workspace_id=workspace_id, context_text=conv_context)
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

    if workspace_id:
        decision_key = ":".join([
            "routing",
            str(decision.get("tier", "unknown")),
            str(decision.get("action") or "decision").lower(),
            str(decision.get("directed_task_type") or decision.get("path") or "general").lower(),
        ])
        workspace_facts.record_section_entry(
            workspace_id,
            "decisions",
            {
                "key": decision_key,
                "title": decision.get("directed_task_type") or decision.get("path") or decision.get("action") or "Routing decision",
                "summary": decision.get("reasoning") or decision.get("action") or "Routing decision",
                "text": task_text,
                "data": decision,
            },
            source="chat_task_runner",
            source_ref=session_id,
            event="decision",
        )
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

    return {"resolved": True, "decision": decision, "tier": tier, "hires": mode_result["hires"]}


def _dispatch_resolved(task_text: str, decision: dict, tier, hires: list, app_slug: str,
                        run_tests: bool, session_id: str, mode: str, project_unique_name: str,
                        approval_roles: set, no_conversation_context_roles: set = None) -> dict:
    """The tier-branch dispatch that runs once classification + hiring
    are resolved — shared by _run_task_inner() (auto, one-shot path),
    confirm_task() (Part 2 §2.5's post-review path, where `hires` may
    have been user-edited since staff_task() first produced it), and
    run_task_from_template() (Part 2 §2.3/§2.6's template-driven path).

    no_conversation_context_roles (Part 2 §2.6) only has any effect on
    the tier-3 hires-driven branch below — tiers 0/1/2 never dispatch
    through generic_worker with a Panel/template-assigned role set the
    same way, so this is a no-op for them, same as approval_roles
    already is."""
    if tier == 0:
        return _run_tier0(task_text, decision, session_id)
    elif tier == 1:
        return _run_tier1(task_text, decision, run_tests, session_id)
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
                                     app_slug=app_slug)
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
                     no_conversation_context_roles: set = None, owner_id: str = None) -> dict:
    """The actual routing/execution body — split out of run_task() so
    that wrapper can do turn-recording on either side without every
    early-return point needing to do it individually. session_id is
    always already resolved to a real value by the time this is called.

    owner_id: NEW — passed through to _resolve_decision_and_hires()."""
    resolved = _resolve_decision_and_hires(task_text, tier_override, directed_task_type_override,
                                            app_slug, session_id, mode, owner_id=owner_id)   # FIXED
    if not resolved["resolved"]:
        return resolved["response"]
    return _dispatch_resolved(task_text, resolved["decision"], resolved["tier"], resolved["hires"],
                               app_slug, run_tests, session_id, mode, project_unique_name, approval_roles,
                               no_conversation_context_roles=no_conversation_context_roles)

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
        from memory.bus import read, KEYS
        import json
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