"""
eo/graph_edges.py — knowledge-graph edges (Part 0, Section 0.2).

Nodes (Section 0.1) live in Upstash Vector under the `node:{workspace_id}:
{node_id}` id-prefix convention, because they're semantically searchable
content. Edges are NOT semantically searchable — they're structured
relationships — so they belong in a small JSON store instead, same shape
as eo/chat_workspace.py's _workspaces.json and eo/memory_batch.py's
_batches.json: a single file, a lock around read/modify/write, one array
of records.

`relation` is deliberately a free-form string, not a fixed enum — same
philosophy as eo/routing_memory.py's `outcome` field. "supports", "cites",
"contradicts", "promoted_from", etc. are all valid without pre-defining
every possible relationship type up front.

Two ways an edge gets created (matching the blueprint's own distinction):
  - Auto-created: an agent that consumes one node while producing another
    calls create_edge() itself, right after it writes the new node (same
    discipline agents/dependency_mapper.py already follows for code
    modules — recorded at the moment the relationship is known, not
    guessed after the fact).
  - Manually created: a UI affordance (drag node onto node, or a "link
    to..." picker) hits a `create_edge` endpoint that also just calls
    create_edge() below. No agent involvement needed for this path.
"""
import os, json, uuid, threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGES_PATH = os.path.join(BASE_DIR, "data", "graph", "_edges.json")
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read():
    if not os.path.exists(EDGES_PATH):
        return {"edges": []}
    with open(EDGES_PATH) as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(EDGES_PATH), exist_ok=True)
    with open(EDGES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _workspace_of(node_id: str) -> str | None:
    """node ids are `node:{workspace_id}:{node_id}` (Section 0.1) — pull
    the workspace back out of the id itself rather than storing it a
    second time on the edge record."""
    parts = (node_id or "").split(":", 2)
    return parts[1] if len(parts) >= 2 and parts[0] == "node" else None


def create_edge(from_node_id: str, to_node_id: str, relation: str, created_by: str) -> dict:
    """created_by: agent name (e.g. "plan_writer") for auto-created edges,
    or "user" for manually-drawn ones — same free-form convention as the
    rest of this store, no separate is_auto flag needed."""
    if not from_node_id or not to_node_id:
        raise ValueError("from_node_id and to_node_id are required")
    if from_node_id == to_node_id:
        raise ValueError("an edge can't connect a node to itself")

    edge = {
        "edge_id": f"edge_{uuid.uuid4().hex[:10]}",
        "from_node_id": from_node_id,
        "to_node_id": to_node_id,
        "relation": (relation or "").strip() or "related",
        "created_by": created_by,
        "created_at": _now(),
    }
    with _lock:
        data = _read()
        data["edges"].append(edge)
        _write(data)
    return edge


def delete_edge(edge_id: str) -> None:
    with _lock:
        data = _read()
        remaining = [e for e in data["edges"] if e["edge_id"] != edge_id]
        if len(remaining) == len(data["edges"]):
            raise FileNotFoundError(edge_id)
        data["edges"] = remaining
        _write(data)


def get_edge(edge_id: str) -> dict:
    for e in _read()["edges"]:
        if e["edge_id"] == edge_id:
            return e
    raise FileNotFoundError(edge_id)


def list_edges(workspace_id: str | None = None) -> list:
    """All edges, optionally scoped to a workspace. Scoping is derived
    from the node ids on each edge (see _workspace_of) rather than a
    stored workspace_id, so there's no risk of the two drifting apart."""
    edges = _read()["edges"]
    if workspace_id is None:
        return edges
    return [
        e for e in edges
        if _workspace_of(e["from_node_id"]) == workspace_id
        or _workspace_of(e["to_node_id"]) == workspace_id
    ]


def edges_for_node(node_id: str) -> list:
    """Every edge touching this node, either direction — what the graph
    view needs to expand a single node, and what a "delete this node"
    flow needs to know what it would orphan."""
    return [
        e for e in _read()["edges"]
        if e["from_node_id"] == node_id or e["to_node_id"] == node_id
    ]


def edges_between(node_id_a: str, node_id_b: str) -> list:
    """Existing edges connecting two specific nodes (either direction) —
    used by the "link to..." UI picker to show/avoid duplicate links."""
    pair = {node_id_a, node_id_b}
    return [
        e for e in _read()["edges"]
        if {e["from_node_id"], e["to_node_id"]} == pair
    ]