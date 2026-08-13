"""
daemon/connection.py — F2 Part 2 + Part 3: the daemon's outbound
WebSocket client. Part 2 built connect/hello/hello_ack/reconnect;
Part 3 adds actual handling for the one message type that can arrive
on this connection now -- {"type": "tool_call", ...} -- dispatching it
to daemon/tools.py and sending the {"type": "tool_result", ...} back.
See eo/local_workspace.py's docstring (backend side) for the full wire
shape both halves of this protocol agree on.

Part 4 (write_file/delete/execute_command + propose-confirm) still
adds nothing here beyond new entries in daemon/tools.py's dispatch
table -- this module's loop doesn't need to change again for that.

Place this file at: daemon/connection.py
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from daemon import tools
from daemon.config import DaemonConfig

logger = logging.getLogger("minime_daemon")

_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0
_HELLO_ACK_TIMEOUT_SECONDS = 10.0


async def _run_once(config: DaemonConfig) -> None:
    """One connect-hello-idle cycle. Returns normally on a clean
    disconnect (nothing left to do until run_forever() reconnects);
    raises on anything that should trigger backoff."""
    url = f"{config.backend_ws_url.rstrip('/')}/ws/daemon/{config.workspace_id}"
    logger.info("connecting to backend: %s", url)

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "pairing_token": config.pairing_token,
        }))

        ack_raw = await asyncio.wait_for(ws.recv(), timeout=_HELLO_ACK_TIMEOUT_SECONDS)
        try:
            ack = json.loads(ack_raw)
        except (TypeError, ValueError):
            ack = None

        if not isinstance(ack, dict) or ack.get("type") != "hello_ack":
            # Not a network error -- the backend actively rejected the
            # handshake (bad token, bad first message). Retrying
            # immediately with the same bad config would just fail the
            # same way forever, so log clearly and let run_forever()'s
            # normal backoff space out the retries rather than treating
            # this as an exceptional case that needs its own handling.
            logger.error(
                "backend rejected pairing (response to hello: %r) -- check "
                "MINIME_PAIRING_TOKEN matches the backend's, and "
                "MINIME_WORKSPACE_ID is a real workspace",
                ack_raw,
            )
            return

        logger.info(
            "paired with backend for workspace %s -- ready for tool calls",
            config.workspace_id,
        )

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("received non-JSON message, ignoring: %r", raw)
                continue

            if not isinstance(msg, dict) or msg.get("type") != "tool_call":
                # Nothing else is defined on this connection yet --
                # log and move on rather than treat an unexpected
                # message as a reason to drop the connection.
                logger.debug("received message with no handling defined: %r", msg)
                continue

            await _handle_tool_call(ws, config, msg)


async def _handle_tool_call(ws: "websockets.ClientConnection", config: DaemonConfig, msg: dict) -> None:
    """Runs one {"type": "tool_call", ...} request against
    daemon/tools.py's dispatch table and sends back the matching
    tool_result. A malformed request (missing request_id/tool) is
    logged and dropped rather than answered, since there's no
    request_id to address a response to in that case."""
    request_id = msg.get("request_id")
    tool = msg.get("tool")
    params = msg.get("params") or {}

    if not request_id or not tool:
        logger.warning("tool_call missing request_id/tool, ignoring: %r", msg)
        return

    logger.info("tool_call %s: %s(%r)", request_id, tool, params)

    try:
        # Disk IO runs in a thread so a large read_file (up to
        # tools.MAX_READ_FILE_BYTES) or a big directory listing never
        # blocks this connection's event loop -- which would otherwise
        # stall every other message on the same single daemon
        # connection, including the next tool_call.
        result = await asyncio.to_thread(tools.dispatch, config.allowed_root, tool, params)
    except tools.ToolError as exc:
        await ws.send(json.dumps({
            "type": "tool_result",
            "request_id": request_id,
            "ok": False,
            "error": str(exc),
        }))
        return
    except Exception:
        logger.exception("unexpected error executing tool_call %s", request_id)
        await ws.send(json.dumps({
            "type": "tool_result",
            "request_id": request_id,
            "ok": False,
            "error": "internal daemon error",
        }))
        return

    await ws.send(json.dumps({
        "type": "tool_result",
        "request_id": request_id,
        "ok": True,
        "result": result,
    }))


async def run_forever(config: DaemonConfig, stop_event: asyncio.Event) -> None:
    """Connects, and on any disconnect (clean or not) reconnects with
    exponential backoff, until stop_event is set (minime_daemon.py's
    signal handler sets this on Ctrl+C/SIGTERM). A daemon that gives up
    and exits the first time wifi hiccups would be worse than one that
    just keeps trying -- this is meant to run unattended.
    """
    backoff = _INITIAL_BACKOFF_SECONDS
    while not stop_event.is_set():
        try:
            await _run_once(config)
            backoff = _INITIAL_BACKOFF_SECONDS  # a clean pairing resets backoff
        except (ConnectionClosed, InvalidStatus, OSError) as exc:
            logger.warning("connection lost (%s), reconnecting in %.1fs", exc, backoff)
        except asyncio.TimeoutError:
            logger.warning(
                "backend didn't respond to hello within %.0fs, retrying in %.1fs",
                _HELLO_ACK_TIMEOUT_SECONDS, backoff,
            )
        except Exception:
            logger.exception("unexpected error in connection loop, retrying in %.1fs", backoff)

        if stop_event.is_set():
            break

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass  # normal case: backoff elapsed without a shutdown request
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
