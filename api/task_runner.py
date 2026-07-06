"""
api/task_runner.py — Stage 6, step 2 (Part 10).

A programmatic sibling to eo/loop_v4.py's CLI dispatch. Reuses the same
underlying decision logic (imported straight from loop_v4, not
reimplemented) but returns structured dicts instead of printing to
stdout, so the FastAPI layer (api/server.py) can turn a task into a JSON
response instead of terminal output.

Scope for this step: tiers 0, 1, 2 only, PLUS (Migration Part 10
testability wiring) a hires-driven tier-3 path when the Panel actually
staffed the task — see _run_tier3_hires() below. Tier 3's full
loop.py-backed path is still NOT run here: loop_v4.py's CLI-only tier-3
path blocks on input() for the cost-ceiling confirmation (Part 8.1) and
hands off to loop.py, which is a long-running process, not something a
single HTTP request/response cycle can represent. That full path belongs
later in Stage 6, once the relay (step 1) and background execution exist
to support it — see run_task()'s tier-3 branch below for both the new
hires-driven path and the remaining placeholder response for the
no-hires case.

Deliberately does NOT modify loop_v4.py. It imports loop_v4's
underscore-prefixed helpers directly (_get_decision, in particular)
rather than duplicating the Inspector/panel/override logic — that logic
is the one piece that must never drift between the CLI and the API.

Part 2 (MiniMe v6 migration) addition: the same Starter General Agents
pre-filter (eo/sga.py) that loop_v4.py's CLI runs is added here too, so
a task submitted through the API gets the identical SGA-first treatment
instead of always going straight to the Inspector — this is the one
piece of logic loop_v4.py's own docstring above says must never drift
between the CLI and the API.

Migration Part 8 §8.3: project_unique_name threads through run_task() ->
_run_tier2() -> execute_graph() -> file_manager.py, so a tier-2 task can
target a registered external project (eo/project_registry.py) instead of
this system's own apps/ directory. Only tier 2 touches disk today, so
tiers 0/1 don't need this parameter at all.
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
from eo import code_loader
from eo import routing_memory


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
                      project_unique_name: str = None, mode: str = "auto") -> dict:
    """
    Migration Part 10 testability wiring, updated by Part 14 §2: now
    routes through eo/loop_controller.py's run_with_looping() instead of
    calling execute_graph() directly, so the adaptive-looping machinery
    from Parts 11-12 (macro-loop gatekeeper, hard safety caps) actually
    fires here too — same swap as loop_v4.py's CLI mirror, except this
    path has a real session_id to pass through (the CLI path doesn't).

    Still deliberately not a cost-ceiling-gated or loop.py-unified path;
    when hires is empty, run_task() falls through to the existing
    not_wired_yet response exactly as before this change.

    Note: since run_with_looping() doesn't expose a single "last agent"
    (the execution order can change between macro-loop passes), "output"
    below is now the full role-keyed results dict rather than just the
    final role's output — a necessary shape change, not a stylistic one.
    """
    results = run_with_looping(
        hires, decision.get("execution_order"), task_text, session_id=session_id,
        mode=mode, domain=decision.get("domain"), project_unique_name=project_unique_name,
        path="adaptive",   # NEW — Part 15 §2c, optional path label
    )
    routing_memory.log_outcome(task_text, decision, outcome="tier-3 hires-driven pipeline completed")
    return {
        "decision": decision,
        "tier": 3,
        "session_id": session_id,
        "status": "ok",
        "result": {"output": results},
        "message": None,
    }


def run_task(task_text: str, tier_override: int = None, directed_task_type_override: str = None,
             app_slug: str = None, run_tests: bool = False, session_id: str = None,
             mode: str = "auto", project_unique_name: str = None) -> dict:
    """
    ...docstring unchanged, plus:
    mode: one of "auto", "simple", "fast", "expert", "beast" — controls
        how many staffed hires actually get used (eo/modes.py, Part 3).
    project_unique_name: Migration Part 8 §8.3 — when set, redirects
        tier-2 disk writes to the external project registered under
        this control-unit name (eo/project_registry.py) instead of this
        system's own apps/<app_slug> directory. Has no effect on tiers
        0/1, which never touch disk.
    """
    session_id = session_id or str(uuid.uuid4())

    # NEW — Part 4 step 4: Semantic Cache checked first, ahead of SGA
    # itself, so a near-duplicate task skips the whole SGA relay too —
    # same as loop_v4.py's CLI path, kept symmetric per the guide.
    # CHANGE: an explicit "beast" mode selection also skips cache/SGA,
    # same as a manual tier_override does — Beast Mode is meant to force
    # the full staffed pipeline, not have a fast-path answer slip in
    # ahead of it. Auto/simple/fast/expert are unaffected; only "beast"
    # bypasses this block.
    if tier_override is None and mode != "beast":
        cached = check_cache(task_text, app_slug=app_slug)
        if cached:
            return {
                "decision": {},
                "tier": "cache",
                "session_id": session_id,
                "status": "ok",
                "result": {"answer": cached},
                "message": None,
            }

        sga_result = sga_attempt(task_text, session_id=session_id)
        if sga_result["resolved"]:
            write_cache(task_text, sga_result["answer"], app_slug=app_slug)
            return {
                "decision": {},
                "tier": "sga",
                "session_id": session_id,
                "status": "ok",
                "result": {"answer": sga_result["answer"]},
                "message": None,
            }

    decision = loop_v4._get_decision(task_text, tier_override, directed_task_type_override, session_id=session_id)
    tier = decision["tier"]

    # CHANGE — Part 7 §2.1: staff_task() now needs the original task text
    # (to write a good brief if a suggested role is genuinely new) and the
    # session_id (so the brief-writer's agent_start/agent_done events show
    # up on the same live channel the frontend is already subscribed to).
    hires = staff_task(decision, task_text=task_text, session_id=session_id)

    # NEW — Part 3: apply mode ceiling
    assessed_max = decision.get("agent_count_max", len(decision.get("suggested_agents", [])) or 1)
    mode_result = apply_mode(mode, hires, assessed_max)

    if mode_result["action"] == "offer_beast_mode":
        return {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "needs_beast_mode_confirmation", "result": {"suggested_hires": len(hires)},
            "message": "The Inspector assumed it's a Beast Mode level task. Switch to beast mode?",
        }
    elif mode_result["action"] == "stop_ask_beast_mode":
        return {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "needs_beast_mode_choice", "result": None,
            "message": "Please choose Beast Mode explicitly for a task this large.",
        }
    hires = mode_result["hires"]

    if tier == 0:
        return _run_tier0(task_text, decision, session_id)
    elif tier == 1:
        return _run_tier1(task_text, decision, run_tests, session_id)
    elif tier == 2:
        return _run_tier2(task_text, decision, app_slug, session_id, hires=hires,
                           project_unique_name=project_unique_name, mode=mode)
    elif tier == 3:
        # Migration Part 10 testability wiring: this is Part 5 §3's same
        # "if hires: build_execution_graph_from_hires() + execute_graph(),
        # else: fall through" pattern that tier 2's _run_tier2 already
        # uses, applied here so a hires-driven task (often non-coding —
        # Part 10's whole point) actually reaches generic_worker and can
        # be tested end to end. Deliberately NOT a full tier-3
        # replacement or a unification with loop.py's 19-agent path —
        # that's out of scope here.
        if hires:
            return _run_tier3_hires(task_text, decision, session_id, hires=hires,
                                     project_unique_name=project_unique_name, mode=mode)
        return {
            "decision": decision, "tier": 3, "session_id": session_id,
            "status": "not_wired_yet", "result": None,
            "message": ("Tier 3 requires the real-time relay and background "
                        "execution (later in Stage 6) — not available through "
                        "this endpoint yet."),
        }
    else:
        return {
            "decision": decision, "tier": tier, "session_id": session_id,
            "status": "error", "result": None,
            "message": f"Unknown tier {tier!r} returned by the EO layer.",
        }

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
        # Fixed single-agent, read-only route -- unaffected by hires or
        # project_unique_name, same reasoning as loop_v4.py's mirrored
        # branch. Nothing here touches disk, so there's no root to
        # redirect.
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
        # Migration Part 5 §3 — build from the Panel's staffing decision
        # when it staffed this task; fall back to the fixed
        # DIRECTED_TASK_MAP list when hires is empty (mirrors
        # loop_v4.py's CLI _run_tier2 exactly).
        #
        # Migration Part 8 §8.3 — project_unique_name is forwarded to
        # execute_graph() on both branches, which forwards it on to
        # file_manager.py's disk-touching calls. When None, this is the
        # exact unchanged behavior (writes go to apps/<app_slug>).
        if hires:
            # Migration Part 10 §3.1/§4: build_execution_graph_from_hires()
            # now returns a 3rd list (role_names) and accepts the Panel's
            # synthesized execution_order, so hired non-coding roles get
            # ordered and dispatched correctly (see eo/router.py's
            # docstring for why role_names exists). task_text is now
            # passed through too — generic_worker.run() needs the actual
            # task text to build its context; before Part 10, nothing in
            # this hires-branch's graph ever read task_text directly, so
            # omitting it was invisible.
            graph, key_overrides, role_names = build_execution_graph_from_hires(
                hires, execution_order=decision.get("execution_order"))
            results = execute_graph(graph, task_text=task_text, session_id=session_id, path="fixed",
                                     key_overrides=key_overrides, project_unique_name=project_unique_name,
                                     mode=mode, role_names=role_names)
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