"""
eo/loop_v4.py — Stage 4 steps 4-7 of the roadmap (Part 10):
    4. "Let tier 0/1 classifications actually take effect."
    5. "Enable tier 2 directed-task routing."
    6. "Enable panel escalation."
    7. "Wire eo:routing_outcome and the Cross-Cycle Memory Search feedback
        loop."
Plus Part 8.1's cost-ceiling confirmation before tier 3.

This replaces the earlier "always forced to tier 3, Inspector only
observes" version (Stage 4.2). Real routing now happens:

  tier 0 -> eo/executor.py runs ["responder"] directly, no memory, no
            loop.py, answer printed and returned.
  tier 1 -> eo/executor.py runs the lean pipeline (Part 2.4), optionally
            appending sandbox_tester_lean if --test was passed.
  tier 2 -> eo/code_loader.py loads an existing app's code from disk into
            memory first (a directed task needs a codebase to act on),
            then eo/executor.py runs the directed_task_type's agent
            subset from eo/router.py's DIRECTED_TASK_MAP.
  tier 3 -> Part 8.1's cost-ceiling confirmation, then hands off to
            loop.py exactly as before -- unmodified, same process, same
            behavior. This is still the only tier that touches the full
            19-agent roster and the full infra layer.

Manual override (--tier N) bypasses the Inspector's own tier entirely,
per Part 3 ("you sometimes know better than the classifier ... generate
labeled (task, correct_tier) pairs to calibrate the confidence
threshold"). It does NOT bypass tier 3's cost-ceiling confirmation --
that guardrail exists specifically for the expensive case, and knowing
you want tier 3 doesn't make it free.

Usage:
    python eo/loop_v4.py "a one-sentence idea for the app"      (routes automatically)
    python eo/loop_v4.py --tier 1 "reverse a string from stdin" (manual override)
    python eo/loop_v4.py --tier 1 --test "..."                  (tier 1 + sandbox test)
    python eo/loop_v4.py --tier 2 --directed-task-type debug --app my_app "fix the login bug"
    python eo/loop_v4.py                                         (resumes an existing tier-3 run)
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.inspector import classify
from eo.router import build_execution_graph, DIRECTED_TASK_MAP, EXPLAIN_CODE_ROUTE
from eo.executor import execute_graph
from eo import panel as eo_panel
from eo import code_loader
from eo import routing_memory
from relay.emitter import emit_event 
from memory.bus import write

# Part 8.3 — starting guess, not a measured value. Recalibrate manually
# once eo:routing_outcome has enough entries.
CONFIDENCE_THRESHOLD = 0.75

# The conservative fallback used when the Inspector's own chain is fully
# exhausted (network down, all keys unset, etc.) -- tier 3 is the safe
# default to fall back to, same reasoning as the old forced-tier-3 stage,
# just no longer the ALWAYS case.
_CLASSIFY_FAILURE_DRAFT = {
    "tier": 3, "directed_task_type": None, "confidence": 0.0,
    "suggested_agents": [], "reasoning": "classification failed, defaulting to tier 3 (safest)",
}


def _parse_args(argv: list) -> dict:
    """Manual flag stripping, matching this file's existing style (no new
    dependency on argparse). Returns a dict of parsed options plus the
    remaining task text."""
    args = list(argv)
    opts = {"tier": None, "app": None, "test": False, "directed_task_type": None}
    if "--tier" in args:
        i = args.index("--tier")
        opts["tier"] = int(args[i + 1])
        del args[i:i + 2]
    if "--app" in args:
        i = args.index("--app")
        opts["app"] = args[i + 1]
        del args[i:i + 2]
    if "--directed-task-type" in args:
        i = args.index("--directed-task-type")
        opts["directed_task_type"] = args[i + 1]
        del args[i:i + 2]
    if "--test" in args:
        args.remove("--test")
        opts["test"] = True
    opts["task_text"] = " ".join(args) if args else None
    return opts


def _get_decision(task_text: str, tier_override: int, directed_override: str,
                   session_id: str = None) -> dict:          # <-- add param
    """
    ...docstring unchanged...
    """
    context = routing_memory.retrieve_similar_outcomes(task_text)
    try:
        draft = classify(task_text, context=context or None)
    except Exception as exc:
        print(f"  [Inspector] classification failed ({exc.__class__.__name__}: {exc}), "
              f"defaulting to a conservative tier-3 draft.")
        draft = dict(_CLASSIFY_FAILURE_DRAFT)

    should_escalate = draft["confidence"] < CONFIDENCE_THRESHOLD or draft["tier"] >= 2
    if should_escalate:
        print(f"  [EO] escalating to panel (confidence={draft['confidence']:.2f}, "
              f"tier={draft['tier']}) ...")
        decision = eo_panel.run_panel(task_text, draft)
    else:
        decision = draft

    if tier_override is not None:
        print(f"  [EO] manual override: tier {decision.get('tier')} -> {tier_override}")
        decision = {**decision, "tier": tier_override}
        if tier_override == 2:
            decision["directed_task_type"] = directed_override or decision.get("directed_task_type")

    write("eo:original_task", task_text)
    write("eo:task_classification", draft)
    write("eo:routing_decision", decision)
    write("eo:execution_graph", _safe_graph_preview(decision))

    emit_event("routing_decision", session_id=session_id,               # <-- add this block
                tier=decision.get("tier"), payload=decision)

    return decision


def _safe_graph_preview(decision: dict) -> list:
    """Best-effort execution-graph preview for logging -- swallows the
    ValueError/KeyError build_execution_graph raises for an incomplete
    tier-2 decision (missing directed_task_type) rather than letting a
    logging call crash routing itself."""
    try:
        return build_execution_graph(decision.get("tier"), decision.get("directed_task_type"))
    except (ValueError, KeyError):
        return []


def _run_tier0(task_text: str, decision: dict) -> None:
    graph = build_execution_graph(tier=0)
    results = execute_graph(graph, task_text=task_text)
    answer = results["responder"]
    print(f"\n[Responder]\n{answer}\n")
    routing_memory.log_outcome(task_text, decision, outcome="tier-0 responder answered directly")


def _run_tier1(task_text: str, decision: dict, run_tests: bool) -> None:
    graph = build_execution_graph(tier=1, run_tests=run_tests)
    results = execute_graph(graph, task_text=task_text)
    fixed = results["reviewer_fixer_lean"]
    print(f"\n[Tier 1] module '{fixed.get('name')}':\n{fixed.get('code')}\n")
    if fixed.get("issues_found"):
        print(f"  issues fixed: {fixed['issues_found']}")
    if run_tests and "sandbox_tester_lean" in results:
        test_results = results["sandbox_tester_lean"]
        for name, result in test_results.items():
            status = "PASSED" if result.get("passed") else "FAILED"
            print(f"  [Sandbox] {name}: {status}")
    outcome = "tier-1 lean pipeline completed" + (" and tested" if run_tests else "")
    routing_memory.log_outcome(task_text, decision, outcome=outcome)


def _run_tier2(task_text: str, decision: dict, app_slug: str) -> None:
    directed_task_type = decision.get("directed_task_type")
    if not directed_task_type:
        print("[EO] Tier 2 requires a directed_task_type, but none was set "
              "(Inspector/panel disagreement, or a bad manual override). "
              "Pass --directed-task-type explicitly. Aborting.")
        return
    if not app_slug:
        available = code_loader.list_available_apps()
        print(f"[EO] Tier 2 needs an existing app to act on. Pass --app <slug>. "
              f"Available apps: {available or '(none found under apps/)'}")
        return
    print(f"[EO] Tier 2 ({directed_task_type}) targeting app: {app_slug}")
    code_loader.load_existing_app(app_slug)
    if directed_task_type == "explain_code":
        # explain_code is read-only and doesn't go through the 19-roster
        # agents at all (Part 4's note) -- Responder answers directly,
        # given the loaded code as context.
        from memory.bus import read, KEYS
        submitted_code = read(KEYS["submitted_code"], default={})
        combined = (
            f"{task_text}\n\nHere is the codebase (module_name -> code):\n"
            + json.dumps(submitted_code, indent=2)
        )
        graph = list(EXPLAIN_CODE_ROUTE)
        results = execute_graph(graph, task_text=combined)
        print(f"\n[Responder — explain_code]\n{results['responder']}\n")
    else:
        graph = build_execution_graph(tier=2, directed_task_type=directed_task_type)
        results = execute_graph(graph)
        last_agent = graph[-1]
        print(f"\n[Tier 2 — {directed_task_type}] final output from '{last_agent}':")
        print(json.dumps(results[last_agent], indent=2, default=str))
    routing_memory.log_outcome(task_text, decision, outcome=f"tier-2 {directed_task_type} completed on {app_slug}")


def _confirm_tier3(decision: dict) -> bool:
    """Part 8.1's cost-ceiling confirmation. Defaults to NOT proceeding
    when the classification is panel-reviewed and still borderline
    (confidence below threshold even after escalation) -- exactly the
    "ambiguous tier-3 classification" case Part 8.1 calls out."""
    borderline = decision.get("panel_reviewed") and decision.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
    print("\nThis will run the full 19-agent pipeline (~19 LLM calls, "
          "sandboxed testing, scheduled execution).")
    if borderline:
        print("Note: this classification was panel-reviewed and is still "
              "borderline -- defaulting to NOT proceeding.")
    default = "N"
    confirm = input(f"Proceed with the full tier-3 pipeline? [y/{default}]: ").strip().lower()
    return confirm == "y"


def main():
    opts = _parse_args(sys.argv[1:])
    task_text = opts["task_text"]

    if not task_text:
        print("[EO] No new task text — resuming an existing run. Only tier-3 "
              "runs have resumable state, so handing off to loop.py directly.")
        sys.argv = ["loop.py"]
        import loop
        loop.main()
        return

    print("[EO] Classifying task...")
    decision = _get_decision(task_text, opts["tier"], opts["directed_task_type"])
    tier = decision["tier"]
    print(f"[EO] Routing decision: tier={tier} directed_task_type={decision.get('directed_task_type')} "
          f"confidence={decision.get('confidence', 0):.2f}"
          f"{' (panel-reviewed)' if decision.get('panel_reviewed') else ''} — {decision.get('reasoning', '')}")

    if tier == 0:
        _run_tier0(task_text, decision)
    elif tier == 1:
        _run_tier1(task_text, decision, run_tests=opts["test"])
    elif tier == 2:
        _run_tier2(task_text, decision, app_slug=opts["app"])
    elif tier == 3:
        if not _confirm_tier3(decision):
            print("Aborted — nothing was run.")
            return
        routing_memory.log_outcome(task_text, decision, outcome="handed off to tier-3 loop.py")
        print("[EO] Handing off to loop.py, tier 3, unmodified.\n")
        sys.argv = ["loop.py", task_text]
        import loop
        loop.main()
    else:
        print(f"[EO] Unknown tier {tier!r} — aborting.")


if __name__ == "__main__":
    main()
