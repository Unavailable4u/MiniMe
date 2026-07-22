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
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from eo.price_cache import get_cached_price, set_cached_price
from utils.llm_client import generate_text, log_usage

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
REQUEST_TIMEOUT = 12

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
    {"provider": "cerebras", "model": "llama-3.3-70b", "key_env": "CEREBRAS_API_KEY_9"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

EXTRACTION_PROMPT = """You are given raw search snippets about a hardware
part from Bangladeshi electronics retailers. Extract ONLY what's directly
stated in the snippets — never invent a price or product name that isn't
present. Return strict JSON:
{"found": true|false, "listings": [{"vendor","product_name","price_bdt","url"}]}
If nothing relevant was found, return {"found": false, "listings": []}.
"""


def _search_tavily(query: str, domain: str = None) -> list[dict]:
    """domain, if given, is passed as Tavily's own include_domains param —
    NOT baked into the query string as `site:domain`. Confirmed against
    Tavily's actual /search API: it has no query-operator parsing at all,
    so a `site:` prefix in `query` is just treated as literal search
    terms and silently ignored, which is why the first debug run
    returned generic web results instead of domain-scoped ones."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    try:
        payload = {
            "api_key": key, "query": query, "max_results": 3,
            "include_raw_content": False,
        }
        if domain:
            payload["include_domains"] = [domain]
        resp = requests.post(TAVILY_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        log_usage("tavily", "TAVILY_API_KEY", tokens=None, agent_name="part_price_finder")
        return [{"url": r["url"], "snippet": r.get("content", "")}
                for r in resp.json().get("results", [])]
        return [{"url": r["url"], "snippet": r.get("content", "")}
                for r in resp.json().get("results", [])]
    except Exception as exc:
        print(f"  [Part Price Finder] Tavily failed: {exc}")
        return []


def _search_brave(query: str) -> list[dict]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    try:
        resp = requests.get(BRAVE_URL, params={"q": query, "count": 3},
                             headers={"X-Subscription-Token": key},
                             timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [{"url": r["url"], "snippet": r.get("description", "")}
                for r in results]
    except Exception as exc:
        print(f"  [Part Price Finder] Brave failed: {exc}")
        return []


def find_price(part_name: str, force_refresh: bool = False) -> dict:
    """Returns {"part_name", "listings": [...], "checked_at", "cached": bool}."""
    if not force_refresh:
        cached = get_cached_price(part_name)
        if cached:
            return {**cached, "cached": True}

    snippets = []
    for domain in BD_VENDOR_DOMAINS:
        query = f"{part_name} price"
        # Tavily: domain scoping via include_domains (see _search_tavily).
        # Brave: kept as a site: prefix — Brave's API does parse standard
        # search operators in the query string, unlike Tavily, so this
        # one is left as-is for now. Worth re-verifying with its own
        # debug run once Brave is actually wired in as the fallback.
        results = _search_tavily(query, domain=domain) or _search_brave(f"site:{domain} {query}")
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