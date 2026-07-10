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


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from eo.project_registry import list_projects, generate_control_unit, register_project
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Union, Any

from api.task_runner import run_task, preview_task, confirm_task   # preview/confirm NEW — Part 2 §2.5
from eo.executor import resume_graph   # NEW — Part 2 §2.4
from eo.quota_sentinel import get_quota_snapshot, get_usage_history, get_usage_history_scoped
from eo import chat_store
from eo import memory_batch
from eo import chat_workspace
from eo import workspace_facts
from eo import graph_edges

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