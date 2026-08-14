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
  - "general"      no domain restriction -- best-available broad web
                   search, via utils/web_search.search()'s own
                   Tavily-then-Exa fallback.
  - "forum"        Reddit + a couple of adjacent forums (FORUM_DOMAINS).
  - "news"         a fixed allowlist of major outlets (NEWS_DOMAINS).
  - "hackernews"   its own dedicated fetch function -- free Algolia HN
                   Search API, no key -- one function per source, same
                   shape academic_search.py's SOURCE_FNS uses for
                   arXiv/CrossRef/OpenAlex/Semantic Scholar. This is NOT
                   a Tavily/Exa domain-scoped call: HN's own search API
                   is both free and already scoped to exactly what
                   "hackernews" as a source means, so routing it through
                   utils/web_search.search() would just be worse (no
                   points/comment-count metadata, no story-vs-comment
                   distinction).
  - "hw_reference" same no-domain-restriction search as "general"
                   (published builds/app-notes aren't confined to a
                   fixed domain list any more than "general" results
                   are), but routed to eo/hw_reference.write_hw_reference()
                   instead of eo/knowledge_graph.write_node() -- see
                   _index_hw_references() below. Kept as its own preset
                   rather than overloading "general" so a caller's scope
                   choice alone decides which graph a result lands in,
                   no separate flag needed.

Phase 0 of the Mech/Enclosure implementation guide: when scope is
"hw_reference", run() also expects `generic_name` (and optionally
`aliases`) for the component the caller is gathering precedent for --
the SAME canonical vocabulary hardware_speccer.py's
_ensure_generic_names() already guarantees exists on every part, not
whatever wording the source article itself uses. Each result is
resolved against component_dimension_table.py's own
lookup_curated_dimensions() -- using the caller's generic_name/aliases,
never text mined from the article -- so every indexed entry is
findable later under one canonical name instead of fragmenting across
near-duplicate labels. This mirrors the retrieval side (Patch 0.3/0.4):
both query by a part's own generic_name/aliases, never by ad-hoc
wording.

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
from eo.hw_reference import write_hw_reference
from eo.research_cache import get_cached_research, set_cached_research
from utils.web_search import search as web_search
from agents.component_dimension_table import lookup_curated_dimensions

REQUEST_TIMEOUT = 15
MAX_RESULTS = 8

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"

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
    # No domain restriction, same reasoning as "general" -- see the
    # module docstring's "hw_reference" bullet for why this still gets
    # its own preset instead of just reusing "general".
    "hw_reference": None,
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


def _index_hw_references(
    raw_results: list[dict], workspace_id: str, generic_name: str,
    aliases: list | None, session_id: str = None, tier: int = None,
) -> list[dict]:
    """Patch 0.2's indexing path. Resolves `generic_name` (the caller's
    own canonical name for the component it's gathering precedent for,
    not text mined from the results) against
    component_dimension_table.lookup_curated_dimensions() once, then
    writes every result under that same resolved name via
    eo/hw_reference.write_hw_reference() -- mirrors write_node()'s loop
    in run() below, just routed to the hw_ref: prefix instead of node:.

    Failure posture matches the module docstring's degrade-don't-fail
    stance throughout: an unresolved generic_name (no curated-table
    match) still indexes -- dimension_ref_id just stays absent -- since
    G1a's curated table is deliberately small/hand-curated (its own
    docstring's words) and not every real component will be in it yet;
    refusing to index anything outside the table would make Phase 0
    only as useful as G1a's current coverage instead of growing ahead
    of it. A per-result write_hw_reference() failure only drops that
    one entry, same as a single write_node() failure does for run()'s
    existing "general"/"forum"/"news" path.
    """
    dimension_match = lookup_curated_dimensions(generic_name, aliases)
    dimension_ref_id = dimension_match.get("dimension_ref_id") if dimension_match else None

    sources_out = []
    for r in raw_results:
        if not r.get("url"):
            continue
        ref_id = write_hw_reference({
            "workspace_id": workspace_id,
            "generic_name": generic_name,
            "aliases": aliases or [],
            "title": r.get("title") or r["url"],
            "content": r.get("snippet") or r.get("title") or "",
            "source_url": r["url"],
            "dimension_ref_id": dimension_ref_id,
            "created_by": "web_researcher",
            "session_id": session_id,
            "tier": tier,
        })
        sources_out.append({
            "source_id": r["url"], "node_id": ref_id,
            "title": r.get("title") or r["url"], "url": r["url"],
            "snippet": r.get("snippet", ""), "scope": "hw_reference",
            "generic_name": generic_name,
        })
    return sources_out


def run(task_text: str = None, session_id: str = None, tier: int = None,
        scope: str = "general", force_refresh: bool = False,
        generic_name: str = None, aliases: list = None) -> dict:
    """scope: one of SCOPE_PRESETS' keys ("general"/"forum"/"news"/
    "hw_reference"), or "hackernews" (handled separately -- see
    _search_hackernews()).

    generic_name/aliases: required when scope="hw_reference" -- the
    canonical component vocabulary this indexing run is for (see the
    module docstring's "hw_reference" bullet). Ignored for every other
    scope.
    """
    query = (task_text or "").strip()
    if not query:
        report = {"sources": [], "scope": scope, "cached": False,
                   "summary": "No search query provided."}
        write(KEYS["web_researcher_report"], report)
        return report

    if scope not in SCOPE_PRESETS and scope != "hackernews":
        print(f"  [Web Researcher] Unknown scope {scope!r}, falling back to 'general'.")
        scope = "general"

    if scope == "hw_reference" and not (generic_name or "").strip():
        # Same "degrade, don't hard-fail" posture as everywhere else in
        # this module -- a mis-called hw_reference run shouldn't crash
        # the caller, it just isn't indexable without a canonical name
        # to index under, so fall back to today's behavior.
        print("  [Web Researcher] scope='hw_reference' requires generic_name, falling back to 'general'.")
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

    if scope == "hw_reference":
        # Routed to eo/hw_reference.write_hw_reference() (hw_ref:
        # prefix), not write_node() (node: prefix) -- see the module
        # docstring's "hw_reference" bullet for why these stay on
        # separate id prefixes.
        sources_out = _index_hw_references(
            raw_results, workspace_id, generic_name.strip(), aliases,
            session_id=session_id, tier=tier,
        )
    else:
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
