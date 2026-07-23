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
import zipfile   # NEW — Part 8.7: bundling multi-chat file exports
import json   # NEW — Part 7 §7.3: parsing integration_flagger's fenced json block
import re     # NEW — Part 7 §7.3
import requests  # NEW — Part 8.3: Admin API lookup for workspace-invite-by-email
import secrets   # NEW — Part 8.5: OAuth state tokens
import urllib.parse  # NEW — Part 8.5: building the Google consent URL
from eo import panel_content
from agents import pagespeed_agent   # NEW — Step 2: PageSpeed Insights connector for GrowthTab's Content Audit
from agents.part_price_finder import find_price
from eo.knowledge_graph import list_nodes, delete_node, rename_node   # rename_node added
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if python-dotenv isn't installed; real env vars can be set directly instead

import jwt
from jwt import PyJWKClient

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")  # only used as a fallback for
                                                          # projects still on the legacy
                                                          # shared HS256 secret — most
                                                          # current Supabase projects sign
                                                          # with an asymmetric key instead
                                                          # (see require_auth() below).
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # NEW — Part 8.3: admin
                                                          # key, used ONLY to resolve an
                                                          # invited collaborator's email to
                                                          # their user_id. Never sent to a
                                                          # client, never used for auth.

# NEW — Part 8.5: Google Calendar OAuth. Same "read from env, fail loud at
# the point of use if missing" convention as the Supabase vars above.
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
# e.g. "https://your-api-host/api/integrations/google_calendar/callback"

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Lazily-built JWKS client — fetches and caches Supabase's public signing
# keys from its well-known endpoint. This is what verifies the asymmetric
# (ES256/RS256) tokens that current Supabase projects issue by default.
# No secret involved on this path: these are public keys, safe to fetch
# over the network on every cold start.
_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not SUPABASE_URL:
            raise HTTPException(
                status_code=500,
                detail="Server misconfigured: SUPABASE_URL is not set.",
            )
        _jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _jwk_client

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
    STRUCTURE_TEMPLATES,   # NEW — Test tab: /simulate reads the "simulate" domain's own role list
)
from eo.quota_sentinel import get_quota_snapshot, get_usage_history, get_usage_history_scoped
from memory.bus import read_many as bus_read_many, set_app_slug, KEYS   # NEW — Part 7 §7.2: GET /api/tasks/{session_id}
from eo.errors import MissingDependencyError   # NEW — Part 7 §7.4: deploy endpoints' 409 handling
from eo import chat_store
from eo import quiz_progress
from eo import memory_batch
from eo import chat_workspace
from eo import audit_log   # NEW — Part 8.6: audit log read endpoints
from eo import integrations   # NEW — Part 8.5: third-party OAuth credential storage
from agents import calendar_agent   # NEW — Part 8.5: Google Calendar connector
from agents.calendar_agent import IntegrationNotConnectedError   # NEW — Part 8.5
from fastapi.responses import RedirectResponse   # NEW — Part 8.5: OAuth callback redirect
from eo import workspace_facts
from eo import graph_edges
from eo import note_candidates   # NEW — §4.7: silent note-taker's propose/accept/reject surface
from eo.knowledge_graph import list_nodes, delete_node   # NEW — §2 fix: delete_node was added, list_nodes already here
from agents.backlink_detector import detect_backlinks
from agents.note_clusterer import propose_clusters, list_candidates as list_cluster_candidates, \
    accept_candidate as accept_cluster_candidate, reject_candidate as reject_cluster_candidate
from agents.note_table_builder import build_table
from agents import deploy_config_writer as deploy_config_writer_agent   # NEW — Part 7 §7.4
from agents import deploy_agent as deploy_agent_module                  # NEW — Part 7 §7.4
from agents.web_clipper import clip_url
from agents.video_ingestor import ingest_video
from agents.voice_ingestor import ingest_voice
from agents.importer import import_artifact, SUPPORTED_FORMATS as IMPORTABLE_FORMATS
from agents.pdf_ingestor import ingest_pdf   # NEW — §1 fix: PDF ingestion, was never wired to an endpoint
from agents.source_ingestor import write_ingested_source
from agents.tts_synthesizer import synthesize_podcast
from agents.video_overview_builder import build_video_overview
from agents.exporter import export_artifact, SUPPORTED_FORMATS as EXPORTABLE_FORMATS
from graph.adapters import markdown_text_to_artifact, chat_to_artifact   # chat_to_artifact NEW — Part 8.7
from fastapi.responses import FileResponse

app = FastAPI(title="MiniMe v6 — EO layer API")

# Part 4 §4.4 -- where generated reports/decks/scripts land before being
# handed back as a download. Sibling to eo/graph_edges.py's data/graph/
# and eo/chat_workspace.py's data/chats/ -- same "small dedicated
# subfolder under data/" convention this codebase already uses throughout.
NOTES_EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exports",
)

def require_auth(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header[len("Bearer "):].strip()

    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    alg = header.get("alg", "")

    try:
        if alg == "HS256":
            # Legacy shared-secret projects only.
            if not SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status_code=500,
                    detail="Server misconfigured: SUPABASE_JWT_SECRET is not set.",
                )
            payload = jwt.decode(
                token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated",
            )
        else:
            # Current Supabase default: asymmetric signing (ES256/RS256).
            # get_signing_key_from_jwt looks up the right public key by the
            # token's own `kid`, so this works whether Supabase issued
            # ES256, RS256, or rotates keys later — nothing here is
            # hardcoded to one algorithm except what the token itself claims.
            signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token, signing_key.key, algorithms=[alg], audience="authenticated",
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")

    request.state.user_id = user_id  # kept for any code that reads it off the request directly
    return user_id


def _lookup_user_id_by_email(email: str) -> str | None:
    """Admin-API lookup, used only by the workspace-invite endpoint to
    turn 'alice@example.com' into a user_id. Paginates and matches
    exactly rather than trusting the API's own email filter — it did
    not reliably filter server-side during testing (see scripts/
    get_test_jwt.py's find_user_by_email, which hit the same issue and
    was fixed the same way)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: SUPABASE_SERVICE_ROLE_KEY is not set.",
        )
    page = 1
    per_page = 200
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            params={"page": page, "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("users", [])
        if not users:
            return None
        for u in users:
            if (u.get("email") or "").lower() == email.lower():
                return u["id"]
        if len(users) < per_page:
            return None
        page += 1


def _lookup_users_by_ids(user_ids: set[str]) -> dict:
    """Admin-API lookup, the reverse of _lookup_user_id_by_email — turns a
    set of user_ids into {id: {email, name, avatar_url}} so member rosters
    can show a real identity instead of a raw UUID. Same single-pass,
    early-exit-once-all-found pagination as the email lookup above; a
    workspace roster is small (partners/moderators/etc., not a whole
    user base), so this stays cheap even without caching.

    'name' falls back through user_metadata's common shapes (Supabase
    email/password signup doesn't set any of these — only OAuth
    providers or an app-side profile step would — so the final fallback
    is the local part of the email, then the raw id if even email is
    somehow missing).
    """
    if not user_ids:
        return {}
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: SUPABASE_SERVICE_ROLE_KEY is not set.",
        )
    remaining = set(user_ids)
    found = {}
    page = 1
    per_page = 200
    while remaining:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            params={"page": page, "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("users", [])
        if not users:
            break
        for u in users:
            if u["id"] in remaining:
                meta = u.get("user_metadata") or {}
                email = u.get("email")
                found[u["id"]] = {
                    "email": email,
                    "name": meta.get("full_name") or meta.get("name")
                            or (email.split("@")[0] if email else u["id"]),
                    "avatar_url": meta.get("avatar_url") or meta.get("picture"),
                }
                remaining.discard(u["id"])
        if len(users) < per_page:
            break
        page += 1
    return found


ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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

class PromoteWorkspaceRequest(BaseModel):
    to_stage: Optional[str] = None
    # NEW — §2.2: "complete" (default) is today's unchanged behavior —
    # workspace leaves the old tab entirely. "partial" keeps it active
    # in both the old and new tab (see chat_workspace.promote()).
    mode: Optional[str] = "complete"


class WorkspaceChatRequest(BaseModel):
    chat_id: str
    delete_chat: Optional[bool] = False


class CreateWorkspaceChatRequest(BaseModel):
    title: Optional[str] = "New Chat"


class AddWorkspaceMemberRequest(BaseModel):
    email: str
    role: str = "viewer"  # 'viewer' or 'editor'


class UpdateWorkspaceMemberRequest(BaseModel):
    role: str  # 'viewer', 'editor', 'moderator', or 'partner'


class LeaveWorkspaceRequest(BaseModel):
    successor_id: Optional[str] = None  # owner-only; must be a current partner

class RenameNodeRequest(BaseModel):
    title: str

class CastVoteRequest(BaseModel):
    vote_target: Optional[str] = None  # another partner's user_id, or None = "stay joint"


class SetAttributionRequest(BaseModel):
    show: bool


class AttributionGrantRequest(BaseModel):
    can_toggle: bool


class ImportWorkspaceDataRequest(BaseModel):
    manifest: dict   # the exact object returned by GET /api/workspaces/{ws_id}/export


class AppendMessageRequest(BaseModel):
    message: dict

class RefreshPricesRequest(BaseModel):
    parts: list[dict]        # each: {"id","name","category","qty", ...}
    force_refresh: bool = False

class ToggleInstructionStepRequest(BaseModel):
    done: bool

class WorkspaceFactsRequest(BaseModel):
    # Matches eo/workspace_facts.py's EMPTY_FACTS shape. All optional —
    # a settings-panel save can send just the fields it's touching;
    # set_facts() merges rather than requiring the full object every
    # time.
    brand_voice: Optional[str] = None
    target_user: Optional[str] = None
    tech_stack: Optional[list[str]] = None
    custom: Optional[dict[str, Any]] = None

class PanelContentRequest(BaseModel):
    content: str

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


class SimulateRequest(BaseModel):
    session_id: str


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

def _resolve_chat_or_404(chat_id: str, user_id: str, require_edit: bool = False) -> str:
    """Confirms user_id has access to chat_id — as its owner, or as a
    workspace collaborator — and returns the chat's REAL owner_id, which
    every chat_store function below must be called with (never the
    requester's own id, unless they happen to be the same person).
    Raises 404 for no access at all (never distinguishes 'doesn't
    exist' from 'exists but isn't shared with you'), and 403 if the
    requester has viewer-only access but the route needs edit rights."""
    resolved = chat_store.resolve_chat_access(chat_id, user_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown chat_id")
    real_owner_id, role = resolved
    if require_edit and role == "viewer":
        raise HTTPException(status_code=403, detail="Viewer access does not permit this action")
    return real_owner_id


@app.get("/api/chats")
def get_chats(owner_id: str = Depends(require_auth)):
    return chat_store.list_chats(owner_id)


@app.post("/api/chats")
def create_chat(req: CreateChatRequest, owner_id: str = Depends(require_auth)):
    return chat_store.create_chat(owner_id, title=req.title or "New Chat", template_id=req.template_id)


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, owner_id: str = Depends(require_auth)):
    real_owner_id = _resolve_chat_or_404(chat_id, owner_id)
    try:
        chat = chat_store.get_chat(chat_id, real_owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")

    # Part 8.4: strip author_id from each message if this requester's
    # role/workspace setting says they shouldn't see who-wrote-what.
    # `owner_id` here is the ACTUAL caller (pre-resolution) — the right
    # identity to check attribution visibility against, not real_owner_id.
    ws_id = chat.get("workspace_id")
    if ws_id and not chat_workspace.can_see_attribution(ws_id, owner_id):
        chat["messages"] = [
            {k: v for k, v in m.items() if k != "author_id"} for m in chat.get("messages", [])
        ]
    return chat


@app.patch("/api/chats/{chat_id}/rename")
def rename_chat(chat_id: str, req: RenameChatRequest, owner_id: str = Depends(require_auth)):
    real_owner_id = _resolve_chat_or_404(chat_id, owner_id, require_edit=True)
    try:
        return chat_store.rename_chat(chat_id, real_owner_id, req.title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@app.patch("/api/chats/{chat_id}/links")
def link_chats(chat_id: str, req: LinkChatsRequest, owner_id: str = Depends(require_auth)):
    real_owner_id = _resolve_chat_or_404(chat_id, owner_id, require_edit=True)
    try:
        return chat_store.set_linked_chats(chat_id, real_owner_id, req.linked_chat_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@app.post("/api/chats/{chat_id}/messages")
def append_message(chat_id: str, req: AppendMessageRequest, owner_id: str = Depends(require_auth)):
    # append_message historically auto-creates the chat row on first
    # use (a brand-new chat_id with no row yet) — that path stays
    # owner_id-scoped to the caller, since it's genuinely their new
    # chat. Only route through the collaborator resolver when the
    # chat_id already belongs to someone else.
    if chat_store.chat_exists(chat_id, owner_id):
        real_owner_id = owner_id
    else:
        resolved = chat_store.resolve_chat_access(chat_id, owner_id)
        if resolved is None:
            real_owner_id = owner_id  # genuinely new chat_id — caller becomes its owner
        else:
            real_owner_id, role = resolved
            if role == "viewer":
                raise HTTPException(status_code=403, detail="Viewer access does not permit this action")

    # Part 8.4: stamp the ACTUAL acting user (owner_id, pre-resolution) as
    # author_id — never real_owner_id, which is the chat's owner and may be
    # a different person than whoever is actually typing this message.
    message = dict(req.message)
    message["author_id"] = owner_id
    return chat_store.append_message(chat_id, real_owner_id, message)


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, owner_id: str = Depends(require_auth)):
    # Deliberately NOT routed through the collaborator resolver — outright
    # deletion stays owner-only, same discipline as workspace deletion. An
    # editor can remove a chat from the workspace grouping (see
    # chat_workspace.remove_chat) but cannot delete someone else's chat.
    chat_store.delete_chat(chat_id, owner_id)
    return {"status": "deleted", "id": chat_id}


# --- memory batches: mutual-membership groups (see eo/memory_batch.py) ---

@app.get("/api/batches")
def get_batches(owner_id: str = Depends(require_auth)):
    return memory_batch.list_batches(owner_id)


@app.post("/api/batches/estimate")
def estimate_batch(req: EstimateBatchRequest, owner_id: str = Depends(require_auth)):
    """Called live from the create-batch modal as the user checks/unchecks
    chats — NOT tied to an existing batch_id, since the whole point is to
    show the cost BEFORE creating one. See chat_store.estimate_batch_context_tokens."""
    return chat_store.estimate_batch_context_tokens(owner_id, req.chat_ids)


@app.post("/api/batches")
def create_batch(req: CreateBatchRequest, owner_id: str = Depends(require_auth)):
    try:
        return memory_batch.create_batch(owner_id, req.name, req.member_chat_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/batches/{batch_id}/rename")
def rename_batch(batch_id: str, req: RenameBatchRequest, owner_id: str = Depends(require_auth)):
    try:
        return memory_batch.rename_batch(batch_id, owner_id, req.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")


@app.post("/api/batches/{batch_id}/unlink")
def unlink_batch_members(batch_id: str, req: BatchMembersRequest, owner_id: str = Depends(require_auth)):
    """Returns {"dissolved": true} if removing these members collapsed the
    batch to <=1, otherwise returns the updated batch."""
    try:
        result = memory_batch.unlink_members(batch_id, owner_id, req.chat_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")
    return result if result else {"dissolved": True, "id": batch_id}


@app.post("/api/batches/{batch_id}/members")
def add_batch_member(batch_id: str, req: BatchMembersRequest, owner_id: str = Depends(require_auth)):
    try:
        for cid in req.chat_ids:
            memory_batch.add_member(batch_id, owner_id, cid)
        return memory_batch.get_batch(batch_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str, owner_id: str = Depends(require_auth)):
    try:
        memory_batch.delete_batch(batch_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")
    return {"status": "deleted", "id": batch_id}


# --- workspaces: named containers with auto-linking membership (§7) ------
# UI label is "Projects" — named chat_workspace.py / /api/workspaces in
# code to avoid colliding with eo/project_registry.py, which tracks
# external codebase roots for Cross-Project File Control (unrelated
# concept, same word).
#
# Part 8.3: `owner_id` below is what require_auth's dependency-injected
# param has always been called — for these routes it now really means
# "the acting user's id", which may be the workspace's real owner OR a
# collaborator (viewer/editor). chat_workspace.py's functions resolve
# actual access themselves; these routes just map its exceptions to the
# right HTTP status: FileNotFoundError -> 404 (no access at all, same
# opacity as "doesn't exist"), WorkspaceAccessError -> 403 (some access,
# not enough for this action).

@app.get("/api/workspaces")
def get_workspaces(owner_id: str = Depends(require_auth)):
    return chat_workspace.list_workspaces(owner_id)


@app.post("/api/workspaces")
def create_workspace(req: CreateWorkspaceRequest, owner_id: str = Depends(require_auth)):
    return chat_workspace.create_workspace(owner_id, req.name)


@app.get("/api/workspaces/{ws_id}")
def get_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@app.patch("/api/workspaces/{ws_id}/rename")
def rename_workspace(ws_id: str, req: RenameWorkspaceRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.rename_workspace(ws_id, owner_id, req.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
@app.post("/api/workspaces/{ws_id}/promote")
def promote_workspace(ws_id: str, req: PromoteWorkspaceRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.promote(ws_id, owner_id, req.to_stage, mode=req.mode or "complete")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/workspaces/{ws_id}/chats")
def add_workspace_chat(ws_id: str, req: WorkspaceChatRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.add_chat(ws_id, owner_id, req.chat_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/api/workspaces/{ws_id}/chats/create")
def create_workspace_chat(ws_id: str, req: CreateWorkspaceChatRequest,
                           owner_id: str = Depends(require_auth)):
    """One-step version of create-then-attach — a collaborator (owner or
    editor) creates a brand-new chat that's immediately part of this
    workspace, instead of two round trips. Same access rules as
    add_workspace_chat: requires edit access to ws_id."""
    try:
        return chat_workspace.create_chat_in_workspace(ws_id, owner_id, title=req.title or "New Chat")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.delete("/api/workspaces/{ws_id}/chats/{chat_id}")
def remove_workspace_chat(ws_id: str, chat_id: str, delete_chat: bool = Query(False),
                           owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.remove_chat(ws_id, owner_id, chat_id, delete_chat=delete_chat)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.delete("/api/workspaces/{ws_id}")
def delete_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.delete_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "deleted", "id": ws_id}


# --- Part 8.3: workspace membership (owner-only) --------------------------

@app.get("/api/workspaces/{ws_id}/members")
def get_workspace_members(ws_id: str, owner_id: str = Depends(require_auth)):
    """Returns one flat array, owner first (if any), then
    workspace_members rows — the frontend renders this uniformly rather
    than special-casing the owner, even though the owner isn't actually
    a workspace_members row in the database (see chat_workspace.py).
    Each entry is enriched with email/name/avatar_url via the Admin API
    so the UI never has to show a raw user_id."""
    try:
        members = chat_workspace.list_members(ws_id, owner_id)
        ws = chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))

    all_ids = {m["user_id"] for m in members}
    if ws["owner_id"]:
        all_ids.add(ws["owner_id"])
    profiles = _lookup_users_by_ids(all_ids)

    def _enrich(uid: str, extra: dict) -> dict:
        p = profiles.get(uid, {})
        return {
            "user_id": uid,
            "email": p.get("email"),
            "name": p.get("name"),
            "avatar_url": p.get("avatar_url"),
            **extra,
        }

    result = []
    if ws["owner_id"]:
        result.append(_enrich(ws["owner_id"], {
            "role": "owner", "can_toggle_attribution": True, "added_at": None,
        }))
    for m in members:
        result.append(_enrich(m["user_id"], {
            "role": m["role"],
            "can_toggle_attribution": m["can_toggle_attribution"],
            "added_at": m["added_at"],
        }))
    return result


@app.post("/api/workspaces/{ws_id}/members")
def add_workspace_member(ws_id: str, req: AddWorkspaceMemberRequest, owner_id: str = Depends(require_auth)):
    target_user_id = _lookup_user_id_by_email(req.email)
    if not target_user_id:
        raise HTTPException(status_code=404, detail=f"No user found with email {req.email!r}")
    try:
        return chat_workspace.add_member(ws_id, owner_id, target_user_id, role=req.role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/workspaces/{ws_id}/members/{target_user_id}")
def update_workspace_member(ws_id: str, target_user_id: str, req: UpdateWorkspaceMemberRequest,
                             owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.update_member_role(ws_id, owner_id, target_user_id, req.role)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/workspaces/{ws_id}/members/{target_user_id}")
def remove_workspace_member(ws_id: str, target_user_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.remove_member(ws_id, owner_id, target_user_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "removed", "workspace_id": ws_id, "user_id": target_user_id}


# --- Part 8.4: ownership transitions, voting, attribution ------------------

@app.post("/api/workspaces/{ws_id}/leave")
def leave_workspace_endpoint(ws_id: str, req: LeaveWorkspaceRequest,
                              owner_id: str = Depends(require_auth)):
    """Any member (including the owner) can leave voluntarily. If the
    caller is the owner and names a successor, ownership transfers
    directly. If the owner names no successor, the workspace becomes
    joint. Non-owners just drop their own membership row —
    successor_id is ignored for them."""
    try:
        chat_workspace.leave_workspace(ws_id, owner_id, successor_id=req.successor_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "left", "workspace_id": ws_id, "user_id": owner_id}


@app.post("/api/workspaces/{ws_id}/owner/remove")
def remove_owner_endpoint(ws_id: str, owner_id: str = Depends(require_auth)):
    """Forced removal — caller must be a partner. Ejects the current
    owner with no successor choice and puts the workspace into joint
    state. Named 'owner_id' for consistency with every other route's
    Depends(require_auth) parameter, but here it's the ACTING PARTNER,
    not the workspace's owner — same overloaded-name convention noted
    in chat_workspace.py's Part 8.3 section."""
    try:
        return chat_workspace.remove_owner(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/workspaces/{ws_id}/votes")
def get_workspace_votes(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.get_vote_status(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@app.post("/api/workspaces/{ws_id}/votes")
def cast_workspace_vote(ws_id: str, req: CastVoteRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.cast_vote(ws_id, owner_id, req.vote_target)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/workspaces/{ws_id}/attribution")
def set_workspace_attribution(ws_id: str, req: SetAttributionRequest,
                               owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.set_show_attribution(ws_id, owner_id, req.show)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.patch("/api/workspaces/{ws_id}/members/{target_user_id}/attribution-grant")
def set_member_attribution_grant(ws_id: str, target_user_id: str, req: AttributionGrantRequest,
                                  owner_id: str = Depends(require_auth)):
    """Owner/partner-only: grant or revoke a specific moderator's right
    to toggle workspace-wide attribution visibility."""
    try:
        return chat_workspace.set_moderator_attribution_grant(
            ws_id, owner_id, target_user_id, req.can_toggle
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/workspaces/{ws_id}/audit")
def get_workspace_audit(ws_id: str, limit: int = Query(100, le=500),
                         owner_id: str = Depends(require_auth)):
    """Part 8.6: 'what happened to this workspace' — owner/partner-tier
    only, same restriction as delete_workspace/set_moderator_attribution_grant,
    since this surfaces every member add/remove/role-change and every
    ownership transition, not just the caller's own actions."""
    role = chat_workspace.member_role(ws_id, owner_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    if role not in ("owner", "partner"):
        raise HTTPException(
            status_code=403,
            detail=f"user {owner_id} must be an owner or partner of workspace {ws_id} to view its audit log",
        )
    return audit_log.list_for_target("workspace", ws_id, limit=limit)


@app.get("/api/audit/me")
def get_my_audit(limit: int = Query(100, le=500), owner_id: str = Depends(require_auth)):
    """Part 8.6: 'what have I done' — always self-scoped by the
    authenticated caller's own id, so no separate access check is
    needed beyond require_auth itself."""
    return audit_log.list_for_user(owner_id, limit=limit)


# --- Part 8.5: third-party integrations ------------------------------------
#
# Google Calendar is the first connector built; Gmail/Slack/Jira-Asana-
# Linear repeat this exact shape (eo/integrations.py's storage is already
# provider-agnostic) against a different base URL/payload. See
# eo/integrations.py and agents/calendar_agent.py.

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# In-memory state->user_id map for the OAuth redirect round-trip. Process-
# local, same class of gap Part 8.1 flagged for the old file-store locks —
# fine for a single server instance, but move this into Redis (via
# memory/bus.py, already connected) with a short TTL if this deployment
# ever runs multiple replicas behind a load balancer.
_oauth_state: dict[str, str] = {}


@app.get("/api/integrations")
def list_integrations(owner_id: str = Depends(require_auth)):
    """Everything this user has connected, for the frontend's
    integrations panel. Never returns tokens — see
    eo.integrations.list_connected()'s own docstring."""
    return integrations.list_connected(owner_id)


@app.get("/api/integrations/google_calendar/connect")
def connect_google_calendar(owner_id: str = Depends(require_auth)):
    """Returns the Google consent URL for the frontend to redirect the
    browser to. Doesn't redirect itself — this route is hit by frontend
    JS, not a real browser navigation, same as every other JSON endpoint
    in this file; only the callback below is a real browser redirect
    target."""
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Server misconfigured: Google OAuth env vars not set.")

    state = secrets.token_urlsafe(24)
    _oauth_state[state] = owner_id

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",   # required to get a refresh_token back
        "prompt": "consent",        # forces a refresh_token on every connect,
                                     # not just the first-ever consent — otherwise
                                     # a user who disconnects and reconnects gets
                                     # no refresh_token the second time.
        "state": state,
    }
    return {"auth_url": f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@app.get("/api/integrations/google_calendar/callback")
def google_calendar_callback(code: str = Query(...), state: str = Query(...)):
    """Google redirects the user's browser here directly — this route is
    NOT behind require_auth, because the browser arrives via Google's own
    redirect, not an Authorization header. Identity instead comes from the
    state token minted in connect_google_calendar() above, which only
    that authenticated user's own browser could have received. An
    unknown/expired state is rejected outright."""
    owner_id = _oauth_state.pop(state, None)
    if not owner_id:
        raise HTTPException(status_code=400, detail="Unknown or expired OAuth state")

    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {resp.text}")
    payload = resp.json()

    # account_label: which Google account this actually is, for the UI —
    # a second, cheap call, same "fetch identity via the token itself"
    # pattern _lookup_user_id_by_email uses the service-role key for.
    account_label = None
    try:
        userinfo = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
            timeout=10,
        )
        if userinfo.status_code == 200:
            account_label = userinfo.json().get("email")
    except Exception:
        pass  # cosmetic only — a missing label never blocks the connection itself

    integrations.save_credentials(
        owner_id, "google_calendar", payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        scope=payload.get("scope"),
        account_label=account_label,
    )
    # Redirect back into the app rather than returning raw JSON — this
    # endpoint is hit by a real browser navigation, unlike every other
    # route in this file.
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    return RedirectResponse(f"{frontend_url}/settings/integrations?connected=google_calendar")


@app.delete("/api/integrations/{provider}")
def disconnect_integration(provider: str, owner_id: str = Depends(require_auth)):
    integrations.disconnect(owner_id, provider)
    return {"provider": provider, "disconnected": True}


class CreateEventRequest(BaseModel):
    summary: str
    start: str              # RFC3339
    end: str                 # RFC3339
    description: str = ""
    location: str = ""


@app.get("/api/integrations/google_calendar/events")
def get_calendar_events(time_min: str = Query(...), time_max: str = Query(...),
                         owner_id: str = Depends(require_auth)):
    try:
        return calendar_agent.list_events(owner_id, time_min, time_max)
    except IntegrationNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this user")


@app.post("/api/integrations/google_calendar/events")
def post_calendar_event(req: CreateEventRequest, owner_id: str = Depends(require_auth)):
    try:
        return calendar_agent.create_event(
            owner_id, req.summary, req.start, req.end,
            description=req.description, location=req.location,
        )
    except IntegrationNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this user")


@app.delete("/api/integrations/google_calendar/events/{event_id}")
def delete_calendar_event(event_id: str, owner_id: str = Depends(require_auth)):
    try:
        return calendar_agent.delete_event(owner_id, event_id)
    except IntegrationNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this user")


@app.get("/api/workspaces/{ws_id}/export")
def export_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    """Part 8.7: any current member can export — a portable JSON backup
    of the CALLER's own chats in this workspace (never a collaborator's,
    see chat_workspace.export_workspace_data's docstring). Not a
    docx/pptx/etc. file — agents/exporter.py's format writers weren't in
    scope this session, so this is the JSON interchange format the
    restore path below actually consumes."""
    try:
        return chat_workspace.export_workspace_data(ws_id, owner_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")


@app.post("/api/workspaces/{ws_id}/import")
def import_workspace(ws_id: str, req: ImportWorkspaceDataRequest,
                      owner_id: str = Depends(require_auth)):
    """Part 8.7: restores a manifest's chats as new chats owned by the
    caller, attached to ws_id. Requires edit-tier+ access to ws_id."""
    try:
        return chat_workspace.import_workspace_data(ws_id, owner_id, req.manifest)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.get("/api/workspaces/{ws_id}/export/files")
def export_workspace_files(ws_id: str, fmt: str = Query("md"),
                            owner_id: str = Depends(require_auth)):
    """Part 8.7 (file-format path): human-readable export of the
    caller's own chats in this workspace via agents/exporter.py, using
    graph/adapters.py's chat_to_artifact() to shape each chat into the
    {title, sections} artifact every exporter in that module already
    consumes — same "one adapter per domain, one exporter set total"
    discipline node_to_artifact/markdown_text_to_artifact already
    follow, just fed a chat instead of a node or raw Markdown.

    This is deliberately separate from GET /export (the JSON backup):
    that one preserves exact message structure for restore_chats() to
    replay losslessly; this one produces a real docx/pptx/pdf/md/csv/json
    file meant for a human to read, not for round-tripping back through
    import. A single chat downloads directly; more than one gets zipped
    (FileResponse can only serve one file per request)."""
    fmt = fmt.lower().lstrip(".")
    if fmt not in EXPORTABLE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported export format '{fmt}'. Supported: {', '.join(EXPORTABLE_FORMATS)}.",
        )
    try:
        manifest = chat_workspace.export_workspace_data(ws_id, owner_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")

    chats = manifest["chats"]
    if not chats:
        raise HTTPException(status_code=404, detail="No chats to export for this user in this workspace")

    paths = []
    for chat in chats:
        artifact = chat_to_artifact(chat)
        path = export_artifact(artifact, fmt, NOTES_EXPORTS_DIR)
        paths.append(path)

    if len(paths) == 1:
        return FileResponse(paths[0], filename=os.path.basename(paths[0]))

    zip_path = os.path.join(NOTES_EXPORTS_DIR, f"{ws_id}_export.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=os.path.basename(p))
    return FileResponse(zip_path, filename=os.path.basename(zip_path))


# --- workspace facts: tier-3 memory (see eo/workspace_facts.py, §0.3) ----
# The settings-panel-facing surface for "facts true across the whole
# project" — brand voice, target user, tech stack, plus a free-form
# `custom` bucket. Reading these into agent prompts happens automatically
# inside eo/conversation_memory.py; nothing here needs to be called at
# generation time, only when the user views/edits the panel.

@app.get("/api/workspaces/{ws_id}/facts")
def get_workspace_facts(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)  # 404s if the workspace doesn't exist / isn't owned
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return workspace_facts.get_facts(ws_id)


@app.put("/api/workspaces/{ws_id}/facts")
def put_workspace_facts(ws_id: str, req: WorkspaceFactsRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    # exclude_unset -> a save that only touched brand_voice doesn't wipe
    # target_user/tech_stack/custom back to empty.
    return workspace_facts.set_facts(ws_id, req.dict(exclude_unset=True))
@app.post("/api/workspaces/{ws_id}/parts/refresh-prices")
def refresh_part_prices(ws_id: str, req: RefreshPricesRequest,
                         owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    updated = []
    for part in req.parts:
        result = find_price(part["name"], force_refresh=req.force_refresh)
        listing = result["listings"][0] if result["listings"] else None
        updated.append({
            **part,
            "estimated_price_bdt": listing.get("price_bdt") if listing else None,
            "vendor_name": listing.get("vendor") if listing else None,
            "vendor_url": listing.get("url") if listing else None,
            "price_checked_at": result["checked_at"],
        })

    # Merge into the existing custom bucket rather than overwriting it —
    # `custom` already holds unrelated data (e.g. the UptimeRobot API key
    # from deploy_agent.py's set_uptimerobot_api_key()). Read-modify-write
    # at this level keeps that safe regardless of whether set_facts()
    # itself does a shallow or deep merge internally.
    facts = workspace_facts.get_facts(ws_id)
    custom = dict(facts.get("custom") or {})
    custom["parts"] = updated
    workspace_facts.set_facts(ws_id, {"custom": custom})
    workspace_facts.record_section_entries(
        ws_id,
        "hardware",
        [
            {
                "key": part.get("id") or part.get("name") or f"part_{index}",
                "title": part.get("name") or part.get("id") or f"Part {index + 1}",
                "summary": f"{part.get('category') or 'module'} ×{part.get('qty') or 1}",
                "data": part,
            }
            for index, part in enumerate(updated)
        ],
        source="refresh_part_prices",
        source_ref=ws_id,
        event="parts_refresh",
    )

    return {"parts": updated}

@app.get("/api/workspaces/{ws_id}/device-spec")
def get_device_spec(ws_id: str, owner_id: str = Depends(require_auth)):
    """Assembles agents/hardware_speccer.py's four sub-view slices back
    into one response -- they're stored as four separate
    workspace_facts.custom keys (parts/wiring/mech/instructions), not one
    blob, so BlueprintView's single fetch-per-workspace-select needs this
    endpoint to stitch them together rather than reading facts.custom
    directly and hoping all four keys exist. Returns empty-but-valid
    shapes for any key nothing has written yet (no device spec generated
    == every sub-view renders its own empty state, not a 404 for the
    whole page)."""
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
 
    custom = workspace_facts.get_facts(ws_id).get("custom") or {}
    return {
        "parts": custom.get("parts", []),
        "wiring": custom.get("wiring", {"nodes": [], "edges": []}),
        "mech": custom.get("mech", {"enclosure": {"w": 0, "h": 0, "d": 0}, "placements": []}),
        "instructions": custom.get("instructions", {"phases": []}),
    }
 
 
@app.patch("/api/workspaces/{ws_id}/device-spec/instructions/steps/{step_id}")
def toggle_instruction_step(ws_id: str, step_id: str, req: ToggleInstructionStepRequest,
                             owner_id: str = Depends(require_auth)):
    """Instructions is the only Blueprint sub-view with mutable state
    (Blueprint design guide §5) -- everything else here is regenerated
    wholesale by agents/hardware_speccer.py, so only this route needs a
    read-modify-write-a-single-step shape rather than a full-object PUT.
    Same custom-dict merge discipline as refresh_part_prices() above:
    read the whole facts object, touch only custom["instructions"], write
    the whole object back, so an in-flight price refresh and a step
    toggle can't clobber each other's key."""
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
 
    facts = workspace_facts.get_facts(ws_id)
    custom = dict(facts.get("custom") or {})
    instructions = custom.get("instructions") or {"phases": []}
 
    found = False
    for phase in instructions.get("phases", []):
        for step in phase.get("steps", []):
            if step["id"] == step_id:
                step["done"] = req.done
                found = True
    if not found:
        raise HTTPException(status_code=404, detail="Unknown step_id")
 
    custom["instructions"] = instructions
    workspace_facts.set_facts(ws_id, {"custom": custom})
    workspace_facts.record_section_entries(
        ws_id,
        "instructions",
        [
            {
                "key": phase.get("id") or phase.get("name") or f"phase_{phase_index}",
                "title": phase.get("name") or phase.get("id") or f"Phase {phase_index + 1}",
                "summary": f"{len(phase.get('steps', []))} step(s)",
                "data": phase,
            }
            for phase_index, phase in enumerate(instructions.get("phases", []))
        ],
        source="toggle_instruction_step",
        source_ref=step_id,
        event="instruction_step",
    )
    return {"status": "ok", "instructions": instructions}
 
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

# --- generic paste-panel content (see eo/panel_content.py) ---------------
# Same "gone on reload" fix as workspace facts, generalized to every
# paste-a-chat's-output-into-a-box panel: Mind Map, Study
# (flashcards/quiz/study guide), PRD, Architecture, Schema, API
# Contract, Devil's Advocate, Feasibility, Wireframes, Contradictions.

@app.get("/api/workspaces/{ws_id}/panels", dependencies=[Depends(require_auth)])
def list_workspace_panel_content(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return panel_content.list_content(ws_id)


@app.get("/api/workspaces/{ws_id}/panels/{panel_key}", dependencies=[Depends(require_auth)])
def get_workspace_panel_content(ws_id: str, panel_key: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return panel_content.get_content(ws_id, panel_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/workspaces/{ws_id}/panels/{panel_key}", dependencies=[Depends(require_auth)])
def put_workspace_panel_content(ws_id: str, panel_key: str, req: PanelContentRequest,
                                 owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return panel_content.set_content(ws_id, panel_key, req.content, owner_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- content audit: PageSpeed Insights (see agents/pagespeed_agent.py) --
# Live-fetched, not persisted — same "fetch fresh on load/refresh, no
# backing store" pattern GrowthTab's CalendarView already uses for
# Google Calendar events, not the panel_content paste-and-save pattern.
# ws_id is only used for the same ownership gate every workspace-scoped
# route already applies; the audit itself isn't workspace-specific data.

@app.get("/api/workspaces/{ws_id}/audit/pagespeed", dependencies=[Depends(require_auth)])
def get_pagespeed_audit(ws_id: str, url: str = Query(...), strategy: str = Query("mobile"),
                         owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return pagespeed_agent.run_audit(url, strategy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except pagespeed_agent.PageSpeedError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
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

@app.get("/api/workspaces/{ws_id}/nodes")
def get_workspace_nodes(ws_id: str, node_type: Optional[str] = Query(None),
                         owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return list_nodes(ws_id, node_type=node_type)

@app.patch("/api/workspaces/{ws_id}/nodes/{node_id}/rename", dependencies=[Depends(require_auth)])
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
@app.delete("/api/workspaces/{ws_id}/nodes/{node_id}", dependencies=[Depends(require_auth)])
def delete_workspace_node(ws_id: str, node_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    delete_node(ws_id, node_id)

    # eo/graph_edges.py's own edges_for_node() docstring: "what a 'delete
    # this node' flow needs to know what it would orphan" -- built for
    # exactly this, confirmed against that module's source rather than
    # guessed.
    full_node_id = f"node:{ws_id}:{node_id}"
    for edge in graph_edges.edges_for_node(full_node_id):
        try:
            graph_edges.delete_edge(edge["edge_id"])
        except FileNotFoundError:
            pass

    for candidate in list_cluster_candidates(ws_id):
        if node_id in (candidate.get("node_ids") or []):
            try:
                reject_cluster_candidate(ws_id, candidate["candidate_id"])
            except FileNotFoundError:
                pass

    return {"status": "deleted", "id": node_id}


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


@app.post("/api/workspaces/{ws_id}/backlinks/detect")
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

@app.post("/api/workspaces/{ws_id}/clusters/propose")
def propose_clusters_endpoint(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
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

@app.post("/api/workspaces/{ws_id}/table")
def build_table_endpoint(ws_id: str, req: BuildTableRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        table = build_table(ws_id, req.field_names, node_type=req.node_type, expanded=req.expanded)
        workspace_facts.record_section_entries(
            ws_id,
            "extractions",
            [
                {
                    "key": row.get("node_id") or row.get("title") or f"row_{index}",
                    "title": row.get("title") or row.get("node_id") or f"Row {index + 1}",
                    "summary": ", ".join(
                        f"{field}={row.get(field)!r}"
                        for field in req.field_names
                        if row.get(field) not in (None, "")
                    ) or table.get("summary") or "Extraction row",
                    "text": row.get("title") or "",
                    "data": row,
                }
                for index, row in enumerate(table.get("rows", []))
            ],
            source="note_table_builder",
            source_ref="/api/workspaces/{ws_id}/table",
            event="extraction",
        )
        return table
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Test tab / "simulate" domain (see eo/structure.py's STRUCTURE_TEMPLATES
# ["simulate"], Part 1) -----------------------------------------------------
# Test tab design spec's Build Order step 1 originally called for wrapping
# agents/review_aggregator.py's aggregate_reviews() merge step here -- that
# doesn't actually fit: aggregate_reviews expects each member's output
# already shaped as {"issues": [...], "summary": ...} (Reviewer Pool's
# structured JSON), while every persona role's own ROLE_PROMPTS_SEED brief
# (persona_customer, persona_skeptic, critic_reviewer, usability_walkthrough,
# red_team, pricing_sensitivity, support_ticket_predictor, competitor_response)
# is a plain generic_worker role writing free-form in-character prose, not
# structured issues -- there'd be nothing for aggregate_reviews to parse.
# More importantly, simulation_synthesizer's own brief explicitly rejects
# review_aggregator-style merging ("Preserve real disagreement between
# personas explicitly -- do not average conflicting reactions into a single
# flattened conclusion"). The synthesis this tab needs already runs as part
# of the domain's own execution order -- simulation_synthesizer is
# deliberately hired last, after every persona (see STRUCTURE_TEMPLATES'
# own comment), so it can read their outputs via input_keys. So this
# endpoint's job is just reading back what already ran off the memory bus,
# same pattern GET /api/tasks/{session_id} already uses for
# integration_flagger's stage_output -- not a new merge step.
#
# marketplace_review_batch is read separately from the other persona roles
# since its own brief specifies a different, already-structured fenced-json
# shape (a bare array of {"rating","sentiment","text"}) rather than free
# prose -- each role's own brief decides its shape, this endpoint just
# reads it back either way.

@app.post("/api/workspaces/{ws_id}/simulate")
def get_simulation_results(ws_id: str, req: SimulateRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    session_id = req.session_id
    persona_roles = [r for r in STRUCTURE_TEMPLATES["simulate"] if r != "simulation_synthesizer"]

    # Same read-side app_slug scoping GET /api/tasks/{session_id} uses for
    # stage_output:* keys -- without it, read_many() falls back to
    # whatever app_slug happens to be the persisted Redis global, the
    # exact cross-session collision Migration Part B fixed on the write
    # side.
    set_app_slug(session_id)
    keys = [f"stage_output:{session_id}:{role}" for role in persona_roles]
    synthesis_key = f"stage_output:{session_id}:simulation_synthesizer"
    data = bus_read_many(keys + [synthesis_key], default=None)

    personas = []
    for role in persona_roles:
        text = data[f"stage_output:{session_id}:{role}"]
        if not text:
            continue
        if role == "marketplace_review_batch":
            reviews = _parse_marketplace_reviews(text)
            if reviews:
                personas.append({"role": role, "reviews": reviews})
        else:
            personas.append({"role": role, "text": text})

    return {
        "session_id": session_id,
        "synthesis": data[synthesis_key],
        "personas": personas,
    }


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
        original_title = os.path.splitext(file.filename or "")[0] or None
        artifact = import_artifact(tmp_path, fmt=ext, default_title=original_title)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.remove(tmp_path)
    node_ids = write_ingested_source(artifact, workspace_id, created_by="user")
    return {"node_ids": node_ids, "title": artifact["title"]}


@app.post("/api/notes/pdf", dependencies=[Depends(require_auth)])
async def ingest_pdf_endpoint(workspace_id: str = Form(...), file: UploadFile = File(...)):
    """PDF ingestion -- agents/pdf_ingestor.py (pdfplumber, page-by-page
    extraction) already exists and was fully implemented, it just had no
    endpoint calling it. PDF is deliberately absent from IMPORTABLE_FORMATS
    (see /api/notes/import above) -- this is that "other job", same
    temp-file-then-cleanup, two-step write_ingested_source() shape as
    every other ingestor here."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        artifact = ingest_pdf(tmp_path)
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
def post_task(req: TaskRequest, owner_id: str = Depends(require_auth)):   # FIXED — capture owner_id
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
            approval_roles=set(req.approval_roles) if req.approval_roles else None,
            owner_id=owner_id,   # FIXED — thread it down to run_task()
        )
    except Exception as exc:
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
def post_task_preview(req: PreviewTaskRequest, owner_id: str = Depends(require_auth)):   # FIXED
    """... docstring unchanged ..."""
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
            owner_id=owner_id,   # FIXED
        )
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision={}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


@app.post("/api/task/confirm", response_model=TaskResponse)
def post_task_confirm(req: ConfirmTaskRequest, owner_id: str = Depends(require_auth)):
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
            owner_id=owner_id,   
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


@app.post("/api/resume", response_model=ResumeResponse)
def post_resume(req: ResumeRequest, owner_id: str = Depends(require_auth)):
    """Part 2 §2.4: resumes a run paused at an approval_roles checkpoint.
    Mirrors post_task()'s error-handling shape (clean JSON on unexpected
    failure, real HTTP status codes for the specific, anticipated
    failure modes resume_graph() raises).

    Part 8.8 regression fix: session_id and chat_id are the same string
    everywhere in this system (see the comment above _resolve_chat_or_404),
    so the resuming caller's access is checked exactly the same way every
    other chat route checks it — owner or workspace collaborator, edit-tier
    required (approving/editing/rejecting a paused run is not a read-only
    action). Without this, any authenticated user who knew or guessed
    another user's session_id could resume/approve/edit their paused run;
    resume_graph() itself has no identity concept at all, so this check
    has to happen here, before it's ever called."""
    _resolve_chat_or_404(req.session_id, owner_id, require_edit=True)

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

@app.get("/api/roles")
def get_roles(owner_id: str = Depends(require_auth)):
    """Every role the system has ever briefed, metadata included — the
    Role Library panel's one data source. Shape: [{role, brief, source,
    updated_at, times_hired}, ...]. Uses list_role_metadata() for a
    single bulk read instead of list_known_roles()+get_role_metadata()
    per role, which was doing N+1 round-trips against the memory bus.

    owner_id (Part 8.3): always passed through to eo.registry now — it's
    only actually USED to select a per-user store if this deployment set
    ROLE_LIBRARY_SCOPE=per_user; with the default "global" scope every
    caller's owner_id is accepted but ignored (see eo/registry.py's
    _role_prompts_key()), so this route's behavior is unchanged for the
    common case."""
    return list_role_metadata(owner_id)


class UpdateRoleRequest(BaseModel):
    brief: str


@app.put("/api/roles/{role_name}")
def put_role(role_name: str, req: UpdateRoleRequest, owner_id: str = Depends(require_auth)):
    """Saves an inline Role Library edit. Always source="user_edited" —
    this is the one path that's allowed to claim that (see
    eo/registry.py's update_role_prompt() docstring)."""
    update_role_prompt(role_name, req.brief, source="user_edited", user_id=owner_id)
    return {"role": role_name, **(get_role_metadata(role_name, owner_id) or {})}


class SetRolePinnedRequest(BaseModel):
    pinned: bool


@app.patch("/api/roles/{role_name}/pin")
def patch_role_pinned(role_name: str, req: SetRolePinnedRequest, owner_id: str = Depends(require_auth)):
    """Pinned-roles feature — server-persisted so it syncs across
    devices, same store as everything else in the Role Library. Doesn't
    require the role to already have a brief; a role can be pinned from
    a picker before it's ever been hired."""
    entry = set_role_pinned(role_name, req.pinned, user_id=owner_id)
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


@app.get("/api/workflow-templates/{template_id}/chat")
def get_template_chat(template_id: str, owner_id: str = Depends(require_auth)):
    """The one chat this template already owns, if any — lets the
    frontend reuse it instead of minting a new chat on every run."""
    chat = chat_store.find_chat_for_template(owner_id, template_id)
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
def post_task_from_template(req: RunFromTemplateRequest, owner_id: str = Depends(require_auth)):
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
            owner_id=owner_id,   
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


def _parse_fenced_json(text):
    """integration_flagger (Part 7 §7.3) is a generic_worker role, so its
    output lands in stage_output:* as plain text -- a strict fenced
    ```json code block per its ROLE_PROMPTS_SEED brief, not real
    structured output the way a REAL_ACTION_ROLES module's return value
    would be. Same strip-the-fence approach agents/prompt_writer.py and
    agents/idea_planner.py already use on their own raw LLM text before
    json.loads(), just tolerant of surrounding prose since a
    generic_worker role's brief-enforced discipline is never as airtight
    as a dedicated module's own parsing. Returns [] (not None) on
    anything unparseable, so the checklist UI can render "no integrations
    flagged yet" rather than an error state for a role that hasn't run.
    """
    if not text:
        return []
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1) if match else text
    try:
        parsed = json.loads(raw.strip())
        return parsed.get("integrations", []) if isinstance(parsed, dict) else []
    except (json.JSONDecodeError, AttributeError):
        return []


def _parse_marketplace_reviews(text):
    """marketplace_review_batch (Part 1 §1.4 track 2) is a generic_worker
    role whose own ROLE_PROMPTS_SEED brief instructs it to emit a single
    fenced ```json code block containing a bare array of
    {"rating", "sentiment", "text"} objects -- not the {"integrations":
    [...]} wrapper shape _parse_fenced_json above expects, since that's
    what THIS role's own brief specifies. Same strip-the-fence-then-
    json.loads approach, same "[] on anything unparseable, never an
    error state" posture as _parse_fenced_json.
    """
    if not text:
        return []
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1) if match else text
    try:
        parsed = json.loads(raw.strip())
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, AttributeError):
        return []


def _sentry_status(module_specs: dict, submitted_code: dict) -> str:
    """Part 7 §7.5. Three states, not a bare yes/no, so the monitoring
    widget can be honest about where things stand:
      - "not_planned"  -- integration_flagger hasn't flagged monitoring
                          yet, or prompt_writer.py hasn't run this cycle
      - "planned"      -- monitoring_setup is in this cycle's
                          module_specs, but code_writers.py hasn't
                          generated it yet
      - "configured"   -- it's in submitted_code, i.e. real code exists

    "monitoring_setup" is agents/prompt_writer.py's own
    MONITORING_MODULE_NAME constant; matched here by the same literal
    string rather than importing it, same "agents/ and api/ don't share
    private internals across the layer boundary" reasoning
    agents/deploy_config_writer.py's own docstring already gives for
    duplicating structure_architect.py's _get_project_tree() instead of
    importing it.
    """
    names = {
        (m.get("name") or "").strip().lower()
        for m in (module_specs.get("modules") or [])
    }
    if "monitoring_setup" not in names:
        return "not_planned"
    if "monitoring_setup" in (submitted_code or {}):
        return "configured"
    return "planned"


@app.get("/api/tasks/{session_id}", dependencies=[Depends(require_auth)])
def get_tasks(session_id: str):
    """Part 7 §7.2 — read-only kanban view over data idea_planner.py and
    prompt_writer.py already write every cycle. No new storage: this just
    exposes current_plan / feature_status / module_specs as one combined
    object.

    set_app_slug(session_id) scopes the read the same way every tier-3
    adaptive-path run already scopes its writes (see memory/bus.py's
    set_app_slug() docstring) -- without it, read_many() would fall back
    to whatever app_slug happens to be the persisted Redis global, which
    is exactly the cross-session collision Migration Part B fixed on the
    write side. This is the read-side equivalent of that same fix.

    Uses read_many() -- the same batched MGET helper
    eo/quota_sentinel.py's get_usage_history() already uses -- so this is
    one Redis round trip, not one-per-key.

    Part 7 §7.3 -- also reads integration_flagger's stage_output entry
    (cached once per session, never re-run per cycle, per that role's own
    seed brief) and parses its fenced ```json block into a plain
    "integrations" list for the checklist rendered alongside the board.

    Part 7 §7.5 addition -- also reads deploy_config_plan /
    last_deploy_config_summary / last_deploy_trigger_result (so the
    frontend's deploy button + status indicator has something to render
    without a second round trip) and derives monitoring status: Sentry
    from module_specs/submitted_code (see _sentry_status() above),
    UptimeRobot verbatim from last_uptimerobot_registration. One combined
    object, same "one call, not four" reasoning §7.2/§7.3 already used
    when they extended this same endpoint.
    """
    set_app_slug(session_id)
    data = bus_read_many(
        [KEYS["current_plan"], KEYS["feature_status"], KEYS["module_specs"],
         KEYS["submitted_code"],
         f"stage_output:{session_id}:integration_flagger",
         deploy_config_writer_agent.DEPLOY_CONFIG_PLAN_KEY,
         deploy_agent_module.LAST_DEPLOY_CONFIG_SUMMARY_KEY,
         "last_deploy_trigger_result",
         deploy_agent_module.LAST_UPTIMEROBOT_REGISTRATION_KEY],
        default=None,
    )
    module_specs = data[KEYS["module_specs"]] or {}
    submitted_code = data[KEYS["submitted_code"]] or {}
    return {
        "current_plan": data[KEYS["current_plan"]] or {},
        "feature_status": data[KEYS["feature_status"]] or {},
        "module_specs": module_specs,
        "integrations": _parse_fenced_json(data[f"stage_output:{session_id}:integration_flagger"]),
        "deploy_config_plan": data[deploy_config_writer_agent.DEPLOY_CONFIG_PLAN_KEY],
        "last_deploy_config_summary": data[deploy_agent_module.LAST_DEPLOY_CONFIG_SUMMARY_KEY],
        "last_deploy_trigger_result": data["last_deploy_trigger_result"],
        "monitoring": {
            "sentry_status": _sentry_status(module_specs, submitted_code),
            "uptimerobot": data[deploy_agent_module.LAST_UPTIMEROBOT_REGISTRATION_KEY],
        },
    }

@app.get("/api/tasks/workspace/{ws_id}", dependencies=[Depends(require_auth)])
def get_tasks_for_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    """§7 — Tasks scoped to a workspace instead of a raw chat session.
    Resolves ws_id -> a chat_id using the exact same "first chat_id, or
    create one" convention NotebooksTab/ResearchTab's handleOpenChat
    already established, then delegates to get_tasks()'s existing
    memory-bus read unchanged. current_plan/feature_status/etc. still
    live in the bus keyed by app_slug=session_id -- this route only
    changes what session_id gets resolved and passed in; nothing about
    how idea_planner.py or any other agent writes.

    Also stamps the resolved session_id back onto the response as
    "_session_id" -- TasksTab.jsx's DeployPanel/MonitoringWidget still
    call /api/deploy/{session_id}/... and /api/monitoring/{session_id}/...
    directly (those routes are unchanged, still session-keyed), so the
    frontend needs this id rather than re-deriving ws.chat_ids[0] itself,
    which could be stale on the very first call when no chat existed yet
    and one was just created here.
    """
    try:
        ws = chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    chat_ids = ws["chat_ids"]
    if chat_ids:
        session_id = chat_ids[0]
    else:
        created = chat_workspace.create_chat_in_workspace(
            ws_id, owner_id, title=f"{ws['name']} — Build"
        )
        session_id = created["chat_ids"][0]

    data = get_tasks(session_id)
    data["_session_id"] = session_id
    return data

# --- Part 7 §7.4 — Deploy action button. Deliberately three separate
# endpoints for three separate-risk actions, same split as
# agents/deploy_config_writer.py / agents/deploy_agent.py themselves:
# propose (LLM call, no filesystem write), write (filesystem write,
# reversible, no confirmation), go-live (irreversible, gated behind
# _confirm_deploy()'s interactive y/N prompt every time). These call the
# agent modules directly rather than going through eo.registry.resolve()/
# eo/executor.py -- same "import an agent module, call it straight from a
# route" convention this file already uses for agents.backlink_detector /
# agents.note_table_builder, appropriate here since this is a one-off
# UI-button action, not a Panel-hired pipeline step (see
# agents/deploy_agent.py's own docstring).
class DeployActionRequest(BaseModel):
    project_unique_name: Optional[str] = None


@app.post("/api/deploy/{session_id}/propose", dependencies=[Depends(require_auth)])
def deploy_propose(session_id: str, req: DeployActionRequest = DeployActionRequest()):
    """Runs deploy_config_writer.py -- proposes a platform + config file
    content, does NOT write anything to disk yet. Safe to call more than
    once; each call overwrites the prior proposal."""
    set_app_slug(session_id)
    return deploy_config_writer_agent.run_deploy_config_writer(session_id=session_id)


@app.post("/api/deploy/{session_id}/write", dependencies=[Depends(require_auth)])
def deploy_write(session_id: str, req: DeployActionRequest = DeployActionRequest()):
    """Writes the proposed config file to disk. Reversible, low-stakes --
    no confirmation gate, matching file_manager.py's own treatment of an
    ordinary file write."""
    set_app_slug(session_id)
    try:
        return deploy_agent_module.write_deploy_config(
            project_unique_name=req.project_unique_name, session_id=session_id
        )
    except MissingDependencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/deploy/{session_id}/go-live", dependencies=[Depends(require_auth)])
def deploy_go_live(session_id: str, req: DeployActionRequest = DeployActionRequest()):
    """The actual "push this live" trigger -- blocks on an interactive
    y/N confirmation (agents/deploy_agent.py's _confirm_deploy()) every
    single call, regardless of target, before returning. See that
    module's docstring for why nothing is silently pushed live past this
    point yet (no real per-host API client exists in this codebase)."""
    set_app_slug(session_id)
    try:
        return deploy_agent_module.trigger_live_deploy(
            project_unique_name=req.project_unique_name, session_id=session_id
        )
    except MissingDependencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

# --- Part 7 §7.5 — Monitoring hooks. Sentry needs no endpoint at all --
# it's an ordinary module_specs/submitted_code entry now
# (agents/prompt_writer.py's _maybe_add_monitoring_module()), same pool
# as everything else code_writers.py generates. UptimeRobot is the one
# piece that needs real endpoints, since it needs a user-supplied API
# key and an explicit URL (agents/deploy_agent.py's trigger_live_deploy()
# has no real deployed URL to read automatically yet -- see that
# module's docstring).
class UptimeRobotKeyRequest(BaseModel):
    api_key: str


class UptimeRobotRegisterRequest(BaseModel):
    url: str
    friendly_name: Optional[str] = None


@app.post("/api/monitoring/{session_id}/uptimerobot-key", dependencies=[Depends(require_auth)])
def set_uptimerobot_key(session_id: str, req: UptimeRobotKeyRequest):
    """Stores the user's UptimeRobot API key against this session's
    workspace (eo/workspace_facts.py's `custom` dict, via
    agents/deploy_agent.py's set_uptimerobot_api_key()). 409 if this
    session isn't part of a workspace -- there's nowhere durable to put
    the key for an ad-hoc chat."""
    try:
        deploy_agent_module.set_uptimerobot_api_key(session_id, req.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "saved"}


@app.post("/api/monitoring/{session_id}/uptimerobot-register", dependencies=[Depends(require_auth)])
def register_uptimerobot(session_id: str, req: UptimeRobotRegisterRequest):
    """Registers req.url as a new UptimeRobot HTTP(s) monitor -- a real
    external call, made immediately with no confirmation gate. Different
    risk class than the live-deploy trigger on purpose: see
    agents/deploy_agent.py's register_uptimerobot_monitor() docstring
    for why (reversible, and the URL is already public by the time this
    runs, unlike a live-deploy trigger which is the act of making
    something public)."""
    set_app_slug(session_id)
    try:
        return deploy_agent_module.register_uptimerobot_monitor(
            req.url, session_id=session_id, friendly_name=req.friendly_name
        )
    except MissingDependencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/projects", dependencies=[Depends(require_auth)])
def projects():
    return list_projects()