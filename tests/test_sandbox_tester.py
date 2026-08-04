import sys
import os
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS
from agents.sandbox_tester import run_sandbox_tester

FAKE_FIXED_CODE = {
    "todo_storage": {
        "language": "python",
        "code": (
            "def add_todo(todos, item):\n"
            "    todos.append(item)\n"
            "    return todos\n\n"
            "todos = []\n"
            "add_todo(todos, 'buy milk')\n"
            "print(todos)\n"
        ),
    },
    "todo_api": {
        "language": "python",
        "code": (
            "def get_todo(todos, index):\n"
            "    return todos[index]\n\n"
            "def delete_all(todos):\n"
            "    todos.clear()\n\n"
            "todos = ['buy milk']\n"
            "delete_all(todos)\n"
            "print('deleted')\n"
        ),
    },
    "broken_module": {
        "language": "python",
        "code": "print(undefined_variable)\n",
    },
}


def main():
    print("Writing fake fixed_code to memory...")
    write(KEYS["fixed_code"], FAKE_FIXED_CODE)

    print("Running Sandbox Tester (spins up parallel E2B sandboxes, may take a moment)...")
    test_results = run_sandbox_tester()

    print("\n--- test_results ---")
    print(json.dumps(test_results, indent=2))

    if test_results.get("todo_storage", {}).get("passed") and \
       test_results.get("todo_api", {}).get("passed") and \
       not test_results.get("broken_module", {}).get("passed"):
        print("\nOK: two clean modules passed, and the deliberately broken one correctly failed.")
    else:
        print("\nWARNING: results didn't match expectations — check stderr/error fields above.")


if __name__ == "__main__":
    main()