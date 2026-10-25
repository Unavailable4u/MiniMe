"""
m2c/src/m2c/link/__init__.py -- Patch C04: the device link, this client's
WebSocket connection to the MiniMe server.

    client.py      ``DeviceLink``: handshake (hello with the device credential,
                   welcome, protocol check), run.start / run.resume, the
                   request table (ping/pong, error by ref_id), keepalive,
                   graceful shutdown, and the rule that every accepted server
                   event is appended to the session's event log exactly once and
                   in order (duplicates dropped, gaps never stored out of order)
    reconnect.py   the backoff (exponential, full jitter, capped, reset only by a
                   connection that stayed up), the error classes, and which
                   failures are final (unauthorized, device_revoked,
                   protocol_unsupported, superseded, store failure) versus
                   retried

Typical use::

    from m2c.link import DeviceLink, LinkCallbacks
    from m2c.store import open_db

    async with DeviceLink(open_db(), callbacks=LinkCallbacks(on_event=show)) as link:
        await link.wait_connected(10)
        run_id = await link.start_run(project_path=cwd, mode="ask",
                                      cwd_label="my-project", instructions=text)
        await link.send_user_message(run_id, "hello")
        ...

Executing tools is not here (C05 and later): ``tool_proposed`` events are stored
and handed to ``LinkCallbacks.on_tool_proposed``.

Only this package imports ``websockets`` (tests/test_cli.py enforces it).

Place this file at: m2c/src/m2c/link/__init__.py
"""
from __future__ import annotations

from .client import (
    SOCKET_PATH,
    SUPPORTED_PROTOCOLS,
    DeviceLink,
    LinkCallbacks,
    LinkEvent,
    LinkStats,
    RunInfo,
    ServerError,
    Welcome,
    device_socket_url,
    recover_last_seq,
)
from .reconnect import (
    Backoff,
    BackoffPolicy,
    LinkClosedError,
    LinkConnectionError,
    LinkDeviceRevokedError,
    LinkError,
    LinkInternalError,
    LinkMessageTooLargeError,
    LinkNotConnectedError,
    LinkProtocolUnsupportedError,
    LinkRateLimitedError,
    LinkRequestError,
    LinkRequestTimeout,
    LinkStoreError,
    LinkSupersededError,
    LinkUnauthorizedError,
    classify,
    retry_after_of,
)

__all__ = [
    "Backoff",
    "BackoffPolicy",
    "DeviceLink",
    "LinkCallbacks",
    "LinkClosedError",
    "LinkConnectionError",
    "LinkDeviceRevokedError",
    "LinkError",
    "LinkEvent",
    "LinkInternalError",
    "LinkMessageTooLargeError",
    "LinkNotConnectedError",
    "LinkProtocolUnsupportedError",
    "LinkRateLimitedError",
    "LinkRequestError",
    "LinkRequestTimeout",
    "LinkStats",
    "LinkStoreError",
    "LinkSupersededError",
    "LinkUnauthorizedError",
    "RunInfo",
    "SOCKET_PATH",
    "SUPPORTED_PROTOCOLS",
    "ServerError",
    "Welcome",
    "classify",
    "device_socket_url",
    "recover_last_seq",
    "retry_after_of",
]
