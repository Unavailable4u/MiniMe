import sys
import os
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS
from agents.fixer_pool import run_fixer_pool

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

    print("Running Fixer Pool (this calls Cerebras across up to 3 parallel workers)...")
    fixed_code = run_fixer_pool()

    print("\n--- fixed_code ---")
    print(json.dumps(fixed_code, indent=2))

    todo_api_fix = fixed_code.get("todo_api", {}).get("code", "")
    if "global storage" not in todo_api_fix and "storage.clear" not in todo_api_fix:
        print("\nOK: todo_api no longer calls the undefined 'storage' global.")
    else:
        print("\nWARNING: todo_api still references the buggy 'storage' global — the fix may not have applied.")


if __name__ == "__main__":
    main()