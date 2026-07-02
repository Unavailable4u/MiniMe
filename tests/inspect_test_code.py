"""
One-off inspection script: prints what test_writer actually generated for
shipping_test's shipping.py module, plus the raw sandbox_tester result,
so we can see whether the "passed: true" from the last run means
anything or is a false-positive from tests that were defined but never
executed.

Run from the ai_loop project root, same venv as loop_v4.py:
    python inspect_test_code.py
"""
import sys, os, json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, KEYS

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