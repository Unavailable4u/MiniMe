"""
api/routes/code_edit.py — W5.1 (Build Workbench plan, step 197): the
proposal endpoints behind Chat's Edit mode (W4.2) and the review UI
(W5.3/W5.4). New file rather than folding into api/routes/code.py, per
the plan's own step text ("api/routes/code.py (or new code_edit.py
router + include_router in api/server.py)") — code.py's own docstring
already scopes itself to workspace_code_files' plain file/folder CRUD;
a proposal is a distinct resource with its own lifecycle (pending ->
accepted/rejected/partial/stale/failed) that only ever TOUCHES
workspace_code_files indirectly, via eo/code_proposals.py's
resolve_proposal() — see that module's own docstring.

Same ownership-gate-then-delegate shape every workspace-scoped router
in this codebase already uses (api/routes/code.py, api/routes/
local_workspace.py, api/routes/mcp.py) — confirm the caller can see
this workspace via chat_workspace.get_workspace() before touching
eo/code_proposals.py at all, so a stranger's ws_id guess 404s before
it ever reaches a proposal's content. `_require_workspace` is
duplicated here rather than imported from api/routes/code.py, same
convention every one of those sibling router files already follows
for this exact helper (each keeps its own copy — see any of their own
docstrings for why: no shared api/routes/_common.py exists for this
yet, and a two-line ownership check isn't worth inventing one for).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_auth
from eo import chat_workspace, code_proposals
from eo.code_proposals import ProposalStaleError

router = APIRouter()


def _require_workspace(ws_id: str, owner_id: str):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


# refs is passed through to eo/code_proposals.py as plain dicts, not a
# typed model — deliberately. The ref shape this carries is
# frontend/app/lib/workbench/codeContext.js's own `{id, kind, path,
# fromLine, toLine, snippet, hash, provider, ...}`, which is still
# growing (W6.x's `element`/`error` kinds add fields to the SAME
# shape) and is only PARTIALLY interpreted server-side even once fully
# grown — eo/code_proposals.py's _paths_from_refs() only ever reads
# `kind`/`path` out of it today (see that function's own docstring).
# Pinning a strict pydantic model here would mean this route needs a
# matching edit every time the frontend's ref model gains a field for
# a LATER step, for validation this layer doesn't actually need to do.
class CodeProposalCreateRequest(BaseModel):
    instruction: str
    refs: list[dict] = Field(default_factory=list)
    session_id: str | None = None


class ProposalFileDecision(BaseModel):
    path: str
    # Validated against eo/code_proposals.py's own _VALID_DECISIONS
    # inside resolve_proposal(), not here — same "the eo layer owns
    # the ValueError for its own closed sets" pattern every other
    # route in this codebase already follows (see e.g.
    # api/routes/code.py's RestoreVersionRequest, which doesn't
    # pre-validate `version` either).
    decision: str
    final_content: str | None = None


class CodeProposalResolveRequest(BaseModel):
    files: list[ProposalFileDecision]


@router.post("/api/workspaces/{ws_id}/code/proposals", dependencies=[Depends(require_auth)])
def create_code_proposal(
    ws_id: str, req: CodeProposalCreateRequest, owner_id: str = Depends(require_auth)
):
    """Calls the (today: stub — see eo/code_proposals.py's
    _stub_generate_edit() docstring, and W5.2 for its real
    replacement) edit generator SYNCHRONOUSLY and returns the stored
    proposal — no separate poll needed for this step's own "Done when"
    (create -> get -> resolve via curl), though W5.4's Pusher-driven
    proposal card is still the intended long-term UX once a real
    (slower) agent call sits behind this in W5.2.

    A bad request shape (empty instruction, no refs, an unsupported or
    folder ref kind) is a 400. A generation FAILURE is not — see
    create_proposal()'s own docstring for why that still returns 200
    with a status='failed' proposal body instead."""
    _require_workspace(ws_id, owner_id)
    try:
        return code_proposals.create_proposal(
            ws_id, req.instruction, req.refs, req.session_id, owner_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/workspaces/{ws_id}/code/proposals", dependencies=[Depends(require_auth)])
def list_code_proposals(
    ws_id: str, status: str | None = None, owner_id: str = Depends(require_auth)
):
    """`?status=pending` is W5.4's pending tray's own call shape
    ("the tray is the source of truth... populated from GET
    .../code/proposals?status=pending on mount"); omitting `status`
    lists every proposal regardless of status, most recent first."""
    _require_workspace(ws_id, owner_id)
    try:
        return code_proposals.list_proposals(ws_id, status=status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/api/workspaces/{ws_id}/code/proposals/{proposal_id}",
    dependencies=[Depends(require_auth)],
)
def get_code_proposal(ws_id: str, proposal_id: str, owner_id: str = Depends(require_auth)):
    """Full shape, files[] (original + proposed per file) included —
    what W5.4's "reopen the tab -> Review restores the diff" needs.
    404 when the proposal doesn't exist in THIS workspace, same
    "stranger's guess never even confirms the id is real elsewhere"
    posture every other 404 in this file already gives."""
    _require_workspace(ws_id, owner_id)
    try:
        return code_proposals.get_proposal(ws_id, proposal_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown proposal_id")


@router.post(
    "/api/workspaces/{ws_id}/code/proposals/{proposal_id}/resolve",
    dependencies=[Depends(require_auth)],
)
def resolve_code_proposal(
    ws_id: str, proposal_id: str, req: CodeProposalResolveRequest,
    owner_id: str = Depends(require_auth),
):
    """The only route in this codebase that turns a proposal's stored
    `proposed` text into a real file write — see
    code_proposals.resolve_proposal()'s own docstring for the full
    two-phase base_version re-check this delegates to. 409 (not 400)
    on a ProposalStaleError, body = {"stale_files": [...]} — same
    "everything the client needs to offer Reload/Keep-mine/Compare per
    file, without a second round trip" shape W1.1's plain-Save 409
    already gives for a single file, just carrying a list here instead
    of one file. A ValueError (already-resolved proposal, or a bad
    `decision` value) is a plain 400: the proposal and the route both
    exist, it's the request against them that's invalid."""
    _require_workspace(ws_id, owner_id)
    decisions = [d.model_dump() for d in req.files]
    try:
        return code_proposals.resolve_proposal(ws_id, proposal_id, decisions, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown proposal_id")
    except ProposalStaleError as e:
        raise HTTPException(status_code=409, detail={"stale_files": e.stale_files})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
