"""
agents/reviewer.py — Reviewer Pool

Rewritten from a single Cerebras call to match the blueprint:
- Provider: Groq (openai/gpt-oss-120b), 3 genuinely parallel workers.
- Keys: GROQ_API_KEY_6, GROQ_API_KEY_7, GROQ_API_KEY_8 -- one dedicated
  key per worker, isolated from the shared GROQ_API_KEY used by the
  low-volume sequential agents (Idea Planner, Prompt Writer, Test Writer,
  Report Writer, Gatekeeper).
- Fallback: if a worker's Groq call fails, it falls back to Cloudflare
  Workers AI using key #2 (CLOUDFLARE_API_KEY_2 / CLOUDFLARE_ACCOUNT_ID_2),
  same account/key pair already used elsewhere in the production roster.
- The 3 independent review outputs are merged by review_aggregator.py
  (deterministic, no LLM) into the single review_notes object that
  fixer_tester.py already expects -- no change needed downstream.

Migration Part 11 §2: each worker's prompt also gets NEXT_TAG_INSTRUCTION
appended (agents/generic_worker.py's shared tag mechanism), so a reviewer
can flag that its own findings need another pass from a specific earlier
or later stage, not just list issues. Each worker's raw output has its
NEXT: tag parsed off (via agents.generic_worker.parse_next_tag) BEFORE
JSON parsing -- the tag line is never part of the JSON body. run_reviewer()
merges the 3 (or 5, expanded) workers' votes deterministically: majority
vote wins; on a tie or no majority, the first non-DONE vote by worker
order wins; if every worker said DONE (or gave no parseable tag),
next_destination is None. The merged value is written straight into
review_notes -- safe, since that dict previously only ever had
"issues"/"summary".
"""

import os
import sys
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from agents.review_aggregator import aggregate_reviews
from agents.generic_worker import NEXT_TAG_INSTRUCTION, parse_next_tag
from utils.llm_client import generate_text
from eo.registry import AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot

load_dotenv()

GROQ_MODEL = "openai/gpt-oss-120b"

# Migration Part 9 §2.1: replaces the old static GROQ_KEY_ENVS +
# GROQ_RESERVE_KEY_ENVS lists with a registry-driven pool, ranked by live
# quota every run — same pattern as code_writers.py, see that module's
# _eligible_pool()/_select_workers() docstrings for the full reasoning.
ROLE_TAG = "verifier"


def _eligible_pool() -> list:
    return [key for key, info in AGENT_CAPABILITIES.items() if ROLE_TAG in info.get("natural_roles", [])]


def _select_workers(worker_count: int, key_override=None) -> list:
    if key_override:
        return key_override if isinstance(key_override, list) else [key_override]
    pool = _eligible_pool()
    if not pool:
        raise RuntimeError("reviewer: no accounts tagged 'verifier' in AGENT_CAPABILITIES.")
    snapshot = get_quota_snapshot()
    ranked = sorted(pool, key=lambda k: (snapshot.get(k) or {}).get("pct") or 0.0)
    return ranked[:worker_count]

# Cloudflare fallback -- key #2 in the production roster, per the
# blueprint's "Cloudflare Workers AI ... key #2" cell for this agent.
CLOUDFLARE_ACCOUNT_ID_ENV = "CLOUDFLARE_ACCOUNT_ID_2"
CLOUDFLARE_TOKEN_ENV = "CLOUDFLARE_API_KEY_2"
# Not pinned by the blueprint for this specific fallback slot -- using the
# same general-purpose instruct model chosen for the EO panel's Cloudflare
# member for consistency. Swap if you want a different Workers AI model.
CLOUDFLARE_MODEL = "@cf/meta/llama-3.1-8b-instruct"

SYSTEM_PROMPT = """You are a strict code reviewer. You will be given JSON containing
multiple code modules submitted by different writers. List every bug, security risk,
and interface mismatch between modules. Rate each issue: critical, moderate, minor.
Be specific about which module/file the issue is in.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly this shape:
{
  "issues": [
    {"module": "module_name", "severity": "critical|moderate|minor", "description": "..."}
  ],
  "summary": "one or two sentence overall verdict"
}
If there are no issues, return an empty issues list.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _run_one_worker(worker_index: int, key_env: str, user_prompt: str,
                     session_id: str = None, path: str = None) -> tuple:
    """
    Runs on one thread. Tries this worker's dedicated Groq key first,
    falls back to the shared Cloudflare account #2 if Groq is unavailable
    or errors transiently. Returns (review_dict, next_destination) --
    review_dict is a parsed review dict, or an empty review (not a crash)
    if both fail -- one bad worker shouldn't sink the whole pool's
    aggregation. next_destination is this worker's own NEXT: vote, or
    None if both fail, the output wasn't valid JSON, or the worker simply
    voted DONE / gave no tag at all.

    Stage 6 step 5: fires agent_start/agent_done per worker so the
    frontend's live activity panel shows all 3 reviewers running at
    once. Unlike Code Writer Pool's workers, each reviewer_N here is
    reviewing the SAME submitted_code, not a different module -- expect
    to see similar/overlapping content across the 3 lanes, with
    potentially different verdicts. That's the reviewer-pool pattern
    (independent opinions -> aggregated), not a bug.
    """
    agent_name = f"reviewer_{worker_index}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Reviewer {worker_index}"})
    started = time.monotonic()

    chain = [
        {"provider": "groq", "model": GROQ_MODEL, "key_env": key_env},
        {"provider": "cloudflare", "model": CLOUDFLARE_MODEL,
         "account_id_env": CLOUDFLARE_ACCOUNT_ID_ENV, "token_env": CLOUDFLARE_TOKEN_ENV},
    ]

    try:
        raw = generate_text(SYSTEM_PROMPT + NEXT_TAG_INSTRUCTION, user_prompt, chain,
                             agent_name=agent_name, session_id=session_id, path=path)
    except RuntimeError as exc:
        print(f"  [Reviewer {worker_index}] {exc}")
        result = {"issues": [], "summary": f"Reviewer {worker_index} produced no output (both providers failed)."}
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": result["summary"], "duration_ms": duration_ms})
        return result, None

    # Migration Part 11 §2: the NEXT: tag is parsed off BEFORE JSON
    # parsing -- it lives on its own final line, outside the JSON body
    # the reviewer was asked to produce, so it must come off first or
    # json.loads() would choke on the trailing text.
    body, next_destination = parse_next_tag(raw)
    cleaned = _strip_fences(body)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {"issues": [], "summary": f"Reviewer {worker_index} output was not valid JSON, discarded."}
        next_destination = None

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = result.get("summary", "") or f"{len(result.get('issues', []))} issue(s) found"
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})
    return result, next_destination


def _merge_next_destinations(votes: list) -> str:
    """
    Migration Part 11 §2: deterministic merge of the reviewer pool's
    per-worker NEXT: votes -- majority wins; on a tie or no majority, the
    first non-None vote by worker order wins; if every worker said DONE
    (None), the merged result is None.
    """
    cast = [v for v in votes if v]
    if not cast:
        return None
    counts = Counter(cast)
    top_count = max(counts.values())
    winners = [v for v in cast if counts[v] == top_count]
    # `cast` is already in worker order, so the first element of `winners`
    # in that order is both "the majority winner" (if top_count > 1 and
    # unique) and "the first non-DONE vote by worker order" (the
    # tie/no-majority fallback) -- one pass covers both rules.
    for v in cast:
        if v in winners:
            return v
    return None


def run_reviewer(session_id: str = None, path: str = None, expanded: bool = False,
                  key_override=None):
    """
    Migration Part 5 §2.3 — key_override, if given, is the Panel's specific
    account-selection decision for this hire (mirrors code_writers.py's
    run(), see that module's docstring for the full three-case explanation):

    key_override: None (default) -> today's exact behavior, picks
        GROQ_KEY_ENVS/GROQ_RESERVE_KEY_ENVS via _worker_keys(expanded).
    key_override: a single key_env string -> use ONLY that account for
        all 3 (or however many) review workers this call.
    key_override: a list of key_env strings -> use exactly those accounts
        as the parallel reviewer pool for this call.
    """
    submitted_code = read(KEYS["submitted_code"])
    if not submitted_code:
        # Migration Part 26 fix: a hires-driven plan can staff a
        # "verifier" role (which resolves to this module, per
        # eo/registry.py's REAL_ACTION_ROLES) without ever having staffed
        # an "implementer" first -- e.g. a hardware/embedded task hiring
        # only custom roles like "hardware_expert"/"embedded_software_developer"
        # through generic_worker. In that case nothing ever wrote
        # submitted_code, through no fault of this call. Raising here used
        # to crash the whole run; failing soft instead lets the plan
        # continue past a review step that genuinely has nothing to
        # review. Shape matches aggregate_reviews()'s normal return
        # ("issues"/"summary") plus next_destination, so callers that read
        # either key (e.g. eo/loop_controller.py's _extract_critical_issue)
        # keep working unchanged.
        review_notes = {
            "issues": [],
            "summary": "Reviewer skipped: no submitted_code found in memory "
                       "(no Code Writer step ran before this one).",
            "next_destination": None,
        }
        write(KEYS["review_notes"], review_notes)
        return review_notes

    user_prompt = (
        "Here is the submitted code from all modules this cycle:\n\n"
        + json.dumps(submitted_code, indent=2)
    )

    worker_count = 5 if expanded else 3
    key_envs = _select_workers(worker_count, key_override)

    member_reviews = [None] * len(key_envs)
    next_votes = [None] * len(key_envs)
    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(_run_one_worker, i + 1, key_env, user_prompt,
                             session_id=session_id, path=path): i
            for i, key_env in enumerate(key_envs)
        }
        for future in as_completed(futures):
            i = futures[future]
            member_reviews[i], next_votes[i] = future.result()

    review_notes = aggregate_reviews(member_reviews)
    # Migration Part 11 §2: safe to add directly -- review_notes only
    # ever had "issues"/"summary" before this.
    review_notes["next_destination"] = _merge_next_destinations(next_votes)
    write(KEYS["review_notes"], review_notes)
    return review_notes


if __name__ == "__main__":
    notes = run_reviewer()
    print(json.dumps(notes, indent=2))