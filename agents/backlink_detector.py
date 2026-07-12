"""
agents/backlink_detector.py — Part 4 §4.3. Deterministic, no-LLM-call
backlink detection: for every node in a workspace, checks whether the
node's content mentions another node's title, and creates a
"references" edge (eo/graph_edges.py, Part 0 §0.2) when it does.
"Backlinks shown automatically" needs no code beyond this — it's just
edges_for_node() filtered to this direction, the graph's natural shape.

Plain substring matching, case-insensitive — the notes doc is explicit
this doesn't need an LLM. Titles under MIN_TITLE_LENGTH characters are
skipped as a match target: a short, generic title (e.g. "Notes", "Q3")
would false-positive against nearly every other node's content, turning
"detect real cross-references" into "link everything to everything."

Place this file at: agents/backlink_detector.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.knowledge_graph import list_nodes
from eo.graph_edges import create_edge, edges_between

RELATION = "references"
MIN_TITLE_LENGTH = 4


def detect_backlinks(workspace_id: str, created_by: str = "system") -> list[dict]:
    """Scans every node in `workspace_id` for title mentions, creating a
    "references" edge for every match not already linked in either
    direction. Returns the list of newly created edges (empty if
    nothing new was found).
    """
    nodes = list_nodes(workspace_id)
    targets = [n for n in nodes if len(n.get("title", "").strip()) >= MIN_TITLE_LENGTH]

    created = []
    for source in nodes:
        content = (source.get("content") or "").lower()
        if not content:
            continue
        for target in targets:
            if target["node_id"] == source["node_id"]:
                continue
            title = target["title"].strip()
            if title.lower() not in content:
                continue
            if edges_between(source["vector_id"], target["vector_id"]):
                continue  # already linked (either direction) -- don't duplicate
            edge = create_edge(
                from_node_id=source["vector_id"],
                to_node_id=target["vector_id"],
                relation=RELATION,
                created_by=created_by,
            )
            created.append(edge)
    return created


if __name__ == "__main__":
    import sys
    import json
    for ws in sys.argv[1:]:
        edges = detect_backlinks(ws)
        print(f"--- {ws}: {len(edges)} new backlink(s) ---")
        print(json.dumps(edges, indent=2)[:1000])