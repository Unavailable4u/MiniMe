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

W1.1 (Build Workbench plan): PUT gains an optional base_version for
optimistic conflict detection (409 with the current file on a stale
write), and two new routes — GET .../history and POST .../restore —
expose eo/workspace_code_files.py's new version-history layer. See
that module's own docstring for the full "why" (W1.1 update section
near the top).

W1.2 (Build Workbench plan): DELETE .../code/files/{path} now covers a
whole folder subtree, not just one file, and two new fixed-path
routes — POST .../code/move and POST .../code/folders — expose
eo/workspace_code_files.py's move_path() and create_folder(). Neither
new route uses the `{file_path:path}` converter (their paths are the
literal segments "move"/"folders", not a captured file path), so
unlike GET .../history and POST .../restore above, they don't need any
particular registration order relative to the generic
`{file_path:path}` routes.

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
from pydantic import BaseModel, ConfigDict, Field

from api.deps import require_auth
from eo import chat_workspace, workspace_code_files
from eo.workspace_code_files import VersionConflictError

router = APIRouter()


class CodeFileWriteRequest(BaseModel):
    content: str
    language: str | None = None  # optional -- write_file() infers from the extension when omitted
    # W1.1: optional optimistic-concurrency check. None (the default)
    # keeps the pre-W1.1 blind-overwrite behavior -- see
    # workspace_code_files.write_file()'s own docstring for why that's
    # still the right default for a caller with nothing to compare
    # against. The editor (once W2.5's Save flow lands) always sends
    # the version it last read.
    base_version: int | None = None


class RestoreVersionRequest(BaseModel):
    version: int


class MovePathRequest(BaseModel):
    """W1.2: field names match the plan's `{from, to}` shape on the
    wire via `alias` — `from` alone can't be a Python attribute name,
    it's a reserved keyword. `populate_by_name=True` lets tests/other
    Python callers construct this with `from_path=`/`to_path=` too,
    not just the aliased JSON keys."""
    model_config = ConfigDict(populate_by_name=True)

    from_path: str = Field(alias="from")
    to_path: str = Field(alias="to")


class CreateFolderRequest(BaseModel):
    path: str


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


# W1.1: registered BEFORE the generic GET/PUT `{file_path:path}` routes
# below on purpose. FastAPI/Starlette matches routes in registration
# order, and `{file_path:path}` is a greedy converter that swallows
# every remaining `/`-separated segment -- if the generic GET route
# came first, a request for `.../code/files/src/app.py/history` would
# match IT, with file_path resolving to the nonsensical
# "src/app.py/history", and this route would never be reached. Keep
# any future `{file_path:path}`-suffixed route above the two generic
# ones for the same reason.

@router.get(
    "/api/workspaces/{ws_id}/code/files/{file_path:path}/history",
    dependencies=[Depends(require_auth)],
)
def get_code_file_history(ws_id: str, file_path: str, owner_id: str = Depends(require_auth)):
    """Past versions only, most recent first — see
    workspace_code_files.get_file_history()'s own docstring for why the
    current/live version isn't included here (GET the file itself for
    that). Patch W2.6's History panel is the intended caller."""
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.get_file_history(ws_id, file_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/api/workspaces/{ws_id}/code/files/{file_path:path}/restore",
    dependencies=[Depends(require_auth)],
)
def restore_code_file(
    ws_id: str, file_path: str, req: RestoreVersionRequest, owner_id: str = Depends(require_auth)
):
    """Brings an old version back as a NEW version — see
    workspace_code_files.restore_version()'s own docstring for why this
    never conflicts and never rewrites history. 400 (not 404) when
    `version` doesn't exist for this file, same "bad reference, not a
    missing resource" treatment a bad file_path already gets from every
    other route in this file — the workspace and the route both exist;
    it's the referenced version that's invalid."""
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.restore_version(ws_id, file_path, req.version, owner_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    """W1.1: `req.base_version` is optional optimistic-concurrency —
    None keeps the old blind-overwrite behavior. When it's given and
    stale, workspace_code_files.write_file() raises
    VersionConflictError, caught below and turned into a 409 whose body
    IS the current file (version included) — everything a client needs
    to offer Reload / Keep mine / Compare (W2.5) without a second
    round trip."""
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.write_file(
            ws_id, file_path, req.content, owner_id,
            language=req.language, base_version=req.base_version,
        )
    except VersionConflictError as e:
        raise HTTPException(status_code=409, detail=e.current)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/workspaces/{ws_id}/code/files/{file_path:path}", dependencies=[Depends(require_auth)])
def delete_code_file(ws_id: str, file_path: str, owner_id: str = Depends(require_auth)):
    """W1.2: `file_path` is matched as an exact file OR, by prefix, a
    whole folder subtree — see workspace_code_files.delete_file()'s
    own docstring for exactly what "prefix" means here (the same rule
    move_path() uses below). Idempotent: a path with nothing under it
    is 200 with an empty `deleted_paths`, not a 404 — deleting
    something that's already gone is still success."""
    _require_workspace(ws_id, owner_id)
    try:
        deleted_paths = workspace_code_files.delete_file(ws_id, file_path, owner_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted_paths": deleted_paths}


# --- W1.2: folder/subtree operations (fixed paths — no `{file_path:path}`,
# see this file's own docstring for why registration order doesn't matter
# for these two) --------------------------------------------------------

@router.post("/api/workspaces/{ws_id}/code/move", dependencies=[Depends(require_auth)])
def move_code_path(ws_id: str, req: MovePathRequest, owner_id: str = Depends(require_auth)):
    """W1.2: renames/moves a file or, by prefix, a folder subtree — see
    workspace_code_files.move_path()'s own docstring for the full
    "one transaction, history follows the file, refuses clobbering an
    existing target" contract. A 400 here covers every one of that
    function's refusal cases (same path twice, moving into your own
    subtree, nothing at the source, a destination collision) — they're
    all bad-request shape, not a missing-resource 404."""
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.move_path(ws_id, req.from_path, req.to_path, owner_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/workspaces/{ws_id}/code/folders", dependencies=[Depends(require_auth)])
def create_code_folder(ws_id: str, req: CreateFolderRequest, owner_id: str = Depends(require_auth)):
    """W1.2: creates an (otherwise-invisible, see
    workspace_code_files.create_folder()'s own docstring) empty folder
    by writing a `.gitkeep` placeholder under it. Returns that
    placeholder file's shape — same response shape PUT's write_file()
    already returns."""
    _require_workspace(ws_id, owner_id)
    try:
        return workspace_code_files.create_folder(ws_id, req.path, owner_id)
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
