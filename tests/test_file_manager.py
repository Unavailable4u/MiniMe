"""
tests/test_file_manager.py — isolates file_manager.py's own write/move/
delete logic, using a hand-written file_plan (no LLM involved at all).
This checks whether file_manager.py itself is correct GIVEN a correct
plan -- i.e. rules out file_manager.py as the bug, separately from
structure_architect.py (see test_structure_architect.py for that half).

Includes one deliberate mismatch (a module name in the plan that does NOT
exist in fixed_code) to confirm the skip path fires with a clear reason,
and prints every skip reason -- file_manager.py currently only writes
skip reasons to memory, not the console, so this test surfaces them
directly instead of you having to infer them from counts.

Run standalone: python tests/test_file_manager.py
This DOES write real files, but only under
apps/__test_file_manager_scratch/ -- safe to delete afterward, and won't
touch any real app directory.
"""
import sys
import os
import json
import shutil

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS
from agents.file_manager import run_file_manager, APPS_ROOT

SCRATCH_SLUG = "__test_file_manager_scratch"

FAKE_FIXED_CODE = {
    "Sandbox Engine": {
        "language": "python",
        "code": "def run_sandboxed(fn, *args):\n    return fn(*args)\n",
    },
    "Callable Validator": {
        "language": "python",
        "code": "def is_safe_callable(fn):\n    return callable(fn)\n",
    },
}

# Hand-written plan: two ops that correctly match fixed_code keys, and one
# deliberately broken one (mismatched module name) to prove the skip path
# reports a clear reason.
FAKE_PLAN = {
    "operations": [
        {"action": "write", "module": "Sandbox Engine",
         "path": "src/sandbox/sandbox_engine.py", "reason": "test"},
        {"action": "write", "module": "Callable Validator",
         "path": "src/validation/callable_validator.py", "reason": "test"},
        {"action": "write", "module": "callable_validator",  # <- deliberate mismatch
         "path": "src/validation/duplicate.py", "reason": "test (should be skipped)"},
    ]
}


def main():
    scratch_dir = os.path.join(APPS_ROOT, SCRATCH_SLUG)
    # start clean
    if os.path.isdir(scratch_dir):
        shutil.rmtree(scratch_dir)

    print("Seeding fake fixed_code + hand-written file_plan into memory...")
    write(KEYS["fixed_code"], FAKE_FIXED_CODE)
    write("file_plan", FAKE_PLAN)
    write(KEYS["app_slug"], SCRATCH_SLUG)
    write(KEYS["file_map"], {})
    write(KEYS["original_idea"], "test scratch app for file_manager isolation")

    print("Running File Manager...")
    summary = run_file_manager()

    print("\n--- summary ---")
    print(json.dumps(summary, indent=2))

    print("\n--- skip reasons (file_manager.py doesn't print these itself) ---")
    for s in summary.get("skipped", []):
        op = s["op"]
        label = op.get("module", op.get("path"))
        print(f"  skipped: {label} — {s['reason']}")

    print("\n--- verdict ---")
    expect_written = 2
    if len(summary["written"]) == expect_written:
        print(f"OK: {expect_written} correctly-matched ops were written as expected.")
    else:
        print(f"UNEXPECTED: expected {expect_written} written, got "
              f"{len(summary['written'])}. file_manager.py's own write logic "
              f"may have a separate bug beyond the module-name-mismatch issue.")

    if len(summary["skipped"]) == 1 and "no code found" in summary["skipped"][0]["reason"]:
        print("OK: the mismatched module name was correctly skipped with a clear reason.")
    else:
        print("UNEXPECTED: skip behavior for the mismatched name didn't match prediction.")

    print(f"\nScratch files written under: {scratch_dir}")
    print("Safe to delete this directory -- it's not a real app.")


if __name__ == "__main__":
    main()