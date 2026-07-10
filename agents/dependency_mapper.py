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
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry
from utils.llm_client import generate_text
from relay.emitter import emit_event

load_dotenv()

# Same model choice as fixer_pool.py's Cloudflare fallback -- confirmed on
# Cloudflare's JSON Mode model list, unlike the smaller 8B instruct model.
# json_mode: True is critical here, not decorative -- see llm_client.py's
# _call_cloudflare_step() docstring. Routed through generate_text() instead
# of a hand-rolled request so this call actually gets usage-logged --
# previously it logged nothing.
CHAIN = [
    {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
     "account_id_env": "CLOUDFLARE_ACCOUNT_ID_1", "token_env": "CLOUDFLARE_API_KEY_1",
     "json_mode": True},
]

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


def run(session_id: str = None, tier: int = None, domain: str = None) -> dict:
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
        lambda: generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Dependency Mapper",
                               session_id=session_id, tier=tier, domain=domain),
        agent_name="Dependency Mapper",
    )
    dep_map = json.loads(_strip_fences(raw_text))
    write(KEYS["dependency_map"], dep_map)
    emit_event("dependency_map", session_id, agent="dependency_mapper", payload={"map": dep_map})
    return dep_map


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))