"""
verify_patch2_huggingface.py — run this from your repo root after applying
patch1-wire-gemini-provider.diff AND patch2-wire-huggingface-provider.diff
(patch2 applies on top of patch1 -- apply both, in order), with a real
HUGGINGFACE_API_KEY_2 filled in.

    cd MiniMe
    python verify_patch2_huggingface.py

Expected: prints a short reply routed through HF's Inference Providers
router, plus a note to check the usage dashboard.

If "openai/gpt-oss-120b:fastest" 404s (HF's live provider roster shifts),
check GET https://router.huggingface.co/v1/models with your token and
swap MODEL below to whatever's currently live -- that's expected
maintenance, not a bug in the wiring itself.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from utils.llm_client import generate_text  # noqa: E402

KEY_ENV = "HUGGINGFACE_API_KEY_2"
MODEL = "openai/gpt-oss-120b:fastest"

if not os.getenv(KEY_ENV):
    raise SystemExit(
        f"{KEY_ENV} is not set in your .env -- fill in a real Fine-grained/"
        f"Inference-preset token from https://huggingface.co/settings/tokens "
        f"before running this."
    )

CHAIN = [
    {"provider": "huggingface", "model": MODEL, "key_env": KEY_ENV},
]

result = generate_text(
    system_prompt="You are a terse test assistant.",
    user_content="Reply with exactly the words: huggingface wiring ok",
    chain=CHAIN,
    agent_name="Patch2VerificationScript",
)

print("--- Hugging Face response ---")
print(result)
print("------------------------------")
print("If that printed real text (not a traceback), Patch 2 is working.")
print(f"Check your usage dashboard / Upstash for a fresh "
      f"usage:huggingface:{KEY_ENV}:<today> entry to confirm logging too.")
