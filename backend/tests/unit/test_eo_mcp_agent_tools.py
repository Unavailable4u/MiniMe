"""
tests/unit/test_eo_mcp_agent_tools.py — Patch A3.

Fakes eo.mcp_client (already proven by test_eo_mcp_client.py) and
eo.mcp_registry (already proven by test_eo_mcp_registry.py) at their
own module boundary -- this file only tests what Patch A3 itself adds:
name encoding/decoding, tools-array shape, connected-only filtering,
one-bad-server-doesn't-block-the-rest, and that call_agent_mcp_tool()
emits the right MCP_TOOL_CALLED/MCP_TOOL_RESULT events via
relay.emitter.emit_workspace_event (monkeypatched, same "assert on
call args, don't hit a real Pusher" approach
test_eo_local_workspace_tools.py already uses for its own
_emit_tool_event assertions).
"""
import pytest

import eo.mcp_agent_tools as mcp_agent_tools
import eo.mcp_client as mcp_client
from eo.mcp_agent_tools import (
    _agent_tool_name,
    _parse_agent_tool_name,
    call_agent_mcp_tool,
    mcp_tools_for_agent,
)
from eo.mcp_client import MCPClientError, MCPTool
from relay.emitter import EventType


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_module_state():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


# ---------------------------------------------------------------------
# Name encoding / decoding
# ---------------------------------------------------------------------

def test_agent_tool_name_round_trips():
    name = _agent_tool_name("github", "search_issues")
    assert name == "mcp__github__search_issues"
    assert _parse_agent_tool_name(name) == ("github", "search_issues")


def test_parse_agent_tool_name_handles_double_underscore_in_tool_name():
    # MCP itself doesn't forbid "__" inside a tool's own name -- only
    # the first separator is this module's to parse (maxsplit=1).
    name = _agent_tool_name("context7", "resolve__library_id")
    assert _parse_agent_tool_name(name) == ("context7", "resolve__library_id")


@pytest.mark.parametrize("bad_name", [
    "search_issues",              # no prefix at all
    "mcp__github",                # no tool half
    "mcp__",                      # nothing after the prefix
    "generate_flashcards",        # an internal tool name, not MCP
])
def test_parse_agent_tool_name_rejects_malformed_names(bad_name):
    with pytest.raises(ValueError):
        _parse_agent_tool_name(bad_name)


# ---------------------------------------------------------------------
# mcp_tools_for_agent()
# ---------------------------------------------------------------------

def _fake_list_mcp_servers(servers):
    def _fake(path=None):
        return servers
    return _fake


@pytest.mark.anyio
async def test_only_connected_servers_are_offered(monkeypatch):
    monkeypatch.setattr(
        mcp_agent_tools.mcp_registry, "list_mcp_servers",
        _fake_list_mcp_servers([
            {"name": "github", "enabled": True, "transport": "http", "connected": True, "default_tool_trust": "mutating"},
            {"name": "context7", "enabled": True, "transport": "stdio", "connected": False, "default_tool_trust": "read_only"},
        ]),
    )

    calls = []

    async def fake_list_tools(server_name, refresh=False):
        calls.append(server_name)
        return [MCPTool(name="search_issues", description="Search issues", input_schema={}, server_name=server_name)]

    monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)

    tools = await mcp_tools_for_agent()
    assert calls == ["github"]  # context7 skipped -- not connected
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "mcp__github__search_issues"
    assert tools[0]["type"] == "function"


@pytest.mark.anyio
async def test_tool_schema_shape_and_missing_input_schema_defaults_to_empty_object(monkeypatch):
    monkeypatch.setattr(
        mcp_agent_tools.mcp_registry, "list_mcp_servers",
        _fake_list_mcp_servers([
            {"name": "github", "enabled": True, "transport": "http", "connected": True, "default_tool_trust": "mutating"},
        ]),
    )

    async def fake_list_tools(server_name, refresh=False):
        return [MCPTool(name="list_repos", description="List repos", input_schema=None, server_name=server_name)]

    monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)

    tools = await mcp_tools_for_agent()
    fn = tools[0]["function"]
    assert fn["name"] == "mcp__github__list_repos"
    assert "github MCP server" in fn["description"]
    assert fn["parameters"] == {"type": "object", "properties": {}, "required": []}


@pytest.mark.anyio
async def test_one_server_tools_list_failure_does_not_block_others(monkeypatch):
    monkeypatch.setattr(
        mcp_agent_tools.mcp_registry, "list_mcp_servers",
        _fake_list_mcp_servers([
            {"name": "flaky", "enabled": True, "transport": "http", "connected": True, "default_tool_trust": "mutating"},
            {"name": "github", "enabled": True, "transport": "http", "connected": True, "default_tool_trust": "mutating"},
        ]),
    )

    async def fake_list_tools(server_name, refresh=False):
        if server_name == "flaky":
            raise MCPClientError("server hiccup")
        return [MCPTool(name="search_issues", description="Search issues", input_schema={}, server_name=server_name)]

    monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)

    tools = await mcp_tools_for_agent()
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "mcp__github__search_issues"


# ---------------------------------------------------------------------
# call_agent_mcp_tool()
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_agent_mcp_tool_success_emits_called_then_result(monkeypatch):
    events = []

    def fake_emit(event_type, workspace_id=None, agent=None, payload=None):
        events.append((event_type, workspace_id, agent, payload))
        return True

    monkeypatch.setattr(mcp_agent_tools, "emit_workspace_event", fake_emit)

    async def fake_call_mcp_tool(server_name, tool_name, params, timeout=30.0):
        assert server_name == "github"
        assert tool_name == "search_issues"
        assert params == {"query": "bug"}
        return {"content": [{"type": "text", "text": "3 issues found"}]}

    monkeypatch.setattr(mcp_client, "call_mcp_tool", fake_call_mcp_tool)

    result = await call_agent_mcp_tool("mcp__github__search_issues", {"query": "bug"}, workspace_id="ws1")
    assert result == {"content": [{"type": "text", "text": "3 issues found"}]}

    assert [e[0] for e in events] == [EventType.MCP_TOOL_CALLED, EventType.MCP_TOOL_RESULT]
    assert all(e[1] == "ws1" for e in events)
    assert events[1][3]["ok"] is True


@pytest.mark.anyio
async def test_call_agent_mcp_tool_failure_still_emits_result_and_reraises(monkeypatch):
    events = []

    def fake_emit(event_type, workspace_id=None, agent=None, payload=None):
        events.append((event_type, payload))
        return True

    monkeypatch.setattr(mcp_agent_tools, "emit_workspace_event", fake_emit)

    async def fake_call_mcp_tool(server_name, tool_name, params, timeout=30.0):
        raise MCPClientError("rate limited")

    monkeypatch.setattr(mcp_client, "call_mcp_tool", fake_call_mcp_tool)

    with pytest.raises(MCPClientError, match="rate limited"):
        await call_agent_mcp_tool("mcp__github__search_issues", {})

    assert events[-1][0] == EventType.MCP_TOOL_RESULT
    assert events[-1][1]["ok"] is False
    assert events[-1][1]["error"] == "rate limited"


@pytest.mark.anyio
async def test_call_agent_mcp_tool_rejects_malformed_name_before_any_call():
    with pytest.raises(ValueError):
        await call_agent_mcp_tool("not_an_mcp_tool", {})
