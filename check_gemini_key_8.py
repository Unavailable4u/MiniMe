"""
check_gemini_key_8.py — isolates GEMINI_API_KEY_8 to see exactly why it
404'd in the Patch 5 test, separate from chain fallback logic.

    python check_gemini_key_8.py
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from utils.llm_client import generate_text  # noqa: E402

key = os.getenv("GEMINI_API_KEY_8")
if not key:
    raise SystemExit("GEMINI_API_KEY_8 is not set at all.")
print(f"GEMINI_API_KEY_8 is set, length={len(key)}, starts with '{key[:6]}...'")

CHAIN = [{"provider": "gemini", "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY_8"}]

try:
    result = generate_text(
        system_prompt="You are a terse test assistant.",
        user_content="Reply with exactly the words: gemini key 8 ok",
        chain=CHAIN,
        agent_name="Key8CheckScript",
    )
    print("--- response ---")
    print(result)
except Exception as e:
    print(f"--- FAILED: {type(e).__name__}: {e} ---")
    print(
        "If this says 404/model_not_found, this specific key's project "
        "likely doesn't have gemini-2.5-flash enabled -- check "
        "https://aistudio.google.com/apikey and confirm which project "
        "issued this key. If it says 401/invalid API key, the value in "
        ".env for GEMINI_API_KEY_8 is wrong/incomplete."
    )
