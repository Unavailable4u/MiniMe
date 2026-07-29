"""
verify_patch1_gemini.py — run this from your repo root after applying
patch1-wire-gemini-provider.diff and filling in a real GEMINI_API_KEY_1
in your .env.

    cd MiniMe
    python verify_patch1_gemini.py

Expected: prints a short reply from Gemini, plus a confirmation that the
usage_update event fired. If GEMINI_API_KEY_1 is unset or invalid, this
fails loudly with the same error path any real chain step would hit --
that's the point of testing it standalone before it's live in a chain.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from utils.llm_client import generate_text  # noqa: E402

if not os.getenv("GEMINI_API_KEY_1"):
    raise SystemExit(
        "GEMINI_API_KEY_1 is not set in your .env -- fill in a real key from "
        "https://aistudio.google.com/apikey before running this."
    )

CHAIN = [
    {"provider": "gemini", "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY_1"},
]

result = generate_text(
    system_prompt="You are a terse test assistant.",
    user_content="Reply with exactly the words: gemini wiring ok",
    chain=CHAIN,
    agent_name="Patch1VerificationScript",
)

print("--- Gemini response ---")
print(result)
print("------------------------")
print("If that printed real text (not a traceback), Patch 1 is working.")
print("Check your usage dashboard / Upstash for a fresh "
      "usage:gemini:GEMINI_API_KEY_1:<today> entry to confirm logging too.")
