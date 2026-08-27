"""
tests/unit/test_api_routes_user_profile.py — Patch B6 coverage for
api/routes/user_profile.py.

Mounts just this one route on a throwaway FastAPI app and overrides
require_auth (same pattern tests/unit/test_task_routes_full_payload.py
already established for tasks.py's route tests) — real auth is
Supabase-JWT-backed and out of scope for this route's own contract.

Highest-value things to pin down, mirroring
tests/unit/test_eo_user_profile.py's own coverage priorities for the
module underneath this surface:
  - GET /api/profile always returns the full shape, even for an
    account that's never written anything (Patch B1's empty-shape
    guarantee, now exercised through the HTTP layer).
  - PUT routes are explicit-signal writes: they jump straight to
    EXPLICIT_CONFIDENCE, never the gradual inferred curve.
  - DELETE routes remove the entry outright and log a correction with
    new_value=None; deleting something never set is a no-op, not a
    404 or 500.
  - The route-level guard steering output_prefs to its own key-less
    endpoint instead of the {field}/{key} routes.
  - list_corrections() surfaces the full audit trail through
    GET /api/profile/corrections.
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.user_profile as user_profile_routes
from api.deps import require_auth
from eo import user_profile

OWNER_ID = "test-owner-id"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(user_profile_routes.router)
    app.dependency_overrides[require_auth] = lambda: OWNER_ID
    return TestClient(app)


# ---------------------------------------------------------------------
# GET /api/profile
# ---------------------------------------------------------------------

def test_get_profile_on_a_never_written_account_returns_the_full_empty_shape(client, fake_bus):
    resp = client.get("/api/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["domains"] == {}
    assert body["likes"] == {}
    assert body["dislikes"] == {}
    assert body["error_patterns"] == {}
    assert body["output_prefs"]["default_format"] is None
    assert body["corrections"] == []


def test_get_profile_reflects_prior_writes_through_the_module(client, fake_bus):
    user_profile.record_signal(OWNER_ID, "likes", "diagrams", value=True, explicit=True)
    resp = client.get("/api/profile")
    assert resp.status_code == 200
    assert resp.json()["likes"]["diagrams"]["value"] is True


# ---------------------------------------------------------------------
# PUT /api/profile/output-format
# ---------------------------------------------------------------------

def test_put_output_format_sets_explicit_confidence_immediately(client, fake_bus):
    resp = client.put("/api/profile/output-format", json={"default_format": "diagram"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["output_prefs"]["default_format"] == "diagram"
    assert body["output_prefs"]["confidence"] == user_profile.EXPLICIT_CONFIDENCE


def test_put_output_format_logs_a_correction_when_it_overrides_a_prior_value(client, fake_bus):
    client.put("/api/profile/output-format", json={"default_format": "diagram"})
    resp = client.put("/api/profile/output-format", json={"default_format": "markdown", "reason": "changed my mind"})
    assert resp.status_code == 200
    corrections = client.get("/api/profile/corrections").json()
    assert len(corrections) == 1
    assert corrections[0]["field"] == "output_prefs"
    assert corrections[0]["old_value"] == "diagram"
    assert corrections[0]["new_value"] == "markdown"
    assert corrections[0]["reason"] == "changed my mind"


# ---------------------------------------------------------------------
# DELETE /api/profile/output-format
# ---------------------------------------------------------------------

def test_delete_output_format_resets_it_and_logs_a_correction(client, fake_bus):
    client.put("/api/profile/output-format", json={"default_format": "diagram"})
    resp = client.delete("/api/profile/output-format?reason=no+longer+applies")
    assert resp.status_code == 200
    assert resp.json()["output_prefs"]["default_format"] is None

    corrections = client.get("/api/profile/corrections").json()
    assert corrections[-1]["field"] == "output_prefs"
    assert corrections[-1]["old_value"] == "diagram"
    assert corrections[-1]["new_value"] is None
    assert corrections[-1]["reason"] == "no longer applies"


def test_delete_output_format_on_an_unset_account_is_a_quiet_no_op(client, fake_bus):
    resp = client.delete("/api/profile/output-format")
    assert resp.status_code == 200
    assert resp.json()["output_prefs"]["default_format"] is None
    assert client.get("/api/profile/corrections").json() == []


# ---------------------------------------------------------------------
# PUT /api/profile/{field}/{key}
# ---------------------------------------------------------------------

def test_put_bucketed_fact_sets_explicit_confidence_immediately(client, fake_bus):
    resp = client.put("/api/profile/dislikes/Python", json={"value": True, "reason": "user said so"})
    assert resp.status_code == 200
    entry = resp.json()["dislikes"]["Python"]
    assert entry["value"] is True
    assert entry["confidence"] == user_profile.EXPLICIT_CONFIDENCE
    assert entry["explicit"] is True


def test_put_bucketed_fact_overriding_a_weak_inferred_guess_is_logged_as_a_correction(client, fake_bus):
    user_profile.record_signal(OWNER_ID, "dislikes", "Python", value=True, explicit=False)
    resp = client.put("/api/profile/dislikes/Python", json={"value": False, "reason": "actually I like it"})
    assert resp.status_code == 200
    assert resp.json()["dislikes"]["Python"]["value"] is False

    corrections = client.get("/api/profile/corrections").json()
    assert len(corrections) == 1
    assert corrections[0]["key"] == "Python"
    assert corrections[0]["old_value"] is True
    assert corrections[0]["new_value"] is False


def test_put_output_prefs_via_the_keyed_route_is_rejected(client, fake_bus):
    """output_prefs is a single-value record, not a keyed bucket — it
    has its own /output-format route above precisely so a caller can't
    stash an arbitrary key under it here."""
    resp = client.put("/api/profile/output_prefs/anything", json={"value": "markdown"})
    assert resp.status_code == 400


def test_put_bucketed_fact_rejects_an_unknown_field(client, fake_bus):
    resp = client.put("/api/profile/not_a_real_field/x", json={"value": True})
    assert resp.status_code == 400


# ---------------------------------------------------------------------
# DELETE /api/profile/{field}/{key}
# ---------------------------------------------------------------------

def test_delete_bucketed_fact_removes_it_and_logs_a_correction(client, fake_bus):
    # %20, not "+" — "+" only decodes to a space in a query string, not
    # in a path segment, and `key` here is a path param.
    client.put("/api/profile/likes/dark%20mode", json={"value": True})
    resp = client.delete("/api/profile/likes/dark%20mode?reason=no+longer+true")
    assert resp.status_code == 200
    assert "dark mode" not in resp.json()["likes"]

    corrections = client.get("/api/profile/corrections").json()
    assert corrections[-1]["field"] == "likes"
    assert corrections[-1]["key"] == "dark mode"
    assert corrections[-1]["new_value"] is None


def test_delete_bucketed_fact_never_set_is_a_quiet_no_op(client, fake_bus):
    resp = client.delete("/api/profile/likes/never-set-key")
    assert resp.status_code == 200
    assert client.get("/api/profile/corrections").json() == []


def test_delete_output_prefs_via_the_keyed_route_is_rejected(client, fake_bus):
    resp = client.delete("/api/profile/output_prefs/anything")
    assert resp.status_code == 400


# ---------------------------------------------------------------------
# GET /api/profile/corrections
# ---------------------------------------------------------------------

def test_get_corrections_on_a_never_written_account_is_an_empty_list(client, fake_bus):
    resp = client.get("/api/profile/corrections")
    assert resp.status_code == 200
    assert resp.json() == []
