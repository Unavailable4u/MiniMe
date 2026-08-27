"""
tests/unit/test_eo_mcp_client.py — Patch A1.

eo/mcp_client.py had zero test coverage before this (it's a new
module). These tests fake both transports rather than making a real
subprocess/network call in CI:

  - HTTP transport: `httpx.AsyncClient.post` is monkeypatched to a fake
    that answers `initialize`/`notifications/initialized`/`tools/list`/
    `tools/call` with canned JSON-RPC responses -- this is the
    "smoke-test against GitHub MCP" case the module's own docstring
    describes, minus the real network call, same trade-off
    test_eo_worker_pool.py documents for patching AGENT_CAPABILITIES
    instead of the real registry.
  - stdio transport: exercised against a tiny real subprocess (a
    one-file Python script that speaks the newline-delimited JSON-RPC
    framing over stdin/stdout) rather than a mock, since the whole
    point of that path is the subprocess plumbing (asyncio pipes, the
    reader-loop task, process teardown) -- mocking asyncio subprocess
    internals would test the mock, not the code.

Both test classes share one requirement: eo.mcp_client's own
module-level `_connections` dict must be empty at the start and end of
every test, same "clean shared module state between tests" discipline
test_eo_local_workspace.py's `_clean_module_state` fixture documents
for that module's own `_connections`/`_pending` dicts.
"""
import asyncio
import json
import sys
import textwrap

import httpx
import pytest

import eo.mcp_client as mcp_client
from eo.mcp_client import MCPClientError, call_mcp_tool, connect_server, disconnect_server, list_tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_module_state():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


# ---------------------------------------------------------------------
# HTTP transport (the GitHub-MCP-shaped smoke test)
# ---------------------------------------------------------------------

def _rpc_response(request_body: dict, result: dict | None = None, error: dict | None = None) -> httpx.Response:
    payload = {"jsonrpc": "2.0", "id": request_body["id"]}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://example.test/mcp"))


class _FakeHttpPost:
    """Stands in for httpx.AsyncClient.post -- routes on the JSON-RPC
    `method` field the same way a real MCP server would, so
    connect_server()'s full initialize -> notifications/initialized ->
    (test calls) tools/list / tools/call sequence exercises this
    module's real request-building/response-parsing code, not a
    hand-rolled shortcut."""

    def __init__(self, tools=None, call_result=None, call_error=None):
        self.tools = tools if tools is not None else [
            {"name": "search_issues", "description": "Search GitHub issues", "inputSchema": {"type": "object"}},
        ]
        self.call_result = call_result
        self.call_error = call_error
        self.calls = []

    async def __call__(self, url, json=None, timeout=None):
        self.calls.append(json["method"])
        method = json["method"]
        if method == "initialize":
            return _rpc_response(json, {"serverInfo": {"name": "fake-github-mcp", "version": "1.0"}})
        if method == "notifications/initialized":
            return httpx.Response(202, request=httpx.Request("POST", url))
        if method == "tools/list":
            return _rpc_response(json, {"tools": self.tools})
        if method == "tools/call":
            if self.call_error is not None:
                return _rpc_response(json, error=self.call_error)
            return _rpc_response(json, self.call_result or {"content": [{"type": "text", "text": "ok"}]})
        raise AssertionError(f"unexpected MCP method in test fake: {method}")


@pytest.mark.anyio
async def test_connect_list_and_call_over_http(monkeypatch):
    fake_post = _FakeHttpPost()
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await connect_server("github", transport="http", url="https://example.test/mcp", headers={"Authorization": "Bearer x"})
    assert mcp_client.is_connected("github")

    tools = await list_tools("github")
    assert [t.name for t in tools] == ["search_issues"]
    assert tools[0].server_name == "github"

    result = await call_mcp_tool("github", "search_issues", {"query": "is:open"})
    assert result["content"][0]["text"] == "ok"

    # initialize + notifications/initialized happened before either the
    # list or the call -- the handshake really did run first.
    assert fake_post.calls[:2] == ["initialize", "notifications/initialized"]

    await disconnect_server("github")
    assert not mcp_client.is_connected("github")


@pytest.mark.anyio
async def test_connect_is_idempotent(monkeypatch):
    fake_post = _FakeHttpPost()
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await connect_server("github", transport="http", url="https://example.test/mcp")
    first_conn = mcp_client._connections["github"]
    await connect_server("github", transport="http", url="https://example.test/mcp")
    # Same connection object -- the second call was a no-op, not a fresh handshake.
    assert mcp_client._connections["github"] is first_conn


@pytest.mark.anyio
async def test_tool_call_error_raises_mcp_client_error(monkeypatch):
    fake_post = _FakeHttpPost(call_error={"code": -32000, "message": "bad credentials"})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await connect_server("github", transport="http", url="https://example.test/mcp")
    with pytest.raises(MCPClientError, match="bad credentials"):
        await call_mcp_tool("github", "search_issues", {"query": "is:open"})


@pytest.mark.anyio
async def test_is_error_content_raises():
    """A tool that responds successfully at the JSON-RPC level but sets
    isError=true (MCP's own convention for a tool-level failure, distinct
    from a transport-level JSON-RPC error) must still surface as
    MCPClientError, not a silently 'successful' result."""
    async def fake_post(url, json=None, timeout=None):
        if json["method"] == "initialize":
            return _rpc_response(json, {"serverInfo": {}})
        if json["method"] == "notifications/initialized":
            return httpx.Response(202, request=httpx.Request("POST", url))
        if json["method"] == "tools/call":
            return _rpc_response(json, {"isError": True, "content": [{"type": "text", "text": "rate limited"}]})
        raise AssertionError(json["method"])

    import unittest.mock
    with unittest.mock.patch.object(httpx.AsyncClient, "post", fake_post):
        await connect_server("github", transport="http", url="https://example.test/mcp")
        with pytest.raises(MCPClientError, match="rate limited"):
            await call_mcp_tool("github", "search_issues", {})


@pytest.mark.anyio
async def test_call_without_connecting_raises():
    with pytest.raises(MCPClientError, match="no MCP connection"):
        await call_mcp_tool("never-connected", "some_tool", {})


# ---------------------------------------------------------------------
# stdio transport -- real subprocess, no mocking
# ---------------------------------------------------------------------

_FAKE_STDIO_SERVER = textwrap.dedent("""
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method == "notifications/initialized":
            continue  # no response to a notification
        req_id = msg["id"]
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"serverInfo": {"name": "fake-stdio-mcp"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "echo", "description": "Echoes input", "inputSchema": {"type": "object"}}
            ]}})
        elif method == "tools/call":
            args = msg["params"]["arguments"]
            send({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": args.get("text", "")}]}})
""")


@pytest.mark.anyio
async def test_connect_list_and_call_over_stdio(tmp_path):
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_STDIO_SERVER)

    await connect_server("local-echo", transport="stdio", command=[sys.executable, str(script)])
    try:
        tools = await list_tools("local-echo")
        assert [t.name for t in tools] == ["echo"]

        result = await call_mcp_tool("local-echo", "echo", {"text": "hi"})
        assert result["content"][0]["text"] == "hi"
    finally:
        await disconnect_server("local-echo")

    assert not mcp_client.is_connected("local-echo")
