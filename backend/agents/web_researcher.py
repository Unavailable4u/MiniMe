"""
agents/web_researcher.py — the E1 research role (task 13c).

REAL_ACTION_ROLES tool agent, modeled directly on academic_search.py's
shape (see that module's own docstring for the shared convention this
follows): zero LLM calls, plain HTTP calls to free search backends, then
each result lands as a Part-0-style graph node (node_type="source",
section="research", same as academic_search.py's own papers) via
eo/knowledge_graph.write_node -- this is the wiring note the guide itself
calls out as the reason to copy academic_search.py's shape rather than
part_price_finder.py's (search snippets -> one LLM extraction call)
shape.

Domain scope is a caller-supplied parameter with a few named presets
(see SCOPE_PRESETS below):
  - "general"    no domain restriction -- best-available broad web
                 search, via utils/web_search.search()'s own
                 Tavily-then-Exa fallback.
  - "forum"      Reddit + a couple of adjacent forums (FORUM_DOMAINS).
  - "news"       a fixed allowlist of major outlets (NEWS_DOMAINS).
  - "hackernews" its own dedicated fetch function -- free Algolia HN
                 Search API, no key -- one function per source, same
                 shape academic_search.py's SOURCE_FNS uses for
                 arXiv/CrossRef/OpenAlex/Semantic Scholar. This is NOT a
                 Tavily/Exa domain-scoped call: HN's own search API is
                 both free and already scoped to exactly what
                 "hackernews" as a source means, so routing it through
                 utils/web_search.search() would just be worse (no
                 points/comment-count metadata, no story-vs-comment
                 distinction).

Caching: every (scope, query) pair goes through eo/research_cache.py
first (task 13b) -- a cache hit returns the SAME report shape straight
from the cache and skips the network call and all node-writing entirely,
so the same research task fired twice in close succession doesn't
double-write duplicate graph nodes.

Result written to KEYS["web_researcher_report"]:
{
  "sources": [{"source_id", "node_id", "title", "url", "snippet", "scope"}],
  "scope": "<the scope that was actually used>",
  "cached": bool,
  "summary": "...",
}
"""
import os
import sys
import json
import requests
from urllib.parse import urlparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, get_current_app_slug
from eo.knowledge_graph import write_node
from eo.research_cache import get_cached_research, set_cached_research
from utils.web_search import search as web_search

REQUEST_TIMEOUT = 15
MAX_RESULTS = 8

HN_SEARCH_URL = "http://hn.algolia.com/api/v1/search"

# Reddit + a COUPLE of adjacent forums -- the guide's own phrasing for
# the "forum" scope, not just reddit.com alone.
FORUM_DOMAINS = ["reddit.com", "stackexchange.com", "quora.com"]

# Fixed allowlist for "news" scope. Short and curated, not exhaustive --
# same "traceable to a specific known source" reasoning
# part_price_finder.py's BD_VENDOR_DOMAINS uses, just for news outlets
# instead of BD vendors. Widened past the original 4 (Reuters/AP/BBC/NPR
# alone under-covers real news diversity -- a story can easily be missed
# entirely if it wasn't picked up by exactly those four), while still
# staying to well-known, broadly high-trust wire services and major
# outlets rather than an open-ended list that starts admitting anything.
NEWS_DOMAINS = [
    "reuters.com", "apnews.com", "bbc.com", "npr.org",
    "theguardian.com", "aljazeera.com", "nytimes.com", "wsj.com",
    "bloomberg.com", "economist.com",
]

# scope name -> domains list to pass into utils/web_search.search(), or
# None for "general" (no restriction -- utils/web_search.search()
# already handles the Tavily-then-Exa fallback with no domains param).
# "hackernews" is deliberately NOT in this dict: it never reaches
# utils/web_search.search() at all -- see _search_hackernews() and
# run()'s own branch below.
SCOPE_PRESETS = {
    "general": None,
    "forum": FORUM_DOMAINS,
    "news": NEWS_DOMAINS,
}


def _workspace_id() -> str:
    # Same session-isolation reasoning as academic_search.py's own
    # _workspace_id() -- the graph is scoped per-workspace, not global.
    return get_current_app_slug() or read(KEYS["original_idea"], default="untitled")


def _search_hackernews(query: str, limit: int) -> list[dict]:
    """Free Algolia HN Search API, no key required. Own function, same
    "one function per source" shape SOURCE_FNS uses in
    academic_search.py -- HN is a distinct source with its own metadata
    shape (points, comment count, story vs. comment text), not a
    Tavily/Exa domain scope."""
    try:
        resp = requests.get(
            HN_SEARCH_URL,
            params={"query": query, "tags": "story", "hitsPerPage": limit},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as exc:
        print(f"  [Web Researcher] Hacker News failed: {exc}")
        return []
    results = []
    for h in hits:
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        title = h.get("title") or "Untitled"
        snippet_bits = [
            f"{h['points']} points" if h.get("points") is not None else None,
            f"{h['num_comments']} comments" if h.get("num_comments") is not None else None,
            h.get("story_text") or "",
        ]
        results.append({
            "url": url, "title": title,
            "snippet": " | ".join(b for b in snippet_bits if b),
        })
    return results


def _title_from_url(url: str) -> str:
    # Last-resort fallback only, for the rare case a provider genuinely
    # returns no title at all -- both utils/web_search.py providers
    # (Tavily and Exa) DO carry a real page title in their response
    # (confirmed against a live run: Exa's own response included e.g.
    # "Best Mechanical Keyboards 2026 - Honest Picks by Layout and
    # Budget", not just a bare URL), so this should rarely actually
    # fire. It fired on EVERY result during this module's first live
    # test because _search_tavily()/_search_exa() in utils/web_search.py
    # weren't extracting the title field from either provider's response
    # at all -- fixed there (see that module's own dict now including
    # "title"), so this fallback goes back to being the edge case it was
    # meant to be, not the common path.
    return urlparse(url).netloc or url


def run(task_text: str = None, session_id: str = None, tier: int = None,
        scope: str = "general", force_refresh: bool = False) -> dict:
    """scope: one of SCOPE_PRESETS' keys ("general"/"forum"/"news"), or
    "hackernews" (handled separately -- see _search_hackernews())."""
    query = (task_text or "").strip()
    if not query:
        report = {"sources": [], "scope": scope, "cached": False,
                   "summary": "No search query provided."}
        write(KEYS["web_researcher_report"], report)
        return report

    if scope not in SCOPE_PRESETS and scope != "hackernews":
        print(f"  [Web Researcher] Unknown scope {scope!r}, falling back to 'general'.")
        scope = "general"

    if not force_refresh:
        cached = get_cached_research(scope, query)
        if cached:
            write(KEYS["web_researcher_report"], {**cached, "cached": True})
            return {**cached, "cached": True}

    workspace_id = _workspace_id()

    if scope == "hackernews":
        raw_results = _search_hackernews(query, MAX_RESULTS)
    else:
        domains = SCOPE_PRESETS[scope]
        raw = web_search(query, domains=domains, max_results=MAX_RESULTS, agent_name="web_researcher")
        raw_results = [{"url": r["url"], "snippet": r.get("snippet", ""),
                         "title": r.get("title") or _title_from_url(r["url"])} for r in raw]

    sources_out = []
    for r in raw_results:
        if not r.get("url"):
            continue
        node_id = write_node(
            workspace_id=workspace_id, section="research", node_type="source",
            title=r.get("title") or r["url"],
            content=r.get("snippet") or r.get("title") or "",
            created_by="web_researcher",
            tags=[scope],
            session_id=session_id, tier=tier,
        )
        sources_out.append({
            "source_id": r["url"], "node_id": node_id,
            "title": r.get("title") or r["url"], "url": r["url"],
            "snippet": r.get("snippet", ""), "scope": scope,
        })

    report = {
        "sources": sources_out, "scope": scope, "cached": False,
        "summary": f"{len(sources_out)} source(s) found for scope '{scope}'.",
    }
    set_cached_research(scope, query, report)
    write(KEYS["web_researcher_report"], report)
    return report


if __name__ == "__main__":
    # Manual smoke test -- mirrors academic_search.py's own __main__
    # block. One hardcoded query/scope before touching registry wiring.
    print(json.dumps(run(task_text="best mechanical keyboards 2026", scope="forum",
                          force_refresh=True), indent=2))
