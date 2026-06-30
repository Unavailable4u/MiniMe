"""
loop.py — the orchestrator.

Wires all 7 agents into one running cycle, per Section 3 / Section 8 of the
blueprint:

    Idea Planner -> Prompt Writer -> Code Writers (x3) -> Reviewer
        -> Fixer/Tester -> Report Writer -> Gatekeeper -> (loop or stop)

Every agent module exposes a no-argument run function and reads/writes
everything through memory.bus (Upstash Redis), so this file's only job is
sequencing, cycle bookkeeping, and turning the Gatekeeper's verdict into
control flow. No business logic lives here.

Usage:
    python loop.py "a one-sentence idea for the app"
    python loop.py            (resumes/continues an existing run from memory)
"""

import sys
import traceback

from memory.bus import read, write, KEYS

from agents import idea_planner
from agents import prompt_writer
from agents import code_writers
from agents import reviewer
from agents import fixer_tester
from agents import report_writer
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

    _print_status("Reviewer", "auditing submitted code...")
    review_notes = reviewer.run_reviewer()
    issue_count = len(review_notes.get("issues", []))
    _print_status("Reviewer done", f"{issue_count} issue(s) found")

    _print_status("Fixer + Tester", "patching issues, running sandbox tests...")
    _, test_results = fixer_tester.run_fixer_and_tester()
    passed = sum(1 for r in test_results.values() if r.get("passed"))
    total = len(test_results)
    _print_status("Fixer + Tester done", f"{passed}/{total} modules passed sandbox run")

    _print_status("Report Writer", "summarizing the cycle...")
    report = report_writer.run_report_writer()
    _print_status("Report Writer done", f"all_tests_passed: {report.get('all_tests_passed')}")

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

        if decision == "STOP":
            print(f"\nGatekeeper says STOP after cycle {cycle_num}. Loop ending.")
            break

        if decision == "PAUSE_FOR_HUMAN":
            print(f"\nGatekeeper says PAUSE_FOR_HUMAN after cycle {cycle_num}.")
            print("Review the latest_report in memory, then rerun `python loop.py` "
                  "(no idea argument) to continue from here.")
            break

        cycle_num += 1
        write(KEYS["cycle_count"], cycle_num)


if __name__ == "__main__":
    main()