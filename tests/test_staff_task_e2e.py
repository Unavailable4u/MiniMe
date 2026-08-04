import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo.panel import staff_task
from eo.registry import get_role_prompt, ROLE_PROMPTS_KEY
from memory.bus import read, write

NOVEL_ROLE = "changelog_summarizer_test_role"  # not implementer/verifier/researcher/writer/fact_checker

# Clean slate for this one role only.
existing = read(ROLE_PROMPTS_KEY, default={})
if NOVEL_ROLE in existing:
    del existing[NOVEL_ROLE]
    write(ROLE_PROMPTS_KEY, existing)
    print(f"Removed pre-existing '{NOVEL_ROLE}' entry before testing.")

print(f"Confirmed not in registry yet: {get_role_prompt(NOVEL_ROLE)}\n")

fake_classification = {
    "tier": 2,
    "directed_task_type": "write_docs",
    "confidence": 0.8,
    "suggested_agents": [NOVEL_ROLE],
    "reasoning": "task needs a role we've never seen before",
}

task_text = "Summarize the last 10 commits into a punchy one-paragraph changelog entry."

hires = staff_task(fake_classification, task_text=task_text, session_id=None)
print("hires returned by staff_task():")
for h in hires:
    print(" ", h)

assert len(hires) == 1, f"expected exactly 1 hire, got {len(hires)}"
assert hires[0]["role"] == NOVEL_ROLE

stored_brief = get_role_prompt(NOVEL_ROLE)
print(f"\nBrief now in registry for '{NOVEL_ROLE}': {stored_brief}")

assert stored_brief == hires[0]["brief"]
print("\nPASS: novel role got hired, a real brief was written, and it's now persisted in the registry.")