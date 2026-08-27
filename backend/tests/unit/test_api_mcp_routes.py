"""
tests/unit/test_api_mcp_routes.py -- Patch A8.

Mounts just api/routes/mcp.py's router on a throwaway FastAPI app,
same pattern test_task_routes_full_payload.py already uses for a
single-file route module. list_mcp_servers/mcp_server_status
themselves are already covered by test_eo_mcp_registry.py (A2) --
these tests only check that the two routes call through correctly and
pass results back unmodified, not the underlying registry logic
itself, so both are monkeypatched at the api.routes.mcp module
boundary (where they were imported into, not where they're defined).
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.mcp as mcp_routes
from api.deps import require_auth


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mcp_routes.router)
    app.dependency_overrides[require_auth] = lambda: "test-owner-id"
    return TestClient(app)


def test_list_servers_returns_the_registry_list_unmodified(client, monkeypatch):
    fake_servers = [
        {"name": "github", "enabled": True, "transport": "http", "connected": True, "default_tool_trust": "read_only"},
        {"name": "context7", "enabled": False, "transport": "stdio", "connected": False, "default_tool_trust": "mutating"},
    ]
    monkeypatch.setattr(mcp_routes, "list_mcp_servers", lambda: fake_servers)

    resp = client.get("/api/mcp/servers")
    assert resp.status_code == 200
    assert resp.json() == fake_servers


def test_list_servers_requires_auth():
    app = FastAPI()
    app.include_router(mcp_routes.router)
    resp = TestClient(app).get("/api/mcp/servers")
    assert resp.status_code in (401, 403)


def test_server_status_returns_the_registry_status_unmodified(client, monkeypatch):
    fake_status = {
        "name": "github",
        "enabled": True,
        "transport": "http",
        "connected": True,
        "default_tool_trust": "read_only",
        "tools": [{"name": "search_issues", "description": "...", "trust": "read_only"}],
    }

    async def _fake_status(server_name, path=None):
        assert server_name == "github"
        return fake_status

    monkeypatch.setattr(mcp_routes, "mcp_server_status", _fake_status)

    resp = client.get("/api/mcp/servers/github/status")
    assert resp.status_code == 200
    assert resp.json() == fake_status


def test_server_status_for_an_unknown_server_returns_200_with_an_error_field(client, monkeypatch):
    """mcp_server_status() itself returns {"error": ...} rather than
    raising for an unrecognized name (see its own docstring) -- this
    route deliberately does not translate that into a 404."""
    async def _fake_status(server_name, path=None):
        return {"name": server_name, "error": "not found in mcp_servers.json"}

    monkeypatch.setattr(mcp_routes, "mcp_server_status", _fake_status)

    resp = client.get("/api/mcp/servers/nonexistent/status")
    assert resp.status_code == 200
    assert resp.json() == {"name": "nonexistent", "error": "not found in mcp_servers.json"}
