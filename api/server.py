"""
api/server.py — Stage 6, step 2 (Part 10).

The thin HTTP layer in front of api/task_runner.py. This is intentionally
the smallest possible FastAPI app: one endpoint, one job — take a task,
run it through the EO layer synchronously, return the result as JSON.

No streaming, no Pusher, no live panels here — that's Stage 6 step 1
(relay) and steps 3-6 (live UI), layered on top of this later. Step 2's
job is just proving task-in/result-out works end to end.

Run locally:
    pip install fastapi uvicorn
    uvicorn api.server:app --reload --port 8000

CORS is open to the Next.js dev server origin (localhost:3000) only —
tighten this before deploying anywhere real.
"""
import os
import sys
import traceback


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from eo.project_registry import list_projects, generate_control_unit, register_project
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Union

from api.task_runner import run_task
from eo.quota_sentinel import get_quota_snapshot, get_usage_history
from eo import chat_store   # NEW — persistent, file-per-chat storage (see chat_store.py)
from eo import memory_batch  # NEW — mutual-membership chat groups (§3)
from eo import chat_workspace  # NEW — named containers with auto-linking membership (§7)

app = FastAPI(title="MiniMe v6 — EO layer API")

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
    return chat_store.create_chat(title=req.title or "New Chat")


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


class TaskRequest(BaseModel):
    task_text: str
    tier_override: Optional[int] = None
    directed_task_type: Optional[str] = None
    app_slug: Optional[str] = None
    run_tests: bool = False
    session_id: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None   # NEW


class TaskResponse(BaseModel):
    # tier is int for tiers 0-3, or the literal string "sga" when the
    # Starter General Agents resolved the task before classification
    # (Part 2) — loosened from `int` to fix a latent validation bug that
    # would have 500'd on every real SGA-resolved HTTP request.
    decision: dict
    tier: Union[int, str]
    session_id: Optional[str] = None
    # status values: "ok" | "error" | "needs_app" | "needs_directed_task_type"
    # | "not_wired_yet" | "needs_beast_mode_confirmation" (Part 3)
    # | "needs_beast_mode_choice" (Part 3)
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
            project_unique_name=req.project_unique_name,   # NEW
        )
    except Exception as exc:
        # Step 2 has no relay yet, so a stack trace on the server console
        # is the only debugging signal you get — keep it, but also return
        # a clean JSON error instead of a raw 500 with no body.
        traceback.print_exc()
        return TaskResponse(
            decision={},
            tier=-1,
            status="error",
            result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/quota", dependencies=[Depends(require_auth)])
def quota():
    return get_quota_snapshot()


@app.get("/api/usage/history", dependencies=[Depends(require_auth)])
def usage_history(days: int = Query(7, ge=1, le=90)):
    # Cross-session, persisted day-by-day usage (Part 19 candidate flagged
    # in the Part 17 guide) -- reads the same usage:{provider}:{key_id}:
    # {date} records /api/quota already reads for today, just repeated
    # across the last `days` calendar dates. See eo/quota_sentinel.py's
    # get_usage_history() docstring for the exact response shape.
    return get_usage_history(days=days)


@app.get("/api/projects", dependencies=[Depends(require_auth)])
def projects():
    return list_projects()