"""
api/task_runner.py — Stage 6, step 2 (Part 10).

A programmatic sibling to eo/loop_v4.py's CLI dispatch. Reuses the same
underlying decision logic (imported straight from loop_v4, not
reimplemented) but returns structured dicts instead of printing to
stdout, so the FastAPI layer (api/server.py) can turn a task into a JSON
response instead of terminal output.

Scope for this step: tiers 0, 1, 2 only. Each of those already resolves
in a single call — no background process, no interactivity. Tier 3 is
NOT run here: loop_v4.py's tier-3 path blocks on input() for the
cost-ceiling confirmation (Part 8.1) and hands off to loop.py, which is
a long-running process, not something a single HTTP request/response
cycle can represent. Wiring tier 3 through the API belongs later in
Stage 6, once the relay (step 1) and background execution exist to
support it — see run_task()'s tier-3 branch below for the placeholder
response.

Deliberately does NOT modify loop_v4.py. It imports loop_v4's
underscore-prefixed helpers directly (_get_decision, in particular)
rather than duplicating the Inspector/panel/override logic — that logic
is the one piece that must never drift between the CLI and the API.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo import loop_v4
from eo.router import build_execution_graph, EXPLAIN_CODE_ROUTE
from eo.executor import execute_graph
from eo import code_loader
from eo import routing_memory


def run_task(task_text: str, tier_override: int = None, directed_task_type_override: str = None,
             app_slug: str = None, run_tests: bool = False, session_id: str = None) -> dict:
    """
    Runs one task through the EO layer and returns a single structured
    result dict — no printing, no blocking on stdin.

    session_id: pass the frontend's own generated ID here so it can
        subscribe to the Pusher channel *before* the request is sent
        (see relay/emitter.py's channel-per-session-id design). Since
        this endpoint runs the whole graph synchronously and only
        returns once it's done, a session_id minted after the fact
        would be useless -- every event would already have fired with
        nobody subscribed yet. Falls back to generating one here only
        for callers that don't care about live events (CLI, tests).

    Returns a dict always shaped as:
        {
            "decision": {...},      # the Inspector/panel routing decision
            "tier": int,
            "session_id": str,      # the caller's session_id, or one
                                     # generated here if none was given
                                     # (Stage 6 step 4) -- the frontend
                                     # subscribes to this session's Pusher
                                     # channel to watch the run live
            "status": "ok" | "error" | "needs_app" | "needs_directed_task_type" | "not_wired_yet",
            "result": {...} | None, # tier-specific payload, see below
            "message": str | None,  # human-readable note, set on non-"ok" statuses
        }

    result payload by tier:
        tier 0 -> { "answer": str }
        tier 1 -> { "module_name": str, "code": str, "issues_found": [...],
                     "test_results": {...} | None }
        tier 2 -> { "directed_task_type": str, "app_slug": str, "output": ... }
    """
    decision = loop_v4._get_decision(task_text, tier_override, directed_task_type_override)
    tier = decision["tier"]

    # One session_id per incoming task, regardless of tier. Prefer the
    # caller's own ID (the frontend generates one and subscribes to its
    # Pusher channel BEFORE sending the request -- see the session_id
    # docstring note above for why that ordering matters). Only mint a
    # fresh one here for callers that don't pass one (CLI, tests) --
    # included on every returned dict, even the not_wired_yet/error
    # branches, so server.py can always read result["session_id"] the
    # same way without branching on status (Stage 6 step 4, item 1).
    session_id = session_id or str(uuid.uuid4())

    if tier == 0:
        return _run_tier0(task_text, decision, session_id)
    elif tier == 1:
        return _run_tier1(task_text, decision, run_tests, session_id)
    elif tier == 2:
        return _run_tier2(task_text, decision, app_slug, session_id)
    elif tier == 3:
        return {
            "decision": decision,
            "tier": 3,
            "session_id": session_id,
            "status": "not_wired_yet",
            "result": None,
            "message": ("Tier 3 requires the real-time relay and background "
                        "execution (later in Stage 6) — not available through "
                        "this endpoint yet."),
        }
    else:
        return {
            "decision": decision,
            "tier": tier,
            "session_id": session_id,
            "status": "error",
            "result": None,
            "message": f"Unknown tier {tier!r} returned by the EO layer.",
        }


def _run_tier0(task_text: str, decision: dict, session_id: str) -> dict:
    graph = build_execution_graph(tier=0)
    results = execute_graph(graph, task_text=task_text, session_id=session_id, tier=0)
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
    results = execute_graph(graph, task_text=task_text, session_id=session_id, tier=1)
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


def _run_tier2(task_text: str, decision: dict, app_slug: str, session_id: str) -> dict:
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
        from memory.bus import read, KEYS
        import json
        submitted_code = read(KEYS["submitted_code"], default={})
        combined = (
            f"{task_text}\n\nHere is the codebase (module_name -> code):\n"
            + json.dumps(submitted_code, indent=2)
        )
        graph = list(EXPLAIN_CODE_ROUTE)
        results = execute_graph(graph, task_text=combined, session_id=session_id, tier=2)
        output = results["responder"]
    else:
        graph = build_execution_graph(tier=2, directed_task_type=directed_task_type)
        results = execute_graph(graph, session_id=session_id, tier=2)
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