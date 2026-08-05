"""
eo/ws_registry.py — Data Layer architecture §9b: the per-session
WebSocket connection registry, plus the thread-safe bridge that lets
eo/notify.py's notify() actually push.

Why this is its own module rather than living in api/server.py or
eo/notify.py directly: notify() is called synchronously from deep
inside agent code (agents/source_manager.py, agents/backlink_detector.py)
that itself runs inside FastAPI's sync-endpoint threadpool, not on the
asyncio event loop -- see api/task_runner.py, which is all plain `def`,
never `async def`. api/server.py owns the actual WebSocket route
(accept/receive loop) and needs a place to register live connections;
eo/notify.py needs a place to hand off an event dict from whatever
thread it happens to be running on. Splitting this out means neither
of those two files has to import the other just for this.

Place this file at: eo/ws_registry.py
"""
import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# session_id -> the set of live sockets open for it. A set, not a
# single connection, because one session can legitimately have more
# than one open socket (the same chat open in two tabs) -- same
# "more than one listener per channel" shape Pusher's channel model
# already assumes elsewhere in this codebase (relay/emitter.py).
_connections: dict[str, set[WebSocket]] = {}

# Captured once, from api/server.py's lifespan startup hook, so push()
# below -- which may be called from a worker thread, not the event
# loop -- has somewhere thread-safe to hand its coroutine off to.
_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def register(session_id: str, websocket: WebSocket) -> None:
    """Called once the handshake is accepted, from inside
    api/server.py's websocket_endpoint -- i.e. always on the event
    loop itself, so (unlike push()/_broadcast() below) there's no
    cross-thread race to guard here."""
    _connections.setdefault(session_id, set()).add(websocket)


def unregister(session_id: str, websocket: WebSocket) -> None:
    conns = _connections.get(session_id)
    if not conns:
        return
    conns.discard(websocket)
    if not conns:
        _connections.pop(session_id, None)


async def _broadcast(event: dict) -> None:
    """Scheduled onto the event loop by push() below. Sends to every
    socket currently open for event['session_id'], dropping any that
    error out -- a stale or half-closed socket must never take the
    notify() call site down with it (same "an emission failure can't
    take down the actual work" rule relay/emitter.py's own docstring
    already states for its transport)."""
    session_id = event["session_id"]
    conns = _connections.get(session_id)
    if not conns:
        return
    dead = []
    for ws in conns:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        unregister(session_id, ws)


def push(event: dict) -> None:
    """eo/notify.py's _deliver() calls this once per event. Safe to
    call from any thread. If no event loop has been captured yet (app
    never started, or this is running under a test/CLI context that
    never called set_event_loop) this is a documented no-op -- same
    "nowhere to push to yet, log and move on" posture §9a's stub
    already had, not an exception every caller now has to guard
    against.
    """
    if _loop is None:
        logger.debug(
            "[ws_registry] no event loop registered yet, dropping %s for session %s",
            event.get("kind"), event.get("session_id"),
        )
        return
    asyncio.run_coroutine_threadsafe(_broadcast(event), _loop)
