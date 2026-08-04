import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read, write, redis, _namespaced
from eo.registry import ROLE_PROMPTS_KEY

# ---- 1. Remove the stray top-level key from Test 1 ----
raw = redis.get(_namespaced("registry:test"))
if raw is not None:
    redis.delete(_namespaced("registry:test"))
    print("Deleted stray 'registry:test' key.")
else:
    print("'registry:test' key already absent, nothing to delete.")

# ---- 2. Remove test-only role entries from registry:role_prompts ----
TEST_ROLES = [
    "diagram_designer_test_role",
    "changelog_summarizer_test_role",
    "cli_api_symmetry_test_role",
]

prompts = read(ROLE_PROMPTS_KEY, default={})
removed = []
for role in TEST_ROLES:
    if role in prompts:
        del prompts[role]
        removed.append(role)

if removed:
    write(ROLE_PROMPTS_KEY, prompts)
    print(f"Removed test role entries: {removed}")
else:
    print("No test role entries found in registry:role_prompts, nothing to remove.")

print(f"\nRemaining roles in registry:role_prompts: {sorted(prompts.keys())}")

# ---- 3. Reset app_slug ----
# Edit REAL_APP_SLUG below to your actual working project's slug, then
# re-run, OR just check what it currently is first without changing it.
REAL_APP_SLUG = None  # <-- set this to a string and re-run to actually reset it

current = read("app_slug")
print(f"\nCurrent app_slug: {current!r}")
if REAL_APP_SLUG is not None:
    write("app_slug", REAL_APP_SLUG)
    print(f"app_slug reset to: {REAL_APP_SLUG!r}")
else:
    print("REAL_APP_SLUG not set in this script — app_slug left unchanged. "
          "Edit the script and set REAL_APP_SLUG to reset it.")