"""
eo/node_summaries.py — Notebooks integration guide §6.6 (Phase 3) / §7:
a short agent-written blurb per node, for the Backlinks concept graph's
node-click display.

Guide §7 is explicit this shouldn't be a new database table: "reuse the
same per-workspace small-JSON-store pattern eo/graph_edges.py /
agents/note_clusterer.py's candidate store already use." Same shape as
those two -- a single JSON file, a lock around read/modify/write --
just keyed workspace_id -> {node_id: summary} instead of workspace_id
-> [edges]/[candidates].

Written by agents/concept_linker.py's role pass; read by
KnowledgeGraphView.jsx on node click, via
GET /api/workspaces/{ws_id}/graph/node_summaries (api/server.py).

Place this file at: eo/node_summaries.py
"""
import os
import json
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARIES_PATH = os.path.join(BASE_DIR, "data", "graph", "_node_summaries.json")
_lock = threading.Lock()


def _read() -> dict:
    if not os.path.exists(SUMMARIES_PATH):
        return {}
    with open(SUMMARIES_PATH) as f:
        return json.load(f)


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(SUMMARIES_PATH), exist_ok=True)
    with open(SUMMARIES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def set_summaries(workspace_id: str, summaries: dict) -> dict:
    """Merges into whatever's already stored for this workspace rather
    than replacing it outright. A concept_linker run scoped to a few
    sources (guide §4.2's scope picker) only has fresh summaries for
    the nodes it actually read this time -- it shouldn't blank out
    summaries for every other node in the workspace just because they
    weren't in scope for *this* run. Empty/falsy summary values are
    skipped rather than written, so a node keeps its last real summary
    if a later run has nothing useful to say about it.

    Returns the workspace's full merged summary map.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")
    with _lock:
        data = _read()
        existing = dict(data.get(workspace_id, {}))
        for node_id, summary in (summaries or {}).items():
            if node_id and summary:
                existing[node_id] = summary
        data[workspace_id] = existing
        _write(data)
    return data[workspace_id]


def get_summaries(workspace_id: str) -> dict:
    """All node_id -> summary pairs stored for this workspace."""
    return _read().get(workspace_id, {})


def get_summary(workspace_id: str, node_id: str) -> str | None:
    return get_summaries(workspace_id).get(node_id)
