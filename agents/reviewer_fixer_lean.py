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
Groq llama-3.3-70b-versatile -> Cerebras gpt-oss-120b -> GitHub Models
gpt-4.1-mini. (Same deprecation note as idea_planner.py: Cerebras's
original llama-3.3-70b 404s now, gpt-oss-120b is the current guaranteed
model.)

One call does both jobs at once — review AND fix in the same pass —
since for one small module there's no independent value in reviewing
first and fixing second as two separate LLM calls; it's the same model
looking at the same code twice.
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.llm_client import generate_text
from eo.errors import MissingDependencyError   # NEW — bug fix

CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_1"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
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
        if text.startswith("json"):
            text = text[4:]
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