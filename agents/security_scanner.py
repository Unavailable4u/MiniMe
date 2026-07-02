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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from utils.llm_client import generate_text

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


def _scan_one(module_name: str, code: str, slot: int, session_id: str = None, tier: int = None) -> tuple:
    """
    Stage 6 step 5: fires agent_start/agent_done labeled scanner_{slot}.
    Uses the Cloudflare slot number (4-8) rather than a plain worker
    index, since that's what actually identifies which account did the
    work -- and slots get reused round-robin when there are more than 5
    modules, same reasoning as code_writers.py's key-slot labeling.

    NOTE: the old hand-rolled call used Cloudflare's json_object response
    mode as a light steering hint (not a strict schema, unlike
    fixer_pool.py's old json_schema mode -- there's nothing lost here that
    wasn't already just "asking nicely" via the prompt). generate_text()
    has no equivalent parameter, so this now relies purely on
    SYSTEM_PROMPT's instructions plus the broad except below, same
    tolerance as before.
    """
    agent_name = f"scanner_{slot}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, tier=tier,
               payload={"label": f"Scanner {slot} — {module_name}"})
    started = time.monotonic()

    chain = [
        {"provider": "cloudflare", "model": CLOUDFLARE_MODEL,
         "account_id_env": f"CLOUDFLARE_ACCOUNT_ID_{slot}", "token_env": f"CLOUDFLARE_API_KEY_{slot}"},
    ]

    try:
        raw_text = generate_text(
            SYSTEM_PROMPT,
            json.dumps({"module": module_name, "code": code[:6000]}),
            chain,
            agent_name=agent_name,
            session_id=session_id,
            tier=tier,
        )
        result = json.loads(_strip_fences(raw_text))
    except Exception as exc:
        result = {"findings": [], "error": str(exc)}

    duration_ms = int((time.monotonic() - started) * 1000)
    findings = result.get("findings", [])
    if result.get("error"):
        summary = f"scan failed: {result['error']}"
    elif findings:
        summary = f"{len(findings)} finding(s)"
    else:
        summary = "no findings"
    emit_event("agent_done", session_id=session_id, agent=agent_name, tier=tier,
               payload={"summary": summary, "duration_ms": duration_ms})
    return module_name, result


def run(session_id: str = None, tier: int = None) -> dict:
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
            futures[executor.submit(_scan_one, name, code, slot, session_id=session_id, tier=tier)] = name
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result

    write(KEYS["security_scan_results"], results)
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))