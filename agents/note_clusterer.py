"""
agents/note_clusterer.py — Part 4 §4.3. Deterministic, no-LLM-call
auto-clustering: reads a workspace's node embeddings straight from
Vector (already computed and stored at ingestion time, per 4.2) and
runs scikit-learn KMeans locally, writing suggested groupings back as
*candidates* -- not auto-applied groupings -- the same accept/discard
pattern eo/workspace_facts.py's proposal candidates already use.

Zero additional quota cost: no embed_text/LLM call happens here, only
KMeans over embeddings eo/knowledge_graph.py already paid to generate
at ingestion (list_nodes(include_vectors=True)).

Candidates are a single-file JSON store keyed by workspace_id, same
small-store shape as eo/graph_edges.py's _edges.json. Accepting a
candidate doesn't invent new node/tag storage -- it connects every
member node to the cluster's first node with a "clustered_with" edge
(eo/graph_edges.py, Part 0 §0.2), the same graph primitive backlinks
already use, just a different relation string and a star topology
(linear edge count) instead of a full mesh.

Requires scikit-learn + numpy (add to requirements.txt if not already
present -- neither is used anywhere else in this codebase yet).

Place this file at: agents/note_clusterer.py
"""
import os
import sys
import json
import uuid
import threading
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.knowledge_graph import list_nodes
from eo.graph_edges import create_edge

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES_PATH = os.path.join(BASE_DIR, "data", "graph", "_cluster_candidates.json")
_lock = threading.Lock()

RELATION = "clustered_with"
MIN_NODES_TO_CLUSTER = 4   # KMeans over a handful of points isn't a real suggestion
DEFAULT_MAX_CLUSTERS = 6


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read():
    if not os.path.exists(CANDIDATES_PATH):
        return {}
    with open(CANDIDATES_PATH) as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(CANDIDATES_PATH), exist_ok=True)
    with open(CANDIDATES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _label_for(members: list[dict]) -> str:
    """No LLM call for the label either: the most common existing tag
    among the cluster's members, falling back to the first member's
    title. Good enough for a candidate the user reviews and can
    rename/discard anyway -- it isn't written anywhere permanent until
    accept_candidate() runs.
    """
    tag_counts: dict[str, int] = {}
    for m in members:
        for t in m.get("tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    if tag_counts:
        return max(tag_counts, key=tag_counts.get)
    return members[0].get("title") or "Untitled cluster"


def propose_clusters(workspace_id: str, max_clusters: int = DEFAULT_MAX_CLUSTERS) -> list[dict]:
    """Runs KMeans over every node's embedding in `workspace_id` and
    replaces that workspace's pending candidate list with the fresh
    result -- same on-demand-rescan choice agents/backlink_detector.py
    makes, rather than incrementally maintaining clusters as nodes
    arrive. Returns the new candidate list.
    """
    nodes = [n for n in list_nodes(workspace_id, include_vectors=True) if n.get("vector")]

    if len(nodes) < MIN_NODES_TO_CLUSTER:
        candidates = []
    else:
        from sklearn.cluster import KMeans
        import numpy as np

        k = max(2, min(max_clusters, len(nodes) // 2))
        vectors = np.array([n["vector"] for n in nodes])
        labels = KMeans(n_clusters=k, n_init="auto", random_state=0).fit_predict(vectors)

        groups: dict[int, list[dict]] = {}
        for node, label in zip(nodes, labels):
            groups.setdefault(int(label), []).append(node)

        candidates = [
            {
                "candidate_id": f"cluster_{uuid.uuid4().hex[:10]}",
                "suggested_label": _label_for(members),
                "node_ids": [m["vector_id"] for m in members],
                "titles": [m.get("title", "") for m in members],
                "created_at": _now(),
            }
            for members in groups.values()
            if len(members) >= 2   # a cluster of one node isn't a suggestion
        ]

    with _lock:
        data = _read()
        data[workspace_id] = candidates
        _write(data)
    return candidates


def list_candidates(workspace_id: str) -> list[dict]:
    return _read().get(workspace_id, [])


def accept_candidate(workspace_id: str, candidate_id: str) -> list[dict]:
    """Connects every member to the cluster's first node with a
    "clustered_with" edge (star topology), then drops the candidate
    from the pending list. Returns the newly created edges.
    """
    candidates = list_candidates(workspace_id)
    candidate = next((c for c in candidates if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise FileNotFoundError(candidate_id)

    node_ids = candidate["node_ids"]
    created = [
        create_edge(from_node_id=node_ids[0], to_node_id=nid, relation=RELATION, created_by="note_clusterer")
        for nid in node_ids[1:]
    ]

    with _lock:
        data = _read()
        data[workspace_id] = [c for c in data.get(workspace_id, []) if c["candidate_id"] != candidate_id]
        _write(data)
    return created


def reject_candidate(workspace_id: str, candidate_id: str) -> None:
    with _lock:
        data = _read()
        remaining = [c for c in data.get(workspace_id, []) if c["candidate_id"] != candidate_id]
        if len(remaining) == len(data.get(workspace_id, [])):
            raise FileNotFoundError(candidate_id)
        data[workspace_id] = remaining
        _write(data)


if __name__ == "__main__":
    import sys as _sys
    for ws in _sys.argv[1:]:
        result = propose_clusters(ws)
        print(f"--- {ws}: {len(result)} candidate cluster(s) ---")
        print(json.dumps(result, indent=2)[:1000])