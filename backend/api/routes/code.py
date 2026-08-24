"""
api/routes/code.py

Master Guide V2 step 16 (T3), patch 8 — API routes for the Build tab's
Code sub-tab, backed by eo/workspace_code_files.py. New file rather than
folding into api/routes/workspace_data.py: this surface is going to keep
growing across the next few patches (patch 9's write-back wiring writes
through the same module but no new routes; patch 11 adds a ZIP-download
route here), and workspace_data.py's own docstring already scopes itself
to the pre-existing facts/panels/progress/export family — code files are
a new, separate concern that reads more clearly with its own file.

Same ownership-gate-then-delegate shape every workspace-scoped route in
this repo already uses (see workspace_data.py's panel-content routes,
api/routes/tasks.py's get_tasks_for_workspace): confirm the caller can
see this workspace via chat_workspace.get_workspace() before touching
eo/workspace_code_files.py at all, so a stranger's ws_id guess 404s
before it ever reaches the file-content query.
"""
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.deps import require_auth
from eo import chat_workspace, workspace_code_files

router = APIRouter()


class CodeFileWriteRequest(BaseModel):
    content: str
    language: str | None = None  # optional -- write_file() infers from the extension when omitted


def _require_workspace(ws_id: str, owner_id: str):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


# --- Code sub-tab file persistence (see eo/workspace_code_files.py) ------

@router.get("/api/workspaces/{ws_id}/code/files", dependencies=[Depends(require_auth)])
def list_code_files(ws_id: str, owner_id: str = Depends(require_auth)):
    """Metadata only (path/language/size/updated_at), not content -- see
    workspace_code_files.list_files()'s own docstring for why. Patch
    10's file-tree view is the intended caller."""
    _require_workspace(ws_id, owner_id)
    return workspace_code_files.list_files(ws_id)


@router.get("/api/workspaces/{ws_id}/code/files/{file_path:path}", dependencies=[Depends(require_auth)])
def get_code_file(ws_id: str, file_path: str, owner_id: str = Depends(require_auth)):
    """`{file_path:path}` (not the default `{file_path}`) so a nested
    path like `src/todo/task_editor.py` is captured whole, slashes
    included, instead of FastAPI treating each `/` as a new path
    segment boundary."""
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.get_file(ws_id, file_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/api/workspaces/{ws_id}/code/files/{file_path:path}", dependencies=[Depends(require_auth)])
def put_code_file(ws_id: str, file_path: str, req: CodeFileWriteRequest, owner_id: str = Depends(require_auth)):
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.write_file(ws_id, file_path, req.content, owner_id, language=req.language)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Patch 11: server-side ZIP of the current file set --------------------

@router.get("/api/workspaces/{ws_id}/code/zip", dependencies=[Depends(require_auth)])
def download_code_zip(ws_id: str, owner_id: str = Depends(require_auth)):
    """Streams the workspace's saved files back as one .zip — built
    in-memory by workspace_code_files.build_zip_archive(), see that
    function's own docstring for why this doesn't touch disk. 404 (not
    an empty zip) when nothing's been saved yet, same "nothing to
    export" posture as workspace_data.export_workspace_files()."""
    _require_workspace(ws_id, owner_id)
    data = workspace_code_files.build_zip_archive(ws_id)
    if data is None:
        raise HTTPException(status_code=404, detail="No code files saved for this workspace yet")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{ws_id}_code.zip"'},
    )
