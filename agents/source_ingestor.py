"""
agents/source_ingestor.py — Part 4 §4.2. The single "write ingested
source as node(s)" step every Capture ingestor (agents/pdf_ingestor.py,
and the web/video/voice ingestors still to come) feeds into. Each
per-format ingestor's only job is to parse into the common artifact
shape ({title, sections, metadata}, Part 0 §0.5) — this module is the
one place that turns that shape into real eo/knowledge_graph.py nodes,
so ingestion isn't six copies of the same node-writing logic.

Deliberately NOT graph/adapters.py's artifact_to_candidate_node() /
write_imported_node() path: that exists for re-importing a
previously-exported artifact through an accept-before-persist review
step (Part 0 §0.3's propose-then-accept discipline). A freshly
ingested source isn't being proposed for review — Part 4's own
Definition of done (#1) calls for it landing as a real node directly,
the same way every other node in this system already is.

One node per section when a document has more than one (e.g. one per
PDF page), rather than one node holding the whole document's text —
keeps each node's embedded content scoped to a section instead of a
whole document, matching write_node()'s own guidance to pass a summary
rather than a full document's text for long sources. A single-section
artifact (e.g. a web clip) becomes exactly one node, since there's
nothing to split.

Place this file at: agents/source_ingestor.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.knowledge_graph import write_node
from eo.graph_edges import create_edge

NODE_TYPE = "source"


def write_ingested_source(artifact: dict, workspace_id: str, created_by: str,
                           section: str = "notes") -> list[str]:
    """Writes an ingested artifact (any Capture ingestor's output) as one
    or more real knowledge-graph nodes. Returns the list of new node_ids
    (may be shorter than the section count if an embed/upsert failed for
    one — write_node() degrades to None on failure rather than raising,
    same posture this function keeps).
    """
    sections = [s for s in artifact.get("sections", []) if (s.get("content") or "").strip()]
    title = artifact.get("title", "Untitled")
    tags = (artifact.get("metadata") or {}).get("tags", []) or []

    if len(sections) <= 1:
        content = sections[0]["content"] if sections else ""
        node_id = write_node(
            workspace_id=workspace_id, section=section, node_type=NODE_TYPE,
            title=title, content=content, created_by=created_by, tags=tags,
        )
        return [node_id] if node_id else []

    node_ids = []
    for s in sections:
        heading = s.get("heading") or title
        node_id = write_node(
            workspace_id=workspace_id, section=section, node_type=NODE_TYPE,
            title=f"{title} — {heading}", content=s["content"],
            created_by=created_by, tags=tags,
        )
        if node_id:
            node_ids.append(node_id)

    # Chains every section back to the first so the graph records these
    # as one source split across nodes, not N unrelated ones — the same
    # "structured relationship, not similarity" job edges already do
    # (Part 0 §0.2).
    if len(node_ids) > 1:
        first_vector_id = f"node:{workspace_id}:{node_ids[0]}"
        for nid in node_ids[1:]:
            try:
                create_edge(
                    from_node_id=f"node:{workspace_id}:{nid}",
                    to_node_id=first_vector_id,
                    relation="same_source",
                    created_by=created_by,
                )
            except Exception as exc:
                print(f"  [Source Ingestor] edge creation skipped for {nid}: {exc}")

    return node_ids


def ingest_pdf_to_graph(path: str, workspace_id: str, created_by: str,
                         section: str = "notes") -> list[str]:
    """End-to-end convenience wrapper (parse + write) for callers that
    don't need the intermediate artifact shape, e.g. an upload endpoint.
    A caller that wants to show a preview before committing should call
    pdf_ingestor.ingest_pdf() and write_ingested_source() separately
    instead of this.
    """
    from agents.pdf_ingestor import ingest_pdf
    artifact = ingest_pdf(path)
    return write_ingested_source(artifact, workspace_id, created_by, section)