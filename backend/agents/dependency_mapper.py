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
import json
import os
import sys

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import KEYS, read, write
from relay.emitter import emit_event
from utils.llm_client import generate_text

load_dotenv()

# Same model choice as fixer_pool.py's Cloudflare fallback -- confirmed on
# Cloudflare's JSON Mode model list, unlike the smaller 8B instruct model.
# json_mode: True is critical here, not decorative -- see llm_client.py's
# _call_cloudflare_step() docstring. Routed through generate_text() instead
# of a hand-rolled request so this call actually gets usage-logged --
# previously it logged nothing.
#
# FALLBACK_CHAIN: last-resort static chain, used ONLY if
# eo/dynamic_chain.py's build_fallback_chain() below returns nothing at
# all (every registered account excluded/cooling down at once -- should
# be very rare). This used to be the ONLY chain this module ever tried
# (module-level CHAIN, one entry, one Cloudflare key/account with no
# fallback) -- see run()'s real call below, which now builds a live,
# quota-ranked, multi-provider chain instead (eo/registry.py tags
# CLOUDFLARE_ACCOUNT_ID_4 for "dependency_mapper", so this has a real
# sibling account to fall back to, not just this pool's whole-account
# quota-ranked degradation).
FALLBACK_CHAIN = [
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
        text = text.removeprefix("json")
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

    # perf audit §4.4 / priority #7: was double-wrapped in call_with_retry
    # on top of generate_text()'s own chain-walk fallback — a real
    # multi-provider outage retried the whole CHAIN up to 4 times with
    # real sleeps (1/2/4/8s) in between, on top of generate_text() already
    # having walked every step in CHAIN once per attempt. generate_text()
    # is the single source of retry/fallback behavior now.
    #
    # Bug fix (2026-08-12): deferred import -- see eo/dynamic_chain.py's
    # module docstring for why this can't be a module-level import.
    # Quota-ranked, cooldown-aware, spread across providers -- replaces
    # the old single-entry CHAIN that had nothing to fall back to when
    # its one Cloudflare account rate-limited.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("dependency_mapper") or FALLBACK_CHAIN

    raw_text = generate_text(SYSTEM_PROMPT, user_prompt, chain, agent_name="Dependency Mapper",
                              session_id=session_id, tier=tier, domain=domain)
    dep_map = json.loads(_strip_fences(raw_text))
    write(KEYS["dependency_map"], dep_map)
    emit_event("dependency_map", session_id, agent="dependency_mapper", payload={"map": dep_map})
    return dep_map


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))