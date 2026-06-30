from memory.bus import write, KEYS
from agents.reviewer import run_reviewer

FAKE_SUBMITTED_CODE = {
    "todo_storage": {
        "language": "python",
        "code": (
            "def add_todo(todos, item):\n"
            "    todos.append(item)\n"
            "    return todos\n\n"
            "def remove_todo(todos, index):\n"
            "    todos.pop(index)\n"
            "    return todos\n"
        ),
    },
    "todo_api": {
        "language": "python",
        "code": (
            "# deliberately buggy: no bounds check, uses undefined variable\n"
            "def get_todo(todos, index):\n"
            "    return todos[index]\n\n"
            "def delete_all(todos):\n"
            "    global storage\n"
            "    storage.clear()\n"
        ),
    },
}


def main():
    print("Writing fake submitted_code to memory...")
    write(KEYS["submitted_code"], FAKE_SUBMITTED_CODE)

    print("Running Reviewer agent...")
    notes = run_reviewer()

    print("\n--- review_notes ---")
    print(notes)

    issues = notes.get("issues", [])
    if not issues:
        print("\nWARNING: Reviewer found zero issues on code that has an obvious bug. Check the prompt.")
    else:
        print(f"\nOK: Reviewer flagged {len(issues)} issue(s) as expected.")


if __name__ == "__main__":
    main()