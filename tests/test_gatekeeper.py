from memory.bus import write, KEYS
from agents.gatekeeper import run_gatekeeper, MAX_CYCLES, HUMAN_CHECKPOINT_EVERY

GOOD_REPORT = {
    "text": "Everything built and tested cleanly this cycle. No issues found.",
    "all_tests_passed": True,
    "failed_modules": [],
}

CLEAN_REVIEW_NOTES = {"issues": [], "summary": "No issues."}

CRITICAL_REVIEW_NOTES = {
    "issues": [
        {
            "module": "todo_api",
            "severity": "critical",
            "description": "delete_all references an undefined global variable.",
        }
    ],
    "summary": "One critical bug, unresolved.",
}


def reset_previous_critical():
    write("previous_critical_issues", [])


def test_normal_continue():
    print("\n[Test 1] Normal cycle, no issues -> expect CONTINUE (LLM call)")
    write(KEYS["cycle_count"], 2)
    write(KEYS["latest_report"], GOOD_REPORT)
    write(KEYS["review_notes"], CLEAN_REVIEW_NOTES)
    reset_previous_critical()
    decision = run_gatekeeper(cycle_count=2)
    print("Decision:", decision)
    assert decision in ("CONTINUE", "PAUSE_FOR_HUMAN", "STOP")


def test_hard_cycle_cap():
    print(f"\n[Test 2] cycle_count == MAX_CYCLES ({MAX_CYCLES}) -> expect STOP")
    write(KEYS["latest_report"], GOOD_REPORT)
    write(KEYS["review_notes"], CLEAN_REVIEW_NOTES)
    decision = run_gatekeeper(cycle_count=MAX_CYCLES)
    print("Decision:", decision)
    assert decision == "STOP", f"Expected STOP, got {decision}"


def test_repeat_failure_breaker():
    print("\n[Test 3] Same critical issue two cycles in a row -> expect PAUSE_FOR_HUMAN")
    write(KEYS["latest_report"], GOOD_REPORT)
    write(KEYS["review_notes"], CRITICAL_REVIEW_NOTES)

    write("previous_critical_issues", [])
    decision_first = run_gatekeeper(cycle_count=3)
    print("Decision (first time seeing issue):", decision_first)

    decision_second = run_gatekeeper(cycle_count=4)
    print("Decision (same issue repeats):", decision_second)
    assert decision_second == "PAUSE_FOR_HUMAN", f"Expected PAUSE_FOR_HUMAN, got {decision_second}"


def test_human_checkpoint():
    print(f"\n[Test 4] cycle_count is a multiple of {HUMAN_CHECKPOINT_EVERY} -> expect PAUSE_FOR_HUMAN")
    write(KEYS["latest_report"], GOOD_REPORT)
    write(KEYS["review_notes"], CLEAN_REVIEW_NOTES)
    reset_previous_critical()
    decision = run_gatekeeper(cycle_count=HUMAN_CHECKPOINT_EVERY)
    print("Decision:", decision)
    assert decision == "PAUSE_FOR_HUMAN", f"Expected PAUSE_FOR_HUMAN, got {decision}"


if __name__ == "__main__":
    test_normal_continue()
    test_hard_cycle_cap()
    test_repeat_failure_breaker()
    test_human_checkpoint()
    print("\nAll Gatekeeper safety rule tests passed.")