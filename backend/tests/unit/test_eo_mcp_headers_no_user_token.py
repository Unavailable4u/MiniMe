"""
tests/unit/test_eo_mcp_headers_no_user_token.py — Patch SX-27.

The question: can a signed-in user's token (the JWT api/deps.py checks on each
request) ever end up in the HTTP headers the backend sends to a remote MCP
server? It must not -- that would hand one person's identity to third-party
code.

How headers reach an MCP server today (read from the code, then pinned here):
  - eo/mcp_registry.py builds them ONCE from the "headers" object of an entry in
    backend/config/mcp_servers.json, replacing ${VAR} placeholders with the
    environment variables that file names;
  - eo/mcp_client.py hands that dict to httpx.AsyncClient(headers=...) at
    connect time and adds nothing per request.
Nothing on that path reads a request, a context variable or an auth module.

These tests run the real connect/list/call code against a fake HTTP transport
that records every request, with a user JWT planted everywhere it could leak
from (environment variables with likely names, changed after connecting, plus
a context variable), and assert:
  - the JWT is in no header name or value of any request;
  - the only credential sent is the one the config asked for;
  - only a known set of header names is ever sent, so a new header added by a
    later change has to be looked at here;
  - the checker itself works: a config that DOES name such a variable is caught;
  - the MCP modules do not import request, auth or context-variable code.

Nothing here skips and nothing touches the network. The "user JWT" below is a
made-up marker string, not a real token.
"""
import ast
import contextvars
import json
import os

import httpx
import pytest

import eo.mcp_client as mcp_client
import eo.mcp_pins as mcp_pins
from eo.mcp_registry import connect_configured_servers

USER_JWT = "user-jwt-header.SX27-MARKER-payload.signature"
REGISTRY_TOKEN = "registry-token-for-this-server"

# What httpx adds on its own, plus the two headers this repo's config asks for.
ALLOWED_HEADER_NAMES = {
    "host", "accept", "accept-encoding", "connection", "user-agent",
    "content-length", "content-type",
    "authorization", "x-static",
}

# Environment variable names a developer might plausibly use for a user's token.
_USER_TOKEN_ENV_NAMES = ["USER_JWT", "SUPABASE_USER_JWT", "AUTHORIZATION", "HTTP_AUTHORIZATION", "USER_TOKEN"]

_request_user_token = contextvars.ContextVar("request_user_token", default=None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_module_state():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


@pytest.fixture(autouse=True)
def _pins_not_under_test(monkeypatch):
    # This file is about headers, not tool pins (test_eo_mcp_pins.py covers those).
    async def approved(server_name, *, pins_path=None):
        return None

    monkeypatch.setattr(mcp_pins, "verify_server", approved)


@pytest.fixture
def recorded(monkeypatch):
    """Replaces httpx.AsyncClient with one that talks to a fake MCP server and
    records every request it is asked to send (headers included)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if "id" not in body:  # a notification: no reply expected
            return httpx.Response(202)
        results = {
            "initialize": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}},
            "tools/list": {"tools": [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object"}}]},
            "tools/call": {"content": [{"type": "text", "text": "ok"}]},
        }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": results[body["method"]]})

    real_client = httpx.AsyncClient

    def client_with_fake_server(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_fake_server)
    return requests


def _write_config(tmp_path, headers):
    entry = {
        "name": "srv",
        "enabled": True,
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "default_tool_trust": "read_only",
    }
    if headers is not None:
        entry["headers"] = headers
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"servers": [entry]}))
    return str(path)


def _plant_user_token(monkeypatch, suffix=""):
    for name in _USER_TOKEN_ENV_NAMES:
        monkeypatch.setenv(name, f"Bearer {USER_JWT}{suffix}")
    _request_user_token.set(USER_JWT + suffix)


def _leaks(requests):
    """Every (header name, value) pair, across all requests, that contains the user's JWT."""
    return [
        (name, value)
        for r in requests
        for name, value in r.headers.items()
        if USER_JWT in name or USER_JWT in value
    ]


async def _connect_list_and_call(config_path, monkeypatch):
    assert await connect_configured_servers(config_path) == {"srv": None}
    _plant_user_token(monkeypatch, "-after-connect")  # a token that only exists while a request runs
    await mcp_client.list_tools("srv", refresh=True)
    await mcp_client.call_mcp_tool("srv", "echo", {"text": "hello"})


# ---------------------------------------------------------------------
# The user's JWT never reaches the headers
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_user_jwt_never_reaches_mcp_headers(tmp_path, monkeypatch, recorded):
    monkeypatch.setenv("SX27_REGISTRY_TOKEN", REGISTRY_TOKEN)
    _plant_user_token(monkeypatch)  # present BEFORE connecting ...
    config = _write_config(tmp_path, {"Authorization": "Bearer ${SX27_REGISTRY_TOKEN}", "X-Static": "fixed"})

    await _connect_list_and_call(config, monkeypatch)  # ... and changed AFTER

    assert len(recorded) >= 4  # initialize, initialized notification, tools/list, tools/call
    assert _leaks(recorded) == []
    for request in recorded:
        assert request.headers["authorization"] == f"Bearer {REGISTRY_TOKEN}"
        assert request.headers["x-static"] == "fixed"
        assert {name.lower() for name in request.headers} <= ALLOWED_HEADER_NAMES


@pytest.mark.anyio
async def test_server_with_no_headers_configured_sends_no_credentials(tmp_path, monkeypatch, recorded):
    _plant_user_token(monkeypatch)
    config = _write_config(tmp_path, None)

    await _connect_list_and_call(config, monkeypatch)

    assert len(recorded) >= 4
    assert _leaks(recorded) == []
    for request in recorded:
        assert "authorization" not in request.headers
        assert "x-static" not in request.headers
        assert {name.lower() for name in request.headers} <= ALLOWED_HEADER_NAMES - {"authorization", "x-static"}


# ---------------------------------------------------------------------
# The checks themselves would catch a leak
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_checker_catches_a_header_that_names_a_user_token_variable(tmp_path, monkeypatch, recorded):
    # If someone DID write "${USER_JWT}" into mcp_servers.json, the registry would
    # expand it, and this test's detector must notice. This proves the checks
    # above are able to fail, i.e. that their "no leaks" result means something.
    _plant_user_token(monkeypatch)
    config = _write_config(tmp_path, {"X-Debug": "${USER_JWT}"})

    assert await connect_configured_servers(config) == {"srv": None}

    assert [name for name, _ in _leaks(recorded)] != []
    assert any(name.lower() not in ALLOWED_HEADER_NAMES for r in recorded for name in r.headers)


# ---------------------------------------------------------------------
# The MCP modules stay clear of request and auth code
# ---------------------------------------------------------------------

_FORBIDDEN_IMPORTS = {"fastapi", "starlette", "jwt", "contextvars", "flask", "api"}


@pytest.mark.parametrize("module", ["mcp_client.py", "mcp_registry.py", "mcp_pins.py"])
def test_mcp_modules_do_not_import_request_or_auth_code(module):
    path = os.path.join(os.path.dirname(os.path.abspath(mcp_client.__file__)), module)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert imported & _FORBIDDEN_IMPORTS == set(), (
        f"eo/{module} imports request/auth code; if that is intended, check it cannot put "
        "a user's token into an MCP header, then update this test"
    )
