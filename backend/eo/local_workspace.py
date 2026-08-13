"""
eo/local_workspace.py — F2 Part 2: the WebSocket route the local
daemon (daemon/minime_daemon.py) connects OUT to, plus the session
registry that tracks which daemon (if any) is live for which
workspace_id.

Deliberately still does nothing with tool calls -- that's Part 3
(list_dir/read_file message shape) and Part 4 (write_file/delete/
execute_command + propose-confirm). This module's whole job is:
accept the connection, run a pairing handshake, register it, and
notice when it goes away.

Why this is its own module rather than folding into api/server.py the
way eo/ws_registry.py's /ws/{session_id} route lives there directly:
that route is browser-facing and auths via `_verify_supabase_jwt` (a
logged-in user). This one is daemon-facing and auths via a shared
pairing token instead -- a different actor, a different registry shape
(session_id -> set() there, since many browser tabs can watch one
session; workspace_id -> a single WebSocket here, since only one
machine should ever be the live local daemon for a given workspace at
once), and no user JWT exists on this path at all. Keeping the two
apart means neither route's auth logic has to branch on which kind of
caller it's talking to.

Part 3 adds the actual tool-call request/response bridge on top of
this same connection: call_daemon() sends a {"type": "tool_call", ...}
message and hands back an asyncio.Future that daemon_endpoint's
receive loop resolves once the matching {"type": "tool_result", ...}
comes back over the wire (see _resolve_pending below). See
eo/local_workspace_tools.py for the actual list_dir/read_file
functions built on top of call_daemon(), and daemon/tools.py for the
daemon-side implementation and the exact message shapes.

Place this file at: eo/local_workspace.py
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()

# workspace_id -> the single live daemon WebSocket for it. A dict, not
# eo/ws_registry.py's dict-of-sets, because Part 3/4's tool calls need
# one unambiguous target to route a request to -- there's no
# "broadcast a list_dir to every daemon" case the way there's a
# legitimate "broadcast a notify() event to every open browser tab"
# case on the other registry.
_connections: dict[str, WebSocket] = {}

# request_id -> (workspace_id, the Future call_daemon() is waiting on).
# workspace_id is carried alongside the Future purely so a disconnect
# (see _fail_pending_for_workspace) can find and fail every request
# still in flight for that daemon, rather than leaving callers to hang
# until their own timeout even though the connection is already gone.
_pending: dict[str, tuple[str, "asyncio.Future[dict]"]] = {}

DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 30.0


class ToolCallError(Exception):
    """Raised by call_daemon() for any of: no daemon connected for
    this workspace, the daemon disconnecting mid-call, a timeout
    waiting for a response, or the daemon itself reporting
    {"ok": false} (message is the daemon's own error string in that
    last case). Callers (eo/local_workspace_tools.py, and the routes
    built on it) catch this one type rather than needing to know which
    of those four actually happened."""


class PairingError(Exception):
    """Raised when the backend itself isn't configured to pair at all
    (MINIME_PAIRING_TOKEN unset) -- distinct from a wrong token, which
    is just a normal auth failure, not a config error."""


def _expected_token() -> str:
    token = os.environ.get("MINIME_PAIRING_TOKEN", "").strip()
    if not token:
        raise PairingError(
            "MINIME_PAIRING_TOKEN is not set in backend/.env -- the local "
            "daemon feature (F2) can't accept any daemon connections until "
            "this is set to the same value as the daemon's own "
            "daemon/.env MINIME_PAIRING_TOKEN. See daemon/.env.example."
        )
    return token


def _check_token(presented: str) -> bool:
    expected = _expected_token()
    # Constant-time compare -- this is a shared secret presented over
    # the wire on every daemon (re)connect, same reasoning as any
    # other bearer-token check in this codebase.
    return hmac.compare_digest(presented, expected)


def is_live(workspace_id: str) -> bool:
    """Part 3/4 call this before attempting to route a tool call, so
    'no daemon connected for this workspace' surfaces as a normal 4xx
    from the tool-call endpoint rather than a KeyError deep in here."""
    return workspace_id in _connections


def get(workspace_id: str) -> WebSocket | None:
    """Part 3/4's tool-call dispatch uses this to get the actual socket
    to send a request to, once is_live() has confirmed one exists."""
    return _connections.get(workspace_id)


async def _register(workspace_id: str, websocket: WebSocket) -> None:
    existing = _connections.get(workspace_id)
    if existing is not None and existing is not websocket:
        logger.info(
            "[local_workspace] workspace %s: new daemon connection "
            "supersedes the existing one",
            workspace_id,
        )
        try:
            await existing.close(code=4409)  # 4409: superseded, mirrors HTTP 409
        except Exception:
            pass  # already gone -- nothing left to clean up beyond the dict entry below
    _connections[workspace_id] = websocket


def _unregister(workspace_id: str, websocket: WebSocket) -> None:
    # Guards against a superseded connection's own `finally` block
    # popping the *new* connection that replaced it -- only remove the
    # entry if it's still pointing at this exact socket.
    if _connections.get(workspace_id) is websocket:
        _connections.pop(workspace_id, None)


def _fail_pending_for_workspace(workspace_id: str, reason: str) -> None:
    """Called from daemon_endpoint's `finally` block on disconnect, so
    any call_daemon() still awaiting a response for this workspace
    fails immediately with a clear reason instead of sitting there
    until its own timeout elapses."""
    stale_ids = [
        request_id
        for request_id, (ws_id, future) in _pending.items()
        if ws_id == workspace_id and not future.done()
    ]
    for request_id in stale_ids:
        _, future = _pending.pop(request_id)
        if not future.done():
            future.set_exception(ToolCallError(reason))


def _resolve_pending(msg: Any) -> bool:
    """Called from daemon_endpoint's receive loop for every inbound
    message. Returns True if the message was a tool_result matched to
    a pending call_daemon() (and has therefore been fully handled) so
    the loop knows not to also log/discard it as an unhandled message."""
    if not isinstance(msg, dict) or msg.get("type") != "tool_result":
        return False

    request_id = msg.get("request_id")
    entry = _pending.get(request_id)
    if entry is None:
        logger.warning(
            "[local_workspace] tool_result for unknown or already-"
            "resolved request_id %r, discarding",
            request_id,
        )
        return True

    _, future = entry
    if not future.done():
        future.set_result(msg)
    return True


async def call_daemon(
    workspace_id: str,
    tool: str,
    params: dict[str, Any],
    timeout: float = DEFAULT_TOOL_CALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """The actual send-a-request-and-await-the-response half of the
    protocol -- eo/local_workspace_tools.py's list_workspace_dir()/
    read_workspace_file() are thin wrappers over this. Raises
    ToolCallError; never returns a partial/ambiguous result.
    """
    websocket = _connections.get(workspace_id)
    if websocket is None:
        raise ToolCallError(f"no daemon is currently connected for workspace {workspace_id}")

    request_id = str(uuid.uuid4())
    future: "asyncio.Future[dict]" = asyncio.get_running_loop().create_future()
    _pending[request_id] = (workspace_id, future)

    try:
        await websocket.send_json({
            "type": "tool_call",
            "request_id": request_id,
            "tool": tool,
            "params": params,
        })
        try:
            result_msg = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise ToolCallError(
                f"daemon did not respond to {tool!r} within {timeout:.0f}s"
            ) from None
    finally:
        _pending.pop(request_id, None)

    if not result_msg.get("ok"):
        raise ToolCallError(result_msg.get("error") or f"{tool} failed with no error message")
    return result_msg.get("result") or {}


@router.websocket("/ws/daemon/{workspace_id}")
async def daemon_endpoint(websocket: WebSocket, workspace_id: str) -> None:
    """The backend never dials the daemon -- most dev machines sit
    behind NAT and can't accept inbound connections anyway, so the
    daemon is always the one reaching out (see daemon/connection.py's
    Part 2 client half of this same handshake).

    Wire shape for this part: the very first message must be
    {"type": "hello", "pairing_token": "..."}. Anything else as the
    first message, or a hello with a wrong/missing token, closes the
    socket. A successful hello gets {"type": "hello_ack",
    "workspace_id": ...} back and the connection is registered. Every
    message received after that is either a {"type": "tool_result",
    ...} answering an in-flight call_daemon() (handled by
    _resolve_pending -- Part 3) or, for now, anything else, which is
    logged and discarded purely to detect disconnects, same as
    api/server.py's browser-facing /ws/{session_id} loop already does
    for the same reason.
    """
    await websocket.accept()

    try:
        hello = await websocket.receive_json()
    except Exception:
        logger.warning(
            "[local_workspace] workspace %s: no valid hello received, closing",
            workspace_id,
        )
        await websocket.close(code=4400)  # 4400: bad handshake, mirrors HTTP 400
        return

    if not isinstance(hello, dict) or hello.get("type") != "hello":
        logger.warning(
            "[local_workspace] workspace %s: first message wasn't a hello, closing",
            workspace_id,
        )
        await websocket.close(code=4400)
        return

    presented = str(hello.get("pairing_token", ""))
    try:
        token_ok = _check_token(presented)
    except PairingError as exc:
        logger.error("[local_workspace] %s", exc)
        await websocket.close(code=4500)  # 4500: server misconfigured, not the daemon's fault
        return

    if not token_ok:
        logger.warning(
            "[local_workspace] workspace %s: pairing token mismatch, closing",
            workspace_id,
        )
        await websocket.close(code=4401)
        return

    await _register(workspace_id, websocket)
    logger.info(
        "[local_workspace] daemon connected and paired for workspace %s",
        workspace_id,
    )

    try:
        await websocket.send_json({"type": "hello_ack", "workspace_id": workspace_id})

        while True:
            msg = await websocket.receive_json()
            if _resolve_pending(msg):
                continue
            # Reading (and, for anything that isn't a tool_result,
            # discarding) is also how Starlette surfaces a
            # client-initiated disconnect (WebSocketDisconnect)
            # instead of this loop spinning against an already-closed
            # socket.
            logger.debug(
                "[local_workspace] workspace %s: unhandled message %r",
                workspace_id, msg,
            )
    except WebSocketDisconnect:
        pass
    finally:
        _unregister(workspace_id, websocket)
        _fail_pending_for_workspace(
            workspace_id,
            f"daemon for workspace {workspace_id} disconnected before responding",
        )
        logger.info(
            "[local_workspace] daemon disconnected for workspace %s",
            workspace_id,
        )
