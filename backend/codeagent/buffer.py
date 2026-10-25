"""
codeagent/buffer.py -- S06 (part b): the ephemeral, encrypted run buffer.

While a run is active the server must keep two things so the loop can call the model
and so a device that drops can come back (build plan section O1, "Server, ephemeral
content"):

  * the CONVERSATION: the run's state (profile, mode, instructions, ...) and every
    history item, so the next model call can be built and prompt-cache prefixes stay
    stable;
  * the REPLAY EVENTS: every stream message (run_event, tool_proposed, budget_warning)
    the server sent to the device, stored VERBATIM, so run.resume{last_seq} can
    re-deliver everything after last_seq with the same id and seq (protocol/README.md
    section 5). They carry reply text and tool output, so they are content too.

Both live ONLY here: in Redis, through the existing Upstash client (memory.bus.redis),
encrypted with AES-GCM, expiring, and deleted when the run ends. Never Postgres, never a
log line. runs.py (S06 part a) is the content-free record of the same run; this module is
the content half and never touches that table.

WHERE THIS SITS
---------------
    ws.py (S04)      hands run-scoped messages to the installed MessageHandler
    S14              the router that is that handler
    runs.py (S06a)   the run row and its state machine
    buffer.py (here) the encrypted conversation + replay buffer
    S06c             glues the two (delete on a terminal state, run_guard cancel keys,
                     expire_stale, run.resume rebuild), then S07 runs the loop

Nothing here installs a handler, opens a socket, calls a model or reads Postgres. Every
function is synchronous (like db.cursor and runs.py); the async router calls them
through run_in_threadpool.

WHAT IS STORED (three keys per run, one prefix, nothing but the run id in a key name)
-------------------------------------------------------------------------------------
    run_buffer:<run_id>:conv      list of sealed records: the state, then the history
                                  and every item appended since
    run_buffer:<run_id>:replay    list of sealed records: a header, then the events
    run_buffer:<run_id>:meta      hash: "meta" (the sealed creation stamp), "seq" (the
                                  stream counter), "conv_bytes" / "replay_bytes" (how
                                  much each stream holds; sizes only)

The prefix is not run_guard's ("run_guard:") and the keys do not go through memory.bus's
app_slug namespacing: this module talks to the raw client, like eo/run_guard.py does,
because a run is a property of the account's device, not of a project.

ENCRYPTION
----------
* AES-256-GCM (the `cryptography` package, already pinned). One key PER RUN, derived
  with HKDF-SHA256 from the server secret and the run id, so a leaked run key opens one
  run and nothing else.
* A fresh random 96-bit nonce for every record. (A key only ever seals the records of
  one run, far below the birthday bound for random nonces.)
* Associated data binds each record to its run id, its STREAM (conv / replay / meta) and
  its key id. A record copied under another run's keys, moved between streams, or
  relabelled with another key id fails authentication. Nothing is decoded before it has
  been authenticated.
* Wire form of every record:  "<format version>.<key id>.<base64url(nonce + ciphertext
  + tag)>". The key id is what lets the secret be rotated: new records are sealed under
  the current id; records under an id in RUN_BUFFER_PREVIOUS_SECRETS still read. An id
  that is not configured is BufferCorrupt, never a guess.
* Content is never in a key name, a field name or a counter. The only plaintext in Redis
  is the three key names (run id), the field names and the two size counters.

ENVIRONMENT (new in this patch; .env.example is deliberately not edited -- add these there)
-------------------------------------------------------------------------------------------
    RUN_BUFFER_SECRET            REQUIRED. At least 32 characters (surrounding whitespace
                                 is ignored). Missing or weak => BufferNotConfigured, and
                                 no buffer is created (fail closed). Generate one with
                                 `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    RUN_BUFFER_KEY_ID            Optional. Id of the current secret: 1-16 of A-Z a-z 0-9
                                 _ - . Default "k1". Set but malformed => refused.
    RUN_BUFFER_PREVIOUS_SECRETS  Optional. JSON object {"<key id>": "<secret>"} of older
                                 secrets, kept so older records still read during and
                                 after a rotation. Malformed, weak or reusing the current
                                 id => refused. Never echoed.
    RUN_BUFFER_IDLE_TTL_SECONDS  Optional, default 7200 (decision D6: idle 2 hours).
    RUN_BUFFER_HARD_CAP_SECONDS  Optional, default 86400 (D6: hard cap 24 hours).
                                 A value that is not a whole number of seconds from 1 up
                                 to 30 days falls back to the default.

To rotate: set RUN_BUFFER_KEY_ID to a new id, RUN_BUFFER_SECRET to a new secret, and put
the old pair under RUN_BUFFER_PREVIOUS_SECRETS. After the longest a run can live (24 h)
drop the old pair; any record still under it becomes BufferCorrupt, which S06c answers
by deleting the buffer and rebuilding it from the device's history.

TTLS (D6)
---------
* IDLE: every key of a run gets IDLE_TTL_SECONDS at creation, and EVERY read and write
  (read_conversation, append_conversation, next_seq, append_replay, replay_after, touch)
  re-applies it to ALL THREE keys in one atomic request, so the keys can never expire
  at different times. exists() is a status check, not use: it refreshes nothing.
* HARD CAP: HARD_CAP_SECONDS from creation, enforced two ways. (1) The creation time is
  stored sealed in the meta hash; every operation reads it, so a buffer past its cap is
  refused (BufferExpired) and its keys are deleted, EVEN IF someone refreshed the Redis
  TTLs by hand or by a bug. A forged stamp does not verify without the secret. (2) The
  refresh never asks for more than the time the cap has left: TTL = min(idle, remaining).
* One deliberate exception. The tiny meta key (no content: the sealed stamp and two
  size counters) never gets a TTL shorter than EXPIRED_MARKER_SECONDS (60 s, and never
  more than the idle TTL). In the last minute before the cap it therefore outlives the
  content keys, so a caller that arrives just after the cap gets a definite
  BufferExpired instead of a BufferMissing it could mistake for "never existed". The
  content keys die exactly at the cap; the marker is deleted by whoever sees it, or
  expires within a minute on its own.
* A second create_buffer for a live run id can never lengthen anything: it claims the id
  with HSETNX and sets its TTL with EXPIRE ... LT ("only ever shortens"), so a refused
  create leaves the first buffer's TTL as it was.

SIZE: WHY A LONG CONVERSATION CANNOT EXCEED A REQUEST LIMIT
-----------------------------------------------------------
Upstash's REST API rejects a request over its max request size. The current docs
(upstash.com/docs/redis/troubleshooting/max_request_size_exceeded and the pricing page)
say 10 MB on the Free, Pay-as-you-go and Fixed 250 MB plans (an older page and an old
blog post said 1 MB), and a max RECORD size of 100 MB per key. The docs add
that a list may grow past the request limit; only one request may not. So:

* A conversation is NEVER one value. It is a Redis LIST of small sealed records, and an
  item bigger than CHUNK_BYTES (48 KiB of plaintext) is split into several records,
  each carrying (item id, index, count), which are reassembled on read.
* A write is a series of requests of at most TX_BUDGET_BYTES (256 KiB) of records, each
  an atomic MULTI/EXEC that also refreshes the TTLs and bumps the size counter.
  REQUEST_GUARD_BYTES (512 KiB) is a last-line check: a request that would be larger is
  refused before it is sent. The budget is 40x under today's 10 MB and still half of
  the old 1 MB, so the design survives either number.
* A read is a series of LRANGE pages, sized from the records just received so that a
  reply stays around READ_PAGE_BYTES (512 KiB) and never more than READ_MAX_PAGE
  records.
* Two caps keep a key far below the 100 MB record limit: MAX_ITEM_BYTES (8 MiB) for one
  item, and MAX_STREAM_BYTES (32 MiB of plaintext, about 44 MB sealed) per stream. A
  write that would pass the stream cap is refused WHOLE (BufferFull) before anything is
  sent.

HALF-WRITTEN AND DUPLICATED WRITES
----------------------------------
A multi-record item spans several requests, so one of them can fail part way. Every
record carries its item id, index and count, and the reader returns an item only when
ALL its records are present: a half-written item is invisible, and later writes work as
if it had never been tried (a few orphan records stay until the TTL). An append_*() that
raises BufferUnavailable may therefore have stored a PREFIX of the items it was given
(never half an item); a caller that retries the whole call must expect that.
upstash_redis retries a request once when a reply is lost, so a push can land twice: the
reader keeps the first copy of every (item id, index) and ignores the rest, and replay
events collapse by seq. Events come back in seq order whatever order they were stored in.

FAIL CLOSED (this is not metering)
----------------------------------
* No usable RUN_BUFFER_SECRET => BufferNotConfigured and Redis is not touched. Only
  delete_buffer() works without it: deleting needs no key.
* Any Redis fault (connection, timeout, a driver bug, an unexpected reply) =>
  BufferUnavailable with fixed text and NO chained cause (a driver's message can carry a
  host, a key or a value). Only the exception's TYPE is logged. S07 turns it into
  paused_error. Domain refusals (BufferMissing, BufferExpired, BufferExists, BufferFull,
  BufferCorrupt, InvalidBufferInput, BufferNotConfigured) are never reported as
  "unavailable".
* Nothing is logged but the TYPE of a fault. No content, no key name, no run id, no
  secret, no error text (tests/static/test_buffer_logging.py reads this file).
* Content is validated before any request: an item must be a JSON object (finite
  numbers, no sets or objects) under MAX_ITEM_BYTES; a replay message must be a stream
  message of THIS run (run_event, tool_proposed or budget_warning, with id, seq >= 1,
  payload). A refusal stores nothing.

THE REDIS INTERFACE
-------------------
The module talks to Redis through a two-method interface (RedisLike), so unit tests
inject an in-memory stand-in (tests/unit/buffer_testkit.FakeRunRedis) and Upstash is
never needed:

    command(*args)       one command, its reply
    atomic(commands)     a list of commands as ONE MULTI/EXEC request; the list of replies

In production _UpstashAdapter maps command() to client.execute([...]) and atomic() to
client.multi() ... exec() (NOT pipeline(): a pipeline is not a transaction), and
normalises booleans to 0/1. The client is memory.bus.redis, imported on first use, so
importing this module does not import the bus or Upstash (eo/run_guard.py does the same).

DECISIONS THIS PATCH HAD TO MAKE (each is a small change if you disagree)
-------------------------------------------------------------------------
* Existence is atomic. Appends use RPUSHX (push only if the list exists), so a write to
  an expired buffer can never bring a half-buffer back; a command that would recreate a
  key (HINCRBY in next_seq) is followed by a check and the leftovers are deleted.
* BufferExpired is a subclass of BufferMissing: "the buffer is gone" is what S06c acts
  on (rebuild from the device's history); the subclass only says why.
* delete_buffer deletes this module's three keys and nothing else. The run_guard cancel
  keys for the run are S06c's to delete (it owns the terminal-state hook); this module
  does not import run_guard.
* The sealed meta stamp is the source of truth for the hard cap, not the Redis TTL.
* last_seq: create_buffer(last_seq=N) starts the counter at N so a rebuilt buffer
  continues from the device's last received seq and next_seq() returns N + 1.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import struct
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Configurable constants (decision D6) and size budgets
# ---------------------------------------------------------------------

_MAX_CONFIGURABLE_SECONDS = 30 * 24 * 3600


def _env_seconds(name: str, default: int) -> int:
    """A whole number of seconds from the environment, or `default` when the variable
    is unset, empty, not a plain positive integer, or absurdly large (over 30 days).
    A bad setting must never silently turn a 2 hour TTL into forever."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw or not raw.isascii() or not raw.isdigit():
        return default
    value = int(raw)
    if value < 1 or value > _MAX_CONFIGURABLE_SECONDS:
        return default
    return value


IDLE_TTL_SECONDS = _env_seconds("RUN_BUFFER_IDLE_TTL_SECONDS", 2 * 3600)
HARD_CAP_SECONDS = _env_seconds("RUN_BUFFER_HARD_CAP_SECONDS", 24 * 3600)
EXPIRED_MARKER_SECONDS = 60             # floor of the meta key's TTL; see TTLS in the module docstring

CHUNK_BYTES = 48 * 1024                 # plaintext per record
TX_BUDGET_BYTES = 256 * 1024            # sealed bytes of records per write request
REQUEST_GUARD_BYTES = 512 * 1024        # no request may be bigger than this
MAX_ITEM_BYTES = 8 * 1024 * 1024        # one conversation item / one replay message
MAX_STREAM_BYTES = 32 * 1024 * 1024     # plaintext one stream may hold
READ_FIRST_PAGE = 16                    # records in the first LRANGE page
READ_PAGE_BYTES = 512 * 1024            # target size of a later page
READ_MAX_PAGE = 64                      # records in a page, at most
MIN_SECRET_CHARS = 32
DEFAULT_KEY_ID = "k1"

REPLAYABLE_TYPES = frozenset({"run_event", "tool_proposed", "budget_warning"})

# The same pattern as protocol/v1/common.json $defs.run_id (a unit test keeps them equal).
# Always used with fullmatch(): "$" would accept a trailing newline.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
_B64_RE = re.compile(r"[A-Za-z0-9_-]*")

_FORMAT_VERSION = "1"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_HKDF_SALT = b"minime.codeagent.run-buffer.v1"
_AAD_PREFIX = b"minime.run-buffer.v1"

_CONV, _REPLAY, _META = "conv", "replay", "meta"
_STATE, _ITEM, _EVENT, _HEAD = b"S", b"I", b"E", b"H"
_FRAME = struct.Struct(">c16sII")               # kind, item id, record index, record count
_MAX_RECORDS_PER_ITEM = 1 << 16
_ALLOWED_KINDS = {_CONV: (_STATE, _ITEM), _REPLAY: (_HEAD, _EVENT)}


# ---------------------------------------------------------------------
# Errors (fixed text: never an exception's text, a key or a value)
# ---------------------------------------------------------------------

class RunBufferError(Exception):
    """Base of every error this module raises."""


class BufferNotConfigured(RunBufferError):
    """RUN_BUFFER_SECRET (or a rotation setting) is missing or malformed. Fail closed."""


class InvalidBufferInput(RunBufferError):
    """The caller's argument is not acceptable. Raised before Redis is touched."""


class BufferMissing(RunBufferError):
    """There is no buffer for this run (never created, deleted, or idle-expired)."""


class BufferExpired(BufferMissing):
    """The buffer outlived its hard cap. It has been deleted. Treat as BufferMissing."""


class BufferExists(RunBufferError):
    """create_buffer for a run id that already has a buffer. The first is untouched."""


class BufferCorrupt(RunBufferError):
    """A record failed authentication or is malformed: wrong key, wrong run, tampered,
    truncated, relabelled, or sealed under a key id that is no longer configured."""


class BufferFull(RunBufferError):
    """The write would pass MAX_STREAM_BYTES. Nothing was written."""


class BufferUnavailable(RunBufferError):
    """Redis could not be reached or answered something unexpected. Fail closed."""


_UNAVAILABLE = "the run buffer store is unavailable"
_CORRUPT = "a run buffer record failed verification"
_MISSING = "the run buffer does not exist"


# ---------------------------------------------------------------------
# Time (a module attribute so tests can drive it)
# ---------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _now_ms() -> int:
    return int(_now() * 1000)


# ---------------------------------------------------------------------
# Redis access: one thin interface, one production adapter
# ---------------------------------------------------------------------

class RedisLike:
    """The interface the module needs (documentation only; any object with these two
    methods will do)."""

    def command(self, *args):                      # pragma: no cover - interface
        raise NotImplementedError

    def atomic(self, commands):                    # pragma: no cover - interface
        raise NotImplementedError


def _normalise(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value


class _UpstashAdapter:
    """RedisLike over an upstash_redis client, which `get_client` returns on each use
    (so memory.bus is imported lazily and a test can swap the client)."""

    def __init__(self, get_client):
        self._get_client = get_client

    def command(self, *args):
        return _normalise(self._get_client().execute([str(a) for a in args]))

    def atomic(self, commands):
        pipe = self._get_client().multi()                 # MULTI/EXEC, one REST request
        for cmd in commands:
            pipe.execute([str(a) for a in cmd])
        return [_normalise(r) for r in pipe.exec()]


def _bus_redis():
    from memory.bus import redis      # deferred: importing this module needs no Upstash env
    return redis


_REDIS = None                         # tests inject a RedisLike here
_DEFAULT = _UpstashAdapter(_bus_redis)


def _client():
    return _REDIS if _REDIS is not None else _DEFAULT


def _log_fault(exc: BaseException) -> None:
    logger.warning("run buffer: redis request failed (%s)", type(exc).__name__)


def _call(*args):
    """One command. Any fault becomes BufferUnavailable: fixed text, no cause."""
    try:
        return _client().command(*args)
    except Exception as exc:
        _log_fault(exc)
        raise BufferUnavailable(_UNAVAILABLE) from None


def _atomic(commands):
    """Several commands as one MULTI/EXEC request, size-guarded."""
    commands = [tuple(c) for c in commands]
    if sum(len(str(a)) for c in commands for a in c) > REQUEST_GUARD_BYTES:
        raise RunBufferError("a run buffer request would exceed the request size guard")
    try:
        results = _client().atomic(commands)
    except Exception as exc:
        _log_fault(exc)
        raise BufferUnavailable(_UNAVAILABLE) from None
    if not isinstance(results, (list, tuple)) or len(results) != len(commands):
        logger.warning("run buffer: redis answered with an unexpected shape")
        raise BufferUnavailable(_UNAVAILABLE)
    return list(results)


# ---------------------------------------------------------------------
# Keys and validation
# ---------------------------------------------------------------------

def _valid_run_id(run_id) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise InvalidBufferInput("run id is malformed")
    return run_id


def buffer_keys(run_id: str) -> tuple:
    """The three Redis keys of a run: (conversation list, replay list, meta hash)."""
    run_id = _valid_run_id(run_id)
    return (f"run_buffer:{run_id}:conv", f"run_buffer:{run_id}:replay", f"run_buffer:{run_id}:meta")


def _valid_seq(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidBufferInput(f"{what} must be a non-negative integer")
    return value


def _serialize(obj, what: str) -> bytes:
    """A JSON object as compact UTF-8, or InvalidBufferInput. Never echoes the content."""
    if not isinstance(obj, dict):
        raise InvalidBufferInput(f"{what} must be a JSON object")
    try:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise InvalidBufferInput(f"{what} is not valid JSON") from None
    if len(data) > MAX_ITEM_BYTES:
        raise InvalidBufferInput(f"{what} is too large")
    return data


def _valid_message(run_id: str, message) -> bytes:
    """A stream message of THIS run that may be replayed, serialized."""
    if not isinstance(message, dict):
        raise InvalidBufferInput("a replay message must be a JSON object")
    seq, mid = message.get("seq"), message.get("id")
    if (message.get("t") not in REPLAYABLE_TYPES
            or not isinstance(mid, str) or not 1 <= len(mid) <= 128
            or message.get("run_id") != run_id
            or not isinstance(seq, int) or isinstance(seq, bool) or seq < 1
            or not isinstance(message.get("payload"), dict)):
        raise InvalidBufferInput("not a replayable stream message of this run")
    return _serialize(message, "a replay message")


# ---------------------------------------------------------------------
# Configuration, key derivation, sealing
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class _Config:
    key_id: str
    secrets: dict          # key id -> secret (the current one and the retired ones)


def _strong(secret) -> "str | None":
    if isinstance(secret, str) and len(secret.strip()) >= MIN_SECRET_CHARS:
        return secret.strip()
    return None


def _config() -> _Config:
    """Read and validate the settings, on every call (cheap; a test or a rotation takes
    effect at once). Raises BufferNotConfigured with fixed text that never contains a
    value."""
    secret = _strong(os.environ.get("RUN_BUFFER_SECRET"))
    if secret is None:
        raise BufferNotConfigured("RUN_BUFFER_SECRET is not set or is too weak (32 characters minimum)")
    key_id = os.environ.get("RUN_BUFFER_KEY_ID")
    if key_id is None:
        key_id = DEFAULT_KEY_ID
    elif not _KEY_ID_RE.fullmatch(key_id):
        raise BufferNotConfigured("RUN_BUFFER_KEY_ID is malformed")
    secrets = {key_id: secret}
    raw = os.environ.get("RUN_BUFFER_PREVIOUS_SECRETS")
    if raw is not None and raw.strip():
        try:
            previous = json.loads(raw)
        except Exception:
            raise BufferNotConfigured("RUN_BUFFER_PREVIOUS_SECRETS is malformed") from None
        if not isinstance(previous, dict):
            raise BufferNotConfigured("RUN_BUFFER_PREVIOUS_SECRETS is malformed")
        for kid, old in previous.items():
            if (not isinstance(kid, str) or not _KEY_ID_RE.fullmatch(kid) or kid == key_id
                    or _strong(old) is None):
                raise BufferNotConfigured("RUN_BUFFER_PREVIOUS_SECRETS is malformed")
            secrets[kid] = _strong(old)
    return _Config(key_id, secrets)


def _derive_key(secret: str, run_id: str) -> bytes:
    """The run's own 32-byte AES key: HKDF-SHA256 over the server secret, bound to the run id."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT,
        info=b"run-buffer-key\x00" + run_id.encode("utf-8"),
    ).derive(secret.strip().encode("utf-8"))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text) -> bytes:
    """Strict, canonical unpadded base64url, or BufferCorrupt."""
    if not isinstance(text, str) or not _B64_RE.fullmatch(text) or len(text) % 4 == 1:
        raise BufferCorrupt(_CORRUPT)
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        raise BufferCorrupt(_CORRUPT) from None
    if _b64e(raw) != text:
        raise BufferCorrupt(_CORRUPT)
    return raw


def _aad(run_id: str, stream: str, key_id: str) -> bytes:
    return b"\x00".join((_AAD_PREFIX, run_id.encode("utf-8"), stream.encode("ascii"), key_id.encode("ascii")))


class _Box:
    """Seals and opens the records of ONE run, deriving each key once."""

    def __init__(self, run_id: str, config: _Config):
        self.run_id = run_id
        self.config = config
        self._aeads: dict = {}

    def _aead(self, key_id: str):
        aead = self._aeads.get(key_id)
        if aead is None:
            secret = self.config.secrets.get(key_id)
            if secret is None:
                raise BufferCorrupt(_CORRUPT)           # a key id that is not configured
            aead = self._aeads[key_id] = AESGCM(_derive_key(secret, self.run_id))
        return aead

    def seal(self, stream: str, plaintext: bytes) -> str:
        key_id = self.config.key_id
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead(key_id).encrypt(nonce, plaintext, _aad(self.run_id, stream, key_id))
        return f"{_FORMAT_VERSION}.{key_id}.{_b64e(nonce + sealed)}"

    def unseal(self, stream: str, wire) -> bytes:
        if not isinstance(wire, str):
            raise BufferCorrupt(_CORRUPT)
        parts = wire.split(".")
        if len(parts) != 3 or parts[0] != _FORMAT_VERSION or not _KEY_ID_RE.fullmatch(parts[1]):
            raise BufferCorrupt(_CORRUPT)
        raw = _b64d(parts[2])
        if len(raw) < _NONCE_BYTES + _TAG_BYTES:
            raise BufferCorrupt(_CORRUPT)
        try:
            return self._aead(parts[1]).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:],
                                                _aad(self.run_id, stream, parts[1]))
        except InvalidTag:
            raise BufferCorrupt(_CORRUPT) from None


def _box(run_id: str) -> _Box:
    return _Box(run_id, _config())


def _seal(run_id: str, stream: str, plaintext: bytes) -> str:
    """One record under the CURRENT key (used for the meta stamp; tests use it to forge)."""
    return _box(run_id).seal(stream, plaintext)


# ---------------------------------------------------------------------
# Records: chunking, framing, reassembly
# ---------------------------------------------------------------------

def _seal_item(box: _Box, stream: str, kind: bytes, payload: bytes) -> list:
    """One item as sealed records [(wire, plaintext bytes it carries)], each at most
    CHUNK_BYTES of data. All records carry the same random item id and the count."""
    item_id = os.urandom(16)
    parts = [payload[i:i + CHUNK_BYTES] for i in range(0, len(payload), CHUNK_BYTES)] or [b""]
    return [(box.seal(stream, _FRAME.pack(kind, item_id, i, len(parts)) + part), len(part))
            for i, part in enumerate(parts)]


def _groups(records: list) -> list:
    """Split records into groups whose sealed size fits one write request's budget."""
    groups, current, size = [], [], 0
    for rec in records:
        if current and size + len(rec[0]) > TX_BUDGET_BYTES:
            groups.append(current)
            current, size = [], 0
        current.append(rec)
        size += len(rec[0])
    if current:
        groups.append(current)
    return groups


def _read_stream(run_id: str, box: _Box, stream: str) -> list:
    """Every COMPLETE item of a stream as [(kind, payload bytes)], in the order they
    became complete. Authenticates every record; half-written items are skipped and a
    record that appears twice counts once. A stream with no records at all is gone."""
    key = buffer_keys(run_id)[0 if stream == _CONV else 1]
    allowed = _ALLOWED_KINDS[stream]
    pending: dict = {}          # item id -> [kind, count, {index: data}]
    finished: set = set()
    out: list = []
    start, page = 0, READ_FIRST_PAGE
    while True:
        wires = _call("LRANGE", key, start, start + page - 1)
        if not isinstance(wires, list):
            logger.warning("run buffer: redis answered with an unexpected shape")
            raise BufferUnavailable(_UNAVAILABLE)
        if start == 0 and not wires:
            _delete(run_id)
            raise BufferMissing(_MISSING)
        for wire in wires:
            plain = box.unseal(stream, wire)
            if len(plain) < _FRAME.size:
                raise BufferCorrupt(_CORRUPT)
            kind, item_id, index, count = _FRAME.unpack_from(plain)
            if kind not in allowed or not 1 <= count <= _MAX_RECORDS_PER_ITEM or index >= count:
                raise BufferCorrupt(_CORRUPT)
            if item_id in finished:
                continue
            entry = pending.get(item_id)
            if entry is None:
                entry = pending[item_id] = [kind, count, {}]
            elif entry[0] != kind or entry[1] != count:
                raise BufferCorrupt(_CORRUPT)
            if index in entry[2]:
                continue                                    # a duplicated record
            entry[2][index] = plain[_FRAME.size:]
            if len(entry[2]) == count:
                out.append((kind, b"".join(entry[2][i] for i in range(count))))
                finished.add(item_id)
                del pending[item_id]
        start += len(wires)
        if len(wires) < page:
            break
        average = sum(len(w) for w in wires) / len(wires)
        page = max(1, min(READ_MAX_PAGE, int(READ_PAGE_BYTES // max(average, 1))))
    return out                                              # `pending` left over = half-written items


def _loads(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except Exception:
        raise BufferCorrupt(_CORRUPT) from None
    if not isinstance(value, dict):
        raise BufferCorrupt(_CORRUPT)
    return value


# ---------------------------------------------------------------------
# TTLs and the hard cap
# ---------------------------------------------------------------------

def _ttls(created_ms: int, now_ms: int):
    """(content TTL, meta TTL) in whole seconds, or None when the hard cap has passed.
    Content TTL = min(idle, time the cap has left). The meta TTL is the same but never
    below EXPIRED_MARKER_SECONDS (nor above the idle window), so it can outlive the
    content by up to a minute at the very end (see the module docstring)."""
    remaining_ms = created_ms + HARD_CAP_SECONDS * 1000 - now_ms
    if remaining_ms <= 0:
        return None
    remaining = -(-remaining_ms // 1000)
    content = max(1, min(IDLE_TTL_SECONDS, remaining))
    marker = max(content, min(EXPIRED_MARKER_SECONDS, IDLE_TTL_SECONDS))
    return content, marker


def _expire_commands(run_id: str, created_ms: int) -> list:
    """The three EXPIREs that refresh a buffer, or BufferExpired (after deleting it)."""
    ttls = _ttls(created_ms, _now_ms())
    if ttls is None:
        _delete(run_id)
        raise BufferExpired("the run buffer passed its hard cap and was deleted")
    conv, replay, stats = buffer_keys(run_id)
    return [("EXPIRE", conv, ttls[0]), ("EXPIRE", replay, ttls[0]), ("EXPIRE", stats, ttls[1])]


def _delete(run_id: str) -> int:
    return int(_call("DEL", *buffer_keys(run_id)) or 0)


def _gone(run_id: str):
    """The buffer lost a key under us (expiry, deletion, a race): delete what is left
    (a command may have resurrected a key) and report it missing."""
    _delete(run_id)
    raise BufferMissing(_MISSING)


@dataclass(frozen=True)
class _Meta:
    created_ms: int
    conv_bytes: int
    replay_bytes: int


def _count(value) -> int:
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BufferCorrupt(_CORRUPT) from None
    if number < 0:
        raise BufferCorrupt(_CORRUPT)
    return number


def _check(run_id: str, box: _Box) -> _Meta:
    """One read: does the buffer exist, does its creation stamp verify under this key
    and this run, and is it inside the hard cap? Refreshes nothing."""
    reply = _call("HMGET", buffer_keys(run_id)[2], "meta", "conv_bytes", "replay_bytes")
    if not isinstance(reply, list) or len(reply) != 3:
        logger.warning("run buffer: redis answered with an unexpected shape")
        raise BufferUnavailable(_UNAVAILABLE)
    stamp, conv_bytes, replay_bytes = reply
    if stamp is None:
        raise BufferMissing(_MISSING)
    body = _loads(box.unseal(_META, stamp))
    created = body.get("created")
    if body.get("v") != 1 or not isinstance(created, int) or isinstance(created, bool) or created <= 0:
        raise BufferCorrupt(_CORRUPT)
    if _ttls(created, _now_ms()) is None:
        _delete(run_id)
        raise BufferExpired("the run buffer passed its hard cap and was deleted")
    return _Meta(created, _count(conv_bytes), _count(replay_bytes))


def _refresh(run_id: str, meta: _Meta) -> None:
    """Re-apply the idle TTL (never past the cap) to all three keys in one request."""
    results = _atomic(_expire_commands(run_id, meta.created_ms))
    if results[0] != 1 or results[1] != 1:
        _gone(run_id)


# ---------------------------------------------------------------------
# Public API: lifecycle
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Conversation:
    """What read_conversation returns: the run's state and its items, oldest first."""
    state: dict
    items: list


def require_configured() -> None:
    """Raise BufferNotConfigured unless the secret and rotation settings are usable.
    Touches nothing: lets a caller (run_manager.start_run) refuse BEFORE it records a
    run that could never get a buffer."""
    _config()


def create_buffer(run_id: str, *, state: dict, history=(), last_seq: int = 0) -> None:
    """Create the buffer for a run: `state` (a JSON object), the `history` items
    already known (device history on a resume; empty on a new run), and a stream
    counter that continues from `last_seq`.

    Raises BufferExists if the run id already has a buffer (the first is untouched and
    its TTL is not lengthened), BufferNotConfigured, InvalidBufferInput, BufferFull,
    BufferUnavailable. A creation that fails part way deletes what it wrote; if even
    that fails every key already has a TTL, so nothing is immortal."""
    run_id = _valid_run_id(run_id)
    last_seq = _valid_seq(last_seq, "last_seq")
    if not isinstance(history, (list, tuple)):
        raise InvalidBufferInput("history must be a list")
    payloads = [(_STATE, _serialize(state, "state"))]
    payloads += [(_ITEM, _serialize(item, "a history item")) for item in history]
    if sum(len(p) for _, p in payloads) > MAX_STREAM_BYTES:
        raise BufferFull("the conversation is too large for the run buffer")
    box = _box(run_id)
    conv, replay, stats = buffer_keys(run_id)
    created_ms = _now_ms()
    content_ttl, marker_ttl = _ttls(created_ms, created_ms)
    stamp = box.seal(_META, json.dumps({"v": 1, "created": created_ms}).encode("utf-8"))
    records = [rec for kind, payload in payloads for rec in _seal_item(box, _CONV, kind, payload)]
    groups = _groups(records)
    head = box.seal(_REPLAY, _FRAME.pack(_HEAD, os.urandom(16), 0, 1))

    # Claim the id. HSETNX is the atomic "create if absent"; EXPIRE ... LT can only
    # shorten, so a refused claim cannot lengthen the existing buffer's life.
    claimed = _atomic([("HSETNX", stats, "meta", stamp), ("EXPIRE", stats, content_ttl, "LT")])
    if claimed[0] != 1:
        raise BufferExists("a run buffer already exists for this run")
    try:
        _atomic([
            ("DEL", conv, replay),
            ("RPUSH", conv, *[wire for wire, _ in groups[0]]),
            ("RPUSH", replay, head),
            ("HSET", stats, "seq", last_seq, "conv_bytes", sum(n for _, n in groups[0]), "replay_bytes", 0),
            ("EXPIRE", conv, content_ttl), ("EXPIRE", replay, content_ttl), ("EXPIRE", stats, marker_ttl),
        ])
        for group in groups[1:]:
            _push(run_id, created_ms, _CONV, group)
    except Exception:
        try:
            _delete(run_id)
        except RunBufferError:
            pass                      # every key already has a TTL
        raise


def delete_buffer(run_id: str) -> bool:
    """Delete every key of the run. True if anything was deleted. Needs no secret, works
    on a corrupt or foreign buffer, and is idempotent."""
    run_id = _valid_run_id(run_id)
    return _delete(run_id) > 0


def exists(run_id: str) -> bool:
    """Is there a live buffer? Applies the hard cap (a buffer past it is deleted and
    reported absent) but does NOT refresh the TTLs: a status check is not use."""
    run_id = _valid_run_id(run_id)
    try:
        _check(run_id, _box(run_id))
    except BufferMissing:
        return False
    return True


def touch(run_id: str) -> None:
    """Refresh every key's idle TTL (never past the hard cap)."""
    run_id = _valid_run_id(run_id)
    box = _box(run_id)
    _refresh(run_id, _check(run_id, box))


# ---------------------------------------------------------------------
# Public API: the conversation
# ---------------------------------------------------------------------

def read_conversation(run_id: str) -> Conversation:
    """The state and every complete item, oldest first. Refreshes the TTLs."""
    run_id = _valid_run_id(run_id)
    box = _box(run_id)
    meta = _check(run_id, box)
    _refresh(run_id, meta)
    entries = _read_stream(run_id, box, _CONV)
    states = [p for kind, p in entries if kind == _STATE]
    if len(states) != 1:
        raise BufferCorrupt(_CORRUPT)
    return Conversation(_loads(states[0]), [_loads(p) for kind, p in entries if kind == _ITEM])


def append_conversation(run_id: str, *items: dict) -> None:
    """Append one or more items (JSON objects), in order. A BufferUnavailable may leave
    a prefix of the items stored, never half an item (see the module docstring)."""
    run_id = _valid_run_id(run_id)
    if not items:
        raise InvalidBufferInput("nothing to append")
    _append(run_id, _CONV, [(_ITEM, _serialize(item, "a conversation item")) for item in items])


# ---------------------------------------------------------------------
# Public API: the stream counter and the replay events
# ---------------------------------------------------------------------

def next_seq(run_id: str) -> int:
    """The next stream sequence number for the run: atomic, contiguous, 1 for a new run
    (or last_seq + 1 for a rebuilt one), and kept in Redis so it survives a restart."""
    run_id = _valid_run_id(run_id)
    box = _box(run_id)
    meta = _check(run_id, box)
    conv, replay, stats = buffer_keys(run_id)
    results = _atomic([("HINCRBY", stats, "seq", 1)] + _expire_commands(run_id, meta.created_ms))
    if results[1] != 1 or results[2] != 1:
        _gone(run_id)                  # HINCRBY would have recreated the hash: remove it again
    try:
        return int(results[0])
    except (TypeError, ValueError):
        raise BufferUnavailable(_UNAVAILABLE) from None


def append_replay(run_id: str, message: dict) -> None:
    """Store one stream message verbatim for replay."""
    append_replay_many(run_id, [message])


def append_replay_many(run_id: str, messages) -> None:
    """Store several stream messages verbatim, in as few requests as the size budget allows."""
    run_id = _valid_run_id(run_id)
    if not isinstance(messages, (list, tuple)) or not messages:
        raise InvalidBufferInput("nothing to append")
    _append(run_id, _REPLAY, [(_EVENT, _valid_message(run_id, m)) for m in messages])


def replay_after(run_id: str, last_seq: int) -> list:
    """Every stored event with seq > last_seq, verbatim, in seq order, each seq once.
    Refreshes the TTLs."""
    run_id = _valid_run_id(run_id)
    last_seq = _valid_seq(last_seq, "last_seq")
    box = _box(run_id)
    meta = _check(run_id, box)
    _refresh(run_id, meta)
    events: dict = {}
    for kind, payload in _read_stream(run_id, box, _REPLAY):
        if kind != _EVENT:
            continue
        message = _loads(payload)
        seq = message.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise BufferCorrupt(_CORRUPT)
        if seq > last_seq:
            events.setdefault(seq, message)
    return [events[seq] for seq in sorted(events)]


# ---------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------

def _append(run_id: str, stream: str, payloads: list) -> None:
    box = _box(run_id)
    meta = _check(run_id, box)
    used = meta.conv_bytes if stream == _CONV else meta.replay_bytes
    if used + sum(len(p) for _, p in payloads) > MAX_STREAM_BYTES:
        raise BufferFull("the run buffer stream is full")
    records = [rec for kind, payload in payloads for rec in _seal_item(box, stream, kind, payload)]
    for group in _groups(records):
        _push(run_id, meta.created_ms, stream, group)


def _push(run_id: str, created_ms: int, stream: str, group: list) -> None:
    """One atomic request: push the records only if the list exists (RPUSHX), refresh
    all three TTLs, count the bytes. If a key vanished under us, clean up and say so."""
    conv, replay, stats = buffer_keys(run_id)
    target = conv if stream == _CONV else replay
    field = "conv_bytes" if stream == _CONV else "replay_bytes"
    results = _atomic(
        [("RPUSHX", target, *[wire for wire, _ in group])]
        + _expire_commands(run_id, created_ms)
        + [("HINCRBY", stats, field, sum(n for _, n in group))]
    )
    if results[0] == 0 or results[1] != 1 or results[2] != 1:
        _gone(run_id)
