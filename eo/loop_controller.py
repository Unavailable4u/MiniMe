"""
eo/loop_controller.py — v6 migration Part 11 §3.1, superseded in place by
Part 12 §1.

Wraps one pass of execute_graph() with an optional macro-loop: after a
full structure completes, ask a gatekeeper-equivalent whether to stop,
redo everything, or redo specific stages (e.g. "add the features I
described, using your own judgment for anything I didn't specify" — a
genuinely iterative ask, not a one-shot).

Migration Part 12 §0/§1: coding no longer keeps a separate code path.
Part 11 §3.1's original `_run_gatekeeper()` special-cased
`if domain == "coding": call agents/gatekeeper.py directly`. That branch
is retired here — agents/gatekeeper.py's three deterministic safety rules
(hard cycle cap, repeat-failure breaker, forced checkpoint) are
generalized into `_hard_safety_check()` below so EVERY domain gets them,
not just coding. agents/gatekeeper.py itself is left untouched and unused
in the repo; nothing here imports it.
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from eo.router import build_execution_graph_from_hires
from eo.executor import execute_graph
from memory.bus import read, write
from relay.emitter import emit_event

MAX_MACRO_LOOPS = 3          # matches Part 11's original cap
FORCED_CHECKPOINT_EVERY = 5  # same cadence agents/gatekeeper.py already used for coding


def _extract_critical_issue(results: dict):
    """
    Generalized past coding's own review_notes["issues"] shape (the
    original agents/gatekeeper.py._get_critical_issue_keys()). `results`
    here is execute_graph()'s full role-keyed output, so this scans every
    role's result for anything shaped like {"issues": [...]} — reviewer/
    verifier-style output — rather than assuming one specific key exists,
    since a non-coding domain may not have a role called "reviewer" at
    all. Returns a frozenset of (role, module_or_field, description)
    tuples for every critical-severity issue found across all roles, or
    an empty frozenset if none. Empty (not None) so "no critical issues
    this loop" reliably intersects to nothing against any previous loop,
    rather than short-circuiting the comparison in _hard_safety_check.
    """
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
    """The three deterministic rules agents/gatekeeper.py always ran
    BEFORE any LLM judgment — generalized past coding, domain-agnostic.
    Returns a decision dict to force STOP/PAUSE, or None if none of the
    hard rules fire (meaning: proceed to the LLM judgment call).

    Note on the "action" values below: Part 12 §1's guide snippet maps
    both forced_checkpoint and repeat_failure to a plain "STOP", same as
    hard_cap. That collapses a real distinction the original
    agents/gatekeeper.py made -- those two were resumable PAUSE_FOR_HUMAN
    cases, not hard stops. Kept that distinction here; only hard_cap
    returns "STOP". Flag if you want the literal guide behavior (all
    three -> "STOP") instead.
    """
    if loop_num >= MAX_MACRO_LOOPS:
        return {"action": "STOP", "cause": "hard_cap"}
    if loop_num > 0 and loop_num % FORCED_CHECKPOINT_EVERY == 0:
        return {"action": "PAUSE_FOR_HUMAN", "cause": "forced_checkpoint"}

    # Repeat-failure breaker: same critical issue flagged 2 loops running.
    # Migration note: kept as a SET INTERSECTION against the previous
    # loop's critical issues (any overlap counts as a repeat, even if new
    # issues also appeared this loop), matching the original
    # agents/gatekeeper.py behavior -- Part 12 §1's snippet uses exact
    # equality instead, which is strictly less sensitive to a genuine
    # repeat-failure (any single new/resolved issue would mask it).
    prev = frozenset(tuple(item) for item in read(f"prev_critical_issues:{session_id}", default=[]))
    current = _extract_critical_issue(results)
    repeated = current & prev
    write(f"prev_critical_issues:{session_id}", [list(item) for item in current])
    if repeated:
        return {"action": "PAUSE_FOR_HUMAN", "cause": "repeat_failure"}

    return None


def _run_gatekeeper(results: dict, task_text: str, session_id: str, loop_num: int) -> dict:
    """
    Migration Part 12 §1: replaces Part 11 §3.1's domain-branching
    version entirely. No domain branch anymore -- EVERY domain, coding
    included, asks the same LLM judgment question through
    generic_worker's "gatekeeper" role. Part 7's registry writes this
    brief ONCE, on whichever domain asks for it first, and reuses it
    forever after -- coding no longer needs its own separate prompt for
    this.
    """
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

    # Migration Part 12 item 2 (double event emission): _hard_safety_check
    # already emits macro_loop_decision on its own STOP/PAUSE path above
    # (and returns before reaching here). This is the single emission
    # point for the LLM-judgment path, so run_with_looping() below must
    # NOT also emit macro_loop_decision after calling this function --
    # doing both was a duplicate fire for the same decision.
    emit_event("macro_loop_decision", session_id=session_id,
               payload={"decision": decision["action"], "loop": loop_num})
    return decision


def run_with_looping(hires, execution_order, task_text, session_id, mode,
                      domain=None, project_unique_name=None, path=None) -> dict:
    """
    `domain` is accepted for call-site compatibility with Part 11 §3.2's
    wiring but is no longer branched on anywhere in this module (Part 12
    §0) -- every domain takes the same path through _run_gatekeeper().
    """
    current_order = execution_order
    loop_num = 1
    results = {}

    while True:
        agent_names, role_names, key_overrides = build_execution_graph_from_hires(hires, current_order)
        pass_results = execute_graph(agent_names, role_names=role_names, task_text=task_text,
                                       session_id=session_id, path=path, key_overrides=key_overrides,
                                       project_unique_name=project_unique_name, mode=mode)
        results.update(pass_results)   # merge, don't replace — a redo pass should only
                                        # overwrite the specific roles it re-ran, not erase
                                        # everything from earlier passes.

        if mode.lower() not in ("expert", "beast") or loop_num >= MAX_MACRO_LOOPS:
            break

        # Migration Part 12 §1 call-site change: loop_num is now passed
        # through so _hard_safety_check can actually count loops (Part
        # 11's original call site never passed it at all).
        decision = _run_gatekeeper(results, task_text, session_id, loop_num)

        if decision["action"] in ("STOP", "PAUSE_FOR_HUMAN"):
            break
        loop_num += 1
        current_order = decision.get("redo_roles") or execution_order

    return results