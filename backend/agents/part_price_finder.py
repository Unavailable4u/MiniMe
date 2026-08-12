"""
agents/part_price_finder.py — Bangladesh hardware part price lookup.

Same shape as agents/academic_search.py: plain HTTP calls to a free
search API (no LLM), for each of a fixed vendor-domain allowlist, then
ONE LLM extraction call per part to turn snippets into structured
{name, price_bdt, vendor, url} entries. Results are cached (see
eo/price_cache.py) since BD_VENDOR_DOMAINS searches are the same query
shape every time and prices don't move minute to minute.

NOTE on the CHAIN below vs. the original guide draft: generate_text()
has no built-in default fallback chain — it walks exactly the list you
pass it, and an empty list means the loop body never runs, so it raises
RuntimeError immediately ("Last error: None") every single call. Every
other agent in this codebase defines its own module-level CHAIN
(see utils/llm_client.py's own docstring example); this one does the
same rather than relying on a "default" that doesn't exist.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from utils.web_search import search as web_search
from eo.price_cache import get_cached_price, set_cached_price

BD_VENDOR_DOMAINS = [
    "startech.com.bd", "ryanscomputers.com", "techlandbd.com",
    "ultrasource.com.bd", "daraz.com.bd", "pickaboo.com",
]

# FALLBACK_CHAIN: last-resort static chain, used only if find_price() is
# called with no chain_override AND eo/dynamic_chain.py's
# build_fallback_chain() comes back empty (every registered account
# excluded/cooling down -- should be very rare). This used to be the
# ONLY chain find_price() ever tried, which meant every part in
# hardware_speccer.py's sequential price-lookup loop fought over these
# exact same 2 accounts one at a time -- see find_price()'s own
# chain_override parameter below, which is what actually fixes that.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    # FIX — bug audit: "llama-3.3-70b" was retired from Cerebras' catalog
    # (confirmed via GET /v1/models: only gpt-oss-120b/gemma-4-31b/
    # zai-glm-4.7 served now). See agents/generic_worker.py's
    # PROVIDER_DEFAULT_MODEL comment for the full trace.
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_9"},
    # Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
    # its fallback step is removed here, not replaced. The Groq -> Cerebras
    # redundancy above is unchanged.
]

EXTRACTION_PROMPT = """You are given raw search snippets about a hardware
part from Bangladeshi electronics retailers. Extract ONLY what's directly
stated in the snippets — never invent a price or product name that isn't
present. Return strict JSON:
{"found": true|false, "listings": [{"vendor","product_name","price_bdt","url"}]}
If nothing relevant was found, return {"found": false, "listings": []}.
"""


def find_price(part_name: str, force_refresh: bool = False, chain_override: list = None,
                agent_name: str = "part_price_finder") -> dict:
    """Returns {"part_name", "listings": [...], "checked_at", "cached": bool}.

    chain_override: Bug fix (2026-08-12) -- NEW. When a caller is running
    many find_price() calls in parallel (e.g.
    agents/hardware_speccer.py's _populate_prices(), now a
    ThreadPoolExecutor pool, one worker per account), each worker thread
    passes its OWN chain here so every parallel call uses a different
    account instead of all of them racing for the module-level
    FALLBACK_CHAIN's single Groq key. None (default) keeps every existing
    caller's behavior exactly as before -- FALLBACK_CHAIN is used, same
    as the old always-CHAIN behavior.

    agent_name: NEW, purely a usage-logging label so per-worker calls
    show up distinctly (e.g. "part_price_finder_2") instead of all N
    parallel calls logging under the same generic name. Defaults to the
    original "part_price_finder" -- unchanged for existing callers.
    """
    if not force_refresh:
        cached = get_cached_price(part_name)
        if cached:
            return {**cached, "cached": True}

    # Bug fix (2026-08-12): the 6 BD_VENDOR_DOMAINS lookups are plain
    # HTTP calls (no LLM key involved), so unlike the extraction call
    # below there's no quota to protect by keeping them sequential --
    # they were only ever serialized because nothing had parallelized
    # them yet. Fanning them out with a small thread pool cuts this
    # part's latency by roughly 6x without touching any account's quota.
    # Traceability-per-vendor (the reason this is still one domain per
    # call, not one combined web_search() call) is unchanged.
    from concurrent.futures import ThreadPoolExecutor

    def _search_domain(domain: str) -> list:
        return web_search(f"{part_name} price", domains=[domain], agent_name=agent_name)

    snippets = []
    with ThreadPoolExecutor(max_workers=len(BD_VENDOR_DOMAINS)) as pool:
        for results in pool.map(_search_domain, BD_VENDOR_DOMAINS):
            snippets.extend(results)

    if not snippets:
        result = {"part_name": part_name, "listings": [], "checked_at": _now_iso()}
        set_cached_price(part_name, result)
        return {**result, "cached": False}

    snippet_text = "\n\n".join(f"{s['url']}\n{s['snippet']}" for s in snippets[:12])
    raw = generate_text(
        system_prompt=EXTRACTION_PROMPT,
        user_content=f"Part: {part_name}\n\nSnippets:\n{snippet_text}",
        chain=chain_override or FALLBACK_CHAIN,
        agent_name=agent_name,
    )
    parsed = _safe_json(raw) or {"found": False, "listings": []}
    result = {
        "part_name": part_name,
        "listings": parsed.get("listings", []),
        "checked_at": _now_iso(),
    }
    set_cached_price(part_name, result)
    return {**result, "cached": False}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _safe_json(text: str) -> dict | None:
    import json, re
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


if __name__ == "__main__":
    # Manual smoke test — step 1 of the build order: get find_price()
    # working end-to-end for one hardcoded part before touching the UI.
    import json
    result = find_price("HolyBro Kakute H7 V2")
    print(json.dumps(result, indent=2))