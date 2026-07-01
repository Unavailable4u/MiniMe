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
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras, RateLimitError, APIStatusError, APIConnectionError, APITimeoutError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

CEREBRAS_MODEL = "gpt-oss-120b"
CEREBRAS_KEY_ENVS = ["CEREBRAS_API_KEY_6", "CEREBRAS_API_KEY_7", "CEREBRAS_API_KEY_8"]

# Cloudflare fallback -- key #3 in the production roster.
# Using llama-3.3-70b-instruct-fp8-fast rather than the smaller
# llama-3.1-8b-instruct: Cloudflare only lists a specific set of models as
# supporting JSON Mode (https://developers.cloudflare.com/workers-ai/features/json-mode/),
# and this is the strongest one on that list -- the 8B model isn't on it
# and was unreliable at returning parseable JSON under this prompt.
CLOUDFLARE_ACCOUNT_ID_ENV = "CLOUDFLARE_ACCOUNT_ID_3"
CLOUDFLARE_TOKEN_ENV = "CLOUDFLARE_API_KEY_3"
CLOUDFLARE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

_TRANSIENT_ERRORS = (RateLimitError, APIStatusError, APIConnectionError, APITimeoutError)

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

_cerebras_client_cache = {}


def _get_cerebras_client(key_env: str):
    key = os.getenv(key_env)
    if not key:
        return None
    if key_env not in _cerebras_client_cache:
        _cerebras_client_cache[key_env] = Cerebras(api_key=key)
    return _cerebras_client_cache[key_env]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _call_cerebras(key_env: str, user_prompt: str) -> str:
    client = _get_cerebras_client(key_env)
    if client is None:
        raise RuntimeError(f"{key_env} not set")
    response = client.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content or ""


def _build_json_schema(module_names) -> dict:
    """Builds a JSON Schema requiring exactly the given module names as
    top-level keys, each shaped like {"language": str, "code": str}.
    Passed as response_format to Cloudflare's JSON Mode so the model is
    constrained at decode time rather than just asked nicely."""
    return {
        "type": "object",
        "properties": {
            name: {
                "type": "object",
                "properties": {
                    "language": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["code"],
            }
            for name in module_names
        },
        "required": list(module_names),
    }


def _call_cloudflare_fallback(user_prompt: str, module_names) -> str:
    account_id = os.getenv(CLOUDFLARE_ACCOUNT_ID_ENV)
    token = os.getenv(CLOUDFLARE_TOKEN_ENV)
    if not account_id or not token:
        raise RuntimeError(f"{CLOUDFLARE_ACCOUNT_ID_ENV} / {CLOUDFLARE_TOKEN_ENV} not set")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{CLOUDFLARE_MODEL}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": _build_json_schema(module_names),
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {}).get("response", "")


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
    """
    if isinstance(entry, dict) and "code" in entry:
        return entry
    if isinstance(entry, str):
        return {"language": original.get("language", "python"), "code": entry}
    # Anything else unexpected (list, None, number...) -- not worth
    # guessing, fall back to the pre-fix version of this module.
    return original


def _run_one_worker(worker_index: int, key_env: str, modules: dict, review_notes: dict) -> dict:
    """
    Runs on one thread with one fixed Cerebras key. Fixes only the modules
    assigned to this worker. Falls back to the shared Cloudflare account #3
    if Cerebras is unavailable or errors transiently. Returns the modules
    unchanged (not a crash) if both fail, so one bad worker doesn't lose
    the rest of the pool's output.
    """
    if not modules:
        return {}

    relevant = _relevant_issues(review_notes, set(modules.keys()))
    user_prompt = (
        "Modules assigned to you:\n" + json.dumps(modules, indent=2)
        + "\n\nReview issues relevant to these modules:\n" + json.dumps(relevant, indent=2)
    )

    raw = None
    try:
        raw = _call_cerebras(key_env, user_prompt)
    except _TRANSIENT_ERRORS as exc:
        print(f"  [Fixer {worker_index}] Cerebras ({key_env}) failed "
              f"({exc.__class__.__name__}), falling back to Cloudflare...")
    except RuntimeError as exc:
        print(f"  [Fixer {worker_index}] {exc}, falling back to Cloudflare...")

    if raw is None:
        try:
            raw = _call_cloudflare_fallback(user_prompt, modules.keys())
        except Exception as exc:
            print(f"  [Fixer {worker_index}] Cloudflare fallback also failed: {exc}. "
                  f"Keeping original code for these modules.")
            return modules

    cleaned = _strip_fences(raw)
    fixed = _extract_json(cleaned)
    if fixed is None or not isinstance(fixed, dict):
        print(f"  [Fixer {worker_index}] output was not valid JSON, keeping original code.")
        return modules

    # Normalize each module's shape, and guard against a worker silently
    # dropping a module it was given.
    result = {}
    for name, original in modules.items():
        entry = fixed.get(name)
        result[name] = _normalize_entry(entry, original) if entry is not None else original
    return result


def run_fixer_pool():
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
            executor.submit(_run_one_worker, i + 1, CEREBRAS_KEY_ENVS[i], buckets[i], review_notes): i
            for i in range(num_workers)
        }
        for future in as_completed(futures):
            fixed_code.update(future.result())

    write(KEYS["fixed_code"], fixed_code)
    return fixed_code


if __name__ == "__main__":
    fixed = run_fixer_pool()
    print(json.dumps(fixed, indent=2))