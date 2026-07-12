"""
eo/knowledge_graph.py — Knowledge Graph: Nodes (Part 0 §0.1 of the v5
Master Blueprint).

Every note, source, finding, spec, and artifact across every domain
(Notes/Research/Plan/Growth/Build/Admin) becomes a searchable, linkable
"node." This is what makes global search and cross-section promotion
possible without each domain inventing its own storage.

Same trick as agents/memory_search.py's "cyclemem:{app_slug}:{cycle_num}"
and eo/semantic_cache.py's project-scoped entries -- one shared Upstash
Vector index, a fourth id-prefix convention:

    node:{workspace_id}:{node_id}

Deliberately its own top-level id prefix (not reusing "cyclemem" or
matching semantic_cache's un-prefixed ids) so a query here never
accidentally matches memory_search.py's cycle memories or
semantic_cache.py's cached answers, same reasoning semantic_cache.py's
own docstring gives for using "project" instead of "app_slug" as its
metadata field name.

This module is intentionally domain-agnostic: it knows nothing about
Notes, Research, or Plan. Any agent that produces something worth
remembering imports write_node() and calls it after producing its real
output -- e.g. agents/documentation_agent.py calls it right after writing
doc_output. Failure to embed/upsert degrades to "this artifact isn't
searchable yet," never a hard error, same posture memory_search.py takes
toward HF/Vector failures.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import vector_index
from utils.llm_client import log_usage, embed_text

ID_PREFIX = "node"

# Keep this list in sync with what domains actually emit. Free-form
# strings would work too (same "don't over-specify" philosophy as the
# edges' `relation` field in §0.2), but node_type feeds filter queries
# directly, so a small fixed vocabulary here catches typos at the call
# site instead of silently fragmenting search results across
# "spec_requirement" vs "spec-requirement" vs "requirement".
NODE_TYPES = {
    "note", "source", "finding", "spec_requirement", "task", "persona_output",
}


def _node_vector_id(workspace_id: str, node_id: str) -> str:
    return f"{ID_PREFIX}:{workspace_id}:{node_id}"


def write_node(
    workspace_id: str,
    section: str,
    node_type: str,
    title: str,
    content: str,
    created_by: str,
    tags: list | None = None,
    node_id: str | None = None,
    session_id: str = None,
    tier: int = None,
) -> str | None:
    """Embeds `content` (pass a summary instead of the full text for long
    sources, e.g. PDFs -- the blueprint's own guidance) and upserts it as
    a node. Returns the new node_id on success, None if embedding/upsert
    failed (caller should treat this the same way memory_search.py treats
    a failed store: the cycle/artifact itself is unaffected, it's just
    not searchable yet).

    workspace_id/section/node_type/created_by are required so every node
    is filterable the same way semantic_cache.py already filters by
    `project` -- this is the metadata cross-section promotion and global
    search (§0.1) depend on.
    """
    if node_type not in NODE_TYPES:
        raise ValueError(f"Unknown node_type {node_type!r}; expected one of {sorted(NODE_TYPES)}")

    node_id = node_id or uuid.uuid4().hex[:12]
    vector_id = _node_vector_id(workspace_id, node_id)

    try:
        vector = embed_text(content)
    except Exception as exc:
        print(f"  [Knowledge Graph] embed failed, node not stored: {exc}")
        return None

    # Same placement rationale as memory_search.py: log right after the
    # embed call succeeds, so a downstream Vector failure doesn't hide
    # the fact that the billable HF call already happened.
    log_usage("huggingface", "HUGGINGFACE_API_KEY", None,
              session_id=session_id, tier=tier, agent_name="Knowledge Graph")

    metadata = {
        "workspace_id": workspace_id,
        "section": section,
        "node_type": node_type,
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "tags": tags or [],
        # Stored, not just embedded: a node exists to be re-read
        # (rendered, exported, cited), not just matched against. Same
        # "summary for long sources" guidance from this module's own
        # docstring keeps this bounded.
        "content": content,
    }

    try:
        vector_index().upsert(vectors=[(vector_id, vector, metadata)])
    except Exception as exc:
        print(f"  [Knowledge Graph] vector upsert failed: {exc}")
        return None

    return node_id


def get_node(workspace_id: str, node_id: str) -> dict | None:
    """Fetch a single node by id, without a similarity query."""
    vector_id = _node_vector_id(workspace_id, node_id)
    try:
        result = vector_index().fetch(ids=[vector_id], include_metadata=True)
    except Exception as exc:
        print(f"  [Knowledge Graph] fetch failed: {exc}")
        return None
    if not result:
        return None
    match = result[0]
    if not getattr(match, "metadata", None):
        return None
    return {"node_id": node_id, "vector_id": vector_id, **match.metadata}


def search_nodes(
    workspace_id: str,
    query_text: str,
    top_k: int = 10,
    section: str | None = None,
    node_type: str | None = None,
    tags: list | None = None,
    session_id: str = None,
    tier: int = None,
) -> list[dict]:
    """Global search (§0.1) and the tag-filterable search §0.4 promises
    "for free" once tags ride along as node metadata. Always scoped to a
    single workspace_id -- same non-negotiable isolation memory_search.py
    enforces with app_slug and semantic_cache.py enforces with project.
    """
    try:
        vector = embed_text(query_text)
    except Exception as exc:
        print(f"  [Knowledge Graph] search embed failed: {exc}")
        return []

    log_usage("huggingface", "HUGGINGFACE_API_KEY", None,
              session_id=session_id, tier=tier, agent_name="Knowledge Graph")

    filter_clauses = [f"workspace_id = '{workspace_id}'"]
    if section:
        filter_clauses.append(f"section = '{section}'")
    if node_type:
        filter_clauses.append(f"node_type = '{node_type}'")
    filter_str = " AND ".join(filter_clauses)

    try:
        results = vector_index().query(
            vector=vector, top_k=top_k, include_metadata=True, filter=filter_str,
        )
    except Exception as exc:
        print(f"  [Knowledge Graph] search query failed: {exc}")
        return []

    nodes = []
    for match in results:
        meta = getattr(match, "metadata", None)
        if not meta:
            continue
        # tags filtering done client-side: Upstash Vector's metadata
        # filter syntax doesn't do array-contains-any cleanly, and tag
        # vocabularies are small per workspace (§0.4 explicitly says not
        # to pre-build a tag registry), so this is cheap.
        if tags and not set(tags) & set(meta.get("tags", [])):
            continue
        node_id = match.id.split(":", 2)[-1] if hasattr(match, "id") else None
        nodes.append({"node_id": node_id, "vector_id": getattr(match, "id", None),
                       "score": getattr(match, "score", None), **meta})
    return nodes


def list_nodes(
    workspace_id: str,
    node_type: str | None = None,
    include_vectors: bool = False,
) -> list[dict]:
    """Part 4 §4.3 — every node in a workspace, not just the top_k most
    similar to some query. search_nodes() above needs a query_text
    because Vector's query() is inherently a similarity search; backlink
    detection (scanning every note's content for every other note's
    title) and auto-clustering (KMeans/HDBSCAN over a whole workspace's
    embeddings) both need the actual full set instead, which is what
    Vector's range() is for: a paginated scan with the same metadata
    filter syntax query() uses, no query vector required.

    Pages through range()'s cursor rather than a single unbounded call,
    since a workspace's node count isn't bounded the way top_k already
    bounds search_nodes(). include_vectors=True is for auto-clustering's
    benefit (it needs the raw embeddings, not just metadata) -- left
    False by default since backlink detection only needs title/content
    and fetching every node's full vector unnecessarily is wasted
    bandwidth for that caller.

    Degrades to whatever was collected so far on failure (same posture
    as search_nodes() degrading to []), rather than raising, so a
    Vector hiccup mid-scan doesn't take down whatever's calling this.
    """
    filter_clauses = [f"workspace_id = '{workspace_id}'"]
    if node_type:
        filter_clauses.append(f"node_type = '{node_type}'")
    filter_str = " AND ".join(filter_clauses)

    nodes = []
    cursor = ""
    try:
        while True:
            page = vector_index().range(
                cursor=cursor, limit=100,
                include_metadata=True, include_vectors=include_vectors,
                filter=filter_str,
            )
            for v in page.vectors:
                meta = getattr(v, "metadata", None) or {}
                node_id = v.id.split(":", 2)[-1]
                node = {"node_id": node_id, "vector_id": v.id, **meta}
                if include_vectors:
                    node["vector"] = v.vector
                nodes.append(node)
            cursor = page.next_cursor
            if not cursor:
                break
    except Exception as exc:
        print(f"  [Knowledge Graph] list_nodes failed (partial results kept): {exc}")

    return nodes