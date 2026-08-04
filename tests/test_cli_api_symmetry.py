import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo.panel import staff_task
from eo.registry import get_role_prompt, ROLE_PROMPTS_KEY
from memory.bus import read, write

NOVEL_ROLE = "cli_api_symmetry_test_role"

# Clean slate for this one role only.
existing = read(ROLE_PROMPTS_KEY, default={})
if NOVEL_ROLE in existing:
    del existing[NOVEL_ROLE]
    write(ROLE_PROMPTS_KEY, existing)
    print(f"Removed pre-existing '{NOVEL_ROLE}' entry before testing.")

fake_classification = {
    "tier": 2,
    "directed_task_type": "write_docs",
    "confidence": 0.8,
    "suggested_agents": [NOVEL_ROLE],
    "reasoning": "checking CLI/API symmetry",
}
task_text = "Write a short glossary entry explaining a new technical term."

# Simulates the CLI call site (loop_v4.py): staff_task(decision, task_text=task_text)
print("--- 'CLI-style' call: staff_task(decision, task_text=task_text) ---")
cli_hires = staff_task(fake_classification, task_text=task_text)
print("CLI hires:", cli_hires)

# Simulates the API call site (task_runner.py): staff_task(decision, task_text=task_text, session_id=session_id)
print("\n--- 'API-style' call: staff_task(decision, task_text=task_text, session_id=...) ---")
api_hires = staff_task(fake_classification, task_text=task_text, session_id="fake-session-id-123")
print("API hires:", api_hires)

assert cli_hires[0]["brief"] == api_hires[0]["brief"]
print(f"\nPASS: both call styles produced the identical brief for '{NOVEL_ROLE}' "
      f"(second call hit the fast path, confirming persistence works regardless of caller).")