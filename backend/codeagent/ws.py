"""
codeagent/ws.py -- S04: WS /v1/code/device, the socket every M2C device client
(`m2c`, WP C04) holds open to the server.

This module implements the handshake and the transport rules of protocol v1
(protocol/README.md and protocol/v1/*.json are the contract; if this file and they
disagree, they win). It does NOT run anything: runs, the model loop and tools are
S05/S06/S07/S09. What it gives them is an authenticated, validated, rate-limited
connection and a seam to plug into (MessageHandler, below).

HANDSHAKE (protocol/v1/hello.json, welcome.json)
-------------------------------------------------
    device                                         server
    ------                                         ------
    connect ---------------------------------->    accept (so errors can be answered in v1 shape)
    hello{protocol_versions, device_credential,
          client_version, capabilities} ------->   within hello_timeout_s, and it must be a hello
                                                   validate the envelope and payload against protocol/v1
                                                   pick the highest version both sides speak
                                                   authenticate the credential (eo/devices.py, S03)
                                    <------------  welcome{protocol, min_protocol, token_ttl_s,
                                                           limits, server_version}
    ... run.start, user_message, ping, ...   <-->  validated one by one, then handed on
Any failure is answered with an `error` message (never anything else, never silence)
and then the close frame:

    unauthorized          credential malformed or unknown            close 4401
    device_revoked        the device was revoked                     close 4403
    protocol_unsupported  no common version; details.min_protocol /
                          max_protocol say what this server speaks   close 4426
    rate_limited          too many failed handshakes from this
                          address; details.retry_after_s             close 4429
    invalid_message       first message late, not a hello, not
                          JSON, not valid, binary, or too large      close 4408 / 4400 / 1009
    internal              server fault (database, validator)         close 1011

A server that no longer speaks v1 must still answer a v1 hello with
protocol_unsupported in v1 shape (README, error codes); the hello envelope is
therefore always `v: 1`, and negotiation lives in hello.protocol_versions.

THERE IS NO TOKEN REFRESH, ON PURPOSE
-------------------------------------
P00 left a refresh message out and protocol/v1 is not changed here. The device
credential in hello authenticates the connection for its whole life; instead of
asking the client to renew anything, the SERVER re-validates. Every
revalidate_interval_s (REVALIDATE_INTERVAL_SECONDS = 30, at most 30 by rule, and
injectable through SETTINGS so tests need not wait) a background task asks
eo.devices.is_device_active(user_id, device_id). If the device is no longer active
the server sends `error device_revoked` and closes with 4403; if it cannot find out
(database fault) it sends `error internal` and closes with 1011 -- fail closed, but
without telling the person to log in again because of an outage. So revoking a
device in the web app ends its socket within about 30 seconds.

welcome.token_ttl_s is S03's DEVICE_JWT_TTL_SECONDS (900). Read it as "the server's
advertised authentication lifetime": informational for the client (it may use it to
pace its own housekeeping), never a deadline the server enforces by dropping the
connection; the 30 second re-check is what enforces revocation.

CREDENTIALS NEVER CROSS OVER
----------------------------
This socket accepts exactly one thing: a device credential, checked by
eo.devices.authenticate_credential (hash lookup, revoked devices refused,
last_seen_at stamped). It does not use eo/local_workspace.py's shared-token check,
and it never calls api.deps.require_auth. A Supabase user token is not a device
credential (it has dots, so it fails the credential shape before any lookup), a
device JWT is not one either, and the reverse holds too: require_auth, which
verifies against the Supabase key and audience, rejects a device JWT and a
credential. tests/unit/test_codeagent_ws.py pins both directions.

TRANSPORT RULES
---------------
* Frames are JSON text. A binary frame is invalid_message.
* Size: welcome.limits.max_message_bytes (SETTINGS.max_message_bytes, 1 MiB) is
  enforced here, in bytes of UTF-8, inclusive. An oversized frame is answered with
  invalid_message and the connection closed with 1009. The ASGI server has already
  read the frame by then, so the hard memory cap is uvicorn's `--ws-max-size`
  (default 16 MiB); set it to the limit plus a little slack in deployment.
* Every message is validated against protocol/v1 with the TOLERANT reader
  (validate_message(tolerant=True), README section 8): unknown optional properties
  from a newer client are ignored rather than fatal, everything else (types, enums,
  required fields, ids, ranges) is strict. Tolerant means unknown properties still
  reach the handler: handlers must project onto the schema's known properties before
  they persist anything (README section 6).
* Direction is enforced: only the nine client-to-server types (read from
  envelope.json's x-m2c-types, not hard-coded here) are accepted. A server type sent
  by a device, an unknown type and a second hello are invalid_message. hello is
  never re-read after the handshake.
* An invalid message is answered with invalid_message (ref_id = the message's id when
  that is a well-formed UUID) and the socket carries on. MAX_CONSECUTIVE_INVALID in a
  row (default 20) closes it with 1008. The error text is fixed: nothing the client
  sent is echoed back.
* `ping` is answered here with `pong` (nonce echoed). Between pings, liveness at the
  transport level is the ASGI server's WebSocket ping (uvicorn --ws-ping-interval).
* Failed handshakes are counted per client address (connections.HandshakeLimiter):
  unauthorized, and any malformed, late, oversized or non-hello first message. A
  revoked device (its credential was valid, so it is not a guess) and an old client
  (protocol_unsupported) are not counted. Over budget, the address is answered
  rate_limited without reading its hello or touching the database.

CLOSE CODES (4000-4999 is the application range; 4xxx mirrors the HTTP status)
------------------------------------------------------------------------------
    1008  policy violation: too many invalid messages in a row
    1009  message too big
    1011  internal error (database or validator unavailable)
    4400  bad handshake: first message not a valid hello
    4401  unauthorized
    4403  device revoked
    4408  hello not received in time
    4409  superseded: the same device connected again (the newer connection wins,
          like eo/local_workspace.py's workspace daemon)
    4426  protocol unsupported
    4429  rate limited

THE SEAM FOR THE RUN MANAGER
----------------------------
After the handshake, every valid message other than ping (and the forbidden second
hello) goes to the installed MessageHandler: set_message_handler(handler) at
startup (S06 does this). on_message(conn, msg) receives the validated envelope as a
dict; conn is a connections.DeviceConnection to answer on; on_disconnect(conn) is
called once when the connection ends, for any reason. It is awaited inside the
receive loop, so a handler must return quickly (start a task for long work), or the
device's pings and cancels wait behind it. A handler that raises does not take the
socket down: the device gets `internal` with ref_id, the exception's TYPE is
logged, and the loop continues. Until S06 installs one, UnavailableHandler answers
every such message with `internal` ("run handling is not available"), so a client
never waits on silence.

LOGGING
-------
Device id, user id, message type (only after the message validated, so it is one of
the schema's own names) and error code. Never a message, a payload field, a
credential, a client address, a validator issue, or an exception's text: exceptions
are logged by type name only, and there is no logger.exception()/exc_info anywhere
(tests/static/test_codeagent_logging.py reads this file and fails if one appears).

RUNTIME NEEDS: jsonschema AND the protocol/ directory
-----------------------------------------------------
Validation uses protocol/validate.py (P00's reference reader), which needs
jsonschema (with `referencing`), and reads the schemas from protocol/v1.

  1. jsonschema: a backend install gets it today only TRANSITIVELY --
     guardrails-ai 0.10.2 requires jsonschema>=4.22,<5 (so does litellm), and
     validate.py works with any such version -- but backend/requirements.txt does not
     name or pin it. protocol/README.md section 10 already names the follow-up
     ("add jsonschema==4.26.0 and its pins ... once S04/S05 import the schemas at
     runtime"). Not done here: S04 may not add a dependency or touch that file.
  2. protocol/ inside the backend container: NOT there today. backend.Dockerfile
     copies only backend/ (build context is the repo root, so `COPY protocol/
     /app/protocol/` is one line), and docker-compose.dev.yml bind-mounts only
     ./backend. load_protocol() looks in <backend>/protocol (that layout) and
     <repo root>/protocol (a checkout).

Until both are in place the endpoint FAILS CLOSED: load_protocol() raises
ProtocolUnavailable, the socket answers `internal` and closes with 1011 before
reading anything, and nobody is authenticated. Nothing else in the backend is
affected: the validator is loaded lazily on the first connection, never at import,
so a missing dependency or directory cannot stop api/server.py from starting.

NOT HERE (other patches)
------------------------
Runs, replay and seq numbering (S06), tool brokering (S09), the model (S05),
moving the registry and limiter counters to Redis (S11), the Devices page's
online/offline column (X01 would read connections.get_registry().is_online()).
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import logging
import re
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool

from codeagent import connections
from codeagent.connections import ConnectionClosed, DeviceConnection
from eo import devices

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

SERVER_VERSION = "codeagent-s04"
SUPPORTED_VERSIONS = (1,)
MIN_PROTOCOL = 1
MAX_PROTOCOL = max(SUPPORTED_VERSIONS)

REVALIDATE_INTERVAL_SECONDS = 30.0      # the rule: at most 30; SETTINGS makes it injectable
HELLO_TIMEOUT_SECONDS = 10.0
MAX_MESSAGE_BYTES = 1_048_576
MAX_PARALLEL_RUNS = 3                   # advertised in welcome; enforced by the run manager (S06)
MAX_CONSECUTIVE_INVALID = 20
_VALIDATE_INLINE_MAX_BYTES = 32 * 1024  # bigger messages are validated off the event loop

CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_INTERNAL = 1011
CLOSE_BAD_HANDSHAKE = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_DEVICE_REVOKED = 4403
CLOSE_HANDSHAKE_TIMEOUT = 4408
CLOSE_SUPERSEDED = 4409
CLOSE_PROTOCOL_UNSUPPORTED = 4426
CLOSE_RATE_LIMITED = 4429

# Fixed texts (error.json: "message is fixed, sanitized text"). Nothing from the
# client is ever interpolated into one.
MSG_UNAUTHORIZED = "The device credential was not accepted."
MSG_REVOKED = "This device was revoked."
MSG_UNSUPPORTED = "No protocol version in common with this server."
MSG_RATE_LIMITED = "Too many failed attempts. Try again later."
MSG_INTERNAL = "The server could not complete the request."
MSG_NO_RUN_MANAGER = "Run handling is not available on this server yet."
MSG_HELLO_TIMEOUT = "No hello was received in time."
MSG_NOT_HELLO = "The first message must be a valid hello."
MSG_INVALID = "The message is not valid for this protocol."
MSG_BINARY = "Binary frames are not part of this protocol."
MSG_TOO_BIG = "The message exceeds the size limit."
MSG_SECOND_HELLO = "A hello is only valid as the first message."

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclasses.dataclass(frozen=True)
class SocketSettings:
    """Tunables, read once per connection from the module-level SETTINGS so tests
    can replace them (dataclasses.replace) without touching code."""
    hello_timeout_s: float = HELLO_TIMEOUT_SECONDS
    revalidate_interval_s: float = REVALIDATE_INTERVAL_SECONDS
    max_message_bytes: int = MAX_MESSAGE_BYTES
    max_parallel_runs: int = MAX_PARALLEL_RUNS
    max_consecutive_invalid: int = MAX_CONSECUTIVE_INVALID


SETTINGS = SocketSettings()


# ---------------------------------------------------------------------
# The protocol reader (lazy: see "Runtime needs")
# ---------------------------------------------------------------------

_VALIDATOR_MODULE_NAME = "codeagent_protocol_validate"
BACKEND_DIR = Path(__file__).resolve().parent.parent


class ProtocolUnavailable(Exception):
    """protocol/validate.py or the schemas could not be loaded. The message is
    fixed text: the reason (a missing dependency, a path) goes to the log only."""


_PROTOCOL = None
_PROTOCOL_LOCK = threading.Lock()
_CLIENT_TYPES: dict = {}


def _protocol_candidates() -> list:
    return [BACKEND_DIR / "protocol", BACKEND_DIR.parent / "protocol"]


def load_protocol():
    """P00's validate.py as a module (validate_message, load_json, V1_DIR, ...),
    loaded once, from the first candidate directory that has validate.py and v1/.
    Raises ProtocolUnavailable. Blocking (imports jsonschema the first time):
    the endpoint calls it in a worker thread."""
    global _PROTOCOL
    if _PROTOCOL is not None:
        return _PROTOCOL
    with _PROTOCOL_LOCK:
        if _PROTOCOL is not None:
            return _PROTOCOL
        for directory in _protocol_candidates():
            path = directory / "validate.py"
            if not path.is_file() or not (directory / "v1").is_dir():
                continue
            spec = importlib.util.spec_from_file_location(_VALIDATOR_MODULE_NAME, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[_VALIDATOR_MODULE_NAME] = module     # dataclasses in validate.py need it
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                sys.modules.pop(_VALIDATOR_MODULE_NAME, None)
                logger.error("[codeagent] protocol validator failed to load (%s); "
                             "the device socket will refuse connections", type(exc).__name__)
                raise ProtocolUnavailable("protocol validator unavailable") from None
            _PROTOCOL = module
            return module
        logger.error("[codeagent] protocol/ directory not found; "
                     "the device socket will refuse connections")
        raise ProtocolUnavailable("protocol schemas not found")


def reset_protocol_cache() -> None:
    """Forget the loaded validator (tests; a deploy that fixes the dependency needs a restart anyway)."""
    global _PROTOCOL
    with _PROTOCOL_LOCK:
        _PROTOCOL = None
        _CLIENT_TYPES.clear()
        sys.modules.pop(_VALIDATOR_MODULE_NAME, None)


def _client_types(protocol) -> frozenset:
    """The client-to-server message types, read from the schema itself."""
    key = id(protocol)
    if key not in _CLIENT_TYPES:
        doc = protocol.load_json(protocol.V1_DIR / "envelope.json")
        _CLIENT_TYPES[key] = frozenset(doc["x-m2c-types"]["client_to_server"])
    return _CLIENT_TYPES[key]


# ---------------------------------------------------------------------
# The seam for the run manager
# ---------------------------------------------------------------------

class MessageHandler:
    """What S06 (and S09) implement. See the module docstring, "The seam"."""

    async def on_message(self, conn: DeviceConnection, msg: dict) -> None:
        raise NotImplementedError

    async def on_disconnect(self, conn: DeviceConnection) -> None:
        return None


class UnavailableHandler(MessageHandler):
    """Installed until a run manager is: says so instead of staying silent."""

    async def on_message(self, conn, msg):
        await conn.send_error("internal", MSG_NO_RUN_MANAGER, ref_id=_ref_id(msg))


_HANDLER: MessageHandler = UnavailableHandler()


def set_message_handler(handler: MessageHandler) -> None:
    global _HANDLER
    _HANDLER = handler


def get_message_handler() -> MessageHandler:
    return _HANDLER


# ---------------------------------------------------------------------
# Frames and validation helpers
# ---------------------------------------------------------------------

_DISCONNECT, _TOO_BIG, _BINARY, _TEXT = "disconnect", "too_big", "binary", "text"


def _ref_id(obj) -> "str | None":
    """The id of the message an error answers, if it is a well-formed lower-case UUID
    (error.json's ref_id has that pattern); otherwise nothing."""
    if isinstance(obj, dict):
        value = obj.get("id")
        if isinstance(value, str) and _UUID_RE.match(value):
            return value
    return None


async def _read_frame(websocket: WebSocket, max_bytes: int):
    """(kind, text) for the next frame. Size is measured in UTF-8 bytes."""
    frame = await websocket.receive()
    if frame["type"] == "websocket.disconnect":
        return _DISCONNECT, None
    text, data = frame.get("text"), frame.get("bytes")
    if text is not None:
        # len(text) * 4 bounds the UTF-8 size from above, so encode only when it could matter.
        if len(text) > max_bytes // 4 and len(text.encode("utf-8", "replace")) > max_bytes:
            return _TOO_BIG, None
        return _TEXT, text
    if data is not None:
        return (_TOO_BIG, None) if len(data) > max_bytes else (_BINARY, None)
    return _BINARY, None


def _parse(text: str):
    """The JSON value, or the sentinel ValueError for anything that is not JSON
    (including nesting too deep for the parser)."""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return ValueError


async def _problem_with(protocol, obj, size: int) -> "str | None":
    """None if `obj` is a valid client message; otherwise the fixed error text."""
    if obj is ValueError:
        return MSG_INVALID
    try:
        if size <= _VALIDATE_INLINE_MAX_BYTES:
            issues = protocol.validate_message(obj, tolerant=True)
        else:
            issues = await run_in_threadpool(protocol.validate_message, obj, tolerant=True)
    except Exception as exc:                  # a validator bug must not take the socket down
        logger.error("[codeagent] validator raised (%s); message refused", type(exc).__name__)
        return MSG_INVALID
    if issues:
        return MSG_INVALID
    if obj["t"] not in _client_types(protocol):
        return MSG_INVALID                    # a server type, or a type this version does not have
    return None


async def _reject(websocket: WebSocket, code: str, message: str, close_code: int, *,
                  ref_id: "str | None" = None, details: "dict | None" = None) -> None:
    """Pre-authentication refusal: one v1 `error`, then the close frame. There is no
    DeviceConnection yet, and the peer may already be gone, so nothing here raises."""
    envelope = connections.make_envelope(
        "error", connections.error_payload(code, message, ref_id=ref_id, details=details))
    try:
        await websocket.send_text(connections.encode(envelope))
        await websocket.close(code=close_code)
    except Exception:
        pass
    logger.info("[codeagent] connection refused code=%s close=%s", code, close_code)


# ---------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------

async def _handshake(websocket, protocol, settings, address):
    """Run the hello/welcome exchange up to authentication. Returns
    (identity, version, hello_payload) on success; None after having answered and
    closed on any failure (or if the peer simply left)."""
    limiter = connections.get_handshake_limiter()

    async def refuse(code, message, close_code, *, count, ref_id=None, details=None):
        if count:
            limiter.record_failure(address)
        await _reject(websocket, code, message, close_code, ref_id=ref_id, details=details)
        return None

    try:
        kind, text = await asyncio.wait_for(
            _read_frame(websocket, settings.max_message_bytes), timeout=settings.hello_timeout_s)
    except asyncio.TimeoutError:
        return await refuse("invalid_message", MSG_HELLO_TIMEOUT, CLOSE_HANDSHAKE_TIMEOUT, count=True)
    if kind == _DISCONNECT:
        return None
    if kind == _TOO_BIG:
        return await refuse("invalid_message", MSG_TOO_BIG, CLOSE_TOO_BIG, count=True)
    if kind == _BINARY:
        return await refuse("invalid_message", MSG_BINARY, CLOSE_BAD_HANDSHAKE, count=True)

    obj = _parse(text)
    ref_id = _ref_id(obj)
    if await _problem_with(protocol, obj, len(text)) is not None or obj["t"] != "hello":
        return await refuse("invalid_message", MSG_NOT_HELLO, CLOSE_BAD_HANDSHAKE,
                            count=True, ref_id=ref_id)

    hello = obj["payload"]
    common = sorted(set(hello["protocol_versions"]) & set(SUPPORTED_VERSIONS))
    if not common:
        return await refuse("protocol_unsupported", MSG_UNSUPPORTED, CLOSE_PROTOCOL_UNSUPPORTED,
                            count=False, ref_id=ref_id,
                            details={"min_protocol": MIN_PROTOCOL, "max_protocol": MAX_PROTOCOL})
    version = common[-1]

    try:
        identity = await run_in_threadpool(devices.authenticate_credential, hello["device_credential"])
    except devices.DeviceRevoked:
        return await refuse("device_revoked", MSG_REVOKED, CLOSE_DEVICE_REVOKED,
                            count=False, ref_id=ref_id)
    except devices.InvalidCredential:
        return await refuse("unauthorized", MSG_UNAUTHORIZED, CLOSE_UNAUTHORIZED,
                            count=True, ref_id=ref_id)
    except Exception as exc:                  # database fault: refuse (fail closed), do not blame the device
        logger.error("[codeagent] credential check failed (%s)", type(exc).__name__)
        return await refuse("internal", MSG_INTERNAL, CLOSE_INTERNAL, count=False, ref_id=ref_id)
    return identity, version, hello


# ---------------------------------------------------------------------
# After the handshake
# ---------------------------------------------------------------------

async def _read_loop(conn: DeviceConnection, websocket, protocol, settings, handler) -> None:
    """Receive, validate, answer pings, hand everything else on. Returns when the
    connection is over (peer left, or this loop closed it)."""
    invalid_in_a_row = 0
    while True:
        kind, text = await _read_frame(websocket, settings.max_message_bytes)
        if kind == _DISCONNECT:
            conn.note_disconnected()
            return
        if kind == _TOO_BIG:
            await _safe_error(conn, "invalid_message", MSG_TOO_BIG)
            await conn.close(CLOSE_TOO_BIG, "message too big")
            return

        obj, problem = None, MSG_BINARY
        if kind == _TEXT:
            obj = _parse(text)
            problem = await _problem_with(protocol, obj, len(text))
            if problem is None and obj["t"] == "hello":
                problem = MSG_SECOND_HELLO
        if problem is not None:
            invalid_in_a_row += 1
            if not await _safe_error(conn, "invalid_message", problem, ref_id=_ref_id(obj)):
                return
            if invalid_in_a_row >= settings.max_consecutive_invalid:
                logger.warning("[codeagent] closing device=%s user=%s: too many invalid messages",
                               conn.device_id, conn.user_id)
                await conn.close(CLOSE_POLICY, "too many invalid messages")
                return
            continue
        invalid_in_a_row = 0

        msg_type = obj["t"]
        try:
            if msg_type == "ping":
                nonce = obj["payload"].get("nonce")
                await conn.send_message("pong", {} if nonce is None else {"nonce": nonce})
            else:
                await handler.on_message(conn, obj)
        except ConnectionClosed:
            return
        except Exception as exc:              # a handler bug must not take the socket down
            logger.error("[codeagent] handler failed type=%s device=%s user=%s error=%s",
                         msg_type, conn.device_id, conn.user_id, type(exc).__name__)
            if not await _safe_error(conn, "internal", MSG_INTERNAL, ref_id=_ref_id(obj)):
                return


async def _safe_error(conn, code, message, *, ref_id=None) -> bool:
    """Send an error; False if the connection is gone."""
    try:
        await conn.send_error(code, message, ref_id=ref_id)
        return True
    except ConnectionClosed:
        return False


async def _recheck_loop(conn: DeviceConnection, interval: float) -> str:
    """Ask, every `interval` seconds, whether the device is still active. Returns
    "revoked" or "fault" when it must be disconnected; never returns while all is well."""
    while True:
        await asyncio.sleep(interval)
        try:
            active = await run_in_threadpool(devices.is_device_active, conn.user_id, conn.device_id)
        except Exception as exc:
            logger.error("[codeagent] device re-check failed device=%s user=%s error=%s",
                         conn.device_id, conn.user_id, type(exc).__name__)
            return "fault"
        if not active:
            return "revoked"


async def _serve(conn, websocket, protocol, settings, handler) -> None:
    reader = asyncio.create_task(_read_loop(conn, websocket, protocol, settings, handler))
    checker = asyncio.create_task(_recheck_loop(conn, settings.revalidate_interval_s))
    closed = asyncio.create_task(conn.wait_closed())
    tasks = (reader, checker, closed)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if checker.done():
            outcome = checker.result()
            if outcome == "revoked":
                logger.info("[codeagent] device revoked while connected device=%s user=%s",
                            conn.device_id, conn.user_id)
                await _safe_error(conn, "device_revoked", MSG_REVOKED)
                await conn.close(CLOSE_DEVICE_REVOKED, "device revoked")
            else:
                await _safe_error(conn, "internal", MSG_INTERNAL)
                await conn.close(CLOSE_INTERNAL, "internal error")
        elif reader.done() and reader.exception() is not None:
            logger.error("[codeagent] receive loop failed device=%s user=%s error=%s",
                         conn.device_id, conn.user_id, type(reader.exception()).__name__)
            await _safe_error(conn, "internal", MSG_INTERNAL)
            await conn.close(CLOSE_INTERNAL, "internal error")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/v1/code/device")
async def device_socket(websocket: WebSocket) -> None:
    """See the module docstring. Never raises into the ASGI server: every failure
    path answers in v1 shape (when the peer is still there) and closes."""
    settings = SETTINGS
    await websocket.accept()
    address = websocket.client.host if websocket.client else None
    conn = None
    try:
        try:
            protocol = await run_in_threadpool(load_protocol)
        except ProtocolUnavailable:
            await _reject(websocket, "internal", MSG_INTERNAL, CLOSE_INTERNAL)
            return

        wait = connections.get_handshake_limiter().retry_after(address)
        if wait > 0:
            await _reject(websocket, "rate_limited", MSG_RATE_LIMITED, CLOSE_RATE_LIMITED,
                          details={"retry_after_s": wait})
            return

        accepted = await _handshake(websocket, protocol, settings, address)
        if accepted is None:
            return
        identity, version, hello = accepted

        conn = DeviceConnection(
            websocket, user_id=identity.user_id, device_id=identity.device_id, protocol=version,
            client_version=hello["client_version"], capabilities=tuple(hello["capabilities"]))
        del hello                             # it carried the credential: do not keep it in scope
        registry = connections.get_registry()
        superseded = registry.add(conn)
        if superseded is not None:
            logger.info("[codeagent] device=%s user=%s: new connection supersedes the old one",
                        conn.device_id, conn.user_id)
            await superseded.close(CLOSE_SUPERSEDED, "superseded")

        await conn.send_message("welcome", {
            "protocol": version,
            "min_protocol": MIN_PROTOCOL,
            "token_ttl_s": devices.DEVICE_JWT_TTL_SECONDS,
            "limits": {"max_parallel_runs": settings.max_parallel_runs,
                       "max_message_bytes": settings.max_message_bytes},
            "server_version": SERVER_VERSION,
        })
        logger.info("[codeagent] device connected device=%s user=%s", conn.device_id, conn.user_id)
        await _serve(conn, websocket, protocol, settings, _HANDLER)
    except ConnectionClosed:
        pass                                  # the peer left during the welcome
    except Exception as exc:
        logger.error("[codeagent] device socket failed (%s)", type(exc).__name__)
        if conn is not None:
            await _safe_error(conn, "internal", MSG_INTERNAL)
            await conn.close(CLOSE_INTERNAL, "internal error")
        else:
            await _reject(websocket, "internal", MSG_INTERNAL, CLOSE_INTERNAL)
    finally:
        if conn is not None:
            connections.get_registry().remove(conn)
            try:
                await _HANDLER.on_disconnect(conn)
            except Exception as exc:
                logger.error("[codeagent] on_disconnect failed device=%s user=%s error=%s",
                             conn.device_id, conn.user_id, type(exc).__name__)
            logger.info("[codeagent] device disconnected device=%s user=%s", conn.device_id, conn.user_id)
