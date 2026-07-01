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
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from groq import Groq, RateLimitError, APIStatusError, APIConnectionError, APITimeoutError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from agents.review_aggregator import aggregate_reviews

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

_TRANSIENT_ERRORS = (RateLimitError, APIStatusError, APIConnectionError, APITimeoutError)

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

_groq_client_cache = {}


def _get_groq_client(key_env: str):
    key = os.getenv(key_env)
    if not key:
        return None
    if key_env not in _groq_client_cache:
        _groq_client_cache[key_env] = Groq(api_key=key)
    return _groq_client_cache[key_env]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _call_groq(key_env: str, user_prompt: str) -> str:
    client = _get_groq_client(key_env)
    if client is None:
        raise RuntimeError(f"{key_env} not set")
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content or ""


def _call_cloudflare_fallback(user_prompt: str) -> str:
    account_id = os.getenv(CLOUDFLARE_ACCOUNT_ID_ENV)
    token = os.getenv(CLOUDFLARE_TOKEN_ENV)
    if not account_id or not token:
        raise RuntimeError(f"{CLOUDFLARE_ACCOUNT_ID_ENV} / {CLOUDFLARE_TOKEN_ENV} not set")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{CLOUDFLARE_MODEL}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {}).get("response", "")


def _run_one_worker(worker_index: int, key_env: str, user_prompt: str) -> dict:
    """
    Runs on one thread. Tries this worker's dedicated Groq key first,
    falls back to the shared Cloudflare account #2 if Groq is unavailable
    or errors transiently. Returns a parsed review dict, or an empty
    review (not a crash) if both fail -- one bad worker shouldn't sink
    the whole pool's aggregation.
    """
    raw = None
    try:
        raw = _call_groq(key_env, user_prompt)
    except _TRANSIENT_ERRORS as exc:
        print(f"  [Reviewer {worker_index}] Groq ({key_env}) failed "
              f"({exc.__class__.__name__}), falling back to Cloudflare...")
    except RuntimeError as exc:
        print(f"  [Reviewer {worker_index}] {exc}, falling back to Cloudflare...")

    if raw is None:
        try:
            raw = _call_cloudflare_fallback(user_prompt)
        except Exception as exc:
            print(f"  [Reviewer {worker_index}] Cloudflare fallback also failed: {exc}")
            return {"issues": [], "summary": f"Reviewer {worker_index} produced no output (both providers failed)."}

    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"issues": [], "summary": f"Reviewer {worker_index} output was not valid JSON, discarded."}


def run_reviewer():
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
            executor.submit(_run_one_worker, i + 1, key_env, user_prompt): i
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