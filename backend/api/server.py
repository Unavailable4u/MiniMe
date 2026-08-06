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
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if python-dotenv isn't installed; real env vars can be set directly instead

# NEW — B4: Sentry. Errors + full performance tracing
# (tracesSampleRate=1.0). Note this shares the free tier's separate
# ~10k/month transaction quota (distinct from the ~5k/month error
# quota) — at 1.0 every request across the agent roster is traced, so
# watch usage and dial this down (e.g. 0.1) if the quota gets tight.
# Silently a no-op if SENTRY_DSN isn't set, same convention as the
# dotenv import above — local dev without a DSN configured shouldn't
# fail loud.
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv("ENVIRONMENT", "development"),
        integrations=[FastApiIntegration()],
        traces_sample_rate=1.0,
    )

# B6 — auth/JWT verification (SUPABASE_URL, require_auth,
# _verify_supabase_jwt, _resolve_chat_or_404, etc.) moved to api/deps.py
# so api/routes/* modules can import it without a circular import back
# into this file. See api/deps.py's module docstring for why.
from api.deps import (
    SUPABASE_URL, SUPABASE_JWT_SECRET, SUPABASE_SERVICE_ROLE_KEY,
    require_auth, _verify_supabase_jwt,
)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # NEW — Data Layer architecture §9b: capturing the running
                 # event loop at startup for eo/ws_registry.py's thread-safe push
from contextlib import asynccontextmanager  # NEW — §9b: lifespan startup hook

from fastapi import (
    FastAPI, Request, HTTPException, Depends, Query,
    WebSocket, WebSocketDisconnect,  # NEW — §9b
)
from eo import ws_registry  # NEW — §9b: per-session WebSocket connection registry
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Union

# B6 — run_task/preview_task/confirm_task/run_task_from_template,
# resume_graph, the eo.registry role-library functions, and the
# eo.structure workflow-template functions moved to api/routes/tasks.py
# along with the routes that used them. get_quota_snapshot/
# get_usage_history/get_usage_history_scoped moved to api/routes/
# system.py the same way. STRUCTURE_TEMPLATES moved to
# api/routes/notebooks.py with /simulate (piece 7) — nothing left in
# this file reads it directly anymore.
from memory.bus import set_app_slug   # still used by the deploy/monitoring block below
from eo.errors import MissingDependencyError   # NEW — Part 7 §7.4: deploy endpoints' 409 handling
# B6 piece 6 — graph_edges, note_candidates, list_nodes/delete_node/
# rename_node, detect_backlinks/cleanup_for_removed_source, and
# list_cluster_candidates/accept_cluster_candidate/reject_cluster_candidate
# moved to api/routes/graph_and_notes.py with the routes that used them.
#
# B6 piece 7 — panel_content, chat_store, chat_workspace, quiz_progress,
# study_progress, workspace_facts, propose_clusters, and every
# agents.* generation/ingestion import below this comment (detect_facts,
# generate_study_content, generate_podcast_script,
# generate_rehearsal_script, generate_slide_deck, generate_mindmap,
# generate_suggested_route, link_concepts, suggest_workflows,
# build_topic_workflow, build_table, process_upload, IMPORTABLE_FORMATS,
# synthesize_podcast, build_video_overview, export_artifact,
# EXPORTABLE_FORMATS, markdown_text_to_artifact, FileResponse, json, re,
# tempfile, manifest_to_tools/study_progress_tools, classify_tool_intent)
# all moved to api/routes/notebooks.py with the routes that used them —
# nothing left in this file needs any of them.
from agents import deploy_config_writer as deploy_config_writer_agent   # NEW — Part 7 §7.4
from agents import deploy_agent as deploy_agent_module                  # NEW — Part 7 §7.4

@asynccontextmanager
async def _lifespan(app: FastAPI):
    # NEW — Data Layer architecture §9b: eo/notify.py's notify() runs
    # synchronously, often from Starlette's sync-endpoint threadpool
    # (every agent call site — see agents/source_manager.py,
    # agents/backlink_detector.py — is a plain `def`, not `async def`),
    # so it needs a thread-safe way to reach the event loop that the
    # WebSocket connections below actually live on. Capturing it here,
    # once, at startup, is that hand-off point.
    ws_registry.set_event_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="MiniMe v6 — EO layer API", lifespan=_lifespan)

# NOTES_EXPORTS_DIR (Part 4 §4.4) used to live here -- moved to
# api/routes/notebooks.py (B6 piece 7) along with every route that
# read it. See that file's own copy of this same comment/definition.

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# B6 — routers split out of this file. tasks_router owns /api/task*,
# /api/resume, /api/roles*, /api/workflow-templates*, and (as of piece 6)
# /api/tasks/{session_id} + /api/tasks/workspace/{ws_id} (piece 1, plus
# the leftover pair folded in alongside piece 6); system_router owns
# /api/health, /api/quota, /api/usage/history (piece 2); graph_and_notes_
# router owns graph edges, node summaries, topics/graph, nodes, note
# candidates, backlinks, and clusters (piece 6). This is also where
# CO3's /api/task/{id}/pause and CO5's /api/task/{id}/stream will
# register once built, inside tasks_router.
from api.routes.tasks import router as tasks_router
from api.routes.system import router as system_router
from api.routes.chats import router as chats_router
from api.routes.workspaces import router as workspaces_router
from api.routes.workspace_data import router as workspace_data_router
from api.routes.graph_and_notes import router as graph_and_notes_router
from api.routes.notebooks import router as notebooks_router
app.include_router(tasks_router)
app.include_router(system_router)
app.include_router(chats_router)
app.include_router(workspaces_router)
app.include_router(workspace_data_router)
app.include_router(graph_and_notes_router)
app.include_router(notebooks_router)


# NEW — Data Layer architecture §9b: the real transport behind
# eo/notify.py's notify() calls. A browser opens one of these per
# session_id it cares about (the same id agents/source_manager.py and
# agents/backlink_detector.py already pass to notify() — a chat/task
# session, not a user) and eo/ws_registry.py fans events out to
# whichever sockets are open for that session_id at the moment
# notify() fires. §9c (Generate-button loading state) and §9d (chat
# proactive suggestions) both consume this same socket — neither
# needs a route of its own, just a new "kind" in eo/notify.py.
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, token: str = Query(None)):
    # Auth token travels as a query param rather than a header: the
    # browser WebSocket API has no way to set Authorization on the
    # handshake request the way require_auth()'s HTTP dependency
    # expects. 4401 (a custom code in the reserved-for-app-use 4000-4999
    # range) mirrors HTTP 401 as closely as a WS close code allows.
    if not token:
        await websocket.close(code=4401)
        return
    try:
        _verify_supabase_jwt(token)
    except HTTPException:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    ws_registry.register(session_id, websocket)
    try:
        while True:
            # Nothing meaningful is expected from the client on this
            # socket — it exists to receive push events, not send
            # them. Reading (and discarding) keeps the connection
            # alive and is how Starlette surfaces a client-initiated
            # disconnect (WebSocketDisconnect) instead of this loop
            # spinning against an already-closed socket.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_registry.unregister(session_id, websocket)


# B6 — graph edges, node summaries, topics/graph, node get/rename/delete,
# note-candidates, backlinks/detect, and clusters propose/candidates/
# accept/reject moved to api/routes/graph_and_notes.py (piece 6), along
# with the RenameNodeRequest/CreateEdgeRequest models they used. See
# that file for the actual route code.
#
# B6 piece 7 — the Notebooks "Generate" command and its whole target
# roster, classify-intent, per-topic workflow, data tables, the
# "simulate" domain, the whole /api/notes/* family, and /api/capabilities
# moved to api/routes/notebooks.py, along with every pydantic model and
# helper (including _parse_marketplace_reviews) that only they used. See
# that file for the actual route code.


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