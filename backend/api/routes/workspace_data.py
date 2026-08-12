"""
api/routes/workspace_data.py

B6, piece 5 — workspace-scoped data sub-resources that sit alongside
the core container (piece 4 / workspaces.py) but aren't part of it:
facts (+ device-spec, parts pricing, which are built on top of
workspace_facts.py), corrections/Patch Review, generic panel content,
progress tracking, PageSpeed content audit, workspace export/import,
and the Google Calendar integration. Pulled out of api/server.py
verbatim (same functions, same error handling, same docstrings) —
nothing here changes behavior, this is a pure move.

Two judgment calls worth flagging:

- The Google Calendar / third-party integrations block (/api/integrations*)
  is NOT actually ws_id-scoped — it's owner_id-scoped, same as the rest
  of this file's auth pattern, just not nested under /api/workspaces.
  It's grouped here per the original split plan ("Google Calendar
  integration" was explicitly called out for this piece) rather than
  left behind alone in server.py.
- /api/workspaces/{ws_id}/export, /import, and /export/files aren't
  named in the original scoping note either, but they're workspace-
  scoped sub-resources in the same sense as facts/panels/progress, so
  they came along rather than staying stranded in server.py.

Deliberately NOT included: the core /api/workspaces CRUD/members/votes/
attribution/audit routes (piece 4); graph edges, node summaries,
topics/graph, nodes, notes, backlinks, clusters (piece 6); notebooks/
podcast/video/table/simulate generation endpoints (piece 7).
"""
import os
import secrets
import urllib.parse
import zipfile
from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from api.deps import require_auth
from agents.exporter import export_artifact, SUPPORTED_FORMATS as EXPORTABLE_FORMATS
from agents.correction_locator import locate_correction
from agents.part_price_finder import find_price
from agents import calendar_agent
from agents.calendar_agent import IntegrationNotConnectedError
from agents import pagespeed_agent
from eo import chat_workspace
from eo import correction_candidates
from eo import integrations
from eo import panel_content
from eo import study_progress
from eo import workspace_facts
from graph.adapters import chat_to_artifact

router = APIRouter()

# NEW — Part 8.5: Google Calendar OAuth. Same "read from env, fail loud at
# the point of use if missing" convention as the Supabase vars in
# api/deps.py.
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
# e.g. "https://your-api-host/api/integrations/google_calendar/callback"

# Same NOTES_EXPORTS_DIR every notebooks export/podcast/video route in
# api/server.py writes to (backend/data/exports) — duplicated here rather
# than imported from server.py to avoid a circular import (server.py
# imports this router). One more os.path.dirname() than server.py's own
# definition since this file lives one directory deeper (api/routes/
# instead of api/).
NOTES_EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "exports",
)


class ImportWorkspaceDataRequest(BaseModel):
    manifest: dict   # the exact object returned by GET /api/workspaces/{ws_id}/export


class RefreshPricesRequest(BaseModel):
    parts: list[dict]        # each: {"id","name","category","qty", ...}
    force_refresh: bool = False


class ToggleInstructionStepRequest(BaseModel):
    done: bool


class WorkspaceFactsRequest(BaseModel):
    # Matches eo/workspace_facts.py's EMPTY_FACTS shape. All optional —
    # a settings-panel save can send just the fields it's touching;
    # set_facts() merges rather than requiring the full object every
    # time.
    brand_voice: Optional[str] = None
    target_user: Optional[str] = None
    tech_stack: Optional[list[str]] = None
    custom: Optional[dict[str, Any]] = None


class PanelContentRequest(BaseModel):
    content: str


class ProgressUpdateRequest(BaseModel):
    # Manual override body (step 6.5) — mirrors set_progress()'s optional,
    # independent args: pass only what you're changing. Both are optional
    # so a caller can e.g. edit notes without touching status.
    status: Optional[str] = None
    notes: Optional[str] = None


class SubmitCorrectionRequest(BaseModel):  # NEW — Data Layer architecture §8c
    text: str
    scope_node_id: Optional[str] = None  # None == "All files", same convention
                                          # get_packet()'s own scope arg uses


# --- Part 8.5: third-party integrations ------------------------------------
#
# Google Calendar is the first connector built; Gmail/Slack/Jira-Asana-
# Linear repeat this exact shape (eo/integrations.py's storage is already
# provider-agnostic) against a different base URL/payload. See
# eo/integrations.py and agents/calendar_agent.py.

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# In-memory state->user_id map for the OAuth redirect round-trip. Process-
# local, same class of gap Part 8.1 flagged for the old file-store locks —
# fine for a single server instance, but move this into Redis (via
# memory/bus.py, already connected) with a short TTL if this deployment
# ever runs multiple replicas behind a load balancer.
_oauth_state: dict[str, str] = {}


@router.get("/api/integrations")
def list_integrations(owner_id: str = Depends(require_auth)):
    """Everything this user has connected, for the frontend's
    integrations panel. Never returns tokens — see
    eo.integrations.list_connected()'s own docstring."""
    return integrations.list_connected(owner_id)


@router.get("/api/integrations/google_calendar/connect")
def connect_google_calendar(owner_id: str = Depends(require_auth)):
    """Returns the Google consent URL for the frontend to redirect the
    browser to. Doesn't redirect itself — this route is hit by frontend
    JS, not a real browser navigation, same as every other JSON endpoint
    in this file; only the callback below is a real browser redirect
    target."""
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Server misconfigured: Google OAuth env vars not set.")

    state = secrets.token_urlsafe(24)
    _oauth_state[state] = owner_id

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",   # required to get a refresh_token back
        "prompt": "consent",        # forces a refresh_token on every connect,
                                     # not just the first-ever consent — otherwise
                                     # a user who disconnects and reconnects gets
                                     # no refresh_token the second time.
        "state": state,
    }
    return {"auth_url": f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"}


@router.get("/api/integrations/google_calendar/callback")
def google_calendar_callback(code: str = Query(...), state: str = Query(...)):
    """Google redirects the user's browser here directly — this route is
    NOT behind require_auth, because the browser arrives via Google's own
    redirect, not an Authorization header. Identity instead comes from the
    state token minted in connect_google_calendar() above, which only
    that authenticated user's own browser could have received. An
    unknown/expired state is rejected outright."""
    owner_id = _oauth_state.pop(state, None)
    if not owner_id:
        raise HTTPException(status_code=400, detail="Unknown or expired OAuth state")

    resp = requests.post(GOOGLE_TOKEN_URL, data={
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {resp.text}")
    payload = resp.json()

    # account_label: which Google account this actually is, for the UI —
    # a second, cheap call, same "fetch identity via the token itself"
    # pattern _lookup_user_id_by_email uses the service-role key for.
    account_label = None
    try:
        userinfo = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
            timeout=10,
        )
        if userinfo.status_code == 200:
            account_label = userinfo.json().get("email")
    except Exception:
        pass  # cosmetic only — a missing label never blocks the connection itself

    integrations.save_credentials(
        owner_id, "google_calendar", payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        scope=payload.get("scope"),
        account_label=account_label,
    )
    # Redirect back into the app rather than returning raw JSON — this
    # endpoint is hit by a real browser navigation, unlike every other
    # route in this file.
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    return RedirectResponse(f"{frontend_url}/settings/integrations?connected=google_calendar")


@router.delete("/api/integrations/{provider}")
def disconnect_integration(provider: str, owner_id: str = Depends(require_auth)):
    integrations.disconnect(owner_id, provider)
    return {"provider": provider, "disconnected": True}


class CreateEventRequest(BaseModel):
    summary: str
    start: str              # RFC3339
    end: str                 # RFC3339
    description: str = ""
    location: str = ""


@router.get("/api/integrations/google_calendar/events")
def get_calendar_events(time_min: str = Query(...), time_max: str = Query(...),
                         owner_id: str = Depends(require_auth)):
    try:
        return calendar_agent.list_events(owner_id, time_min, time_max)
    except IntegrationNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this user")


@router.post("/api/integrations/google_calendar/events")
def post_calendar_event(req: CreateEventRequest, owner_id: str = Depends(require_auth)):
    try:
        return calendar_agent.create_event(
            owner_id, req.summary, req.start, req.end,
            description=req.description, location=req.location,
        )
    except IntegrationNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this user")


@router.delete("/api/integrations/google_calendar/events/{event_id}")
def delete_calendar_event(event_id: str, owner_id: str = Depends(require_auth)):
    try:
        return calendar_agent.delete_event(owner_id, event_id)
    except IntegrationNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this user")


@router.get("/api/workspaces/{ws_id}/export")
def export_workspace(ws_id: str, owner_id: str = Depends(require_auth)):
    """Part 8.7: any current member can export — a portable JSON backup
    of the CALLER's own chats in this workspace (never a collaborator's,
    see chat_workspace.export_workspace_data's docstring). Not a
    docx/pptx/etc. file — agents/exporter.py's format writers weren't in
    scope this session, so this is the JSON interchange format the
    restore path below actually consumes."""
    try:
        return chat_workspace.export_workspace_data(ws_id, owner_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")


@router.post("/api/workspaces/{ws_id}/import")
def import_workspace(ws_id: str, req: ImportWorkspaceDataRequest,
                      owner_id: str = Depends(require_auth)):
    """Part 8.7: restores a manifest's chats as new chats owned by the
    caller, attached to ws_id. Requires edit-tier+ access to ws_id."""
    try:
        return chat_workspace.import_workspace_data(ws_id, owner_id, req.manifest)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")
    except chat_workspace.WorkspaceAccessError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/api/workspaces/{ws_id}/export/files")
def export_workspace_files(ws_id: str, fmt: str = Query("md"),
                            owner_id: str = Depends(require_auth)):
    """Part 8.7 (file-format path): human-readable export of the
    caller's own chats in this workspace via agents/exporter.py, using
    graph/adapters.py's chat_to_artifact() to shape each chat into the
    {title, sections} artifact every exporter in that module already
    consumes — same "one adapter per domain, one exporter set total"
    discipline node_to_artifact/markdown_text_to_artifact already
    follow, just fed a chat instead of a node or raw Markdown.

    This is deliberately separate from GET /export (the JSON backup):
    that one preserves exact message structure for restore_chats() to
    replay losslessly; this one produces a real docx/pptx/pdf/md/csv/json
    file meant for a human to read, not for round-tripping back through
    import. A single chat downloads directly; more than one gets zipped
    (FileResponse can only serve one file per request)."""
    fmt = fmt.lower().lstrip(".")
    if fmt not in EXPORTABLE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported export format '{fmt}'. Supported: {', '.join(EXPORTABLE_FORMATS)}.",
        )
    try:
        manifest = chat_workspace.export_workspace_data(ws_id, owner_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "Unknown workspace_id")

    chats = manifest["chats"]
    if not chats:
        raise HTTPException(status_code=404, detail="No chats to export for this user in this workspace")

    paths = []
    for chat in chats:
        artifact = chat_to_artifact(chat)
        path = export_artifact(artifact, fmt, NOTES_EXPORTS_DIR)
        paths.append(path)

    if len(paths) == 1:
        return FileResponse(paths[0], filename=os.path.basename(paths[0]))

    zip_path = os.path.join(NOTES_EXPORTS_DIR, f"{ws_id}_export.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=os.path.basename(p))
    return FileResponse(zip_path, filename=os.path.basename(zip_path))


# --- workspace facts: tier-3 memory (see eo/workspace_facts.py, §0.3) ----
# The settings-panel-facing surface for "facts true across the whole
# project" — brand voice, target user, tech stack, plus a free-form
# `custom` bucket. Reading these into agent prompts happens automatically
# inside eo/conversation_memory.py; nothing here needs to be called at
# generation time, only when the user views/edits the panel.

@router.get("/api/workspaces/{ws_id}/facts")
def get_workspace_facts(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)  # 404s if the workspace doesn't exist / isn't owned
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return workspace_facts.get_facts(ws_id)


@router.put("/api/workspaces/{ws_id}/facts")
def put_workspace_facts(ws_id: str, req: WorkspaceFactsRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    # exclude_unset -> a save that only touched brand_voice doesn't wipe
    # target_user/tech_stack/custom back to empty.
    return workspace_facts.set_facts(ws_id, req.dict(exclude_unset=True))
@router.post("/api/workspaces/{ws_id}/parts/refresh-prices")
def refresh_part_prices(ws_id: str, req: RefreshPricesRequest,
                         owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    updated = []
    for part in req.parts:
        result = find_price(part["name"], force_refresh=req.force_refresh)
        listing = result["listings"][0] if result["listings"] else None
        updated.append({
            **part,
            "estimated_price_bdt": listing.get("price_bdt") if listing else None,
            "vendor_name": listing.get("vendor") if listing else None,
            "vendor_url": listing.get("url") if listing else None,
            "price_checked_at": result["checked_at"],
        })

    # Merge into the existing custom bucket rather than overwriting it —
    # `custom` already holds unrelated data (e.g. the UptimeRobot API key
    # from deploy_agent.py's set_uptimerobot_api_key()). Read-modify-write
    # at this level keeps that safe regardless of whether set_facts()
    # itself does a shallow or deep merge internally.
    facts = workspace_facts.get_facts(ws_id)
    custom = dict(facts.get("custom") or {})
    custom["parts"] = updated
    workspace_facts.set_facts(ws_id, {"custom": custom})
    workspace_facts.record_section_entries(
        ws_id,
        "hardware",
        [
            {
                "key": part.get("id") or part.get("name") or f"part_{index}",
                "title": part.get("name") or part.get("id") or f"Part {index + 1}",
                "summary": f"{part.get('category') or 'module'} ×{part.get('qty') or 1}",
                "data": part,
            }
            for index, part in enumerate(updated)
        ],
        source="refresh_part_prices",
        source_ref=ws_id,
        event="parts_refresh",
    )

    return {"parts": updated}

@router.get("/api/workspaces/{ws_id}/device-spec")
def get_device_spec(ws_id: str, owner_id: str = Depends(require_auth)):
    """Assembles agents/hardware_speccer.py's five sub-view slices back
    into one response -- they're stored as five separate
    workspace_facts.custom keys (parts/wiring/mech/instructions/info),
    not one blob, so BlueprintView's single fetch-per-workspace-select
    needs this endpoint to stitch them together rather than reading
    facts.custom directly and hoping all five keys exist. Returns
    empty-but-valid shapes for any key nothing has written yet (no
    device spec generated == every sub-view renders its own empty
    state, not a 404 for the whole page).

    "info" added T2b, step 19b: same empty-but-valid convention as the
    original four -- a spec generated before hardware_speccer.py's
    step 19a (_generate_info()) simply has no custom["info"] key yet,
    so this defaults to {"summary": "", "tags": [], "image_url": ""}
    rather than omitting the field or erroring, matching what a fresh,
    un-generated spec's "parts"/"wiring"/etc. already do above.
    "image_url" folded into that same default at step 19d (optional
    Pollinations.ai render) -- a spec written before 19d shipped has
    "summary"/"tags" but no "image_url" key, so this defaults it to ""
    the same empty-but-valid way rather than a KeyError downstream."""
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
 
    custom = workspace_facts.get_facts(ws_id).get("custom") or {}
    info = dict(custom.get("info") or {})
    info.setdefault("summary", "")
    info.setdefault("tags", [])
    info.setdefault("image_url", "")
    return {
        "parts": custom.get("parts", []),
        "wiring": custom.get("wiring", {"nodes": [], "edges": []}),
        "mech": custom.get("mech", {"enclosure": {"w": 0, "h": 0, "d": 0}, "placements": []}),
        "instructions": custom.get("instructions", {"phases": []}),
        "info": info,
    }
 
 
@router.patch("/api/workspaces/{ws_id}/device-spec/instructions/steps/{step_id}")
def toggle_instruction_step(ws_id: str, step_id: str, req: ToggleInstructionStepRequest,
                             owner_id: str = Depends(require_auth)):
    """Instructions is the only Blueprint sub-view with mutable state
    (Blueprint design guide §5) -- everything else here is regenerated
    wholesale by agents/hardware_speccer.py, so only this route needs a
    read-modify-write-a-single-step shape rather than a full-object PUT.
    Same custom-dict merge discipline as refresh_part_prices() above:
    read the whole facts object, touch only custom["instructions"], write
    the whole object back, so an in-flight price refresh and a step
    toggle can't clobber each other's key."""
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
 
    facts = workspace_facts.get_facts(ws_id)
    custom = dict(facts.get("custom") or {})
    instructions = custom.get("instructions") or {"phases": []}
 
    found = False
    for phase in instructions.get("phases", []):
        for step in phase.get("steps", []):
            if step["id"] == step_id:
                step["done"] = req.done
                found = True
    if not found:
        raise HTTPException(status_code=404, detail="Unknown step_id")
 
    custom["instructions"] = instructions
    workspace_facts.set_facts(ws_id, {"custom": custom})
    workspace_facts.record_section_entries(
        ws_id,
        "instructions",
        [
            {
                "key": phase.get("id") or phase.get("name") or f"phase_{phase_index}",
                "title": phase.get("name") or phase.get("id") or f"Phase {phase_index + 1}",
                "summary": f"{len(phase.get('steps', []))} step(s)",
                "data": phase,
            }
            for phase_index, phase in enumerate(instructions.get("phases", []))
        ],
        source="toggle_instruction_step",
        source_ref=step_id,
        event="instruction_step",
    )
    return {"status": "ok", "instructions": instructions}
 
@router.get("/api/workspaces/{ws_id}/facts/candidates", dependencies=[Depends(require_auth)])
def get_workspace_fact_candidates(ws_id: str):
    """Agent-proposed facts awaiting user accept/reject — see
    workspace_facts.propose_fact()."""
    return workspace_facts.list_candidates(ws_id)


@router.post("/api/workspaces/{ws_id}/facts/candidates/{candidate_id}/accept", dependencies=[Depends(require_auth)])
def accept_workspace_fact_candidate(ws_id: str, candidate_id: str):
    # FIX — bug audit §9 (candidates accept/reject write path): was
    # `{index}: int`, addressed by list position. Two reviewers can be
    # looking at the same pending list at once (Part 8.4's notification
    # fan-out), so an index can silently point at a different candidate
    # than the one the user actually clicked accept/reject on by the
    # time the request lands. Same fix as the notes candidates route
    # below and eo/workspace_facts.py's accept_candidate/reject_candidate.
    try:
        return workspace_facts.accept_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")


@router.delete("/api/workspaces/{ws_id}/facts/candidates/{candidate_id}", dependencies=[Depends(require_auth)])
def reject_workspace_fact_candidate(ws_id: str, candidate_id: str):
    try:
        workspace_facts.reject_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")
    return {"status": "rejected", "candidate_id": candidate_id}

# --- Corrections + Patch Review (Data Layer architecture §8c) -----------
# §8a's Corrections tab captures a plain-language correction (scope +
# text) and posts it here. This is the wiring §8b's
# agents/correction_locator.py never got on its own: locate a candidate
# edit, and if one comes back, hand it to
# eo/correction_candidates.py's pending store for the Patch Review tab
# below to render as a before/after and let the person accept/reject.
# A located-but-empty result (no matching topic, or the source didn't
# support the correction) never reaches the pending store at all --
# there's nothing to review, so the reason goes straight back to the
# Corrections tab to show inline instead.

@router.post("/api/workspaces/{ws_id}/corrections", dependencies=[Depends(require_auth)])
def submit_correction(ws_id: str, body: SubmitCorrectionRequest):
    scope_node_ids = {body.scope_node_id} if body.scope_node_id else None
    scope_label = "All files" if body.scope_node_id is None else body.scope_node_id
    result = locate_correction(ws_id, body.text, scope_node_ids=scope_node_ids)

    if not result.get("op"):
        return {"status": "no_match", "reason": result.get("reason") or "couldn't locate this correction"}

    candidate = correction_candidates.propose_candidate(
        ws_id, body.text, scope_label, result["topic_id"], result["op"],
    )
    return {"status": "queued", "candidate": candidate}


@router.get("/api/workspaces/{ws_id}/corrections/candidates", dependencies=[Depends(require_auth)])
def get_correction_candidates(ws_id: str):
    return correction_candidates.list_candidates(ws_id)


@router.post("/api/workspaces/{ws_id}/corrections/candidates/{candidate_id}/accept", dependencies=[Depends(require_auth)])
def accept_correction_candidate(ws_id: str, candidate_id: str):
    try:
        return correction_candidates.accept_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/workspaces/{ws_id}/corrections/candidates/{candidate_id}", dependencies=[Depends(require_auth)])
def reject_correction_candidate(ws_id: str, candidate_id: str):
    try:
        correction_candidates.reject_candidate(ws_id, candidate_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown candidate_id")
    return {"status": "rejected", "candidate_id": candidate_id}

# --- generic paste-panel content (see eo/panel_content.py) ---------------
# Same "gone on reload" fix as workspace facts, generalized to every
# paste-a-chat's-output-into-a-box panel: Mind Map, Study
# (flashcards/quiz/study guide), PRD, Architecture, Schema, API
# Contract, Devil's Advocate, Feasibility, Wireframes, Contradictions.

@router.get("/api/workspaces/{ws_id}/panels", dependencies=[Depends(require_auth)])
def list_workspace_panel_content(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    return panel_content.list_content(ws_id)


@router.get("/api/workspaces/{ws_id}/panels/{panel_key}", dependencies=[Depends(require_auth)])
def get_workspace_panel_content(ws_id: str, panel_key: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return panel_content.get_content(ws_id, panel_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/api/workspaces/{ws_id}/panels/{panel_key}", dependencies=[Depends(require_auth)])
def put_workspace_panel_content(ws_id: str, panel_key: str, req: PanelContentRequest,
                                 owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return panel_content.set_content(ws_id, panel_key, req.content, owner_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- mass progress-tracking (see eo/study_progress.py) -------------------
# The Not Started/Ongoing/Done board view (step 6.9) and per-topic status
# reads both go through here. Same ownership-gate-then-delegate shape as
# get_workspace_facts()/list_workspace_panel_content() above.

@router.get("/api/workspaces/{ws_id}/progress", dependencies=[Depends(require_auth)])
def get_workspace_progress(ws_id: str, topic_id: Optional[str] = Query(None),
                            owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    # No topic_id -> whole board (every topic touched so far, step 6.9's
    # board view). With topic_id -> single record, defaulted to
    # "not_started" by study_progress.get_progress() itself if the topic
    # has never been written.
    return study_progress.get_progress(ws_id, topic_id)


@router.put("/api/workspaces/{ws_id}/progress", dependencies=[Depends(require_auth)])
def put_workspace_progress(ws_id: str, topic_id: str, req: ProgressUpdateRequest,
                            owner_id: str = Depends(require_auth)):
    """Manual override (step 6.5) — a person correcting the board directly,
    distinct from the automatic hooks (steps 6.6/6.7) that call
    set_progress() off workflow/quiz events. topic_id is a query param
    (not part of the body) since PUT targets one topic's record within
    the workspace-scoped path, matching the GET route's query-param
    topic_id above rather than nesting it into the path itself.
    """
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return study_progress.set_progress(
            ws_id, topic_id, status=req.status, notes=req.notes
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- content audit: PageSpeed Insights (see agents/pagespeed_agent.py) --
# Live-fetched, not persisted — same "fetch fresh on load/refresh, no
# backing store" pattern GrowthTab's CalendarView already uses for
# Google Calendar events, not the panel_content paste-and-save pattern.
# ws_id is only used for the same ownership gate every workspace-scoped
# route already applies; the audit itself isn't workspace-specific data.

@router.get("/api/workspaces/{ws_id}/audit/pagespeed", dependencies=[Depends(require_auth)])
def get_pagespeed_audit(ws_id: str, url: str = Query(...), strategy: str = Query("mobile"),
                         owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        return pagespeed_agent.run_audit(url, strategy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except pagespeed_agent.PageSpeedError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
