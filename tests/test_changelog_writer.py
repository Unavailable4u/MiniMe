import sys
import os
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS
from agents.changelog_writer import run

FAKE_PLAN = {"cycle_goal": "add input validation to the task creation form"}
FAKE_FM_SUMMARY = {
    "written": ["src/api/task_creation_api.py", "src/todo/task_validator.py"],
    "moved": [],
    "deleted": [],
}
FAKE_TEST_RESULTS = {
    "task_creation_api": {"passed": True},
    "task_validator": {"passed": True},
}


def main():
    print("Writing fake cycle data to memory...")
    write(KEYS["current_plan"], FAKE_PLAN)
    write("last_file_manager_summary", FAKE_FM_SUMMARY)
    write(KEYS["test_results"], FAKE_TEST_RESULTS)
    write(KEYS["cycle_count"], 3)

    print("Running Changelog Writer...")
    result = run()

    print("\n--- result ---")
    print(json.dumps(result, indent=2))

    if result.get("commit_message") and len(result["commit_message"]) <= 72:
        print("\nOK: got a commit message within length limits.")
    else:
        print("\nWARNING: commit_message missing or too long — check output above.")


if __name__ == "__main__":
    main()
