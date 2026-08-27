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
import signal
import sys

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

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # NEW — Data Layer architecture §9b: capturing the running

# event loop at startup for eo/ws_registry.py's thread-safe push
from contextlib import asynccontextmanager  # NEW — §9b: lifespan startup hook

from fastapi import (  # NEW — §9b
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware

# B6 — auth/JWT verification (SUPABASE_URL, require_auth,
# _verify_supabase_jwt, _resolve_chat_or_404, etc.) moved to api/deps.py
# so api/routes/* modules can import it without a circular import back
# into this file. See api/deps.py's module docstring for why.
from api.deps import _verify_supabase_jwt
from eo import mcp_client  # NEW — Patch A2: clean shutdown of any live MCP connections
from eo import mcp_registry  # NEW — Patch A2: startup connect for configured MCP servers
from utils.llm_client import request_shutdown  # NEW — Patch 6.2
from api.routes.chats import router as chats_router
from api.routes.code import router as code_router
from api.routes.deploy import router as deploy_router
from api.routes.graph_and_notes import router as graph_and_notes_router

# F2 Part 3 — local_workspace_data_router owns the HTTP surface over the
# read-only list_dir/read_file tool calls (+ /local/status). A separate
# router/file from local_workspace_router above (which is the websocket
# route + registry) since this one follows the plain require_auth +
# ws_id-ownership shape every other api/routes/*.py file uses, unlike
# the websocket route's pairing-token auth.
from api.routes.local_workspace import router as local_workspace_data_router
from api.routes.notebooks import router as notebooks_router
from api.routes.system import router as system_router

# B6 — routers split out of this file. tasks_router owns /api/task*,
# /api/resume, /api/roles*, /api/workflow-templates*, and (as of piece 6)
# /api/tasks/{session_id} + /api/tasks/workspace/{ws_id} (piece 1, plus
# the leftover pair folded in alongside piece 6); system_router owns
# /api/health, /api/quota, /api/usage/history (piece 2); graph_and_notes_
# router owns graph edges, node summaries, topics/graph, nodes, note
# candidates, backlinks, and clusters (piece 6). This is also where
# CO3's /api/task/{id}/pause and CO5's /api/task/{id}/stream will
# register once built, inside tasks_router. deploy_router (piece 8, the
# last piece of the B6 split) owns the deploy propose/write/go-live
# trio and the two UptimeRobot monitoring routes. code_router (patch 8)
# owns the Build tab's Code sub-tab file persistence -- list/get/write
# per workspace file path, see api/routes/code.py's own docstring for
# why it's a separate file rather than folded into workspace_data.py.
from api.routes.tasks import router as tasks_router
from api.routes.workspace_data import router as workspace_data_router
from api.routes.workspaces import router as workspaces_router
from eo import ws_registry  # NEW — §9b: per-session WebSocket connection registry

# F2 Part 2 — local_workspace_router owns /ws/daemon/{workspace_id}, the
# daemon's own websocket route. Included as a router (not a bare
# @app.websocket like /ws/{session_id} below) purely so its handshake
# logic lives in eo/local_workspace.py alongside the registry it shares
# module state with, rather than split across two files.
from eo.local_workspace import router as local_workspace_router

SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv("ENVIRONMENT", "development"),
        integrations=[FastApiIntegration()],
        traces_sample_rate=1.0,
    )

# B6 — run_task/preview_task/confirm_task/run_task_from_template,
# resume_graph, the eo.registry role-library functions, and the
# eo.structure workflow-template functions moved to api/routes/tasks.py
# along with the routes that used them. get_quota_snapshot/
# get_usage_history/get_usage_history_scoped moved to api/routes/
# system.py the same way. STRUCTURE_TEMPLATES moved to
# api/routes/notebooks.py with /simulate (piece 7) — nothing left in
# this file reads it directly anymore.
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
#
# B6 piece 8 (final piece) — DeployActionRequest/UptimeRobotKeyRequest/
# UptimeRobotRegisterRequest, the deploy_config_writer/deploy_agent
# imports, set_app_slug, and MissingDependencyError, along with the five
# routes that used them (/api/deploy/{session_id}/propose|write|go-live,
# /api/monitoring/{session_id}/uptimerobot-key|uptimerobot-register),
# moved to api/routes/deploy.py. Nothing left in this file needs any of
# them — this file is now app setup + the websocket endpoint only.

def _handle_sigint(signum, frame):
    # NEW — Patch 6.2: signal in-flight rate-limit retry waits (see
    # utils/llm_client.py's _interruptible_sleep()) to stop instead of
    # letting uvicorn's graceful shutdown wait out every retry sleep in
    # the worker thread it can't otherwise interrupt.
    print("[shutdown] SIGINT received -- signaling in-flight LLM retries to stop")
    request_shutdown()
    # Let uvicorn's own default handler still run afterward so it
    # proceeds with its normal graceful-shutdown sequence once the
    # in-flight request (now unblocked) returns.
    signal.default_int_handler(signum, frame)


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
    # NEW — Patch 6.2: register once the app actually starts serving
    # (module import alone can happen under --reload's parent process
    # too; this is the one place we know we're the live server).
    signal.signal(signal.SIGINT, _handle_sigint)
    # NEW — Patch A2: connect every `enabled` server in
    # backend/config/mcp_servers.json exactly once, at real server
    # startup (not on every --reload parent-process import, same
    # reasoning as the signal.signal() call just above). A server that
    # fails to connect (missing token, `npx` not on PATH, etc.) is
    # logged and skipped by connect_configured_servers() itself -- this
    # never blocks the rest of the app from starting.
    await mcp_registry.connect_configured_servers()
    yield
    # NEW — Patch A2: mirror image of the connect above -- clean
    # shutdown of every live MCP connection (closes stdio subprocesses,
    # closes HTTP clients) on the same graceful-shutdown path uvicorn
    # already drives this generator through. eo.mcp_client.shutdown_all()
    # existed since Patch A1 but had no caller until now.
    await mcp_client.shutdown_all()


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

# B6 — routers split out of this file (imported near the top of this
# file, alongside the other imports). tasks_router owns /api/task*,
# /api/resume, /api/roles*, /api/workflow-templates*, and (as of piece 6)
# /api/tasks/{session_id} + /api/tasks/workspace/{ws_id} (piece 1, plus
# the leftover pair folded in alongside piece 6); system_router owns
# /api/health, /api/quota, /api/usage/history (piece 2); graph_and_notes_
# router owns graph edges, node summaries, topics/graph, nodes, note
# candidates, backlinks, and clusters (piece 6). This is also where
# CO3's /api/task/{id}/pause and CO5's /api/task/{id}/stream will
# register once built, inside tasks_router. deploy_router (piece 8, the
# last piece of the B6 split) owns the deploy propose/write/go-live
# trio and the two UptimeRobot monitoring routes. code_router (patch 8)
# owns the Build tab's Code sub-tab file persistence -- list/get/write
# per workspace file path, see api/routes/code.py's own docstring for
# why it's a separate file rather than folded into workspace_data.py.
app.include_router(tasks_router)
app.include_router(system_router)
app.include_router(chats_router)
app.include_router(workspaces_router)
app.include_router(workspace_data_router)
app.include_router(graph_and_notes_router)
app.include_router(notebooks_router)
app.include_router(deploy_router)
app.include_router(code_router)
app.include_router(local_workspace_router)  # F2 Part 2: /ws/daemon/{workspace_id}
app.include_router(local_workspace_data_router)  # F2 Part 3: /api/workspaces/{ws_id}/local/*


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
