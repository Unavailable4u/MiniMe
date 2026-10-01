"""
tests/unit/test_api_routes_code_edit_wireframe.py — W8.6.

Route-level coverage for api/routes/code_edit.py's
POST /api/workspaces/{ws_id}/code/proposals/from-wireframe — the half of
W8.6 that tests/unit/test_eo_code_proposals_wireframe.py (the store) can't
reach: what the HTTP surface does with a request.

Mounts just this one router on a throwaway FastAPI app and overrides
require_auth (same pattern as test_api_routes_deploy_propose.py). The two
collaborators the route calls are faked at the names the route module
itself holds (`code_edit.chat_workspace`, `code_edit.code_proposals`), so
nothing here touches Postgres.

What matters most, worst-silent-failure first:

  1. Ownership: an unknown / someone else's workspace is a 404 BEFORE the
     proposal store is touched.
  2. The route is authenticated — and files the proposal under the
     authenticated caller's id, not anything from the body.
  3. A usable wireframe returns the stored proposal as-is (the shape
     POST .../code/proposals returns), so the same card/tray code reads it.
  4. Unusable html is a 400 with the store's own message, not a 500 and
     not a stored row.
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.code_edit as code_edit
from api.deps import require_auth

OWNER_ID = "owner_1"
WS = "ws_1"
URL = f"/api/workspaces/{WS}/code/proposals/from-wireframe"
HTML = "<!doctype html><html><body><button>Go</button></body></html>"


@pytest.fixture
def calls():
    return {"workspace": [], "create": []}


@pytest.fixture
def wired(monkeypatch, calls):
    state = {
        "workspace_missing": False,
        "create": lambda *a: {"id": "prop_1", "status": "pending", "files": []},
    }

    def fake_get_workspace(ws_id, owner_id):
        calls["workspace"].append((ws_id, owner_id))
        if state["workspace_missing"]:
            raise FileNotFoundError(ws_id)
        return {"id": ws_id}

    def fake_create(ws_id, html, screen_label, session_id, user_id):
        calls["create"].append((ws_id, html, screen_label, session_id, user_id))
        return state["create"](ws_id, html, screen_label, session_id, user_id)

    monkeypatch.setattr(code_edit.chat_workspace, "get_workspace", fake_get_workspace)
    monkeypatch.setattr(code_edit.code_proposals, "create_wireframe_proposal", fake_create)
    return state


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(code_edit.router)
    app.dependency_overrides[require_auth] = lambda: OWNER_ID
    return TestClient(app)


class TestFiled:
    def test_returns_the_stored_proposal_as_is(self, client, wired):
        wired["create"] = lambda *a: {"id": "prop_9", "status": "pending", "files": [{"path": "index.html"}]}
        resp = client.post(URL, json={"html": HTML})
        assert resp.status_code == 200
        assert resp.json() == {"id": "prop_9", "status": "pending", "files": [{"path": "index.html"}]}

    def test_passes_the_body_fields_through_and_files_under_the_callers_id(self, client, wired, calls):
        client.post(URL, json={"html": HTML, "screen_label": "Login", "session_id": "chat_7"})
        assert calls["create"] == [(WS, HTML, "Login", "chat_7", OWNER_ID)]

    def test_label_and_session_are_optional(self, client, wired, calls):
        assert client.post(URL, json={"html": HTML}).status_code == 200
        assert calls["create"] == [(WS, HTML, None, None, OWNER_ID)]

    def test_a_failed_generation_row_is_still_a_200(self, client, wired):
        # create_proposal() stores status='failed' rows rather than raising;
        # the route must not turn that into an error.
        wired["create"] = lambda *a: {"id": "prop_2", "status": "failed", "files": []}
        resp = client.post(URL, json={"html": HTML})
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"


class TestRejected:
    def test_unknown_workspace_is_a_404_before_the_store_is_touched(self, client, wired, calls):
        wired["workspace_missing"] = True
        resp = client.post(URL, json={"html": HTML})
        assert resp.status_code == 404
        assert calls["workspace"] == [(WS, OWNER_ID)]  # scoped to the caller
        assert calls["create"] == []

    def test_unusable_html_is_a_400_with_the_stores_message(self, client, wired):
        def boom(*a):
            raise ValueError("wireframe html is empty")
        wired["create"] = boom
        resp = client.post(URL, json={"html": "   "})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "wireframe html is empty"

    def test_missing_html_is_a_422(self, client, wired, calls):
        assert client.post(URL, json={"screen_label": "x"}).status_code == 422
        assert calls["create"] == []

    def test_the_route_is_authenticated(self, wired, calls):
        app = FastAPI()
        app.include_router(code_edit.router)  # no dependency override
        resp = TestClient(app).post(URL, json={"html": HTML})
        assert resp.status_code in (401, 403)
        assert calls["create"] == []
