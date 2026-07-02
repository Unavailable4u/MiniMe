"""
agents/fixer_pool.py — Fixer Pool (Part 4, agent #9 of the v5 Master
Blueprint).

Replaces the fixing half of the old agents/fixer_tester.py. Testing is now
a separate agent: agents/sandbox_tester.py (#10).

- Provider: Cerebras `gpt-oss-120b`, 3 genuinely parallel workers.
- Keys: CEREBRAS_API_KEY_6, CEREBRAS_API_KEY_7, CEREBRAS_API_KEY_8 -- kept
  isolated from the Code Writer Pool's keys #1-#5.
- Fallback: Cloudflare Workers AI, key #3 (CLOUDFLARE_ACCOUNT_ID_3 /
  CLOUDFLARE_API_KEY_3), same pattern as reviewer.py's key #2 fallback.
- Unlike the Reviewer Pool (all 3 workers independently look at ALL code,
  then get merged), the Fixer Pool partitions modules round-robin across
  workers -- fixing module A doesn't need to see module B, so splitting
  the work is strictly parallel, not redundant-and-merged.
"""

import os
import sys
import json
import time
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from utils.llm_client import generate_text

load_dotenv()

CEREBRAS_MODEL = "gpt-oss-120b"
CEREBRAS_KEY_ENVS = ["CEREBRAS_API_KEY_6", "CEREBRAS_API_KEY_7", "CEREBRAS_API_KEY_8"]

# Cloudflare fallback -- key #3 in the production roster.
# Using llama-3.3-70b-instruct-fp8-fast rather than the smaller
# llama-3.1-8b-instruct: it was the more reliable of the two at returning
# parseable JSON under this prompt. NOTE: this fallback used to be pinned
# to Cloudflare's JSON Mode with an explicit schema (see git history) --
# since the swap to generate_text() (which has no response_format
# support), that decode-time constraint is gone. _extract_json()'s
# forgiving parse + _normalize_entry()'s shape coercion below are the
# remaining safety net for malformed output on this path.
CLOUDFLARE_ACCOUNT_ID_ENV = "CLOUDFLARE_ACCOUNT_ID_3"
CLOUDFLARE_TOKEN_ENV = "CLOUDFLARE_API_KEY_3"
CLOUDFLARE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

SYSTEM_PROMPT = """You are a bug-fixing engineer. You will be given JSON containing
one or more code modules and a list of review issues found in them. Resolve every
"critical" and "moderate" issue relevant to these modules. You may leave "minor"
issues unless they're trivial to fix. Do not change module names. Do not add new
modules.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly this shape:
{
  "module_name": {"language": "python", "code": "...full corrected code..."}
}
Return every module you were given, fixed or not, with its full code each time.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _extract_json(text: str):
    """Tries a straight json.loads first; if the model wrapped valid JSON
    in prose or fences despite instructions, falls back to slicing between
    the first '{' and the last '}' and retrying once. Returns None if
    neither works."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _partition_modules(submitted_code: dict, num_workers: int) -> list:
    """Round-robin split of {module_name: {...}} into num_workers dicts."""
    buckets = [dict() for _ in range(num_workers)]
    for i, (name, data) in enumerate(submitted_code.items()):
        buckets[i % num_workers][name] = data
    return buckets


def _relevant_issues(review_notes: dict, module_names: set) -> list:
    issues = review_notes.get("issues", []) if review_notes else []
    return [issue for issue in issues if issue.get("module") in module_names]


def _normalize_entry(entry, original: dict) -> dict:
    """
    Coerces one module's fixer output into the {"language": ..., "code": ...}
    shape the rest of the pipeline expects. Models don't always follow the
    nested schema exactly -- sometimes they return the code as a bare
    string for a given key instead of a {"language", "code"} object. Rather
    than let that malformed shape propagate downstream (where
    sandbox_tester.py would crash on .get("code")), fix it here at the
    source.

    Also guards against a shape that WAS valid JSON but whose "code" string
    is not valid Python -- e.g. a worker fixing one reported issue while
    silently de-indenting an unrelated block (observed bug: `if weight_kg >
    5:` followed by a same-level `return` instead of an indented one).
    _extract_json()'s JSON validation has no visibility into the Python
    embedded inside a string value, so it passes shape-checks cleanly and
    would otherwise reach fixed_code -- and from there sandbox_tester.py --
    as a SyntaxError several stages downstream, several steps removed from
    the actual cause. Only python-language modules are checked; other
    languages have no cheap syntax check available here and are passed
    through as before.
    """
    if isinstance(entry, dict) and "code" in entry:
        candidate = entry
    elif isinstance(entry, str):
        candidate = {"language": original.get("language", "python"), "code": entry}
    else:
        # Anything else unexpected (list, None, number...) -- not worth
        # guessing, fall back to the pre-fix version of this module.
        return original

    if candidate.get("language", "python") == "python":
        try:
            ast.parse(candidate["code"])
        except SyntaxError as exc:
            print(f"  [Fixer] rejected syntactically invalid fix, keeping original "
                  f"code for this module: {exc}")
            return original

    return candidate


def _run_one_worker(worker_index: int, key_env: str, modules: dict, review_notes: dict,
                     session_id: str = None, tier: int = None) -> dict:
    """
    Runs on one thread with one fixed Cerebras key. Fixes only the modules
    assigned to this worker. Falls back to the shared Cloudflare account #3
    (via generate_text()'s chain) if Cerebras is unavailable or errors
    transiently. Returns the modules unchanged (not a crash) if both fail,
    so one bad worker doesn't lose the rest of the pool's output.

    Stage 6 step 5: fires agent_start/agent_done. Unlike Code Writer
    Pool's 1-module-per-worker lanes, a Fixer worker may hold multiple
    modules in its bucket (round-robin partition) -- the label lists all
    module names this worker owns, not just one.
    """
    agent_name = f"fixer_{worker_index}"
    if not modules:
        emit_event("agent_start", session_id=session_id, agent=agent_name, tier=tier,
                   payload={"label": f"Fixer {worker_index} — (no modules assigned)"})
        emit_event("agent_done", session_id=session_id, agent=agent_name, tier=tier,
                   payload={"summary": "no modules assigned", "duration_ms": 0})
        return {}

    module_names = list(modules.keys())
    emit_event("agent_start", session_id=session_id, agent=agent_name, tier=tier,
               payload={"label": f"Fixer {worker_index} — {', '.join(module_names)}"})
    started = time.monotonic()

    def _done(result: dict) -> dict:
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, tier=tier,
                   payload={"summary": f"fixed: {', '.join(result.keys())}", "duration_ms": duration_ms})
        return result

    relevant = _relevant_issues(review_notes, set(modules.keys()))
    user_prompt = (
        "Modules assigned to you:\n" + json.dumps(modules, indent=2)
        + "\n\nReview issues relevant to these modules:\n" + json.dumps(relevant, indent=2)
    )

    chain = [
        {"provider": "cerebras", "model": CEREBRAS_MODEL, "key_env": key_env},
        {"provider": "cloudflare", "model": CLOUDFLARE_MODEL,
         "account_id_env": CLOUDFLARE_ACCOUNT_ID_ENV, "token_env": CLOUDFLARE_TOKEN_ENV},
    ]

    try:
        raw = generate_text(SYSTEM_PROMPT, user_prompt, chain, agent_name=agent_name,
                             session_id=session_id, tier=tier)
    except RuntimeError as exc:
        print(f"  [Fixer {worker_index}] {exc}. Keeping original code for these modules.")
        return _done(modules)

    cleaned = _strip_fences(raw)
    fixed = _extract_json(cleaned)
    if fixed is None or not isinstance(fixed, dict):
        print(f"  [Fixer {worker_index}] output was not valid JSON, keeping original code.")
        return _done(modules)

    # Normalize each module's shape, and guard against a worker silently
    # dropping a module it was given.
    result = {}
    for name, original in modules.items():
        entry = fixed.get(name)
        result[name] = _normalize_entry(entry, original) if entry is not None else original
    return _done(result)


def run_fixer_pool(session_id: str = None, tier: int = None):
    submitted_code = read(KEYS["submitted_code"])
    review_notes = read(KEYS["review_notes"])

    if not submitted_code:
        raise ValueError("No submitted_code found in memory. Run the Code Writers first.")
    if not review_notes:
        review_notes = {"issues": [], "summary": ""}

    num_workers = min(len(CEREBRAS_KEY_ENVS), max(len(submitted_code), 1))
    buckets = _partition_modules(submitted_code, num_workers)

    fixed_code = {}
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_run_one_worker, i + 1, CEREBRAS_KEY_ENVS[i], buckets[i], review_notes,
                             session_id=session_id, tier=tier): i
            for i in range(num_workers)
        }
        for future in as_completed(futures):
            fixed_code.update(future.result())

    write(KEYS["fixed_code"], fixed_code)
    return fixed_code


if __name__ == "__main__":
    fixed = run_fixer_pool()
    print(json.dumps(fixed, indent=2))