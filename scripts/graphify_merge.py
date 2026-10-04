"""Merge the generated Graphify graph variants without overwriting inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GRAPH_DIR = ROOT / "graphify-out"
INPUT_NAMES = ("graph.json", "graph.linked.json", "graph.docs.json")
OUTPUT_PATH = GRAPH_DIR / "graph.merged.json"
DOC_FILE_TYPES = {"document", "concept", "rationale"}


def read_graph(name: str) -> dict:
    return json.loads((GRAPH_DIR / name).read_text(encoding="utf-8"))


def node_ids(graph: dict) -> set[str]:
    return {node["id"] for node in graph.get("nodes", []) if "id" in node}


def community_count(graph: dict) -> int:
    return len({node.get("community") for node in graph.get("nodes", []) if node.get("community") is not None})


def link_key(link: dict) -> tuple[object, object, object]:
    return link.get("source"), link.get("target"), link.get("relation")


def docs_content_graph(graph: dict, allowed_ids: set[str]) -> dict:
    nodes = [
        node
        for node in graph.get("nodes", [])
        if node.get("file_type") in DOC_FILE_TYPES
        and str(node.get("source_file", "")).replace("\\", "/").startswith("docs/")
    ]
    docs_ids = {node["id"] for node in nodes}
    links = [
        link
        for link in graph.get("links", [])
        if str(link.get("source_file", "")).replace("\\", "/").startswith("docs/")
        and link.get("source") in allowed_ids | docs_ids
        and link.get("target") in allowed_ids | docs_ids
        and (link.get("source") in docs_ids or link.get("target") in docs_ids)
    ]
    return {"nodes": nodes, "links": links, "hyperedges": []}


def filtered_linked_graph(base: dict, linked: dict) -> tuple[dict, bool]:
    base_ids = node_ids(base)
    linked_ids = node_ids(linked)
    if base_ids == linked_ids:
        return linked, False
    repaired = copy.deepcopy(base)
    repaired_links = list(base.get("links", []))
    seen = {link_key(link) for link in repaired_links}
    for link in linked.get("links", []):
        if link.get("source") not in base_ids or link.get("target") not in base_ids:
            continue
        if link_key(link) not in seen:
            repaired_links.append(link)
            seen.add(link_key(link))
    repaired["links"] = repaired_links
    return repaired, True


def merge_graphs(graphs: list[dict]) -> dict:
    merged = copy.deepcopy(graphs[0])
    nodes_by_id = {node["id"]: node for node in merged.get("nodes", []) if "id" in node}
    links_by_key = {link_key(link): link for link in merged.get("links", [])}
    hyperedges_by_id = {
        hyperedge.get("id"): hyperedge
        for hyperedge in merged.get("hyperedges", [])
        if hyperedge.get("id") is not None
    }
    for graph in graphs[1:]:
        for node in graph.get("nodes", []):
            if node.get("id") is not None:
                nodes_by_id.setdefault(node["id"], node)
        for link in graph.get("links", []):
            links_by_key.setdefault(link_key(link), link)
        for hyperedge in graph.get("hyperedges", []):
            if hyperedge.get("id") is not None:
                hyperedges_by_id.setdefault(hyperedge["id"], hyperedge)
    merged["nodes"] = list(nodes_by_id.values())
    merged["links"] = list(links_by_key.values())
    merged["hyperedges"] = list(hyperedges_by_id.values())
    if isinstance(merged.get("graph"), dict):
        merged["graph"]["hyperedges"] = merged["hyperedges"]
    return merged


def main() -> None:
    raw_inputs = [read_graph(name) for name in INPUT_NAMES]
    inputs = raw_inputs[:2]
    linked, repaired = filtered_linked_graph(inputs[0], inputs[1])
    if repaired:
        print("graph.linked.json did not match the filtered graph; repaired in memory from graph.json")
    else:
        print("graph.linked.json matches the ignore-filtered graph")
    inputs[1] = linked
    docs = docs_content_graph(raw_inputs[2], node_ids(inputs[0]))
    inputs.append(docs)
    for name, graph in zip((INPUT_NAMES[0], INPUT_NAMES[1], "graph.docs.json (docs content only)"), inputs):
        print(f"{name}: nodes={len(graph.get('nodes', []))} links={len(graph.get('links', []))} communities={community_count(graph)}")
    merged = merge_graphs(inputs)
    OUTPUT_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"graph.merged.json: nodes={len(merged['nodes'])} links={len(merged['links'])} communities={community_count(merged)}")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
