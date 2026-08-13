"""
api/routes/local_workspace.py — F2 Part 3 + Part 4. Part 3 shipped the
HTTP surface over eo/local_workspace_tools.py's list_dir/read_file,
plus one status check (is a daemon even connected right now) Part 6's
frontend file tree will poll before trying to render anything real.

Part 4 adds the propose -> confirm/deny -> execute surface over
write_file/delete/execute_command:
  - POST .../local/propose   body: {tool, params}   -> {action_id, tool,
    params, expires_in_seconds} -- nothing has touched the daemon yet.
  - POST .../local/confirm   body: {action_id}       -> runs the
    proposed action for real and returns the daemon's result.
  - POST .../local/deny      body: {action_id}       -> discards the
    proposal; the daemon is never contacted.
These three deliberately do NOT run list_dir/read_file -- see
eo/local_workspace_tools.py's propose_action() docstring for why
routing a read tool through here is a ValueError, not a shortcut: the
read tools already have their own always-runs-free routes above, and
propose/confirm existing for them too would just be a slower way to
do the same thing with no safety benefit.

Same auth/ownership shape every other workspace-scoped route uses (see
api/routes/workspaces.py, api/routes/notebooks.py's classify_intent):
require_auth + chat_workspace.get_workspace(ws_id, owner_id) to 404 an
unknown/inaccessible workspace before doing anything daemon-related.

Place this file at: api/routes/local_workspace.py
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from eo import chat_workspace, local_workspace
from eo.local_workspace_tools import (
    PENDING_ACTION_TTL_SECONDS,
    PendingActionError,
    confirm_action,
    deny_action,
    list_workspace_dir,
    propose_action,
    read_workspace_file,
)

router = APIRouter()


def _require_workspace(ws_id: str, owner_id: str) -> None:
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@router.get("/api/workspaces/{ws_id}/local/status")
def local_status(ws_id: str, owner_id: str = Depends(require_auth)):
    """Cheap poll target -- a registry lookup, not a daemon round-trip
    -- so Part 6's frontend can check this on a short interval without
    driving load through the actual websocket connection."""
    _require_workspace(ws_id, owner_id)
    return {"live": local_workspace.is_live(ws_id)}


class ListDirRequest(BaseModel):
    path: str = "."


class ReadFileRequest(BaseModel):
    path: str


@router.post("/api/workspaces/{ws_id}/local/list_dir")
async def local_list_dir(
    ws_id: str, req: ListDirRequest, owner_id: str = Depends(require_auth)
):
    _require_workspace(ws_id, owner_id)
    try:
        return await list_workspace_dir(ws_id, req.path)
    except local_workspace.ToolCallError as exc:
        # 409, not 500/404: the workspace is real and the request is
        # well-formed -- this is specifically "no live daemon right
        # now" or "the daemon itself rejected this path/tool", both of
        # which the frontend needs to distinguish from an actual
        # server error (see Part 6/7's confirm/deny + connection-state
        # UI, which branches on exactly this).
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/workspaces/{ws_id}/local/read_file")
async def local_read_file(
    ws_id: str, req: ReadFileRequest, owner_id: str = Depends(require_auth)
):
    _require_workspace(ws_id, owner_id)
    try:
        return await read_workspace_file(ws_id, req.path)
    except local_workspace.ToolCallError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---------------------------------------------------------------------
# Part 4 — propose / confirm / deny for write_file, delete, and
# execute_command.
# ---------------------------------------------------------------------

class ProposeActionRequest(BaseModel):
    tool: str  # one of "write_file" | "delete" | "execute_command"
    params: Dict[str, Any] = {}


class ProposeActionResponse(BaseModel):
    action_id: str
    tool: str
    params: Dict[str, Any]
    expires_in_seconds: int


class ActionIdRequest(BaseModel):
    action_id: str


@router.post("/api/workspaces/{ws_id}/local/propose", response_model=ProposeActionResponse)
def local_propose_action(
    ws_id: str, req: ProposeActionRequest, owner_id: str = Depends(require_auth)
):
    """Validates and stores the proposal; never contacts the daemon.
    A 400 here (bad tool name / missing params) means the proposal
    itself was malformed -- distinct from the 409s below, which mean
    "well-formed, but the action_id or its execution didn't work
    out." No `_require_workspace` bypass for read tools slipping in
    here: propose_action() itself rejects list_dir/read_file with the
    same ValueError->400 path as any other bad tool name, since routing
    a read through propose/confirm isn't a smaller ask than this
    endpoint is scoped for, it's just the wrong endpoint for it."""
    _require_workspace(ws_id, owner_id)
    try:
        action = propose_action(ws_id, req.tool, req.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return ProposeActionResponse(
        action_id=action.action_id,
        tool=action.tool,
        params=action.params,
        expires_in_seconds=PENDING_ACTION_TTL_SECONDS,
    )


@router.post("/api/workspaces/{ws_id}/local/confirm")
async def local_confirm_action(
    ws_id: str, req: ActionIdRequest, owner_id: str = Depends(require_auth)
):
    """Runs the proposed action for real. 404 if action_id is unknown/
    expired/wrong-workspace (nothing to confirm); 409 if the action was
    valid but the daemon call itself failed (no daemon connected, the
    daemon rejected the path, the command errored, etc.) -- same 409
    convention local_list_dir/local_read_file above already use for
    "well-formed request, daemon-side failure.\""""
    _require_workspace(ws_id, owner_id)
    try:
        return await confirm_action(ws_id, req.action_id)
    except PendingActionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except local_workspace.ToolCallError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/workspaces/{ws_id}/local/deny")
def local_deny_action(
    ws_id: str, req: ActionIdRequest, owner_id: str = Depends(require_auth)
):
    """Discards a pending action. The daemon is never contacted on this
    path. 404 under the same conditions as confirm above."""
    _require_workspace(ws_id, owner_id)
    try:
        deny_action(ws_id, req.action_id)
    except PendingActionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"action_id": req.action_id, "status": "denied"}
