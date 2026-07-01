"""
tests/inspect_memory.py — read-only peek at the current state of shared
memory. Doesn't run any agent, doesn't touch the filesystem. Use this
whenever something looks inconsistent (e.g. cycle_goal doesn't match the
idea you passed in) to see what's actually stored right now.

Run: python tests/inspect_memory.py
"""
import sys
import os
import json


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, KEYS

KEYS_TO_CHECK = [
    "original_idea",
    "app_slug",
    "current_plan",
    "file_map",
    "fixed_code",
    "file_plan",  # not in KEYS dict, but used directly by structure_architect/file_manager
]

def main():
    print("--- current memory state ---\n")
    for key_name in KEYS_TO_CHECK:
        actual_key = KEYS.get(key_name, key_name)
        value = read(actual_key, default="<not set>")
        if isinstance(value, (dict, list)) and value != "<not set>":
            preview = json.dumps(value, indent=2)
            if len(preview) > 500:
                preview = preview[:500] + "\n... (truncated)"
        else:
            preview = value
        print(f"[{key_name}] =")
        print(preview)
        print()

if __name__ == "__main__":
    main()