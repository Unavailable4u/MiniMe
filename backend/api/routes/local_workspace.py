"""
api/routes/local_workspace.py — F2 Part 3: the HTTP surface over
eo/local_workspace_tools.py's list_dir/read_file, plus one status check
(is a daemon even connected right now) Part 6's frontend file tree will
poll before trying to render anything real.

Same auth/ownership shape every other workspace-scoped route uses (see
api/routes/workspaces.py, api/routes/notebooks.py's classify_intent):
require_auth + chat_workspace.get_workspace(ws_id, owner_id) to 404 an
unknown/inaccessible workspace before doing anything daemon-related.

Place this file at: api/routes/local_workspace.py
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from eo import chat_workspace, local_workspace
from eo.local_workspace_tools import list_workspace_dir, read_workspace_file

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
