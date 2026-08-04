"""
tests/test_structure_architect.py — isolates structure_architect.py to check
whether it echoes back module names EXACTLY as given, or "normalizes" them
(e.g. "Sandbox Engine" -> "sandbox_engine") in the "module" field of its
plan. If it normalizes, file_manager.py's exact-match lookup against
fixed_code will silently skip every write -- which is the bug this test
is built to catch.

Run standalone: python tests/test_structure_architect.py
Does NOT touch the real filesystem (uses a throwaway app_slug), and does
NOT run the rest of the loop.
"""
import sys
import os
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, read, KEYS
from agents.structure_architect import run_structure_architect, FILE_PLAN_KEY

# Deliberately use spaced, title-case module names -- this matches what
# Prompt Writer produced in the failing runs ("Sandbox Engine",
# "Callable Validator", "Interface Adapter"), as opposed to the
# PascalCase-no-spaces names ("GameBoardModel") that worked better.
FAKE_FIXED_CODE = {
    "Sandbox Engine": {
        "language": "python",
        "code": "def run_sandboxed(fn, *args):\n    return fn(*args)\n",
    },
    "Callable Validator": {
        "language": "python",
        "code": "def is_safe_callable(fn):\n    return callable(fn)\n",
    },
    "Interface Adapter": {
        "language": "python",
        "code": "def adapt(obj):\n    return obj\n",
    },
}


def main():
    print("Seeding fake fixed_code (spaced module names) into memory...")
    write(KEYS["fixed_code"], FAKE_FIXED_CODE)
    # throwaway slug so this never collides with / touches a real app dir
    write(KEYS["app_slug"], "__test_structure_architect_scratch")
    write(KEYS["file_map"], {})

    print("Running Structure Architect...")
    plan = run_structure_architect()

    print("\n--- plan ---")
    print(json.dumps(plan, indent=2))

    valid_names = set(FAKE_FIXED_CODE.keys())
    ops = plan.get("operations", [])
    mismatches = []
    covered = set()

    for op in ops:
        m = op.get("module")
        if m is None:
            continue
        covered.add(m)
        if m not in valid_names:
            mismatches.append(m)

    missing = valid_names - covered

    print("\n--- verdict ---")
    if mismatches:
        print(f"BUG CONFIRMED: {len(mismatches)} module name(s) in the plan "
              f"don't exactly match fixed_code keys: {mismatches}")
        print("This is why file_manager.py silently skips these writes --")
        print("_get_module_code() does an exact dict lookup and finds nothing.")
    else:
        print("OK: every 'module' field in the plan matches a fixed_code key exactly.")

    if missing:
        print(f"WARNING: {len(missing)} module(s) never appeared in any operation "
              f"at all: {missing}")


if __name__ == "__main__":
    main()