"""
api/server.py

The thin HTTP layer in front of api/task_runner.py.

Run locally:
    pip install fastapi uvicorn
    uvicorn api.server:app --reload --port 8000

CORS is open to the Next.js dev server origin (localhost:3000) only —
tighten this before deploying anywhere real.
"""
import os
import sys
import traceback
import tempfile


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, Depends, Query, UploadFile, File, Form
from eo.project_registry import list_projects, generate_control_unit, register_project
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Union, Any

from api.task_runner import run_task, preview_task, confirm_task, run_task_from_template   # preview/confirm NEW — Part 2 §2.5, run_task_from_template NEW — Part 2 §2.7
from eo.executor import resume_graph   # NEW — Part 2 §2.4
from eo.registry import list_known_roles, get_role_metadata, update_role_prompt, set_role_pinned, list_role_metadata   # NEW — Part 2 §2.7: Role Library panel; set_role_pinned NEW — pinned roles; list_role_metadata NEW — bulk read, fixes N+1
from eo.structure import (   # NEW — Part 2 §2.7: Workflow Template builder
    save_workflow_template, list_workflow_templates, delete_workflow_template, update_workflow_template,
)
from eo.quota_sentinel import get_quota_snapshot, get_usage_history, get_usage_history_scoped
from eo import chat_store
from eo import quiz_progress
from eo import memory_batch
from eo import chat_workspace
from eo import workspace_facts
from eo import graph_edges
from eo import note_candidates   # NEW — §4.7: silent note-taker's propose/accept/reject surface
from eo.knowledge_graph import list_nodes   # NEW — §4.7: Notebooks tab source list / mind map / backlinks all read this
from agents.backlink_detector import detect_backlinks
from agents.note_clusterer import propose_clusters, list_candidates as list_cluster_candidates, \
    accept_candidate as accept_cluster_candidate, reject_candidate as reject_cluster_candidate
from agents.note_table_builder import build_table
from agents.web_clipper import clip_url
from agents.video_ingestor import ingest_video
from agents.voice_ingestor import ingest_voice
from agents.importer import import_artifact, SUPPORTED_FORMATS as IMPORTABLE_FORMATS
from agents.source_ingestor import write_ingested_source
from agents.tts_synthesizer import synthesize_podcast
from agents.video_overview_builder import build_video_overview
from agents.exporter import export_artifact, SUPPORTED_FORMATS as EXPORTABLE_FORMATS
from graph.adapters import markdown_text_to_artifact
from fastapi.responses import FileResponse

app = FastAPI(title="MiniMe v6 — EO layer API")

# Part 4 §4.4 -- where generated reports/decks/scripts land before being
# handed back as a download. Sibling to eo/graph_edges.py's data/graph/
# and eo/chat_workspace.py's data/chats/ -- same "small dedicated
# subfolder under data/" convention this codebase already uses throughout.
NOTES_EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exports",
)

API_AUTH_MODE = os.getenv("API_AUTH_MODE", "api_key")
API_AUTH_SECRET = os.getenv("API_AUTH_SECRET")


def require_auth(request: Request):
    if not API_AUTH_SECRET:
        return  # auth disabled if no secret configured — same
                # fail-open-to-local-dev behavior the CORS default has
    provided = request.headers.get("x-api-key")
    if provided != API_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

class RegisterProjectRequest(BaseModel):
    path: str
    display_name: str


class CreateChatRequest(BaseModel):
    title: Optional[str] = "New Chat"
    template_id: Optional[str] = None   # NEW — one chat per template


class RenameChatRequest(BaseModel):
    title: str


class LinkChatsRequest(BaseModel):
    linked_chat_ids: list[str]


class CreateBatchRequest(BaseModel):
    name: str
    member_chat_ids: list[str]


class EstimateBatchRequest(BaseModel):
    chat_ids: list[str]


class RenameBatchRequest(BaseModel):
    name: str


class BatchMembersRequest(BaseModel):
    chat_ids: list[str]


class CreateWorkspaceRequest(BaseModel):
    name: str


class RenameWorkspaceRequest(BaseModel):
    name: str


class WorkspaceChatRequest(BaseModel):
    chat_id: str
    delete_chat: Optional[bool] = False


class AppendMessageRequest(BaseModel):
    message: dict


class WorkspaceFactsRequest(BaseModel):
    # Matches eo/workspace_facts.py's EMPTY_FACTS shape. All optional —
    # a settings-panel save can send just the fields it's touching;
    # set_facts() merges rather than requiring the full object every
    # time.
    brand_voice: Optional[str] = None
    target_user: Optional[str] = None
    tech_stack: Optional[list[str]] = None
    custom: Optional[dict[str, Any]] = None


class CreateEdgeRequest(BaseModel):
    from_node_id: str
    to_node_id: str
    relation: str = "related"


class ClipUrlRequest(BaseModel):
    url: str
    workspace_id: str


class ExportArtifactRequest(BaseModel):
    text: str                    # a generator role's raw Markdown stage_output
    title: str = "Untitled"
    fmt: str                     # one of agents/exporter.py's SUPPORTED_FORMATS
    workspace_id: Optional[str] = None
    tags: Optional[list[str]] = None


class BuildTableRequest(BaseModel):
    field_names: list[str]
    node_type: Optional[str] = None
    expanded: bool = False


class SynthesizePodcastRequest(BaseModel):
    script_text: str             # podcast_scriptwriter's raw Markdown stage_output
    title: str = "podcast"

class RecordQuizAttemptRequest(BaseModel):
    workspace_id: str
    quiz_node_id: str            # vector_id of the exported/stored quiz node
    quiz_text: str                # quiz_writer's raw Markdown stage_output
    answers: list[int]            # one option-index per question, in question order


class GradeQuizRequest(BaseModel):
    quiz_text: str                # quiz_writer's raw Markdown stage_output
    answers: list[int]

class BuildVideoOverviewRequest(BaseModel):
    slide_text: str               # slide_planner's raw Markdown stage_output
    podcast_title: str            # the `title` used in a prior POST
                                   # /api/notes/podcast/synthesize call for
                                   # this notebook -- locates that mp3 on
                                   # disk rather than re-synthesizing it
    title: str = "video_overview"


@app.post("/api/projects", dependencies=[Depends(require_auth)])
def register_project_endpoint(req: RegisterProjectRequest):
    unit = generate_control_unit(req.display_name)
    register_project(unit["unique_name"], req.path)
    return {"unique_name": unit["unique_name"], "root_path": req.path}


# --- persistent chats (see eo/chat_store.py) ------------------------------
# chat_id and session_id are the same string everywhere in this system —
# the sidebar creates a chat_id via POST /api/chats, and that value is
# passed straight through as session_id on /api/task.

@app.get("/api/chats", dependencies=[Depends(require_auth)])
def get_chats():
    return chat_store.list_chats()


@app.post("/api/chats", dependencies=[Depends(require_auth)])
def create_chat(req: CreateChatRequest):
    return chat_store.create_chat(title=req.title or "New Chat", template_id=req.template_id)


@app.get("/api/chats/{chat_id}", dependencies=[Depends(require_auth)])
def get_chat(chat_id: str):
    try:
        return chat_store.get_chat(chat_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@app.patch("/api/chats/{chat_id}/rename", dependencies=[Depends(require_auth)])
def rename_chat(chat_id: str, req: RenameChatRequest):
    try:
        return chat_store.rename_chat(chat_id, req.title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@app.patch("/api/chats/{chat_id}/links", dependencies=[Depends(require_auth)])
def link_chats(chat_id: str, req: LinkChatsRequest):
    try:
        return chat_store.set_linked_chats(chat_id, req.linked_chat_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@app.post("/api/chats/{chat_id}/messages", dependencies=[Depends(require_auth)])
def append_message(chat_id: str, req: AppendMessageRequest):
    return chat_store.append_message(chat_id, req.message)


@app.delete("/api/chats/{chat_id}", dependencies=[Depends(require_auth)])
def delete_chat(chat_id: str):
    chat_store.delete_chat(chat_id)
    return {"status": "deleted", "id": chat_id}


# --- memory batches: mutual-membership groups (see eo/memory_batch.py) ---

@app.get("/api/batches", dependencies=[Depends(require_auth)])
def get_batches():
    return memory_batch.list_batches()


@app.post("/api/batches/estimate", dependencies=[Depends(require_auth)])
def estimate_batch(req: EstimateBatchRequest):
    """Called live from the create-batch modal as the user checks/unchecks
    chats — NOT tied to an existing batch_id, since the whole point is to
    show the cost BEFORE creating one. See chat_store.estimate_batch_context_tokens."""
    return chat_store.estimate_batch_context_tokens(req.chat_ids)


@app.post("/api/batches", dependencies=[Depends(require_auth)])
def create_batch(req: CreateBatchRequest):
    try:
        return memory_batch.create_batch(req.name, req.member_chat_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/batches/{batch_id}/rename", dependencies=[Depends(require_auth)])
def rename_batch(batch_id: str, req: RenameBatchRequest):
    try:
        return memory_batch.rename_batch(batch_id, req.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")


@app.post("/api/batches/{batch_id}/unlink", dependencies=[Depends(require_auth)])
def unlink_batch_members(batch_id: str, req: BatchMembersRequest):
    """Returns {"dissolved": true} if removing these members collapsed the
    batch to <=1, otherwise returns the updated batch."""
    try:
        result = memory_batch.unlink_members(batch_id, req.chat_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")
    return result if result else {"dissolved": True, "id": batch_id}


@app.post("/api/batches/{batch_id}/members", dependencies=[Depends(require_auth)])
def add_batch_member(batch_id: str, req: BatchMembersRequest):
    try:
        for cid in req.chat_ids:
            memory_batch.add_member(batch_id, cid)
        return memory_batch.get_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")


@app.delete("/api/batches/{batch_id}", dependencies=[Depends(require_auth)])
def delete_batch(batch_id: str):
    try:
        memory_batch.delete_batch(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")
    return {"status": "deleted", "id": batch_id}


# --- workspaces: named containers with auto-linking membership (§7) ------
# UI label is "Projects" — named chat_workspace.py / /api/workspaces in
# code to avoid colliding with eo/project_registry.py, which tracks
# external codebase roots for Cross-Project File Control (unrelated
# concept, same word).

@app.get("/api/workspaces", dependencies=[Depends(require_auth)])
def get_workspaces():
    return chat_workspace.list_workspaces()


@app.post("/api/workspaces", dependencies=[Depends(require_auth)])
def create_workspace(req: CreateWorkspaceRequest):
    return chat_workspace.create_workspace(req.name)


@app.patch("/api/workspaces/{ws_id}/rename", dependencies=[Depends(require_auth)])
def rename_workspace(ws_id: str, req: RenameWorkspaceRequest):
    try:
        return chat_workspace.rename_workspace(ws_id, req.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@app.post("/api/workspaces/{ws_id}/chats", dependencies=[Depends(require_auth)])
def add_workspace_chat(ws_id: str, req: WorkspaceChatRequest):
    try:
        return chat_workspace.add_chat(ws_id, req.chat_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@app.delete("/api/workspaces/{ws_id}/chats/{chat_id}", dependencies=[Depends(require_auth)])
def remove_workspace_chat(ws_id: str, chat_id: str, delete_chat: bool = Query(False)):
    try:
        return chat_workspace.remove_chat(ws_id, chat_id, delete_chat=delete_chat)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@app.delete("/api/workspaces/{ws_id}", dependencies=[Depends(require_auth)])
def delete_workspace(ws_id: str):
    try:
        chat_workspace.delete_workspace(ws_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return {"status": "deleted", "id": ws_id}


# --- workspace facts: tier-3 memory (see eo/workspace_facts.py, §0.3) ----
# The settings-panel-facing surface for "facts true across the whole
# project" — brand voice, target user, tech stack, plus a free-form
# `custom` bucket. Reading these into agent prompts happens automatically
# inside eo/conversation_memory.py; nothing here needs to be called at
# generation time, only when the user views/edits the panel.

@app.get("/api/workspaces/{ws_id}/facts", dependencies=[Depends(require_auth)])
def get_workspace_facts(ws_id: str):
    chat_workspace.get_workspace(ws_id)  # 404s if the workspace doesn't exist
    return workspace_facts.get_facts(ws_id)


@app.put("/api/workspaces/{ws_id}/facts", dependencies=[Depends(require_auth)])
def put_workspace_facts(ws_id: str, req: WorkspaceFactsRequest):
    try:
        chat_workspace.get_workspace(ws_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    # exclude_unset -> a save that only touched brand_voice doesn't wipe
    # target_user/tech_stack/custom back to empty.
    return workspace_facts.set_facts(ws_id, req.dict(exclude_unset=True))


@app.get("/api/workspaces/{ws_id}/facts/candidates", dependencies=[Depends(require_auth)])
def get_workspace_fact_candidates(ws_id: str):
    """Agent-proposed facts awaiting user accept/reject — see
    workspace_facts.propose_fact()."""
    return workspace_facts.list_candidates(ws_id)


@app.post("/api/workspaces/{ws_id}/facts/candidates/{index}/accept", dependencies=[Depends(require_auth)])
def accept_workspace_fact_candidate(ws_id: str, index: int):
    try:
        return workspace_facts.accept_candidate(ws_id, index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Unknown candidate index")


@app.delete("/api/workspaces/{ws_id}/facts/candidates/{index}", dependencies=[Depends(require_auth)])
def reject_workspace_fact_candidate(ws_id: str, index: int):
    try:
        workspace_facts.reject_candidate(ws_id, index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Unknown candidate index")
    return {"status": "rejected", "index": index}


# --- knowledge-graph edges (see eo/graph_edges.py, §0.2) -----------------
# Auto-created edges are written directly by whichever agent produced
# them (no HTTP round-trip). This is the manual path: the "link to..."
# UI picker / drag-node-onto-node affordance calls this directly, no
# agent involvement.

@app.get("/api/graph/edges", dependencies=[Depends(require_auth)])
def get_graph_edges(workspace_id: Optional[str] = Query(None)):
    return graph_edges.list_edges(workspace_id=workspace_id)


@app.post("/api/graph/edges", dependencies=[Depends(require_auth)])
def create_graph_edge(req: CreateEdgeRequest):
    try:
        return graph_edges.create_edge(req.from_node_id, req.to_node_id, req.relation, created_by="user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/graph/edges/{edge_id}", dependencies=[Depends(require_auth)])
def delete_graph_edge(edge_id: str):
    try:
        graph_edges.delete_edge(edge_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edge_id")
    return {"status": "deleted", "id": edge_id}


# --- knowledge-graph nodes (see eo/knowledge_graph.py, §0.1) -------------
# §4.7: the Notebooks tab's one read for "everything in this notebook" —
# the source list, the mind map's underlying content, and
# KnowledgeGraphView's backlink visualization all page through this same
# list_nodes() call rather than each inventing their own fetch.

@app.get("/api/workspaces/{ws_id}/nodes", dependencies=[Depends(require_auth)])
def get_workspace_nodes(ws_id: str, node_type: Optional[str] = Query(None)):
    try:
        chat_workspace.get_workspace(ws_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return list_nodes(ws_id, node_type=node_type)


# --- silent note-taking agent candidates (see eo/note_candidates.py, §4.6)
# Same propose/accept/reject shape as workspace-fact candidates and
# cluster candidates above — a candidate note proposed by agents/note_taker.py
# while watching other chats in this workspace, never auto-committed.

@app.get("/api/workspaces/{ws_id}/notes/candidates", dependencies=[Depends(require_auth)])
def get_note_candidates(ws_id: str):
    return note_candidates.list_candidates(ws_id)


@app.post("/api/workspaces/{ws_id}/notes/candidates/{index}/accept", dependencies=[Depends(require_auth)])
def accept_note_candidate(ws_id: str, index: int):
    try:
        return {"node_id": note_candidates.accept_candidate(ws_id, index)}
    except IndexError:
        raise HTTPException(status_code=404, detail="Unknown candidate index")


@app.delete("/api/workspaces/{ws_id}/notes/candidates/{index}", dependencies=[Depends(require_auth)])
def reject_note_candidate(ws_id: str, index: int):
    try:
        note_candidates.reject_candidate(ws_id, index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Unknown candidate index")
    return {"status": "rejected", "index": index}


@app.post("/api/workspaces/{ws_id}/backlinks/detect", dependencies=[Depends(require_auth)])
def detect_backlinks_endpoint(ws_id: str):
    """Part 4 §4.3 -- on-demand rescan rather than wired into every
    ingestion call: re-running this is cheap (edges_between() already
    skips anything already linked) and a manual "detect backlinks" action
    is simpler to reason about than re-scanning a whole workspace after
    every single new node."""
    try:
        chat_workspace.get_workspace(ws_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return {"edges_created": detect_backlinks(ws_id)}


# --- auto-clustering (see agents/note_clusterer.py, Part 4 §4.3) ---------
# Same on-demand-rescan + candidate accept/reject shape as backlinks and
# workspace-fact proposals above -- the third use of this affordance in
# the build order, not a new UX pattern.

@app.post("/api/workspaces/{ws_id}/clusters/propose", dependencies=[Depends(require_auth)])
def propose_clusters_endpoint(ws_id: str):
    try:
        chat_workspace.get_workspace(ws_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return {"candidates": propose_clusters(ws_id)}


@app.get("/api/workspaces/{ws_id}/clusters/candidates", dependencies=[Depends(require_auth)])
def get_cluster_candidates(ws_id: str):
    return list_cluster_candidates(ws_id)


@app.post("/api/workspaces/{ws_id}/clusters/candidates/{candidate_id}/accept", dependencies=[Depends(require_auth)])
def accept_cluster_candidate_endpoint(ws_id: str, candidate_id: str):
    try:
        return {"edges_created": accept_cluster_candidate(ws_id, candidate_id)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")


@app.delete("/api/workspaces/{ws_id}/clusters/candidates/{candidate_id}", dependencies=[Depends(require_auth)])
def reject_cluster_candidate_endpoint(ws_id: str, candidate_id: str):
    try:
        reject_cluster_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")
    return {"status": "rejected", "id": candidate_id}


# --- data tables from scattered facts (see agents/note_table_builder.py,
# Part 4 §4.4) --------------------------------------------------------------
# Same directly-called, own-endpoint shape as backlinks/clustering above,
# not routed through the Panel/executor role-hiring pipeline -- see that
# module's docstring for why.

@app.post("/api/workspaces/{ws_id}/table", dependencies=[Depends(require_auth)])
def build_table_endpoint(ws_id: str, req: BuildTableRequest):
    try:
        chat_workspace.get_workspace(ws_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return build_table(ws_id, req.field_names, node_type=req.node_type, expanded=req.expanded)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- notes domain: capture (see agents/web_clipper.py, Part 4 §4.2) ------
# Driven by a small bookmarklet/extension that POSTs the current page's
# URL here. One new ingestion endpoint, not a new backend paradigm --
# same shape as every other write endpoint above, just backed by a
# deterministic tool agent instead of a memory-bus write. PDF/Office/
# video/voice ingestion land the same way once those ingestors exist;
# this is the first one wired end to end.

@app.post("/api/notes/clip", dependencies=[Depends(require_auth)])
def clip_url_endpoint(req: ClipUrlRequest):
    try:
        artifact = clip_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    node_ids = write_ingested_source(artifact, req.workspace_id, created_by="user")
    return {"node_ids": node_ids, "title": artifact["title"]}


@app.post("/api/notes/video", dependencies=[Depends(require_auth)])
def ingest_video_endpoint(req: ClipUrlRequest):
    # Reuses ClipUrlRequest -- identical {url, workspace_id} shape, no
    # reason for a separate model.
    try:
        artifact = ingest_video(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    node_ids = write_ingested_source(artifact, req.workspace_id, created_by="user")
    return {"node_ids": node_ids, "title": artifact["title"]}


@app.post("/api/notes/import", dependencies=[Depends(require_auth)])
async def import_file_endpoint(workspace_id: str = Form(...), file: UploadFile = File(...)):
    """Office/docx/pptx/xlsx/csv/md/json ingestion. No new parsing code —
    agents/importer.py (Part 0 §0.5) already reads every one of these
    formats back into the common artifact shape; this endpoint is just
    that plus write_ingested_source(), the same two-step shape
    /api/notes/clip above already uses. PDF is deliberately absent from
    IMPORTABLE_FORMATS -- that's agents/pdf_ingestor.py's job, not
    agents/importer.py's (see that module's own docstring)."""
    ext = os.path.splitext(file.filename or "")[1].lstrip(".").lower()
    if ext not in IMPORTABLE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported import format '{ext}'. Supported: {', '.join(IMPORTABLE_FORMATS)}.",
        )
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        artifact = import_artifact(tmp_path, fmt=ext)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.remove(tmp_path)
    node_ids = write_ingested_source(artifact, workspace_id, created_by="user")
    return {"node_ids": node_ids, "title": artifact["title"]}


@app.post("/api/notes/voice", dependencies=[Depends(require_auth)])
async def ingest_voice_endpoint(workspace_id: str = Form(...), file: UploadFile = File(...)):
    """Voice notes / meeting recordings -- agents/voice_ingestor.py
    transcribes locally (faster-whisper, no API key), same temp-file-
    then-cleanup shape as /api/notes/import above. No format allowlist
    here: unlike Office import, faster-whisper/ffmpeg handles a broad
    range of audio containers, and an unsupported one already surfaces
    as ingest_voice()'s own ValueError -> 400 rather than needing a
    second check here."""
    suffix = os.path.splitext(file.filename or "")[1] or ".audio"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        artifact = ingest_voice(tmp_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.remove(tmp_path)
    node_ids = write_ingested_source(artifact, workspace_id, created_by="user")
    return {"node_ids": node_ids, "title": artifact["title"]}


# --- notes domain: generate (see agents/exporter.py, Part 0 §0.5 /
# graph/adapters.py, Part 4 §4.4) ------------------------------------------
# Turns a generator role's raw Markdown stage_output (mapper, report_writer,
# slide_planner, podcast_scriptwriter -- every one asks for headered
# Markdown via generic_worker.py's MARKDOWN_INSTRUCTION) into a real file.
# Takes the text straight from the client rather than re-reading it off
# the memory bus here: stage_output:* keys are app_slug-namespaced
# (memory/bus.py's _namespaced()), and the client already has the exact
# text it rendered to the user, so this sidesteps reconstructing that
# namespace server-side.

@app.post("/api/notes/export", dependencies=[Depends(require_auth)])
def export_artifact_endpoint(req: ExportArtifactRequest):
    fmt = req.fmt.lower().lstrip(".")
    if fmt not in EXPORTABLE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported export format '{fmt}'. Supported: {', '.join(EXPORTABLE_FORMATS)}.",
        )
    artifact = markdown_text_to_artifact(
        req.text, title_fallback=req.title,
        workspace_id=req.workspace_id, tags=req.tags,
    )
    path = export_artifact(artifact, fmt, NOTES_EXPORTS_DIR)
    return FileResponse(path, filename=os.path.basename(path))


# --- notes domain: podcast synthesis (see agents/tts_synthesizer.py,
# Part 4 §4.4) --------------------------------------------------------------
# Synthesis half of Audio Overview. Takes podcast_scriptwriter's raw
# Markdown stage_output straight from the client, same take-the-text-
# from-the-client reasoning export_artifact_endpoint above already uses —
# no re-read off the namespaced memory bus here either. No LLM call in
# this handler; synthesize_podcast() is pure edge-tts.

@app.post("/api/notes/podcast/synthesize", dependencies=[Depends(require_auth)])
def synthesize_podcast_endpoint(req: SynthesizePodcastRequest):
    safe_title = "".join(c for c in req.title if c.isalnum() or c in ("-", "_")) or "podcast"
    out_path = os.path.join(NOTES_EXPORTS_DIR, f"{safe_title}.mp3")
    try:
        synthesize_podcast(req.script_text, out_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(out_path, filename=os.path.basename(out_path))


# --- notes domain: Video Overview (see agents/video_overview_builder.py,
# Part 4 §4.4) ----------------------------------------------------------
# Labeled "narrated slideshow" in-product, not "video" -- see that
# module's docstring for why. Reuses slide_planner's own Markdown via the
# same markdown_text_to_artifact() adapter export_artifact_endpoint above
# already uses. The podcast audio is NOT re-synthesized here -- it's
# located on disk by `podcast_title`, the same safe-slugified filename
# synthesize_podcast_endpoint above already writes to NOTES_EXPORTS_DIR.
# This by-title lookup is a deliberate simplification (no session_id/
# workspace_id-keyed store for exports exists yet); call podcast
# synthesis first with a title, then pass that same title here.

@app.post("/api/notes/video-overview/build", dependencies=[Depends(require_auth)])
def build_video_overview_endpoint(req: BuildVideoOverviewRequest):
    safe_podcast_title = "".join(c for c in req.podcast_title if c.isalnum() or c in ("-", "_")) or "podcast"
    audio_path = os.path.join(NOTES_EXPORTS_DIR, f"{safe_podcast_title}.mp3")
    if not os.path.exists(audio_path):
        raise HTTPException(
            status_code=404,
            detail=(f"No synthesized podcast audio found for title {req.podcast_title!r}. "
                     "Call POST /api/notes/podcast/synthesize with this title first."),
        )
    slide_artifact = markdown_text_to_artifact(req.slide_text, title_fallback=req.title)
    safe_title = "".join(c for c in req.title if c.isalnum() or c in ("-", "_")) or "video_overview"
    out_path = os.path.join(NOTES_EXPORTS_DIR, f"{safe_title}.mp4")
    try:
        build_video_overview(slide_artifact, audio_path, out_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(out_path, filename=os.path.basename(out_path))

# --- notes domain: study tools (see eo/quiz_progress.py, Part 4 §4.5) -----
# flashcard_writer/study_guide_writer need no new endpoint -- both already
# use the '# Title' / '## Heading' grammar export_artifact_endpoint above
# already handles, same as report_writer. quiz_writer's output round-trips
# through that same endpoint too (its '- [ ]'/'- [x]' lines are just
# ordinary section content to markdown_text_to_artifact()) -- these
# endpoints only cover what export/import can't: grading a submission
# against quiz_writer's own Markdown and recording the result.

@app.post("/api/notes/study/quiz/grade", dependencies=[Depends(require_auth)])
def grade_quiz_endpoint(req: GradeQuizRequest):
    """Grades without persisting -- lets the frontend show results before
    committing an attempt (e.g. a "check my answers" button before final
    submit). POST .../attempts below does the same grading AND records
    it; this is the preview-only half."""
    return quiz_progress.grade_quiz(req.quiz_text, req.answers)


@app.post("/api/notes/study/quiz/attempts", dependencies=[Depends(require_auth)])
def record_quiz_attempt_endpoint(req: RecordQuizAttemptRequest):
    return quiz_progress.record_attempt(
        workspace_id=req.workspace_id,
        quiz_node_id=req.quiz_node_id,
        quiz_markdown=req.quiz_text,
        answers=req.answers,
        created_by="user",
    )


@app.get("/api/notes/study/quiz/attempts", dependencies=[Depends(require_auth)])
def list_quiz_attempts_endpoint(workspace_id: str = Query(...),
                                 quiz_node_id: Optional[str] = Query(None)):
    return quiz_progress.list_attempts(workspace_id, quiz_node_id)


@app.get("/api/notes/study/quiz/missed", dependencies=[Depends(require_auth)])
def missed_quiz_questions_endpoint(workspace_id: str = Query(...),
                                    quiz_node_id: str = Query(...)):
    return quiz_progress.get_missed_questions(workspace_id, quiz_node_id)

class TaskRequest(BaseModel):
    task_text: str
    tier_override: Optional[int] = None
    directed_task_type: Optional[str] = None
    app_slug: Optional[str] = None
    run_tests: bool = False
    session_id: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None
    approval_roles: Optional[list[str]] = None   # NEW — Part 2 §2.4: role
    # names that require a human approval pause after they finish
    # (tier-3 hires-driven path only). None/empty = full-auto, unchanged
    # default behavior.


class TaskResponse(BaseModel):
    # tier is int for tiers 0-3, or the literal string "sga" when the
    # Starter General Agents resolved the task before classification —
    # loosened from `int` to fix a latent validation bug that would have
    # 500'd on every real SGA-resolved HTTP request.
    decision: dict
    tier: Union[int, str]
    session_id: Optional[str] = None
    # status values: "ok" | "error" | "needs_app" | "needs_directed_task_type"
    # | "not_wired_yet" | "needs_beast_mode_confirmation" | "needs_beast_mode_choice"
    # | "paused" (Part 2 §2.4 — a role in approval_roles just finished;
    #   POST /api/resume with the same session_id to continue)
    # | "preview_ready" (Part 2 §2.5 — only ever returned by
    #   POST /api/task/preview, never by this endpoint; a real, editable
    #   hires list is sitting in result.hires. POST /api/task/confirm
    #   with this same session_id, decision, and hires to dispatch.)
    status: str
    result: Optional[dict] = None
    message: Optional[str] = None


@app.post("/api/task", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task(req: TaskRequest):
    try:
        return run_task(
            task_text=req.task_text,
            tier_override=req.tier_override,
            directed_task_type_override=req.directed_task_type,
            app_slug=req.app_slug,
            run_tests=req.run_tests,
            session_id=req.session_id,
            mode=req.mode,
            project_unique_name=req.project_unique_name,
            approval_roles=set(req.approval_roles) if req.approval_roles else None,   # NEW
        )
    except Exception as exc:
        # No relay-based error surface here, so a stack trace on the
        # server console is the only debugging signal you get — keep it,
        # but also return a clean JSON error instead of a raw 500 with no
        # body.
        traceback.print_exc()
        return TaskResponse(
            decision={},
            tier=-1,
            status="error",
            result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


class PreviewTaskRequest(BaseModel):
    # Same shape as TaskRequest, minus approval_roles — approval_roles is
    # only meaningful once a run actually dispatches (confirm_task()/
    # run_task()), not at the preview stage.
    task_text: str
    tier_override: Optional[int] = None
    directed_task_type: Optional[str] = None
    app_slug: Optional[str] = None
    run_tests: bool = False
    session_id: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None


class HireEdit(BaseModel):
    # Part 2 §2.5 — one entry from a preview_task() response's
    # result.hires, echoed back (possibly edited) to /api/task/confirm.
    role: str
    agent_key: str
    brief: str
    update_library: Optional[bool] = False   # "just this once" (default)
    # vs "update the library" (True — calls eo/registry.py's
    # update_role_prompt(), making this edit the new stored default for
    # every future hire of this role).


class ConfirmTaskRequest(BaseModel):
    task_text: str
    decision: dict          # the unmodified `decision` object from the
                             # matching preview_task() response
    hires: list[HireEdit]   # possibly user-edited hires from that same response
    session_id: str
    app_slug: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None
    approval_roles: Optional[list[str]] = None   # same meaning as on /api/task


@app.post("/api/task/preview", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task_preview(req: PreviewTaskRequest):
    """Part 2 §2.5 — runs classification + staff_task() and stops before
    dispatch whenever there's a real, editable hires list (tier 2/3).
    Everything else (cache hit, SGA, needs_beast_mode_*, tier 0/1,
    hires-empty tier 2/3) runs straight through to a normal finished
    response, exactly like POST /api/task — there's nothing to review
    on those paths, so this endpoint doesn't invent an empty review step
    for them. See preview_task()'s own docstring for the full breakdown."""
    try:
        return preview_task(
            task_text=req.task_text,
            tier_override=req.tier_override,
            directed_task_type_override=req.directed_task_type,
            app_slug=req.app_slug,
            run_tests=req.run_tests,
            session_id=req.session_id,
            mode=req.mode,
            project_unique_name=req.project_unique_name,
        )
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision={}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


@app.post("/api/task/confirm", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task_confirm(req: ConfirmTaskRequest):
    """Part 2 §2.5 — dispatches a (possibly user-edited) hires list from
    a prior POST /api/task/preview response, without calling staff_task()
    again. Each hire's `update_library` flag controls whether an edited
    brief is a one-off override or becomes the new stored default via
    eo/registry.py's update_role_prompt() (2.2)."""
    try:
        return confirm_task(
            task_text=req.task_text,
            decision=req.decision,
            hires=[h.dict() for h in req.hires],
            session_id=req.session_id,
            app_slug=req.app_slug,
            mode=req.mode,
            project_unique_name=req.project_unique_name,
            approval_roles=set(req.approval_roles) if req.approval_roles else None,
        )
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision=req.decision or {}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


class ResumeRequest(BaseModel):
    # Part 2 §2.4
    session_id: str
    action: str          # "approve" | "edit" | "reject_redo"
    text: Optional[str] = None   # required when action == "edit"


class ResumeResponse(BaseModel):
    session_id: str
    # status values: "ok" | "paused" | "error"
    status: str
    result: Optional[dict] = None
    message: Optional[str] = None


@app.post("/api/resume", response_model=ResumeResponse, dependencies=[Depends(require_auth)])
def post_resume(req: ResumeRequest):
    """Part 2 §2.4: resumes a run paused at an approval_roles checkpoint.
    Mirrors post_task()'s error-handling shape (clean JSON on unexpected
    failure, real HTTP status codes for the specific, anticipated
    failure modes resume_graph() raises)."""
    decision = {"action": req.action}
    if req.action == "edit":
        decision["text"] = req.text or ""

    try:
        result = resume_graph(req.session_id, decision)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No paused run for session_id={req.session_id!r}")
    except RuntimeError as exc:
        # reject_redo hit MAX_STAGE_REVISITS — a real conflict (the run
        # cannot resume as requested), not a client input error.
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        # unknown action string
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        return ResumeResponse(
            session_id=req.session_id,
            status="error",
            result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )

    if isinstance(result, dict) and result.get("status") == "paused":
        return ResumeResponse(
            session_id=req.session_id,
            status="paused",
            result={"paused_at_role": result["paused_at_role"]},
            message=(f"Run paused again for approval at role "
                     f"'{result['paused_at_role']}'. POST to /api/resume again to continue."),
        )

    # Finished — result here is the same role-keyed results dict
    # execute_graph()/_run_loop() always returns. Mirrors
    # api/task_runner.py's _run_tier3_hires() rendering of the final
    # role's output, so a resumed run's answer looks the same as one
    # that never paused.
    from eo.result_render import render_agent_result
    final_role = list(result.keys())[-1] if result else None
    final_output = result.get(final_role) if final_role else None
    answer = render_agent_result(final_output) if final_output is not None else ""

    return ResumeResponse(
        session_id=req.session_id,
        status="ok",
        result={"output": result, "answer": answer, "final_role": final_role},
        message=None,
    )


# ---------------------------------------------------------------------------
# Part 2 §2.7 — thin HTTP layer over eo/registry.py's Role Library (§2.2)
# and eo/structure.py's Workflow Templates (§2.3/§2.6). Both backing
# stores and their functions already existed; these five routes are the
# only thing that was actually missing before the frontend panels below
# could read or write real data.
# ---------------------------------------------------------------------------

@app.get("/api/roles", dependencies=[Depends(require_auth)])
def get_roles():
    """Every role the system has ever briefed, metadata included — the
    Role Library panel's one data source. Shape: [{role, brief, source,
    updated_at, times_hired}, ...]. Uses list_role_metadata() for a
    single bulk read instead of list_known_roles()+get_role_metadata()
    per role, which was doing N+1 round-trips against the memory bus."""
    return list_role_metadata()


class UpdateRoleRequest(BaseModel):
    brief: str


@app.put("/api/roles/{role_name}", dependencies=[Depends(require_auth)])
def put_role(role_name: str, req: UpdateRoleRequest):
    """Saves an inline Role Library edit. Always source="user_edited" —
    this is the one path that's allowed to claim that (see
    eo/registry.py's update_role_prompt() docstring)."""
    update_role_prompt(role_name, req.brief, source="user_edited")
    return {"role": role_name, **(get_role_metadata(role_name) or {})}


class SetRolePinnedRequest(BaseModel):
    pinned: bool


@app.patch("/api/roles/{role_name}/pin", dependencies=[Depends(require_auth)])
def patch_role_pinned(role_name: str, req: SetRolePinnedRequest):
    """Pinned-roles feature — server-persisted so it syncs across
    devices, same store as everything else in the Role Library. Doesn't
    require the role to already have a brief; a role can be pinned from
    a picker before it's ever been hired."""
    entry = set_role_pinned(role_name, req.pinned)
    return {"role": role_name, **entry}


class SaveWorkflowTemplateRequest(BaseModel):
    name: str
    roles: list   # role-name strings, or nested lists of them for a
                  # concurrent group (eo/structure.py §2.6) — validated
                  # by save_workflow_template() itself.
    description: str = ""
    domain_hint: Optional[str] = None
    approval_roles: Optional[list[str]] = None
    no_conversation_context_roles: Optional[list[str]] = None
    created_by: Optional[str] = None


@app.get("/api/workflow-templates", dependencies=[Depends(require_auth)])
def get_workflow_templates():
    """Every saved template, newest first — for the template picker and
    the Workflow Template builder's own list view."""
    return list_workflow_templates()


@app.get("/api/workflow-templates/{template_id}/chat", dependencies=[Depends(require_auth)])
def get_template_chat(template_id: str):
    """The one chat this template already owns, if any — lets the
    frontend reuse it instead of minting a new chat on every run."""
    chat = chat_store.find_chat_for_template(template_id)
    return chat or {}


@app.post("/api/workflow-templates", dependencies=[Depends(require_auth)])
def post_workflow_template(req: SaveWorkflowTemplateRequest):
    """Covers both write paths the design calls for: "save from a
    finished run" (caller passes that run's own execution_order as
    `roles`) and "build from scratch" (caller passes a list assembled in
    the Role Library UI) — both are just a plain roles list here."""
    try:
        return save_workflow_template(
            name=req.name,
            roles=req.roles,
            description=req.description,
            domain_hint=req.domain_hint,
            approval_roles=req.approval_roles,
            no_conversation_context_roles=req.no_conversation_context_roles,
            created_by=req.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/workflow-templates/{template_id}", dependencies=[Depends(require_auth)])
def put_workflow_template(template_id: str, req: SaveWorkflowTemplateRequest):
    """Template editing — there was previously no update path at all,
    only save (create) and delete."""
    updated = update_workflow_template(
        template_id, name=req.name, roles=req.roles, description=req.description,
        domain_hint=req.domain_hint, approval_roles=req.approval_roles,
        no_conversation_context_roles=req.no_conversation_context_roles,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown template_id={template_id!r}")
    return updated


@app.delete("/api/workflow-templates/{template_id}", dependencies=[Depends(require_auth)])
def delete_workflow_template_endpoint(template_id: str):
    deleted = delete_workflow_template(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Unknown template_id={template_id!r}")
    return {"status": "deleted", "template_id": template_id}


class RunFromTemplateRequest(BaseModel):
    template_id: str
    task_text: str
    session_id: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None


@app.post("/api/task/from-template", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task_from_template(req: RunFromTemplateRequest):
    """Part 2 §2.3/§2.6 — starts a new task from a saved workflow
    template instead of running Inspector/Panel classification.
    Mirrors post_task()'s exact error-handling shape."""
    try:
        return run_task_from_template(
            template_id=req.template_id,
            task_text=req.task_text,
            session_id=req.session_id,
            mode=req.mode,
            project_unique_name=req.project_unique_name,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision={}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/quota", dependencies=[Depends(require_auth)])
def quota():
    return get_quota_snapshot()


@app.get("/api/usage/history", dependencies=[Depends(require_auth)])
def usage_history(
    days: int = Query(7, ge=1, le=90),
    domain: Optional[str] = Query(None),
    workspace_id: Optional[str] = Query(None),
):
    # Cross-session, persisted day-by-day usage -- reads the same
    # usage:{provider}:{key_id}:{date} records /api/quota already reads
    # for today, just repeated across the last `days` calendar dates.
    # See eo/quota_sentinel.py's get_usage_history() docstring for the
    # exact response shape.
    #
    # Part 2 §2.6 -- when domain and/or workspace_id is given, this
    # branches to get_usage_history_scoped() instead, returning
    # {dates, domain, workspace} (see that function's docstring) rather
    # than {dates, providers, accounts}. Same route, response shape
    # depends on query params -- exactly the way `days` already changes
    # this endpoint's window without becoming a separate route.
    if domain or workspace_id:
        return get_usage_history_scoped(days=days, domain=domain, workspace_id=workspace_id)
    return get_usage_history(days=days)


@app.get("/api/projects", dependencies=[Depends(require_auth)])
def projects():
    return list_projects()