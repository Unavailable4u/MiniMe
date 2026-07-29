"""
verify_patch5b_gemini_model_fix.py — run this after applying
patch5b-gemini-model-fix.diff.

    python verify_patch5b_gemini_model_fix.py

Re-runs the same standalone GEMINI_API_KEY_8 check that surfaced the
original gemini-2.5-flash 404, but now against gemini-3.6-flash, plus a
quick sanity check that PROVIDER_DEFAULT_MODEL["gemini"] was actually
updated (not just the two hardcoded CHAIN entries in structure_architect
/documentation_agent). If this still 404s, gemini-3.6-flash may have
been retired/restricted too by the time you're reading this -- check
https://ai.google.dev/gemini-api/docs/models for the current stable
model ID and re-run patch5b's sed-equivalent edits with that instead.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

# IMPORTANT: eo/registry.py imported first, deliberately. There's a
# pre-existing circular import in this codebase --
# agents/generic_worker.py's own "from eo.registry import
# get_role_prompt, AGENT_CAPABILITIES" (line 50) -- that only surfaces
# if something imports agents.generic_worker as the very FIRST touch of
# this module cluster: agents.generic_worker starts loading, hits its
# own eo.registry import, which pulls in agents/reviewer.py, which
# needs NEXT_TAG_INSTRUCTION from agents.generic_worker -- but that name
# isn't defined yet because generic_worker.py is still mid-import
# (stuck at line 50). Nothing in the real app hits this because normal
# entry points import eo.registry (or something that imports it) before
# ever importing agents.generic_worker directly. Importing eo.registry
# here first sidesteps it the same way; not a codebase bug this patch
# needs to fix, just an import-order quirk of testing this one module
# standalone.
import eo.registry  # noqa: E402,F401

from agents.generic_worker import PROVIDER_DEFAULT_MODEL  # noqa: E402
from utils.llm_client import generate_text  # noqa: E402

print("PROVIDER_DEFAULT_MODEL['gemini'] =", PROVIDER_DEFAULT_MODEL.get("gemini"))
assert PROVIDER_DEFAULT_MODEL.get("gemini") != "gemini-2.5-flash", (
    "still pointing at the deprecated-for-new-users model -- patch5b didn't apply?"
)

key = os.getenv("GEMINI_API_KEY_8")
if not key:
    raise SystemExit("GEMINI_API_KEY_8 is not set.")

CHAIN = [{"provider": "gemini", "model": PROVIDER_DEFAULT_MODEL["gemini"], "key_env": "GEMINI_API_KEY_8"}]

try:
    result = generate_text(
        system_prompt="You are a terse test assistant.",
        user_content="Reply with exactly the words: gemini model fix ok",
        chain=CHAIN,
        agent_name="Patch5bVerificationScript",
    )
    print("--- response ---")
    print(result)
    print("------------------------")
    print(f"GEMINI_API_KEY_8 works against {CHAIN[0]['model']}. Patch 5b confirmed.")
except Exception as e:
    print(f"--- STILL FAILING: {type(e).__name__}: {e} ---")
    print(
        "gemini-3.6-flash may itself have been deprecated/restricted since this "
        "patch was written -- check https://ai.google.dev/gemini-api/docs/models "
        "for the current stable model ID."
    )
