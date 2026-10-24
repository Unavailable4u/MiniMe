"""
codeagent/connections.py -- S04: who is connected to the device socket right now,
and how to talk to them.

Three small pieces, each behind an interface the later patches build on:

  DeviceConnection   one authenticated socket: who it is, a serialized send path
                     that builds protocol/v1 envelopes, and an idempotent close.
                     S06 (run manager) and S09 (tool broker) hold one of these and
                     call send_message()/send_error(); they never touch the
                     WebSocket itself.
  ConnectionRegistry the connected devices per user. InMemoryRegistry is the only
                     implementation for now. It holds LIVE SOCKETS OF THIS PROCESS,
                     which can never leave process memory, so its methods are
                     plain synchronous calls. S11 (process split) keeps this
                     registry as the gateway's local table and adds cross-process
                     presence ("is this device online anywhere") on top, in Redis.
                     That is why callers go through get_registry() and never
                     import InMemoryRegistry directly.
  HandshakeLimiter   a per-address budget for FAILED handshakes. In memory, behind
                     get_handshake_limiter(); S11 can swap in a Redis-backed one.

None of this module logs: it handles message content on its way to a socket.

THE SEND PATH
-------------
Every outbound frame goes through DeviceConnection.send_json under a per-connection
lock, so frames from the receive loop (pong, errors), the revocation checker and
the run manager (events) never interleave or race a close. A send on a closed
connection raises ConnectionClosed (callers decide whether that matters); a
transport error during a send marks the connection closed and raises the same.
send_message() mints the envelope fields the protocol requires of a sender (a fresh
lower-case UUID `id`, an RFC 3339 UTC `ts`) and puts `run_id`/`seq` only where the
caller gives them; assigning `seq` is the run manager's job (S06), not this module's.

CLOSING
-------
close() sends the WebSocket close frame once (the first code wins) and sets an
event that wait_closed() waits on. The endpoint watches that event, so a close
requested by someone else (a newer connection from the same device superseding
this one, a future administrative disconnect) ends the endpoint's loop on any ASGI
server, instead of relying on the server to wake a blocked receive.

HANDSHAKE LIMITER
-----------------
Counts failures per client address in a sliding window (default 10 per 60 s). The
address is the transport peer, `websocket.client.host`; this module never reads
X-Forwarded-For, since a header the client sets is not something to rate-limit on
(behind a proxy, set uvicorn's FORWARDED_ALLOW_IPS to the proxy so the peer is the
real client -- the same stance as eo/devices.py's pair/start limit). Successes are
not counted and do not reset the count. Once an address is over its budget, further
failures are not recorded, so hammering a limited address cannot extend its own
penalty. Memory is bounded (max_tracked addresses; expired entries are dropped
first, then the oldest-seen). That bound is also its weakness -- a flood from more
than max_tracked distinct addresses can evict a limited address early -- which is
acceptable for an in-process first line of defence; the per-process counters also
mean the effective budget is limit x workers until S11 moves them to Redis.
The limiter is called only from the event loop and takes no lock.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from datetime import datetime, timezone

from starlette.websockets import WebSocketDisconnect

PROTOCOL_ENVELOPE_VERSION = 1


class ConnectionClosed(Exception):
    """A send was attempted on a connection that is closed (or just failed)."""


# ---------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------

def now_ts() -> str:
    """RFC 3339 UTC with milliseconds and a Z offset, as common.json's timestamp wants."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_envelope(t: str, payload: dict, *, run_id: "str | None" = None,
                  seq: "int | None" = None) -> dict:
    """A protocol/v1 envelope with a fresh id and timestamp. `run_id` and `seq` are
    included only when given (the schema forbids them on some types and requires
    them on others; the caller knows which)."""
    env = {"v": PROTOCOL_ENVELOPE_VERSION, "t": t, "id": str(uuid.uuid4()), "ts": now_ts(),
           "payload": payload}
    if run_id is not None:
        env["run_id"] = run_id
    if seq is not None:
        env["seq"] = seq
    return env


def error_payload(code: str, message: str, *, ref_id: "str | None" = None,
                  details: "dict | None" = None) -> dict:
    """The payload of an `error` message. `message` must be fixed, sanitized text
    (error.json: never echo anything the client sent)."""
    payload = {"code": code, "message": message}
    if ref_id is not None:
        payload["ref_id"] = ref_id
    if details:
        payload["details"] = details
    return payload


def encode(envelope: dict) -> str:
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------
# One connection
# ---------------------------------------------------------------------

class DeviceConnection:
    """An authenticated device socket. Created by ws.py after a successful hello."""

    def __init__(self, websocket, *, user_id: str, device_id: str, protocol: int,
                 client_version: str, capabilities: tuple):
        self._ws = websocket
        self.user_id = user_id
        self.device_id = device_id
        self.protocol = protocol                  # the version negotiated for this connection
        self.client_version = client_version
        self.capabilities = tuple(capabilities)
        self.connected_at = now_ts()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._closed_event = asyncio.Event()

    def __repr__(self) -> str:                    # ids only: safe to appear in a traceback
        return f"<DeviceConnection device={self.device_id} user={self.user_id}>"

    @property
    def closed(self) -> bool:
        return self._closed

    def note_disconnected(self) -> None:
        """The peer went away (no close frame to send). Wakes wait_closed()."""
        self._closed = True
        self._closed_event.set()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def send_json(self, envelope: dict) -> None:
        """Send an already-built envelope verbatim (the golden-transcript tests, and
        a run manager replaying stored events with their original id/seq)."""
        if self._closed:
            raise ConnectionClosed("connection closed")
        text = encode(envelope)
        async with self._send_lock:
            if self._closed:
                raise ConnectionClosed("connection closed")
            try:
                await self._ws.send_text(text)
            except (RuntimeError, WebSocketDisconnect, OSError):
                self.note_disconnected()
                raise ConnectionClosed("connection closed") from None

    async def send_message(self, t: str, payload: dict, *, run_id: "str | None" = None,
                           seq: "int | None" = None) -> dict:
        """Build and send one envelope; returns it (a run manager keeps it for replay)."""
        env = make_envelope(t, payload, run_id=run_id, seq=seq)
        await self.send_json(env)
        return env

    async def send_error(self, code: str, message: str, *, ref_id: "str | None" = None,
                         details: "dict | None" = None) -> None:
        await self.send_message("error", error_payload(code, message, ref_id=ref_id, details=details))

    async def close(self, code: int, reason: str = "") -> None:
        """Close the socket. Idempotent: the first code and reason win."""
        if self._closed:
            self._closed_event.set()
            return
        self._closed = True
        try:
            await self._ws.close(code=code, reason=reason or None)
        except (RuntimeError, WebSocketDisconnect, OSError):
            pass                                  # already gone: nothing left to close
        finally:
            self._closed_event.set()


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

class ConnectionRegistry(ABC):
    """The connected devices of this process, per user. One live connection per
    device: a second one supersedes the first (the caller closes the loser)."""

    @abstractmethod
    def add(self, conn: DeviceConnection) -> "DeviceConnection | None":
        """Register `conn`; return the connection it replaced, if the same device
        was already connected."""

    @abstractmethod
    def remove(self, conn: DeviceConnection) -> None:
        """Forget `conn`, but only if it is still the registered one for its device
        (a superseded connection cleaning up must not remove its replacement)."""

    @abstractmethod
    def get(self, user_id: str, device_id: str) -> "DeviceConnection | None": ...

    @abstractmethod
    def for_user(self, user_id: str) -> list: ...

    @abstractmethod
    def count(self) -> int: ...

    def is_online(self, user_id: str, device_id: str) -> bool:
        return self.get(user_id, device_id) is not None


class InMemoryRegistry(ConnectionRegistry):
    def __init__(self):
        self._by_user: dict[str, dict[str, DeviceConnection]] = {}

    def add(self, conn):
        devices = self._by_user.setdefault(conn.user_id, {})
        previous = devices.get(conn.device_id)
        devices[conn.device_id] = conn
        return previous if previous is not conn else None

    def remove(self, conn):
        devices = self._by_user.get(conn.user_id)
        if devices is None or devices.get(conn.device_id) is not conn:
            return
        del devices[conn.device_id]
        if not devices:
            del self._by_user[conn.user_id]

    def get(self, user_id, device_id):
        return self._by_user.get(user_id, {}).get(device_id)

    def for_user(self, user_id):
        return list(self._by_user.get(user_id, {}).values())

    def count(self):
        return sum(len(d) for d in self._by_user.values())


# ---------------------------------------------------------------------
# Failed-handshake limiter
# ---------------------------------------------------------------------

class HandshakeLimiter:
    def __init__(self, limit: int = 10, window_s: float = 60.0, *, clock=time.monotonic,
                 max_tracked: int = 10_000):
        self.limit = limit
        self.window_s = window_s
        self.max_tracked = max_tracked
        self._clock = clock
        self._failures: "OrderedDict[str, deque]" = OrderedDict()

    @staticmethod
    def _key(address) -> str:
        return address if isinstance(address, str) and address else "unknown"

    def _prune(self, key: str, now: float) -> "deque | None":
        stamps = self._failures.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= self.window_s:
            stamps.popleft()
        if not stamps:
            del self._failures[key]
            return None
        return stamps

    def retry_after(self, address) -> int:
        """0 if `address` may attempt a handshake; otherwise the whole seconds
        (rounded up, at least 1) until its oldest counted failure ages out."""
        now = self._clock()
        key = self._key(address)
        stamps = self._prune(key, now)
        if stamps is None or len(stamps) < self.limit:
            return 0
        return max(1, math.ceil(stamps[0] + self.window_s - now))

    def record_failure(self, address) -> None:
        now = self._clock()
        key = self._key(address)
        stamps = self._prune(key, now)
        if stamps is not None and len(stamps) >= self.limit:
            return                                # already limited: do not extend the penalty
        is_new = stamps is None
        if is_new:
            self._failures[key] = stamps = deque()
        stamps.append(now)
        if is_new:
            self._enforce_bound(now, keep=key)    # after the append: sweep() drops empty entries

    def _enforce_bound(self, now: float, keep: str) -> None:
        if len(self._failures) <= self.max_tracked:
            return
        self.sweep()
        while len(self._failures) > self.max_tracked:
            oldest = next(iter(self._failures))
            if oldest == keep:
                break
            del self._failures[oldest]

    def sweep(self) -> None:
        """Drop every address whose failures have all aged out."""
        now = self._clock()
        for key in list(self._failures):
            self._prune(key, now)

    def tracked(self) -> int:
        return len(self._failures)


# ---------------------------------------------------------------------
# Process-wide instances (swappable: S11, and tests)
# ---------------------------------------------------------------------

_REGISTRY: ConnectionRegistry = InMemoryRegistry()
_LIMITER: HandshakeLimiter = HandshakeLimiter()


def get_registry() -> ConnectionRegistry:
    return _REGISTRY


def set_registry(registry: ConnectionRegistry) -> None:
    global _REGISTRY
    _REGISTRY = registry


def get_handshake_limiter() -> HandshakeLimiter:
    return _LIMITER


def set_handshake_limiter(limiter: HandshakeLimiter) -> None:
    global _LIMITER
    _LIMITER = limiter
