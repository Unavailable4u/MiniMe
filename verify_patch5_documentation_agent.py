"""
verify_patch5_documentation_agent.py — run this from your repo root after
applying patch5-documentation-agent-fallback-chain.diff and filling in
real GEMINI_API_KEY_8 and MISTRAL_API_KEY_5 values in your .env.

    cd MiniMe
    python verify_patch5_documentation_agent.py

Same forced-failure pattern as verify_patch4: pops MISTRAL_API_KEY out of
os.environ AFTER importing agents.documentation_agent (that import
triggers the module's own load_dotenv() call, same trap Patch 4's first
verify script hit -- popping before the import would just get silently
undone), so the primary account is guaranteed unavailable, then confirms
the CHAIN still produces a real response via GEMINI_API_KEY_8.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

missing = [k for k in ("GEMINI_API_KEY_8", "MISTRAL_API_KEY_5") if not os.getenv(k)]
if missing:
    raise SystemExit(
        "These Patch-5 keys are not set in your .env yet, fill in at least "
        f"one before running this: {', '.join(missing)}"
    )

from utils.llm_client import generate_text  # noqa: E402
from agents.documentation_agent import CHAIN, SYSTEM_PROMPT  # noqa: E402

# After the imports above (see docstring) -- this is what actually
# simulates MISTRAL_API_KEY being down for this run. In-process only,
# your real .env file is untouched.
os.environ.pop("MISTRAL_API_KEY", None)

print("--- CHAIN as documentation_agent.py will actually use it ---")
for step in CHAIN:
    print(" ", step["provider"], step["key_env"])

result = generate_text(
    system_prompt=SYSTEM_PROMPT,
    user_content=(
        '{"idea": "a todo app", "feature_status": {"add_task": "done"}, '
        '"this_cycle_summary": "added the add_task feature", "file_map": {}}'
    ),
    chain=CHAIN,
    agent_name="Patch5VerificationScript",
)

print("\n--- documentation_agent response (with MISTRAL_API_KEY forced unavailable) ---")
print(result)
print("------------------------")
print(
    "If that's real JSON (not a traceback), Patch 5's fallback worked -- "
    "the call had to skip MISTRAL_API_KEY (unset for this run) and land "
    "on GEMINI_API_KEY_8."
)
print("Check your usage dashboard / Upstash for a fresh "
      "usage:gemini:GEMINI_API_KEY_8:<today> entry to confirm.")
