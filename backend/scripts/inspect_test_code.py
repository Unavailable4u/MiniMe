"""
One-off inspection script: prints what test_writer actually generated for
shipping_test's shipping.py module, plus the raw sandbox_tester result,
so we can see whether the "passed: true" from the last run means
anything or is a false-positive from tests that were defined but never
executed.

Moved from tests/ to scripts/ (B1 reorg) -- this is a debug/inspection
script, not a test (no assertions), so it was never collected by
pytest.ini's testpaths anyway. Kept alongside scripts/inspect_memory.py,
same category of tool.

Run from the project root, same venv as eo/loop_v4.py:
    python scripts/inspect_test_code.py
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import KEYS, read

app_slug = read(KEYS["app_slug"])
print(f"active app_slug: {app_slug!r}\n")

test_code = read(KEYS["test_code"], default={})
print("=== test_code (what Test Writer generated) ===")
if not test_code:
    print("  (empty -- Test Writer produced nothing, or wrote under a different key)")
else:
    for module_name, code in test_code.items():
        print(f"--- {module_name} ---")
        print(code)
        print()

test_results = read(KEYS["test_results"], default={})
print("=== test_results (what Sandbox Tester reported) ===")
print(json.dumps(test_results, indent=2))