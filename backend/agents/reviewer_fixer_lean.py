"""
agents/reviewer_fixer_lean.py — Combined Reviewer+Fixer, Part 2.4's tier-1
pipeline, third step. This is a genuinely new agent (the blueprint calls
it out as such) — tier 3's Reviewer Pool and Fixer Pool are two separate
multi-worker stages; a single small module doesn't need either the
redundancy (3 reviewers) or the partitioning (multiple fixer workers) that
exist for a multi-module tier-3 cycle.

Part 2.4's table: "mirrors the Idea Planner's existing 3-tier fallback
chain, reused here since both jobs are single-pass judgment calls" — so
this CHAIN is copied from agents/idea_planner.py's, not hand-derived:
Groq openai/gpt-oss-120b -> qwen/qwen3.6-27b (migrated off llama-3.3-70b-
versatile, decommissioned by Groq) -> Cerebras gpt-oss-120b -> GitHub Models
gpt-4.1-mini. (Same deprecation note as idea_planner.py: Cerebras's
original llama-3.3-70b 404s now, gpt-oss-120b is the current guaranteed
model.)

One call does both jobs at once — review AND fix in the same pass —
since for one small module there's no independent value in reviewing
first and fixing second as two separate LLM calls; it's the same model
looking at the same code twice.

Patch 8.9 fix: the CHAIN copied from idea_planner.py (see above) also
copied its single-shared-Cerebras-key bug. Now a 3-step Cerebras rotation
across CEREBRAS_API_KEY_1/_2/_3 plus a real Gemini fallback, the exact
fix agents/code_writer_lean.py already proved out and agents/
prompt_writer_lean.py's Patch 8.8 already applied for the same reason --
see this file's own CHAIN comment below.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.errors import MissingDependencyError  # NEW — bug fix
from memory.bus import KEYS, read, write
from utils.llm_client import generate_text

# Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
# its fallback step is removed here, not replaced. The Groq -> Cerebras
# redundancy above is unchanged.
#
# Patch 8.9 fix: the Cerebras step read CEREBRAS_API_KEY_1 -- copied
# verbatim from agents/idea_planner.py's own pre-fix CHAIN (see this
# file's docstring), carrying that account's single-point-of-failure bug
# along with it. Confirmed by Patch 8.1's audit as the same account
# hardcoded in idea_planner.py, responder.py, and prompt_writer_lean.py
# too -- a cooldown on it took out this agent's whole fallback step at
# the same moment it took out the others. idea_planner.py's own fix
# (Patch 8.2) wired it into eo/registry.py's build_fallback_chain(), but
# this is the tier-1 lean pipeline, which deliberately bypasses the
# registry for speed (see eo/registry.py:110) -- build_fallback_chain()
# has nothing to resolve under a "reviewer_fixer_lean" role, so unlike
# idea_planner.py this can't just call into the registry. Same exact fix
# as agents/code_writer_lean.py's 2026-08-12 fix (and agents/
# prompt_writer_lean.py's Patch 8.8, applied for the identical reason):
# each Cerebras step now uses its own real account (CEREBRAS_API_KEY_1/
# _2/_3, siblings of the production 5-key pool -- see eo/registry.py)
# instead of one shared key, plus a genuine second-provider step (Gemini,
# its own key/account/infrastructure) appended after so a Cerebras-wide
# outage doesn't take this agent down entirely either.
#
# OR-3b (reliability_overhaul_plan.md): Cerebras retired to a paid tier
# (see OR-2's .env.example note) -- the 3-step rotation above is now
# OpenRouter instead. "openrouter/free" for all three steps (not a pinned
# model slug -- see utils/llm_client.py's OPENROUTER_BASE_URL comment on
# why OpenRouter's free roster rotates too fast to hardcode a slug
# safely); the redundancy here is across 3 separate OpenRouter accounts
# (key_env), same role the 3 distinct Cerebras models used to play. Same
# key slots (OPENROUTER_API_KEY_1/_2/_3) as prompt_writer_lean.py's
# identical fix -- deliberate, matching how both files shared
# CEREBRAS_API_KEY_1/_2/_3 before. idea_planner.py and responder.py still
# read CEREBRAS_API_KEY_1 for now -- separate, not-yet-migrated files
# (OR-3d audit), unaffected by this change.
OPENROUTER_MODEL = "openrouter/free"
OPENROUTER_KEYS = ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3"]

CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY"},
] + [
    {"provider": "openrouter", "model": OPENROUTER_MODEL, "key_env": k}
    for k in OPENROUTER_KEYS
] + [
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_1"},
]

SYSTEM_PROMPT = """You are reviewing and fixing ONE small, self-contained code \
module in a single pass. You will be given the module's spec and its code.
Find any bugs, missing edge-case handling, or spec mismatches, then fix them \
directly in the code. Keep the fix minimal and in the same style — do not \
restructure the module or introduce new files, adapters, or indirection \
layers unless the spec explicitly requires integrating with an external \
system (Part 8.5's simplicity constraint applies here too).

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly \
this shape:
{
  "issues_found": ["short description of each issue found, or empty list"],
  "code": "the full corrected code"
}
If no issues were found, return the original code unchanged in "code" and \
an empty "issues_found" list."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json")
    return text.strip()


def run(module: dict = None, session_id: str = None, path: str = None,
        domain: str = None) -> dict:
    if module:
        write(KEYS["tier1_code"], module)
    else:
        module = read(KEYS["tier1_code"])
        if not module:
            # Bug fix: consistent error type (see eo/errors.py). Same
            # "fixed pipeline, won't auto-heal" note as code_writer_lean.py.
            raise MissingDependencyError(
                "code_writer_lean",
                "No tier1_code found in memory and none passed in. "
                "Run code_writer_lean first.",
            )
    user_content = json.dumps(module)
    raw = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        chain=CHAIN,
        agent_name="Reviewer+Fixer (lean)",
        session_id=session_id,
        path=path,  # Migration Part 27 §1: generate_text() now accepts `path` for real
        domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
    )
    try:
        parsed = json.loads(_strip_fences(raw))
        fixed_code = parsed.get("code") or module.get("code", "")
        issues = parsed.get("issues_found", [])
    except json.JSONDecodeError:
        # Same discipline as fixer_pool.py: never propagate malformed
        # output downstream, fall back to the pre-fix version.
        fixed_code = module.get("code", "")
        issues = ["Reviewer+Fixer output was not valid JSON — kept original code."]
    result = {
        "name": module.get("name", "module"),
        "language": module.get("language", "python"),
        "code": fixed_code,
        "issues_found": issues,
    }
    write(KEYS["tier1_review_notes"], {"issues_found": issues})
    write(KEYS["tier1_fixed_code"], result)
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))