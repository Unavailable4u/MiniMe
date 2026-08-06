"""
api/routes/workspaces.py

B6, piece 4 — core /api/workspaces: CRUD, chat attach/detach, membership,
ownership transitions, voting, attribution, and audit. Pulled out of
api/server.py verbatim (same functions, same error handling, same
docstrings) — nothing here changes behavior, this is a pure move.

This is the single largest domain and the one most likely to have deep
shared auth/permission logic (chat_workspace.py's owner/partner/editor/
viewer/moderator resolution underlies almost every route here) — budget
real review time against this file, not just a diff skim.

Deliberately NOT included here, even though they're workspace-scoped:
facts, corrections, panels, progress, and the Google Calendar
integration (piece 5 / workspace_data.py); graph edges, node summaries,
topics/graph, nodes, notes, backlinks, clusters (piece 6 /
graph_and_notes.py); and notebooks/podcast/video/table/simulate
generation endpoints (piece 7 / notebooks.py). This module is the core
container object plus who-can-do-what-to-it; everything workspace_id
scopes but doesn't govern access to lives elsewhere.

GET /api/audit/me is included even though it isn't under /api/workspaces
-- it shares audit_log with GET /api/workspaces/{ws_id}/audit immediately
above it in the original file, and doesn't fit any other piece.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import require_auth, _lookup_user_id_by_email, _lookup_users_by_ids
from eo import chat_workspace
from eo import audit_log

router = APIRouter()


class CreateWorkspaceRequest(BaseModel):
    name: str
    # NEW — item #10 / B0: lets a caller specify which stage tab this
    # project should natively belong to. Omitted = old behavior ("note").
    stage: Optional[str] = None


class RenameWorkspaceRequest(BaseModel):
    name: str


class PromoteWorkspaceRequest(BaseModel):
    to_stage: Optional[str] = None
    # NEW — §2.2: "complete" (default) is today's unchanged behavior —
    # workspace leaves the old tab entirely. "partial" keeps it active
    # in both the old and new tab (see chat_workspace.promote()).
    mode: Optional[str] = "complete"
    # NEW — §10d: when set, this request is understood to be the
    # chat-triggered path (a person explicitly agreeing, mid chat-turn,
    # to add a tab) rather than a plain settings-panel promote. Only
    # meaningful together with mode="partial" — see promote_workspace()
    # below for the branch this drives.
    session_id: Optional[str] = None


class WorkspaceChatRequest(BaseModel):
    chat_id: str
    delete_chat: Optional[bool] = False


class CreateWorkspaceChatRequest(BaseModel):
    title: Optional[str] = "New Chat"


class AddWorkspaceMemberRequest(BaseModel):
    email: str
    role: str = "viewer"  # 'viewer' or 'editor'


class UpdateWorkspaceMemberRequest(BaseModel):
    role: str  # 'viewer', 'editor', 'moderator', or 'partner'


class LeaveWorkspaceRequest(BaseModel):
    successor_id: Optional[str] = None  # owner-only; must be a current partner


class CastVoteRequest(BaseModel):
    vote_target: Optional[str] = None  # another partner's user_id, or None = "stay joint"


class SetAttributionRequest(BaseModel):
    show: bool


class AttributionGrantRequest(BaseModel):
    can_toggle: bool


# --- workspaces: named containers with auto-linking membership (§7) ------
# UI label is "Projects" — named chat_workspace.py / /api/workspaces in
# code to avoid colliding with eo/project_registry.py, which tracks
# external codebase roots for Cross-Project File Control (unrelated
# concept, same word).
#
# Part 8.3: `owner_id` below is what require_auth's dependency-injected
# param has always been called — for these routes it now really means
# "the acting user's id", which may be the workspace's real owner OR a
# collaborator (viewer/editor). chat_workspace.py's functions resolve
# actual access themselves; these routes just map its exceptions to the
# right HTTP status: FileNotFoundError -> 404 (no access at all, same
# opacity as "doesn't exist"), WorkspaceAccessError -> 403 (some access,
# not enough for this action).

@router.get("/api/workspaces")
def get_workspaces(owner_id: str = Depends(require_auth)):
    return chat_workspace.list_workspaces(owner_id)


@router.post("/api/workspaces")
def create_workspace(req: CreateWorkspaceRequest, owner_id: str = Depends(require_auth)):
    return chat_workspace.create_workspace(owner_id, req.name, stage=req.stage or "note")


@router.get("/api/workspaces/{ws_id}")
def get_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@router.patch("/api/workspaces/{ws_id}/rename")
def rename_workspace(ws_id: str, req: RenameWorkspaceRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.rename_workspace(ws_id, owner_id, req.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
@router.post("/api/workspaces/{ws_id}/promote")
def promote_workspace(ws_id: str, req: PromoteWorkspaceRequest, owner_id: str = Depends(require_auth)):
    try:
        # §10d: session_id present + mode="partial" is the chat-triggered
        # path — adds the shared active_stages_precheck() (§10b)
        # short-circuit ahead of the mutation, and fires notify() on an
        # actual promote so the person's own open chat tab reflects the
        # new tab immediately. Every other combination (no session_id, or
        # mode="complete") is today's unchanged settings-panel behavior.
        if req.session_id and (req.mode or "complete") == "partial":
            if not req.to_stage:
                raise HTTPException(
                    status_code=400,
                    detail="to_stage is required for a chat-triggered partial promote",
                )
            result = chat_workspace.chat_triggered_partial_promote(
                ws_id, owner_id, req.to_stage, session_id=req.session_id,
            )
            if result is None:
                # Precheck said not eligible (already active in to_stage,
                # unknown stage, etc.) — an expected no-op, not an error.
                return {"promoted": False, "workspace_id": ws_id, "to_stage": req.to_stage}
            return {"promoted": True, **result}
        return chat_workspace.promote(ws_id, owner_id, req.to_stage, mode=req.mode or "complete")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/workspaces/{ws_id}/chats")
def add_workspace_chat(ws_id: str, req: WorkspaceChatRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.add_chat(ws_id, owner_id, req.chat_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/api/workspaces/{ws_id}/chats/create")
def create_workspace_chat(ws_id: str, req: CreateWorkspaceChatRequest,
                           owner_id: str = Depends(require_auth)):
    """One-step version of create-then-attach — a collaborator (owner or
    editor) creates a brand-new chat that's immediately part of this
    workspace, instead of two round trips. Same access rules as
    add_workspace_chat: requires edit access to ws_id."""
    try:
        return chat_workspace.create_chat_in_workspace(ws_id, owner_id, title=req.title or "New Chat")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete("/api/workspaces/{ws_id}/chats/{chat_id}")
def remove_workspace_chat(ws_id: str, chat_id: str, delete_chat: bool = Query(False),
                           owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.remove_chat(ws_id, owner_id, chat_id, delete_chat=delete_chat)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete("/api/workspaces/{ws_id}")
def delete_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.delete_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "deleted", "id": ws_id}


# --- Part 8.3: workspace membership (owner-only) --------------------------

@router.get("/api/workspaces/{ws_id}/members")
def get_workspace_members(ws_id: str, owner_id: str = Depends(require_auth)):
    """Returns one flat array, owner first (if any), then
    workspace_members rows — the frontend renders this uniformly rather
    than special-casing the owner, even though the owner isn't actually
    a workspace_members row in the database (see chat_workspace.py).
    Each entry is enriched with email/name/avatar_url via the Admin API
    so the UI never has to show a raw user_id."""
    try:
        members = chat_workspace.list_members(ws_id, owner_id)
        ws = chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))

    all_ids = {m["user_id"] for m in members}
    if ws["owner_id"]:
        all_ids.add(ws["owner_id"])
    profiles = _lookup_users_by_ids(all_ids)

    def _enrich(uid: str, extra: dict) -> dict:
        p = profiles.get(uid, {})
        return {
            "user_id": uid,
            "email": p.get("email"),
            "name": p.get("name"),
            "avatar_url": p.get("avatar_url"),
            **extra,
        }

    result = []
    if ws["owner_id"]:
        result.append(_enrich(ws["owner_id"], {
            "role": "owner", "can_toggle_attribution": True, "added_at": None,
        }))
    for m in members:
        result.append(_enrich(m["user_id"], {
            "role": m["role"],
            "can_toggle_attribution": m["can_toggle_attribution"],
            "added_at": m["added_at"],
        }))
    return result


@router.post("/api/workspaces/{ws_id}/members")
def add_workspace_member(ws_id: str, req: AddWorkspaceMemberRequest, owner_id: str = Depends(require_auth)):
    target_user_id = _lookup_user_id_by_email(req.email)
    if not target_user_id:
        raise HTTPException(status_code=404, detail=f"No user found with email {req.email!r}")
    try:
        return chat_workspace.add_member(ws_id, owner_id, target_user_id, role=req.role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/api/workspaces/{ws_id}/members/{target_user_id}")
def update_workspace_member(ws_id: str, target_user_id: str, req: UpdateWorkspaceMemberRequest,
                             owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.update_member_role(ws_id, owner_id, target_user_id, req.role)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/workspaces/{ws_id}/members/{target_user_id}")
def remove_workspace_member(ws_id: str, target_user_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.remove_member(ws_id, owner_id, target_user_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "removed", "workspace_id": ws_id, "user_id": target_user_id}


# --- Part 8.4: ownership transitions, voting, attribution ------------------

@router.post("/api/workspaces/{ws_id}/leave")
def leave_workspace_endpoint(ws_id: str, req: LeaveWorkspaceRequest,
                              owner_id: str = Depends(require_auth)):
    """Any member (including the owner) can leave voluntarily. If the
    caller is the owner and names a successor, ownership transfers
    directly. If the owner names no successor, the workspace becomes
    joint. Non-owners just drop their own membership row —
    successor_id is ignored for them."""
    try:
        chat_workspace.leave_workspace(ws_id, owner_id, successor_id=req.successor_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "left", "workspace_id": ws_id, "user_id": owner_id}


@router.post("/api/workspaces/{ws_id}/owner/remove")
def remove_owner_endpoint(ws_id: str, owner_id: str = Depends(require_auth)):
    """Forced removal — caller must be a partner. Ejects the current
    owner with no successor choice and puts the workspace into joint
    state. Named 'owner_id' for consistency with every other route's
    Depends(require_auth) parameter, but here it's the ACTING PARTNER,
    not the workspace's owner — same overloaded-name convention noted
    in chat_workspace.py's Part 8.3 section."""
    try:
        return chat_workspace.remove_owner(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/workspaces/{ws_id}/votes")
def get_workspace_votes(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.get_vote_status(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@router.post("/api/workspaces/{ws_id}/votes")
def cast_workspace_vote(ws_id: str, req: CastVoteRequest, owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.cast_vote(ws_id, owner_id, req.vote_target)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/api/workspaces/{ws_id}/attribution")
def set_workspace_attribution(ws_id: str, req: SetAttributionRequest,
                               owner_id: str = Depends(require_auth)):
    try:
        return chat_workspace.set_show_attribution(ws_id, owner_id, req.show)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.patch("/api/workspaces/{ws_id}/members/{target_user_id}/attribution-grant")
def set_member_attribution_grant(ws_id: str, target_user_id: str, req: AttributionGrantRequest,
                                  owner_id: str = Depends(require_auth)):
    """Owner/partner-only: grant or revoke a specific moderator's right
    to toggle workspace-wide attribution visibility."""
    try:
        return chat_workspace.set_moderator_attribution_grant(
            ws_id, owner_id, target_user_id, req.can_toggle
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/workspaces/{ws_id}/audit")
def get_workspace_audit(ws_id: str, limit: int = Query(100, le=500),
                         owner_id: str = Depends(require_auth)):
    """Part 8.6: 'what happened to this workspace' — owner/partner-tier
    only, same restriction as delete_workspace/set_moderator_attribution_grant,
    since this surfaces every member add/remove/role-change and every
    ownership transition, not just the caller's own actions."""
    role = chat_workspace.member_role(ws_id, owner_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    if role not in ("owner", "partner"):
        raise HTTPException(
            status_code=403,
            detail=f"user {owner_id} must be an owner or partner of workspace {ws_id} to view its audit log",
        )
    return audit_log.list_for_target("workspace", ws_id, limit=limit)


@router.get("/api/audit/me")
def get_my_audit(limit: int = Query(100, le=500), owner_id: str = Depends(require_auth)):
    """Part 8.6: 'what have I done' — always self-scoped by the
    authenticated caller's own id, so no separate access check is
    needed beyond require_auth itself."""
    return audit_log.list_for_user(owner_id, limit=limit)
