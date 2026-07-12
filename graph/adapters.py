"""
graph/adapters.py — the "one adapter per domain, one exporter set total"
glue described in Part 0 §0.5. This module has two jobs:

  1. node_to_artifact()      — shape a graph node (§0.1) + its related
                                nodes (via edges, §0.2) into the common
                                {title, sections} artifact shape that
                                agents/exporter.py consumes. This is the
                                ONE place that understands the node
                                schema well enough to do that — Notes,
                                Research, and Plan call this instead of
                                each writing their own DOCX/PPTX writer.

  2. artifact_to_candidate_node() — the mirror path for import: take
                                whatever agents/importer.py parsed back
                                out of a docx/pptx/xlsx/csv/md/json file
                                and shape it into a *candidate* node
                                ready for write_node(), rather than
                                writing directly. Matches the same
                                propose-then-accept discipline §0.3
                                already uses for the silent note-taking
                                agent's candidate facts — an imported
                                file shouldn't silently become part of
                                the graph without the user seeing it
                                first.

  3. markdown_text_to_artifact() — Part 4 §4.4: the same {title,
                                sections} shape, but built directly from
                                a generic_worker role's raw Markdown
                                output (mapper/report_writer/
                                slide_planner/podcast_scriptwriter)
                                rather than from a node or an imported
                                file. Reuses agents/importer.py's own
                                Markdown grammar instead of parsing it
                                twice.

This module intentionally has NO hard dependency on memory.bus / the
Upstash Vector client / eo.errors -- it's called by code that already
has a node record in hand (however that node was fetched), so it stays
testable without the rest of the stack wired up. The write_node() /
create_edge() calls it makes are late-bound (imported inside the
functions, not at module load) against eo.knowledge_graph (§0.1) and
eo.graph_edges (§0.2) respectively.

Field names below match eo/knowledge_graph.py's actual write_node()/
get_node()/search_nodes() exactly: node_id, title, content, workspace_id,
section, node_type, created_by, created_at, tags. (content requires the
Part 0 §0.5 fix to write_node()'s metadata dict -- see that module's
inline comment -- since the original version embedded content but never
stored it, so get_node() had no text to return.)

Place this file at: graph/adapters.py
"""

from typing import Optional


def node_to_artifact(node: dict, related_nodes: Optional[list] = None) -> dict:
    """Builds the common export shape from a single node plus, optionally,
    a list of related nodes (typically everything one hop out via
    data/graph/_edges.json — e.g. all Research findings a Plan requirement
    "cites"). Each related node becomes its own section, so exporting a
    Plan requirement naturally pulls its supporting Research findings
    into the same document.

    `node` is expected to look like what get_node()/search_nodes() in
    eo/knowledge_graph.py actually return:
        {
            "node_id": str, "vector_id": str, "title": str, "content": str,
            "workspace_id": str, "section": str, "node_type": str,
            "tags": [...], "created_by": str, "created_at": str,
        }
    Only "title" and "content" are required here -- everything else is
    passed through to metadata for the exporter's benefit (and for
    round-tripping back through artifact_to_candidate_node on import).
    node_refs use vector_id ("node:{workspace_id}:{node_id}"), not bare
    node_id, since that's the format eo/graph_edges.py's edge records
    actually reference -- falls back to constructing it from node_id/
    workspace_id if a caller passes a node dict without vector_id set.
    """
    def _ref(n: dict) -> Optional[str]:
        if n.get("vector_id"):
            return n["vector_id"]
        if n.get("node_id") and n.get("workspace_id"):
            return f"node:{n['workspace_id']}:{n['node_id']}"
        return None

    sections = [{
        "heading": node.get("title", "Untitled"),
        "content": node.get("content", "") or "",
        "node_refs": [r for r in [_ref(node)] if r],
    }]

    for related in (related_nodes or []):
        sections.append({
            "heading": related.get("title", "Untitled"),
            "content": related.get("content", "") or "",
            "node_refs": [r for r in [_ref(related)] if r],
        })

    return {
        "title": node.get("title", "Untitled"),
        "sections": sections,
        "metadata": {
            "workspace_id": node.get("workspace_id"),
            "tags": node.get("tags", []),
            "created_by": node.get("created_by"),
            "created_at": node.get("created_at"),
            "source_node_id": node.get("node_id"),
        },
    }


def markdown_text_to_artifact(text: str, title_fallback: str = "Untitled",
                               workspace_id: Optional[str] = None,
                               tags: Optional[list] = None,
                               created_by: Optional[str] = None) -> dict:
    """Part 4 §4.4 — the adapter for generic_worker's generator roles
    (mapper, report_writer, slide_planner, podcast_scriptwriter): every
    one of them returns Markdown (generic_worker.py's own
    MARKDOWN_INSTRUCTION asks for '##'-headed sections), and this is
    what turns that raw stage_output text straight into the same
    {title, sections} shape agents/exporter.py consumes -- the same
    "one adapter per domain" role this module already plays for graph
    nodes, just fed LLM text instead of a stored node.

    No new parsing code: agents/importer.py's parse_markdown_text() is
    the exact "##-headed Markdown -> sections" grammar this needs,
    already built and tested for the file-import direction. Reused
    here as-is, late-imported for the same testability reason the rest
    of this module's cross-imports are.
    """
    from agents.importer import parse_markdown_text
    artifact = parse_markdown_text(text, default_title=title_fallback)
    artifact["metadata"] = {
        "workspace_id": workspace_id,
        "tags": tags or [],
        "created_by": created_by,
    }
    return artifact


def artifact_to_candidate_node(artifact: dict, workspace_id: str, section: str,
                                node_type: str, created_by: str) -> dict:
    """Shapes an imported artifact (agents/importer.py's output) into a
    candidate node dict -- NOT yet written to the graph. Concatenates all
    sections into one node's content with '## heading' separators so a
    multi-section import (e.g. a multi-slide PPTX) still becomes a single
    reviewable candidate rather than N disconnected candidates the user
    has to accept one at a time.

    Caller is responsible for presenting this to the user and, on accept,
    passing it to write_node() (§0.1) -- same accept-before-persist flow
    as §0.3's candidate facts.
    """
    content_parts = []
    all_refs = []
    for s in artifact.get("sections", []):
        heading = s.get("heading", "")
        content = s.get("content", "")
        if heading:
            content_parts.append(f"## {heading}\n\n{content}".strip())
        else:
            content_parts.append(content)
        all_refs.extend(s.get("node_refs", []))

    return {
        "title": artifact.get("title", "Untitled"),
        "content": "\n\n".join(p for p in content_parts if p.strip()),
        "workspace_id": workspace_id,
        "section": section,
        "node_type": node_type,
        "created_by": created_by,
        "tags": artifact.get("metadata", {}).get("tags", []),
        "related_node_refs": all_refs,  # caller may turn these into edges on accept
    }


def write_imported_node(candidate: dict, relation: str = "imported_from") -> Optional[str]:
    """Convenience wrapper for the common case where the caller wants to
    accept a candidate immediately (e.g. a CLI/script import rather than
    an interactive UI accept step). Late-imports eo.knowledge_graph /
    eo.graph_edges so this module has no hard dependency on either being
    importable in a lighter test context. Returns the new node_id, or
    None if the write failed (write_node() itself never raises -- see
    its docstring -- so this mirrors that: caller checks for None rather
    than catching an exception).

    Also creates an edge from the new node back to each node referenced
    in candidate["related_node_refs"] (e.g. the source nodes an imported
    file cited), so re-importing a previously exported artifact doesn't
    just create an orphaned node -- it reconnects into the graph it came
    from. Edge creation is best-effort: a failed edge doesn't undo the
    node write, matching write_node()'s own "degrade, don't hard-fail"
    posture.
    """
    from eo.knowledge_graph import write_node

    node_id = write_node(
        workspace_id=candidate["workspace_id"],
        section=candidate["section"],
        node_type=candidate["node_type"],
        title=candidate["title"],
        content=candidate["content"],
        created_by=candidate["created_by"],
        tags=candidate.get("tags", []),
    )
    if node_id is None:
        return None

    related_refs = candidate.get("related_node_refs", [])
    if related_refs:
        try:
            from eo.graph_edges import create_edge
            new_vector_id = f"node:{candidate['workspace_id']}:{node_id}"
            for ref in related_refs:
                create_edge(
                    from_node_id=new_vector_id,
                    to_node_id=ref,
                    relation=relation,
                    created_by=candidate["created_by"],
                )
        except Exception as exc:
            print(f"  [Adapters] edge creation skipped for imported node {node_id}: {exc}")

    return node_id