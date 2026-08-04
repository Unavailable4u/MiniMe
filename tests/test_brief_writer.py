import sys
import os
import time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo.panel import _get_or_write_role_prompt
from eo.registry import get_role_prompt, add_role_prompt, ROLE_PROMPTS_KEY
from memory.bus import read

TEST_ROLE = "diagram_designer_test_role"  # deliberately not in ROLE_PROMPTS_SEED

# Clean slate for this one role only (don't touch any other roles already
# in the registry).
existing = read(ROLE_PROMPTS_KEY, default={})
if TEST_ROLE in existing:
    del existing[TEST_ROLE]
    from memory.bus import write
    write(ROLE_PROMPTS_KEY, existing)
    print(f"Removed pre-existing '{TEST_ROLE}' entry before testing.")

print(f"\nConfirmed not in registry yet: {get_role_prompt(TEST_ROLE)}")

print("\n--- Call 1 (expect: slow path, LLM call happens) ---")
t0 = time.time()
brief1 = _get_or_write_role_prompt(
    TEST_ROLE,
    task_text="Design a clear architecture diagram showing how the EO Panel, "
              "Inspector, and Registry interact.",
)
t1 = time.time()
print(f"Brief 1: {brief1}")
print(f"Took {t1 - t0:.2f}s")

print("\n--- Call 2 (expect: fast path, no LLM call) ---")
t2 = time.time()
brief2 = _get_or_write_role_prompt(TEST_ROLE, task_text="irrelevant on the fast path")
t3 = time.time()
print(f"Brief 2: {brief2}")
print(f"Took {t3 - t2:.2f}s")

assert brief1 == brief2, "Fast path returned a DIFFERENT brief than the slow path wrote — bug!"
print(f"\nPASS: same brief both times. Call 1 took {t1-t0:.2f}s, call 2 took {t3-t2:.2f}s "
      f"(call 2 should be dramatically faster — no network round-trip to an LLM provider).")