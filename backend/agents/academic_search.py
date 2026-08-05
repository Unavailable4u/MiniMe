"""
agents/academic_search.py — Discovery (Part 3 §3.3).

REAL_ACTION_ROLES tool agent, modeled on duplication_checker.py's shape:
makes zero LLM calls, only plain HTTP requests to four free, no-key APIs
(Semantic Scholar, arXiv, CrossRef, OpenAlex), then writes results as
Part 0 knowledge-graph nodes/edges.

Per query: search the requested source(s), dedup by DOI/title, write each
paper as a node (node_type="source", section="research") and each
citation relationship as an edge (relation="cites"). Everything
downstream (citation_graph_builder's view, extraction, etc.) just reads
what this step wrote.

Result written to KEYS["academic_search_report"]:
{
  "papers": [{"paper_id", "node_id", "title", "authors", "year",
              "abstract", "doi", "venue", "citation_count", "source"}],
  "edges_written": <int>,
  "summary": "...",
}
"""
import os
import sys
import json
import xml.etree.ElementTree as ET
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, get_current_app_slug
from eo.knowledge_graph import write_node
from eo.graph_edges import create_edge

REQUEST_TIMEOUT = 15
MAX_RESULTS_PER_SOURCE = 10
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = (
    "title,authors,year,abstract,externalIds,venue,citationCount,"
    "references.title,references.externalIds,citations.title,citations.externalIds"
)
ARXIV_URL = "http://export.arxiv.org/api/query"
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_URL = "https://api.openalex.org/works"


def _workspace_id() -> str:
    # Same session-isolation reasoning as duplication_checker.py's
    # _app_slug() -- the graph is scoped per-workspace, not global.
    return get_current_app_slug() or read(KEYS["original_idea"], default="untitled")


def _search_semantic_scholar(query: str, limit: int) -> list:
    try:
        resp = requests.get(S2_SEARCH_URL, params={"query": query, "limit": limit, "fields": S2_FIELDS},
                             timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        print(f"  [Academic Search] Semantic Scholar failed: {exc}")
        return []
    papers = []
    for p in data:
        papers.append({
            "title": p.get("title"),
            "authors": [a.get("name") for a in p.get("authors", []) if a.get("name")],
            "year": p.get("year"), "abstract": p.get("abstract") or "",
            "doi": (p.get("externalIds") or {}).get("DOI"),
            "venue": p.get("venue"), "citation_count": p.get("citationCount"),
            "source": "semantic_scholar",
            "_cites": [r.get("title") for r in (p.get("references") or []) if r.get("title")],
        })
    return papers


def _search_arxiv(query: str, limit: int) -> list:
    try:
        resp = requests.get(ARXIV_URL, params={"search_query": f"all:{query}", "start": 0, "max_results": limit},
                             timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as exc:
        print(f"  [Academic Search] arXiv failed: {exc}")
        return []
    papers = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
        authors = [a.findtext("atom:name", default="", namespaces=ATOM_NS) for a in entry.findall("atom:author", ATOM_NS)]
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS) or ""
        papers.append({
            "title": title, "authors": [a for a in authors if a],
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "abstract": summary, "doi": None, "venue": "arXiv", "citation_count": None,
            "source": "arxiv", "_cites": [],
        })
    return papers


def _search_crossref(query: str, limit: int) -> list:
    try:
        resp = requests.get(CROSSREF_URL, params={"query": query, "rows": limit}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except Exception as exc:
        print(f"  [Academic Search] CrossRef failed: {exc}")
        return []
    papers = []
    for it in items:
        titles = it.get("title") or []
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip()
                   for a in it.get("author", []) if a.get("family")]
        date_parts = (it.get("issued", {}).get("date-parts") or [[None]])[0]
        venues = it.get("container-title") or []
        papers.append({
            "title": titles[0] if titles else None, "authors": authors,
            "year": date_parts[0] if date_parts else None, "abstract": "",
            "doi": it.get("DOI"), "venue": venues[0] if venues else None,
            "citation_count": it.get("is-referenced-by-count"), "source": "crossref", "_cites": [],
        })
    return papers


def _reconstruct_openalex_abstract(inverted_index: dict) -> str:
    # OpenAlex encodes abstracts as {word: [positions]} to avoid publisher
    # copyright on full-text reproduction -- rebuild the plain string.
    if not inverted_index:
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def _search_openalex(query: str, limit: int) -> list:
    try:
        resp = requests.get(OPENALEX_URL, params={"search": query, "per_page": limit}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        print(f"  [Academic Search] OpenAlex failed: {exc}")
        return []
    papers = []
    for r in results:
        authors = [a.get("author", {}).get("display_name") for a in r.get("authorships", []) if a.get("author")]
        venue = (r.get("primary_location") or {}).get("source") or {}
        doi = (r.get("doi") or "").replace("https://doi.org/", "") or None
        papers.append({
            "title": r.get("title"), "authors": [a for a in authors if a],
            "year": r.get("publication_year"),
            "abstract": _reconstruct_openalex_abstract(r.get("abstract_inverted_index")),
            "doi": doi, "venue": venue.get("display_name"),
            "citation_count": r.get("cited_by_count"), "source": "openalex", "_cites": [],
        })
    return papers


SOURCE_FNS = {
    "semantic_scholar": _search_semantic_scholar,
    "arxiv": _search_arxiv,
    "crossref": _search_crossref,
    "openalex": _search_openalex,
}


def _dedup_key(paper: dict) -> str | None:
    if paper.get("doi"):
        return f"doi:{paper['doi'].lower()}"
    title = (paper.get("title") or "").strip().lower()
    return f"title:{title}" if title else None


def run(task_text: str = None, session_id: str = None, tier: int = None,
        domain: str = None, sources: list = None) -> dict:
    """sources defaults to all four; a task can narrow it (e.g. only
    "arxiv" for recent preprints)."""
    query = (task_text or "").strip()
    if not query:
        report = {"papers": [], "edges_written": 0, "summary": "No search query provided."}
        write(KEYS["academic_search_report"], report)
        return report

    sources = sources or list(SOURCE_FNS.keys())
    workspace_id = _workspace_id()

    merged = {}
    for source in sources:
        fn = SOURCE_FNS.get(source)
        if not fn:
            continue
        for paper in fn(query, MAX_RESULTS_PER_SOURCE):
            key = _dedup_key(paper)
            if not paper.get("title") or not key:
                continue
            if key not in merged:
                merged[key] = paper
                continue
            # Same paper via a second source -- fill in whatever the
            # first hit was missing (Semantic Scholar's citation data
            # beats a bare CrossRef/OpenAlex hit for the same title/DOI).
            existing = merged[key]
            existing["abstract"] = existing.get("abstract") or paper.get("abstract")
            if existing.get("citation_count") is None:
                existing["citation_count"] = paper.get("citation_count")
            existing["_cites"] = existing.get("_cites") or paper.get("_cites")

    # Pass 1: write every paper as a node, and remember its node_id by
    # title so pass 2 can resolve citation edges between them.
    title_to_node_id = {}
    papers_out = []
    for key, paper in merged.items():
        node_id = write_node(
            workspace_id=workspace_id, section="research", node_type="source",
            title=paper.get("title") or "Untitled",
            content=paper.get("abstract") or paper.get("title") or "",
            created_by="academic_search",
            tags=[paper["source"]] + ([str(paper["year"])] if paper.get("year") else []),
            session_id=session_id, tier=tier,
        )
        title_to_node_id[(paper.get("title") or "").strip().lower()] = node_id
        papers_out.append({
            "paper_id": key, "node_id": node_id, "title": paper.get("title"),
            "authors": paper.get("authors", []), "year": paper.get("year"),
            "abstract": paper.get("abstract", ""), "doi": paper.get("doi"),
            "venue": paper.get("venue"), "citation_count": paper.get("citation_count"),
            "source": paper.get("source"),
        })

    # Pass 2: citation edges, only between papers actually in this result
    # set (an edge to a paper we didn't fetch would point at no node).
    edges_written = 0
    for paper in merged.values():
        from_id = title_to_node_id.get((paper.get("title") or "").strip().lower())
        if not from_id:
            continue
        for cited_title in paper.get("_cites") or []:
            to_id = title_to_node_id.get((cited_title or "").strip().lower())
            if not to_id or to_id == from_id:
                continue
            try:
                create_edge(f"node:{workspace_id}:{from_id}", f"node:{workspace_id}:{to_id}",
                            relation="cites", created_by="academic_search")
                edges_written += 1
            except ValueError:
                continue

    report = {
        "papers": papers_out, "edges_written": edges_written,
        "summary": f"{len(papers_out)} paper(s) found across {len(sources)} source(s), "
                   f"{edges_written} citation edge(s) written.",
    }
    write(KEYS["academic_search_report"], report)
    return report


if __name__ == "__main__":
    print(json.dumps(run(task_text="transformer attention mechanisms"), indent=2))