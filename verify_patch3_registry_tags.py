"""
verify_patch3_registry_tags.py — run this from your repo root after
applying patch3-tag-driven-registry-entries.diff.

    cd MiniMe
    python verify_patch3_registry_tags.py

This does NOT hand-build a chain the way verify_patch1/2 did — the whole
point of Patch 3 is the *routing* (eo/panel.py's _best_match() and
eo/worker_pool.py's _select_workers() picking a new account on their
own), so this script calls those functions directly and checks their
output, then makes one real generate_text() call through whichever
account came back.

Checks three of the four Patch-3 pools (one is enough to prove the
mechanism per pool type; the guide's §1 table already confirms all four
role groups share the same two dispatch functions):

  1. _best_match("formatter", ...)  -> should now return GEMINI_API_KEY_1
     (generic_worker / catch-all pool)
  2. _best_match("final_qa", ...)   -> should return whichever of
     GEMINI_API_KEY_3 / MISTRAL_API_KEY_4 / MISTRAL_API_KEY has the most
     quota headroom right now (three real candidates as of this patch,
     up from one)
  3. _select_workers("source_manager", 5) -> should include
     HUGGINGFACE_API_KEY_2/_3 alongside the existing GROQ_* extraction
     pool accounts

Expected: all three print a candidate list that includes the new
Patch-3 keys, and the final live call returns real text (not a
traceback). If a pool prints WITHOUT any of the new keys, something
about the natural_roles spelling doesn't match what resolve_role()/the
calling module actually passes in -- recheck against §1's verification
recipe before assuming the patch is broken.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from eo.panel import _best_match  # noqa: E402
from eo.worker_pool import _select_workers, _eligible_pool  # noqa: E402
from utils.llm_client import generate_text  # noqa: E402
from agents.generic_worker import _chain_step_for  # noqa: E402

missing = [k for k in ("GEMINI_API_KEY_1", "GEMINI_API_KEY_3",
                        "MISTRAL_API_KEY_4",
                        "HUGGINGFACE_API_KEY_2", "HUGGINGFACE_API_KEY_3")
           if not os.getenv(k)]
if missing:
    raise SystemExit(
        "These Patch-3 keys are not set in your .env yet, fill them in "
        f"before running this: {', '.join(missing)}"
    )

print("--- 1. catch-all pool: formatter ---")
formatter_pick = _best_match("formatter")
print("_best_match('formatter') ->", formatter_pick)
assert formatter_pick is not None, "formatter should always resolve to something"

print("\n--- 2. final_qa now has 3 real candidates instead of 1 ---")
# final_qa is a generic_worker role, not a worker_pool role, so list its
# candidates the same way _best_match() does internally:
from eo.registry import AGENT_CAPABILITIES  # noqa: E402
final_qa_candidates = [k for k, v in AGENT_CAPABILITIES.items() if "final_qa" in v.get("natural_roles", [])]
print("final_qa candidates:", final_qa_candidates)
assert "GEMINI_API_KEY_3" in final_qa_candidates and "MISTRAL_API_KEY_4" in final_qa_candidates, (
    "expected both new final_qa keys to show up as candidates"
)
final_qa_pick = _best_match("final_qa")
print("_best_match('final_qa') ->", final_qa_pick)

print("\n--- 3. extraction pool: source_manager ---")
pool = _eligible_pool("source_manager")
print("source_manager eligible pool:", pool)
assert "HUGGINGFACE_API_KEY_2" in pool and "HUGGINGFACE_API_KEY_3" in pool, (
    "expected the new HF accounts in the source_manager pool"
)
selected = _select_workers("source_manager", 5)
print("_select_workers('source_manager', 5) ->", selected)

print("\n--- 4. one real live call through whichever account formatter picked ---")
chain = [_chain_step_for(formatter_pick)]
result = generate_text(
    system_prompt="You are a terse test assistant.",
    user_content="Reply with exactly the words: patch3 routing ok",
    chain=chain,
    agent_name="Patch3VerificationScript",
)
print(result)
print("------------------------")
print(f"Live call went through {formatter_pick} ({chain[0]['provider']}, "
      f"model={chain[0]['model']}).")
print("Check your usage dashboard / Upstash for a fresh "
      f"usage:{chain[0]['provider']}:{formatter_pick}:<today> entry.")
print(
    "\nNOTE: that model field is PROVIDER_DEFAULT_MODEL['gemini'] "
    "(gemini-2.5-flash) regardless of which of GEMINI_API_KEY_1/_2/_3 got "
    "picked -- the flash-lite/flash/pro split in the guide's §4a table "
    "does not apply to tag-driven routing (see the comment above the new "
    "entries in eo/registry.py). Routing/redundancy is real; per-key "
    "model choice for these three isn't, as of this patch."
)
