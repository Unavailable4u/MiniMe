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
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from utils.web_search import search as web_search
from eo.price_cache import get_cached_price, set_cached_price

# Bug fix (pricing-audit root cause 2): every failure in this module used
# to be silently swallowed -- a genuine "no listing exists" and a broken
# LLM-extraction call both landed on the exact same {"found": False,
# "listings": []} result, with no way to tell them apart after the fact.
# This logger makes every non-success path here greppable (search
# "part_price_finder" in logs) instead of invisible.
log = logging.getLogger(__name__)

BD_VENDOR_DOMAINS = [
    "startech.com.bd", "ryanscomputers.com", "techlandbd.com",
    "ultrasource.com.bd", "daraz.com.bd", "pickaboo.com",
]

# INTL_VENDOR_DOMAINS: NEW (T2b, step 17) -- second-tier fallback used
# only when every BD_VENDOR_DOMAINS lookup comes back with nothing for a
# part. The BD-only scope was the single biggest lever on the actual
# found-rate: most parts that showed "not found" here priced cleanly on
# these two sites instead. See _find_international_fallback() below.
INTL_VENDOR_DOMAINS = ["aliexpress.com", "ebay.com"]

# FALLBACK_CHAIN: last-resort static chain, used only if find_price() is
# called with no chain_override AND eo/dynamic_chain.py's
# build_fallback_chain() comes back empty (every registered account
# excluded/cooling down -- should be very rare). This used to be the
# ONLY chain find_price() ever tried, which meant every part in
# hardware_speccer.py's sequential price-lookup loop fought over these
# exact same 2 accounts one at a time -- see find_price()'s own
# chain_override parameter below, which is what actually fixes that.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY"},
    # OR-3d: Cerebras -> OpenRouter, same slot (was CEREBRAS_API_KEY_9,
    # see OR-2's .env.example note) -- "openrouter/free" is OpenRouter's
    # own auto-router, not a pinned model slug (see utils/llm_client.py's
    # OR-1 notes), so there's no equivalent of "gpt-oss-120b" to pin here.
    {"provider": "openrouter", "model": "openrouter/free", "key_env": "OPENROUTER_API_KEY_9"},
    # Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
    # its fallback step is removed here, not replaced. The Groq -> OpenRouter
    # redundancy above is unchanged.
]

EXTRACTION_PROMPT = """You are given raw search snippets about a hardware
part from Bangladeshi electronics retailers. Extract ONLY what's directly
stated in the snippets — never invent a price or product name that isn't
present. "price_bdt" MUST be a plain number with no currency symbol, no
thousands separators, and no text like "N/A" or "contact for price" — if
the snippet doesn't state a clear numeric price, use JSON null instead of
a string. Return strict JSON:
{"found": true|false, "listings": [{"vendor","product_name","price_bdt","url"}]}
If nothing relevant was found, return {"found": false, "listings": []}.
"""


def _sanitize_listings(listings: list) -> list:
    """Bug fix (T2b, step 17): the prompt above tells the LLM to return
    price_bdt as a plain number or null, but nothing enforced that
    before this — a stray "1,200" or "contact for price" string could
    survive straight into a part's estimated_price_bdt field, which is
    exactly what turns PartsTable.jsx's BOM total into NaN. Same
    "validate the LLM's JSON after parsing" pattern hardware_speccer.py
    already uses for its own strict-JSON contract: coerce anything that
    isn't int/float (or is a bool, which is technically an int subclass
    in Python) to None rather than writing a poisoning value into the
    part.
    """
    for listing in listings:
        price = listing.get("price_bdt")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            listing["price_bdt"] = None
    return listings


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
        # NEW (T2b, step 17): all six BD vendor domains came back empty
        # -- try AliExpress/eBay before giving up and marking the part
        # unpriced, instead of stopping here as before.
        return _find_international_fallback(part_name, chain_override, agent_name)

    snippet_text = "\n\n".join(f"{s['url']}\n{s['snippet']}" for s in snippets[:12])
    raw = generate_text(
        system_prompt=EXTRACTION_PROMPT,
        user_content=f"Part: {part_name}\n\nSnippets:\n{snippet_text}",
        chain=chain_override or FALLBACK_CHAIN,
        agent_name=agent_name,
        # Bug fix (pricing-audit root cause 1): this is a single-shot JSON
        # classifier, same shape as eo/inspector.py's own call. Without
        # this, a reasoning model (e.g. FALLBACK_CHAIN's qwen/qwen3.6-27b)
        # that gets truncated mid-<think> and told to "continue
        # seamlessly" just finishes its train of thought and never emits
        # the JSON -- see generate_text()'s own allow_continuation
        # docstring. A truncation here should discard the partial output
        # and retry fresh on the next chain step, not splice onto an
        # empty <think> block.
        allow_continuation=False,
    )
    parsed = _safe_json(raw)
    if parsed is None:
        log.warning(
            "find_price: unparseable JSON from LLM extraction — part=%r branch=bd "
            "raw=%r",
            part_name, raw[:500] if raw else raw,
        )
        parsed = {"found": False, "listings": []}
    result = {
        "part_name": part_name,
        "listings": _sanitize_listings(parsed.get("listings", [])),
        "checked_at": _now_iso(),
    }
    set_cached_price(part_name, result)
    return {**result, "cached": False}


def _find_international_fallback(part_name: str, chain_override: list, agent_name: str) -> dict:
    """T2b, step 17: second-tier search scoped to INTL_VENDOR_DOMAINS,
    called only when every BD_VENDOR_DOMAINS lookup came back empty.
    Same fan-out / extraction / sanitize shape as the BD path above,
    just a second domain list and a second extraction call -- cached
    under tier="intl" so a later BD listing, if one ever appears, can
    still take priority over a cached international one.
    """
    cached = get_cached_price(part_name, tier="intl")
    if cached:
        return {**cached, "cached": True}

    from concurrent.futures import ThreadPoolExecutor

    def _search_domain(domain: str) -> list:
        return web_search(f"{part_name} price", domains=[domain], agent_name=agent_name)

    snippets = []
    with ThreadPoolExecutor(max_workers=len(INTL_VENDOR_DOMAINS)) as pool:
        for results in pool.map(_search_domain, INTL_VENDOR_DOMAINS):
            snippets.extend(results)

    if not snippets:
        result = {"part_name": part_name, "listings": [], "checked_at": _now_iso()}
        set_cached_price(part_name, result, tier="intl")
        return {**result, "cached": False}

    snippet_text = "\n\n".join(f"{s['url']}\n{s['snippet']}" for s in snippets[:12])
    raw = generate_text(
        system_prompt=EXTRACTION_PROMPT,
        user_content=f"Part: {part_name}\n\nSnippets:\n{snippet_text}",
        chain=chain_override or FALLBACK_CHAIN,
        agent_name=agent_name,
        # Same fix as find_price()'s BD-path call above -- see the
        # comment there.
        allow_continuation=False,
    )
    parsed = _safe_json(raw)
    if parsed is None:
        log.warning(
            "find_price: unparseable JSON from LLM extraction — part=%r branch=intl "
            "raw=%r",
            part_name, raw[:500] if raw else raw,
        )
        parsed = {"found": False, "listings": []}
    result = {
        "part_name": part_name,
        "listings": _sanitize_listings(parsed.get("listings", [])),
        "checked_at": _now_iso(),
    }
    set_cached_price(part_name, result, tier="intl")
    return {**result, "cached": False}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _safe_json(text: str) -> dict | None:
    import json
    import re
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