"""
codeagent/emitter.py -- S14 (part b): numbered, stored, delivered server events.

Everything the server streams to a device about a run (reply text, tool proposals, state
changes, budget warnings) is a numbered message: protocol/README.md section 5 says the
server numbers its run stream 1, 2, 3 ... with no gaps, keeps every message so a device
that dropped can ask for the ones it missed, and re-delivers them verbatim. This module is
the one place that does the numbering, the keeping and the sending, so the loop (S07), the
tool broker (S09) and the handlers (S14c-e) never have to agree on how.

    ws.py (S04)        the socket: authenticated, validated; a connection to send on
    router.py          S14a: dispatch by message type
    emitter.py (here)  number it, store it (encrypted), send it if the device is there
    run_handlers.py    S14c-e: calls the emitter around every state change
    buffer.py (S06b)   the encrypted replay stream and the stream counter (next_seq)

ONE EMIT = THREE STEPS, IN THIS ORDER, UNDER ONE LOCK
-----------------------------------------------------
    1. number   seq = buffer.next_seq(run_id)           the counter lives in Redis
    2. store    buffer.append_replay(run_id, envelope)  encrypted, verbatim
    3. send     connection.send_json(envelope)          only if the device is connected

Steps 1 and 2 run in ONE worker-thread call, so a caller that is cancelled between them
cannot burn a seq number (next_seq cannot be given back). Storing comes BEFORE sending: a
device that resumes at any moment finds in the replay everything it could ever have been
sent, and an event it saw but the server lost cannot exist. The price is that a device
that resumes exactly while an event is being sent may get that event twice; the protocol
says duplicates are ignored by seq.

The three steps are atomic PER RUN: every run has its own asyncio.Lock, held from step 1
to the end of step 3, so delivery order equals seq order however many tasks emit at once
(the model stream, the tool broker, a handler). Different runs never wait for each other.

AN OFFLINE DEVICE IS NOT AN ERROR
---------------------------------
If the run's device has no live connection, or the connection closes under the send, the
event is already in the replay and emit() returns normally (a debug line says so). The
device gets it with run.resume. Only a fault that is NOT "the peer left" propagates.

BINDING: WHO A RUN'S EVENTS GO TO
---------------------------------
emit(run_id, ...) takes no user and no device. The emitter keeps, per run, the (user,
device) it belongs to, set by bind() when a handler starts or resumes the run. The
connection itself is looked up in the registry at send time (a device that reconnected has
a new one). The ownership CHECK is the handlers' job (S14c: the run must be this user's AND
this device's before anything is bound); the emitter only refuses to bind one run to two
owners (BindingConflict), because a run belongs to one device for its whole life.

    bind(run_id, user_id, device_id, *, last_seq)   run.start passes 0; run.resume passes the
                                                    device's last_seq (see "THE TERMINAL EVENT")
    unbind(run_id)                                  drop everything held for the run

A bound run costs a few bytes and a lock. A run that is PAUSED must not stay bound: a device
can create paused runs faster than a server restarts. The disconnect handler therefore
emits the pause, then unbinds; run.resume binds again. A run that ends is unbound by
emit_final(). A run that is bound and whose buffer disappears (idle expiry) stays bound
until someone unbinds it: emit() raises buffer.BufferMissing and leaves that decision to
the caller, because racing a terminal event is worse than keeping a few bytes.

EXCLUSIVE: SEVERAL STEPS AS ONE
-------------------------------
Some sequences must not have another emit slip in between:

    async with emitter.exclusive(run_id) as run:
        resumed = await run_in_threadpool(run_manager.resume_run, ...)
        await run.redeliver(resumed.replay)         # the old events first ...
        await run.emit_state_changed("running", "resumed")    # ... then the new one

and the end of a run (below). exclusive() yields a handle with the same methods as the
emitter minus the run id; they run under the lock the block already holds, so they do not
deadlock on themselves. The handle is dead once the block ends (EmitterError). Do not call
the emitter's own emit() from inside the block for the same run: it waits for the lock the
block holds. Do not hold the lock across anything long: while it is held, the run's other
events wait.

RE-DELIVERY (run.resume)
------------------------
redeliver(events) sends events the buffer returned for a resume, exactly as stored: the same
id, seq and ts. It does not number and does not store (they are in the replay already), so
the replay never holds a seq twice. It validates the whole list first (every event is a
stream message of THIS run, seq strictly increasing) and sends nothing if any is not. If
the connection goes away part way it stops quietly and returns how many were sent. It moves
the run's "last seq" tracker (below) to the end of what it delivered.

THE TERMINAL EVENT (why emit_final exists)
------------------------------------------
When a run ends (cancelled, completed, failed, expired) run_manager deletes its buffer, and
next_seq needs the buffer: the last state_changed cannot be numbered from Redis after the
move. emit_final(run_id, state, reason=None) therefore numbers it from an in-process
tracker -- the highest seq this emitter has seen for the run (bind(last_seq=...) seeds it;
every emit and redeliver moves it) plus one -- and sends it LIVE ONLY: it is not stored,
because the buffer it would be stored in is gone. Then it unbinds the run. A device that
was offline misses it, and learns the run is over from run.resume (run_not_found: an ended
run is not resumable). Use it AFTER run_manager.end_run / cancel_run, inside exclusive(), so
no other emit can race the buffer's deletion:

    async with emitter.exclusive(run_id) as run:
        await run_in_threadpool(run_manager.cancel_run, user_id, run_id)
        await run.emit_final("cancelled")

A terminal state is refused by emit_state_changed (it would be stored in a buffer that is
about to be deleted, and announced twice); emit_final is the one way. The tracker is exact
as long as every seq the counter handed out was stored or sent; an emit that FAILED after
numbering (Redis went away between next_seq and the append) leaves a hole the caller must
not paper over: any buffer error from emit() means the stream is unreliable and the run
goes to paused_error (S07's rule), never on as if nothing happened.

WHAT GOES THROUGH HERE, AND WHAT DOES NOT
-----------------------------------------
Only the three stream types carry a seq and go in the replay: run_event, tool_proposed,
budget_warning (buffer.REPLAYABLE_TYPES). error, pong and welcome carry no seq and are sent
by the connection itself (send_error / send_message); emit() refuses them. The payload is
checked BEFORE a seq is taken (a JSON object, finite numbers, under the buffer's size
limit), because a payload the buffer would refuse after next_seq would leave a hole. It is
not checked against protocol/v1 here (the buffer is not a validator and the schema walk
is not free); the callers build payloads from the schema's own vocabulary, and the tests
check every frame the device receives against protocol/v1.

ERRORS
------
    RunNotBound        the run is not attached (never bound, unbound, or ended). Nothing was
                       numbered, stored or sent.
    BindingConflict    bind() for a run that is bound to another user or device.
    InvalidEvent       a message type, payload, state or reason that must not be sent.
                       Nothing was numbered. (A ValueError as well.)
    buffer.*           whatever the buffer raised (BufferMissing, BufferUnavailable,
                       BufferFull, ...). Fixed text, no content.
Messages never name a run, a user, a device or a value.

LOGGING
-------
Run id, device id, user id, seq, state name and the type name of an exception. Never an
event, a payload, an exception's text or a reason (tests/static/test_emitter_logging.py
reads this file and fails if a logging call names one). Replay content stays in the
encrypted buffer: it is never written to Postgres, a log or a metric (protocol README, 6).

NOT HERE
--------
No handler, no run state change, no model call, no ownership check and no decision about
what a failed emit means for a run: those are S14c-e's and S07's. Importing this module
installs nothing and opens nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from starlette.concurrency import run_in_threadpool

from codeagent import buffer, connections, runs
from codeagent.connections import ConnectionClosed

logger = logging.getLogger(__name__)

# The same pattern as protocol/v1/common.json $defs.run_id and buffer._RUN_ID_RE (a unit
# test keeps them equal). Always used with fullmatch(): "$" would accept a trailing newline.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# protocol/v1/run_event.json: state_changed.reason
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# The envelope around a payload (v, t, id, run_id, seq, ts) is well under a kilobyte;
# the margin keeps "the payload fits" and "the whole message fits" the same question.
_ENVELOPE_MARGIN_BYTES = 1024


# ---------------------------------------------------------------------
# Errors (fixed text: never a run id, a user, a device or a value)
# ---------------------------------------------------------------------

class EmitterError(Exception):
    """Base of every error this module raises itself (buffer.RunBufferError is the other
    family emit() can raise)."""


class RunNotBound(EmitterError):
    """The run is not attached to a device here: never bound, unbound, or ended."""

    def __init__(self):
        super().__init__("the run is not attached to a device")


class BindingConflict(EmitterError):
    """bind() for a run that is already bound to another user or device."""

    def __init__(self):
        super().__init__("the run is already bound to another device")


class InvalidEvent(EmitterError, ValueError):
    """A message type, payload, state or reason the emitter will not send."""


# ---------------------------------------------------------------------
# Validation (before anything is numbered)
# ---------------------------------------------------------------------

def _check_type(t) -> None:
    if not isinstance(t, str) or t not in buffer.REPLAYABLE_TYPES:
        raise InvalidEvent("only run_event, tool_proposed and budget_warning carry a seq")


def _check_payload(payload) -> None:
    """A JSON object the buffer will accept. Mirrors what append_replay checks, because
    a refusal AFTER next_seq would leave a hole in the numbering."""
    if not isinstance(payload, dict):
        raise InvalidEvent("an event payload must be a JSON object")
    try:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise InvalidEvent("an event payload is not valid JSON") from None
    if size + _ENVELOPE_MARGIN_BYTES > buffer.MAX_ITEM_BYTES:
        raise InvalidEvent("an event payload is too large")


def _state_changed(state_name, reason, *, terminal: bool) -> dict:
    """The payload of a run_event state_changed, or InvalidEvent."""
    if not isinstance(state_name, str) or state_name not in runs.RUN_STATES:
        raise InvalidEvent("not a run state")
    if terminal and state_name not in runs.TERMINAL_STATES:
        raise InvalidEvent("emit_final announces a terminal state only")
    if not terminal and state_name in runs.TERMINAL_STATES:
        raise InvalidEvent("a terminal state is announced with emit_final")
    payload = {"type": "state_changed", "state": state_name}
    if reason is not None:
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            raise InvalidEvent("not a valid state_changed reason")
        payload["reason"] = reason
    return payload


def _check_events(run_id: str, events) -> list:
    """The list redeliver() may send: stream messages of THIS run, seq strictly increasing."""
    if not isinstance(events, (list, tuple)):
        raise InvalidEvent("the events to re-deliver must be a list")
    last = 0
    for event in events:
        if not isinstance(event, dict):
            raise InvalidEvent("a re-delivered event must be a JSON object")
        seq = event.get("seq")
        kind = event.get("t")
        if (not isinstance(kind, str) or kind not in buffer.REPLAYABLE_TYPES or event.get("run_id") != run_id
                or not isinstance(seq, int) or isinstance(seq, bool) or seq <= last):
            raise InvalidEvent("not a stream message of this run, in seq order")
        last = seq
    return list(events)


# ---------------------------------------------------------------------
# One run's emitter state
# ---------------------------------------------------------------------

class _Binding:
    """What the emitter holds for one bound run: who it belongs to, the lock that orders
    its events, and the highest seq seen. One object per bind: unbind + bind makes a NEW
    one, which is how an emit that was waiting on the old lock knows it must give up."""

    __slots__ = ("run_id", "user_id", "device_id", "lock", "last")

    def __init__(self, run_id: str, user_id: str, device_id: str, last: int):
        self.run_id = run_id
        self.user_id = user_id
        self.device_id = device_id
        self.lock = asyncio.Lock()
        self.last = last


def _store(binding: _Binding, t: str, payload: dict) -> dict:
    """Steps 1 and 2 of an emit, in one worker-thread call (so cancelling the awaiting task
    cannot split them). The tracker follows the counter even if the append then fails: the
    counter has moved, and the tracker must say where it is."""
    run_id = binding.run_id
    seq = buffer.next_seq(run_id)
    binding.last = max(binding.last, seq)
    envelope = connections.make_envelope(t, payload, run_id=run_id, seq=seq)
    buffer.append_replay(run_id, envelope)
    return envelope


class ExclusiveRun:
    """What `async with emitter.exclusive(run_id)` yields: the run's lock is held, and these
    methods run under it. Dead once the block ends."""

    def __init__(self, emitter: "RunEmitter", binding: _Binding):
        self._emitter = emitter
        self._binding = binding
        self._open = True

    def _live(self) -> _Binding:
        if not self._open:
            raise EmitterError("the exclusive block has ended")
        return self._binding

    @property
    def run_id(self) -> str:
        return self._binding.run_id

    @property
    def last_seq(self) -> int:
        return self._binding.last

    async def emit(self, t: str, payload: dict) -> dict:
        """Number, store and (if the device is there) send one stream message. Returns the
        envelope. See the module docstring."""
        binding = self._live()
        _check_type(t)
        _check_payload(payload)
        envelope = await run_in_threadpool(_store, binding, t, payload)
        await self._emitter._send(binding, envelope)
        return envelope

    async def emit_state_changed(self, state_name: str, reason: "str | None" = None) -> dict:
        """A run_event state_changed for a non-terminal state."""
        self._live()
        return await self.emit("run_event", _state_changed(state_name, reason, terminal=False))

    async def emit_final(self, state_name: str, reason: "str | None" = None) -> dict:
        """The terminal state_changed: numbered from the in-process tracker, sent LIVE ONLY
        (not stored: the run's buffer is gone), and the run is unbound. Returns the envelope.
        Call it after run_manager.end_run / cancel_run, inside the exclusive block."""
        binding = self._live()
        payload = _state_changed(state_name, reason, terminal=True)
        binding.last += 1
        envelope = connections.make_envelope("run_event", payload, run_id=binding.run_id, seq=binding.last)
        self._emitter._release(binding)               # unbound whatever the send does
        await self._emitter._send(binding, envelope, in_replay=False)
        logger.info("[emitter] run ended run=%s device=%s user=%s state=%s seq=%s",
                    binding.run_id, binding.device_id, binding.user_id, state_name, binding.last)
        return envelope

    async def redeliver(self, events) -> int:
        """Send stored events to the run's device exactly as they are (same id, seq, ts),
        without numbering or storing them. Returns how many were sent; stops quietly if the
        connection goes away. Raises InvalidEvent, before sending anything, for a list that
        is not stream messages of this run in strictly increasing seq order."""
        binding = self._live()
        events = _check_events(binding.run_id, events)
        if events:
            binding.last = max(binding.last, events[-1]["seq"])
        conn = self._emitter._connection(binding)
        if conn is None:
            return 0
        sent = 0
        for event in events:
            try:
                await conn.send_json(event)
            except ConnectionClosed:
                break
            sent += 1
        return sent


# ---------------------------------------------------------------------
# The emitter
# ---------------------------------------------------------------------

class RunEmitter:
    """Numbers, stores and sends a run's stream. See the module docstring.

    `registry` is a callable returning the ConnectionRegistry to look devices up in;
    the default reads connections.get_registry() at every send, so a registry swapped in
    later (S11) or by a test is the one used."""

    def __init__(self, registry=None):
        self._registry = registry
        self._bound: dict = {}

    # -- binding ----------------------------------------------------------
    def bind(self, run_id: str, user_id: str, device_id: str, *, last_seq: int) -> None:
        """Attach a run to the (user, device) it belongs to. `last_seq` is the highest seq
        known to exist for the run: 0 for a new run, the device's last_seq on a resume (and
        the replay's last seq is added by redeliver). Binding a bound run again with the
        same owner only raises its last_seq; another owner is BindingConflict."""
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("run_id is malformed")
        for value in (user_id, device_id):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError("user_id and device_id must be non-empty strings")
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
            raise ValueError("last_seq must be a non-negative integer")
        current = self._bound.get(run_id)
        if current is not None:
            if (current.user_id, current.device_id) != (user_id, device_id):
                raise BindingConflict()
            current.last = max(current.last, last_seq)
            return
        self._bound[run_id] = _Binding(run_id, user_id, device_id, last_seq)

    def unbind(self, run_id: str) -> bool:
        """Forget the run (idempotent). True if it was bound. An emit waiting on the run's
        lock will find the run gone and raise RunNotBound."""
        return self._bound.pop(run_id, None) is not None

    def is_bound(self, run_id: str) -> bool:
        return run_id in self._bound

    def bound_runs(self) -> tuple:
        """The bound run ids, sorted (diagnostics and tests)."""
        return tuple(sorted(self._bound))

    def last_seq(self, run_id: str) -> "int | None":
        """The highest seq the emitter knows for the run, or None if it is not bound."""
        binding = self._bound.get(run_id)
        return None if binding is None else binding.last

    # -- the lock ---------------------------------------------------------
    @contextlib.asynccontextmanager
    async def exclusive(self, run_id: str):
        """Hold the run's lock for a block; yields an ExclusiveRun. RunNotBound if the run
        is not bound (also if it was unbound while this call waited for the lock)."""
        binding = self._bound.get(run_id)
        if binding is None:
            raise RunNotBound()
        async with binding.lock:
            if self._bound.get(run_id) is not binding:
                raise RunNotBound()
            handle = ExclusiveRun(self, binding)
            try:
                yield handle
            finally:
                handle._open = False

    # -- emitting ---------------------------------------------------------
    async def emit(self, run_id: str, t: str, payload: dict) -> dict:
        """Number, store and (if the device is connected) send one stream message of the
        run: run_event, tool_proposed or budget_warning. Returns the envelope."""
        async with self.exclusive(run_id) as run:
            return await run.emit(t, payload)

    async def emit_state_changed(self, run_id: str, state_name: str, reason: "str | None" = None) -> dict:
        """A run_event state_changed (non-terminal states; emit_final for the end)."""
        async with self.exclusive(run_id) as run:
            return await run.emit_state_changed(state_name, reason)

    async def emit_final(self, run_id: str, state_name: str, reason: "str | None" = None) -> dict:
        """The terminal state_changed, live only; unbinds the run. For the ordering that
        keeps another emit from racing the buffer's deletion, use exclusive() around
        end_run/cancel_run and the handle's emit_final instead (module docstring)."""
        async with self.exclusive(run_id) as run:
            return await run.emit_final(state_name, reason)

    async def redeliver(self, run_id: str, events) -> int:
        """Re-send stored events verbatim (run.resume). Returns how many were sent."""
        async with self.exclusive(run_id) as run:
            return await run.redeliver(events)

    # -- delivery ---------------------------------------------------------
    def _connection(self, binding: _Binding):
        registry = self._registry() if self._registry is not None else connections.get_registry()
        return registry.get(binding.user_id, binding.device_id)

    def _release(self, binding: _Binding) -> None:
        """Unbind `binding`, but only if it is still the run's current one."""
        if self._bound.get(binding.run_id) is binding:
            del self._bound[binding.run_id]

    async def _send(self, binding: _Binding, envelope: dict, *, in_replay: bool = True) -> bool:
        """Step 3: send to the run's device if it is connected. False when it is not (or
        left during the send): the event is in the replay (or, for a live-only one, lost
        by design) and that is not an error."""
        run_id, seq = binding.run_id, envelope["seq"]
        conn = self._connection(binding)
        if conn is None:
            logger.debug("[emitter] device offline, %s run=%s device=%s user=%s seq=%s",
                         "kept in the replay" if in_replay else "not stored",
                         run_id, binding.device_id, binding.user_id, seq)
            return False
        try:
            await conn.send_json(envelope)
        except ConnectionClosed:
            logger.debug("[emitter] device left during the send, %s run=%s device=%s user=%s seq=%s",
                         "kept in the replay" if in_replay else "not stored",
                         run_id, binding.device_id, binding.user_id, seq)
            return False
        return True


# ---------------------------------------------------------------------
# The process-wide emitter
# ---------------------------------------------------------------------

_EMITTER = RunEmitter()


def get_emitter() -> RunEmitter:
    """The single emitter the loop, the broker and the handlers share."""
    return _EMITTER
