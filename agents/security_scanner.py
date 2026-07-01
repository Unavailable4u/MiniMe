"""
agents/security_scanner.py — Security/Dependency Scanner Pool (Part 4,
agent #12 of the v5 Master Blueprint).

Provider: Cloudflare Workers AI, 5 parallel workers, keys #4-#8
(CLOUDFLARE_ACCOUNT_ID_4..8 / CLOUDFLARE_API_KEY_4..8) -- same account/key
pattern as reviewer.py and fixer_pool.py's Cloudflare fallbacks, but used
here as the PRIMARY provider for a full 5-worker pool, one worker per
module (round-robin if there are more modules than workers).

Runs after structure_architect.py, before file_manager.py: scan the final
code right before it's written to disk, not before Fixer Pool has had a
chance to patch anything (scanning pre-fix code would just re-flag issues
the Fixer already resolved).

Output, written to KEYS["security_scan_results"]:
{
  "module_name": {"findings": [{"severity": "...", "description": "..."}]}
}
"""
import os
import sys
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

CLOUDFLARE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
CLOUDFLARE_KEY_SLOTS = [4, 5, 6, 7, 8]

SYSTEM_PROMPT = """You are a security auditor. You will be given one code
module. List any security issues: injection risks, hardcoded secrets,
unsafe deserialization, missing input validation, unsafe dependency usage,
path traversal, or similar. Be specific. If there are none, return an
empty findings list -- do not invent issues to have something to say.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:
{
  "findings": [{"severity": "critical|moderate|minor", "description": "..."}]
}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _call_cloudflare(slot: int, user_prompt: str) -> str:
    account_id = os.getenv(f"CLOUDFLARE_ACCOUNT_ID_{slot}")
    token = os.getenv(f"CLOUDFLARE_API_KEY_{slot}")
    if not account_id or not token:
        raise RuntimeError(f"CLOUDFLARE_ACCOUNT_ID_{slot}/CLOUDFLARE_API_KEY_{slot} not set")
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


def _scan_one(module_name: str, code: str, slot: int) -> tuple:
    try:
        raw_text = _call_cloudflare(slot, json.dumps({"module": module_name, "code": code[:6000]}))
        result = json.loads(_strip_fences(raw_text))
    except Exception as exc:
        return module_name, {"findings": [], "error": str(exc)}
    return module_name, result


def run() -> dict:
    submitted_code = read(KEYS["fixed_code"], default=None) or read(KEYS["submitted_code"], default={})
    if not submitted_code:
        write(KEYS["security_scan_results"], {})
        return {}

    modules = list(submitted_code.items())
    results = {}
    with ThreadPoolExecutor(max_workers=len(CLOUDFLARE_KEY_SLOTS)) as executor:
        futures = {}
        for i, (name, data) in enumerate(modules):
            code = data.get("code", "") if isinstance(data, dict) else str(data)
            slot = CLOUDFLARE_KEY_SLOTS[i % len(CLOUDFLARE_KEY_SLOTS)]
            futures[executor.submit(_scan_one, name, code, slot)] = name
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result

    write(KEYS["security_scan_results"], results)
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
