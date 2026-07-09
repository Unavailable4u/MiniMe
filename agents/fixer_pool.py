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

Migration Part 11 §2: each worker's prompt also gets NEXT_TAG_INSTRUCTION
appended, so a fixer can flag that its patch needs another pass from a
specific stage. Design gap worth flagging: unlike the Reviewer Pool (3
overlapping opinions on the same question, so a majority vote makes
sense), Fixer Pool workers own DISJOINT module partitions -- they aren't
answering the same question, so there's no "majority" to take. First
non-DONE vote by worker order wins instead.

This also changes run_fixer_pool()'s return shape: it used to return the
bare {module_name: {...}} modules dict. Since a "next_destination" key
could collide with an actual module name, the return is now
{"fixed_code": {...}, "next_destination": ...}. The memory-bus write
(KEYS["fixed_code"]) is UNAFFECTED -- it still gets just the plain
modules dict, so sandbox_tester.py and every other downstream consumer of
that bus key keeps working unmodified. Only direct callers of this
function (including the __main__ demo below) need the ["fixed_code"]
update.
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
from agents.generic_worker import NEXT_TAG_INSTRUCTION, parse_next_tag
from utils.llm_client import generate_text
from eo.errors import MissingDependencyError   # NEW — bug fix

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
                     session_id: str = None, path: str = None) -> tuple:
    """
    Runs on one thread with one fixed Cerebras key. Fixes only the modules
    assigned to this worker. Falls back to the shared Cloudflare account #3
    (via generate_text()'s chain) if Cerebras is unavailable or errors
    transiently. Returns (modules_dict, next_destination) -- modules_dict
    is the fixed modules (or unchanged, not a crash, if both providers
    fail) so one bad worker doesn't lose the rest of the pool's output.
    next_destination is this worker's own NEXT: vote, or None if both
    providers failed, the output wasn't valid JSON, or this worker had no
    modules assigned at all.

    Stage 6 step 5: fires agent_start/agent_done. Unlike Code Writer
    Pool's 1-module-per-worker lanes, a Fixer worker may hold multiple
    modules in its bucket (round-robin partition) -- the label lists all
    module names this worker owns, not just one.
    """
    agent_name = f"fixer_{worker_index}"
    if not modules:
        emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
                   payload={"label": f"Fixer {worker_index} — (no modules assigned)"})
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": "no modules assigned", "duration_ms": 0})
        return {}, None

    module_names = list(modules.keys())
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Fixer {worker_index} — {', '.join(module_names)}"})
    started = time.monotonic()

    def _done(result: dict, next_destination) -> tuple:
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": f"fixed: {', '.join(result.keys())}", "duration_ms": duration_ms})
        return result, next_destination

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
        raw = generate_text(SYSTEM_PROMPT + NEXT_TAG_INSTRUCTION, user_prompt, chain,
                             agent_name=agent_name, session_id=session_id, path=path)
    except RuntimeError as exc:
        print(f"  [Fixer {worker_index}] {exc}. Keeping original code for these modules.")
        return _done(modules, None)

    # Migration Part 11 §2: the NEXT: tag is parsed off BEFORE JSON
    # extraction -- it's on its own final line, outside the JSON body.
    body, next_destination = parse_next_tag(raw)
    cleaned = _strip_fences(body)
    fixed = _extract_json(cleaned)
    if fixed is None or not isinstance(fixed, dict):
        print(f"  [Fixer {worker_index}] output was not valid JSON, keeping original code.")
        return _done(modules, None)

    # Normalize each module's shape, and guard against a worker silently
    # dropping a module it was given.
    result = {}
    for name, original in modules.items():
        entry = fixed.get(name)
        result[name] = _normalize_entry(entry, original) if entry is not None else original
    return _done(result, next_destination)


def _merge_next_destinations(votes: list) -> str:
    """
    Migration Part 11 §2: Fixer Pool workers own disjoint module
    partitions -- they aren't answering the same question the way
    Reviewer Pool's 3 overlapping opinions are, so there's no majority
    to take. First non-DONE vote by worker order (index order, not
    completion order) wins instead.
    """
    for v in votes:
        if v:
            return v
    return None


def run_fixer_pool(session_id: str = None, path: str = None, key_override=None):
    """
    Migration Part 5 §2.3 — key_override, if given, is the Panel's specific
    account-selection decision for this hire (mirrors code_writers.py's
    run(); see that module's docstring for the full three-case explanation).

    Note: fixer_pool.py has no expanded/reserve-pool concept today (unlike
    code_writers.py/reviewer.py), so "default" here just means
    CEREBRAS_KEY_ENVS as before -- key_override doesn't interact with any
    mode-based sizing, there's nothing to fall back to but the fixed
    3-key list.

    key_override: None (default) -> today's exact behavior, CEREBRAS_KEY_ENVS.
    key_override: a single key_env string -> use ONLY that account; all
        modules go through one worker.
    key_override: a list of key_env strings -> use exactly those accounts
        as the parallel fixer pool for this call, instead of CEREBRAS_KEY_ENVS.

    Migration Part 11 §2: return shape changed from the bare
    {module_name: {...}} dict to {"fixed_code": {...}, "next_destination": ...}
    -- see module docstring. The memory-bus write (KEYS["fixed_code"]) is
    unaffected; it still receives just the plain modules dict.
    """
    submitted_code = read(KEYS["submitted_code"])
    review_notes = read(KEYS["review_notes"])

    if not submitted_code:
        # Bug fix: was `raise ValueError(...)` -- see agents/test_writer.py's
        # identical fix for why this specific role name.
        raise MissingDependencyError(
            "implementer", "No submitted_code found in memory. Run the Code Writers first."
        )
    if not review_notes:
        review_notes = {"issues": [], "summary": ""}

    if key_override is None:
        key_envs = CEREBRAS_KEY_ENVS
    elif isinstance(key_override, list):
        key_envs = key_override
    else:
        key_envs = [key_override]

    num_workers = min(len(key_envs), max(len(submitted_code), 1))
    buckets = _partition_modules(submitted_code, num_workers)

    fixed_code = {}
    next_votes = [None] * num_workers
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_run_one_worker, i + 1, key_envs[i], buckets[i], review_notes,
                             session_id=session_id, path=path): i
            for i in range(num_workers)
        }
        for future in as_completed(futures):
            i = futures[future]
            worker_result, worker_next = future.result()
            fixed_code.update(worker_result)
            next_votes[i] = worker_next

    write(KEYS["fixed_code"], fixed_code)
    return {"fixed_code": fixed_code, "next_destination": _merge_next_destinations(next_votes)}


if __name__ == "__main__":
    result = run_fixer_pool()
    print(json.dumps(result["fixed_code"], indent=2))