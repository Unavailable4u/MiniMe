"""
api/routes/tasks.py

B6, piece 1 — task creation/preview/confirm, run-resume, the Role
Library, and Workflow Templates. Pulled out of api/server.py verbatim
(same functions, same error handling, same docstrings) — nothing here
changes behavior, this is a pure move.

This is also where CO3's new POST /api/task/{session_id}/pause and
CO5's new GET /api/task/{session_id}/stream land next — that's the
whole reason this piece was split out first, ahead of the rest of
server.py's other route groups.
"""
import traceback
from typing import Optional, Union

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth, _resolve_chat_or_404
from api.task_runner import run_task, preview_task, confirm_task, run_task_from_template
from eo import chat_store
from eo.executor import resume_graph
from eo.registry import (
    list_known_roles, get_role_metadata, update_role_prompt, set_role_pinned,
    list_role_metadata,
)
from eo.structure import (
    save_workflow_template, list_workflow_templates, delete_workflow_template,
    update_workflow_template,
)

router = APIRouter()


class AttachmentIn(BaseModel):
    # NEW — Data Layer §4a. Mirrors process_upload()'s own required
    # args (agents/source_manager.py) plus the two optional ingest_kwargs
    # "import" can take; every other kind ignores fmt/default_title, same
    # as process_upload() itself does.
    kind: str            # one of source_manager._INGEST_DISPATCH's keys:
                          # "pdf" | "import" | "voice" | "video" | "web_clip"
    payload: str          # local file path (already saved to disk by this
                          # request) or a url, depending on kind
    fmt: Optional[str] = None
    default_title: Optional[str] = None


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
    attachment: Optional[AttachmentIn] = None   # NEW — Data Layer §4a: a
    # file/url attached to THIS chat turn. Its mere presence bypasses
    # Inspector classification entirely and hires Source Manager (which,
    # per §3a, hires the Backlink Detector right after itself) — see
    # api/task_runner.py's _resolve_decision_and_hires() docstring.
    topic_id: Optional[str] = None   # Step 6.11.c/6.11.f: the Notebooks
    # topic (if any) this chat turn is scoped to, e.g. from a "Work
    # through: <step title>" click on a per-topic workflow step. As of
    # 6.11.f, forwarded to run_task() -> task_runner.py's
    # _grounded_task_text(), which splices that topic's own covered
    # sources + related notes into the turn's context — deterministic,
    # same "presence is the signal" posture `attachment` above uses,
    # but without short-circuiting routing/cache/SGA/classification the
    # way attachment does.


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


@router.post("/api/task", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task(req: TaskRequest, owner_id: str = Depends(require_auth)):   # FIXED — capture owner_id
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
            approval_roles=set(req.approval_roles) if req.approval_roles else None,
            owner_id=owner_id,   # FIXED — thread it down to run_task()
            attachment=req.attachment.dict() if req.attachment else None,   # NEW — Data Layer §4a
            topic_id=req.topic_id,   # NEW — Step 6.11.f: now actually consulted, not just logged
        )
    except Exception as exc:
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


@router.post("/api/task/preview", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task_preview(req: PreviewTaskRequest, owner_id: str = Depends(require_auth)):   # FIXED
    """... docstring unchanged ..."""
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
            owner_id=owner_id,   # FIXED
        )
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision={}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


@router.post("/api/task/confirm", response_model=TaskResponse)
def post_task_confirm(req: ConfirmTaskRequest, owner_id: str = Depends(require_auth)):
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
            owner_id=owner_id,
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


@router.post("/api/resume", response_model=ResumeResponse)
def post_resume(req: ResumeRequest, owner_id: str = Depends(require_auth)):
    """Part 2 §2.4: resumes a run paused at an approval_roles checkpoint.
    Mirrors post_task()'s error-handling shape (clean JSON on unexpected
    failure, real HTTP status codes for the specific, anticipated
    failure modes resume_graph() raises).

    Part 8.8 regression fix: session_id and chat_id are the same string
    everywhere in this system (see the comment above _resolve_chat_or_404),
    so the resuming caller's access is checked exactly the same way every
    other chat route checks it — owner or workspace collaborator, edit-tier
    required (approving/editing/rejecting a paused run is not a read-only
    action). Without this, any authenticated user who knew or guessed
    another user's session_id could resume/approve/edit their paused run;
    resume_graph() itself has no identity concept at all, so this check
    has to happen here, before it's ever called."""
    _resolve_chat_or_404(req.session_id, owner_id, require_edit=True)

    decision = {"action": req.action}
    if req.action == "edit":
        decision["text"] = req.text or ""

    try:
        result = resume_graph(req.session_id, decision)
    except KeyError:
        # NEW — B4: the one user-facing error path new since Phase CO —
        # worth a breadcrumb even though it's handled cleanly as a 404,
        # since a spike here usually means a client is resuming a stale/
        # expired session_id rather than an actual server bug.
        sentry_sdk.add_breadcrumb(
            category="resume_graph",
            message=f"No paused run for session_id={req.session_id!r}",
            level="warning",
        )
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


# ---------------------------------------------------------------------------
# Part 2 §2.7 — thin HTTP layer over eo/registry.py's Role Library (§2.2)
# and eo/structure.py's Workflow Templates (§2.3/§2.6). Both backing
# stores and their functions already existed; these five routes are the
# only thing that was actually missing before the frontend panels below
# could read or write real data.
# ---------------------------------------------------------------------------

@router.get("/api/roles")
def get_roles(owner_id: str = Depends(require_auth)):
    """Every role the system has ever briefed, metadata included — the
    Role Library panel's one data source. Shape: [{role, brief, source,
    updated_at, times_hired}, ...]. Uses list_role_metadata() for a
    single bulk read instead of list_known_roles()+get_role_metadata()
    per role, which was doing N+1 round-trips against the memory bus.

    owner_id (Part 8.3): always passed through to eo.registry now — it's
    only actually USED to select a per-user store if this deployment set
    ROLE_LIBRARY_SCOPE=per_user; with the default "global" scope every
    caller's owner_id is accepted but ignored (see eo/registry.py's
    _role_prompts_key()), so this route's behavior is unchanged for the
    common case."""
    return list_role_metadata(owner_id)


class UpdateRoleRequest(BaseModel):
    brief: str


@router.put("/api/roles/{role_name}")
def put_role(role_name: str, req: UpdateRoleRequest, owner_id: str = Depends(require_auth)):
    """Saves an inline Role Library edit. Always source="user_edited" —
    this is the one path that's allowed to claim that (see
    eo/registry.py's update_role_prompt() docstring)."""
    update_role_prompt(role_name, req.brief, source="user_edited", user_id=owner_id)
    return {"role": role_name, **(get_role_metadata(role_name, owner_id) or {})}


class SetRolePinnedRequest(BaseModel):
    pinned: bool


@router.patch("/api/roles/{role_name}/pin")
def patch_role_pinned(role_name: str, req: SetRolePinnedRequest, owner_id: str = Depends(require_auth)):
    """Pinned-roles feature — server-persisted so it syncs across
    devices, same store as everything else in the Role Library. Doesn't
    require the role to already have a brief; a role can be pinned from
    a picker before it's ever been hired."""
    entry = set_role_pinned(role_name, req.pinned, user_id=owner_id)
    return {"role": role_name, **entry}


class SaveWorkflowTemplateRequest(BaseModel):
    name: str
    roles: list   # role-name strings, or nested lists of them for a
                  # concurrent group (eo/structure.py §2.6) — validated
                  # by save_workflow_template() itself.
    description: str = ""
    domain_hint: Optional[str] = None
    approval_roles: Optional[list[str]] = None
    no_conversation_context_roles: Optional[list[str]] = None
    created_by: Optional[str] = None


@router.get("/api/workflow-templates", dependencies=[Depends(require_auth)])
def get_workflow_templates():
    """Every saved template, newest first — for the template picker and
    the Workflow Template builder's own list view."""
    return list_workflow_templates()


@router.get("/api/workflow-templates/{template_id}/chat")
def get_template_chat(template_id: str, owner_id: str = Depends(require_auth)):
    """The one chat this template already owns, if any — lets the
    frontend reuse it instead of minting a new chat on every run."""
    chat = chat_store.find_chat_for_template(owner_id, template_id)
    return chat or {}


@router.post("/api/workflow-templates", dependencies=[Depends(require_auth)])
def post_workflow_template(req: SaveWorkflowTemplateRequest):
    """Covers both write paths the design calls for: "save from a
    finished run" (caller passes that run's own execution_order as
    `roles`) and "build from scratch" (caller passes a list assembled in
    the Role Library UI) — both are just a plain roles list here."""
    try:
        return save_workflow_template(
            name=req.name,
            roles=req.roles,
            description=req.description,
            domain_hint=req.domain_hint,
            approval_roles=req.approval_roles,
            no_conversation_context_roles=req.no_conversation_context_roles,
            created_by=req.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/api/workflow-templates/{template_id}", dependencies=[Depends(require_auth)])
def put_workflow_template(template_id: str, req: SaveWorkflowTemplateRequest):
    """Template editing — there was previously no update path at all,
    only save (create) and delete."""
    updated = update_workflow_template(
        template_id, name=req.name, roles=req.roles, description=req.description,
        domain_hint=req.domain_hint, approval_roles=req.approval_roles,
        no_conversation_context_roles=req.no_conversation_context_roles,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown template_id={template_id!r}")
    return updated


@router.delete("/api/workflow-templates/{template_id}", dependencies=[Depends(require_auth)])
def delete_workflow_template_endpoint(template_id: str):
    deleted = delete_workflow_template(template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Unknown template_id={template_id!r}")
    return {"status": "deleted", "template_id": template_id}


class RunFromTemplateRequest(BaseModel):
    template_id: str
    task_text: str
    session_id: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None


@router.post("/api/task/from-template", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task_from_template(req: RunFromTemplateRequest, owner_id: str = Depends(require_auth)):
    """Part 2 §2.3/§2.6 — starts a new task from a saved workflow
    template instead of running Inspector/Panel classification.
    Mirrors post_task()'s exact error-handling shape."""
    try:
        return run_task_from_template(
            template_id=req.template_id,
            task_text=req.task_text,
            session_id=req.session_id,
            mode=req.mode,
            project_unique_name=req.project_unique_name,
            owner_id=owner_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision={}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )
