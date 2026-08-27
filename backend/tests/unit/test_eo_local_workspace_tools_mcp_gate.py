"""
tests/unit/test_eo_local_workspace_tools_mcp_gate.py — Patch A4: Safety
Gate Extension for Mutating MCP Tools.

Companion to tests/unit/test_eo_local_workspace_tools.py (which already
covers the daemon-sourced propose/confirm/deny path this patch extends,
not replaces). This file only tests what Patch A4 itself adds:
  - propose_mcp_action()'s two rejection cases (malformed agent-tool
    name, read_only classification) and its success path.
  - confirm_action()'s new "mcp" branch: it hands off to
    eo.mcp_agent_tools.call_agent_mcp_tool() rather than call_daemon(),
    and emits MCP_TOOL_CONFIRMED (not LOCAL_TOOL_CONFIRMED) first.
  - deny_action()'s new "mcp" branch: emits MCP_TOOL_DENIED, never
    touches call_agent_mcp_tool() at all.
  - That the daemon-sourced path (propose_action/confirm_action/
    deny_action for write_file/delete/execute_command) is completely
    unaffected by any of the above -- same _pending_actions store,
    same TTL sweep, same LOCAL_TOOL_* events, source="daemon" by
    default.

Style matches test_eo_local_workspace_tools.py: eo.mcp_agent_tools.
call_agent_mcp_tool and eo.mcp_registry.classify_tool are monkeypatched
here (both already have their own coverage in test_eo_mcp_agent_tools.py
/ test_eo_mcp_registry.py) so these tests isolate exactly this module's
own propose/confirm/deny bookkeeping for the "mcp" source, without
needing a live MCP server connection.
"""
from unittest.mock import AsyncMock

import pytest

import eo.local_workspace_tools as lwt
from eo.mcp_client import MCPClientError
from relay.emitter import EventType

AGENT_TOOL_NAME = "mcp__github__create_issue"


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    """Same local-fixture convention every other @pytest.mark.anyio
    file in this repo uses (test_eo_local_workspace.py,
    test_eo_mcp_agent_tools.py, test_eo_mcp_registry.py,
    test_eo_ws_registry.py) -- there's no shared one in conftest.py."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_pending_actions():
    lwt._pending_actions.clear()
    yield
    lwt._pending_actions.clear()


@pytest.fixture
def mutating_classify(monkeypatch):
    """Makes every tool classify "mutating" -- the case propose_mcp_action()
    is meant to accept."""
    monkeypatch.setattr(lwt.mcp_registry, "classify_tool", lambda *a, **k: "mutating")


@pytest.fixture
def read_only_classify(monkeypatch):
    monkeypatch.setattr(lwt.mcp_registry, "classify_tool", lambda *a, **k: "read_only")


@pytest.fixture
def mock_call_agent_mcp_tool(monkeypatch):
    mock = AsyncMock(return_value={"issue_number": 42})
    monkeypatch.setattr(lwt.mcp_agent_tools, "call_agent_mcp_tool", mock)
    return mock


@pytest.fixture
def captured_events(monkeypatch):
    events = []

    def _fake_emit(event_type, **kwargs):
        events.append((event_type, kwargs))

    monkeypatch.setattr(lwt, "emit_workspace_event", _fake_emit)
    return events


# ---------------------------------------------------------------------
# propose_mcp_action()
# ---------------------------------------------------------------------

def test_propose_mcp_action_rejects_malformed_agent_tool_name(mutating_classify):
    with pytest.raises(ValueError):
        lwt.propose_mcp_action("ws1", "not_an_mcp_tool_name", {})


def test_propose_mcp_action_rejects_read_only_tool(read_only_classify):
    with pytest.raises(ValueError, match="read_only"):
        lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {"title": "bug"})


def test_propose_mcp_action_stores_pending_action(mutating_classify):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {"title": "bug"})
    assert action.source == "mcp"
    assert action.tool == AGENT_TOOL_NAME
    assert action.params == {"arguments": {"title": "bug"}}
    assert lwt.get_pending_action("ws1", action.action_id) is action


def test_propose_mcp_action_emits_mcp_tool_proposed(mutating_classify, captured_events):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {"title": "bug"})
    event_types = [e[0] for e in captured_events]
    assert EventType.MCP_TOOL_PROPOSED in event_types
    _, kwargs = next(e for e in captured_events if e[0] == EventType.MCP_TOOL_PROPOSED)
    assert kwargs["payload"]["server"] == "github"
    assert kwargs["payload"]["tool"] == "create_issue"
    assert kwargs["payload"]["action_id"] == action.action_id


# ---------------------------------------------------------------------
# confirm_action() -- mcp branch
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_confirm_mcp_action_calls_call_agent_mcp_tool(
    mutating_classify, mock_call_agent_mcp_tool
):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {"title": "bug"})
    result = await lwt.confirm_action("ws1", action.action_id)
    assert result == {"issue_number": 42}
    mock_call_agent_mcp_tool.assert_awaited_once_with(
        AGENT_TOOL_NAME, {"title": "bug"}, workspace_id="ws1"
    )


@pytest.mark.anyio
async def test_confirm_mcp_action_pops_pending_action_first(
    mutating_classify, mock_call_agent_mcp_tool
):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {})
    await lwt.confirm_action("ws1", action.action_id)
    with pytest.raises(lwt.PendingActionError):
        await lwt.confirm_action("ws1", action.action_id)


@pytest.mark.anyio
async def test_confirm_mcp_action_emits_mcp_tool_confirmed_not_local(
    mutating_classify, mock_call_agent_mcp_tool, captured_events
):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {})
    captured_events.clear()
    await lwt.confirm_action("ws1", action.action_id)
    event_types = [e[0] for e in captured_events]
    assert EventType.MCP_TOOL_CONFIRMED in event_types
    assert EventType.LOCAL_TOOL_CONFIRMED not in event_types


@pytest.mark.anyio
async def test_confirm_mcp_action_propagates_mcp_client_error(
    mutating_classify, monkeypatch
):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {})
    mock = AsyncMock(side_effect=MCPClientError("server unreachable"))
    monkeypatch.setattr(lwt.mcp_agent_tools, "call_agent_mcp_tool", mock)
    with pytest.raises(MCPClientError):
        await lwt.confirm_action("ws1", action.action_id)
    # popped even on failure, same "no silent retry of a stale
    # proposal" contract the daemon path already has
    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws1", action.action_id)


# ---------------------------------------------------------------------
# deny_action() -- mcp branch
# ---------------------------------------------------------------------

def test_deny_mcp_action_emits_mcp_tool_denied_and_never_calls_agent_tool(
    mutating_classify, mock_call_agent_mcp_tool, captured_events
):
    action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {})
    captured_events.clear()
    lwt.deny_action("ws1", action.action_id)
    event_types = [e[0] for e in captured_events]
    assert EventType.MCP_TOOL_DENIED in event_types
    assert EventType.LOCAL_TOOL_DENIED not in event_types
    mock_call_agent_mcp_tool.assert_not_awaited()
    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws1", action.action_id)


# ---------------------------------------------------------------------
# Shared store: daemon path is unaffected
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_daemon_action_still_defaults_to_daemon_source(monkeypatch):
    mock_call_daemon = AsyncMock(return_value={"written": True})
    monkeypatch.setattr(lwt, "call_daemon", mock_call_daemon)
    action = lwt.propose_action("ws1", "write_file", {"path": "a.txt", "content": "hi"})
    assert action.source == "daemon"
    result = await lwt.confirm_action("ws1", action.action_id)
    assert result == {"written": True}
    mock_call_daemon.assert_awaited_once()


def test_daemon_and_mcp_actions_coexist_in_one_pending_store(mutating_classify):
    daemon_action = lwt.propose_action("ws1", "delete", {"path": "a.txt"})
    mcp_action = lwt.propose_mcp_action("ws1", AGENT_TOOL_NAME, {})
    assert daemon_action.action_id != mcp_action.action_id
    assert lwt.get_pending_action("ws1", daemon_action.action_id).source == "daemon"
    assert lwt.get_pending_action("ws1", mcp_action.action_id).source == "mcp"
