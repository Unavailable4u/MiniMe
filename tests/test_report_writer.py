import json
from memory.bus import write, KEYS
from agents.report_writer import run_report_writer

FAKE_FIXED_CODE = {
    "todo_storage": {
        "language": "python",
        "code": "def add_todo(todos, item):\n    todos.append(item)\n    return todos\n",
    },
    "todo_api": {
        "language": "python",
        "code": "def get_todo(todos, index):\n    return todos[index]\n",
    },
}

FAKE_TEST_RESULTS = {
    "todo_storage": {"passed": True, "stdout": ["['buy milk']\n"], "stderr": [], "error": None},
    "todo_api": {"passed": True, "stdout": ["deleted\n"], "stderr": [], "error": None},
}

FAKE_REVIEW_NOTES = {
    "issues": [
        {
            "module": "todo_api",
            "severity": "critical",
            "description": "delete_all referenced an undefined global instead of the passed list. Fixed by the Fixer agent.",
        }
    ],
    "summary": "One critical bug found and resolved this cycle.",
}


def main():
    print("Writing fake fixed_code, test_results, and review_notes to memory...")
    write(KEYS["fixed_code"], FAKE_FIXED_CODE)
    write(KEYS["test_results"], FAKE_TEST_RESULTS)
    write(KEYS["review_notes"], FAKE_REVIEW_NOTES)

    print("Running Report Writer agent...")
    report = run_report_writer()

    print("\n--- latest_report ---")
    print(json.dumps(report, indent=2))

    if report.get("all_tests_passed"):
        print("\nOK: Report correctly marked this cycle as fully passing.")
    else:
        print(f"\nNOTE: Report flagged failed modules: {report.get('failed_modules')}")


if __name__ == "__main__":
    main()