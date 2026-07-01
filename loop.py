"""
loop.py — the orchestrator.
Wires all 19 agents into one running cycle, per Part 4 of the v5 Master
Blueprint:
    Memory Search -> Idea Planner -> Prompt Writer -> Code Writers (x5)
        -> Dependency Mapper -> Test Writer -> Reviewer -> Duplication
        Checker -> Fixer Pool -> Sandbox Tester -> Structure Architect
        -> Security Scanner Pool -> File Manager -> Documentation Agent
        -> Changelog Writer -> Report Writer -> Final QA -> Gatekeeper
        -> (loop or stop)

Every agent module exposes a no-argument run function and reads/writes
everything through memory.bus (Upstash Redis + Vector), so this file's only
job is sequencing, cycle bookkeeping, and turning the Gatekeeper's verdict
into control flow. No business logic lives here.

Usage:
    python loop.py "a one-sentence idea for the app"
    python loop.py            (resumes/continues an existing run from memory)
"""
import sys
import traceback

from memory.bus import read, write, KEYS
from agents import memory_search
from agents import idea_planner
from agents import prompt_writer
from agents import code_writers
from agents import dependency_mapper
from agents import test_writer
from agents import reviewer
from agents import duplication_checker
from agents import fixer_pool
from agents import sandbox_tester
from agents import structure_architect
from agents import security_scanner
from agents import file_manager
from agents import documentation_agent
from agents import changelog_writer
from agents import report_writer
from agents import final_qa
from agents import gatekeeper


def _print_header(cycle_num: int) -> None:
    print("\n" + "=" * 60)
    print(f"  CYCLE {cycle_num}")
    print("=" * 60)


def _print_status(label: str, detail: str = "") -> None:
    line = f"  [{label}]"
    if detail:
        line += f" {detail}"
    print(line)


def run_one_cycle(cycle_num: int) -> str:
    """
    Runs every agent once, in order, for a single cycle.
    Returns the Gatekeeper's decision string.
    """
    _print_header(cycle_num)

    _print_status("Memory Search", "retrieving relevant context from past cycles...")
    context = memory_search.run()
    _print_status("Memory Search done", f"{len(context.splitlines())} prior note(s) found" if context else "no prior context")

    _print_status("Planner", "expanding plan / setting cycle goal...")
    plan = idea_planner.run()
    _print_status("Planner done", f"cycle_goal: {plan.get('cycle_goal', '?')}")

    _print_status("Prompt Writer", "breaking goal into module specs...")
    specs = prompt_writer.run()
    module_names = [m.get("name", "?") for m in specs.get("modules", [])]
    _print_status("Prompt Writer done", f"modules: {module_names}")

    _print_status("Code Writers", f"writing {len(module_names)} modules in parallel...")
    code_writers.run()
    _print_status("Code Writers done")

    _print_status("Dependency Mapper", "mapping inter-module dependencies...")
    dep_map = dependency_mapper.run()
    _print_status("Dependency Mapper done", f"{len(dep_map)} module(s) mapped")

    _print_status("Test Writer", "generating tests for the new modules...")
    test_code = test_writer.run()
    _print_status("Test Writer done", f"tests written for {len(test_code)} module(s)")

    _print_status("Reviewer", "auditing submitted code (3 parallel reviewers)...")
    review_notes = reviewer.run_reviewer()
    issue_count = len(review_notes.get("issues", []))
    _print_status("Reviewer done", f"{issue_count} issue(s) found")

    _print_status("Duplication Checker", "checking for near-duplicate modules...")
    dup_report = duplication_checker.run()
    _print_status("Duplication Checker done", dup_report.get("summary", ""))

    _print_status("Fixer Pool", "patching issues in parallel...")
    fixed_code = fixer_pool.run_fixer_pool()
    _print_status("Fixer Pool done", f"{len(fixed_code)} module(s) processed")

    _print_status("Sandbox Tester", "running fixed modules in parallel E2B sandboxes...")
    test_results = sandbox_tester.run_sandbox_tester()
    passed = sum(1 for r in test_results.values() if r.get("passed"))
    total = len(test_results)
    _print_status("Sandbox Tester done", f"{passed}/{total} modules passed sandbox run")

    _print_status("Structure Architect", "planning file/folder layout...")
    plan = structure_architect.run_structure_architect()
    _print_status("Structure Architect done", f"{len(plan.get('operations', []))} operation(s) planned")

    _print_status("Security Scanner", "scanning final code (5 parallel workers)...")
    security_results = security_scanner.run()
    finding_count = sum(len(r.get("findings", [])) for r in security_results.values())
    _print_status("Security Scanner done", f"{finding_count} finding(s) across {len(security_results)} module(s)")

    _print_status("File Manager", "executing file plan...")
    fm_summary = file_manager.run_file_manager()
    _print_status("File Manager done",
    f"{len(fm_summary['written'])} written, {len(fm_summary['moved'])} moved, {len(fm_summary['deleted'])} deleted")

    _print_status("Documentation Agent", "updating README...")
    documentation_agent.run()
    _print_status("Documentation Agent done")

    _print_status("Changelog Writer", "writing commit message + changelog entry...")
    changelog = changelog_writer.run()
    _print_status("Changelog Writer done", changelog.get("commit_message", ""))

    _print_status("Report Writer", "summarizing the cycle...")
    report = report_writer.run_report_writer()
    _print_status("Report Writer done", f"all_tests_passed: {report.get('all_tests_passed')}")

    _print_status("Final QA", "final acceptance review...")
    verdict = final_qa.run()
    _print_status("Final QA done", f"accept: {verdict.get('accept')} — {verdict.get('summary', '')}")

    _print_status("Gatekeeper", "deciding whether to continue...")
    decision = gatekeeper.run_gatekeeper(cycle_count=cycle_num)
    _print_status("Gatekeeper decision", decision)

    return decision


def main():
    existing_idea = read(KEYS["original_idea"], default=None)
    if len(sys.argv) > 1:
        idea = " ".join(sys.argv[1:])
        write(KEYS["original_idea"], idea)
        print(f"Starting new run with idea: {idea}")
        cycle_num = 1
        write(KEYS["cycle_count"], cycle_num)
    elif existing_idea:
        cycle_num = read(KEYS["cycle_count"], default=1)
        print(f"Resuming existing run. Idea: {existing_idea}")
        print(f"Resuming from cycle {cycle_num}")
    else:
        print("No idea provided and no existing run found in memory.")
        print('Usage: python loop.py "a one-sentence idea for the app"')
        sys.exit(1)

    while True:
        try:
            decision = run_one_cycle(cycle_num)
        except Exception as exc:
            print("\n  [ERROR] Cycle crashed — stopping the loop, not guessing.")
            print(f"  {type(exc).__name__}: {exc}")
            traceback.print_exc()
            print("\nFix the issue above, then rerun `python loop.py` to resume "
                  "from this cycle (no idea argument needed).")
            sys.exit(1)

        cycle_num += 1
        write(KEYS["cycle_count"], cycle_num)

        if decision == "STOP":
            print(f"\nGatekeeper says STOP after cycle {cycle_num - 1}. Loop ending.")
            break
        if decision == "PAUSE_FOR_HUMAN":
            print(f"\nGatekeeper says PAUSE_FOR_HUMAN after cycle {cycle_num - 1}.")
            print("Review the latest_report in memory, then rerun `python loop.py` "
                  "(no idea argument) to continue from here.")
            break


if __name__ == "__main__":
    main()
