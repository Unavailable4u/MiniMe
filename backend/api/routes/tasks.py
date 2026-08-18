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
import asyncio  # NEW — CO5 gap fix: paced replay of the already-synthesized answer in stream_answer()
import json   # NEW — B6 cleanup: _parse_fenced_json (get_tasks' Part 7 §7.3 integrations parse)
import re     # NEW — B6 cleanup: _parse_fenced_json
import traceback
from typing import Optional, Union

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.deps import require_auth, _resolve_chat_or_404, _verify_supabase_jwt   # NEW — CO5 Finding B
from api.task_runner import run_task, preview_task, confirm_task, run_task_from_template
from agents import deploy_config_writer as deploy_config_writer_agent   # NEW — B6 cleanup: get_tasks' Part 7 §7.5 deploy-status read
from agents import deploy_agent as deploy_agent_module                  # NEW — B6 cleanup: get_tasks' Part 7 §7.5 monitoring-status read

from eo import chat_store
from eo import chat_workspace   # NEW — B6 cleanup: get_tasks_for_workspace's ws_id -> chat_id resolution
from eo import timeline_node_blurbs   # NEW — CO4 patch 5
from eo.executor import resume_graph
from eo.registry import (
    get_role_metadata, update_role_prompt, set_role_pinned,
    list_role_metadata,
)
from eo.skill_library import list_skills   # NEW — Part 6 §E2, task 14, patch 4
from eo.structure import (
    save_workflow_template, list_workflow_templates, delete_workflow_template,
    update_workflow_template,
)
from memory.bus import read_many as bus_read_many, set_app_slug, KEYS, write, read, delete as bus_delete, read_stage_output_text   # NEW — B6 cleanup: Part 7 §7.2 memory-bus read; write is CO3's new pause_requested flag; read is CO5 Finding B's pending_synthesis lookup; bus_delete is CO5 Step 7 follow-up's cleanup of that same key; read_stage_output_text is Bug 7 (0b)'s new re-fetch route's full-text lookup

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
    scope: Optional[str] = None   # NEW — task 13d/13e: Sources sub-tab's
    # scope selector ("general" | "forum" | "news" | "hackernews").
    # Forwarded unchanged to run_task() -> ... -> execute_graph(), where
    # only web_researcher's dispatch branch reads it. None (the default
    # for every existing caller, and any non-research task) matches
    # today's behavior exactly.


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
            scope=req.scope,   # NEW — task 13d/13e
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


class PauseResponse(BaseModel):
    session_id: str
    status: str  # "pause_requested"


@router.post("/api/task/{session_id}/pause", response_model=PauseResponse)
def request_pause(session_id: str, owner_id: str = Depends(require_auth)):
    """CO3: on-demand pause for whatever's running right now, as opposed
    to the pre-existing approval_roles path (a role pre-listed at task
    start). Sets pause_requested:{session_id}; eo/executor.py's
    Human-in-the-loop pause point (widened this same patch) checks it at
    the SAME checkpoint it already checks approval_roles membership —
    after every role's agent_done, never mid-LLM-call — so this gives the
    identical clean-pause guarantee the existing path does, just on
    demand instead of pre-planned.

    Same access check as post_resume below: pausing is a write action
    against someone's run, so it needs owner/edit-collaborator auth, not
    just any authenticated user who happens to know the session_id.

    Fire-and-forget by design — this only sets a flag for a loop that's
    already running elsewhere to notice; it doesn't wait for the pause to
    actually land, so a 200 here means "requested," not "paused yet."
    The frontend should treat this as pending until it sees the run's
    status flip to "paused" (see MessageBubble.jsx / ChatSidebar.jsx).

    CO5 Step 7 fix: eo/executor.py's pause checkpoint only exists inside
    run_with_looping()'s role loop (see the "after every role's
    agent_done" note above) — it's never consulted by the /stream route.
    By the time pending_synthesis:{session_id} exists, task_runner.py's
    _run_tier3_hires() has already returned a non-paused `looped` result,
    which means every role has finished and there is no loop left
    running anywhere to notice pause_requested:{session_id}. Writing the
    flag at that point wouldn't pause anything currently in flight; it
    would just sit on the bus (nothing consumes/deletes it the way the
    executor's checkpoint does) until this same session_id is reused for
    a *later* task, which would then pause on its very first role for a
    request nobody made. Reject the call instead of accepting a flag
    that can't do what its caller asked.
    """
    _resolve_chat_or_404(session_id, owner_id, require_edit=True)
    if read(f"pending_synthesis:{session_id}", default=None) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This run already finished role execution and is streaming "
                "its synthesized answer — there is no active role loop left "
                "to pause."
            ),
        )
    write(f"pause_requested:{session_id}", True)
    return PauseResponse(session_id=session_id, status="pause_requested")


@router.get("/api/task/{session_id}/stream")
async def stream_answer(session_id: str, token: str = Query(None)):
    """CO5 Finding B: auth for this route can't go through the usual
    Depends(require_auth) — that dependency reads the Authorization
    header off the request (api/deps.py), but the browser's EventSource
    API (Step 4's frontend hook) has no way to set custom headers on the
    request it makes. Same structural problem api/server.py's
    /ws/{session_id} WebSocket route already hit and solved: take the
    token as a query param instead, and verify it manually with the same
    _verify_supabase_jwt() the header path uses under the hood. Frontend
    implication (flagged for Step 4): the EventSource URL needs
    `?token=...` appended, not an Authorization header.

    401 is a real HTTP response here (unlike the WS route, which has no
    response body and has to fake a 401 via a 4401 close code) — this is
    a plain GET, so HTTPException works normally.

    Read access, not edit: require_edit=False, same reasoning as any
    other "just show me a result" GET in this file — a workspace
    viewer-tier collaborator should be able to watch an answer stream
    in, same as they could already read it once finished via the
    ordinary task-result routes. Contrast with request_pause/post_resume
    above, which mutate a run's state and correctly require edit access.

    Reads the pending_synthesis:{session_id} snapshot Finding A's patch
    writes in api/task_runner.py's _run_tier3_hires(), right after
    organize_final_answer() is called there. 404 if it's missing —
    either a bad/stale session_id, or a real one that never reached the
    multi-role synthesis branch (single-role tier-3 runs never write
    this key; see task_runner.py's own len(results) > 1 gate).

    CHANGED — CO5 gap fix (post-audit): this route used to call
    organize_final_answer_stream() itself, re-running the exact same
    synthesis prompt a second time against the exact same inputs
    task_runner.py's synchronous organize_final_answer() call had
    already run moments earlier to populate the POST response's
    `answer`/`dedup_notes`. Two problems with that: (1) it never bought
    the latency win CO5 was for, since the POST response already blocks
    on the full synthesis before the frontend can even open this
    EventSource — there's no "silent wait" left to eliminate by the
    time this route runs; (2) the two LLM calls aren't guaranteed to
    produce identical wording, so the text the user watched stream in
    could diverge from whatever `data.result.answer` persisted to chat
    history/export/copy.
    task_runner.py's snapshot now holds the already-computed
    `answer`/`dedup_notes` instead of the raw role_outputs, so there is
    nothing left to generate here — this route just replays that exact
    string as chunks, at a fixed pace, purely so the frontend's existing
    typewriter UI still has something to animate. One LLM call per
    request, and the streamed text is byte-for-byte identical to what
    got persisted, by construction (same string, not a second
    generation of it).
    """
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    owner_id = _verify_supabase_jwt(token)
    _resolve_chat_or_404(session_id, owner_id, require_edit=False)

    snapshot = read(f"pending_synthesis:{session_id}", default=None)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail="No pending synthesis for this session_id (unknown session, "
                   "or a single-role run with nothing to stream-synthesize).",
        )

    # NEW — CO5 gap fix: chunk size / pacing for the simulated stream.
    # Word-sized chunks (rather than char-by-char) keep the SSE frame
    # count reasonable for a long answer while still reading as a live
    # typewriter effect in the frontend; the short sleep is what makes
    # it visibly incremental instead of one giant flush.
    _CHUNK_WORDS = 6
    _CHUNK_DELAY_SECONDS = 0.05

    async def event_generator():
        answer = snapshot.get("answer") or ""
        dedup_notes = snapshot.get("dedup_notes") or {}

        words = answer.split(" ")
        for i in range(0, len(words), _CHUNK_WORDS):
            piece = " ".join(words[i:i + _CHUNK_WORDS])
            # Re-attach the separating space except before the very
            # first piece, so the reassembled text matches `answer`
            # exactly (join()ing chunks back together must reproduce
            # the original string byte-for-byte).
            chunk = piece if i == 0 else " " + piece
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
            await asyncio.sleep(_CHUNK_DELAY_SECONDS)

        # NEW — CO5 Step 7 follow-up: the snapshot has now been fully
        # consumed. Delete it here, same "consumer deletes on its way
        # out" pattern resume_graph() uses for
        # paused_execution:{session_id} -- otherwise this key just sits
        # in the bus until task_runner.py's ex=3600 backstop eventually
        # expires it. A dropped connection mid-stream skips this line
        # (the loop above never finishes, so execution never reaches
        # here) and falls back to that same TTL, rather than deleting a
        # snapshot a retried request might still want.
        bus_delete(f"pending_synthesis:{session_id}")

        yield f"data: {json.dumps({'dedup_notes': dedup_notes})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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

    # Bug fix 2026-08-12: MessageBubble.jsx's streamEnabled gate only
    # looks at shape -- tier 3, a session_id, and
    # Object.keys(data.result.output).length > 1 -- to decide whether to
    # open GET /api/task/{session_id}/stream. A resumed run satisfies all
    # three exactly the same way a run that never paused does (`result`
    # here has the identical role-keyed shape as task_runner.py's
    # `results`). But this code path never wrote
    # pending_synthesis:{session_id} the way task_runner.py's own
    # len(results) > 1 branch does after a normal run -- so stream_answer()
    # finds no snapshot and 404s EVERY time on a resumed multi-role run,
    # unconditionally, regardless of anything about that particular
    # session. That's the 404 the frontend Network tab captured: not a
    # missing/renamed route (the route resolves fine -- see its own
    # docstring's "404 if [the snapshot is] missing" branch), just this
    # path's write being the missing half of the pair task_runner.py's
    # normal-run path already has.
    #
    # Deliberately NOT re-running task_runner.py's organize_final_answer()
    # multi-role synthesis here: that needs the original task_text, which
    # isn't available at this layer without reaching into resume_graph()'s
    # internal paused_execution snapshot (and, for a macro-loop resume,
    # resume_graph() can return a differently-shaped {"results",
    # "final_role"} envelope rather than a flat role-keyed dict -- a
    # separate discrepancy from this one, not safe to paper over here).
    # Snapshotting the exact `answer` this response already computed keeps
    # the fix minimal and keeps the stream byte-for-byte identical to
    # what's returned/persisted, the same guarantee task_runner.py's own
    # snapshot-write comment describes -- it just replays this route's
    # existing answer instead of generating a different, merged one.
    dedup_notes = {}
    if len(result) > 1:
        write(f"pending_synthesis:{req.session_id}", {
            "answer": answer,
            "dedup_notes": dedup_notes,
        }, ex=3600)

    return ResumeResponse(
        session_id=req.session_id,
        status="ok",
        result={"output": result, "answer": answer, "final_role": final_role, "dedup_notes": dedup_notes},
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


# Part 6 §E2, task 14, patch 4 (optional/nice-to-have per that patch's
# own note — E2's own description is agent-facing, not human-facing;
# this exists for browsing what the self-improvement loop has learned,
# not because any panel currently reads it). Thin read-only mirror of
# the Role Library's own GET /api/roles immediately above, over
# eo/skill_library.py's store instead of eo/registry.py's.
@router.get("/api/skills")
def get_skills(owner_id: str = Depends(require_auth)):
    """Every skill in the library — hand-written SKILL_SEED entries and
    anything eo/skill_library.py's ensure_skill_for_task() has since
    written via the self-improvement loop. Shape: [{skill_id, title,
    doc, source, updated_at, times_matched}, ...], sorted by title —
    same "one bulk read, not N+1" shape list_role_metadata() already
    uses for /api/roles.

    owner_id: accepted for parity with require_auth on every other
    route in this file, but unused here — unlike Role Library's
    optional ROLE_LIBRARY_SCOPE=per_user mode, eo/skill_library.py's
    registry:skill_library key is always a single global store (skills
    are a property of the system, not any one user or project — same
    reasoning eo/skill_library.py's own module docstring gives for its
    `registry:` key prefix).
    """
    skills = list_skills()
    return sorted(
        ({"skill_id": skill_id, **entry} for skill_id, entry in skills.items()),
        key=lambda s: s["title"],
    )


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
    scope: Optional[str] = None   # NEW — task 13e: closes the gap flagged
    # in 13d — without this, a template built around web_researcher
    # silently fell back to "general" on every dispatch through this
    # path. Same semantics as TaskRequest.scope above: forwarded
    # unchanged to run_task_from_template() -> _dispatch_resolved(),
    # None for every existing template/caller.


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
            scope=req.scope,   # NEW — task 13e
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        traceback.print_exc()
        return TaskResponse(
            decision={}, tier=-1, status="error", result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# B6 piece 6 cleanup — /api/tasks/{session_id} and /api/tasks/workspace/
# {ws_id} were named in piece 1's original scope (they read the same
# current_plan/feature_status/module_specs data as everything else in
# this file) but never actually got moved out of api/server.py when
# pieces 1-2 landed. Folded in here, alongside piece 6, rather than left
# stranded any longer. _parse_fenced_json and _sentry_status come with
# them since get_tasks() is their only caller inside this file;
# _parse_marketplace_reviews stays in api/server.py (piece 7 territory —
# it's only used by /api/workspaces/{ws_id}/simulate, which hasn't moved
# yet) and now imports _parse_fenced_json from here instead of having
# its own copy, since both parsers share the same strip-the-fence-then-
# json.loads shape and there's no reason to fork it.
# ---------------------------------------------------------------------------

def _parse_fenced_json(text):
    """integration_flagger (Part 7 §7.3) is a generic_worker role, so its
    output lands in stage_output:* as plain text -- a strict fenced
    ```json code block per its ROLE_PROMPTS_SEED brief, not real
    structured output the way a REAL_ACTION_ROLES module's return value
    would be. Same strip-the-fence approach agents/prompt_writer.py and
    agents/idea_planner.py already use on their own raw LLM text before
    json.loads(), just tolerant of surrounding prose since a
    generic_worker role's brief-enforced discipline is never as airtight
    as a dedicated module's own parsing. Returns [] (not None) on
    anything unparseable, so the checklist UI can render "no integrations
    flagged yet" rather than an error state for a role that hasn't run.
    """
    if not text:
        return []
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1) if match else text
    try:
        parsed = json.loads(raw.strip())
        return parsed.get("integrations", []) if isinstance(parsed, dict) else []
    except (json.JSONDecodeError, AttributeError):
        return []



def _sentry_status(module_specs: dict, submitted_code: dict) -> str:
    """Part 7 §7.5. Three states, not a bare yes/no, so the monitoring
    widget can be honest about where things stand:
      - "not_planned"  -- integration_flagger hasn't flagged monitoring
                          yet, or prompt_writer.py hasn't run this cycle
      - "planned"      -- monitoring_setup is in this cycle's
                          module_specs, but code_writers.py hasn't
                          generated it yet
      - "configured"   -- it's in submitted_code, i.e. real code exists

    "monitoring_setup" is agents/prompt_writer.py's own
    MONITORING_MODULE_NAME constant; matched here by the same literal
    string rather than importing it, same "agents/ and api/ don't share
    private internals across the layer boundary" reasoning
    agents/deploy_config_writer.py's own docstring already gives for
    duplicating structure_architect.py's _get_project_tree() instead of
    importing it.
    """
    names = {
        (m.get("name") or "").strip().lower()
        for m in (module_specs.get("modules") or [])
    }
    if "monitoring_setup" not in names:
        return "not_planned"
    if "monitoring_setup" in (submitted_code or {}):
        return "configured"
    return "planned"


@router.get("/api/tasks/{session_id}", dependencies=[Depends(require_auth)])
def get_tasks(session_id: str):
    """Part 7 §7.2 — read-only kanban view over data idea_planner.py and
    prompt_writer.py already write every cycle. No new storage: this just
    exposes current_plan / feature_status / module_specs as one combined
    object.

    set_app_slug(session_id) scopes the read the same way every tier-3
    adaptive-path run already scopes its writes (see memory/bus.py's
    set_app_slug() docstring) -- without it, read_many() would fall back
    to whatever app_slug happens to be the persisted Redis global, which
    is exactly the cross-session collision Migration Part B fixed on the
    write side. This is the read-side equivalent of that same fix.

    Uses read_many() -- the same batched MGET helper
    eo/quota_sentinel.py's get_usage_history() already uses -- so this is
    one Redis round trip, not one-per-key.

    Part 7 §7.3 -- also reads integration_flagger's stage_output entry
    (cached once per session, never re-run per cycle, per that role's own
    seed brief) and parses its fenced ```json block into a plain
    "integrations" list for the checklist rendered alongside the board.

    Part 7 §7.5 addition -- also reads deploy_config_plan /
    last_deploy_config_summary / last_deploy_trigger_result (so the
    frontend's deploy button + status indicator has something to render
    without a second round trip) and derives monitoring status: Sentry
    from module_specs/submitted_code (see _sentry_status() above),
    UptimeRobot verbatim from last_uptimerobot_registration. One combined
    object, same "one call, not four" reasoning §7.2/§7.3 already used
    when they extended this same endpoint.
    """
    set_app_slug(session_id)
    data = bus_read_many(
        [KEYS["current_plan"], KEYS["feature_status"], KEYS["module_specs"],
         KEYS["submitted_code"],
         f"stage_output:{session_id}:integration_flagger",
         deploy_config_writer_agent.DEPLOY_CONFIG_PLAN_KEY,
         deploy_agent_module.LAST_DEPLOY_CONFIG_SUMMARY_KEY,
         "last_deploy_trigger_result",
         deploy_agent_module.LAST_UPTIMEROBOT_REGISTRATION_KEY],
        default=None,
    )
    module_specs = data[KEYS["module_specs"]] or {}
    submitted_code = data[KEYS["submitted_code"]] or {}
    return {
        "current_plan": data[KEYS["current_plan"]] or {},
        "feature_status": data[KEYS["feature_status"]] or {},
        "module_specs": module_specs,
        "integrations": _parse_fenced_json(data[f"stage_output:{session_id}:integration_flagger"]),
        "deploy_config_plan": data[deploy_config_writer_agent.DEPLOY_CONFIG_PLAN_KEY],
        "last_deploy_config_summary": data[deploy_agent_module.LAST_DEPLOY_CONFIG_SUMMARY_KEY],
        "last_deploy_trigger_result": data["last_deploy_trigger_result"],
        "monitoring": {
            "sentry_status": _sentry_status(module_specs, submitted_code),
            "uptimerobot": data[deploy_agent_module.LAST_UPTIMEROBOT_REGISTRATION_KEY],
        },
    }

@router.get("/api/task/{session_id}/step/{role}/full", dependencies=[Depends(require_auth)])
def get_step_full_result(session_id: str, role: str):
    """Bug 7 fix (0b), first half -- the re-fetch counterpart to
    relay/emitter.py's new `truncated: True` envelope flag.

    When an agent_done (or agent_token_chunk-derived) payload gets
    shrunk by _fit_event_to_pusher_cap() because it blew past Pusher's
    ~10KB cap, the frontend now sees `truncated: True` on that event and
    is expected to call this route instead of trusting the shrunk
    summary as the complete result (second half of 0b wires that call
    site up in SessionContext.jsx / WorkspaceDockContext.jsx).

    Reuses read_stage_output_text() -- the same shared helper
    memory/bus.py already exposes for reading a completed role's full
    text regardless of which of its two legitimate storage shapes
    (plain string vs. {"text": ...} dict from an approval edit) it's
    stored as -- rather than re-implementing that shape-handling here.

    set_app_slug(session_id) for the same cross-session-collision reason
    get_tasks() above already sets it before every stage_output read.

    Returns 404 if the role hasn't actually produced output for this
    session yet (still running, never ran, or genuinely empty) -- the
    frontend should keep the shrunk summary in that case rather than
    blanking the step out.
    """
    set_app_slug(session_id)
    text = read_stage_output_text(session_id, role)
    if text is None:
        raise HTTPException(
            status_code=404,
            detail=f"No stage output found for role {role!r} in session {session_id!r}",
        )
    return {"session_id": session_id, "role": role, "text": text}


@router.get("/api/tasks/workspace/{ws_id}", dependencies=[Depends(require_auth)])
def get_tasks_for_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    """§7 — Tasks scoped to a workspace instead of a raw chat session.
    Resolves ws_id -> a chat_id using the exact same "first chat_id, or
    create one" convention NotebooksTab/ResearchTab's handleOpenChat
    already established, then delegates to get_tasks()'s existing
    memory-bus read unchanged. current_plan/feature_status/etc. still
    live in the bus keyed by app_slug=session_id -- this route only
    changes what session_id gets resolved and passed in; nothing about
    how idea_planner.py or any other agent writes.

    Also stamps the resolved session_id back onto the response as
    "_session_id" -- TasksTab.jsx's DeployPanel/MonitoringWidget still
    call /api/deploy/{session_id}/... and /api/monitoring/{session_id}/...
    directly (those routes are unchanged, still session-keyed), so the
    frontend needs this id rather than re-deriving ws.chat_ids[0] itself,
    which could be stale on the very first call when no chat existed yet
    and one was just created here.
    """
    try:
        ws = chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    chat_ids = ws["chat_ids"]
    if chat_ids:
        session_id = chat_ids[0]
    else:
        created = chat_workspace.create_chat_in_workspace(
            ws_id, owner_id, title=f"{ws['name']} — Build"
        )
        session_id = created["id"]

    data = get_tasks(session_id)
    data["_session_id"] = session_id
    return data


# NEW — CO4 patch 5: short plain-language blurbs for the routing-trace
# timeline's node-click detail panel (RoutingTraceGraph.jsx). Global,
# not workspace-scoped -- see eo/timeline_node_blurbs.py's own
# docstring for why this isn't the same shape as
# get_node_summaries()/graph_and_notes.py's per-workspace store. Still
# behind require_auth like every other route here, purely so an
# unauthenticated caller can't hit it -- the content itself has
# nothing user- or workspace-specific in it.
@router.get("/api/timeline/node_blurbs", dependencies=[Depends(require_auth)])
def get_timeline_node_blurbs():
    return timeline_node_blurbs.get_blurbs()

