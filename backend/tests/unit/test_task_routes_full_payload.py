"""
tests/unit/test_task_routes_full_payload.py — J.4 coverage for J.2,
GET /api/task/{session_id}/step/{role}/full (backend/api/routes/tasks.py).

Mounts just this one route on a throwaway FastAPI app (same pattern as
tests/unit/test_eo_local_workspace.py's websocket tests) and overrides
require_auth, since real auth is Supabase-JWT-backed and out of scope
for this route's own contract.

IMPORTANT FINDING (read below before assuming this route is fully
fixed): the two straightforward tests below match the guide's literal
J.2 "Done when" wording — 200 for a completed step, 404 for a
nonexistent one — and both pass. But a third test,
test_route_misses_the_real_write_namespace_from_a_dock_dispatched_run,
reproduces how a real tier-3 run actually scopes its stage_output
write (per api/task_runner.py's set_app_slug() call and
WorkspaceDockContext.jsx's "BUGFIX (bug audit, patch 5)" comment: the
write happens under app_slug = the dock's real workspace id, or —
for a non-dock/no-app_slug caller — under
f"{slugify(task_text)}_{session_id[:8]}", never under the bare
session_id). The route itself calls set_app_slug(session_id), which is
neither of those. That test currently FAILS against the route as
written: for the common real-world case, this endpoint 404s even
though the step actually completed and its output is sitting in Redis
under a different, session_id-adjacent-but-not-equal key. This is the
same class of bug WorkspaceDockContext.jsx's own "bug audit, patch 5"
comment already fixed once on the *write* side for a different
key — it's back here on this *read* side, unfixed. Flagging rather
than silently patching it, since fixing it needs a decision about
which of the two possible sources of truth (dock workspace id vs. the
task_runner-derived slug) this route should resolve against, and
that's a design call, not a one-line fix.
"""
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.tasks as tasks_module
from api.deps import require_auth
from memory.bus import set_app_slug, slugify, write


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(tasks_module.router)
    app.dependency_overrides[require_auth] = lambda: "test-owner-id"
    return TestClient(app)


def _full_url(session_id, role):
    return f"/api/task/{session_id}/step/{role}/full"


def test_completed_step_returns_full_payload_with_200(client, fake_bus):
    session_id = "session-abc123"
    role = "hardware_speccer"

    # Mirrors the route's own set_app_slug(session_id) + the plain-string
    # write shape (generic_worker.py's normal, non-approval-edited
    # completion — see read_stage_output_text()'s docstring, shape 1).
    set_app_slug(session_id)
    write(f"stage_output:{session_id}:{role}", "full untruncated parts/wiring text")

    resp = client.get(_full_url(session_id, role))

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": session_id,
        "role": role,
        "text": "full untruncated parts/wiring text",
    }


def test_completed_step_handles_approval_edited_dict_shape(client, fake_bus):
    """The other legitimate storage shape read_stage_output_text() must
    handle — eo/executor.py's approval-pause "edit" action writes a
    {"text": ...} dict instead of a plain string."""
    session_id = "session-edited"
    role = "prd_writer"

    set_app_slug(session_id)
    write(f"stage_output:{session_id}:{role}", {"text": "edited full text", "edited_by": "user"})

    resp = client.get(_full_url(session_id, role))

    assert resp.status_code == 200
    assert resp.json()["text"] == "edited full text"


def test_nonexistent_session_or_step_returns_404(client, fake_bus):
    resp = client.get(_full_url("session-that-never-ran", "hardware_speccer"))
    assert resp.status_code == 404
    assert "hardware_speccer" in resp.json()["detail"]
    assert "session-that-never-ran" in resp.json()["detail"]


def test_role_never_run_for_a_real_session_still_404s(client, fake_bus):
    """A session that DID run (has other stage output) but never ran
    THIS role must still 404 — not fall through to some other role's
    text."""
    session_id = "session-partial"
    set_app_slug(session_id)
    write(f"stage_output:{session_id}:prd_writer", "prd text")

    resp = client.get(_full_url(session_id, "hardware_speccer"))
    assert resp.status_code == 404


def test_truncated_agent_done_then_full_recovery_round_trip(client, fake_bus):
    """Integration-style: simulates the actual failure mode from the
    guide (Image logs: `truncated=True` then a 404 on this route) by
    writing something long enough that a Pusher-cap truncator would
    have shrunk it, then confirming the /full route hands back the
    complete text rather than the truncated summary."""
    session_id = "session-truncated-repro"
    role = "hardware_speccer"
    full_text = "PARTS: " + ", ".join(f"part_{i}" for i in range(2000))  # long enough to blow past ~10KB
    truncated_summary = full_text[:200] + "... [truncated]"

    set_app_slug(session_id)
    write(f"stage_output:{session_id}:{role}", full_text)

    # The frontend only ever SEES `truncated_summary` over Pusher — it
    # must call this route to get `full_text` back, not trust the
    # shrunk copy.
    resp = client.get(_full_url(session_id, role))
    assert resp.status_code == 200
    assert resp.json()["text"] == full_text
    assert resp.json()["text"] != truncated_summary


@pytest.mark.xfail(
    reason=(
        "Real finding, not a test bug: a genuine tier-3 run never scopes "
        "its stage_output write under the bare session_id (see module "
        "docstring above). This route's set_app_slug(session_id) call "
        "therefore misses the actual write namespace for both realistic "
        "app_slug sources — a dock-dispatched run's workspace id, and a "
        "non-dock run's task_runner-derived slug. Left as an expected "
        "failure (rather than deleted) so it fails loudly again the "
        "moment someone marks J.2 'done' without actually reconciling "
        "this mismatch."
    ),
    strict=True,
)
def test_route_misses_the_real_write_namespace_from_a_dock_dispatched_run(client, fake_bus):
    session_id = "session-dock-real-run"
    role = "hardware_speccer"
    workspace_id = "ws_real_project_42"  # what WorkspaceDockContext.jsx actually sends as app_slug

    # This is what a REAL dock-dispatched run does: task_runner.py scopes
    # the whole run to app_slug=workspace_id (per TaskRequest.app_slug),
    # then generic_worker.py's bus_write() lands under that namespace.
    set_app_slug(workspace_id)
    write(f"stage_output:{session_id}:{role}", "full untruncated parts/wiring text")

    resp = client.get(_full_url(session_id, role))

    # This is what SHOULD happen once the mismatch is actually fixed.
    assert resp.status_code == 200
    assert resp.json()["text"] == "full untruncated parts/wiring text"


@pytest.mark.xfail(
    reason=(
        "Same underlying finding as the dock-dispatched case above, for "
        "the OTHER real app_slug source: a run with no explicit app_slug "
        "(app_slug=None all the way down) falls back to "
        "f'{slugify(task_text)}_{session_id[:8]}' in "
        "api/task_runner.py's run_with_looping(), never the bare "
        "session_id this route assumes."
    ),
    strict=True,
)
def test_route_misses_the_real_write_namespace_from_a_no_app_slug_run(client, fake_bus):
    session_id = "session-no-app-slug-run12345"
    role = "hardware_speccer"
    task_text = "Design a plant-watering IoT device"
    derived_app_slug = f"{slugify(task_text)}_{session_id[:8]}"

    set_app_slug(derived_app_slug)
    write(f"stage_output:{session_id}:{role}", "full untruncated parts/wiring text")

    resp = client.get(_full_url(session_id, role))

    assert resp.status_code == 200
    assert resp.json()["text"] == "full untruncated parts/wiring text"
