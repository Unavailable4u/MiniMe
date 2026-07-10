"""
eo/loop_controller.py

Wraps one pass of execute_graph() with an optional macro-loop: after a
full structure completes, ask a gatekeeper role whether to stop, redo
everything, or redo specific stages (e.g. "add the features I described,
using your own judgment for anything I didn't specify" — a genuinely
iterative ask, not a one-shot).

Every domain takes the same path through _run_gatekeeper() — no
domain-specific branching.
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from eo.router import build_execution_graph_from_hires
from eo.executor import execute_graph
from memory.bus import read, write
from relay.emitter import emit_event

MAX_MACRO_LOOPS = 3
FORCED_CHECKPOINT_EVERY = 5


def _extract_critical_issue(results: dict):
    """Scans every role's result for anything shaped like
    {"issues": [...]} — reviewer/verifier-style output — rather than
    assuming one specific key exists, since a non-coding domain may not
    have a role called "reviewer" at all. Returns a frozenset of
    (role, module_or_field, description) tuples for every critical-
    severity issue found across all roles, or an empty frozenset if none.
    Empty (not None) so "no critical issues this loop" reliably
    intersects to nothing against any previous loop, rather than
    short-circuiting the comparison in _hard_safety_check."""
    found = set()
    for role, result in (results or {}).items():
        if not isinstance(result, dict):
            continue
        issues = result.get("issues")
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("severity") == "critical":
                found.add((role, issue.get("module", ""), issue.get("description", "")))
    return frozenset(found)


def _hard_safety_check(session_id: str, loop_num: int, results: dict) -> dict | None:
    """Deterministic rules checked BEFORE any LLM judgment. Returns a
    decision dict to force STOP/PAUSE, or None if none of the hard rules
    fire (meaning: proceed to the LLM judgment call).

    forced_checkpoint and repeat_failure are resumable PAUSE_FOR_HUMAN
    cases, not hard stops — only hard_cap returns "STOP"."""
    if loop_num >= MAX_MACRO_LOOPS:
        return {"action": "STOP", "cause": "hard_cap"}
    if loop_num > 0 and loop_num % FORCED_CHECKPOINT_EVERY == 0:
        return {"action": "PAUSE_FOR_HUMAN", "cause": "forced_checkpoint"}

    # Repeat-failure breaker: same critical issue flagged 2 loops running.
    # Uses a SET INTERSECTION against the previous loop's critical issues
    # (any overlap counts as a repeat, even if new issues also appeared
    # this loop) — more sensitive to a genuine repeat-failure than exact
    # equality would be (a single new/resolved issue shouldn't mask it).
    prev = frozenset(tuple(item) for item in read(f"prev_critical_issues:{session_id}", default=[]))
    current = _extract_critical_issue(results)
    repeated = current & prev
    write(f"prev_critical_issues:{session_id}", [list(item) for item in current])
    if repeated:
        return {"action": "PAUSE_FOR_HUMAN", "cause": "repeat_failure"}

    return None


def _run_gatekeeper(results: dict, task_text: str, session_id: str, loop_num: int) -> dict:
    """Every domain, coding included, asks the same LLM judgment question
    through generic_worker's "gatekeeper" role. The registry writes this
    brief once, on whichever domain asks for it first, and reuses it
    forever after."""
    hard = _hard_safety_check(session_id, loop_num, results)
    if hard:
        emit_event("macro_loop_decision", session_id=session_id,
                   payload={"decision": hard["action"], "loop": loop_num, "cause": hard["cause"]})
        return hard

    summary = "\n\n".join(f"[{k}]: {str(v)[:400]}" for k, v in results.items())
    from agents.generic_worker import run as generic_run
    raw = generic_run(role="gatekeeper", task_text=(
        f"Original task: {task_text}\n\nWork completed so far:\n{summary}\n\n"
        "Decide: is this genuinely finished, or would another pass improve it "
        "meaningfully? Reply with exactly one line: "
        "'STOP' or 'CONTINUE: <comma-separated roles to redo>'."
    ), session_id=session_id)
    text = raw["text"].strip()
    if text.upper().startswith("STOP"):
        decision = {"action": "STOP"}
    else:
        redo = [r.strip() for r in text.split(":", 1)[1].split(",")] if ":" in text else []
        decision = {"action": "CONTINUE", "redo_roles": redo}

    # This is the single emission point for the LLM-judgment path —
    # _hard_safety_check already emits macro_loop_decision on its own
    # STOP/PAUSE path above (and returns before reaching here), so
    # run_with_looping() below must NOT also emit macro_loop_decision
    # after calling this function.
    emit_event("macro_loop_decision", session_id=session_id,
               payload={"decision": decision["action"], "loop": loop_num})
    return decision


def run_with_looping(hires, execution_order, task_text, session_id, mode,
                      domain=None, project_unique_name=None, path=None,
                      approval_roles: set = None,
                      no_conversation_context_roles: set = None) -> dict:
    """
    `domain` is not branched on anywhere in this module — every domain
    takes the same path through _run_gatekeeper(). Part 2 §2.6: it IS now
    forwarded to execute_graph() on every pass below (a redo pass still
    belongs to the same domain the run started with), purely so
    utils/llm_client.py's log_usage() can tag each call for the
    per-project/per-section usage breakdown — that's the only thing this
    parameter does; the gatekeeper/looping logic itself stays entirely
    domain-agnostic.

    `approval_roles` is passed straight through to execute_graph()
    unchanged. Defaults to None, matching execute_graph()'s own default —
    today's exact full-auto behavior for any caller that doesn't pass it.

    `no_conversation_context_roles` (Part 2 §2.6) is passed straight
    through to execute_graph() on every pass, the identical treatment as
    approval_roles above — a redo pass (mode expert/beast, gatekeeper
    says CONTINUE) still shouldn't hand unscoped roles the full
    conversation transcript just because it's a later macro-loop pass.
    Defaults to None, matching execute_graph()'s own default.

    Return shape: {"results": {role: output, ...}, "final_role": str | None}
    on a normal completion — a caller can't rely on "last key in the dict"
    to know which role's output is "the" answer (the execution order can
    differ between macro-loop passes, and dict.update() keeps an existing
    key's original insertion position), so final_role is explicitly
    tracked as "the last role that finished in the most recent pass".
    final_role is None only if hires was empty and no pass ever ran.

    OR, if execute_graph() paused mid-pass at an approval_roles role:
    {"status": "paused", "paused_at_role": str, "session_id": str}.
    Callers must check for "status" == "paused" before assuming the
    finished shape above — see api/task_runner.py's _run_tier3_hires()
    for the one place that already does.
    """
    current_order = execution_order
    loop_num = 1
    results = {}
    final_role = None

    while True:
        agent_names, role_names, key_overrides = build_execution_graph_from_hires(hires, current_order)
        pass_results = execute_graph(agent_names, role_names=role_names, task_text=task_text,
                                       session_id=session_id, path=path, key_overrides=key_overrides,
                                       project_unique_name=project_unique_name, mode=mode,
                                       approval_roles=approval_roles,
                                       no_conversation_context_roles=no_conversation_context_roles,
                                       domain=domain)

        # execute_graph() returns {"status": "paused", "paused_at_role": role}
        # instead of a finished {role: output} dict when execution hits a
        # role in approval_roles. Must be checked and returned BEFORE the
        # results.update() below — merging that shape in would silently
        # write "status"/"paused_at_role" into `results` as if they were
        # role names.
        if isinstance(pass_results, dict) and pass_results.get("status") == "paused":
            return {
                "status": "paused",
                "paused_at_role": pass_results["paused_at_role"],
                "session_id": session_id,
            }

        results.update(pass_results)   # merge, don't replace — a redo pass should only
                                        # overwrite the specific roles it re-ran, not erase
                                        # everything from earlier passes.
        if pass_results:
            final_role = list(pass_results.keys())[-1]

        if mode.lower() not in ("expert", "beast") or loop_num >= MAX_MACRO_LOOPS:
            break

        decision = _run_gatekeeper(results, task_text, session_id, loop_num)

        if decision["action"] in ("STOP", "PAUSE_FOR_HUMAN"):
            break
        loop_num += 1
        current_order = decision.get("redo_roles") or execution_order

    return {"results": results, "final_role": final_role}