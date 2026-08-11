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

# Same three-provider free-tier chain utils/llm_client.py's own docstring
# shows as the standard shape (OpenAI-SDK-compatible providers, each with
# a single key_env). This is a small extraction call (a dozen short
# snippets -> one JSON object), so 70B-class free-tier models are more
# than enough — no need to reach for anything bigger here.
CHAIN = [
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


def find_price(part_name: str, force_refresh: bool = False) -> dict:
    """Returns {"part_name", "listings": [...], "checked_at", "cached": bool}."""
    if not force_refresh:
        cached = get_cached_price(part_name)
        if cached:
            return {**cached, "cached": True}

    snippets = []
    for domain in BD_VENDOR_DOMAINS:
        query = f"{part_name} price"
        # One domain per call, on purpose: BD_VENDOR_DOMAINS is a fixed
        # vendor allowlist and results need to stay traceable to a
        # specific vendor, so this loops web_search(domains=[domain])
        # rather than passing the whole list in one call. General
        # research callers with a fixed *scope* (not per-item vendor
        # tracking) should pass their whole domain list in one call
        # instead -- see utils/web_search.py's own docstring.
        results = web_search(query, domains=[domain], agent_name="part_price_finder")
        snippets.extend(results)

    if not snippets:
        result = {"part_name": part_name, "listings": [], "checked_at": _now_iso()}
        set_cached_price(part_name, result)
        return {**result, "cached": False}

    snippet_text = "\n\n".join(f"{s['url']}\n{s['snippet']}" for s in snippets[:12])
    raw = generate_text(
        system_prompt=EXTRACTION_PROMPT,
        user_content=f"Part: {part_name}\n\nSnippets:\n{snippet_text}",
        chain=CHAIN,
        agent_name="part_price_finder",
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