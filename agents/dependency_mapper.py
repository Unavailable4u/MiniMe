"""
agents/dependency_mapper.py — Module Dependency Mapper (Part 4, agent #4
of the v5 Master Blueprint).

Provider: Cloudflare Workers AI, key #1 (CLOUDFLARE_ACCOUNT_ID_1 /
CLOUDFLARE_API_KEY_1) -- same REST-call pattern already used by
reviewer.py's and fixer_pool.py's Cloudflare fallbacks, just as the
primary provider here instead of a fallback.

Runs after code_writers.py, before test_writer.py: the point of mapping
dependencies before tests/review is so both of those steps can see which
modules import/call which others, instead of reviewing each module in
isolation.

Output shape, written to KEYS["dependency_map"]:
{
  "module_name": {"depends_on": ["other_module", ...], "notes": "..."}
}
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry

load_dotenv()

# Same model choice as fixer_pool.py's Cloudflare fallback -- confirmed on
# Cloudflare's JSON Mode model list, unlike the smaller 8B instruct model.
CLOUDFLARE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
CLOUDFLARE_ACCOUNT_ID_ENV = "CLOUDFLARE_ACCOUNT_ID_1"
CLOUDFLARE_TOKEN_ENV = "CLOUDFLARE_API_KEY_1"

SYSTEM_PROMPT = """You are a static-dependency analyst. You will be given JSON
containing several code modules. For each module, list which OTHER modules
(by name, from the set given) it appears to import, call, or otherwise
depend on. Only use module names from the given set -- never invent one.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:
{
  "module_name": {"depends_on": ["other_module_name"], "notes": "one short sentence"}
}
Include every module given, even if depends_on is an empty list.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _call_cloudflare(user_prompt: str) -> str:
    account_id = os.getenv(CLOUDFLARE_ACCOUNT_ID_ENV)
    token = os.getenv(CLOUDFLARE_TOKEN_ENV)
    if not account_id or not token:
        raise RuntimeError(f"{CLOUDFLARE_ACCOUNT_ID_ENV}/{CLOUDFLARE_TOKEN_ENV} not set")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{CLOUDFLARE_MODEL}"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True) and data.get("errors"):
        raise RuntimeError(f"Cloudflare error: {data['errors']}")
    return data["result"]["response"]


def run() -> dict:
    submitted_code = read(KEYS["submitted_code"], default={})
    if not submitted_code:
        write(KEYS["dependency_map"], {})
        return {}

    preview = {
        name: (mod.get("code", "")[:800] if isinstance(mod, dict) else str(mod)[:800])
        for name, mod in submitted_code.items()
    }
    user_prompt = json.dumps({"modules": preview}, indent=2)

    raw_text = call_with_retry(
        lambda: _call_cloudflare(user_prompt),
        agent_name="Dependency Mapper",
    )
    dep_map = json.loads(_strip_fences(raw_text))
    write(KEYS["dependency_map"], dep_map)
    return dep_map


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
