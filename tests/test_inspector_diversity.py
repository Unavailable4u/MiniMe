import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo.inspector import classify

TASKS = [
    "Write a Python function that reverses a linked list.",
    "Research the pros and cons of three different caching strategies and summarize them.",
    "Review this pull request for style and correctness issues.",
    "Fact-check this paragraph about the history of the printing press.",
    "Design a simple flowchart showing our user signup process.",
]

all_agents_seen = set()

for task in TASKS:
    result = classify(task)
    agents = result.get("suggested_agents", [])
    all_agents_seen.update(agents)
    print(f"Task: {task}")
    print(f"  tier={result['tier']} suggested_agents={agents}")
    print()

print(f"Distinct suggested_agents labels seen across all {len(TASKS)} tasks: {sorted(all_agents_seen)}")

if all_agents_seen == {"responder"}:
    print("\nFAIL (or at least concerning): every single task only ever got 'responder'. "
          "Prompt update may not be taking effect.")
else:
    print(f"\nPASS: Inspector produced {len(all_agents_seen)} distinct role label(s), "
          f"not just 'responder' every time.")