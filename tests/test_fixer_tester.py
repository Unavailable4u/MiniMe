import json
from memory.bus import write, KEYS
from agents.fixer_tester import run_fixer_and_tester

FAKE_SUBMITTED_CODE = {
    "todo_storage": {
        "language": "python",
        "code": (
            "def add_todo(todos, item):\n"
            "    todos.append(item)\n"
            "    return todos\n\n"
            "def remove_todo(todos, index):\n"
            "    todos.pop(index)\n"
            "    return todos\n\n"
            "todos = []\n"
            "add_todo(todos, 'buy milk')\n"
            "print(todos)\n"
        ),
    },
    "todo_api": {
        "language": "python",
        "code": (
            "# deliberately buggy: undefined global variable\n"
            "def get_todo(todos, index):\n"
            "    return todos[index]\n\n"
            "def delete_all(todos):\n"
            "    global storage\n"
            "    storage.clear()\n\n"
            "todos = ['buy milk']\n"
            "delete_all(todos)\n"
            "print('deleted')\n"
        ),
    },
}

FAKE_REVIEW_NOTES = {
    "issues": [
        {
            "module": "todo_api",
            "severity": "critical",
            "description": "delete_all references an undefined global 'storage' instead of the passed 'todos' list, causing a NameError.",
        },
        {
            "module": "todo_storage",
            "severity": "moderate",
            "description": "remove_todo does not bounds-check the index before calling pop().",
        },
    ],
    "summary": "One critical NameError bug, one moderate missing bounds check.",
}


def main():
    print("Writing fake submitted_code and review_notes to memory...")
    write(KEYS["submitted_code"], FAKE_SUBMITTED_CODE)
    write(KEYS["review_notes"], FAKE_REVIEW_NOTES)

    print("Running Fixer + Tester agent (this calls Cerebras then E2B, may take a moment)...")
    fixed_code, test_results = run_fixer_and_tester()

    print("\n--- fixed_code ---")
    print(json.dumps(fixed_code, indent=2))

    print("\n--- test_results ---")
    print(json.dumps(test_results, indent=2))

    todo_api_result = test_results.get("todo_api", {})
    if todo_api_result.get("passed"):
        print("\nOK: todo_api ran without error after fixing — the NameError bug appears resolved.")
    else:
        print("\nWARNING: todo_api still failed in the sandbox after the fix attempt. "
              "Check stderr above before trusting this agent on real cycles.")


if __name__ == "__main__":
    main()