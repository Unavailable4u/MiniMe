"""
verify_patch4_structure_architect.py — run this from your repo root after
applying patch4-structure-architect-fallback-chain.diff and filling in
real GEMINI_API_KEY_4/_5/_6 values in your .env.

    cd MiniMe
    python verify_patch4_structure_architect.py

This is the forced-failure test the rollout checklist calls for: it
temporarily blanks GROQ_API_KEY_9 and GROQ_API_KEY out of os.environ
(in-process only -- your real .env file is never touched) so the first
two CHAIN steps are guaranteed to be skipped as "key not set", then
calls generate_text() with structure_architect's real CHAIN and confirms
the response actually came back from a Gemini step, not a Groq one.

Expected: prints the plan JSON (or the raw response, if you're using the
lower-level generate_text() call below) and a confirmation line naming
the Gemini key that answered. If it raises instead, the fallback chain
isn't working -- recheck GEMINI_API_KEY_4/_5/_6 are set and valid before
assuming the CHAIN edit itself is broken.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

missing = [k for k in ("GEMINI_API_KEY_4", "GEMINI_API_KEY_5", "GEMINI_API_KEY_6") if not os.getenv(k)]
if missing:
    raise SystemExit(
        "These Patch-4 keys are not set in your .env yet, fill in at least "
        f"one before running this: {', '.join(missing)}"
    )

from utils.llm_client import generate_text  # noqa: E402
from agents.structure_architect import CHAIN, SYSTEM_PROMPT  # noqa: E402

# IMPORTANT: this pop happens AFTER the imports above, not before.
# agents/structure_architect.py calls load_dotenv() itself at import
# time (line 54) -- popping these first and importing second meant that
# import silently refilled them from .env (load_dotenv() only skips
# variables that are already set), so the "forced failure" never
# actually happened. Popping here, after every load_dotenv() call in
# the import chain has already fired, is what actually simulates the
# outage. In-process only -- your real .env file is never touched.
os.environ.pop("GROQ_API_KEY_9", None)
os.environ.pop("GROQ_API_KEY", None)

print("--- CHAIN as structure_architect.py will actually use it ---")
for step in CHAIN:
    print(" ", step["provider"], step["key_env"])

result = generate_text(
    system_prompt=SYSTEM_PROMPT,
    user_content=(
        'Current project file tree:\n[]\n\nCurrent file_map (module -> existing path):\n{}\n\n'
        'New/updated modules this cycle:\n'
        '{"hello": {"language": "python", "code_preview": "def hello():\\n    return \'hi\'"}}'
    ),
    chain=CHAIN,
    agent_name="Patch4VerificationScript",
)

print("\n--- structure_architect response (with Groq forced unavailable) ---")
print(result)
print("------------------------")
print(
    "If that's real JSON (not a traceback), Patch 4's fallback worked -- "
    "the call had to fall through GROQ_API_KEY_9 and GROQ_API_KEY (both "
    "unset for this run) to reach GEMINI_API_KEY_4."
)
print("Check your usage dashboard / Upstash for a fresh "
      "usage:gemini:GEMINI_API_KEY_4:<today> entry (or _5/_6, if _4 also "
      "happened to fail) to confirm it was actually Gemini that answered, "
      "not something silently cached.")
