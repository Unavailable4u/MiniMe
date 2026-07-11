"""
agents/citation_graph_builder.py — Citation graph view (Part 3).

REAL_ACTION_ROLES tool agent, read-only: zero HTTP calls, zero LLM calls,
zero new nodes or edges written. agents/academic_search.py already writes
every paper as a node and every citation relationship it finds as a
"cites" edge (see that module's own docstring: "citation_graph_builder's
view ... just reads what this step wrote") -- this module's entire job is
to assemble those existing edges into a shape something can actually use:
who cites whom within the fetched set, which papers are hubs (cited by
several others already in this workspace), and which came back with no
citation relationship in the graph at all.

Scoped to the WHOLE workspace's "cites" edges (eo/graph_edges.py's
list_edges()), not just the latest academic_search_report snapshot --
KEYS["academic_search_report"] is a single overwritten key, so a research
task that calls academic_search more than once (different queries) would
otherwise lose earlier runs' papers from this view entirely. A paper from
an earlier run that isn't in the current report is resolved with
eo/knowledge_graph.py's get_node() (a plain id fetch, not a similarity
query -- no embedding, no HF call).

A real, known gap this surfaces rather than hides: only Semantic Scholar
results ever populate `_cites` (see academic_search.py's four source
functions) -- arXiv/CrossRef/OpenAlex results always come back with no
outgoing citations recorded. So a citation graph built from a
non-Semantic-Scholar-heavy search will show most papers as isolated. This
module reports that count plainly (`isolated_count`) rather than papering
over it.

Result written to KEYS["citation_graph"]:
{
  "nodes": [{"node_id", "title", "year", "in_degree", "out_degree"}],
  "edges": [{"from_node_id", "to_node_id"}],
  "hubs": [{"node_id", "title", "in_degree"}],
  "isolated_count": <int>,
  "summary": "...",
  "image": "data:image/svg+xml;base64,..." (Part 3 §3.9, omitted if the
            graph has no nodes or more than MAX_GRAPH_IMAGE_NODES) -- a
            plain circle-layout SVG, dot per paper (gold = hub, blue =
            not), line per citation edge. eo/executor.py's outer
            agent_done already threads any result["image"] key through to
            the frontend generically (see its own comment), so this
            module doesn't need any executor.py-side special case.
}
"""
import os
import sys
import json
import math
import base64

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, get_current_app_slug
from eo.knowledge_graph import get_node
from eo.graph_edges import list_edges
from eo.errors import MissingDependencyError

# Cited by at least this many other papers already in the workspace's
# graph to count as a "hub" -- 1 is just "someone happened to cite you
# once," not a meaningful signal on its own.
MIN_IN_DEGREE_FOR_HUB = 2
MAX_HUBS = 10

# SVG rendering (Part 3 §3.9's frontend work: eo/executor.py's outer
# agent_done now threads any result["image"] data URI through to the
# frontend, generic by key -- see its own comment). Deliberately plain
# Python/math, no plotting library: a fixed circle layout, dot-per-node,
# line-per-edge, labels only on hubs to keep the SVG small and readable.
# Capped at MAX_GRAPH_IMAGE_NODES so a large graph degrades to "no image"
# rather than an unreadably dense one or one that blows executor.py's
# MAX_IMAGE_DATA_URI_CHARS cap and just gets silently dropped there anyway.
MAX_GRAPH_IMAGE_NODES = 25
SVG_SIZE = 360
SVG_CENTER = SVG_SIZE / 2
SVG_RADIUS = 140


def _truncate_title(title: str, max_len: int = 22) -> str:
    title = title or "Untitled"
    return title if len(title) <= max_len else title[: max_len - 1] + "…"


def _escape_xml(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_graph_svg(nodes: list, edges: list) -> str | None:
    """Returns an SVG string, or None if there's nothing worth drawing
    (no nodes) or too much to draw legibly (over MAX_GRAPH_IMAGE_NODES --
    the caller just omits the image in that case, same as any other
    "not worth doing" skip in this module)."""
    if not nodes or len(nodes) > MAX_GRAPH_IMAGE_NODES:
        return None

    # Fixed circle layout, node order = insertion order (dict is already
    # insertion-ordered) -- deterministic, so the same graph always draws
    # the same picture rather than shuffling on every run.
    positions = {}
    n = len(nodes)
    for i, node in enumerate(nodes):
        angle = (2 * math.pi * i) / n
        positions[node["node_id"]] = (
            SVG_CENTER + SVG_RADIUS * math.cos(angle),
            SVG_CENTER + SVG_RADIUS * math.sin(angle),
        )

    parts = [
        f'<svg viewBox="0 0 {SVG_SIZE} {SVG_SIZE}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{SVG_SIZE}" height="{SVG_SIZE}" fill="#111318"/>',
    ]

    for e in edges:
        a, b = positions.get(e["from_node_id"]), positions.get(e["to_node_id"])
        if not a or not b:
            continue
        parts.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                     f'stroke="#818cf8" stroke-opacity="0.35" stroke-width="1"/>')

    for node in nodes:
        x, y = positions[node["node_id"]]
        is_hub = node["in_degree"] >= MIN_IN_DEGREE_FOR_HUB
        radius = 4 + min(node["in_degree"], 6)
        fill = "#fbbf24" if is_hub else "#60a5fa"
        title = _escape_xml(node.get("title"))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{fill}"><title>{title}</title></circle>')
        if is_hub:
            # Labels only on hubs -- see module comment: keeps the SVG
            # small and the picture readable instead of cluttered.
            label = _escape_xml(_truncate_title(node.get("title")))
            parts.append(f'<text x="{x:.1f}" y="{y + radius + 10:.1f}" font-size="9" fill="#e5e7eb" '
                         f'text-anchor="middle" font-family="sans-serif">{label}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _svg_to_data_uri(svg: str) -> str:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _workspace_id() -> str:
    return get_current_app_slug() or read(KEYS["original_idea"], default="untitled")


def _bare_node_id(full_id: str) -> str:
    # Edge endpoints are stored as the full "node:{workspace_id}:{node_id}"
    # vector id (eo/graph_edges.py's own convention) -- same split
    # eo/knowledge_graph.py's search_nodes() already uses to get the bare id.
    return full_id.split(":", 2)[-1] if full_id else full_id


def run(session_id: str = None, tier: int = None, domain: str = None) -> dict:
    report = read(KEYS["academic_search_report"])
    papers = (report or {}).get("papers") or []
    if not papers:
        raise MissingDependencyError(required_role="academic_search")

    workspace_id = _workspace_id()

    # Seed node info from the current report (no fetch needed); resolve
    # anything else an edge references (earlier runs' papers) on demand.
    node_info = {
        p["node_id"]: {"node_id": p["node_id"], "title": p.get("title"), "year": p.get("year")}
        for p in papers if p.get("node_id")
    }

    def _resolve(node_id: str) -> dict:
        if node_id not in node_info:
            fetched = get_node(workspace_id, node_id) or {}
            node_info[node_id] = {
                "node_id": node_id, "title": fetched.get("title"), "year": None,
            }
        return node_info[node_id]

    cite_edges = [e for e in list_edges(workspace_id) if e.get("relation") == "cites"]

    in_degree, out_degree = {}, {}
    edges_out = []
    for e in cite_edges:
        from_id = _bare_node_id(e["from_node_id"])
        to_id = _bare_node_id(e["to_node_id"])
        _resolve(from_id)
        _resolve(to_id)
        out_degree[from_id] = out_degree.get(from_id, 0) + 1
        in_degree[to_id] = in_degree.get(to_id, 0) + 1
        edges_out.append({"from_node_id": from_id, "to_node_id": to_id})

    nodes_out = [
        {**info, "in_degree": in_degree.get(node_id, 0), "out_degree": out_degree.get(node_id, 0)}
        for node_id, info in node_info.items()
    ]
    isolated_count = sum(1 for n in nodes_out if n["in_degree"] == 0 and n["out_degree"] == 0)

    hubs = sorted(
        (n for n in nodes_out if n["in_degree"] >= MIN_IN_DEGREE_FOR_HUB),
        key=lambda n: n["in_degree"], reverse=True,
    )[:MAX_HUBS]

    summary = (
        f"{len(nodes_out)} paper(s), {len(edges_out)} citation edge(s), "
        f"{len(hubs)} hub(s), {isolated_count} isolated (no citation link in this graph)."
    )
    result = {
        "nodes": nodes_out, "edges": edges_out, "hubs": hubs,
        "isolated_count": isolated_count, "summary": summary,
    }

    svg = _render_graph_svg(nodes_out, edges_out)
    if svg:
        result["image"] = _svg_to_data_uri(svg)

    write(KEYS["citation_graph"], result)

    if session_id:
        hub_lines = "\n".join(f"- \"{h['title']}\" (cited by {h['in_degree']} other fetched paper(s))" for h in hubs)
        stage_text = (
            f"{summary}\n\n"
            + (f"Hub papers:\n{hub_lines}" if hubs else "No hub papers (nothing cited by 2+ others in this set).")
        )
        write(f"stage_output:{session_id}:citation_graph_builder", stage_text)

    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))