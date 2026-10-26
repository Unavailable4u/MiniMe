"""
codeagent/router.py -- S14 (part a): the message router.

S04's socket (ws.py) authenticates and validates, then hands every message other than
`ping` to ONE installed MessageHandler (ws.set_message_handler). This module is that
handler. It does one thing: look at the message's type `t` and give the message to the
function registered for it. What those functions do (start a run, queue a turn, cancel,
resume, pause on disconnect) is S14c-e's; how their events reach the device is S14b's.

    ws.py (S04)        authenticated, validated, rate-limited connection
    router.py (here)   dispatch by message type; contain a handler's mistakes
    emitter.py         S14b: numbered, stored, delivered server events
    run_handlers.py    S14c-e: the handlers that are registered here
    run_manager.py     S06: start, resume, pause, end, cancel

THE RULES
---------
* Dispatch is by `msg["t"]`, nothing else. The router never reads a payload: it cannot
  leak what it does not look at, and it cannot get a payload wrong.
* A message type with no registered handler is answered with the protocol error
  `internal`, with the message's id as ref_id and a fixed text (MSG_NO_HANDLER). Never
  silence: a device that sends something nobody handles must not wait for it.
  (The code is `internal` because the protocol has no "not implemented"; the message is
  valid, so `invalid_message` would blame the client for a server gap.)
* A handler that raises is CONTAINED. The exception's TYPE is logged, the device gets
  `internal` with ref_id and ws.MSG_INTERNAL, and the socket stays up. ws.py's read loop
  does the same for any handler that escapes; the router does not rely on that, because
  the router is the one place that knows which handler failed, and because a second
  handler installed later (a test, S11's gateway) should not have to be as careful.
  Two things are NOT contained, on purpose:
    - ConnectionClosed: the peer left. ws.py's read loop ends on it; swallowing it here
      would leave a dead connection's loop running, and answering it with `internal`
      would write to a closed socket.
    - asyncio.CancelledError (a BaseException): task cancellation must always get through.
* Disconnect listeners (register_disconnect) are called, in registration order, when a
  connection ends for any reason. A listener that fails is logged by type and the next
  one still runs: "pause the device's runs" must not be skipped because an earlier
  listener broke. Order matters: S14c registers the run-disconnect hooks BEFORE the pause,
  so that an open tool call is closed with `tool_finished disconnected` first.

HANDLERS MUST RETURN QUICKLY
----------------------------
ws.py awaits on_message inside the connection's receive loop. While a handler runs, that
connection reads nothing: not its next message, not its `cancel`, not its `ping`. A
handler therefore does its short work (a database call through run_in_threadpool, a
validation, a queue put) and returns; work that takes seconds -- a model turn, a tool
wait -- is started as a task (asyncio.create_task) and tracked by whoever started it.
The router does not time handlers: a slow one is a bug in that handler, found by its own
tests.

REGISTRATION
------------
register(message_type, handler)       handler is an async function (conn, msg)
register_disconnect(listener)         listener is an async function (conn)
Both refuse a plain (non-async) function with TypeError: it would be called and its
result thrown away, silently doing nothing. Registering the SAME function twice is a
no-op, which is what makes an install function safe to call twice. Registering a
DIFFERENT function for a type that is already taken raises RouterError, because two
modules claiming one message type is a wiring bug that should fail at startup, not
decide by import order; replace=True overrides on purpose (tests, a later patch that
supersedes a stub). Registration happens at startup, from one thread; the tables are not
locked.

INSTALL
-------
install() sets the single router as the socket's handler (ws.set_message_handler) and is
idempotent. get_router() is the process-wide instance everything registers on. Importing
this module installs nothing: server.py makes the explicit call (S14e), so a unit test
that imports the router cannot change the socket's behaviour as a side effect. Tests
build their own MessageRouter, or monkeypatch router._ROUTER.

LOGGING
-------
Device id, user id, message type and the type name of an exception. Never a message, a
payload, an exception's text, a header or an address (tests/static/test_router_logging.py
reads this file and fails if a logging call names one). The message type is logged only
when it looks like a protocol type name (letters, dots, underscores): ws.py only hands on
validated types, but this is a public seam, and an arbitrary string must not become a log
line.

NOT HERE
--------
No run state, no buffer, no events, no handlers for any message type. A router with
nothing registered answers everything with `internal`, exactly as ws.UnavailableHandler
does today, so installing it early changes nothing a device can observe except the text.
"""
from __future__ import annotations

import inspect
import logging
import re

from codeagent import ws
from codeagent.connections import ConnectionClosed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Fixed texts (error.json: "message is fixed, sanitized text"). Nothing the client
# sent is ever interpolated into one.
# ---------------------------------------------------------------------

MSG_NO_HANDLER = "The server does not handle this message type yet."

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TYPE_NAME_RE = re.compile(r"^[a-z][a-z._]{0,31}$")        # run.start, user_message, run.resume ...


class RouterError(Exception):
    """A wiring mistake (two handlers for one type). Raised at registration, at startup."""


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _ref_id(obj) -> "str | None":
    """The id of the message an error answers, if it is a well-formed lower-case UUID
    (error.json's ref_id has that pattern); otherwise nothing.

    A copy of ws._ref_id: that function is private to ws.py, which S14 may not edit.
    tests/unit/test_codeagent_router.py pins that the two agree."""
    if isinstance(obj, dict):
        value = obj.get("id")
        if isinstance(value, str) and _UUID_RE.match(value):
            return value
    return None


def _type_for_log(value) -> str:
    """The message type if it looks like a protocol type name, else '?'."""
    if isinstance(value, str) and _TYPE_NAME_RE.match(value):
        return value
    return "?"


def _is_async_callable(fn) -> bool:
    """A coroutine function, a functools.partial of one, or an object whose __call__ is one."""
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)                    # noqa: B004 - checking, not calling
    return call is not None and inspect.iscoroutinefunction(call)


# ---------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------

class MessageRouter(ws.MessageHandler):
    """The MessageHandler that dispatches by message type. See the module docstring."""

    def __init__(self):
        self._handlers: dict = {}
        self._disconnect_listeners: list = []

    # -- registration ---------------------------------------------------
    def register(self, message_type: str, handler, *, replace: bool = False) -> None:
        """Route `message_type` to `handler`, an async function (conn, msg)."""
        if not isinstance(message_type, str) or not message_type:
            raise ValueError("message_type must be a non-empty string")
        if not _is_async_callable(handler):
            raise TypeError("a message handler must be an async function (conn, msg)")
        current = self._handlers.get(message_type)
        if current is handler:
            return                                          # an idempotent install
        if current is not None and not replace:
            raise RouterError("another handler is already registered for this message type")
        self._handlers[message_type] = handler

    def register_disconnect(self, listener) -> None:
        """Call `listener(conn)`, an async function, when a connection ends. Listeners run
        in registration order; registering the same one twice keeps one copy."""
        if not _is_async_callable(listener):
            raise TypeError("a disconnect listener must be an async function (conn)")
        if listener in self._disconnect_listeners:
            return
        self._disconnect_listeners.append(listener)

    def is_registered(self, message_type: str) -> bool:
        return message_type in self._handlers

    def registered_types(self) -> tuple:
        """The message types that have a handler, sorted (for tests and diagnostics)."""
        return tuple(sorted(self._handlers))

    # -- the MessageHandler seam ----------------------------------------
    async def on_message(self, conn, msg: dict) -> None:
        msg_type = msg.get("t") if isinstance(msg, dict) else None
        handler = self._handlers.get(msg_type) if isinstance(msg_type, str) else None
        if handler is None:
            logger.warning("[router] no handler type=%s device=%s user=%s",
                           _type_for_log(msg_type), conn.device_id, conn.user_id)
            await conn.send_error("internal", MSG_NO_HANDLER, ref_id=_ref_id(msg))
            return
        try:
            await handler(conn, msg)
        except ConnectionClosed:
            raise                                           # the peer left: ws.py ends the loop
        except Exception as exc:                            # a handler bug must not take the socket down
            logger.error("[router] handler failed type=%s device=%s user=%s error=%s",
                         _type_for_log(msg_type), conn.device_id, conn.user_id, type(exc).__name__)
            await conn.send_error("internal", ws.MSG_INTERNAL, ref_id=_ref_id(msg))

    async def on_disconnect(self, conn) -> None:
        for listener in list(self._disconnect_listeners):
            try:
                await listener(conn)
            except ConnectionClosed:
                continue                                    # the device is gone: that is why we are here
            except Exception as exc:                        # one broken listener must not skip the rest
                logger.error("[router] disconnect listener failed device=%s user=%s error=%s",
                             conn.device_id, conn.user_id, type(exc).__name__)


# ---------------------------------------------------------------------
# The process-wide router
# ---------------------------------------------------------------------

_ROUTER = MessageRouter()


def get_router() -> MessageRouter:
    """The single router every handler module registers on."""
    return _ROUTER


def install() -> MessageRouter:
    """Make the router the device socket's message handler. Idempotent. Returns the router."""
    router = get_router()
    if ws.get_message_handler() is not router:
        ws.set_message_handler(router)
    return router
