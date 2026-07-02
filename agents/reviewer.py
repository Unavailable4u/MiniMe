"""
agents/reviewer.py — Reviewer Pool (Part 4, agent #6 of the v5 Master
Blueprint).

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
"""

import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from agents.review_aggregator import aggregate_reviews
from utils.llm_client import generate_text

load_dotenv()

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_KEY_ENVS = ["GROQ_API_KEY_6", "GROQ_API_KEY_7", "GROQ_API_KEY_8"]

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
                     session_id: str = None, tier: int = None) -> dict:
    """
    Runs on one thread. Tries this worker's dedicated Groq key first,
    falls back to the shared Cloudflare account #2 if Groq is unavailable
    or errors transiently. Returns a parsed review dict, or an empty
    review (not a crash) if both fail -- one bad worker shouldn't sink
    the whole pool's aggregation.

    Stage 6 step 5: fires agent_start/agent_done per worker so the
    frontend's live activity panel shows all 3 reviewers running at
    once. Unlike Code Writer Pool's workers, each reviewer_N here is
    reviewing the SAME submitted_code, not a different module -- expect
    to see similar/overlapping content across the 3 lanes, with
    potentially different verdicts. That's the reviewer-pool pattern
    (independent opinions -> aggregated), not a bug.
    """
    agent_name = f"reviewer_{worker_index}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, tier=tier,
               payload={"label": f"Reviewer {worker_index}"})
    started = time.monotonic()

    chain = [
        {"provider": "groq", "model": GROQ_MODEL, "key_env": key_env},
        {"provider": "cloudflare", "model": CLOUDFLARE_MODEL,
         "account_id_env": CLOUDFLARE_ACCOUNT_ID_ENV, "token_env": CLOUDFLARE_TOKEN_ENV},
    ]

    try:
        raw = generate_text(SYSTEM_PROMPT, user_prompt, chain, agent_name=agent_name,
                             session_id=session_id, tier=tier)
    except RuntimeError as exc:
        print(f"  [Reviewer {worker_index}] {exc}")
        result = {"issues": [], "summary": f"Reviewer {worker_index} produced no output (both providers failed)."}
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, tier=tier,
                   payload={"summary": result["summary"], "duration_ms": duration_ms})
        return result

    cleaned = _strip_fences(raw)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {"issues": [], "summary": f"Reviewer {worker_index} output was not valid JSON, discarded."}

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = result.get("summary", "") or f"{len(result.get('issues', []))} issue(s) found"
    emit_event("agent_done", session_id=session_id, agent=agent_name, tier=tier,
               payload={"summary": summary, "duration_ms": duration_ms})
    return result


def run_reviewer(session_id: str = None, tier: int = None):
    submitted_code = read(KEYS["submitted_code"])
    if not submitted_code:
        raise ValueError("No submitted_code found in memory. Run the Code Writers first.")

    user_prompt = (
        "Here is the submitted code from all modules this cycle:\n\n"
        + json.dumps(submitted_code, indent=2)
    )

    member_reviews = [None] * len(GROQ_KEY_ENVS)
    with ThreadPoolExecutor(max_workers=len(GROQ_KEY_ENVS)) as executor:
        futures = {
            executor.submit(_run_one_worker, i + 1, key_env, user_prompt,
                             session_id=session_id, tier=tier): i
            for i, key_env in enumerate(GROQ_KEY_ENVS)
        }
        for future in as_completed(futures):
            i = futures[future]
            member_reviews[i] = future.result()

    review_notes = aggregate_reviews(member_reviews)
    write(KEYS["review_notes"], review_notes)
    return review_notes


if __name__ == "__main__":
    notes = run_reviewer()
    print(json.dumps(notes, indent=2))