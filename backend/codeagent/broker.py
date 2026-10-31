"""
codeagent/broker.py -- S09: the server tool broker.

The agent loop (S07) decides the model wants a tool run. The tool runs on the user's DEVICE,
under the device's own policy. The broker is the one place on the server that carries that
exchange: it proposes the call to the connected device, waits for the answer, and turns
whatever happens (a result, a denial, silence, a lost connection, a cancel) into exactly one
outcome for the loop and exactly one `tool_finished` event for the device's stream.

    loop (S07)  --propose(...)-->  broker  --tool_proposed-->  emit  -->  device
                <--ToolOutcome---          <--tool_result / approval / tool_stream--  (router, S14)

THE CALL LIFECYCLE (protocol/README.md sections 2, 3 and 5)
-----------------------------------------------------------
    1. propose(): the broker registers the call and emits `tool_proposed` {call_id, tool,
       input, risk}: a STREAM message, so it carries a seq, which the emitter assigns.
    2. The device's policy decides. It answers with `approval` {call_id, decision, scope} when
       a decision was made, and with `tool_result` {call_id, ok, output, truncated, trust, ...}
       if it ran the tool. A DENIED call gets an approval with decision `deny` and NO
       tool_result: the broker finishes it at once instead of waiting for a timeout.
    3. The call ends exactly once, with one `run_event tool_finished` {call_id, tool, status,
       duration_ms}. status is one of:

           ok            the device ran the tool, ok=true
           error         the device ran the tool, ok=false
           denied        the device's policy refused (approval deny)
           timeout       no answer in time; also `error` {code tool_timeout, details {call_id}}
           disconnected  the connection was lost while the call was open
           cancelled     the run was cancelled (cancel_all)

       `tool_started` is optional in the protocol and is not sent. The event is emitted BEFORE
       the loop's waiting propose() returns, so whatever the loop emits next (the next model
       turn's text) is numbered after it, the order protocol/fixtures/02 shows.

THE EMIT INTERFACE (the broker never numbers anything)
-------------------------------------------------------
The broker is built with `emit`, an async callable `emit(run_id, t, payload) -> envelope`:
exactly the signature of emitter.RunEmitter.emit (S14b), which numbers the message (seq),
stores it encrypted in the run's replay buffer and sends it if the device is connected. The
broker uses it for `tool_proposed` (t="tool_proposed") and `tool_finished` (t="run_event"),
and never sets, reads or invents a seq. The loop (S07) supplies the real one; the process-wide
broker (get_broker()) looks get_emitter().emit up at every call; the tests use a recording
fake. Anything `emit` raises is NOT swallowed for the proposal (propose() raises it: the loop
applies S07's rule, any buffer error means the stream is unreliable) and IS recorded, not
raised, for `tool_finished` (the outcome is still returned, with finish_event_failed=True,
because the call really did end and the loop must hear it).

WHO MAY ANSWER A CALL
---------------------
A call belongs to ONE (run, user, device), fixed when it is proposed. tool_result, approval
and tool_stream are matched on (run_id of the message, call_id) AND the connection's user and
device; anything else, a call that does not exist, one that belongs to another run, another
device or another user, a malformed payload, is answered with the SAME fixed `invalid_message`
(MSG_NOT_ACCEPTED, ref_id = the message): a device cannot tell "no such call" from "not yours",
and a result for another run's call_id changes nothing. This is the cross-user injection path
(a device that learns or guesses a call_id of someone else's run) and it is closed here, not
left to the loop. Calls are keyed by (run_id, call_id): two runs may use the same call_id.

A call also remembers the CONNECTION it was proposed on. A result or approval that arrives on
a different connection (the device reconnected, and S14 did not call the disconnect hook for
the superseded one) closes the call as `disconnected` and is ignored: a reconnect never
silently revives an old call. The broker also registers a router disconnect listener
(on_connection_closed) that closes the calls of a connection that ended, the safety net for
that superseded case. A retry after a reconnect is a NEW call_id, proposed on the new
connection.

IDEMPOTENCY
-----------
* The first outcome wins. A duplicate tool_result for a finished call is ignored and counted
  (call_stats()["duplicate_results"]); a result or approval that arrives after the call ended
  any other way (timeout, cancel, disconnect, deny) is ignored and counted as late. Neither is
  an error: the device cannot know the call ended, and duplicates are normal (README section 5).
* propose() with a call_id that already exists does NOT send a second tool_proposed: if the
  call finished it returns the stored outcome, if it is in flight it waits on that call. The
  same call_id with another tool, user or device is a CallConflict.
* The broker owns the deadline (a timer per call), not the caller: a proposer that is
  cancelled (S14 stops the turn on a disconnect) does not leave the call without an end.

DISCONNECT, CANCEL, AND WHO PAUSES THE RUN
------------------------------------------
S14's disconnect handler calls the run-disconnect hooks BEFORE it pauses the run
(run_handlers, "ON_DISCONNECT"). The broker's hook (on_run_disconnect) closes every open call
of the run with `tool_finished disconnected` through the emitter, so that event is numbered
before the pause, and releases whoever waits (the order of fixture 03_disconnect_resume). The
broker does NOT call run_manager.pause_run itself: S14 does that right after the hooks,
through S06, moving the run to paused_disconnected and announcing it; a second pause from here
would be paused_disconnected -> paused_disconnected, which is not an edge of runs.TRANSITIONS,
and S14 would skip announcing the pause. The broker imports nothing from S06.
S14's cancel handler calls the on_cancel hook outside the run's emitter lock; the broker's
hook is cancel_all(run_id), which finishes every pending call of the run as `cancelled`.

OVERSIZE OUTPUT
---------------
The client already truncates; the broker enforces a server-side ceiling (max_output_bytes,
UTF-8 bytes, DEFAULT_MAX_OUTPUT_BYTES) as a backstop. Above it the output is cut on a
character boundary, a marker is appended that says how many bytes were omitted
(omitted_marker), `truncated` is set and `omitted_bytes` carries the count. The marker comes
after the limit, so a truncated output is a little longer than the limit. A flag the device
set stays set.

THE TRUST LABEL (the hook C17 and the loop use)
-----------------------------------------------
Every ToolOutcome carries trust "untrusted" (TRUST_UNTRUSTED), whatever the device says: a
tool result is data that came from outside the server, not an instruction. That label is the
HOOK that C17 (the injection scan and the untrusted-content fence) and the agent loop will
read; the broker only labels. It does not scan, rewrite, redact or fence content. The device's
own label (protocol `trust`: local_project, local_other, external) is kept, as data, in
`origin`; it never raises the broker's label. ("untrusted" is not a value of the protocol's
`trust` enum, and it does not need to be: the label lives on the broker's outcome object, which
is not a protocol message.)

APPROVALS: THE BROKER DECIDES NOTHING
--------------------------------------
The device's policy is final. An `approval` message reports what the device decided; the broker
only OBSERVES it so it can finish a denied call. It never makes, changes or infers a decision,
holds no policy and no rule, and can not widen anything: a `deny` finishes the call as denied
and nothing after it (a late allow, a tool_result) can change that; an `allow` is recorded
and reported and does nothing else; a decision or scope the protocol does not define is
rejected, never read as an allow. The optional callback on_approval(call_id, decision, scope)
(per call in propose(), or a default in the constructor) lets the loop leave the advisory
`awaiting_approval` state (README section 3). It may be sync or async, is given a limited time,
and its failure changes nothing. For a deny it is called BEFORE the call finishes.

LIMITS AND STATE
----------------
max_in_flight caps the open calls of ONE run (TooManyCalls beyond it: a fixed text, nothing
is proposed). Finished calls are kept for idempotency, at most max_retained_per_run per run and
at most retain_s seconds, dropped lazily; forget_run() drops a run's finished calls.

ALL STATE IS IN MEMORY, behind a small interface (propose, handle_*, cancel_all, in_flight,
call_stats, stats, forget_run). One process, one event loop; S11 (the process split) moves the
call table to shared storage behind the same methods. Tool arguments and outputs are content
(protocol README 6): they live here only while a call is open or recently finished, never in
Postgres, a log or a metric.

LOGGING
-------
Run id, call id, device id, user id, tool name, sizes (byte counts), status names, error codes
and the TYPE name of an exception. Never a tool's arguments or output, a stream chunk, a
payload, or an exception's text (tests/static/test_broker_logging.py reads this file and fails
if a logging call names one).

NOT HERE
--------
The loop (S07), the model call, tool-input validation, the injection scan (C17), the policy
decision (the device), run state changes (S14 and S06), sequence numbers (the emitter).
Importing this module installs nothing: api/server.py makes the one explicit call, install().
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import inspect
import logging
import re
import time

from codeagent import emitter as emitter_module, router as router_module, run_handlers
from codeagent import connections
from codeagent.connections import ConnectionClosed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Constants (the constructor injects others)
# ---------------------------------------------------------------------

TRUST_UNTRUSTED = "untrusted"
DEFAULT_TIMEOUT_S = 120.0               # how long the device has to answer one call
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024    # server-side ceiling on one tool output (UTF-8 bytes)
DEFAULT_MAX_IN_FLIGHT = 8               # open calls per run
DEFAULT_MAX_RETAINED_PER_RUN = 256      # finished calls kept per run, for idempotency
DEFAULT_RETAIN_S = 900.0                # ... and for how long
DEFAULT_CALLBACK_TIMEOUT_S = 5.0        # one on_approval / on_stream / error send may take this long
SWEEP_INTERVAL_S = 30.0                 # finished calls are aged out at most this often

STATUS_OK, STATUS_ERROR, STATUS_DENIED = "ok", "error", "denied"
STATUS_TIMEOUT, STATUS_DISCONNECTED, STATUS_CANCELLED = "timeout", "disconnected", "cancelled"
# protocol/v1/run_event.json: tool_finished.status
FINISH_STATUSES = frozenset({STATUS_OK, STATUS_ERROR, STATUS_DENIED, STATUS_TIMEOUT,
                             STATUS_DISCONNECTED, STATUS_CANCELLED})
_RESULT_STATUSES = frozenset({STATUS_OK, STATUS_ERROR})

_RISKS = frozenset({"R0", "R1", "R2", "R3", "R4"})
_DECISIONS = frozenset({"allow", "deny"})                   # protocol/v1/approval.json
_SCOPES = frozenset({"once", "session"})
_STREAMS = frozenset({"stdout", "stderr"})                  # protocol/v1/tool_stream.json
_DEVICE_TRUST = frozenset({"local_project", "local_other", "external"})   # protocol/v1/common.json: trust

# The same patterns as protocol/v1/common.json (run_id, call_id, tool_name). Always fullmatch().
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_CALL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_MAX_OWNER_LEN = 256

_STAT_KEYS = ("proposed", "propose_failed", "offline_proposals", "duplicate_results", "late_results",
              "late_approvals", "rejected", "truncated_outputs", "stream_chunks", "stream_dropped",
              "finish_event_failed", "callback_failed")

# ---------------------------------------------------------------------
# Fixed texts (error.json: "message is fixed, sanitized text"). Nothing the device sent is
# ever put in one.
# ---------------------------------------------------------------------

MSG_TOOL_TIMEOUT = "A tool call did not finish in time."
MSG_NOT_ACCEPTED = "The server could not accept this message."
MSG_TOO_MANY_CALLS = "Too many tool calls are in flight for this run."
MSG_CALL_CONFLICT = "That call id is already in use for a different call."


# ---------------------------------------------------------------------
# Errors (fixed text: never a run id, a call id, a tool argument or a value)
# ---------------------------------------------------------------------

class ToolBrokerError(Exception):
    """Base of every error this module raises itself."""


class InvalidCall(ToolBrokerError, ValueError):
    """propose() was given arguments that must not be sent (nothing was proposed)."""


class TooManyCalls(ToolBrokerError):
    """The run already has max_in_flight open calls (nothing was proposed)."""

    def __init__(self):
        super().__init__(MSG_TOO_MANY_CALLS)


class CallConflict(ToolBrokerError):
    """A call_id that exists was proposed again for another tool, user or device."""

    def __init__(self):
        super().__init__(MSG_CALL_CONFLICT)


# ---------------------------------------------------------------------
# The outcome the loop receives
# ---------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ToolOutcome:
    """How one call ended. `output` is content: a result's text, None for every other status.

    trust            the broker's label, always "untrusted" by default (the hook for C17 and the loop)
    origin           the device's own trust label for a result, or None
    truncated        the output was cut (by the device or by the broker); omitted_bytes is what THE
                     BROKER cut (0 if it did not)
    finish_event_failed
                     the tool_finished event could not be emitted (the call still ended)
    """
    call_id: str
    tool: str
    status: str
    ok: bool
    output: "str | None"
    truncated: bool
    omitted_bytes: int
    trust: str
    origin: "str | None"
    exit_code: "int | None"
    duration_ms: int
    finish_event_failed: bool = False


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def omitted_marker(omitted_bytes: int) -> str:
    """The text appended to a truncated output."""
    return f"\n[output truncated by the server: {omitted_bytes} bytes omitted]"


def limit_output(text: str, limit: int) -> "tuple[str, int]":
    """(text, omitted bytes): `text` unchanged if it fits in `limit` UTF-8 bytes; otherwise its
    first `limit` bytes, cut on a character boundary, plus omitted_marker()."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, 0
    kept = raw[:limit].decode("utf-8", errors="ignore")      # drops a character the cut split
    omitted = len(raw) - len(kept.encode("utf-8"))
    return kept + omitted_marker(omitted), omitted


def _norm_device(value) -> str:
    """The comparison form of a device id (the one run_handlers' ownership guard uses)."""
    return str(value).strip().lower()


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_positive_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and value == value


def _one_of(value, allowed) -> bool:
    """value is a string in `allowed` (never a TypeError for an unhashable value a device sent)."""
    return isinstance(value, str) and value in allowed


def _consume(future) -> None:
    """Mark a future's outcome as looked at (a failed proposal nobody is waiting on any more)."""
    if not future.cancelled():
        future.exception()


def _kind_of(msg) -> str:
    return router_module._type_for_log(msg.get("t") if isinstance(msg, dict) else None)


async def _default_emit(run_id: str, t: str, payload: dict) -> dict:
    """The process-wide emitter, looked up at every call (a test or S11 may swap it)."""
    return await emitter_module.get_emitter().emit(run_id, t, payload)


def _default_connection_of(user_id: str, device_id: str):
    return connections.get_registry().get(user_id, device_id)


class _Call:
    """What the broker holds for one proposed call."""

    __slots__ = ("run_id", "call_id", "user_id", "device_id", "device_key", "tool", "conn", "started",
                 "finished", "finished_at", "finished_by", "outcome", "done", "timer", "duplicates",
                 "late", "late_approvals", "decision", "on_approval", "stream_bytes")

    def __init__(self, run_id, call_id, user_id, device_key, tool, conn, started, done, on_approval):
        self.run_id = run_id
        self.call_id = call_id
        self.user_id = user_id
        self.device_key = device_key                  # as given: how the registry knows the device
        self.device_id = _norm_device(device_key)     # for comparing
        self.tool = tool
        self.conn = conn                              # the connection the call was proposed on
        self.started = started
        self.finished = False
        self.finished_at = None
        self.finished_by = None
        self.outcome = None
        self.done = done                              # resolved AFTER tool_finished is emitted
        self.timer = None
        self.duplicates = 0
        self.late = 0
        self.late_approvals = 0
        self.decision = None                          # (decision, scope) of the first allow, as reported
        self.on_approval = on_approval
        self.stream_bytes = 0


# ---------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------

class ToolBroker:
    """Proposes tool calls to a device and carries their outcome. See the module docstring.

    emit             async (run_id, t, payload) -> envelope; default: the process-wide emitter's emit
    connection_of    (user_id, device_id) -> the live connection or None; default: the connection
                     registry (looked up at every call)
    timeout_s        how long a device has to answer a call (propose() may override)
    max_output_bytes server-side ceiling on one output, in UTF-8 bytes
    max_in_flight    open calls per run
    max_retained_per_run, retain_s
                     how many finished calls are kept per run, and for how long
    on_approval      default callback (call_id, decision, scope); propose() may give its own
    on_stream        optional async-or-sync callback (run_id, call_id, stream, chunk): the seam for a
                     live view (X04); without it a chunk is counted and dropped
    callback_timeout_s
                     how long one callback, or sending the timeout error, may take
    trust            the label every outcome carries (default "untrusted")
    clock            monotonic seconds (tests inject one)
    """

    def __init__(self, *, emit=None, connection_of=None, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES, max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
                 max_retained_per_run: int = DEFAULT_MAX_RETAINED_PER_RUN, retain_s: float = DEFAULT_RETAIN_S,
                 on_approval=None, on_stream=None, callback_timeout_s: float = DEFAULT_CALLBACK_TIMEOUT_S,
                 trust: str = TRUST_UNTRUSTED, clock=time.monotonic):
        if emit is not None and not router_module._is_async_callable(emit):
            raise TypeError("emit must be an async function (run_id, t, payload)")
        if connection_of is not None and not callable(connection_of):
            raise TypeError("connection_of must be callable")
        for name, value in (("max_output_bytes", max_output_bytes), ("max_in_flight", max_in_flight),
                            ("max_retained_per_run", max_retained_per_run)):
            if not _is_count(value):
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (("timeout_s", timeout_s), ("retain_s", retain_s),
                            ("callback_timeout_s", callback_timeout_s)):
            if not _is_positive_number(value):
                raise ValueError(f"{name} must be a positive number")
        for name, value in (("on_approval", on_approval), ("on_stream", on_stream)):
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable")
        if not isinstance(trust, str) or not re.fullmatch(r"[a-z_]{1,32}", trust):
            raise ValueError("trust must be a short lower-case label")
        self._emit = emit if emit is not None else _default_emit
        self._connection_of = connection_of if connection_of is not None else _default_connection_of
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._max_in_flight = max_in_flight
        self._max_retained = max_retained_per_run
        self._retain_s = retain_s
        self._on_approval = on_approval
        self._on_stream = on_stream
        self._callback_timeout_s = callback_timeout_s
        self._trust = trust
        self._clock = clock
        self._runs: dict = {}                      # run_id -> {call_id -> _Call}
        self._stats = collections.Counter()
        self._tasks: set = set()
        self._last_sweep = clock()
        # The router compares registered handlers by identity: a bound method is a new object on
        # every attribute access, so the table holds ONE object per handler (as RunHandlers does).
        self._table = {"tool_result": self.handle_tool_result,
                       "tool_stream": self.handle_tool_stream,
                       "approval": self.handle_approval}
        self._disconnect_listener = self.on_connection_closed

    # -- the loop's side ---------------------------------------------------
    async def propose(self, *, run_id: str, user_id: str, device_id: str, call_id: str, tool: str,
                      tool_input: dict, risk: str, timeout_s: "float | None" = None,
                      on_approval=None) -> ToolOutcome:
        """Propose one tool call to the run's device and wait for how it ends.

        Returns a ToolOutcome (a call that ends badly is an outcome, not an exception: status
        timeout, disconnected, cancelled, denied, error). A device that is not connected at all
        gets nothing proposed and the outcome is `disconnected`, without a tool_finished (the
        call never existed on the stream).

        Raises InvalidCall (bad arguments), TooManyCalls (the run's cap), CallConflict (the
        call_id exists for another tool, user or device), and whatever `emit` raises for the
        proposal itself (nothing is left registered then). A proposer that is cancelled leaves
        the call in place: it ends by result, timeout, disconnect or cancel_all.

        Proposing a call_id that already exists sends nothing new: a finished call returns its
        stored outcome, an open one is waited on."""
        self._check_arguments(run_id, user_id, device_id, call_id, tool, tool_input, risk, timeout_s, on_approval)
        existing = self._runs.get(run_id, {}).get(call_id)
        if existing is not None:
            if (existing.user_id, existing.device_id, existing.tool) != (user_id, _norm_device(device_id), tool):
                raise CallConflict()
            return await self._wait(existing)
        self._sweep()
        if self.in_flight(run_id) >= self._max_in_flight:
            logger.warning("[broker] too many calls in flight run=%s tool=%s", run_id, tool)
            raise TooManyCalls()
        conn = self._connection_of(user_id, device_id)
        if conn is None or getattr(conn, "closed", False):
            self._stats["offline_proposals"] += 1
            logger.info("[broker] device offline, call not proposed run=%s call=%s tool=%s",
                        run_id, call_id, tool)
            return self._bare_outcome(call_id, tool, STATUS_DISCONNECTED)
        call = _Call(run_id, call_id, user_id, device_id, tool, conn, self._clock(),
                     asyncio.get_running_loop().create_future(), on_approval)
        call.done.add_done_callback(_consume)
        self._runs.setdefault(run_id, {})[call_id] = call
        proposal = {"call_id": call_id, "tool": tool, "input": tool_input, "risk": risk}
        self._spawn(self._open(call, proposal, self._timeout_s if timeout_s is None else timeout_s))
        return await self._wait(call)

    def _check_arguments(self, run_id, user_id, device_id, call_id, tool, tool_input, risk, timeout_s, on_approval):
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise InvalidCall("run_id is malformed")
        if not isinstance(call_id, str) or not _CALL_ID_RE.fullmatch(call_id):
            raise InvalidCall("call_id is malformed")
        if not isinstance(tool, str) or not _TOOL_RE.fullmatch(tool):
            raise InvalidCall("tool is malformed")
        for value in (user_id, device_id):
            if not isinstance(value, str) or not value or len(value) > _MAX_OWNER_LEN:
                raise InvalidCall("user_id and device_id must be non-empty strings")
        if not isinstance(tool_input, dict):
            raise InvalidCall("tool_input must be an object")
        if not _one_of(risk, _RISKS):
            raise InvalidCall("risk is not R0 to R4")
        if timeout_s is not None and not _is_positive_number(timeout_s):
            raise InvalidCall("timeout_s must be a positive number")
        if on_approval is not None and not callable(on_approval):
            raise InvalidCall("on_approval must be callable")

    async def _wait(self, call: _Call) -> ToolOutcome:
        """Wait for the call's end. Shielded: a cancelled waiter does not resolve the call."""
        return await asyncio.shield(call.done)

    def _bare_outcome(self, call_id: str, tool: str, status: str, duration_ms: int = 0) -> ToolOutcome:
        return ToolOutcome(call_id=call_id, tool=tool, status=status, ok=False, output=None, truncated=False,
                           omitted_bytes=0, trust=self._trust, origin=None, exit_code=None,
                           duration_ms=duration_ms)

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("[broker] a background task failed error=%s", type(task.exception()).__name__)

    async def _open(self, call: _Call, proposal: dict, timeout_s: float) -> None:
        """Emit the proposal, then start the call's deadline."""
        try:
            await self._emit(call.run_id, "tool_proposed", proposal)
        except Exception as exc:                            # CancelledError is not one
            self._abort(call, exc)
            return
        self._stats["proposed"] += 1
        logger.info("[broker] call proposed run=%s call=%s tool=%s device=%s user=%s",
                    call.run_id, call.call_id, call.tool, call.device_key, call.user_id)
        if not call.finished:                               # a disconnect may have closed it meanwhile
            call.timer = self._spawn(self._expire(call, timeout_s))

    def _abort(self, call: _Call, exc: BaseException) -> None:
        """The proposal could not be emitted: the call never existed. Forget it and tell the proposer."""
        self._stats["propose_failed"] += 1
        logger.error("[broker] proposal not emitted run=%s call=%s tool=%s error=%s",
                     call.run_id, call.call_id, call.tool, type(exc).__name__)
        if call.finished:
            return                                          # closed meanwhile: that outcome stands
        call.finished = True
        calls = self._runs.get(call.run_id)
        if calls is not None and calls.get(call.call_id) is call:
            del calls[call.call_id]
            if not calls:
                del self._runs[call.run_id]
        if not call.done.done():
            call.done.set_exception(exc)

    async def _expire(self, call: _Call, timeout_s: float) -> None:
        await asyncio.sleep(timeout_s)
        await self._finish(call, STATUS_TIMEOUT)

    # -- ending a call ------------------------------------------------------
    def _close(self, call: _Call, status: str, *, ok: bool = False, output: "str | None" = None,
               truncated: bool = False, omitted_bytes: int = 0, origin: "str | None" = None,
               exit_code: "int | None" = None):
        """The synchronous half of ending a call: the first caller wins, everyone else gets None.
        No await between the check and the mark, so exactly one outcome exists. Returns the task
        that announces it (tool_finished, then wakes the waiters)."""
        if call.finished:
            return None
        now = self._clock()
        call.finished = True
        call.finished_at = now
        call.finished_by = status
        call.outcome = ToolOutcome(
            call_id=call.call_id, tool=call.tool, status=status, ok=ok, output=output, truncated=truncated,
            omitted_bytes=omitted_bytes, trust=self._trust, origin=origin, exit_code=exit_code,
            duration_ms=max(0, int((now - call.started) * 1000)))
        timer, call.timer = call.timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self._stats["finished_" + status] += 1
        self._trim(call.run_id)
        return self._spawn(self._announce(call))

    async def _finish(self, call: _Call, status: str, **fields) -> bool:
        """End `call`, announce it, wait until the announcement is done. True if THIS call won."""
        task = self._close(call, status, **fields)
        if task is None:
            return False
        await asyncio.shield(task)                          # a cancelled caller does not cut the announcement
        return True

    async def _announce(self, call: _Call) -> None:
        """tool_finished (exactly once per call), the timeout error if that is how it ended, and
        only then the waiters. Runs as its own task so cancelling a hook cannot cut it."""
        try:
            status, elapsed_ms = call.outcome.status, call.outcome.duration_ms
            event = {"type": "tool_finished", "call_id": call.call_id, "tool": call.tool,
                     "status": status, "duration_ms": elapsed_ms}
            try:
                await self._emit(call.run_id, "run_event", event)
            except Exception as exc:
                self._stats["finish_event_failed"] += 1
                logger.error("[broker] tool_finished not emitted run=%s call=%s tool=%s status=%s error=%s",
                             call.run_id, call.call_id, call.tool, status, type(exc).__name__)
                call.outcome = dataclasses.replace(call.outcome, finish_event_failed=True)
            if status == STATUS_TIMEOUT:
                await self._send_timeout_error(call)
            logger.info("[broker] call finished run=%s call=%s tool=%s status=%s ms=%s",
                        call.run_id, call.call_id, call.tool, status, elapsed_ms)
        finally:
            if not call.done.done():
                call.done.set_result(call.outcome)

    async def _send_timeout_error(self, call: _Call) -> None:
        conn = self._connection_of(call.user_id, call.device_key)
        if conn is None:
            return
        try:
            await asyncio.wait_for(
                conn.send_error("tool_timeout", MSG_TOOL_TIMEOUT, details={"call_id": call.call_id}),
                timeout=self._callback_timeout_s)
        except (ConnectionClosed, asyncio.TimeoutError):
            logger.debug("[broker] timeout error not delivered run=%s call=%s", call.run_id, call.call_id)
        except Exception as exc:
            logger.error("[broker] timeout error not sent run=%s call=%s error=%s",
                         call.run_id, call.call_id, type(exc).__name__)

    async def _close_all(self, calls: list, status: str) -> int:
        """End several calls at once: all are marked first (so a hook that is cancelled half way
        still leaves none of them open), then their events are announced in order."""
        tasks = [task for task in (self._close(call, status) for call in calls) if task is not None]
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        return len(tasks)

    # -- the hooks S14 calls ---------------------------------------------------
    async def cancel_all(self, run_id: str) -> int:
        """Finish every pending call of the run as `cancelled` (one tool_finished each). Returns
        how many were finished. The S14 cancel hook calls this."""
        return await self._close_all(self._pending(run_id=run_id), STATUS_CANCELLED)

    async def on_cancel(self, conn, run) -> None:
        """run_handlers.add_on_cancel hook: fn(conn, run), called outside the run's emitter lock."""
        await self.cancel_all(run.id)

    async def on_run_disconnect(self, conn, run) -> int:
        """run_handlers.add_on_run_disconnect hook: fn(conn, run), called BEFORE S14 pauses the
        run. Closes the run's open calls on the disconnecting device with `tool_finished
        disconnected` and releases whoever waits. Does not pause the run (module docstring)."""
        user_id, device_id = conn.user_id, _norm_device(conn.device_id)
        calls = [c for c in self._pending(run_id=run.id) if c.user_id == user_id and c.device_id == device_id]
        return await self._close_all(calls, STATUS_DISCONNECTED)

    async def on_connection_closed(self, conn) -> int:
        """Router disconnect listener: close the open calls that were proposed on this connection.
        The safety net for what the run-disconnect hook does not see (a superseded connection)."""
        return await self._close_all([c for c in self._pending() if c.conn is conn], STATUS_DISCONNECTED)

    # -- the device's side: handlers registered on the router --------------------
    async def handle_tool_result(self, conn, msg: dict) -> None:
        """tool_result: the first result for the call wins. See the module docstring."""
        fields = _result_fields(msg.get("payload"))
        call = None if fields is None else self._owned(conn, msg.get("run_id"), fields["call_id"])
        if call is None:
            await self._reject(conn, msg)
            return
        if call.finished:
            self._count_ignored(call)
            return
        if self._on_other_connection(call, conn):
            self._count_ignored(call, late=True)
            await self._finish(call, STATUS_DISCONNECTED)
            return
        text, omitted = limit_output(fields["output"], self._max_output_bytes)
        if omitted:
            self._stats["truncated_outputs"] += 1
            logger.info("[broker] output truncated run=%s call=%s tool=%s omitted=%s",
                        call.run_id, call.call_id, call.tool, omitted)
        won = await self._finish(
            call, STATUS_OK if fields["ok"] else STATUS_ERROR, ok=fields["ok"], output=text,
            truncated=fields["truncated"] or omitted > 0, omitted_bytes=omitted,
            origin=fields["origin"], exit_code=fields["exit_code"])
        if not won:
            self._count_ignored(call)

    async def handle_approval(self, conn, msg: dict) -> None:
        """approval: the device reports its decision. A deny finishes the call at once; an allow is
        recorded and reported and does nothing else. The broker decides nothing."""
        payload = msg.get("payload")
        valid = (isinstance(payload, dict) and isinstance(payload.get("call_id"), str)
                 and _CALL_ID_RE.fullmatch(payload["call_id"])
                 and _one_of(payload.get("decision"), _DECISIONS) and _one_of(payload.get("scope"), _SCOPES))
        call = self._owned(conn, msg.get("run_id"), payload["call_id"]) if valid else None
        if call is None:
            await self._reject(conn, msg)
            return
        decision, scope = payload["decision"], payload["scope"]
        if call.finished:
            call.late_approvals += 1
            self._stats["late_approvals"] += 1
            return
        if self._on_other_connection(call, conn):
            call.late_approvals += 1
            self._stats["late_approvals"] += 1
            await self._finish(call, STATUS_DISCONNECTED)
            return
        if decision == "deny":
            await self._notify(call, decision, scope)       # the loop leaves awaiting_approval first
            await self._finish(call, STATUS_DENIED)
            return
        if call.decision is not None:
            return                                          # the same decision again: reported once
        call.decision = (decision, scope)
        await self._notify(call, decision, scope)

    async def handle_tool_stream(self, conn, msg: dict) -> None:
        """tool_stream: live output of a running tool. Never reaches the model (it sees the final
        tool_result) and never finishes a call; handed to on_stream if there is one."""
        payload = msg.get("payload")
        valid = (isinstance(payload, dict) and isinstance(payload.get("call_id"), str)
                 and _CALL_ID_RE.fullmatch(payload["call_id"])
                 and _one_of(payload.get("stream"), _STREAMS) and isinstance(payload.get("chunk"), str))
        call = self._owned(conn, msg.get("run_id"), payload["call_id"]) if valid else None
        if call is None:
            await self._reject(conn, msg)
            return
        if call.finished or self._on_other_connection(call, conn):
            self._stats["stream_dropped"] += 1
            return
        call.stream_bytes += len(payload["chunk"].encode("utf-8", errors="replace"))
        self._stats["stream_chunks"] += 1
        callback = self._on_stream
        if callback is None:
            return
        await self._call_back(call, callback, call.run_id, call.call_id, payload["stream"], payload["chunk"])

    # -- matching ------------------------------------------------------------------
    def _owned(self, conn, run_id, call_id) -> "_Call | None":
        """The call (run_id, call_id) if it was proposed to THIS connection's user and device."""
        if not isinstance(run_id, str):
            return None
        call = self._runs.get(run_id, {}).get(call_id)
        if call is None:
            return None
        if call.user_id != getattr(conn, "user_id", None) or call.device_id != _norm_device(getattr(conn, "device_id", "")):
            return None
        return call

    @staticmethod
    def _on_other_connection(call: _Call, conn) -> bool:
        return call.conn is not None and call.conn is not conn

    def _count_ignored(self, call: _Call, *, late: bool = False) -> None:
        """A result for a call that already ended (or is being closed): ignored and counted."""
        if not late and call.finished_by in _RESULT_STATUSES:
            call.duplicates += 1
            self._stats["duplicate_results"] += 1
        else:
            call.late += 1
            self._stats["late_results"] += 1
        logger.info("[broker] result ignored run=%s call=%s tool=%s", call.run_id, call.call_id, call.tool)

    async def _reject(self, conn, msg: dict) -> None:
        """The one answer for a malformed message and for a call that is not this device's."""
        self._stats["rejected"] += 1
        kind = _kind_of(msg)
        logger.warning("[broker] message rejected type=%s device=%s user=%s", kind, conn.device_id, conn.user_id)
        await conn.send_error("invalid_message", MSG_NOT_ACCEPTED, ref_id=router_module._ref_id(msg))

    async def _notify(self, call: _Call, decision: str, scope: str) -> None:
        callback = call.on_approval if call.on_approval is not None else self._on_approval
        if callback is not None:
            await self._call_back(call, callback, call.call_id, decision, scope)

    async def _call_back(self, call: _Call, callback, *args) -> None:
        """Run a callback of the loop's: sync or async, time-limited, never allowed to change the call."""
        try:
            outcome = callback(*args)
            if inspect.isawaitable(outcome):
                await asyncio.wait_for(outcome, timeout=self._callback_timeout_s)
        except asyncio.TimeoutError:
            self._stats["callback_failed"] += 1
            logger.warning("[broker] callback timed out run=%s call=%s", call.run_id, call.call_id)
        except Exception as exc:
            self._stats["callback_failed"] += 1
            logger.warning("[broker] callback failed run=%s call=%s error=%s",
                           call.run_id, call.call_id, type(exc).__name__)

    # -- the table -------------------------------------------------------------------
    def _pending(self, *, run_id: "str | None" = None) -> list:
        runs_ = [self._runs.get(run_id, {})] if run_id is not None else list(self._runs.values())
        return [call for calls in runs_ for call in list(calls.values()) if not call.finished]

    def _trim(self, run_id: str) -> None:
        """Keep at most max_retained_per_run finished calls per run (the oldest go first)."""
        calls = self._runs.get(run_id)
        if not calls:
            return
        finished = sorted((c for c in calls.values() if c.finished), key=lambda c: c.finished_at)
        for call in finished[:max(0, len(finished) - self._max_retained)]:
            del calls[call.call_id]

    def _sweep(self) -> None:
        """Age out finished calls older than retain_s (at most every SWEEP_INTERVAL_S seconds)."""
        now = self._clock()
        if now - self._last_sweep < SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        for run_id in list(self._runs):
            calls = self._runs[run_id]
            for call in [c for c in calls.values() if c.finished and now - c.finished_at > self._retain_s]:
                del calls[call.call_id]
            if not calls:
                del self._runs[run_id]

    def in_flight(self, run_id: str) -> int:
        """Open calls of the run."""
        return len(self._pending(run_id=run_id))

    def call_stats(self, run_id: str, call_id: str) -> "dict | None":
        """Numbers about one call (no content), or None if the broker holds no such call."""
        call = self._runs.get(run_id, {}).get(call_id)
        if call is None:
            return None
        return {"tool": call.tool, "state": "finished" if call.finished else "pending",
                "status": call.finished_by, "duplicate_results": call.duplicates,
                "late_results": call.late, "late_approvals": call.late_approvals,
                "stream_bytes": call.stream_bytes, "approval": None if call.decision is None else call.decision[0]}

    def stats(self) -> dict:
        """Process-wide counters (no content): proposals, outcomes by status, ignored and rejected
        messages, truncations, failures."""
        return {**{key: 0 for key in _STAT_KEYS}, **self._stats}

    def forget_run(self, run_id: str) -> int:
        """Drop the run's FINISHED calls (the run is over); a pending call stays. Returns how many."""
        calls = self._runs.get(run_id)
        if not calls:
            return 0
        gone = [c for c in calls.values() if c.finished]
        for call in gone:
            del calls[call.call_id]
        if not calls:
            del self._runs[run_id]
        return len(gone)

    # -- registration -----------------------------------------------------------------
    def registrations(self) -> tuple:
        """(message type, handler) pairs for the router: stable objects, one per type."""
        return tuple(self._table.items())

    def register_on(self, router) -> None:
        for message_type, handler in self.registrations():
            router.register(message_type, handler)
        router.register_disconnect(self._disconnect_listener)


def _result_fields(payload) -> "dict | None":
    """The fields of a tool_result the broker uses, or None for a payload it will not act on.
    ws.py has already validated the message against protocol/v1; this is the broker's own check,
    because it is a public seam and what it accepts decides what the loop's model reads."""
    if not isinstance(payload, dict):
        return None
    call_id, ok, output, truncated = (payload.get(k) for k in ("call_id", "ok", "output", "truncated"))
    if (not isinstance(call_id, str) or not _CALL_ID_RE.fullmatch(call_id) or not isinstance(ok, bool)
            or not isinstance(output, str) or not isinstance(truncated, bool)):
        return None
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    origin = payload.get("trust")
    return {"call_id": call_id, "ok": ok, "output": output, "truncated": truncated, "exit_code": exit_code,
            "origin": origin if _one_of(origin, _DEVICE_TRUST) else None}


# ---------------------------------------------------------------------
# The process-wide broker and its install
# ---------------------------------------------------------------------

_BROKER: "ToolBroker | None" = None


def get_broker() -> ToolBroker:
    """The single broker the loop (S07) and the router share. Created on first use, with the
    process-wide emitter and the connection registry."""
    global _BROKER
    if _BROKER is None:
        _BROKER = ToolBroker()
    return _BROKER


def install(router=None, *, broker: "ToolBroker | None" = None, handlers=None) -> ToolBroker:
    """Register the broker with S14: the three message types on the router (tool_result,
    tool_stream, approval; run.start, user_message, cancel and run.resume belong to run_handlers),
    the router's disconnect listener, and, on the run handlers, the run-disconnect hook and the
    cancel hook. Idempotent. Defaults: the process-wide router, run handlers and broker. Importing
    this module installs nothing; api/server.py makes the one call, after run_handlers' and
    hello's. Returns the broker."""
    broker = broker if broker is not None else get_broker()
    handlers = handlers if handlers is not None else run_handlers.get_handlers()
    broker.register_on(router if router is not None else router_module.get_router())
    handlers.add_on_run_disconnect(broker.on_run_disconnect)
    handlers.add_on_cancel(broker.on_cancel)
    return broker
