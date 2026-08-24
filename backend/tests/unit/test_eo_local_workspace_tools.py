"""
tests/unit/test_eo_local_workspace_tools.py — Patch 7b.

eo/local_workspace_tools.py had zero test coverage before this. This is
the confirm-flow layer built on top of eo/local_workspace.py's
call_daemon() bridge (Patch 7a): list_workspace_dir()/read_workspace_file()
run straight through with no confirm step, while write_file/delete/
execute_command are gated behind propose_action() -> confirm_action()/
deny_action() so a mutating tool call never reaches the daemon on the
first ask.

Note on scope: the actual filesystem path-containment/sandboxing check
(stopping a workspace op from escaping its root via `../`, symlinks,
etc.) lives in daemon/path_guard.py, not here -- that module already has
its own coverage (daemon/tests/test_path_guard.py, wired into CI by
patch 3). This file is entirely about the propose/confirm/deny state
machine and the read-tool wrappers, confirmed by reading the module
directly rather than assumed from its name.

Style matches tests/unit/test_eo_local_workspace.py (Patch 7a):
call_daemon() itself is mocked out here (it's exhaustively covered by
Patch 7a already) so these tests isolate exactly this module's own
logic -- propose/confirm/deny bookkeeping, required-param validation,
TTL expiry, and the event-emission call sites -- without needing a real
daemon connection.
"""
import time
from unittest.mock import AsyncMock

import pytest

import eo.local_workspace_tools as lwt
from eo.local_workspace import ToolCallError

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_pending_actions():
    """_pending_actions is a module-level dict shared across every test
    in the process -- without this, a proposal left behind by one test
    (or a stale action_id) leaks into the next."""
    lwt._pending_actions.clear()
    yield
    lwt._pending_actions.clear()


@pytest.fixture
def mock_call_daemon(monkeypatch):
    """Replaces eo.local_workspace_tools.call_daemon (the name this
    module imported directly, `from eo.local_workspace import ...
    call_daemon` -- patching eo.local_workspace.call_daemon itself
    would NOT reach this bound copy) with an AsyncMock returning
    {"ok-result": True} by default."""
    mock = AsyncMock(return_value={"ok-result": True})
    monkeypatch.setattr(lwt, "call_daemon", mock)
    return mock


@pytest.fixture
def captured_events(monkeypatch):
    """Captures every emit_workspace_event() call this module makes,
    without needing real Pusher config -- same rationale as Patch 7a's
    test_forward_stream_chunk_emits_workspace_event."""
    events = []

    def fake_emit(event_type, workspace_id=None, agent=None, payload=None):
        events.append({
            "event_type": event_type, "workspace_id": workspace_id,
            "agent": agent, "payload": payload,
        })
        return True

    monkeypatch.setattr(lwt, "emit_workspace_event", fake_emit)
    return events


# ---------------------------------------------------------------------
# list_workspace_dir / read_workspace_file — the unconfirmed read path
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_workspace_dir_calls_daemon_and_returns_result(mock_call_daemon):
    mock_call_daemon.return_value = {"entries": ["a.txt", "b.txt"]}

    result = await lwt.list_workspace_dir("ws-1", "src")

    assert result == {"entries": ["a.txt", "b.txt"]}
    mock_call_daemon.assert_awaited_once_with("ws-1", "list_dir", {"path": "src"})


@pytest.mark.anyio
async def test_list_workspace_dir_defaults_path_to_dot(mock_call_daemon):
    await lwt.list_workspace_dir("ws-1")
    mock_call_daemon.assert_awaited_once_with("ws-1", "list_dir", {"path": "."})


@pytest.mark.anyio
async def test_list_workspace_dir_emits_executed_then_result_events(mock_call_daemon, captured_events):
    await lwt.list_workspace_dir("ws-1", "src")

    assert [e["event_type"] for e in captured_events] == [
        lwt.EventType.LOCAL_TOOL_EXECUTED, lwt.EventType.LOCAL_TOOL_RESULT,
    ]
    assert captured_events[0]["payload"] == {"tool": "list_dir", "path": "src"}
    assert captured_events[1]["payload"]["ok"] is True


@pytest.mark.anyio
async def test_list_workspace_dir_propagates_and_logs_tool_call_error(mock_call_daemon, captured_events):
    mock_call_daemon.side_effect = ToolCallError("no daemon connected")

    with pytest.raises(ToolCallError, match="no daemon connected"):
        await lwt.list_workspace_dir("ws-1", "src")

    assert captured_events[-1]["event_type"] == lwt.EventType.LOCAL_TOOL_RESULT
    assert captured_events[-1]["payload"]["ok"] is False
    assert captured_events[-1]["payload"]["error"] == "no daemon connected"


@pytest.mark.anyio
async def test_read_workspace_file_calls_daemon_and_returns_result(mock_call_daemon):
    mock_call_daemon.return_value = {"content": "hello world"}

    result = await lwt.read_workspace_file("ws-1", "README.md")

    assert result == {"content": "hello world"}
    mock_call_daemon.assert_awaited_once_with("ws-1", "read_file", {"path": "README.md"})


@pytest.mark.anyio
async def test_read_workspace_file_propagates_tool_call_error(mock_call_daemon, captured_events):
    mock_call_daemon.side_effect = ToolCallError("file not found")

    with pytest.raises(ToolCallError, match="file not found"):
        await lwt.read_workspace_file("ws-1", "missing.txt")

    assert captured_events[-1]["payload"]["error"] == "file not found"


# ---------------------------------------------------------------------
# _tool_event_payload / _preview — event payload shaping
# ---------------------------------------------------------------------

def test_preview_truncates_long_strings():
    long_value = "x" * 300
    result = lwt._preview(long_value)
    assert result == "x" * 200 + "…"


def test_preview_passes_short_strings_and_non_strings_through():
    assert lwt._preview("short") == "short"
    assert lwt._preview(42) == 42
    assert lwt._preview(None) is None
    assert lwt._preview(True) is True


def test_tool_event_payload_includes_path_and_command_when_present():
    payload = lwt._tool_event_payload("execute_command", {"command": "pytest -q"})
    assert payload == {"tool": "execute_command", "command": "pytest -q"}


def test_tool_event_payload_write_file_reports_content_byte_count_not_content():
    payload = lwt._tool_event_payload("write_file", {"path": "a.py", "content": "hello"})
    assert payload["content_bytes"] == 5
    assert "content" not in payload


def test_tool_event_payload_write_file_handles_multibyte_content():
    # UTF-8 byte length, not character length -- e.g. "é" is 2 bytes.
    payload = lwt._tool_event_payload("write_file", {"path": "a.py", "content": "héllo"})
    assert payload["content_bytes"] == len("héllo".encode())


def test_tool_event_payload_write_file_handles_non_string_content():
    payload = lwt._tool_event_payload("write_file", {"path": "a.py", "content": None})
    assert payload["content_bytes"] is None


def test_tool_event_payload_never_leaks_full_content_for_non_write_tools():
    payload = lwt._tool_event_payload("delete", {"path": "a.py"})
    assert "content_bytes" not in payload
    assert "content" not in payload


# ---------------------------------------------------------------------
# propose_action
# ---------------------------------------------------------------------

def test_propose_action_write_file_succeeds_with_required_params(captured_events):
    action = lwt.propose_action("ws-1", "write_file", {"path": "a.py", "content": "x = 1"})

    assert action.workspace_id == "ws-1"
    assert action.tool == "write_file"
    assert action.params == {"path": "a.py", "content": "x = 1"}
    assert action.action_id in lwt._pending_actions
    assert lwt._pending_actions[action.action_id] is action


def test_propose_action_delete_succeeds_with_required_params():
    action = lwt.propose_action("ws-1", "delete", {"path": "old.py"})
    assert action.tool == "delete"


def test_propose_action_execute_command_succeeds_with_required_params():
    action = lwt.propose_action("ws-1", "execute_command", {"command": "pytest -q"})
    assert action.tool == "execute_command"


def test_propose_action_rejects_unknown_tool():
    with pytest.raises(ValueError, match="not a tool that goes through propose/confirm"):
        lwt.propose_action("ws-1", "list_dir", {"path": "."})
    assert lwt._pending_actions == {}


def test_propose_action_rejects_missing_required_param():
    with pytest.raises(ValueError, match="missing required param.*path"):
        lwt.propose_action("ws-1", "delete", {})
    assert lwt._pending_actions == {}


def test_propose_action_rejects_partially_missing_params():
    with pytest.raises(ValueError, match="content"):
        lwt.propose_action("ws-1", "write_file", {"path": "a.py"})


def test_propose_action_reports_all_missing_params_together():
    with pytest.raises(ValueError) as exc_info:
        lwt.propose_action("ws-1", "write_file", {})
    assert "path" in str(exc_info.value)
    assert "content" in str(exc_info.value)


def test_propose_action_treats_none_params_as_empty():
    with pytest.raises(ValueError, match="missing required param"):
        lwt.propose_action("ws-1", "delete", None)


def test_propose_action_generates_unique_action_ids():
    a1 = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    a2 = lwt.propose_action("ws-1", "delete", {"path": "b.py"})
    assert a1.action_id != a2.action_id
    assert len(lwt._pending_actions) == 2


def test_propose_action_emits_proposed_event(captured_events):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["event_type"] == lwt.EventType.LOCAL_TOOL_PROPOSED
    assert event["workspace_id"] == "ws-1"
    assert event["payload"]["action_id"] == action.action_id


def test_propose_action_does_not_touch_the_daemon(mock_call_daemon):
    lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    mock_call_daemon.assert_not_awaited()


def test_propose_action_copies_params_defensively():
    """Mutating the caller's dict after proposing must not affect the
    stored action -- propose_action() does dict(params or {})."""
    params = {"path": "a.py"}
    action = lwt.propose_action("ws-1", "delete", params)
    params["path"] = "mutated.py"
    assert action.params == {"path": "a.py"}


# ---------------------------------------------------------------------
# get_pending_action
# ---------------------------------------------------------------------

def test_get_pending_action_returns_matching_action():
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    fetched = lwt.get_pending_action("ws-1", action.action_id)
    assert fetched is action


def test_get_pending_action_raises_for_unknown_action_id():
    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws-1", "does-not-exist")


def test_get_pending_action_raises_when_workspace_id_does_not_match():
    """An action_id belonging to a different workspace must not be
    fetchable by a caller claiming a different workspace_id -- action_id
    alone (a uuid4) would otherwise let one workspace poke at another's
    pending action just by guessing/reusing an id."""
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws-2", action.action_id)


def test_get_pending_action_raises_for_expired_action(monkeypatch):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    # Push its created_at far enough into the past to be past the TTL.
    action.created_at = time.time() - lwt.PENDING_ACTION_TTL_SECONDS - 1

    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws-1", action.action_id)


def test_get_pending_action_prunes_expired_entries_as_a_side_effect():
    fresh = lwt.propose_action("ws-1", "delete", {"path": "fresh.py"})
    stale = lwt.propose_action("ws-1", "delete", {"path": "stale.py"})
    stale.created_at = time.time() - lwt.PENDING_ACTION_TTL_SECONDS - 1

    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws-1", stale.action_id)

    # The lookup's own _prune_expired() call should have swept the
    # expired entry out of the store entirely, not just rejected it.
    assert stale.action_id not in lwt._pending_actions
    assert fresh.action_id in lwt._pending_actions


def test_get_pending_action_does_not_prune_fresh_entries():
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    lwt.get_pending_action("ws-1", action.action_id)
    assert action.action_id in lwt._pending_actions


# ---------------------------------------------------------------------
# confirm_action
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_confirm_action_calls_daemon_with_proposed_tool_and_params(mock_call_daemon):
    action = lwt.propose_action("ws-1", "write_file", {"path": "a.py", "content": "x = 1"})

    await lwt.confirm_action("ws-1", action.action_id)

    mock_call_daemon.assert_awaited_once_with(
        "ws-1", "write_file", {"path": "a.py", "content": "x = 1"}, action_id=action.action_id,
    )


@pytest.mark.anyio
async def test_confirm_action_returns_daemon_result(mock_call_daemon):
    mock_call_daemon.return_value = {"bytes_written": 42}
    action = lwt.propose_action("ws-1", "write_file", {"path": "a.py", "content": "x = 1"})

    result = await lwt.confirm_action("ws-1", action.action_id)

    assert result == {"bytes_written": 42}


@pytest.mark.anyio
async def test_confirm_action_pops_the_pending_action_before_calling_daemon(mock_call_daemon):
    """Popped before the daemon call so a second confirm attempt on the
    same action_id can't retry it (the action is already gone even if
    the daemon call is still in flight when a second confirm arrives)."""
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})

    async def assert_already_popped(*args, **kwargs):
        assert action.action_id not in lwt._pending_actions
        return {"ok": True}

    mock_call_daemon.side_effect = assert_already_popped

    await lwt.confirm_action("ws-1", action.action_id)


@pytest.mark.anyio
async def test_confirm_action_raises_for_unknown_action_id(mock_call_daemon):
    with pytest.raises(lwt.PendingActionError):
        await lwt.confirm_action("ws-1", "does-not-exist")
    mock_call_daemon.assert_not_awaited()


@pytest.mark.anyio
async def test_confirm_action_cannot_be_confirmed_twice(mock_call_daemon):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    await lwt.confirm_action("ws-1", action.action_id)

    with pytest.raises(lwt.PendingActionError):
        await lwt.confirm_action("ws-1", action.action_id)
    mock_call_daemon.assert_awaited_once()  # only the first confirm reached the daemon


@pytest.mark.anyio
async def test_confirm_action_propagates_tool_call_error_and_action_stays_gone(mock_call_daemon):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    mock_call_daemon.side_effect = ToolCallError("permission denied")

    with pytest.raises(ToolCallError, match="permission denied"):
        await lwt.confirm_action("ws-1", action.action_id)

    # Per the module's own docstring: even though the daemon call
    # failed, the action must NOT be retryable via a second confirm --
    # the caller re-proposes instead.
    with pytest.raises(lwt.PendingActionError):
        await lwt.confirm_action("ws-1", action.action_id)


@pytest.mark.anyio
async def test_confirm_action_emits_confirmed_then_result_events(mock_call_daemon, captured_events):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    captured_events.clear()  # drop the PROPOSED event from propose_action above

    await lwt.confirm_action("ws-1", action.action_id)

    assert [e["event_type"] for e in captured_events] == [
        lwt.EventType.LOCAL_TOOL_CONFIRMED, lwt.EventType.LOCAL_TOOL_RESULT,
    ]
    assert captured_events[0]["payload"]["action_id"] == action.action_id
    assert captured_events[1]["payload"]["ok"] is True


@pytest.mark.anyio
async def test_confirm_action_emits_failed_result_event_on_tool_call_error(mock_call_daemon, captured_events):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    captured_events.clear()
    mock_call_daemon.side_effect = ToolCallError("timed out")

    with pytest.raises(ToolCallError):
        await lwt.confirm_action("ws-1", action.action_id)

    result_event = captured_events[-1]
    assert result_event["event_type"] == lwt.EventType.LOCAL_TOOL_RESULT
    assert result_event["payload"]["ok"] is False
    assert result_event["payload"]["error"] == "timed out"


@pytest.mark.anyio
async def test_confirm_action_wrong_workspace_id_is_rejected(mock_call_daemon):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    with pytest.raises(lwt.PendingActionError):
        await lwt.confirm_action("ws-2", action.action_id)
    mock_call_daemon.assert_not_awaited()


# ---------------------------------------------------------------------
# deny_action
# ---------------------------------------------------------------------

def test_deny_action_removes_pending_action(captured_events):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    lwt.deny_action("ws-1", action.action_id)
    assert action.action_id not in lwt._pending_actions


def test_deny_action_never_touches_the_daemon(mock_call_daemon):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    lwt.deny_action("ws-1", action.action_id)
    mock_call_daemon.assert_not_awaited()


def test_deny_action_emits_denied_event(captured_events):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    captured_events.clear()

    lwt.deny_action("ws-1", action.action_id)

    assert len(captured_events) == 1
    assert captured_events[0]["event_type"] == lwt.EventType.LOCAL_TOOL_DENIED
    assert captured_events[0]["payload"]["action_id"] == action.action_id


def test_deny_action_raises_for_unknown_action_id():
    with pytest.raises(lwt.PendingActionError):
        lwt.deny_action("ws-1", "does-not-exist")


def test_deny_action_cannot_be_denied_twice():
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    lwt.deny_action("ws-1", action.action_id)
    with pytest.raises(lwt.PendingActionError):
        lwt.deny_action("ws-1", action.action_id)


def test_denied_action_cannot_then_be_confirmed(mock_call_daemon):
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    lwt.deny_action("ws-1", action.action_id)
    with pytest.raises(lwt.PendingActionError):
        lwt.get_pending_action("ws-1", action.action_id)
    mock_call_daemon.assert_not_awaited()


def test_deny_action_wrong_workspace_id_is_rejected():
    action = lwt.propose_action("ws-1", "delete", {"path": "a.py"})
    with pytest.raises(lwt.PendingActionError):
        lwt.deny_action("ws-2", action.action_id)
    # Still pending under the correct workspace -- denial under the
    # wrong workspace must not have silently discarded it.
    assert action.action_id in lwt._pending_actions


# ---------------------------------------------------------------------
# local_workspace_tools — the OpenAI tool-schema builder
# ---------------------------------------------------------------------

def test_local_workspace_tools_only_exposes_read_tools():
    schemas = lwt.local_workspace_tools()
    names = [s["function"]["name"] for s in schemas]
    assert names == ["list_local_dir", "read_local_file"]


def test_local_workspace_tools_never_exposes_mutating_tools():
    """Part 4 deliberately does NOT add write/delete/execute_command
    schemas here -- those need the propose/confirm UI, not a tool call
    that appears to complete in one shot."""
    schemas = lwt.local_workspace_tools()
    names = {s["function"]["name"] for s in schemas}
    assert "write_local_file" not in names
    assert "delete_local_file" not in names
    assert "execute_local_command" not in names


def test_local_workspace_tools_read_file_requires_path():
    schemas = lwt.local_workspace_tools()
    read_file_schema = next(s for s in schemas if s["function"]["name"] == "read_local_file")
    assert read_file_schema["function"]["parameters"]["required"] == ["path"]


def test_local_workspace_tools_list_dir_path_is_optional():
    schemas = lwt.local_workspace_tools()
    list_dir_schema = next(s for s in schemas if s["function"]["name"] == "list_local_dir")
    assert list_dir_schema["function"]["parameters"]["required"] == []


def test_local_workspace_tools_returns_well_formed_openai_schema():
    for schema in lwt.local_workspace_tools():
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert "description" in schema["function"]
        assert schema["function"]["parameters"]["type"] == "object"
