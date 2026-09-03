"""
api/routes/chats.py

B6, piece 3 — projects, persistent chats (see eo/chat_store.py), and
memory batches (see eo/memory_batch.py). Pulled out of api/server.py
verbatim (same functions, same error handling, same docstrings) —
nothing here changes behavior, this is a pure move.

Grouped as one module rather than three separate files because chats
belong to batches/projects — splitting them apart would just create
cross-file imports for no benefit.

Includes both /api/projects routes (POST register + GET list), even
though they lived far apart in the original server.py (the GET sat
down near the deploy routes) — same domain, so they move together
rather than leaving one half behind.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import _resolve_chat_or_404, require_auth
from eo import chat_store, chat_workspace, memory_batch
from eo.project_registry import generate_control_unit, list_projects, register_project

router = APIRouter()


class RegisterProjectRequest(BaseModel):
    path: str
    display_name: str


class CreateChatRequest(BaseModel):
    title: str | None = "New Chat"
    template_id: str | None = None   # NEW — one chat per template


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


class AppendMessageRequest(BaseModel):
    message: dict


# Perf audit item #4 (optional hardening): the default GET /api/chats/{id}
# has always been served today by chat_store.get_chat() (limit/before_seq/
# after_seq is opt-in pagination -- see that function's docstring), and the
# current frontend always passes limit=60 (or after_seq) on its chat-open
# flow (checked in WorkspaceDockContext.jsx), so there's no known caller
# relying on the fully-unbounded shape in practice. This constant exists so
# that guarantee is enforced here, server-side, instead of resting entirely
# on "every current caller happens to pass limit" -- a future/other caller
# that omits limit no longer silently reintroduces the pre-migration "ship
# the whole chat" cost. Set to the route's own existing max (`le=200` below)
# so this only changes behavior for a bare request with no params at all;
# any caller already passing an explicit limit, before_seq, or after_seq is
# completely unaffected.
DEFAULT_CHAT_PAGE_LIMIT = 200


@router.post("/api/projects", dependencies=[Depends(require_auth)])
def register_project_endpoint(req: RegisterProjectRequest):
    unit = generate_control_unit(req.display_name)
    register_project(unit["unique_name"], req.path)
    return {"unique_name": unit["unique_name"], "root_path": req.path}


@router.get("/api/projects", dependencies=[Depends(require_auth)])
def projects():
    return list_projects()


# --- persistent chats (see eo/chat_store.py) ------------------------------
# chat_id and session_id are the same string everywhere in this system —
# the sidebar creates a chat_id via POST /api/chats, and that value is
# passed straight through as session_id on /api/task.
# _resolve_chat_or_404 itself now lives in api/deps.py (imported at the
# top of this file) — every route below still calls it exactly the same
# way, this is a pure move.

@router.get("/api/chats")
def get_chats(owner_id: str = Depends(require_auth)):
    return chat_store.list_chats(owner_id)


@router.post("/api/chats")
def create_chat(req: CreateChatRequest, owner_id: str = Depends(require_auth)):
    return chat_store.create_chat(owner_id, title=req.title or "New Chat", template_id=req.template_id)


@router.get("/api/chats/{chat_id}")
def get_chat(
    chat_id: str,
    background_tasks: BackgroundTasks,
    owner_id: str = Depends(require_auth),
    limit: int | None = Query(default=None, ge=1, le=200),
    before_seq: int | None = Query(default=None, ge=0),
    after_seq: int | None = Query(default=None, ge=0),
):
    # Perf audit item #2 (B4 follow-up): resolve access via
    # chat_store.resolve_chat_access() directly (rather than
    # _resolve_chat_or_404(), which discards the role) so the role it
    # computes can be reused below by can_see_attribution() instead of
    # being looked up a second time. get_chat never needs require_edit,
    # so this is a behavior-preserving substitution for the 404 case —
    # same resolution, same "doesn't exist vs. not shared with you"
    # 404, just without throwing away the role we already paid for.
    resolved = chat_store.resolve_chat_access(chat_id, owner_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown chat_id")
    real_owner_id, requester_role = resolved
    if after_seq is not None and (limit is not None or before_seq is not None):
        # Perf audit item #5: after_seq is a distinct fetch mode (see
        # chat_store.get_chat() docstring) — reject an ambiguous
        # combined request instead of silently picking one.
        raise HTTPException(
            status_code=400,
            detail="after_seq cannot be combined with limit or before_seq",
        )
    if limit is None and before_seq is None and after_seq is None:
        # Perf audit item #4: bare "no params at all" request -- apply
        # the server-side default instead of falling through to
        # chat_store.get_chat()'s unbounded limit=None branch. A caller
        # that explicitly passed before_seq or after_seq is left alone;
        # this only closes the "forgot to paginate" gap.
        limit = DEFAULT_CHAT_PAGE_LIMIT
    try:
        # Perf audit item #3: limit/before_seq pass straight through to
        # chat_store.get_chat() (already supported it — see that
        # function's docstring — this is the "wire it into a route"
        # step). A plain GET with no query params no longer reaches
        # chat_store.get_chat() as limit=None, though -- see item #4's
        # DEFAULT_CHAT_PAGE_LIMIT fallback above, which now applies in
        # that exact case.
        #
        # Perf audit item #5: after_seq passes through the same way —
        # "give me only what's new since seq N", for a client that
        # already has messages 1..N cached from a previous open of
        # this chat. Defaults to None, so it's a no-op unless a caller
        # opts in.
        # Perf audit item #7 follow-up: passing this request's
        # BackgroundTasks handle through lets the before_seq branch's
        # cache bookkeeping (hit/miss stats, hit counter, threshold
        # cache-populate) run after the response is sent instead of
        # blocking on it -- see get_chat()'s own docstring for the
        # full reasoning.
        chat = chat_store.get_chat(chat_id, real_owner_id, limit=limit,
                                    before_seq=before_seq, after_seq=after_seq,
                                    background_tasks=background_tasks)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")

    # Part 8.4: strip author_id from each message if this requester's
    # role/workspace setting says they shouldn't see who-wrote-what.
    # `owner_id` here is the ACTUAL caller (pre-resolution) — the right
    # identity to check attribution visibility against, not real_owner_id.
    ws_id = chat.get("workspace_id")
    if ws_id and not chat_workspace.can_see_attribution(ws_id, owner_id, role=requester_role):
        chat["messages"] = [
            {k: v for k, v in m.items() if k != "author_id"} for m in chat.get("messages", [])
        ]
    return chat


@router.patch("/api/chats/{chat_id}/rename")
def rename_chat(chat_id: str, req: RenameChatRequest, owner_id: str = Depends(require_auth)):
    real_owner_id = _resolve_chat_or_404(chat_id, owner_id, require_edit=True)
    try:
        return chat_store.rename_chat(chat_id, real_owner_id, req.title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@router.patch("/api/chats/{chat_id}/links")
def link_chats(chat_id: str, req: LinkChatsRequest, owner_id: str = Depends(require_auth)):
    real_owner_id = _resolve_chat_or_404(chat_id, owner_id, require_edit=True)
    try:
        return chat_store.set_linked_chats(chat_id, real_owner_id, req.linked_chat_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown chat_id")


@router.post("/api/chats/{chat_id}/messages")
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


@router.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, owner_id: str = Depends(require_auth)):
    # Deliberately NOT routed through the collaborator resolver — outright
    # deletion stays owner-only, same discipline as workspace deletion. An
    # editor can remove a chat from the workspace grouping (see
    # chat_workspace.remove_chat) but cannot delete someone else's chat.
    chat_store.delete_chat(chat_id, owner_id)
    return {"status": "deleted", "id": chat_id}


# --- memory batches: mutual-membership groups (see eo/memory_batch.py) ---

@router.get("/api/batches")
def get_batches(owner_id: str = Depends(require_auth)):
    return memory_batch.list_batches(owner_id)


@router.post("/api/batches/estimate")
def estimate_batch(req: EstimateBatchRequest, owner_id: str = Depends(require_auth)):
    """Called live from the create-batch modal as the user checks/unchecks
    chats — NOT tied to an existing batch_id, since the whole point is to
    show the cost BEFORE creating one. See chat_store.estimate_batch_context_tokens."""
    return chat_store.estimate_batch_context_tokens(owner_id, req.chat_ids)


@router.post("/api/batches")
def create_batch(req: CreateBatchRequest, owner_id: str = Depends(require_auth)):
    try:
        return memory_batch.create_batch(owner_id, req.name, req.member_chat_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/api/batches/{batch_id}/rename")
def rename_batch(batch_id: str, req: RenameBatchRequest, owner_id: str = Depends(require_auth)):
    try:
        return memory_batch.rename_batch(batch_id, owner_id, req.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")


@router.post("/api/batches/{batch_id}/unlink")
def unlink_batch_members(batch_id: str, req: BatchMembersRequest, owner_id: str = Depends(require_auth)):
    """Returns {"dissolved": true} if removing these members collapsed the
    batch to <=1, otherwise returns the updated batch."""
    try:
        result = memory_batch.unlink_members(batch_id, owner_id, req.chat_ids)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")
    return result if result else {"dissolved": True, "id": batch_id}


@router.post("/api/batches/{batch_id}/members")
def add_batch_member(batch_id: str, req: BatchMembersRequest, owner_id: str = Depends(require_auth)):
    try:
        for cid in req.chat_ids:
            memory_batch.add_member(batch_id, owner_id, cid)
        return memory_batch.get_batch(batch_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")


@router.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str, owner_id: str = Depends(require_auth)):
    try:
        memory_batch.delete_batch(batch_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown batch_id")
    return {"status": "deleted", "id": batch_id}
