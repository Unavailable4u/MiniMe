"""
tests/unit/test_eo_ws_registry.py — Patch 7e-2.

eo/ws_registry.py had zero test coverage before this. This is the
bridge that lets notify() -- called synchronously from deep inside
sync agent code running on FastAPI's threadpool, not the event loop
-- actually reach a live WebSocket. A bug here either drops real-time
UI updates silently (push() swallowing everything when it shouldn't)
or crashes the calling agent code (an unguarded broadcast failure
propagating back through notify()'s call site, which the module's own
_broadcast() docstring says must never happen).

Isolation: _connections and _loop are plain module-level globals, not
something ws_registry.py exposes a reset for -- an autouse fixture in
this file resets both before every test so state from one test can't
leak into the next (same class of leak tests/conftest.py's own
_reset_role_prompts_cache/_reset_app_slug_context fixtures guard
against for other modules).

_broadcast() is a coroutine, scheduled via a real running event loop
(asyncio.run_coroutine_threadsafe) -- tests that exercise it directly
use @pytest.mark.anyio, matching the pattern tests/unit/
test_eo_local_workspace.py already establishes for testing async eo/
code in this repo (its own local `anyio_backend` fixture, reused here
rather than invented fresh).
"""
import asyncio

import pytest

import eo.ws_registry as ws_registry


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_registry_state():
    ws_registry._connections.clear()
    ws_registry._loop = None
    yield
    ws_registry._connections.clear()
    ws_registry._loop = None


class FakeWebSocket:
    """Minimal async double -- just enough surface for _broadcast()
    (send_json) and hashability (ws_registry stores these in a set)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_json(self, event):
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(event)


# ---------------------------------------------------------------------
# register / unregister
# ---------------------------------------------------------------------

def test_register_adds_the_socket_for_a_new_session():
    ws = FakeWebSocket()
    ws_registry.register("sess-1", ws)
    assert ws_registry._connections["sess-1"] == {ws}


def test_register_allows_multiple_sockets_for_the_same_session():
    """The same chat open in two tabs -- both must be tracked, not
    the second overwriting the first."""
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    ws_registry.register("sess-1", ws_a)
    ws_registry.register("sess-1", ws_b)
    assert ws_registry._connections["sess-1"] == {ws_a, ws_b}


def test_unregister_removes_one_socket_but_keeps_the_session_entry_alive():
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    ws_registry.register("sess-1", ws_a)
    ws_registry.register("sess-1", ws_b)

    ws_registry.unregister("sess-1", ws_a)

    assert ws_registry._connections["sess-1"] == {ws_b}


def test_unregister_last_socket_drops_the_session_key_entirely():
    ws = FakeWebSocket()
    ws_registry.register("sess-1", ws)

    ws_registry.unregister("sess-1", ws)

    assert "sess-1" not in ws_registry._connections


def test_unregister_unknown_session_is_a_noop():
    ws = FakeWebSocket()
    ws_registry.unregister("never-registered", ws)  # must not raise


def test_unregister_a_socket_not_in_the_set_is_a_noop():
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    ws_registry.register("sess-1", ws_a)

    ws_registry.unregister("sess-1", ws_b)  # never registered, must not raise

    assert ws_registry._connections["sess-1"] == {ws_a}


# ---------------------------------------------------------------------
# set_event_loop
# ---------------------------------------------------------------------

def test_set_event_loop_stores_the_loop_globally():
    sentinel_loop = object()
    ws_registry.set_event_loop(sentinel_loop)
    assert ws_registry._loop is sentinel_loop


# ---------------------------------------------------------------------
# push
# ---------------------------------------------------------------------

def test_push_is_a_noop_when_no_event_loop_has_been_registered(monkeypatch):
    """No app-started event loop yet -- push() must silently drop the
    event rather than raising (documented no-op posture)."""
    called = []
    monkeypatch.setattr(
        asyncio, "run_coroutine_threadsafe",
        lambda coro, loop: called.append((coro, loop)),
    )
    ws_registry.push({"kind": "note_added", "session_id": "sess-1"})
    assert called == []


def test_push_schedules_broadcast_onto_the_registered_loop(monkeypatch):
    sentinel_loop = object()
    ws_registry.set_event_loop(sentinel_loop)

    scheduled = []

    def fake_run_coroutine_threadsafe(coro, loop):
        scheduled.append(loop)
        coro.close()  # avoid an "never awaited" warning for the real coroutine
        return object()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    ws_registry.push({"kind": "note_added", "session_id": "sess-1"})

    assert scheduled == [sentinel_loop]


# ---------------------------------------------------------------------
# _broadcast
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_broadcast_sends_to_every_live_socket_for_the_session():
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    ws_registry.register("sess-1", ws_a)
    ws_registry.register("sess-1", ws_b)
    event = {"kind": "note_added", "session_id": "sess-1"}

    await ws_registry._broadcast(event)

    assert ws_a.sent == [event]
    assert ws_b.sent == [event]


@pytest.mark.anyio
async def test_broadcast_does_not_send_to_a_different_sessions_sockets():
    ws_sess1 = FakeWebSocket()
    ws_sess2 = FakeWebSocket()
    ws_registry.register("sess-1", ws_sess1)
    ws_registry.register("sess-2", ws_sess2)

    await ws_registry._broadcast({"kind": "note_added", "session_id": "sess-1"})

    assert ws_sess1.sent
    assert ws_sess2.sent == []


@pytest.mark.anyio
async def test_broadcast_with_no_connections_for_the_session_is_a_noop():
    # must not raise even though "sess-unknown" was never registered
    await ws_registry._broadcast({"kind": "note_added", "session_id": "sess-unknown"})


@pytest.mark.anyio
async def test_broadcast_drops_a_failing_socket_without_raising_and_unregisters_it():
    """A dead/half-closed socket erroring on send_json must not take
    the notify() call site down with it, and must be unregistered so
    future broadcasts stop retrying it."""
    good_ws = FakeWebSocket()
    dead_ws = FakeWebSocket(fail=True)
    ws_registry.register("sess-1", good_ws)
    ws_registry.register("sess-1", dead_ws)
    event = {"kind": "note_added", "session_id": "sess-1"}

    await ws_registry._broadcast(event)  # must not raise

    assert good_ws.sent == [event]
    assert ws_registry._connections["sess-1"] == {good_ws}


@pytest.mark.anyio
async def test_broadcast_all_sockets_failing_drops_the_session_key_entirely():
    dead_ws = FakeWebSocket(fail=True)
    ws_registry.register("sess-1", dead_ws)

    await ws_registry._broadcast({"kind": "note_added", "session_id": "sess-1"})

    assert "sess-1" not in ws_registry._connections
