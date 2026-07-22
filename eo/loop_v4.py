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
            19-agent roster and the full infra layer. User-facing text
            for this tier now calls it the "Ultimate Structure" (Part 3
            step 5 rename) -- internally it's still exactly `tier == 3`.

Manual override (--tier N) bypasses the Inspector's own tier entirely,
per Part 3 ("you sometimes know better than the classifier ... generate
labeled (task, correct_tier) pairs to calibrate the confidence
threshold"). It does NOT bypass tier 3's cost-ceiling confirmation --
that guardrail exists specifically for the expensive case, and knowing
you want tier 3 doesn't make it free.

Part 2 (MiniMe v6 migration) addition: a new Layer 0 (eo/sga.py, the
Starter General Agents) runs BEFORE classification on every task that
isn't a manual --tier override. Most tasks resolve here and never reach
the Inspector at all. If all three SGAs escalate, execution falls
through to classify() exactly as it did before this layer existed.

Migration Part 8 §8.1/§8.3: --register-project registers an external
project folder for cross-project control (eo/project_registry.py), and
--project addresses an already-registered project by its unique_name on
any normal tier-2 task, redirecting file_manager.py's writes into that
project's root instead of this system's own apps/ directory.

Usage:
    python eo/loop_v4.py "a one-sentence idea for the app"      (routes automatically)
    python eo/loop_v4.py --tier 1 "reverse a string from stdin" (manual override)
    python eo/loop_v4.py --tier 1 --test "..."                  (tier 1 + sandbox test)
    python eo/loop_v4.py --tier 2 --directed-task-type debug --app my_app "fix the login bug"
    python eo/loop_v4.py                                         (resumes an existing tier-3 run)
    python eo/loop_v4.py --register-project /path/to/folder "My Project"
    python eo/loop_v4.py --project my_project_a1b2c3 --tier 2 --directed-task-type debug --app my_app "fix the bug"
"""
import os
import sys
import json


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.modes import apply_mode
from eo.inspector import classify
from eo.router import build_execution_graph, build_execution_graph_from_hires, DIRECTED_TASK_MAP, EXPLAIN_CODE_ROUTE
from eo.executor import execute_graph
from eo.loop_controller import run_with_looping
from eo import panel as eo_panel
from eo.panel import staff_task
from eo.sga import attempt as sga_attempt
from eo.semantic_cache import check_cache, write_cache
from eo import code_loader
from eo import routing_memory
from eo import conversation_memory   # NEW — Part 23
from eo.structure import PATH_TO_TIER, TIER_TO_PATH   # CHANGED — Part 26 §4c, was defined locally
from relay.emitter import emit_event
from memory.bus import write

# Part 8.3 — starting guess, not a measured value. Recalibrate manually
# once eo:routing_outcome has enough entries.
CONFIDENCE_THRESHOLD = 0.75

# Migration Part 26 §4c: PATH_TO_TIER / TIER_TO_PATH now come from
# eo/structure.py (one shared definition, imported above) instead of being
# defined here and duplicated again in eo/panel.py. Still the same Part 15
# boundary shim it always was -- translates eo/inspector.py's Part 12
# "path" output back into the "tier" int this file (and eo/panel.py,
# eo/router.py, both not yet renamed) use throughout. This is a boundary
# shim, not a decision-schema rename -- decision/draft keep the "tier" key
# because every downstream collaborator in this file still requires it.
# TIER_TO_PATH (its inverse) is used to label the routing_decision event,
# since relay/emitter.py's own tier->path rename (§4b) means it no longer
# accepts tier= at all.

# The conservative fallback used when the Inspector's own chain is fully
# exhausted (network down, all keys unset, etc.) -- tier 3 is the safe
# default to fall back to, same reasoning as the old forced-tier-3 stage,
# just no longer the ALWAYS case.
_CLASSIFY_FAILURE_DRAFT = {
    "tier": 3, "directed_task_type": None, "confidence": 0.0,
    "suggested_agents": [], "reasoning": "classification failed, defaulting to tier 3 (safest)",
}

# Part 14 §1 -- a tier-3 classification can no longer produce zero
# staffable roles. Keyed by domain; "writer" is the generalist fallback
# for a None/unrecognized domain.
MINIMUM_ADAPTIVE_ROLES = {
    "coding": ["implementer", "verifier", "fixer"],
    "creative_writing": ["writer", "editor"],
    "research": ["researcher", "writer"],
    "data_analysis": ["analyst", "writer"],
}


def _ensure_staffable(decision: dict) -> dict:
    """A tier-3 classification can no longer produce zero staffable
    roles -- if suggested_agents came back empty, fall back to a sane
    minimum set for the classified domain (or a single generalist writer
    if domain is None/unrecognized), so hires downstream is never empty
    either."""
    if decision.get("tier") == 3 and not decision.get("suggested_agents"):
        domain = decision.get("domain")
        decision["suggested_agents"] = MINIMUM_ADAPTIVE_ROLES.get(domain, ["writer"])
    return decision


def _get_decision(task_text: str, tier_override: int, directed_override: str,
                   session_id: str = None, owner_id: str = None) -> dict:
    """
    ...docstring unchanged...

    Part 23: also folds in this session's light conversation-memory
    summary (a follow-up like "now add auth too" reading as more complex
    than the previous turn should be able to bump the classification),
    merged alongside routing_memory's similar-past-tasks context into the
    same `context` slot classify() already treats as pure evidence, never
    an instruction -- see eo/inspector.py's classify() docstring.

    owner_id: NEW — threaded through to conversation_memory.get_light_context()
    so its cross-chat linked-context lookup (chat_store.py) can enforce
    ownership. Optional because non-HTTP callers (the CLI path further
    down this file) have no authenticated owner_id to give it.
    """
    context = routing_memory.retrieve_similar_outcomes(task_text)
    conv_context = conversation_memory.get_light_context(session_id, owner_id)   # FIXED — Part 23, now passes owner_id
    combined_context = "\n\n".join(c for c in [context, conv_context] if c) or None   # NEW — Part 23
    try:
        draft = classify(task_text, context=combined_context, session_id=session_id)   # CHANGED — Part 26, was missing session_id entirely, so classify()'s own agent_start/routing_decision/agent_done events were silent no-ops
        draft["tier"] = PATH_TO_TIER[draft["path"]]   # NEW — Part 15
    except Exception as exc:
        print(f"  [Inspector] classification failed ({exc.__class__.__name__}: {exc}), "
              f"defaulting to a conservative Ultimate Structure (tier 3) draft.")
        draft = dict(_CLASSIFY_FAILURE_DRAFT)
    # ... rest of function unchanged ...

    should_escalate = draft["confidence"] < CONFIDENCE_THRESHOLD or draft["tier"] >= 2
    if should_escalate:
        print(f"  [EO] escalating to panel (confidence={draft['confidence']:.2f}, "
              f"tier={draft['tier']}) ...")
        try:
            decision = eo_panel.run_panel(task_text, draft)
        except Exception as exc:                                    # NEW — Part 15 stopgap
            print(f"  [EO] panel escalation failed ({exc.__class__.__name__}: {exc}), "
                  f"falling back to the Inspector's own draft.")
            decision = draft
    else:
        decision = draft

    if tier_override is not None:
        print(f"  [EO] manual override: tier {decision.get('tier')} -> {tier_override}")
        decision = {**decision, "tier": tier_override}
        if tier_override == 2:
            decision["directed_task_type"] = directed_override or decision.get("directed_task_type")

    decision = _ensure_staffable(decision)   # NEW — Part 14 §1, before
                                              # any write/emit so both
                                              # reflect the filled-in
                                              # suggested_agents, not the
                                              # empty draft.

    write("eo:original_task", task_text)
    write("eo:task_classification", draft)
    write("eo:routing_decision", decision)
    write("eo:execution_graph", _safe_graph_preview(decision))

    emit_event("routing_decision", session_id=session_id,
                path=TIER_TO_PATH.get(decision.get("tier")), payload=decision)

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


def _run_tier2(task_text: str, decision: dict, app_slug: str, hires: list = None,
               project_unique_name: str = None, mode: str = "auto") -> None:
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
        # given the loaded code as context. Fixed single-agent route,
        # unaffected by hires or project_unique_name -- there's no
        # hiring decision to make for a read-only lookup, and nothing
        # here touches disk (same reasoning as router.py's "review"
        # directed task having only one possible reviewer).
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
        # Migration Part 5 §3 — if the Panel actually staffed this task
        # (hires non-empty), build the graph from that staffing decision
        # instead of the fixed DIRECTED_TASK_MAP entry, and thread the
        # Panel's specific key_overrides through. Falls back to the old
        # fixed-list behavior when hires is empty (e.g. a simple directed
        # task where DIRECTED_TASK_MAP's static list is genuinely correct
        # and there's no hiring decision to make).
        #
        # Migration Part 8 §8.1/§8.3 — project_unique_name is forwarded
        # to execute_graph() on both branches, which forwards it on to
        # file_manager.py's disk-touching calls. When None (the default,
        # no --project flag passed), this is the exact unchanged
        # behavior: writes go to apps/<app_slug>.
        if hires:
            # Migration Part 10 §3.1/§4 — mirrors the same change made in
            # api/task_runner.py's _run_tier2 (see that file's comment for
            # the full reasoning): 3-tuple return, execution_order passed
            # in, role_names threaded through, task_text now passed too.
            agent_names, role_names, key_overrides = build_execution_graph_from_hires(
                hires, execution_order=decision.get("execution_order"))
            results = execute_graph(agent_names, task_text=task_text, key_overrides=key_overrides,
                                     project_unique_name=project_unique_name, mode=mode,
                                     role_names=role_names)
            last_agent = role_names[-1] if role_names else agent_names[-1]
        else:
            graph = build_execution_graph(tier=2, directed_task_type=directed_task_type)
            results = execute_graph(graph, project_unique_name=project_unique_name, mode=mode)
            last_agent = graph[-1]
        print(f"\n[Tier 2 — {directed_task_type}] final output from '{last_agent}':")
        print(json.dumps(results[last_agent], indent=2, default=str))
    routing_memory.log_outcome(task_text, decision, outcome=f"tier-2 {directed_task_type} completed on {app_slug}")


def _run_tier3_hires(task_text: str, decision: dict, hires: list,
                      project_unique_name: str = None, mode: str = "auto") -> None:
    """CLI mirror of api/task_runner.py's _run_tier3_hires() — see that
    file's docstring for the full reasoning (Migration Part 10
    testability wiring, Part 5 §3's existing tier-2 pattern applied to
    tier 3).

    Migration Part 14 §2: now routes through eo/loop_controller.py's
    run_with_looping() instead of calling execute_graph() directly, so
    the adaptive-looping machinery from Parts 11-12 (macro-loop
    gatekeeper, hard safety caps) actually fires for a hires-driven
    tier-3 task, rather than sitting fully built and unused. No
    session_id available on this CLI path (same pre-existing gap as
    _get_decision()'s own call above).

    Migration Part B (session isolation fix): generates a throwaway
    per-invocation slug and scopes this run's bus keys to it (see
    api/task_runner.py's _run_tier3_hires() for the full reasoning) —
    without this, two separate CLI runs back to back would share the
    same module_specs/current_plan/submitted_code/etc. exactly like the
    HTTP path did before this fix."""
    from memory.bus import set_app_slug, slugify
    import uuid
    set_app_slug(f"{slugify(task_text)}_{uuid.uuid4().hex[:8]}")

    looped = run_with_looping(
        hires, decision.get("execution_order"), task_text, session_id=None,
        mode=mode, domain=decision.get("domain"), project_unique_name=project_unique_name,
        path="adaptive",   # NEW — Part 15 §2c, optional path label
    )
    results, final_role = looped["results"], looped["final_role"]
    final_output = results.get(final_role) if final_role else None
    if isinstance(final_output, dict) and final_output.get("text"):
        print(f"\n[Tier 3 — hires-driven] final answer (from '{final_role}'):")
        print(final_output["text"])
    print(f"\n[Tier 3 — hires-driven] full results (every role):")
    print(json.dumps(results, indent=2, default=str))
    routing_memory.log_outcome(task_text, decision, outcome="tier-3 hires-driven pipeline completed")


def _confirm_tier3(decision: dict) -> bool:
    """Part 8.1's cost-ceiling confirmation. Defaults to NOT proceeding
    when the classification is panel-reviewed and still borderline
    (confidence below threshold even after escalation) -- exactly the
    "ambiguous tier-3 classification" case Part 8.1 calls out.

    Part 3 step 5: this is a naming/framing change only -- the
    `tier == 3` code path and the confirmation gate itself are unchanged.
    User-facing wording now calls tier 3 the "Ultimate Structure" and
    mentions Beast Mode as an alternative framing, per the migration
    guide's rename."""
    borderline = decision.get("panel_reviewed") and decision.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
    print("\nThis is Ultimate Structure-scale work: the full 19-agent pipeline "
          "(~19 LLM calls, sandboxed testing, scheduled execution).")
    if borderline:
        print("Note: this classification was panel-reviewed and is still "
              "borderline -- defaulting to NOT proceeding.")
    default = "N"
    confirm = input(f"Continue with the Ultimate Structure, or would Beast Mode better "
                     f"fit what you need? Proceed with Ultimate Structure? [y/{default}]: ").strip().lower()
    return confirm == "y"


def _parse_args(argv: list) -> dict:
    """Manual flag stripping, matching this file's existing style (no new
    dependency on argparse). Returns a dict of parsed options plus the
    remaining task text.

    Migration Part 8 §8.1/§8.3: added --register-project PATH NAME (takes
    two args, registers an external project for cross-project control)
    and --project NAME (addresses an already-registered project by its
    unique_name on any normal task)."""
    args = list(argv)
    opts = {"tier": None, "app": None, "test": False, "directed_task_type": None, "mode": "auto",
            "project": None, "register_project": None}
    if "--tier" in args:
        i = args.index("--tier")
        opts["tier"] = int(args[i + 1])
        del args[i:i + 2]
    if "--mode" in args:
        i = args.index("--mode")
        opts["mode"] = args[i + 1]
        del args[i:i + 2]
    if "--app" in args:
        i = args.index("--app")
        opts["app"] = args[i + 1]
        del args[i:i + 2]
    if "--directed-task-type" in args:
        i = args.index("--directed-task-type")
        opts["directed_task_type"] = args[i + 1]
        del args[i:i + 2]
    if "--project" in args:
        i = args.index("--project")
        opts["project"] = args[i + 1]
        del args[i:i + 2]
    if "--register-project" in args:
        i = args.index("--register-project")
        opts["register_project"] = (args[i + 1], args[i + 2])
        del args[i:i + 3]
    if "--test" in args:
        args.remove("--test")
        opts["test"] = True
    opts["task_text"] = " ".join(args) if args else None
    return opts


def main():
    opts = _parse_args(sys.argv[1:])

    # NEW — Part 8 §8.1: registration is a standalone action, not a task,
    # so it short-circuits before the "no task text -> resume tier-3 run"
    # check below (a bare --register-project call has no task_text at
    # all, and shouldn't be misread as "resume").
    if opts["register_project"]:
        from eo.project_registry import generate_control_unit, register_project
        path, display_name = opts["register_project"]
        unit = generate_control_unit(display_name)
        register_project(unit["unique_name"], path)
        print(f"Registered '{display_name}' as '{unit['unique_name']}' -> {path}")
        print(f"You can now address it by name: minime --project {unit['unique_name']} \"...task...\"")
        return

    task_text = opts["task_text"]

    if not task_text:
        print("[EO] No task text given. The old loop.py resume mechanism was retired along "
              "with loop.py — there's no resumable-run feature in the current pipeline yet. "
              "Start a new task, or ask for it to be rebuilt for the adaptive pipeline specifically.")
        return

    # Part 23: the CLI path has never threaded a real session_id through
    # at all -- append_turn()'s "no-op if session_id is falsy" guard
    # means this simply does nothing on the CLI path today. Not a
    # regression (the CLI never had conversation memory before either),
    # just a known limitation -- see conversation_memory.py's own
    # module docstring, and a candidate for a later part if CLI
    # conversation continuity ever matters.
    session_id = None
    conversation_memory.append_turn(session_id, "user", task_text)   # NEW — Part 23 (no-op today)

    # NEW — Part 2: Starter General Agents attempt first, unless a manual
    # --tier override was given (an explicit override skips SGA/cache
    # entirely, same reasoning as the classify() skip below).
    # NEW — Part 4 step 4: Semantic Cache checked first, ahead of SGA
    # itself, so a near-duplicate task skips the whole SGA relay too.
    # CHANGE: --mode beast also skips cache/SGA, same as --tier does —
    # kept symmetric with api/task_runner.py's run_task().
    if opts["tier"] is None and opts["mode"] != "beast":
        conv_context = conversation_memory.get_full_context(session_id)  # NEW
        cached = check_cache(task_text, app_slug=opts["app"], context_text=conv_context)
        if cached:
            print(f"\n[Cache]\n{cached}\n")
            return

        sga_result = sga_attempt(task_text)
        if sga_result["resolved"]:
            write_cache(task_text, sga_result["answer"], app_slug=opts["app"], context_text=conv_context)  # FIXED
            print(f"\n[SGA]\n{sga_result['answer']}\n")
            return

    print("[EO] Classifying task...")
    decision = _get_decision(task_text, opts["tier"], opts["directed_task_type"], session_id=session_id)
    tier = decision["tier"]
    print(f"[EO] Routing decision: tier={tier} directed_task_type={decision.get('directed_task_type')} "
          f"confidence={decision.get('confidence', 0):.2f}"
          f"{' (panel-reviewed)' if decision.get('panel_reviewed') else ''} — {decision.get('reasoning', '')}")

    # CHANGE — Part 7 §2.1: staff_task() now needs the original task text
    # to write a good brief if a suggested role is genuinely new.
    # Note: session_id is passed through as None here — this CLI path has
    # no real session_id variable (same pre-existing gap _get_decision()'s
    # own call above now shares explicitly). Brief-writer events will
    # simply go out unassociated with any session here, exactly like
    # routing_decision events already do.
    hires = staff_task(decision, task_text=task_text)
    assessed_max = decision.get("agent_count_max", len(decision.get("suggested_agents", [])) or 1)
    mode_result = apply_mode(opts["mode"], hires, assessed_max)

    if mode_result["action"] == "offer_beast_mode":
        print("The Inspector assumed it's a Beast Mode level task.")
        confirm = input("Switch to beast mode? [y/N]: ").strip().lower()
        if confirm == "y":
            mode_result = apply_mode("beast", hires, assessed_max)
        else:
            print("Continuing with the capped hire list.")
    elif mode_result["action"] == "stop_ask_beast_mode":
        print("Please choose Beast Mode explicitly for a task this large.")
        return

    hires = mode_result["hires"]
    if tier == 0:
        _run_tier0(task_text, decision)
    elif tier == 1:
        _run_tier1(task_text, decision, run_tests=opts["test"])
    elif tier == 2:
        _run_tier2(task_text, decision, app_slug=opts["app"], hires=hires,
                   project_unique_name=opts["project"], mode=opts["mode"])
    elif tier == 3:
        # Migration Part 14 §1/§3: _ensure_staffable() guarantees hires is
        # never empty for a tier-3 task now, so the old "else: loop.py"
        # fallback is gone — there's no case left for it to catch.
        _run_tier3_hires(task_text, decision, hires=hires,
                          project_unique_name=opts["project"], mode=opts["mode"])
    else:
        print(f"[EO] Unknown tier {tier!r} — aborting.")


if __name__ == "__main__":
    main()