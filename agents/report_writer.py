import os
import sys
import json
from dotenv import load_dotenv
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry
from utils.gemini_client import generate_text
load_dotenv()

SYSTEM_PROMPT = """You are a report writer for an autonomous coding pipeline.
Summarize this cycle in under 200 words for the next planner. Cover: what got
built, what's still broken, what should be prioritized next cycle. Be honest
about failures. Write in plain language, not JSON -- this is read by a human
and by the next planning agent as plain text.
"""


def run_report_writer():
    fixed_code = read(KEYS["fixed_code"])
    test_results = read(KEYS["test_results"])
    review_notes = read(KEYS["review_notes"])
    current_plan = read(KEYS["current_plan"], default={})
    if not fixed_code or not test_results:
        raise ValueError("Missing fixed_code or test_results in memory. Run the Fixer+Tester first.")

    user_prompt = (
        "Review notes from this cycle:\n" + json.dumps(review_notes, indent=2)
        + "\n\nFixed code modules (names only, not full code, to keep this short):\n"
        + json.dumps(list(fixed_code.keys()))
        + "\n\nSandbox test results:\n" + json.dumps(test_results, indent=2)
    )

    report_text = call_with_retry(
        lambda: generate_text(SYSTEM_PROMPT, user_prompt, agent_name="Report Writer"),
        agent_name="Report Writer",
    )

    failed_modules = [
        name for name, result in test_results.items()
        if not result.get("passed", False)
    ]
    all_passed = len(failed_modules) == 0

    target_feature = current_plan.get("target_feature")
    if target_feature:
        feature_status = read(KEYS["feature_status"], default={})
        feature_status[target_feature] = "done" if all_passed else "in_progress"
        write(KEYS["feature_status"], feature_status)

    report_record = {
        "text": report_text,
        "all_tests_passed": all_passed,
        "failed_modules": failed_modules,
        "target_feature": target_feature,
    }
    write(KEYS["latest_report"], report_record)
    return report_record


if __name__ == "__main__":
    report = run_report_writer()
    print(json.dumps(report, indent=2))