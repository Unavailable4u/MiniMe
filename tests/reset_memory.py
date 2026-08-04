"""
tests/reset_memory.py — clears the memory keys that carry state BETWEEN
cycles of the same app. Run this before starting a genuinely different
idea, or whenever memory looks contaminated (e.g. cycle_goal doesn't match
the idea you passed in -- see inspect_memory.py to check first).

Does NOT touch: the Upstash Vector index (cross-cycle memory/duplication
embeddings) -- those are separate and out of scope here.
Does NOT delete any files on disk under apps/ -- only clears memory keys.

Run: python tests/reset_memory.py
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import redis, KEYS

KEYS_TO_CLEAR = [
    "original_idea", "current_plan", "module_specs", "submitted_code",
    "test_code", "review_notes", "fixed_code", "test_results",
    "commit_message", "changelog_entry", "latest_report", "cycle_count",
    "loop_decision", "feature_status", "file_map", "app_slug",
    "retrieved_context", "dependency_map", "duplication_report",
    "security_scan_results", "doc_output", "final_qa_verdict",
    "file_plan", "last_file_manager_summary",
]


def main():
    print("This will clear the following memory keys:")
    for k in KEYS_TO_CLEAR:
        print(f"  - {KEYS.get(k, k)}")
    confirm = input("\nType 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted, nothing cleared.")
        return

    cleared = 0
    for k in KEYS_TO_CLEAR:
        actual_key = KEYS.get(k, k)
        redis.delete(actual_key)
        cleared += 1

    print(f"\nCleared {cleared} keys. Memory is now empty for a fresh run.")


if __name__ == "__main__":
    main()