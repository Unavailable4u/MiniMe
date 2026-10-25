"""
m2c/src/m2c/link/client.py -- Patch C04: the device link, the WebSocket
client that keeps this machine connected to the MiniMe server.

WHAT IT IS
----------
``DeviceLink`` is the one long-lived connection of build plan section 7 / P00
(protocol/README.md).  It does the plumbing so that the rest of the client can
think in runs and events:

* connects to ``wss://<server>/v1/code/device`` and says ``hello`` with the
  device credential (C03's ``load_credential()``); there is no JWT on the
  socket, the credential itself authenticates it.  It then insists on a valid
  ``welcome`` and refuses a server whose ``min_protocol`` is above anything
  this client speaks (``LinkProtocolUnsupportedError``);
* reconnects by itself with exponential backoff and full jitter
  (reconnect.py), stopping for good on ``unauthorized``, ``device_revoked``,
  ``protocol_unsupported`` and being superseded by a newer connection;
* after every (re)connect sends ``run.resume{last_seq}`` for each active run
  and keeps the local event log exactly equal to the server's stream: no
  duplicate, no missing, no out-of-order event (see below);
* correlates requests and answers (ping/pong by nonce, ``error`` by
  ``ref_id``) with timeouts, and runs an application-level keepalive;
* shuts down gracefully: ``aclose()`` closes the socket with a normal close
  frame, cancels every task it started and leaves the store consistent.

What it deliberately does NOT do: execute tools (C05+).  A ``tool_proposed``
event is stored and handed to a callback; whoever owns the policy engine
answers with ``send_approval`` / ``send_tool_result``.

THE EVENT LOG IS THE SOURCE OF TRUTH
------------------------------------
Every accepted server event is appended to the session's event log (C02) before
anything else happens with it.  The server's stream for one run is numbered
``seq = 1, 2, 3, ...`` with no gaps (protocol/README.md section 4), and the link
accepts an event only when ``seq == last_seq + 1``:

* ``seq <= last_seq``  a duplicate (the server re-delivers after a resume):
                       dropped, not stored, not shown to callbacks;
* ``seq >  last_seq+1`` a gap: the event is NOT stored (storing out of order
                       would leave a hole that nothing ever fills).  The link
                       asks again with ``run.resume{last_seq}``, at most once
                       per ``resume_retry_s`` however many events pile up;
* ``seq == last_seq+1`` stored, then ``last_seq`` advances, then callbacks run.
                       The store write comes first, so a crash at any moment
                       leaves a log from which the cursor is recovered
                       exactly: ``recover_last_seq()``.

``last_seq`` is not kept anywhere else: after a restart ``attach_run()`` reads
it back from the log (and fails closed, with ``StoreError``, if the stored
server seqs are not 1..n in order, because resuming from a wrong cursor would
lose events silently).

Two kinds of in-order event are not ordinary and still consume their seq, so
that the log has no hole and the stream never stalls on them (the server would
replay the same event on every resume):

* a *malformed* event (fails its schema): a content-free ``server.invalid``
  marker is stored; callbacks are not called;
* a ``run_event`` of a type this client does not know (a newer server; the
  tolerant reader rule of protocol/README.md section 8): a
  ``server.run_event.unknown`` marker is stored.

Unknown FIELDS of known events are dropped before storing.  Storing happens
through ``store.append_event`` one event per transaction (a few hundred
microseconds each); a failing store is FINAL (``LinkStoreError``): carrying on
would let the device log fall behind the run.

STORED EVENT TYPES (the ``type`` column of the local log)
---------------------------------------------------------
    server.run_event.<subtype>   text_delta, text_done, tool_started,
                                 tool_finished, state_changed, usage, notice
    server.tool_proposed
    server.budget_warning
    server.run_event.unknown, server.invalid     the markers above

each with ``data = {"server_seq", "id", "ts", "payload"}`` (no ``payload`` on a
``server.invalid`` marker).  ``error`` and ``pong`` are not part of the
replayable stream and are not stored.

NEVER LOG CONTENT, NEVER LEAK THE CREDENTIAL
--------------------------------------------
(build plan 11.4.)  The link logs only message types, ids, counts and error
codes, always through ``log_event`` with allowlisted keys, never with
``exc_info``.  test_link_logging.py checks it with caplog and by parsing this
file.  For tracebacks the discipline is C03's:

* the credential is revealed in exactly one function (``_send_hello``) and
  lives in a local only inside a ``Secret``;
* a received message is handled by a function that never raises on its content
  and RETURNS the exception the caller should raise (``_on_raw`` ->
  ``_dispatch`` -> ...), so a traceback never contains a frame that held the
  message text as a local;
* exceptions carry fixed text, and are created outside ``except`` blocks, so
  they have no ``__cause__``/``__context__`` that would drag the original
  (possibly content-bearing) exception along.

Place this file at: m2c/src/m2c/link/client.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from typing import Any, Callable, Iterable, Mapping

from websockets.asyncio.client import connect as _websockets_connect
from websockets.exceptions import ConnectionClosed

from .. import __version__
from ..auth.credentials import Credential, Secret, ServerMismatchError, load_credential
from ..config import normalize_server_url, server_url_from_env
from ..logging_setup import log_event
from ..store import (
    StoreError,
    append_event,
    create_session,
    iter_events,
    require_session,
)
from .reconnect import (
    ERROR_CODES,
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUPPORTED_PROTOCOLS = (1,)
"""Every protocol version this client speaks, sent in ``hello``."""

SOCKET_PATH = "/v1/code/device"
"""S04: WS /v1/code/device (backend/codeagent/ws.py)."""

CLOSE_UNAUTHORIZED = 4401
CLOSE_DEVICE_REVOKED = 4403
CLOSE_SUPERSEDED = 4409
CLOSE_PROTOCOL_UNSUPPORTED = 4426
CLOSE_RATE_LIMITED = 4429
# (4408 hello timeout, 1009 too big and 1011 internal are ordinary, retried drops.)

DEFAULT_MAX_MESSAGE_BYTES = 1_048_576
"""What the server accepts when welcome.limits does not say (protocol/README.md)."""

MAX_INCOMING_BYTES = 4 * 1024 * 1024
"""Largest frame this client reads.  The server's own limit for what it sends is
well below this; the bound only protects memory against a broken peer."""

TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "expired"})
STREAM_TYPES = frozenset({"run_event", "tool_proposed", "budget_warning"})

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$")
_SUBTYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_OUTGOING_SCHEMAS = {
    "hello": "hello.json",
    "run.start": "run.start.json",
    "user_message": "user_message.json",
    "tool_result": "tool_result.json",
    "tool_stream": "tool_stream.json",
    "approval": "approval.json",
    "cancel": "cancel.json",
    "run.resume": "run.resume.json",
    "ping": "ping.json",
}


# ---------------------------------------------------------------------------
# A small schema interpreter over the client's own copy of protocol/v1
# ---------------------------------------------------------------------------
# jsonschema is not a runtime dependency (the runtime has ONE: websockets), so
# the link checks messages with this ~80 line interpreter of the keywords the
# P00 schemas use, reading the schema files shipped in m2c/protocol/v1 (a
# byte-identical copy, enforced by protocol/validate.py --check-client-copy).
# It is used in two modes:
#
#   strict    (what the client SENDS) every keyword is enforced, including
#             additionalProperties: false and the if/then/allOf of history
#             items.  A mismatch is a bug of the caller: ValueError, locally,
#             before anything is sent.
#   tolerant  (what the client RECEIVES, protocol/README.md section 8) unknown
#             properties are dropped, allOf/if/then are skipped (they select
#             per-tool input schemas, which are the policy engine's business),
#             and the CLEANED copy is what gets stored.
class _Invalid(Exception):
    """A value does not fit its schema.  ``where`` names the field (never its value)."""

    def __init__(self, where: str) -> None:
        super().__init__(where)
        self.where = where


@lru_cache(maxsize=None)
def _doc(name: str) -> Mapping[str, Any]:
    node = resources.files("m2c").joinpath("protocol", "v1", *name.split("/"))
    return json.loads(node.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _pattern(text: str) -> "re.Pattern[str]":
    return re.compile(text)


def _resolve(ref: str, doc_name: str) -> tuple[Any, str]:
    file_part, _, pointer = ref.partition("#")
    name = file_part or doc_name
    node: Any = _doc(name)
    for part in [p for p in pointer.split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, name


def _is_type(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "null":
        return value is None
    return False


_ANNOTATIONS = frozenset({"$ref", "description", "title", "$schema", "$id", "$defs", "x-m2c-data-class", "x-m2c-secret"})


def _check(value: Any, schema: Mapping[str, Any], doc: str, where: str, strict: bool) -> Any:
    """Check ``value`` against ``schema``; return the cleaned value (tolerant mode
    drops unknown properties) or raise ``_Invalid``."""
    cleaned = value
    if "$ref" in schema:
        target, target_doc = _resolve(schema["$ref"], doc)
        cleaned = _check(value, target, target_doc, where, strict)
        if not (set(schema) - _ANNOTATIONS):
            return cleaned
        cleaned = value
    kind = schema.get("type")
    if kind is not None and not _is_type(value, kind):
        raise _Invalid(where)
    if "const" in schema and (value != schema["const"] or isinstance(value, bool) != isinstance(schema["const"], bool)):
        raise _Invalid(where)
    if "enum" in schema and (isinstance(value, (dict, list)) or value not in schema["enum"]):
        raise _Invalid(where)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1 << 62):
            raise _Invalid(where)
        pattern = schema.get("pattern")
        if pattern is not None:
            rx = _pattern(pattern)
            matched = rx.fullmatch(value) if pattern.startswith("^") and pattern.endswith("$") else rx.search(value)
            if not matched:
                raise _Invalid(where)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise _Invalid(where)
        if "maximum" in schema and value > schema["maximum"]:
            raise _Invalid(where)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1 << 62):
            raise _Invalid(where)
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise _Invalid(where)
        if "items" in schema:
            cleaned = [_check(item, schema["items"], doc, f"{where}[{i}]", strict) for i, item in enumerate(value)]
    elif isinstance(value, dict):
        for name in schema.get("required", ()):
            if name not in value:
                raise _Invalid(f"{where}.{name}")
        properties = schema.get("properties")
        if properties is not None:
            known: dict[str, Any] = {}
            for key, sub in properties.items():
                if key in value:
                    known[key] = _check(value[key], sub, doc, f"{where}.{key}", strict)
            if strict and schema.get("additionalProperties") is False and any(k not in properties for k in value):
                raise _Invalid(where)
            cleaned = known
    if strict:
        for sub in schema.get("allOf", ()):
            _check(value, sub, doc, where, True)
        if "if" in schema and "then" in schema:
            try:
                _check(value, schema["if"], doc, where, True)
                applies = True
            except _Invalid:
                applies = False
            if applies:
                _check(value, schema["then"], doc, where, True)
    return cleaned


def _validate_outgoing(t: str, payload: Any) -> None:
    """ValueError unless ``payload`` is valid for client message type ``t``."""
    name = _OUTGOING_SCHEMAS[t]
    bad: str | None = None
    try:
        _check(payload, _doc(name), name, "payload", True)
    except _Invalid as exc:
        bad = exc.where
    if bad is not None:
        raise ValueError(f"The {t} message does not match protocol v1 ({bad}).")


def _shape_stream_event(t: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """For a received stream event return ``(stored_type, cleaned_payload)``.

    Raises ``_Invalid`` for a malformed one.  An unknown ``run_event`` subtype
    (a newer server) is not an error: it returns ``("server.run_event.unknown",
    {"type": <subtype>})`` with nothing else kept."""
    if t == "run_event":
        doc = _doc("run_event.json")
        subtype = payload.get("type")
        if not isinstance(subtype, str):
            raise _Invalid("payload.type")
        if subtype not in doc["properties"]["type"]["enum"]:
            return "server.run_event.unknown", ({"type": subtype} if _SUBTYPE_RE.fullmatch(subtype) else {})
        return f"server.run_event.{subtype}", _check(payload, doc["$defs"][subtype], "run_event.json", "payload", False)
    name = f"{t}.json"
    return f"server.{t}", _check(payload, _doc(name), name, "payload", False)


# ---------------------------------------------------------------------------
# Public value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkEvent:
    """One accepted server stream event, as handed to callbacks.

    ``type`` is the protocol message type (``run_event``, ``tool_proposed`` or
    ``budget_warning``); ``subtype`` is the ``run_event`` payload type
    (``text_delta``, ``state_changed``, ...) and None for the other two.
    ``seq`` is the SERVER's per-run sequence number (the local log has its own).
    ``payload`` is the cleaned payload exactly as stored."""

    run_id: str
    seq: int
    id: str
    ts: str
    type: str
    payload: dict = field(repr=False)

    @property
    def subtype(self) -> str | None:
        return self.payload.get("type") if self.type == "run_event" else None


@dataclass(frozen=True)
class ServerError:
    """An ``error`` message that did not settle a waiting request.  ``code`` is
    always one of the protocol's codes (an unknown one is reported as
    ``internal``); ``ref_id`` is the id of the client message it answers."""

    code: str
    ref_id: str | None = None
    run_id: str | None = None
    details: dict | None = None
    message: str = field(default="", repr=False)


@dataclass(frozen=True)
class RunInfo:
    """A snapshot of what the link knows about one run."""

    run_id: str
    last_seq: int
    terminal: bool
    lost: bool


@dataclass(frozen=True)
class Welcome:
    protocol: int
    min_protocol: int
    token_ttl_s: int
    limits: dict
    server_version: str | None


@dataclass
class LinkStats:
    """Counters, for tests and ``m2c doctor``-style diagnostics."""

    connections: int = 0
    events: int = 0
    duplicates: int = 0
    gaps: int = 0
    invalid: int = 0
    unknown_subtype: int = 0
    unknown_run: int = 0
    dropped: int = 0


@dataclass(frozen=True)
class LinkCallbacks:
    """Simple synchronous callbacks.  All optional.  An exception raised by a
    callback is caught and counted in the log (by type name only): a UI bug must
    not kill the connection.

    on_event            every accepted stream event, in order, once
                        (``tool_proposed`` and ``budget_warning`` included)
    on_tool_proposed    additionally, each ``tool_proposed`` (the hook where the
                        approval flow of C06 plugs in)
    on_budget_warning   additionally, each ``budget_warning``
    on_error            an ``error`` message that no waiting request consumed
    on_state            the link's state changed (connecting, connected, ...)
    """

    on_event: Callable[[LinkEvent], Any] | None = None
    on_tool_proposed: Callable[[LinkEvent], Any] | None = None
    on_budget_warning: Callable[[LinkEvent], Any] | None = None
    on_error: Callable[[ServerError], Any] | None = None
    on_state: Callable[[str], Any] | None = None


class _Run:
    __slots__ = ("run_id", "last_seq", "terminal", "lost", "resume_asked_at")

    def __init__(self, run_id: str, last_seq: int = 0, terminal: bool = False) -> None:
        self.run_id = run_id
        self.last_seq = last_seq
        self.terminal = terminal
        self.lost = False
        self.resume_asked_at: float | None = None


class _Pending:
    __slots__ = ("kind", "run_id", "future", "nonce")

    def __init__(self, kind: str, run_id: str | None, future: "asyncio.Future[None]", nonce: str | None = None) -> None:
        self.kind = kind
        self.run_id = run_id
        self.future = future
        self.nonce = nonce


# ---------------------------------------------------------------------------
# Small helpers (no content ever reaches a log or an exception from here)
# ---------------------------------------------------------------------------
def device_socket_url(server_url: str) -> str:
    """``https://host[/base]`` -> ``wss://host[/base]/v1/code/device`` (``http`` -> ``ws``)."""
    base = normalize_server_url(server_url)
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + SOCKET_PATH
    return "ws://" + base[len("http://"):] + SOCKET_PATH


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _envelope(t: str, payload: dict, msg_id: str | None = None, run_id: str | None = None) -> dict:
    out: dict[str, Any] = {"v": 1, "t": t, "id": msg_id or str(uuid.uuid4()), "ts": _now_ts(), "payload": payload}
    if run_id is not None:
        out["run_id"] = run_id
    return out


def _encode(message: Mapping[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _utf8_size(text: str) -> int:
    """Size of ``text`` as it goes on the wire, or -1 if it cannot be encoded."""
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return -1


def _reject_constant(_name: str) -> Any:
    raise ValueError("NaN and Infinity are not valid JSON")


def _json_object(raw: Any) -> dict | None:
    """Parse a received frame into a dict, or None.  Never raises, and keeps the
    text out of any traceback by returning from its own frame."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _is_int(value: Any, low: int | None = None, high: int | None = None) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def _short_str(value: Any, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _envelope_ok(data: Mapping[str, Any]) -> bool:
    """The parts of the envelope every message must have (protocol/v1/envelope.json),
    read tolerantly: ``id`` and ``ts`` are checked where they are stored."""
    if not _is_int(data.get("v")) or data["v"] != 1:
        return False
    if not isinstance(data.get("t"), str) or not isinstance(data.get("payload"), dict):
        return False
    run_id = data.get("run_id")
    if run_id is not None and not (isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id)):
        return False
    seq = data.get("seq")
    if seq is not None and not _is_int(seq, 1):
        return False
    return True


def _clean_details(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("min_protocol", "max_protocol"):
        if _is_int(value.get(key), 1):
            out[key] = value[key]
    if _is_int(value.get("retry_after_s"), 0):
        out["retry_after_s"] = value["retry_after_s"]
    reset_at = value.get("reset_at")
    if isinstance(reset_at, str) and _TS_RE.fullmatch(reset_at):
        out["reset_at"] = reset_at
    call_id = value.get("call_id")
    if isinstance(call_id, str) and _CALL_ID_RE.fullmatch(call_id):
        out["call_id"] = call_id
    return out or None


def _code_of(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code in ERROR_CODES else "none"


def _error_exception(code: str, details: dict | None) -> LinkError | None:
    """The exception for an error code that ends the link or the connection."""
    best = max(SUPPORTED_PROTOCOLS)
    if code == "unauthorized":
        return LinkUnauthorizedError()
    if code == "device_revoked":
        return LinkDeviceRevokedError()
    if code == "protocol_unsupported":
        return LinkProtocolUnsupportedError((details or {}).get("min_protocol"), best)
    return None


def _judge_welcome(raw: Any) -> "Welcome | BaseException":
    """Decide what the server's first message means: a ``Welcome``, or the
    exception the caller should raise.  Returns instead of raising so that the
    text of the message never sits in a frame of a traceback."""
    data = _json_object(raw)
    if data is None or not _envelope_ok(data):
        return LinkConnectionError("The server's first message was not a valid protocol message.")
    t, payload = data["t"], data["payload"]
    if t == "error":
        code = payload.get("code")
        code = code if isinstance(code, str) and code in ERROR_CODES else "internal"
        details = _clean_details(payload.get("details"))
        fatal = _error_exception(code, details)
        if fatal is not None:
            return fatal
        if code == "rate_limited":
            return LinkRateLimitedError((details or {}).get("retry_after_s", 0))
        return LinkConnectionError("The server refused the hello.")
    if t != "welcome":
        return LinkConnectionError("The server's first message was not a welcome.")
    protocol, minimum, ttl, limits = payload.get("protocol"), payload.get("min_protocol"), payload.get("token_ttl_s"), payload.get("limits")
    version = payload.get("server_version")
    limits_ok = (
        isinstance(limits, dict)
        and _is_int(limits.get("max_parallel_runs"), 1)
        and (limits.get("max_message_bytes") is None or _is_int(limits.get("max_message_bytes"), 1024))
    )
    if not (_is_int(protocol, 1) and _is_int(minimum, 1) and _is_int(ttl, 1, 86400) and limits_ok
            and (version is None or _short_str(version, 64) is not None)):
        return LinkConnectionError("The server's welcome was malformed.")
    best = max(SUPPORTED_PROTOCOLS)
    if minimum > best or protocol not in SUPPORTED_PROTOCOLS:
        return LinkProtocolUnsupportedError(max(minimum, protocol), best)
    kept = {"max_parallel_runs": limits["max_parallel_runs"]}
    if limits.get("max_message_bytes") is not None:
        kept["max_message_bytes"] = limits["max_message_bytes"]
    return Welcome(protocol, minimum, ttl, kept, version)


def recover_last_seq(store: sqlite3.Connection, run_id: str) -> tuple[int, bool]:
    """``(last_seq, terminal)`` of a run, read back from its event log.

    ``last_seq`` is the highest SERVER seq stored (0 for none), ``terminal``
    whether the last server event was a ``state_changed`` into a terminal
    state.  The client's own events in the log (user messages, tool results) are
    skipped.  Fails closed: raises ``StoreError`` unless the stored server seqs
    are exactly 1, 2, 3, ... in order, because a cursor taken from a damaged log
    would make the next resume skip events silently.  Raises
    ``UnknownSessionError`` for a session that does not exist.
    """
    require_session(store, run_id)
    expected = 0
    last: Any = None
    for event in iter_events(store, run_id):
        if not event.type.startswith("server."):
            continue
        data = event.data
        seq = data.get("server_seq") if isinstance(data, dict) else None
        expected += 1
        if not _is_int(seq) or seq != expected:
            raise StoreError("the device log's server events are not numbered 1, 2, 3, ... in order")
        last = event
    if last is None:
        return 0, False
    payload = last.data.get("payload")
    terminal = (
        last.type == "server.run_event.state_changed"
        and isinstance(payload, dict)
        and payload.get("state") in TERMINAL_STATES
    )
    return expected, terminal


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------
class DeviceLink:
    """The device's connection to the server.  See the module docstring.

    Typical use::

        link = DeviceLink(open_db(), callbacks=LinkCallbacks(on_event=show))
        link.start()                       # connects and keeps itself connected
        await link.wait_connected(10)
        run_id = await link.start_run(project_path=cwd, mode="ask",
                                      cwd_label="my-project", instructions=text)
        await link.send_user_message(run_id, "hello")
        ...
        await link.aclose()

    or ``async with DeviceLink(...) as link:``.  ``wait_connected`` and
    ``wait_closed`` raise the exception that ended the link, if it failed.

    Parameters (all keyword-only; everything but ``store`` is optional):

    credential_loader   ``() -> Credential``; default C03's ``load_credential``,
                        called again for EVERY connection so that a logout or a
                        re-login takes effect
    server_url          only to check that it names the server that issued the
                        credential (default: ``$M2C_SERVER_URL`` when set); the
                        socket always goes to the credential's own server
    callbacks           ``LinkCallbacks``
    capabilities, client_version   the corresponding ``hello`` fields
    backoff             ``BackoffPolicy`` (default 1 s, x2, cap 30 s, stable 10 s)
    clock, sleep, rng   injectable: ``clock() -> seconds`` (default
                        ``time.monotonic``), ``await sleep(seconds)`` (used for
                        the reconnect delays only; default ``asyncio.sleep``),
                        ``rng() -> [0, 1)`` (the jitter source)
    keepalive_interval_s / keepalive_timeout_s   application-level ping; None
                        turns the keepalive off
    handshake_timeout_s, request_timeout_s, close_timeout_s   timeouts
    resume_retry_s      minimum time between two gap-triggered resume requests
    resume_state        ``(run_id) -> dict | None``: state to attach to a
                        ``run.resume`` (the body of a ``run.start``, with
                        ``history``), so a server that lost the run can rebuild
                        it; an exception from it is logged by type and the
                        resume goes out without state
    connector           ``connector(url, **options) -> awaitable connection``;
                        default ``websockets.asyncio.client.connect`` (tests
                        wrap it)
    """

    def __init__(
        self,
        store: sqlite3.Connection,
        *,
        credential_loader: Callable[[], Credential] = load_credential,
        server_url: str | None = None,
        callbacks: LinkCallbacks | None = None,
        capabilities: Iterable[str] = (),
        client_version: str = __version__,
        backoff: BackoffPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        keepalive_interval_s: float | None = 25.0,
        keepalive_timeout_s: float = 10.0,
        handshake_timeout_s: float = 10.0,
        request_timeout_s: float = 30.0,
        close_timeout_s: float = 5.0,
        resume_retry_s: float = 10.0,
        resume_state: Callable[[str], Mapping[str, Any] | None] | None = None,
        connector: Callable[..., Any] = _websockets_connect,
    ) -> None:
        if isinstance(capabilities, (str, bytes)):
            raise ValueError("capabilities must be a sequence of names")
        self._hello_fields: dict[str, Any] = {
            "protocol_versions": list(SUPPORTED_PROTOCOLS),
            "client_version": client_version,
            "capabilities": list(capabilities),
        }
        # Refuse locally a hello the server would refuse (not retryable, so it
        # must not become a retry loop).  The placeholder is built, not a literal.
        _validate_outgoing("hello", {**self._hello_fields, "device_credential": "x" * 16})
        self._server_url = None if server_url is None else normalize_server_url(server_url)
        self._store = store
        self._credential_loader = credential_loader
        self._callbacks = callbacks or LinkCallbacks()
        self._backoff_policy = backoff or BackoffPolicy()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._keepalive_interval_s = keepalive_interval_s
        self._keepalive_timeout_s = keepalive_timeout_s
        self._handshake_timeout_s = handshake_timeout_s
        self._request_timeout_s = request_timeout_s
        self._close_timeout_s = close_timeout_s
        self._resume_retry_s = resume_retry_s
        self._resume_state = resume_state
        self._connector = connector

        self._state = "idle"
        self._state_waiters: list["asyncio.Future[None]"] = []
        self._ws: Any = None
        self._connected_at: float | None = None
        self._runs: dict[str, _Run] = {}
        self._pending: dict[str, _Pending] = {}
        self._by_nonce: dict[str, _Pending] = {}
        self._tasks: set["asyncio.Task[Any]"] = set()
        self._task: "asyncio.Task[Any] | None" = None
        self._runner: "asyncio.Task[Any] | None" = None
        self._closing = False
        self._finalized = False

        self.limits: dict | None = None
        self.server_version: str | None = None
        self.protocol: int | None = None
        self.failure: BaseException | None = None
        self.stats = LinkStats()

    def __repr__(self) -> str:
        return f"DeviceLink(state={self._state!r}, runs={len(self._runs)})"

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> str:
        """idle, connecting, connected, reconnecting, closed or failed."""
        return self._state

    @property
    def pending_count(self) -> int:
        """Requests (pings, run starts) waiting for their answer."""
        return len(self._pending)

    @property
    def active_tasks(self) -> int:
        """Tasks this link started that have not finished (0 after ``aclose``)."""
        return sum(1 for task in self._tasks if not task.done())

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        waiters, self._state_waiters = self._state_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._call(self._callbacks.on_state, state)

    def _call(self, callback: Callable[[Any], Any] | None, argument: Any) -> None:
        if callback is None:
            return
        failed: str | None = None
        try:
            callback(argument)
        except Exception as exc:  # noqa: BLE001 - a UI bug must not kill the link; only the type is kept
            failed = type(exc).__name__
        if failed is not None:
            log_event(logger, logging.WARNING, "link.callback.failed", exc_type=failed)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "asyncio.Task[Any]":
        """Start connecting in the background; returns the supervising task."""
        if self._closing or self._finalized:
            raise LinkClosedError()
        if self._task is None:
            self._task = self._spawn(self._supervise())
        return self._task

    def _spawn(self, coroutine: Any) -> "asyncio.Task[Any]":
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _supervise(self) -> None:
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - already stored in self.failure; wait_closed() raises it
            return

    async def __aenter__(self) -> "DeviceLink":
        self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Return once connected.  Raises the failure that ended the link
        (``failed``), ``LinkClosedError`` (``closed``) or ``TimeoutError``."""

        async def waiter() -> None:
            while True:
                if self._state == "connected":
                    return
                if self._state == "failed" and self.failure is not None:
                    raise self.failure
                if self._state in ("closed", "failed"):
                    raise LinkClosedError()
                future = asyncio.get_running_loop().create_future()
                self._state_waiters.append(future)
                await future

        await (waiter() if timeout is None else asyncio.wait_for(waiter(), timeout))

    async def wait_closed(self) -> None:
        """Wait until the link has stopped.  Raises the failure if it ended in
        one (revoked device, unsupported protocol, store failure, ...); returns
        quietly after a normal close."""
        while self._state not in ("closed", "failed"):
            future = asyncio.get_running_loop().create_future()
            self._state_waiters.append(future)
            await future
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        """Close the link: a normal close frame, every task cancelled, every
        waiter released.  Idempotent; works on a link that never started."""
        if self._finalized:
            return
        self._closing = True
        runner = self._runner or self._task
        if runner is not None and not runner.done() and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        self._finalize(None)

    def _finalize(self, failure: BaseException | None) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._closing = True
        if failure is not None:
            self.failure = failure
        self._set_state("failed" if failure is not None else "closed")
        self._settle_all(failure if failure is not None else LinkClosedError())

    def _settle_all(self, exc: BaseException) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(exc)

    # -- the connect / reconnect loop --------------------------------------
    async def run(self) -> None:
        """Connect, serve, reconnect, until closed or a final error.  Raises the
        final error (it is also in ``self.failure``).  ``start()`` runs this in a
        task; call it directly to own the task yourself (cancel it to stop)."""
        if self._closing or self._finalized:
            raise LinkClosedError()
        if self._runner is not None:
            raise RuntimeError("the device link is already running")
        self._runner = asyncio.current_task()
        backoff = Backoff(self._backoff_policy, rng=self._rng)
        failure: BaseException | None = None
        try:
            while True:
                outcome = await self._attempt()
                connected_at = self._connected_at
                kept_up_s = 0.0 if connected_at is None else max(0.0, self._clock() - connected_at)
                if connected_at is not None:
                    backoff.record_connection(kept_up_s)
                    log_event(logger, logging.INFO, "link.disconnected", code=_code_of(outcome),
                              exc_type=type(outcome).__name__, duration_ms=int(kept_up_s * 1000))
                else:
                    log_event(logger, logging.INFO, "link.connect.failed", code=_code_of(outcome),
                              exc_type=type(outcome).__name__)
                kind = classify(outcome)
                if kind != "retry":
                    failure = outcome
                    log_event(logger, logging.ERROR, "link.stopped", code=_code_of(outcome),
                              exc_type=type(outcome).__name__)
                    raise failure
                delay = backoff.next_delay(retry_after_of(outcome))
                self._set_state("reconnecting")
                log_event(logger, logging.INFO, "link.backoff", count=backoff.failures, duration_ms=int(delay * 1000))
                await self._sleep(delay)
        finally:
            self._runner = None
            self._finalize(failure)

    async def _attempt(self) -> BaseException:
        """One connection attempt, from credential to disconnect.  Returns why it
        ended (it never raises except for cancellation)."""
        self._connected_at = None
        try:
            return await self._serve_once()
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            return exc

    async def _serve_once(self) -> BaseException:
        credential = self._credential_loader()
        self._check_issuer(credential)
        self._set_state("connecting")
        ws = await self._connector(
            device_socket_url(credential.server_url),
            max_size=MAX_INCOMING_BYTES,
            ping_interval=None,                      # liveness is the application-level ping (see _keepalive)
            open_timeout=self._handshake_timeout_s,
            close_timeout=self._close_timeout_s,
        )
        keepalive: "asyncio.Task[Any] | None" = None
        try:
            welcome = await self._handshake(ws, credential)
            if isinstance(welcome, BaseException):
                return welcome
            self._connected_at = self._clock()           # before anything else: the stability clock starts at welcome
            self.limits, self.server_version, self.protocol = dict(welcome.limits), welcome.server_version, welcome.protocol
            self.stats.connections += 1
            self._ws = ws
            failed = await self._resume_all(ws)
            if failed is not None:
                return failed
            self._set_state("connected")
            log_event(logger, logging.INFO, "link.connected", version=welcome.protocol, device_id=credential.device_id)
            if self._keepalive_interval_s is not None:
                keepalive = self._spawn(self._keepalive(ws))
            return await self._read(ws)
        finally:
            self._ws = None
            if keepalive is not None:
                keepalive.cancel()
                await asyncio.gather(keepalive, return_exceptions=True)
            self._connection_lost()
            await self._close_socket(ws)

    def _check_issuer(self, credential: Credential) -> None:
        wanted = self._server_url if self._server_url is not None else server_url_from_env()
        if wanted is not None and wanted != credential.server_url:
            raise ServerMismatchError(
                "The configured server is not the one this device signed in to, so the credential was not "
                "sent. Unset M2C_SERVER_URL, or run 'm2c logout' and then 'm2c login' for the new server."
            )

    async def _close_socket(self, ws: Any) -> None:
        try:
            await asyncio.wait_for(ws.close(1000), self._close_timeout_s + 1.0)
        except (ConnectionClosed, TimeoutError, OSError):
            return

    def _connection_lost(self) -> None:
        """Pings cannot survive a connection; run starts can (the resume covers them)."""
        for pending in list(self._pending.values()):
            if pending.kind == "ping" and not pending.future.done():
                pending.future.set_exception(LinkClosedError() if self._closing else LinkConnectionError())

    # -- handshake -----------------------------------------------------------
    async def _send_hello(self, ws: Any, credential: Credential) -> None:
        """The ONLY place the device credential is revealed (test_link_logging
        checks).  The frame lives in a ``Secret``; nothing here is bound to a
        name as raw text, so a traceback through this frame shows no secret."""
        frame = Secret(
            _encode(_envelope("hello", {**self._hello_fields, "device_credential": credential.device_credential.reveal()}))
        )
        await ws.send(frame.reveal())

    async def _receive_welcome(self, ws: Any) -> "Welcome | BaseException":
        return _judge_welcome(await ws.recv())

    async def _handshake(self, ws: Any, credential: Credential) -> "Welcome | BaseException":
        verdict: "Welcome | BaseException | None" = None
        timed_out = closed = False
        try:
            await self._send_hello(ws, credential)
            verdict = await asyncio.wait_for(self._receive_welcome(ws), self._handshake_timeout_s)
        except TimeoutError:
            timed_out = True
        except ConnectionClosed:
            closed = True
        if timed_out:
            return LinkConnectionError("The server did not answer the hello in time.")
        if closed or verdict is None:
            return self._closed_verdict(ws)
        return verdict

    def _closed_verdict(self, ws: Any) -> BaseException:
        """What a closed connection means, from its close code alone."""
        code = getattr(ws, "close_code", None)
        if code == CLOSE_UNAUTHORIZED:
            return LinkUnauthorizedError()
        if code == CLOSE_DEVICE_REVOKED:
            return LinkDeviceRevokedError()
        if code == CLOSE_SUPERSEDED:
            return LinkSupersededError()
        if code == CLOSE_PROTOCOL_UNSUPPORTED:
            return LinkProtocolUnsupportedError(None, max(SUPPORTED_PROTOCOLS))
        if code == CLOSE_RATE_LIMITED:
            return LinkRateLimitedError(0)
        return LinkConnectionError()

    # -- reading ---------------------------------------------------------------
    async def _read(self, ws: Any) -> BaseException:
        """Process messages until the connection ends; returns why."""
        while True:
            verdict: BaseException | None = None
            closed = False
            try:
                verdict = await self._on_raw(await ws.recv())
            except ConnectionClosed:
                closed = True
            if closed:
                return self._closed_verdict(ws)
            if verdict is not None:
                return verdict

    async def _on_raw(self, raw: Any) -> BaseException | None:
        """Handle one received frame.  Returns the exception that should end the
        connection, or None.  A bug while handling it ends the link with a fixed
        message, never with the frame's content in the traceback."""
        crashed: str | None = None
        try:
            return await self._dispatch(raw)
        except ConnectionClosed:
            raise
        except Exception as exc:  # noqa: BLE001 - converted below, outside the except block
            crashed = type(exc).__name__
        log_event(logger, logging.ERROR, "link.internal_error", exc_type=crashed)
        return LinkInternalError()

    async def _dispatch(self, raw: Any) -> BaseException | None:
        data = _json_object(raw)
        if data is None or not _envelope_ok(data):
            self.stats.dropped += 1
            log_event(logger, logging.WARNING, "link.message.dropped", count=self.stats.dropped)
            return None
        t = data["t"]
        if t == "pong":
            self._on_pong(data["payload"])
            return None
        if t == "error":
            return self._on_error(data)
        if t in STREAM_TYPES:
            return await self._on_stream(data)
        return None          # welcome mid-session or a type from a newer server: ignored (tolerant reader)

    def _on_pong(self, payload: Mapping[str, Any]) -> None:
        nonce = payload.get("nonce")
        pending = self._by_nonce.get(nonce) if isinstance(nonce, str) else None
        if pending is not None and not pending.future.done():
            pending.future.set_result(None)

    def _on_error(self, data: Mapping[str, Any]) -> BaseException | None:
        payload = data["payload"]
        code = payload.get("code")
        code = code if isinstance(code, str) and code in ERROR_CODES else "internal"
        ref_id = payload.get("ref_id")
        ref_id = ref_id if isinstance(ref_id, str) and _UUID_RE.fullmatch(ref_id) else None
        run_id = data.get("run_id")
        details = _clean_details(payload.get("details"))
        message = payload.get("message")
        if run_id is None:
            log_event(logger, logging.WARNING, "link.error.received", code=code)
        else:
            log_event(logger, logging.WARNING, "link.error.received", code=code, run_id=run_id)
        fatal = _error_exception(code, details)
        if fatal is not None:
            return fatal                          # revoked or unauthorized at ANY time is final
        pending = self._pending.get(ref_id) if ref_id is not None else None
        if pending is not None:
            if not pending.future.done():
                if code == "rate_limited":
                    pending.future.set_exception(LinkRateLimitedError((details or {}).get("retry_after_s", 0)))
                else:
                    pending.future.set_exception(LinkRequestError(code, ref_id, details))
            return None
        if code == "run_not_found" and run_id is not None and run_id in self._runs:
            self._runs[run_id].lost = True
            self._settle_starts(run_id, LinkRequestError(code, ref_id, details))
        self._call(
            self._callbacks.on_error,
            ServerError(code, ref_id, run_id, details, message[:500] if isinstance(message, str) else ""),
        )
        return None

    def _settle_starts(self, run_id: str, exc: BaseException | None) -> None:
        for pending in list(self._pending.values()):
            if pending.kind == "start" and pending.run_id == run_id and not pending.future.done():
                if exc is None:
                    pending.future.set_result(None)
                else:
                    pending.future.set_exception(exc)

    async def _on_stream(self, data: Mapping[str, Any]) -> BaseException | None:
        run_id, seq = data.get("run_id"), data.get("seq")
        if run_id is None or seq is None:
            self.stats.dropped += 1
            log_event(logger, logging.WARNING, "link.message.dropped", count=self.stats.dropped)
            return None
        run = self._runs.get(run_id)
        if run is None:
            self.stats.unknown_run += 1
            log_event(logger, logging.WARNING, "link.event.unknown_run", run_id=run_id, seq=seq)
            return None
        if seq <= run.last_seq:
            self.stats.duplicates += 1
            return None
        if seq > run.last_seq + 1:
            self.stats.gaps += 1
            log_event(logger, logging.WARNING, "link.event.gap", run_id=run_id, seq=seq, count=run.last_seq)
            await self._request_resume(run)
            return None
        return self._accept(run, data)

    def _accept(self, run: _Run, data: Mapping[str, Any]) -> BaseException | None:
        """Store one in-order event, advance the cursor, tell the callbacks."""
        t, seq = data["t"], data["seq"]
        ident, stamp = _short_str(data.get("id"), 64), _short_str(data.get("ts"), 40)
        malformed = False
        try:
            stored_type, cleaned = _shape_stream_event(t, data["payload"])
        except _Invalid:
            malformed = True
        if malformed or ident is None or stamp is None:
            stored_type = "server.invalid"
            record: dict[str, Any] = {"server_seq": seq, "id": ident or "", "ts": stamp or "", "t": t}
            deliver = False
            self.stats.invalid += 1
            log_event(logger, logging.WARNING, "link.event.invalid", run_id=run.run_id, seq=seq)
        else:
            record = {"server_seq": seq, "id": ident, "ts": stamp, "payload": cleaned}
            deliver = stored_type != "server.run_event.unknown"
            if not deliver:
                self.stats.unknown_subtype += 1
                log_event(logger, logging.INFO, "link.event.unknown_subtype", run_id=run.run_id, seq=seq)
        failed = False
        try:
            append_event(self._store, run.run_id, stored_type, record)
        except (StoreError, sqlite3.Error, OSError):
            failed = True
        if failed:
            log_event(logger, logging.ERROR, "link.store.failed", run_id=run.run_id, seq=seq)
            return LinkStoreError()
        run.last_seq = seq
        run.resume_asked_at = None
        self.stats.events += 1
        if stored_type == "server.run_event.state_changed" and cleaned.get("state") in TERMINAL_STATES:
            run.terminal = True
        self._settle_starts(run.run_id, None)
        if deliver:
            event = LinkEvent(run.run_id, seq, ident, stamp, t, cleaned)
            self._call(self._callbacks.on_event, event)
            if t == "tool_proposed":
                self._call(self._callbacks.on_tool_proposed, event)
            elif t == "budget_warning":
                self._call(self._callbacks.on_budget_warning, event)
        return None

    # -- resume ------------------------------------------------------------
    def _resume_payload(self, run: _Run, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"last_seq": run.last_seq}
        failed_type: str | None = None
        if state is None and self._resume_state is not None:
            try:
                state = self._resume_state(run.run_id)
            except Exception as exc:  # noqa: BLE001 - the resume must go out anyway
                failed_type = type(exc).__name__
                state = None
        if failed_type is not None:
            log_event(logger, logging.WARNING, "link.resume.state_failed", run_id=run.run_id, exc_type=failed_type)
        if state is not None:
            try:
                _validate_outgoing("run.start", state)
                _check_cwd_label(state)
                payload["state"] = dict(state)
            except ValueError:
                log_event(logger, logging.WARNING, "link.resume.state_invalid", run_id=run.run_id)
        return payload

    async def _send_resume(self, ws: Any, run: _Run, state: Mapping[str, Any] | None = None) -> None:
        payload = self._resume_payload(run, state)
        too_big = False
        try:
            await self._transmit(ws, "run.resume", payload, run_id=run.run_id)
        except LinkMessageTooLargeError:
            too_big = True
        if too_big and "state" in payload:
            log_event(logger, logging.WARNING, "link.resume.state_too_large", run_id=run.run_id)
            await self._transmit(ws, "run.resume", {"last_seq": run.last_seq}, run_id=run.run_id)
        elif too_big:
            raise LinkMessageTooLargeError(None)
        run.resume_asked_at = self._clock()
        log_event(logger, logging.INFO, "link.resume.sent", run_id=run.run_id, seq=run.last_seq)

    async def _resume_all(self, ws: Any) -> BaseException | None:
        """Right after welcome: ``run.resume`` for every run still going."""
        for run in list(self._runs.values()):
            if run.terminal or run.lost:
                continue
            closed = False
            try:
                await self._send_resume(ws, run)
            except ConnectionClosed:
                closed = True
            if closed:
                return self._closed_verdict(ws)
        return None

    async def _request_resume(self, run: _Run) -> None:
        """A gap: ask again from ``last_seq``, but not more often than ``resume_retry_s``."""
        ws = self._ws
        if ws is None:
            return
        asked = run.resume_asked_at
        if asked is not None and self._clock() - asked < self._resume_retry_s:
            return
        await self._send_resume(ws, run)

    # -- keepalive -----------------------------------------------------------
    async def _keepalive(self, ws: Any) -> None:
        """Half-open connections look healthy to TCP; only an answer proves
        liveness.  A ping that goes unanswered drops the connection (which then
        reconnects and resumes)."""
        assert self._keepalive_interval_s is not None
        while True:
            await asyncio.sleep(self._keepalive_interval_s)
            stalled = False
            try:
                await self.ping(timeout=self._keepalive_timeout_s)
            except LinkRequestTimeout:
                stalled = True
            except (LinkConnectionError, LinkClosedError):
                return
            if stalled:
                log_event(logger, logging.WARNING, "link.keepalive.stalled")
                ws.transport.abort()
                return

    # -- sending -------------------------------------------------------------
    def _require_usable(self) -> Any:
        if self._closing or self._finalized:
            raise LinkClosedError()
        ws = self._ws
        if ws is None or self._state != "connected":
            raise LinkNotConnectedError()
        return ws

    async def _transmit(self, ws: Any, t: str, payload: dict, *, run_id: str | None = None, msg_id: str | None = None) -> str:
        """Validate, size-check and send one client message; returns its id."""
        _validate_outgoing(t, payload)
        ident = msg_id or str(uuid.uuid4())
        text = _encode(_envelope(t, payload, ident, run_id))
        size = _utf8_size(text)
        limit = (self.limits or {}).get("max_message_bytes", DEFAULT_MAX_MESSAGE_BYTES)
        if size < 0:
            del text
            raise ValueError("The message contains text that cannot be encoded.")
        if size > limit:
            del text
            raise LinkMessageTooLargeError(limit)
        closed = False
        try:
            await ws.send(text)
        except ConnectionClosed:
            closed = True
        if closed:
            raise LinkNotConnectedError()
        return ident

    async def _send(self, t: str, payload: dict, *, run_id: str | None = None) -> str:
        return await self._transmit(self._require_usable(), t, payload, run_id=run_id)

    @staticmethod
    def _check_run_id(run_id: Any) -> None:
        if not (isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id)):
            raise ValueError("run_id must be 8 to 64 letters, digits, '_' or '-'")

    async def send_user_message(self, run_id: str, text: str) -> str:
        """Send what the person typed; returns the message id (an ``error``
        about it will name it as ``ref_id``)."""
        self._check_run_id(run_id)
        return await self._send("user_message", {"text": text}, run_id=run_id)

    async def send_tool_result(
        self,
        run_id: str,
        call_id: str,
        *,
        ok: bool,
        output: str,
        truncated: bool,
        trust: str,
        exit_code: int | None = None,
        duration_ms: int | None = None,
    ) -> str:
        self._check_run_id(run_id)
        payload: dict[str, Any] = {"call_id": call_id, "ok": ok, "output": output, "truncated": truncated, "trust": trust}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return await self._send("tool_result", payload, run_id=run_id)

    async def send_tool_stream(self, run_id: str, call_id: str, *, stream: str, chunk: str) -> str:
        self._check_run_id(run_id)
        return await self._send("tool_stream", {"call_id": call_id, "stream": stream, "chunk": chunk}, run_id=run_id)

    async def send_approval(self, run_id: str, call_id: str, *, decision: str, scope: str = "once") -> str:
        """The device's decision on a proposed call (the policy engine stays authoritative)."""
        self._check_run_id(run_id)
        return await self._send("approval", {"call_id": call_id, "decision": decision, "scope": scope}, run_id=run_id)

    async def send_cancel(self, run_id: str) -> str:
        self._check_run_id(run_id)
        return await self._send("cancel", {}, run_id=run_id)

    async def ping(self, nonce: str | None = None, *, timeout: float | None = None) -> float:
        """Send a ``ping`` and wait for the ``pong`` that echoes its nonce.
        Returns the round trip in seconds (by the injected clock)."""
        if nonce is None:
            nonce = secrets.token_hex(8)
        elif not (isinstance(nonce, str) and 0 < len(nonce) <= 64):
            raise ValueError("nonce must be a string of 1 to 64 characters")
        ws = self._require_usable()
        if nonce in self._by_nonce:
            raise ValueError("a ping with this nonce is still waiting")
        msg_id = str(uuid.uuid4())
        pending = _Pending("ping", None, asyncio.get_running_loop().create_future(), nonce)
        self._pending[msg_id] = pending
        self._by_nonce[nonce] = pending
        started = self._clock()
        timed_out = False
        try:
            await self._transmit(ws, "ping", {"nonce": nonce}, msg_id=msg_id)
            try:
                await asyncio.wait_for(pending.future, self._request_timeout_s if timeout is None else timeout)
            except TimeoutError:
                timed_out = True
        finally:
            self._pending.pop(msg_id, None)
            self._by_nonce.pop(nonce, None)
        if timed_out:
            raise LinkRequestTimeout()
        return self._clock() - started

    # -- runs ------------------------------------------------------------------
    def run_info(self, run_id: str) -> RunInfo | None:
        run = self._runs.get(run_id)
        return None if run is None else RunInfo(run.run_id, run.last_seq, run.terminal, run.lost)

    def attach_run(self, run_id: str) -> RunInfo:
        """Adopt a run that already has a session in the store (after a restart):
        ``last_seq`` and whether it is finished are read back from its event
        log.  Call it before ``start()`` and the first connection resumes the
        run; on a link that is already connected follow it with ``resume_run``.
        Raises ``UnknownSessionError`` / ``StoreError`` (see ``recover_last_seq``)."""
        self._check_run_id(run_id)
        existing = self._runs.get(run_id)
        if existing is not None:
            return RunInfo(existing.run_id, existing.last_seq, existing.terminal, existing.lost)
        last, terminal = recover_last_seq(self._store, run_id)
        run = self._runs[run_id] = _Run(run_id, last, terminal)
        return RunInfo(run.run_id, run.last_seq, run.terminal, run.lost)

    async def start_run(
        self,
        *,
        mode: str,
        cwd_label: str,
        instructions: str,
        run_id: str | None = None,
        project_path: str | None = None,
        profile: str = "code",
        rules: Iterable[Mapping[str, Any]] = (),
        memory_index: str = "",
        tool_catalog_version: str = "v0",
        model_pref: str = "auto",
        history: Iterable[Mapping[str, Any]] | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> str:
        """Start a run on the server and return its ``run_id``.

        ``run_id`` is minted here (a UUID) when not given, together with its
        session in the store (``project_path`` is then required: the absolute
        path stays on this machine, only ``cwd_label`` -- a name, never a path --
        goes to the server).  With a ``run_id`` the session must already exist and
        have no server events yet (use ``attach_run``/``resume_run`` otherwise).

        With ``wait=True`` (default) it returns after the first event of the
        run arrived, raises ``LinkRequestError``/``LinkRateLimitedError`` if the
        server answered the start with an error (the run is then forgotten),
        and ``LinkRequestTimeout`` after ``timeout`` seconds."""
        payload: dict[str, Any] = {
            "profile": profile,
            "mode": mode,
            "cwd_label": cwd_label,
            "instructions": instructions,
            "rules": [dict(rule) for rule in rules],
            "memory_index": memory_index,
            "tool_catalog_version": tool_catalog_version,
            "model_pref": model_pref,
        }
        if history is not None:
            payload["history"] = [dict(item) for item in history]
        _validate_outgoing("run.start", payload)
        _check_cwd_label(payload)
        if run_id is not None:
            self._check_run_id(run_id)
        elif project_path is None:
            raise ValueError("project_path is required when no run_id is given")
        ws = self._require_usable()
        if run_id is None:
            run_id = str(uuid.uuid4())
            create_session(self._store, project_path=project_path, mode=mode, model_pref=model_pref, session_id=run_id)
        else:
            require_session(self._store, run_id)
            if run_id in self._runs or recover_last_seq(self._store, run_id)[0] > 0:
                raise ValueError("this run already has events; use attach_run() and resume_run() for it")
        self._runs[run_id] = _Run(run_id)
        msg_id = str(uuid.uuid4())
        pending: _Pending | None = None
        if wait:
            pending = _Pending("start", run_id, asyncio.get_running_loop().create_future())
            self._pending[msg_id] = pending
        timed_out = False
        try:
            await self._transmit(ws, "run.start", payload, run_id=run_id, msg_id=msg_id)
            if pending is not None:
                try:
                    await asyncio.wait_for(pending.future, self._request_timeout_s if timeout is None else timeout)
                except TimeoutError:
                    timed_out = True
        except (LinkRequestError, LinkRateLimitedError, LinkNotConnectedError, LinkMessageTooLargeError, ValueError):
            self._runs.pop(run_id, None)
            raise
        finally:
            self._pending.pop(msg_id, None)
        if timed_out:
            raise LinkRequestTimeout()
        return run_id

    async def resume_run(self, run_id: str, state: Mapping[str, Any] | None = None) -> RunInfo:
        """Send ``run.resume{last_seq}`` for a run now, clearing a ``lost`` mark.
        ``state`` (the body of a ``run.start``, usually with ``history`` replayed
        from the device log) lets a server that no longer knows the run rebuild
        it; without it the ``resume_state`` hook, if any, is asked."""
        self._check_run_id(run_id)
        ws = self._require_usable()
        if run_id not in self._runs:
            self.attach_run(run_id)
        run = self._runs[run_id]
        run.lost = False
        await self._send_resume(ws, run, state)
        return RunInfo(run.run_id, run.last_seq, run.terminal, run.lost)


def _check_cwd_label(payload: Mapping[str, Any]) -> None:
    """A label is a name, never a path (the schema already forbids separators and
    control characters; this keeps the promise explicit where a path could leak)."""
    label = payload.get("cwd_label")
    if label is not None and ("/" in label or "\\" in label):
        raise ValueError("cwd_label must be a name, not a path")
