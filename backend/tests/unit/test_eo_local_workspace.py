"""
tests/unit/test_eo_local_workspace.py — Patch 7a.

eo/local_workspace.py had zero test coverage before this: the pairing
handshake (token check + PairingError vs. plain auth failure), the
_connections registry (register/supersede/unregister, including the
supersession race where a stale connection's own `finally` block must
not evict the connection that replaced it), the _pending call-tracking
dict (_resolve_pending, _fail_pending_for_workspace), and call_daemon()
itself (the send-request/await-response half of the protocol, including
its timeout and daemon-reported-failure paths).

Two test styles here, split by what each thing actually is:
  - Most of this module is plain async functions and module-level dicts
    -- those are exercised directly (no FastAPI/ASGI machinery needed),
    using FakeWebSocket as a minimal async double for the one or two
    methods (`send_json`, `close`) these functions actually call.
  - daemon_endpoint itself IS the ASGI websocket route, so its handshake
    branches (bad first message / wrong token / unconfigured pairing
    token / success) are exercised through a real TestClient websocket
    connection against a throwaway FastAPI app mounting just this
    router, rather than reimplemented as a plain function call.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import eo.local_workspace as lw

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_module_state():
    """_connections/_pending are module-level dicts shared across every
    test in the process -- without this, a connection or pending call
    left behind by one test leaks into the next (e.g. is_live("ws-1")
    coming back True in a test that never registered anything)."""
    lw._connections.clear()
    lw._pending.clear()
    yield
    lw._connections.clear()
    lw._pending.clear()


@pytest.fixture
def pairing_token(monkeypatch):
    token = "test-pairing-token-123"
    monkeypatch.setenv("MINIME_PAIRING_TOKEN", token)
    return token


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeWebSocket:
    """Minimal async double for starlette.websockets.WebSocket, covering
    only what this module's internal helpers (_register, call_daemon)
    actually call on it: .send_json() and .close(). Good enough for
    unit-level tests of those helpers in isolation -- the full
    daemon_endpoint handshake (accept/receive_json, actually wired to
    Starlette's ASGI lifecycle) is exercised through a real TestClient
    connection further down instead."""

    def __init__(self):
        self.sent = []
        self.closed_with = None

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed_with = code


# ---------------------------------------------------------------------
# _expected_token / _check_token
# ---------------------------------------------------------------------

def test_expected_token_raises_pairing_error_when_unset(monkeypatch):
    monkeypatch.delenv("MINIME_PAIRING_TOKEN", raising=False)
    with pytest.raises(lw.PairingError):
        lw._expected_token()


def test_expected_token_raises_when_blank(monkeypatch):
    monkeypatch.setenv("MINIME_PAIRING_TOKEN", "   ")
    with pytest.raises(lw.PairingError):
        lw._expected_token()


def test_expected_token_returns_stripped_value(monkeypatch):
    monkeypatch.setenv("MINIME_PAIRING_TOKEN", "  abc123  ")
    assert lw._expected_token() == "abc123"


def test_check_token_accepts_matching_token(pairing_token):
    assert lw._check_token(pairing_token) is True


def test_check_token_rejects_wrong_token(pairing_token):
    assert lw._check_token("wrong-token") is False


def test_check_token_rejects_empty_string(pairing_token):
    assert lw._check_token("") is False


def test_check_token_raises_pairing_error_when_unconfigured(monkeypatch):
    monkeypatch.delenv("MINIME_PAIRING_TOKEN", raising=False)
    with pytest.raises(lw.PairingError):
        lw._check_token("anything")


# ---------------------------------------------------------------------
# is_live / get
# ---------------------------------------------------------------------

def test_is_live_false_and_get_none_when_no_connection():
    assert lw.is_live("ws-1") is False
    assert lw.get("ws-1") is None


@pytest.mark.anyio
async def test_is_live_and_get_after_register():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)
    assert lw.is_live("ws-1") is True
    assert lw.get("ws-1") is ws


# ---------------------------------------------------------------------
# _register (including supersession)
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_register_new_workspace_stores_connection():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)
    assert lw._connections["ws-1"] is ws


@pytest.mark.anyio
async def test_register_supersedes_existing_connection():
    old_ws = FakeWebSocket()
    new_ws = FakeWebSocket()
    await lw._register("ws-1", old_ws)
    await lw._register("ws-1", new_ws)
    assert lw._connections["ws-1"] is new_ws
    assert old_ws.closed_with == 4409


@pytest.mark.anyio
async def test_register_same_socket_twice_is_a_noop_close():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)
    await lw._register("ws-1", ws)  # re-registering the identical socket
    assert ws.closed_with is None
    assert lw._connections["ws-1"] is ws


@pytest.mark.anyio
async def test_register_tolerates_close_failure_on_superseded_socket():
    class BrokenCloseWebSocket(FakeWebSocket):
        async def close(self, code=1000):
            raise RuntimeError("already gone")

    old_ws = BrokenCloseWebSocket()
    new_ws = FakeWebSocket()
    await lw._register("ws-1", old_ws)
    # Must not propagate even though old_ws.close() blows up -- the new
    # connection still needs to win the dict entry.
    await lw._register("ws-1", new_ws)
    assert lw._connections["ws-1"] is new_ws


@pytest.mark.anyio
async def test_register_is_isolated_per_workspace():
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    await lw._register("ws-1", ws_a)
    await lw._register("ws-2", ws_b)
    assert lw.get("ws-1") is ws_a
    assert lw.get("ws-2") is ws_b
    assert ws_a.closed_with is None
    assert ws_b.closed_with is None


# ---------------------------------------------------------------------
# _unregister
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_unregister_removes_current_connection():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)
    lw._unregister("ws-1", ws)
    assert lw.is_live("ws-1") is False


@pytest.mark.anyio
async def test_unregister_ignores_stale_reference_after_supersession():
    old_ws = FakeWebSocket()
    new_ws = FakeWebSocket()
    await lw._register("ws-1", old_ws)
    await lw._register("ws-1", new_ws)  # old_ws is now superseded

    # Simulates old_ws's own `finally` block firing after it was already
    # replaced -- must NOT evict the connection that superseded it.
    lw._unregister("ws-1", old_ws)

    assert lw.get("ws-1") is new_ws


def test_unregister_noop_for_unknown_workspace():
    ws = FakeWebSocket()
    lw._unregister("never-registered", ws)  # must not raise


# ---------------------------------------------------------------------
# _fail_pending_for_workspace
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_fail_pending_for_workspace_fails_only_matching_entries():
    loop = asyncio.get_running_loop()
    fut_a = loop.create_future()
    fut_b = loop.create_future()
    lw._pending["req-a"] = lw._PendingCall("ws-1", fut_a, None)
    lw._pending["req-b"] = lw._PendingCall("ws-2", fut_b, None)

    lw._fail_pending_for_workspace("ws-1", "daemon disconnected")

    assert fut_a.done()
    with pytest.raises(lw.ToolCallError, match="daemon disconnected"):
        fut_a.result()
    assert not fut_b.done()
    assert "req-a" not in lw._pending
    assert "req-b" in lw._pending


@pytest.mark.anyio
async def test_fail_pending_for_workspace_does_not_clobber_done_future():
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"ok": True})
    lw._pending["req-a"] = lw._PendingCall("ws-1", fut, None)

    # Must not raise InvalidStateError trying to set an exception on an
    # already-resolved future.
    lw._fail_pending_for_workspace("ws-1", "daemon disconnected")

    assert fut.result() == {"ok": True}


def test_fail_pending_for_workspace_noop_when_nothing_pending():
    lw._fail_pending_for_workspace("ws-1", "daemon disconnected")  # must not raise


# ---------------------------------------------------------------------
# _forward_stream_chunk
# ---------------------------------------------------------------------

def test_forward_stream_chunk_emits_workspace_event(monkeypatch):
    captured = {}

    def fake_emit(event_type, workspace_id=None, agent=None, payload=None):
        captured.update(
            event_type=event_type, workspace_id=workspace_id,
            agent=agent, payload=payload,
        )
        return True

    monkeypatch.setattr(lw, "emit_workspace_event", fake_emit)

    lw._forward_stream_chunk(
        "ws-1", "action-42", {"stream": "stdout", "chunk": "hello\n", "type": "tool_stream"},
    )

    assert captured["event_type"] == lw.EventType.LOCAL_TOOL_STREAM_CHUNK
    assert captured["workspace_id"] == "ws-1"
    assert captured["agent"] == "local_workspace"
    assert captured["payload"] == {
        "action_id": "action-42",
        "stream": "stdout",
        "chunk": "hello\n",
    }


def test_forward_stream_chunk_handles_none_action_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        lw, "emit_workspace_event",
        lambda event_type, workspace_id=None, agent=None, payload=None: captured.update(payload=payload),
    )
    lw._forward_stream_chunk("ws-1", None, {"stream": "stderr", "chunk": "oops"})
    assert captured["payload"]["action_id"] is None


# ---------------------------------------------------------------------
# _resolve_pending
# ---------------------------------------------------------------------

def test_resolve_pending_ignores_non_dict_message():
    assert lw._resolve_pending("not a dict") is False
    assert lw._resolve_pending(None) is False
    assert lw._resolve_pending([1, 2, 3]) is False


def test_resolve_pending_ignores_unrelated_message_type():
    assert lw._resolve_pending({"type": "hello"}) is False
    assert lw._resolve_pending({"type": "hello_ack"}) is False
    assert lw._resolve_pending({}) is False


@pytest.mark.anyio
async def test_resolve_pending_resolves_matching_tool_result():
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    lw._pending["req-1"] = lw._PendingCall("ws-1", fut, None)

    msg = {"type": "tool_result", "request_id": "req-1", "ok": True, "result": {"x": 1}}
    handled = lw._resolve_pending(msg)

    assert handled is True
    assert fut.done()
    assert fut.result() == msg


@pytest.mark.anyio
async def test_resolve_pending_does_not_clobber_already_done_future():
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result({"already": "resolved"})
    lw._pending["req-1"] = lw._PendingCall("ws-1", fut, None)

    handled = lw._resolve_pending({"type": "tool_result", "request_id": "req-1", "ok": True})

    assert handled is True
    assert fut.result() == {"already": "resolved"}


def test_resolve_pending_discards_tool_result_for_unknown_request_id():
    handled = lw._resolve_pending({"type": "tool_result", "request_id": "does-not-exist", "ok": True})
    assert handled is True  # still "handled" (logged + discarded), not treated as unrelated


def test_resolve_pending_forwards_matching_tool_stream(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lw, "_forward_stream_chunk",
        lambda workspace_id, action_id, msg: calls.append((workspace_id, action_id, msg)),
    )
    lw._pending["req-1"] = lw._PendingCall("ws-1", MagicMock(), "action-1")

    msg = {"type": "tool_stream", "request_id": "req-1", "stream": "stdout", "chunk": "x"}
    handled = lw._resolve_pending(msg)

    assert handled is True
    assert calls == [("ws-1", "action-1", msg)]


def test_resolve_pending_discards_tool_stream_for_unknown_request_id():
    handled = lw._resolve_pending({"type": "tool_stream", "request_id": "does-not-exist", "stream": "stdout", "chunk": "x"})
    assert handled is True


@pytest.mark.anyio
async def test_resolve_pending_tool_stream_never_resolves_the_future(monkeypatch):
    monkeypatch.setattr(lw, "_forward_stream_chunk", lambda *a, **k: None)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    lw._pending["req-1"] = lw._PendingCall("ws-1", fut, "action-1")

    lw._resolve_pending({"type": "tool_stream", "request_id": "req-1", "stream": "stdout", "chunk": "x"})

    assert not fut.done()
    assert "req-1" in lw._pending  # a stream chunk must not pop the pending entry either


# ---------------------------------------------------------------------
# call_daemon
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_daemon_raises_when_no_daemon_connected():
    with pytest.raises(lw.ToolCallError, match="no daemon is currently connected"):
        await lw.call_daemon("ws-1", "list_dir", {"path": "."})


@pytest.mark.anyio
async def test_call_daemon_happy_path_returns_result():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    async def respond_once_sent():
        while not ws.sent:
            await asyncio.sleep(0)
        request_id = ws.sent[0]["request_id"]
        lw._resolve_pending({
            "type": "tool_result", "request_id": request_id,
            "ok": True, "result": {"entries": ["a.txt"]},
        })

    result, _ = await asyncio.gather(
        lw.call_daemon("ws-1", "list_dir", {"path": "."}),
        respond_once_sent(),
    )

    assert result == {"entries": ["a.txt"]}
    assert ws.sent[0] == {
        "type": "tool_call",
        "request_id": ws.sent[0]["request_id"],
        "tool": "list_dir",
        "params": {"path": "."},
    }
    assert lw._pending == {}  # cleaned up after completion


@pytest.mark.anyio
async def test_call_daemon_missing_result_key_returns_empty_dict():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    async def respond_once_sent():
        while not ws.sent:
            await asyncio.sleep(0)
        request_id = ws.sent[0]["request_id"]
        lw._resolve_pending({"type": "tool_result", "request_id": request_id, "ok": True})

    result, _ = await asyncio.gather(
        lw.call_daemon("ws-1", "list_dir", {"path": "."}),
        respond_once_sent(),
    )
    assert result == {}


@pytest.mark.anyio
async def test_call_daemon_raises_on_daemon_reported_failure():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    async def respond_once_sent():
        while not ws.sent:
            await asyncio.sleep(0)
        request_id = ws.sent[0]["request_id"]
        lw._resolve_pending({
            "type": "tool_result", "request_id": request_id,
            "ok": False, "error": "permission denied",
        })

    with pytest.raises(lw.ToolCallError, match="permission denied"):
        await asyncio.gather(
            lw.call_daemon("ws-1", "delete", {"path": "/etc/passwd"}),
            respond_once_sent(),
        )
    assert lw._pending == {}


@pytest.mark.anyio
async def test_call_daemon_raises_generic_error_when_failure_has_no_message():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    async def respond_once_sent():
        while not ws.sent:
            await asyncio.sleep(0)
        request_id = ws.sent[0]["request_id"]
        lw._resolve_pending({"type": "tool_result", "request_id": request_id, "ok": False})

    with pytest.raises(lw.ToolCallError, match="failed with no error message"):
        await asyncio.gather(
            lw.call_daemon("ws-1", "delete", {"path": "x"}),
            respond_once_sent(),
        )


@pytest.mark.anyio
async def test_call_daemon_times_out_and_cleans_up_pending():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    with pytest.raises(lw.ToolCallError, match="did not respond"):
        await lw.call_daemon("ws-1", "list_dir", {"path": "."}, timeout=0.05)

    assert lw._pending == {}  # timeout must still clean up the pending entry


@pytest.mark.anyio
async def test_call_daemon_tracks_action_id_while_in_flight():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    task = asyncio.create_task(
        lw.call_daemon("ws-1", "execute_command", {"cmd": "ls"}, action_id="action-9")
    )
    try:
        while not ws.sent:
            await asyncio.sleep(0)
        request_id = ws.sent[0]["request_id"]
        assert lw._pending[request_id].action_id == "action-9"
    finally:
        lw._resolve_pending({"type": "tool_result", "request_id": ws.sent[0]["request_id"], "ok": True, "result": {}})
        await task


@pytest.mark.anyio
async def test_call_daemon_read_tools_default_action_id_to_none():
    ws = FakeWebSocket()
    await lw._register("ws-1", ws)

    task = asyncio.create_task(lw.call_daemon("ws-1", "list_dir", {"path": "."}))
    try:
        while not ws.sent:
            await asyncio.sleep(0)
        request_id = ws.sent[0]["request_id"]
        assert lw._pending[request_id].action_id is None
    finally:
        lw._resolve_pending({"type": "tool_result", "request_id": ws.sent[0]["request_id"], "ok": True, "result": {}})
        await task


# ---------------------------------------------------------------------
# daemon_endpoint — full handshake, through a real websocket connection
# ---------------------------------------------------------------------

@pytest.fixture
def daemon_app():
    app = FastAPI()
    app.include_router(lw.router)
    return app


def test_daemon_endpoint_closes_on_non_hello_first_message(daemon_app, pairing_token):
    client = TestClient(daemon_app)
    with client.websocket_connect("/ws/daemon/ws-1") as ws:
        ws.send_json({"type": "not_hello"})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4400
    assert lw.is_live("ws-1") is False


def test_daemon_endpoint_closes_on_wrong_pairing_token(daemon_app, pairing_token):
    client = TestClient(daemon_app)
    with client.websocket_connect("/ws/daemon/ws-1") as ws:
        ws.send_json({"type": "hello", "pairing_token": "wrong-token"})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4401
    assert lw.is_live("ws-1") is False


def test_daemon_endpoint_closes_when_server_unconfigured(daemon_app, monkeypatch):
    monkeypatch.delenv("MINIME_PAIRING_TOKEN", raising=False)
    client = TestClient(daemon_app)
    with client.websocket_connect("/ws/daemon/ws-1") as ws:
        ws.send_json({"type": "hello", "pairing_token": "anything"})
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 4500


def test_daemon_endpoint_accepts_correct_token_and_registers(daemon_app, pairing_token):
    client = TestClient(daemon_app)
    with client.websocket_connect("/ws/daemon/ws-1") as ws:
        ws.send_json({"type": "hello", "pairing_token": pairing_token})
        ack = ws.receive_json()
        assert ack == {"type": "hello_ack", "workspace_id": "ws-1"}
        assert lw.is_live("ws-1") is True

    # Connection is torn down once the `with` block exits (client
    # disconnects); the endpoint's own `finally` should unregister it.
    assert lw.is_live("ws-1") is False


@pytest.mark.anyio
async def test_daemon_endpoint_disconnect_fails_pending_calls(daemon_app, pairing_token):
    # TestClient's websocket session runs the ASGI app on its own thread
    # via an anyio portal, so it works the same whether called from a
    # sync or async test -- marked async here purely so
    # asyncio.get_running_loop() below has a loop to attach the Future
    # to (this test's own thread has none by default, unlike the sync
    # tests above which never need to construct one directly).
    client = TestClient(daemon_app)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    with client.websocket_connect("/ws/daemon/ws-1") as ws:
        ws.send_json({"type": "hello", "pairing_token": pairing_token})
        ws.receive_json()  # hello_ack

        # Simulate a call_daemon() in flight for this workspace when the
        # daemon drops -- daemon_endpoint's `finally` should fail it via
        # _fail_pending_for_workspace rather than leaving it hanging.
        lw._pending["req-in-flight"] = lw._PendingCall("ws-1", fut, None)

    assert fut.done()
    with pytest.raises(lw.ToolCallError, match="disconnected"):
        fut.result()
