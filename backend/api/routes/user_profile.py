"""
api/routes/user_profile.py — Patch B6, Profile Visibility (Settings
Panel). The read/edit/delete surface for eo/user_profile.py's
per-account behavior profile (Patch B1), so a person can see and
correct what's been silently inferred about them even though the
chat itself never mentions it unprompted (eo/user_profile.py's own
docstring: "adapt silently").

Deliberately NOT nested under /api/workspaces/{ws_id}/... the way
workspace_facts' settings routes are (api/routes/workspace_data.py) —
a user profile is owner_id-scoped, true across every workspace the
account touches, not scoped to any one project. require_auth already
resolves straight to owner_id with no workspace lookup needed, so
these routes are flatter than their workspace_facts siblings.

Three jobs, mirroring the three patches that built the module this
wraps:
  - GET  /api/profile                        — Patch B1's get_profile(),
    read-only, full shape.
  - PUT  /api/profile/output-format           — Patch B4's
    override_profile_fact("output_prefs", ...), the single-record field.
  - PUT  /api/profile/{field}/{key}           — same, for the bucketed
    fields (domains/likes/dislikes/error_patterns).
  - DELETE on both of the above               — Patch B6's own
    delete_profile_fact(), the "this doesn't apply to me at all"
    counterpart to override's "this guess is wrong."
  - GET  /api/profile/corrections             — Patch B4's
    list_corrections(), the audit trail: what changed, and why.

Every write here is inherently explicit (a person editing their own
settings panel), so every route below calls into user_profile.py's
explicit-signal entry points (override_profile_fact /
delete_profile_fact) — never record_signal()/set_output_pref() with
explicit=False. Nothing here writes inferred signals; that's
eo/fact_summarizer.py's job (Patch B2), off the model's own reading of
the conversation, not something a settings-panel request should ever
trigger.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from eo import user_profile

router = APIRouter()


class OverrideFactRequest(BaseModel):
    # `value` is intentionally typed loose (matches
    # override_profile_fact()'s own `new_value` parameter) — a
    # domains entry's value is typically {"level": "..."}, a
    # likes/dislikes entry's is often a bare bool or short string, and
    # this route has no business narrowing that shape on the field's
    # behalf.
    value: object
    reason: str | None = None


class OverrideOutputFormatRequest(BaseModel):
    default_format: str
    reason: str | None = None


@router.get("/api/profile")
def get_user_profile(owner_id: str = Depends(require_auth)):
    """Full profile, always the full shape (Patch B1's get_profile()
    empty-shape guarantee) — the settings panel can render straight
    from this with no defensive key checks, same as every other
    get_facts()-shaped read in this codebase."""
    return user_profile.get_profile(owner_id)


@router.get("/api/profile/corrections")
def get_profile_corrections(owner_id: str = Depends(require_auth)):
    """The audit trail behind "a system that quietly profiles someone
    should let them audit it" — every explicit override or delete,
    oldest first, append-only."""
    return user_profile.list_corrections(owner_id)


@router.put("/api/profile/output-format")
def put_profile_output_format(req: OverrideOutputFormatRequest, owner_id: str = Depends(require_auth)):
    try:
        return user_profile.override_profile_fact(
            owner_id, "output_prefs", req.default_format, reason=req.reason, source="settings_panel",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/profile/output-format")
def delete_profile_output_format(reason: str | None = None, owner_id: str = Depends(require_auth)):
    # `reason` as a query param, not a body — a DELETE with an
    # optional one-line "why" reads more naturally as
    # ?reason=... than as a JSON body most HTTP clients make awkward
    # to attach to DELETE requests.
    try:
        return user_profile.delete_profile_fact(owner_id, "output_prefs", reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/api/profile/{field}/{key}")
def put_profile_fact(field: str, key: str, req: OverrideFactRequest, owner_id: str = Depends(require_auth)):
    """field is one of domains/likes/dislikes/error_patterns — see
    eo/user_profile.py's SIGNAL_CATEGORIES. output_prefs deliberately
    doesn't route through here (see module docstring: it's a
    single-value record, not a keyed bucket, so it gets its own
    key-less /output-format route above rather than an awkward
    placeholder key here)."""
    if field == "output_prefs":
        raise HTTPException(
            status_code=400,
            detail="output_prefs has no key — use PUT /api/profile/output-format instead",
        )
    try:
        return user_profile.override_profile_fact(
            owner_id, field, req.value, key=key, reason=req.reason, source="settings_panel",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/profile/{field}/{key}")
def delete_profile_fact_route(field: str, key: str, reason: str | None = None,
                               owner_id: str = Depends(require_auth)):
    if field == "output_prefs":
        raise HTTPException(
            status_code=400,
            detail="output_prefs has no key — use DELETE /api/profile/output-format instead",
        )
    try:
        return user_profile.delete_profile_fact(owner_id, field, key=key, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
