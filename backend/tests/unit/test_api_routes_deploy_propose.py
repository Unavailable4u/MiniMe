"""
tests/unit/test_api_routes_deploy_propose.py — W8.5b.

Route-level coverage for api/routes/deploy.py's POST
/api/deploy/{session_id}/propose, the half of W8.5a that
tests/unit/test_eo_code_proposals_deploy.py (the store) and
tests/unit/test_agent_deploy_config_writer.py (the writer) can't reach:
what the HTTP response actually looks like once the two are wired
together.

Mounts just this one router on a throwaway FastAPI app and overrides
require_auth, same pattern tests/unit/test_api_routes_user_profile.py
uses. The three collaborators the route calls are faked at the names the
route module itself holds (`deploy_routes.chat_workspace`,
`deploy_routes.code_proposals`, `deploy_routes.deploy_config_writer_agent`)
so nothing here touches Postgres or an LLM; the store's own behaviour is
already covered end to end in test_eo_code_proposals_deploy.py.

What matters most, worst-silent-failure first:

  1. The response is still the plan, key for key — the Tasks-tab Deploy
     card and anything else reading /propose's body must see no change
     other than the added `proposal` / `proposal_skipped` keys.
  2. A plan that can't be reviewed (fallback placeholder, no workspace,
     unusable plan) is reported ALONGSIDE the plan with a 200, never as
     an error — and in the fallback case never reaches the store at all.
  3. The route is still authenticated. W8.5a moved `require_auth` from
     the decorator's `dependencies=[...]` to a parameter (it needs the
     returned owner id); this pins that the move didn't open it up.
  4. The owner id the proposal is filed under is the authenticated
     caller's, and the workspace is looked up with that same id.
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.deploy as deploy_routes
from api.deps import require_auth

OWNER_ID = "owner_1"
SESSION = "chat_1"
WS = "ws_1"

PLAN = {
    "platform": "render",
    "config_filename": "render.yaml",
    "config_content": "services:\n  - type: web\n    name: app\n",
    "reason": "backend service detected",
}
FALLBACK_PLAN = {
    "platform": "render",
    "config_filename": "render.yaml",
    "config_content": "# fallback: deploy config writer output was not valid JSON\n",
    "reason": "fallback: could not parse a real proposal",
    "fallback": True,
}


@pytest.fixture
def calls():
    """What each faked collaborator was called with, in order."""
    return {"writer": [], "workspace": [], "create": []}


@pytest.fixture
def wired(monkeypatch, calls):
    """Installs the three fakes and returns a small controller the tests
    use to choose what they hand back."""
    state = {
        "plan": dict(PLAN),
        "workspace": {"id": WS},
        "create": lambda ws_id, plan, session_id, user_id: {"id": "prop_1", "status": "pending"},
    }

    def fake_writer(session_id=None, **kwargs):
        calls["writer"].append(session_id)
        return dict(state["plan"])

    def fake_workspace_for_chat(chat_id, owner_id):
        calls["workspace"].append((chat_id, owner_id))
        return state["workspace"]

    def fake_create(ws_id, plan, session_id, user_id):
        calls["create"].append((ws_id, plan, session_id, user_id))
        return state["create"](ws_id, plan, session_id, user_id)

    monkeypatch.setattr(deploy_routes.deploy_config_writer_agent, "run_deploy_config_writer", fake_writer)
    monkeypatch.setattr(deploy_routes.chat_workspace, "workspace_for_chat", fake_workspace_for_chat)
    monkeypatch.setattr(deploy_routes.code_proposals, "create_deploy_config_proposal", fake_create)
    return state


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(deploy_routes.router)
    app.dependency_overrides[require_auth] = lambda: OWNER_ID
    return TestClient(app)


def _propose(client):
    return client.post(f"/api/deploy/{SESSION}/propose", json={})


# ---------------------------------------------------------------------------
# Filed for review
# ---------------------------------------------------------------------------

class TestFiledForReview:
    def test_response_is_the_plan_plus_a_proposal_summary(self, client, wired):
        resp = _propose(client)

        assert resp.status_code == 200
        body = resp.json()
        # Every plan key, unchanged...
        for key, value in PLAN.items():
            assert body[key] == value
        # ...plus exactly the summary the frontend opens a review from.
        assert body["proposal"] == {
            "id": "prop_1",
            "workspace_id": WS,
            "status": "pending",
            "path": "render.yaml",
        }
        assert "proposal_skipped" not in body

    def test_proposal_is_filed_in_the_callers_workspace_under_the_callers_id(self, client, wired, calls):
        _propose(client)

        assert calls["writer"] == [SESSION]
        # The workspace lookup is scoped to the authenticated caller, not
        # just the session id in the URL.
        assert calls["workspace"] == [(SESSION, OWNER_ID)]
        assert len(calls["create"]) == 1
        ws_id, plan, session_id, user_id = calls["create"][0]
        assert (ws_id, session_id, user_id) == (WS, SESSION, OWNER_ID)
        assert plan["config_filename"] == "render.yaml"
        assert plan["config_content"] == PLAN["config_content"]

    def test_a_non_pending_proposal_status_is_passed_through_not_hidden(self, client, wired):
        # create_proposal() stores a status='failed' row rather than
        # raising when generation fails — the card needs to see that.
        wired["create"] = lambda *a: {"id": "prop_2", "status": "failed"}

        body = _propose(client).json()

        assert body["proposal"]["id"] == "prop_2"
        assert body["proposal"]["status"] == "failed"


# ---------------------------------------------------------------------------
# Not filed: reported alongside the plan, never an error
# ---------------------------------------------------------------------------

class TestNotFiled:
    def test_fallback_plan_is_skipped_and_never_reaches_the_store(self, client, wired, calls):
        wired["plan"] = dict(FALLBACK_PLAN)

        resp = _propose(client)

        assert resp.status_code == 200
        body = resp.json()
        assert body["proposal"] is None
        assert body["proposal_skipped"] == "fallback_plan"
        # The plan itself still comes back, flag included, so the card can
        # tell the placeholder apart from a real plan after a reload.
        assert body["fallback"] is True
        assert body["config_filename"] == "render.yaml"
        # Not even the workspace lookup runs for a placeholder.
        assert calls["workspace"] == []
        assert calls["create"] == []

    def test_session_without_a_workspace_is_skipped(self, client, wired, calls):
        wired["workspace"] = None

        resp = _propose(client)

        assert resp.status_code == 200
        body = resp.json()
        assert body["proposal"] is None
        assert body["proposal_skipped"] == "no_workspace"
        assert body["config_content"] == PLAN["config_content"]
        assert calls["create"] == []

    def test_unusable_plan_reports_the_reason_instead_of_failing(self, client, wired):
        def reject(*args):
            raise ValueError("deploy plan has no config_filename")

        wired["create"] = reject

        resp = _propose(client)

        assert resp.status_code == 200
        body = resp.json()
        assert body["proposal"] is None
        assert body["proposal_skipped"] == "deploy plan has no config_filename"
        assert body["platform"] == "render"

    def test_a_path_the_workspace_rejects_is_skipped_the_same_way(self, client, wired):
        wired["plan"] = {**PLAN, "config_filename": "../outside.yaml"}

        def reject(ws_id, plan, session_id, user_id):
            raise ValueError("invalid file path: '../outside.yaml'")

        wired["create"] = reject

        body = _propose(client).json()

        assert body["proposal"] is None
        assert "invalid file path" in body["proposal_skipped"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_propose_still_requires_authentication(self, wired, calls):
        app = FastAPI()
        app.include_router(deploy_routes.router)  # NO dependency override
        unauthenticated = TestClient(app)

        resp = unauthenticated.post(f"/api/deploy/{SESSION}/propose", json={})

        assert resp.status_code == 401
        # Rejected before the writer (an LLM call) ever ran.
        assert calls["writer"] == []
        assert calls["create"] == []

    def test_write_and_go_live_still_require_authentication(self, wired):
        app = FastAPI()
        app.include_router(deploy_routes.router)
        unauthenticated = TestClient(app)

        assert unauthenticated.post(f"/api/deploy/{SESSION}/write", json={}).status_code == 401
        assert unauthenticated.post(f"/api/deploy/{SESSION}/go-live", json={}).status_code == 401
