"""
api/routes/graph_and_notes.py

B6, piece 6 — knowledge-graph edges, node summaries, the topics/graph
projection, node get/rename/delete, silent note-taking candidates,
backlink detection, and auto-clustering (propose/candidates/accept/
reject). Pulled out of api/server.py verbatim (same functions, same
error handling, same docstrings) — nothing here changes behavior,
this is a pure move.

Unlike the original split plan's note that this territory was
scattered through server.py, by the time pieces 1–5 landed it was
already one contiguous block (the knowledge-graph edges section
through the cluster candidates reject route) — so this piece is a
straight lift of that whole block, not a reassembly from multiple
locations.

Deliberately NOT included: /api/workspaces/{ws_id}/facts/candidates/*
(piece 5 / workspace_data.py — those are workspace-fact candidates,
a different store from the note candidates and cluster candidates
here, even though the accept/reject shape looks the same); the
Notebooks generate/podcast/video/table/simulate endpoints and the
/api/notes/* family (piece 7 / notebooks.py) — several of those call
into propose_clusters()/chat_workspace.get_workspace()/panel_content
too, so server.py keeps its own imports of those for piece 7's sake.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import require_auth
from agents.backlink_detector import detect_backlinks, cleanup_for_removed_source
from agents.note_clusterer import propose_clusters, list_candidates as list_cluster_candidates, \
    accept_candidate as accept_cluster_candidate, reject_candidate as reject_cluster_candidate
from eo import chat_workspace
from eo import graph_edges
from eo import note_candidates
from eo import node_summaries
from eo import panel_content
from eo.knowledge_graph import list_nodes, delete_node, rename_node
from eo.secondary_data import get_secondary_data

router = APIRouter()


class RenameNodeRequest(BaseModel):
    title: str

class CreateEdgeRequest(BaseModel):
    from_node_id: str
    to_node_id: str
    relation: str = "related"


# --- knowledge-graph edges (see eo/graph_edges.py, §0.2) -----------------
# Auto-created edges are written directly by whichever agent produced
# them (no HTTP round-trip). This is the manual path: the "link to..."
# UI picker / drag-node-onto-node affordance calls this directly, no
# agent involvement.

@router.get("/api/graph/edges", dependencies=[Depends(require_auth)])
def get_graph_edges(workspace_id: Optional[str] = Query(None)):
    return graph_edges.list_edges(workspace_id=workspace_id)


@router.post("/api/graph/edges", dependencies=[Depends(require_auth)])
def create_graph_edge(req: CreateEdgeRequest):
    try:
        return graph_edges.create_edge(req.from_node_id, req.to_node_id, req.relation, created_by="user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/graph/edges/{edge_id}", dependencies=[Depends(require_auth)])
def delete_graph_edge(edge_id: str):
    try:
        graph_edges.delete_edge(edge_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edge_id")
    return {"status": "deleted", "id": edge_id}


# NEW -- Notebooks integration guide section 6.6/7 (Phase 3): short
# agent-written blurbs written by agents/concept_linker.py, read by
# KnowledgeGraphView.jsx's node-click panel. Read-only by design, same
# "agent-only writes" posture as the Backlinks concept graph itself --
# there's no corresponding POST/PUT here on purpose.
@router.get("/api/workspaces/{ws_id}/graph/node_summaries", dependencies=[Depends(require_auth)])
def get_node_summaries(ws_id: str):
    return node_summaries.get_summaries(ws_id)


# NEW — Backlinks-as-topic-tree: turns eo/secondary_data.py's
# {topics, connections} document into the same {node_id, workspace_id,
# node_type, title, ...} / {from_node_id, to_node_id, relation} shape
# get_workspace_nodes()/list_edges() already hand KnowledgeGraphView.jsx,
# so that component needs zero new data-shape handling — only a new
# relation string (parent_of) and same_fact_as's own color, both added
# in KnowledgeGraphView.jsx itself.
#
# Read-only, same posture as get_node_summaries above: Secondary Data's
# only write path stays apply_patch() (source_manager.py's Mode A pass,
# backlink_detector.py's reconciliation) -- this just re-projects the
# current document, it never mutates it.
#
# `summary`/`instances` are tucked directly onto each node dict (not
# nested under a separate key) so KnowledgeGraphView's existing
# `obj.raw = n` assignment carries them through untouched to the
# rationale panel/hover tooltip.
@router.get("/api/workspaces/{ws_id}/topics/graph", dependencies=[Depends(require_auth)])
def get_topics_graph(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    doc = get_secondary_data(ws_id)
    topics = doc["topics"]

    nodes = [
        {
            "node_id": topic_id,
            "workspace_id": ws_id,
            "node_type": "topic",
            "title": topic.get("name") or topic_id,
            "summary": topic.get("summary", ""),
            "instances": topic.get("instances", []),
            "content_hint": topic.get("content_hint"),
        }
        for topic_id, topic in topics.items()
    ]

    edges = []
    # Synthetic parent_of edges (from_node = parent, to_node = child) so
    # the tree structure itself renders, not just the cross-links below.
    # Skips a topic whose recorded parent no longer resolves -- same
    # "endpoints must exist" posture _drop_dangling_connections() already
    # enforces inside get_secondary_data() itself.
    for topic_id, topic in topics.items():
        parent_id = topic.get("parent")
        if parent_id and parent_id in topics:
            edges.append({
                "edge_id": f"topic-parent:{parent_id}:{topic_id}",
                "from_node_id": f"node:{ws_id}:{parent_id}",
                "to_node_id": f"node:{ws_id}:{topic_id}",
                "relation": "parent_of",
            })

    # doc["connections"] is already filtered to resolvable endpoints by
    # get_secondary_data()'s own _drop_dangling_connections() pass, so no
    # extra existence check is needed here -- includes the four original
    # relations plus overlapping_checker.py's same_fact_as.
    for i, conn in enumerate(doc["connections"]):
        edges.append({
            "edge_id": f"topic-conn:{i}:{conn['from_topic']}:{conn['to_topic']}",
            "from_node_id": f"node:{ws_id}:{conn['from_topic']}",
            "to_node_id": f"node:{ws_id}:{conn['to_topic']}",
            "relation": conn.get("relation", ""),
        })

    return {"nodes": nodes, "edges": edges}


# --- knowledge-graph nodes (see eo/knowledge_graph.py, §0.1) -------------
# §4.7: the Notebooks tab's one read for "everything in this notebook" —
# the source list, the mind map's underlying content, and
# KnowledgeGraphView's backlink visualization all page through this same
# list_nodes() call rather than each inventing their own fetch.

@router.get("/api/workspaces/{ws_id}/nodes")
def get_workspace_nodes(ws_id: str, node_type: Optional[str] = Query(None),
                         owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return list_nodes(ws_id, node_type=node_type)

@router.patch("/api/workspaces/{ws_id}/nodes/{node_id}/rename", dependencies=[Depends(require_auth)])
def rename_node_endpoint(ws_id: str, node_id: str, req: RenameNodeRequest):
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title can't be empty.")
    ok = rename_node(ws_id, node_id, title)
    if not ok:
        raise HTTPException(status_code=500, detail="Rename failed.")
    return {"status": "ok", "node_id": node_id, "title": title}

# NEW — §2 fix: there was no way to delete an individual ingested
# source/node -- SourcesView's rows only ever *selected* a node, no
# delete affordance existed on either end. Cascades to graph_edges
# referencing this node (edges store the full "node:{ws_id}:{node_id}"
# vector id on from_node_id/to_node_id -- see ResearchTab.jsx's own
# bareNodeId() comment -- so we build that same prefixed id to match)
# and to cluster candidates that included this node, so neither dangles
# pointing at a node that no longer exists. Note-candidates aren't
# node-linked (see their {title, content} shape in CandidatesView.jsx),
# so there's nothing to cascade there.
#
# CHANGED — bug audit §2 (delete cascade), part B: a file that got split
# into multiple nodes at ingestion time (agents/source_ingestor.py's
# write_ingested_source()) links every section after the first back to
# the first with a same_source edge (child --same_source--> root). The
# frontend's "Delete source" button on a grouped source only ever passed
# the group's root node_id, so every sibling section survived as an
# orphaned node -- still searchable, still surfaced in chat grounding
# (api/task_runner.py's _grounded_task_text()) and future Mind
# Map/Study/Facts generations, even though the user believed the whole
# file was gone. Resolve the target node to its full same_source batch
# (itself plus every child pointing at it) and delete all of them, same
# as if each had been deleted individually.
#
# CHANGED — bug audit §2, part A: also clears every saved panel
# (eo/panel_content.py) for the workspace, since Mind Map/Study Guide/
# Facts/Clusters are whole-notebook artifacts with no record of which
# source nodes fed into them -- see clear_workspace()'s docstring for
# why "clear everything" is the deliberate choice here over a partial
# invalidation.
@router.delete("/api/workspaces/{ws_id}/nodes/{node_id}", dependencies=[Depends(require_auth)])
def delete_workspace_node(ws_id: str, node_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    full_node_id = f"node:{ws_id}:{node_id}"
    sibling_ids = [
        edge["from_node_id"].split(":", 2)[-1]
        for edge in graph_edges.edges_for_node(full_node_id)
        if edge.get("relation") == "same_source" and edge["to_node_id"] == full_node_id
    ]
    batch_ids = [node_id, *sibling_ids]

    for nid in batch_ids:
        delete_node(ws_id, nid)

        # eo/graph_edges.py's own edges_for_node() docstring: "what a
        # 'delete this node' flow needs to know what it would orphan" --
        # built for exactly this, confirmed against that module's source
        # rather than guessed.
        for edge in graph_edges.edges_for_node(f"node:{ws_id}:{nid}"):
            try:
                graph_edges.delete_edge(edge["edge_id"])
            except FileNotFoundError:
                pass

        for candidate in list_cluster_candidates(ws_id):
            if nid in (candidate.get("node_ids") or []):
                try:
                    reject_cluster_candidate(ws_id, candidate["candidate_id"])
                except FileNotFoundError:
                    pass

    # Data Layer architecture §3c: same deleted batch_ids the cascade
    # above just tore down at the graph/candidate level also needs
    # Secondary Data (eo/secondary_data.py) cleaned up -- any topic
    # Source Manager's Mode A pass (§3) derived from these nodes, and
    # any connection Backlink Detector (§3b) built against one, or this
    # workspace accumulates orphaned topic-tree entries every time a
    # source is deleted. No-ops instantly if this workspace's Secondary
    # Data has nothing referencing these node_ids (e.g. content ingested
    # before this feature existed, or through a path that never ran
    # Mode A at all).
    cleanup_ops = cleanup_for_removed_source(ws_id, batch_ids)

    # CHANGED — bug audit §2 real fix (migration 0001): used to be
    # panel_content.clear_workspace(ws_id, owner_id) here, wiping every
    # saved panel in the notebook -- Mind Map, Study Guide, Workflows,
    # *and* the unrelated manual-paste panels (PRD, Architecture, etc.)
    # -- on every single source delete. Now that generated panels record
    # which source nodes they were built from, only clear the ones this
    # deleted batch actually touched (or that were built from "the whole
    # notebook" with no recorded scope).
    cleared_panels = panel_content.invalidate_for_nodes(ws_id, batch_ids, owner_id)

    return {
        "status": "deleted", "id": node_id, "deleted_ids": batch_ids,
        "cleared_panels": cleared_panels,
        "secondary_data_ops_cleared": len(cleanup_ops),
    }


# --- silent note-taking agent candidates (see eo/note_candidates.py, §4.6)
# Same propose/accept/reject shape as workspace-fact candidates and
# cluster candidates above — a candidate note proposed by agents/note_taker.py
# while watching other chats in this workspace, never auto-committed.

@router.get("/api/workspaces/{ws_id}/notes/candidates", dependencies=[Depends(require_auth)])
def get_note_candidates(ws_id: str):
    return note_candidates.list_candidates(ws_id)


@router.post("/api/workspaces/{ws_id}/notes/candidates/{candidate_id}/accept", dependencies=[Depends(require_auth)])
def accept_note_candidate(ws_id: str, candidate_id: str):
    # FIX — bug audit §9: was `{index}: int`, see eo/note_candidates.py's
    # module docstring for why list-position addressing is unsafe now
    # that Part 8.4 lets two users watch/review the same pending list.
    try:
        return {"node_id": note_candidates.accept_candidate(ws_id, candidate_id)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")


@router.delete("/api/workspaces/{ws_id}/notes/candidates/{candidate_id}", dependencies=[Depends(require_auth)])
def reject_note_candidate(ws_id: str, candidate_id: str):
    try:
        note_candidates.reject_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")
    return {"status": "rejected", "candidate_id": candidate_id}


@router.post("/api/workspaces/{ws_id}/backlinks/detect")
def detect_backlinks_endpoint(ws_id: str, owner_id: str = Depends(require_auth)):
    """Part 4 §4.3 -- on-demand rescan rather than wired into every
    ingestion call: re-running this is cheap (edges_between() already
    skips anything already linked) and a manual "detect backlinks" action
    is simpler to reason about than re-scanning a whole workspace after
    every single new node."""
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return {"edges_created": detect_backlinks(ws_id)}


# --- auto-clustering (see agents/note_clusterer.py, Part 4 §4.3) ---------
# Same on-demand-rescan + candidate accept/reject shape as backlinks and
# workspace-fact proposals above -- the third use of this affordance in
# the build order, not a new UX pattern.

@router.post("/api/workspaces/{ws_id}/clusters/propose")
def propose_clusters_endpoint(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return {"candidates": propose_clusters(ws_id)}


@router.get("/api/workspaces/{ws_id}/clusters/candidates", dependencies=[Depends(require_auth)])
def get_cluster_candidates(ws_id: str):
    return list_cluster_candidates(ws_id)


@router.post("/api/workspaces/{ws_id}/clusters/candidates/{candidate_id}/accept", dependencies=[Depends(require_auth)])
def accept_cluster_candidate_endpoint(ws_id: str, candidate_id: str):
    try:
        return {"edges_created": accept_cluster_candidate(ws_id, candidate_id)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")


@router.delete("/api/workspaces/{ws_id}/clusters/candidates/{candidate_id}", dependencies=[Depends(require_auth)])
def reject_cluster_candidate_endpoint(ws_id: str, candidate_id: str):
    try:
        reject_cluster_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")
    return {"status": "rejected", "id": candidate_id}

