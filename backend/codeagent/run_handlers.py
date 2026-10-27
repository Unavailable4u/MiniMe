"""
codeagent/run_handlers.py -- S14 (part c): the run handlers' base.

The router (S14a) hands every validated device message to the function registered for its
type; the emitter (S14b) numbers, stores and sends a run's events; run_manager (S06c) records
runs and keeps their buffers. This module is the glue that registers on the router and calls
the other two. It holds the BASE every handler stands on and these handlers:

    run.start          start the run, announce `created` and `running`            (S14c)
    on_disconnect      pause the device's open runs and put the pause in their replay (S14c)
    user_message       hand the message to the turn runner, one turn at a time     (S14d)
    cancel             stop the run, delete its buffer, announce the end           (S14d)
    run.resume         reattach a run, re-deliver what the device missed, go on    (S14e)

api/server.py makes the one call that installs all of it (install(), below).

    ws.py (S04)         authenticated, validated, rate-limited connection
    router.py (S14a)    dispatch by message type
    run_handlers.py     (here) the handlers, the ownership guard, the error map, the hooks
    emitter.py (S14b)   numbered, stored, delivered events
    run_manager.py      S06c: start, resume, pause, end, cancel (synchronous)
    runs.py, buffer.py  the run row and its state machine; the encrypted buffer

S06's functions do database and Redis calls and are synchronous; every call to them here goes
through starlette's run_in_threadpool, by way of _thread(): a handler that is cancelled while
one is in progress (ws.py cancels the receive loop when the connection ends, and only then
calls on_disconnect) waits for it to finish, because a thread cannot be stopped and would
otherwise move the run after on_disconnect had looked at it.

THE OWNERSHIP GUARD
-------------------
owned_run(conn, msg) is the first thing every run-scoped handler does. It reads the run by
(the connection's user, the message's run_id) and accepts it only if the run's device is the
connection's device. Anything else -- another device's run, another user's, no such run, a
run_id that is not even well formed, and by default a run that is already over -- is answered
with the SAME error: `run_not_found`, the same fixed text, no details. The device cannot tell
"does not exist" from "is not yours". The guard never reads the payload. A fault in the run
store is NOT run_not_found (that would tell a device its run is gone because the database
blinked); it is `internal`.

THE ERROR MAP
-------------
map_error(exc) applies run_manager's table ("ERRORS, AND WHAT THE ROUTER MIGHT SAY"):

    run_not_found    RunNotFound, RunNotResumable, RunGone
    invalid_message  InvalidRunState, RunIdCollision, InvalidRun, InvalidBufferInput,
                     BufferFull, IllegalTransition (a stale or duplicate request)
    rate_limited     ParallelRunLimit (details.retry_after_s: RETRY_AFTER_SECONDS)
    internal         everything else in runs / run_manager / buffer: RunStoreUnavailable,
                     TransitionConflict, BufferUnavailable, BufferNotConfigured,
                     BufferMissing / Expired / Exists / Corrupt, ProtocolSchemasUnavailable,
                     CleanupIncomplete, RunNotEnded

and returns (code, fixed text, details); an exception of any other family returns None, and
answer_error() then tells its caller "not mine" so the bug reaches the router, which contains
it and answers `internal` (ws.MSG_INTERNAL) and logs its type. A test fails when a new error
class is added to runs, run_manager or buffer without a row in the map. The text is fixed:
nothing the client sent, and no exception text, is ever put in it.

RUN.START
---------
    1. run_manager.start_run(user, device, run_id, payload, max_parallel)
          validates the payload, records the run (`created`), creates its buffer.
          The OWNER is the connection's user and device, never anything in the message.
          Errors go through the map: a bad payload and an id collision (including another
          user's run id: the protocol says invalid_message, and the answer is the same for
          both) are invalid_message; the cap is rate_limited; a store, buffer or secret
          problem is internal and nothing is recorded.
    2. bind the run on the emitter (last_seq 0) and announce `created`   -> seq 1
    3. runs.transition(created -> running), then announce `running`      -> seq 2
       (protocol fixtures 02 and 03 announce `running` after the first user_message; this
       patch announces it at run.start, as the work package says. The numbering is the same
       either way; a transcript comparison that is literal about the ORDER of the two events
       relative to the first user_message will differ.)

Once the run is recorded (after step 1), ANY failure -- the move to running, an emit, the
buffer -- fails the run (run_manager.end_run(..., "failed", error_code="internal"), which
deletes the buffer, so it cannot sit in `created` holding one of the device's slots), unbinds
it, and answers the device with the mapped error. The device may start a new run with a new
id. Cancellation is NOT a failure: when ws.py ends a connection it cancels the receive loop,
which can interrupt this handler between steps (a step that is in progress in its thread
finishes first, see above); the run is then left as it is (created or running), and
on_disconnect (below) moves it to paused_disconnected so it can be resumed.

THE PARALLEL-RUN CAP
--------------------
MAX_PARALLEL_RUNS = ws.MAX_PARALLEL_RUNS, so the number welcome advertises
(limits.max_parallel_runs) and the number enforced here are one number, and a test pins it.
RunHandlers(max_parallel=...) or monkeypatching run_handlers.MAX_PARALLEL_RUNS injects another
(read at call time). The cap is per device and counts runs in a slot state (created, running,
awaiting_approval); paused runs do not hold a slot.

ON_DISCONNECT
-------------
Called by the router (a registered disconnect listener) when a connection ends for any reason.
The connection is already out of the registry and closed.

    SUPERSEDED FIRST. When a device reconnects, ws.py registers the NEW connection before the
    old one's cleanup runs. If the registry then holds a different, live connection for this
    (user, device), the device is back: this handler does nothing, no hook, no pause, no event.
    (Pausing would pause the runs of the device that just reconnected, and tell it so.) The
    runs stay as they are; the new connection's run.resume reattaches them, and when THAT
    connection ends the pause happens. A newer connection that is already closed does not
    count.

    Otherwise, for each open run of the device (runs.list_open_runs) that holds a slot
    (created, running, awaiting_approval; a paused run has nothing to close and nothing to
    pause, and is left alone):

        a. bind the run on the emitter (a restarted server knows nothing about it) and stop
           its turns (stop_turns: the running turn is cancelled, the queue dropped; a paused
           run must not keep calling the model, and the loop restarts on resume);
        b. call the run-disconnect hooks, in order (the tool broker closes an open call with
           tool_finished `disconnected` here, through the emitter, so that event is numbered
           BEFORE the pause). A hook gets (conn, run). A hook that raises is logged by type
           and the next one still runs; a hook that does not return within hook_timeout
           (HOOK_TIMEOUT_SECONDS, 10 s) is cancelled and given up on: a stuck broker must
           not keep a run from being paused;
        c. run_manager.pause_run(..., "paused_disconnected"). A run still in `created` (an
           interrupted run.start) is moved to running first: the table has no edge from
           created to a paused state. If someone ended the run meanwhile (a sweep, a cancel)
           the pause is skipped quietly;
        d. announce state_changed paused_disconnected, reason device_disconnected, so it is in
           the replay when the device returns. The device is gone, so it is stored, not sent.
           If the buffer is gone (idle expiry) the event cannot be stored, which is logged
           and does not undo the pause: the device's resume rebuilds the buffer from history;
        e. unbind the run: a paused run holds no emitter state.

    A store outage while listing the runs is logged and the handler ends: nothing can be
    paused that cannot be found, and the runs stay as they are until the device resumes or the
    sweep (run_manager.expire_stale) judges them.

USER_MESSAGE AND THE TURN QUEUE
-------------------------------
    1. the ownership guard (a run that is not this connection's is run_not_found, and nothing
       about the turn runner is revealed for it);
    2. the run must be running or awaiting_approval (a turn is in progress or may start);
       created and the paused states are `invalid_message` (the device must run.resume first);
    3. no turn runner installed: `internal`, fixed text MSG_NO_TURN_RUNNER;
    4. otherwise the message is queued for the run and the handler RETURNS. ws.py awaits the
       handler inside the receive loop, so a turn runs as a task (one worker task per run
       that has work, ended when its queue is empty) and the device's `cancel` and `ping`
       are read while a turn is open.

Turns on one run happen ONE AT A TIME, in arrival order: the worker calls
turn_runner(conn, run, msg) and awaits it before it takes the next message. At most
MAX_QUEUED_TURNS messages wait behind the running one (the running one does not count; a cap
of 0 means one turn and no queue); a message beyond that is answered `rate_limited` with
details.retry_after_s and is NOT queued. The cap is per run: runs never wait for each other.

At the start of EACH turn the worker re-reads the run and passes the current record, because
a queued message can wait a while: if the run was paused meanwhile (a budget pause in the
middle of a queue must not call the model for the messages behind it) the message is
answered `invalid_message`, and if it ended, `run_not_found`; if the runner was uninstalled
meanwhile, `internal`. Every message that is accepted is therefore either given to the runner
or answered; none vanishes. A turn that raises is answered `internal` with that message's id
(its exception's TYPE is logged, never its text) and the next message still runs. The turn
runner owns the run's state: this module does not move a run because a turn failed (S07's
rule: a model or buffer failure is paused_error, decided by the loop).

stop_turns(run_id) drops the waiting messages and cancels the worker task, giving the running
turn stop_timeout (TURN_STOP_TIMEOUT_SECONDS) to unwind; a runner that ignores cancellation is
logged and left behind rather than waited for. It is called by cancel and by the disconnect
handler, so that a paused or cancelled run never keeps calling the model: waiting messages
that were accepted but never started are dropped (the device's own log has them; it re-sends
what it needs on resume), and a turn that was in progress is cancelled at its next await and
restarted by whoever resumes the run (the loop's on_resume hook, S14e). The queue holds the
messages in this process's memory only, until their turn; nothing is written anywhere.

CANCEL
------
    1. the ownership guard (a run that is over reads as missing: a second cancel is
       run_not_found);
    2. make sure the emitter knows the run's last seq (see below);
    3. the on_cancel hooks, in order, OUTSIDE the run's lock (the broker's cancel_all emits
       tool_finished `cancelled` through the emitter, which takes that lock; a hook that
       raises is logged by type, one that hangs is cancelled after hook_timeout, the rest run);
    4. stop_turns(run);
    5. inside emitter.exclusive(run): run_manager.cancel_run (sets run_guard's cancel flag,
       moves the run to cancelled, deletes the buffer and the rails), then announce the end.

THE END IS ANNOUNCED LIVE, NOT STORED. cancel_run deletes the buffer and next_seq needs it, so
the last state_changed (state cancelled, reason CANCEL_REASON) is numbered by the emitter's
tracker (emit_final, S14b) and sent without being stored. A run that is bound already has an
exact tracker (every emit moves it). A run that is NOT bound (paused by a disconnect, or this
process restarted) gets one first: its last seq is read from the replay before anything is
deleted. If its buffer is already gone nothing can be numbered, and the cancel is carried out
without an announcement (the device asked for it; it will not get a seq it cannot trust).
A device that is offline misses the event and learns from run.resume (run_not_found).

The device gets no separate "ok": the state_changed event IS the answer. Errors:
the guard's run_not_found; a run that ended between the guard and the cancel (IllegalTransition)
is run_not_found and leaves no flag behind (run_manager clears it); a store or buffer fault
before the move is `internal`, the run is still open and run_manager keeps the cancel flag, so
a second `cancel` finishes the job. If the move succeeded but the buffer could not be deleted
(CleanupIncomplete) the run IS over: the device is told it is cancelled, and purge_run is
retried in the background after each of PURGE_RETRY_DELAYS_SECONDS (the buffer has a TTL, so
giving up after the last attempt costs only time).

If the run is ended or unbound by someone else between the hooks and the lock (the loop
finishing at the same moment), the cancel is answered run_not_found: that is what the guard
would answer an instant later.

RUN.RESUME
----------
    1. the ownership guard (a run that is not this connection's, a missing one and one that is
       over are the same run_not_found; the payload, and so the device's `state`, is not read
       before the guard has said yes);
    2. bind the run on the emitter and take its lock (emitter.exclusive) for steps 3 to 6: a
       turn that is still emitting, or a second resume, waits, so the device gets the events it
       missed, then `running`, then anything new, never a new event between them;
    3. run_manager.resume_run(user, device, run, last_seq, state, max_parallel) in the
       threadpool. It decides everything about the buffer and the state machine (module
       docstring of run_manager): the replay after last_seq from the live buffer, or a rebuild
       from the device's `state` when the buffer is gone, run_not_found when it is gone and no
       state came, and paused_disconnected / paused_error -> running (the cap is checked, so a
       device already at its cap gets rate_limited and the run stays paused);
    4. the emitter's last seq is made exact: the replay's last seq, else, for a rebuilt buffer,
       the device's last_seq (the counter continues from it), else what is stored. It is NOT
       taken from the device's claim: a last_seq beyond the stream must not make the end of the
       run (emit_final) skip numbers;
    5. redeliver the replay verbatim (same id, same seq; not stored again);
    6. announce state_changed `running` with reason RESUME_REASON ("resumed"), numbered and
       stored like any event, if THIS call moved the run out of a pause. A run that was never
       paused (the device came back before the server noticed it was gone) is reattached as it
       is and no state is announced; a run paused for budget stays paused (the budget decides,
       not the device), is told what it missed, and holds no emitter state afterwards;
    7. the lock is released, and then the on_resume hooks are called, in order, with the
       resumed run record. They are called only for a run this call moved out of a pause: for a
       run that was never paused the loop never stopped, and starting it again would run the
       turn twice. A hook may emit through the emitter, which is why they run outside the lock.

A run that is still `created` (a run.start that a superseding connection interrupted: the
disconnect handler skipped it) is completed here: moved to running and announced `running`
(no reason), exactly what run.start was about to do; no hook is called, no loop was started.

When something fails after step 3 the run is put back where it was, so that the same
run.resume can simply be sent again: a run this call took out of a pause goes back to that
pause (paused_error keeps its error code), a created run is failed (as run.start does), the
emitter binding this call made is dropped, and the device gets the mapped error. Nothing is
lost by the retry: the replay is read again, and `running` is announced once, by the call that
succeeds. If the handler is cancelled instead (ws.py cancels the receive loop when the
connection ends) nothing is undone: the step in progress finishes (see above), so the run is
`running` with no device when the cancellation completes, and on_disconnect, which ws.py calls
right after, finds it open and pauses it again.

THE INTEGRATION POINTS
----------------------
Others install behaviour here instead of this module importing them (the loop, S07, and the
tool broker, S09, do not exist yet). All are async callables, refused with TypeError
otherwise (a plain function would be called and its result thrown away). Registering the
same callable twice keeps one copy.

    set_turn_runner(fn)            fn(conn, run, msg): runs one model turn for a user_message
                                   (msg is the validated envelope; the text is in
                                   msg["payload"]). ONE runner. A different one while one is
                                   set is a wiring bug (HandlerWiringError) unless
                                   replace=True; None clears.
    add_on_cancel(fn)              fn(conn, run): the broker's cancel_all, the runner's cancel.
                                   Called in order BEFORE the run is cancelled.
    add_on_resume(fn)              fn(conn, run): the run was taken out of a pause and its
                                   events re-delivered; the loop continues (see RUN.RESUME).
    add_on_run_disconnect(fn)      fn(conn, run): the device is gone (used here).

`run` is the runs.Run record (ids, state names, counters; no content). Module-level functions
with these names drive the process-wide instance (get_handlers()); tests build their own
RunHandlers.

INSTALL
-------
install(router=None, *, handlers=None) registers the handlers on the router (default: the
process-wide router), registers the disconnect listener, and, for the process-wide router,
makes it the socket's handler (router.install()). Idempotent: a second call registers nothing
twice (the router keeps one handler per type and one copy of a listener). Importing this
module installs nothing: api/server.py makes the explicit call, next to the line that includes
the socket's router.

LOGGING
-------
Run id, device id, user id, state names, error codes and the TYPE name of an exception. Never
a message, a payload, a history, an event, an exception's text or a hook (tests/static/
test_run_handlers_logging.py reads this file and fails if a logging call names one). Content
stays in the encrypted buffer (protocol README, 6).

NOT HERE
--------
The model loop (S07) and the tool broker (S09): they plug in through the integration points.
"""
from __future__ import annotations

import asyncio
import collections
import logging

from starlette.concurrency import run_in_threadpool

from codeagent import buffer, connections, emitter as emitter_module, router as router_module
from codeagent import run_manager, runs, ws
from codeagent.connections import ConnectionClosed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Constants (tests inject others)
# ---------------------------------------------------------------------

# The number welcome advertises; read at call time, so a test can monkeypatch it.
MAX_PARALLEL_RUNS = ws.MAX_PARALLEL_RUNS
HOOK_TIMEOUT_SECONDS = 10.0            # one hook may take this long; then it is cancelled
RETRY_AFTER_SECONDS = 5                # details.retry_after_s of a rate_limited answer
DISCONNECT_REASON = "device_disconnected"
CANCEL_REASON = "cancelled_by_device"
RESUME_REASON = "resumed"
MAX_QUEUED_TURNS = 5                   # messages that may wait behind a run's running turn
TURN_STOP_TIMEOUT_SECONDS = 5.0        # how long stop_turns waits for a cancelled turn to unwind
# Seconds to wait before each retry of purge_run after a CleanupIncomplete (the buffer has a
# TTL, so giving up after the last one costs only time).
PURGE_RETRY_DELAYS_SECONDS = (5.0, 30.0, 120.0, 600.0)
# A turn is in progress or may start only in these states.
TURN_STATES = frozenset({"running", "awaiting_approval"})

# ---------------------------------------------------------------------
# Fixed texts (error.json: "message is fixed, sanitized text", 1..500 characters).
# Nothing the client sent, and no exception text, is ever put in one.
# ---------------------------------------------------------------------

MSG_RUN_NOT_FOUND = "No such run."
MSG_NOT_ACCEPTED = "The server could not accept this request."
MSG_RUN_ID_TAKEN = "That run id cannot be used."
MSG_NOT_NOW = "The run is not in a state that allows this."
MSG_TOO_MANY_RUNS = "Too many runs are active on this device."
MSG_NO_TURN_RUNNER = "The server cannot run a conversation turn right now."
MSG_TURN_QUEUE_FULL = "Too many messages are waiting for this run."


class HandlerWiringError(Exception):
    """Two modules claiming the single turn runner: a wiring bug, found at startup."""


# ---------------------------------------------------------------------
# The error map
# ---------------------------------------------------------------------

def map_error(exc: BaseException) -> "tuple | None":
    """(error code, fixed text, details or None) for an error of the run family, per
    run_manager's table; None for any other exception. See the module docstring.

    The classes are looked up on their modules at CALL time, never held in module-level
    tuples: a module that is reloaded (tests/unit/test_codeagent_buffer.py reloads buffer to
    simulate a restart) gets new exception classes, and a copy taken at import time would
    silently stop matching them. run_manager does the same (`except buffer.RunBufferError`)."""
    if isinstance(exc, (runs.RunNotFound, run_manager.RunNotResumable, run_manager.RunGone)):
        return "run_not_found", MSG_RUN_NOT_FOUND, None
    if isinstance(exc, runs.ParallelRunLimit):
        return "rate_limited", MSG_TOO_MANY_RUNS, {"retry_after_s": RETRY_AFTER_SECONDS}
    if isinstance(exc, runs.RunIdCollision):
        return "invalid_message", MSG_RUN_ID_TAKEN, None
    if isinstance(exc, runs.IllegalTransition):
        return "invalid_message", MSG_NOT_NOW, None
    if isinstance(exc, (run_manager.InvalidRunState, runs.InvalidRun,
                        buffer.InvalidBufferInput, buffer.BufferFull)):
        return "invalid_message", MSG_NOT_ACCEPTED, None
    if isinstance(exc, (runs.RunError, run_manager.RunManagerError, buffer.RunBufferError)):
        return "internal", ws.MSG_INTERNAL, None
    return None


def _consume(work: "asyncio.Future") -> None:
    """Mark a finished thread call's outcome as looked at (nobody is waiting for it any more)."""
    if not work.cancelled():
        work.exception()


async def _thread(fn, *args, **kwargs):
    """run_in_threadpool, except that a cancellation waits for the thread. S06's calls are
    synchronous and cannot be stopped half way: a handler that is cancelled (ws.py cancels the
    receive loop when the connection ends, then calls on_disconnect) while one is in progress
    would otherwise finish, and move the run, AFTER on_disconnect had looked at it. So the
    handler's cancellation is held until the call has finished, and the call's own result is
    dropped: what it did is in the store, which is where on_disconnect reads it."""
    work = asyncio.ensure_future(run_in_threadpool(fn, *args, **kwargs))
    work.add_done_callback(_consume)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        if not work.done():
            await asyncio.wait({work})
        raise


def _is_async_callable(fn) -> bool:
    return router_module._is_async_callable(fn)


def _require_async(fn, what: str) -> None:
    if not _is_async_callable(fn):
        raise TypeError(f"{what} must be an async function")


# ---------------------------------------------------------------------
# One run's turn queue
# ---------------------------------------------------------------------

class _Turns:
    """What the handlers hold for a run that has work: the turn being run, the messages
    waiting behind it, and the worker task. One per run, removed when the work is done."""

    __slots__ = ("active", "queue", "task")

    def __init__(self, first):
        self.active = first                      # (conn, msg) being run, or None when the worker is done
        self.queue = collections.deque()         # (conn, msg) waiting
        self.task = None


class _Resume:
    """How far one run.resume got (run.resume's undo reads it): the run as resume_run left it,
    whether the buffer was rebuilt, the pause this call took the run out of (the run record as
    it was, or None) and whether it completed a `created` run."""

    __slots__ = ("run", "rebuilt", "paused_from", "started")

    def __init__(self):
        self.run = None
        self.rebuilt = False
        self.paused_from = None
        self.started = False


# ---------------------------------------------------------------------
# The handlers
# ---------------------------------------------------------------------

class RunHandlers:
    """The run handlers and their integration points. See the module docstring.

    emitter       the RunEmitter to number/store/send with (default: emitter.get_emitter()).
    max_parallel  the per-device cap (default: the module constant MAX_PARALLEL_RUNS).
    hook_timeout  seconds one hook may take (default: HOOK_TIMEOUT_SECONDS).
    max_queued_turns  messages that may wait behind a run's running turn (default: the
                  module constant MAX_QUEUED_TURNS, read at call time).
    stop_timeout  seconds stop_turns waits for a cancelled turn (TURN_STOP_TIMEOUT_SECONDS).
    purge_delays  seconds before each purge_run retry (PURGE_RETRY_DELAYS_SECONDS)."""

    def __init__(self, *, emitter=None, max_parallel: "int | None" = None,
                 hook_timeout: "float | None" = None, max_queued_turns: "int | None" = None,
                 stop_timeout: "float | None" = None, purge_delays: "tuple | None" = None):
        if max_parallel is not None and (not isinstance(max_parallel, int)
                                         or isinstance(max_parallel, bool) or max_parallel < 1):
            raise ValueError("max_parallel must be a positive integer")
        if max_queued_turns is not None and (not isinstance(max_queued_turns, int)
                                             or isinstance(max_queued_turns, bool) or max_queued_turns < 0):
            raise ValueError("max_queued_turns must be a non-negative integer")
        self._emitter = emitter
        self._max_parallel = max_parallel
        self._hook_timeout = hook_timeout
        self._max_queued_turns = max_queued_turns
        self._stop_timeout = stop_timeout
        self._purge_delays = purge_delays
        self._turns: dict = {}
        self._purges: set = set()
        self._turn_runner = None
        self._on_cancel: list = []
        self._on_resume: list = []
        self._on_run_disconnect: list = []
        # The router compares registered handlers by identity: a bound method is a new
        # object on every attribute access, so the table holds ONE object per handler.
        self._table = {"run.start": self.on_run_start,
                       "user_message": self.on_user_message,
                       "cancel": self.on_cancel_request,
                       "run.resume": self.on_run_resume}
        self._disconnect_listener = self.on_disconnect

    # -- configuration ----------------------------------------------------
    @property
    def emitter(self):
        return self._emitter if self._emitter is not None else emitter_module.get_emitter()

    @property
    def max_parallel(self) -> int:
        return self._max_parallel if self._max_parallel is not None else MAX_PARALLEL_RUNS

    @property
    def hook_timeout(self) -> float:
        return self._hook_timeout if self._hook_timeout is not None else HOOK_TIMEOUT_SECONDS

    @property
    def max_queued_turns(self) -> int:
        return self._max_queued_turns if self._max_queued_turns is not None else MAX_QUEUED_TURNS

    @property
    def stop_timeout(self) -> float:
        return self._stop_timeout if self._stop_timeout is not None else TURN_STOP_TIMEOUT_SECONDS

    @property
    def purge_delays(self) -> tuple:
        return self._purge_delays if self._purge_delays is not None else PURGE_RETRY_DELAYS_SECONDS

    # -- the integration points ---------------------------------------------
    def set_turn_runner(self, fn, *, replace: bool = False) -> None:
        """Install the one turn runner (None clears it). See "The integration points"."""
        if fn is None:
            self._turn_runner = None
            return
        _require_async(fn, "the turn runner")
        if self._turn_runner is not None and self._turn_runner is not fn and not replace:
            raise HandlerWiringError("a different turn runner is already installed")
        self._turn_runner = fn

    def get_turn_runner(self):
        return self._turn_runner

    def _add_hook(self, hooks: list, fn, what: str) -> None:
        _require_async(fn, what)
        if fn not in hooks:
            hooks.append(fn)

    def add_on_cancel(self, fn) -> None:
        self._add_hook(self._on_cancel, fn, "an on_cancel hook")

    def add_on_resume(self, fn) -> None:
        self._add_hook(self._on_resume, fn, "an on_resume hook")

    def add_on_run_disconnect(self, fn) -> None:
        self._add_hook(self._on_run_disconnect, fn, "an on_run_disconnect hook")

    def on_cancel_hooks(self) -> tuple:
        return tuple(self._on_cancel)

    def on_resume_hooks(self) -> tuple:
        return tuple(self._on_resume)

    def on_run_disconnect_hooks(self) -> tuple:
        return tuple(self._on_run_disconnect)

    async def call_hooks(self, hooks, conn, run, what: str) -> None:
        """Call each hook(conn, run) in order. A hook that raises is logged by type; one that
        does not return in hook_timeout is cancelled and logged; the next one still runs."""
        for hook in tuple(hooks):
            try:
                await asyncio.wait_for(hook(conn, run), timeout=self.hook_timeout)
            except asyncio.TimeoutError:
                logger.error("[run_handlers] %s hook timed out run=%s device=%s user=%s",
                             what, run.id, conn.device_id, conn.user_id)
            except ConnectionClosed:
                continue                                    # the device left: that is why we are here
            except Exception as exc:                        # one broken hook must not skip the rest
                logger.error("[run_handlers] %s hook failed run=%s device=%s user=%s error=%s",
                             what, run.id, conn.device_id, conn.user_id, type(exc).__name__)

    # -- answering --------------------------------------------------------
    async def answer_error(self, conn, msg: dict, exc: BaseException) -> bool:
        """Answer `msg` with the mapped error for `exc`. False (nothing sent) if `exc` is
        not of the run family: the caller re-raises, the router contains it."""
        mapped = map_error(exc)
        if mapped is None:
            return False
        code, fixed, details = mapped
        logger.warning("[run_handlers] request refused device=%s user=%s code=%s error=%s",
                       conn.device_id, conn.user_id, code, type(exc).__name__)
        await conn.send_error(code, fixed, ref_id=router_module._ref_id(msg), details=details)
        return True

    async def _not_found(self, conn, msg: dict) -> None:
        await conn.send_error("run_not_found", MSG_RUN_NOT_FOUND, ref_id=router_module._ref_id(msg))

    # -- the guard ----------------------------------------------------------
    async def owned_run(self, conn, msg: dict, *, open_only: bool = True):
        """The runs.Run the message is about, if it is this connection's user AND device
        (and, by default, not over); otherwise None, after answering `run_not_found` (the same
        answer for every reason) or, for a fault in the store, `internal`. See "The ownership
        guard"."""
        run_id = msg.get("run_id") if isinstance(msg, dict) else None
        try:
            run = await _thread(runs.get_run, conn.user_id, run_id)
        except (runs.RunNotFound, runs.InvalidRun):
            await self._not_found(conn, msg)
            return None
        except Exception as exc:
            if await self.answer_error(conn, msg, exc):
                return None
            raise
        if run.device_id != str(conn.device_id).strip().lower() or (open_only and run.terminal):
            await self._not_found(conn, msg)
            return None
        return run

    # -- run.start ------------------------------------------------------------
    async def on_run_start(self, conn, msg: dict) -> None:
        """run.start. See the module docstring."""
        run_id, em = msg["run_id"], self.emitter
        try:
            await _thread(
                run_manager.start_run, user_id=conn.user_id, device_id=conn.device_id,
                run_id=run_id, payload=msg["payload"], max_parallel=self.max_parallel)
        except Exception as exc:
            if await self.answer_error(conn, msg, exc):
                return
            raise
        try:
            em.bind(run_id, conn.user_id, conn.device_id, last_seq=0)
            await em.emit_state_changed(run_id, "created")
            await _thread(runs.transition, conn.user_id, run_id, "running")
            await em.emit_state_changed(run_id, "running")
        except Exception as exc:
            await self._abandon(conn, run_id)
            if await self.answer_error(conn, msg, exc):
                return
            raise
        logger.info("[run_handlers] run started run=%s device=%s user=%s",
                    run_id, conn.device_id, conn.user_id)

    async def _abandon(self, conn, run_id: str) -> None:
        """A run that was recorded but could not be started: fail it (which deletes its
        buffer and frees its slot) and forget it."""
        try:
            await _thread(run_manager.end_run, conn.user_id, run_id, "failed", error_code="internal")
        except Exception as exc:
            logger.error("[run_handlers] could not fail run=%s device=%s user=%s error=%s",
                         run_id, conn.device_id, conn.user_id, type(exc).__name__)
        self.emitter.unbind(run_id)

    # -- user_message and the turn queue -----------------------------------------
    async def _tell(self, conn, msg: dict, code: str, fixed: str, details=None) -> None:
        """Answer a message from a worker task, where nobody is waiting for the result: a
        device that has left is not an error."""
        try:
            await conn.send_error(code, fixed, ref_id=router_module._ref_id(msg), details=details)
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.error("[run_handlers] could not answer device=%s user=%s code=%s error=%s",
                         conn.device_id, conn.user_id, code, type(exc).__name__)

    async def on_user_message(self, conn, msg: dict) -> None:
        """user_message: queue it for the run's turn runner and return. See the module
        docstring, "user_message and the turn queue"."""
        run = await self.owned_run(conn, msg)
        if run is None:
            return
        if run.state not in TURN_STATES:
            await conn.send_error("invalid_message", MSG_NOT_NOW, ref_id=router_module._ref_id(msg))
            return
        if self._turn_runner is None:
            await conn.send_error("internal", MSG_NO_TURN_RUNNER, ref_id=router_module._ref_id(msg))
            return
        turns = self._turns.get(run.id)
        if turns is None:
            turns = _Turns((conn, msg))
            self._turns[run.id] = turns
            turns.task = asyncio.create_task(self._work(run.id, turns))
            return
        if len(turns.queue) >= self.max_queued_turns:
            logger.warning("[run_handlers] turn queue full run=%s device=%s user=%s",
                           run.id, conn.device_id, conn.user_id)
            await conn.send_error("rate_limited", MSG_TURN_QUEUE_FULL, ref_id=router_module._ref_id(msg),
                                  details={"retry_after_s": RETRY_AFTER_SECONDS})
            return
        turns.queue.append((conn, msg))

    async def _work(self, run_id: str, turns: _Turns) -> None:
        """The run's worker: take the turns one at a time, in order, until none is left.
        There is no await between "the queue is empty" and removing the entry, so a message
        that arrives either joins this queue or starts the next worker, never neither."""
        try:
            while turns.active is not None:
                conn, msg = turns.active
                await self._turn(run_id, conn, msg)
                turns.active = turns.queue.popleft() if turns.queue else None
        finally:
            if self._turns.get(run_id) is turns:
                del self._turns[run_id]

    async def _turn(self, run_id: str, conn, msg: dict) -> None:
        """One turn: re-read the run, then give the message to the runner."""
        try:
            run = await _thread(runs.get_run, conn.user_id, run_id)
        except Exception as exc:
            mapped = map_error(exc)
            code, fixed, details = mapped if mapped is not None else ("internal", ws.MSG_INTERNAL, None)
            logger.warning("[run_handlers] turn not started run=%s device=%s user=%s code=%s error=%s",
                           run_id, conn.device_id, conn.user_id, code, type(exc).__name__)
            await self._tell(conn, msg, code, fixed, details)
            return
        if run.terminal:
            await self._tell(conn, msg, "run_not_found", MSG_RUN_NOT_FOUND)
            return
        if run.state not in TURN_STATES:
            logger.info("[run_handlers] queued turn dropped, run is not running run=%s device=%s user=%s state=%s",
                        run_id, conn.device_id, conn.user_id, run.state)
            await self._tell(conn, msg, "invalid_message", MSG_NOT_NOW)
            return
        runner = self._turn_runner
        if runner is None:
            await self._tell(conn, msg, "internal", MSG_NO_TURN_RUNNER)
            return
        try:
            await runner(conn, run, msg)
        except Exception as exc:                            # CancelledError is not one: it must get through
            logger.error("[run_handlers] turn failed run=%s device=%s user=%s error=%s",
                         run_id, conn.device_id, conn.user_id, type(exc).__name__)
            await self._tell(conn, msg, "internal", ws.MSG_INTERNAL)

    async def stop_turns(self, run_id: str) -> int:
        """Drop the run's waiting messages and cancel its worker (the running turn gets
        stop_timeout to unwind). Returns how many waiting messages were dropped."""
        turns = self._turns.pop(run_id, None)
        if turns is None:
            return 0
        dropped = len(turns.queue)
        turns.queue.clear()
        task = turns.task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            _, pending = await asyncio.wait({task}, timeout=self.stop_timeout)
            if pending:
                logger.error("[run_handlers] a turn ignored its cancellation run=%s", run_id)
        return dropped

    def turn_active(self, run_id: str) -> bool:
        turns = self._turns.get(run_id)
        return turns is not None and turns.active is not None

    def pending_turns(self, run_id: str) -> int:
        """Messages waiting behind the running turn."""
        turns = self._turns.get(run_id)
        return 0 if turns is None else len(turns.queue)

    def turn_runs(self) -> tuple:
        """The ids of the runs that have turns in progress or waiting, sorted."""
        return tuple(sorted(self._turns))

    async def wait_idle(self, run_id: str, timeout: "float | None" = None) -> None:
        """Wait until the run has no turn running and none waiting (tests; a graceful stop)."""
        turns = self._turns.get(run_id)
        if turns is not None and turns.task is not None:
            await asyncio.wait({turns.task}, timeout=timeout)

    # -- cancel ---------------------------------------------------------------------
    async def on_cancel_request(self, conn, msg: dict) -> None:
        """cancel: hooks, stop the turns, cancel the run, announce the end. See the module
        docstring, "cancel"."""
        run = await self.owned_run(conn, msg)
        if run is None:
            return
        em = self.emitter
        announce = await self._bind_for_end(conn, run)
        await self.call_hooks(self._on_cancel, conn, run, "cancel")
        await self.stop_turns(run.id)
        if not announce:
            await self._cancel_unannounced(conn, msg, run)
            return
        cleanup_incomplete = False
        try:
            async with em.exclusive(run.id) as held:
                try:
                    await _thread(run_manager.cancel_run, conn.user_id, run.id)
                except run_manager.CleanupIncomplete:
                    cleanup_incomplete = True               # the run IS over; the buffer is purged below
                except runs.IllegalTransition:
                    await self._not_found(conn, msg)        # it ended under us: nothing to announce
                    return
                except Exception as exc:
                    if await self.answer_error(conn, msg, exc):
                        return
                    raise
                await held.emit_final("cancelled", CANCEL_REASON)
        except emitter_module.RunNotBound:
            logger.info("[run_handlers] run was released before it could be cancelled run=%s device=%s user=%s",
                        run.id, conn.device_id, conn.user_id)
            await self._not_found(conn, msg)
            return
        if cleanup_incomplete:
            self._schedule_purge(conn.user_id, run.id)
        logger.info("[run_handlers] run cancelled run=%s device=%s user=%s",
                    run.id, conn.device_id, conn.user_id)

    async def _bind_for_end(self, conn, run) -> bool:
        """Make sure the emitter can number the run's last event: it is bound, with the exact
        last seq. A bound run already has one; an unbound run (paused, or this process
        restarted) gets it from the replay. False if the run's buffer is gone or unreadable:
        then no event can be numbered and none is sent."""
        if self.emitter.is_bound(run.id):
            return True
        try:
            events = await _thread(buffer.replay_after, run.id, 0)
        except buffer.BufferMissing:
            return False
        except Exception as exc:
            logger.error("[run_handlers] could not read the replay to end a run run=%s device=%s user=%s error=%s",
                         run.id, conn.device_id, conn.user_id, type(exc).__name__)
            return False
        self.emitter.bind(run.id, conn.user_id, conn.device_id,
                          last_seq=events[-1]["seq"] if events else 0)
        return True

    async def _cancel_unannounced(self, conn, msg: dict, run) -> None:
        """cancel_run for a run whose end cannot be numbered (its buffer is gone)."""
        try:
            await _thread(run_manager.cancel_run, conn.user_id, run.id)
        except run_manager.CleanupIncomplete:
            self._schedule_purge(conn.user_id, run.id)
        except runs.IllegalTransition:
            await self._not_found(conn, msg)
            return
        except Exception as exc:
            if await self.answer_error(conn, msg, exc):
                return
            raise
        self.emitter.unbind(run.id)
        logger.info("[run_handlers] run cancelled without an announcement run=%s device=%s user=%s",
                    run.id, conn.device_id, conn.user_id)

    # -- retrying the buffer's deletion -------------------------------------------------
    def _schedule_purge(self, user_id: str, run_id: str) -> None:
        task = asyncio.create_task(self._purge_later(user_id, run_id))
        self._purges.add(task)
        task.add_done_callback(self._purges.discard)

    async def _purge_later(self, user_id: str, run_id: str) -> None:
        for delay in self.purge_delays:
            await asyncio.sleep(delay)
            try:
                await _thread(run_manager.purge_run, user_id, run_id)
            except Exception as exc:
                logger.error("[run_handlers] purge retry failed run=%s user=%s error=%s",
                             run_id, user_id, type(exc).__name__)
                continue
            logger.info("[run_handlers] buffer purged run=%s user=%s", run_id, user_id)
            return
        logger.error("[run_handlers] gave up purging run=%s user=%s; the buffer's TTL will remove it",
                     run_id, user_id)

    def purges_pending(self) -> int:
        return len(self._purges)

    async def wait_purges(self) -> None:
        """Wait for the background purge retries (tests; a graceful stop)."""
        if self._purges:
            await asyncio.gather(*list(self._purges), return_exceptions=True)

    # -- the disconnect handler ---------------------------------------------------
    @staticmethod
    def _superseded(conn) -> bool:
        """Is the device back on another live connection? (ws.py registers the new
        connection before the old one's cleanup runs.)"""
        other = connections.get_registry().get(conn.user_id, conn.device_id)
        return other is not None and other is not conn and not other.closed

    async def on_disconnect(self, conn) -> None:
        """Pause the device's open runs and put the pause in their replay. See the module
        docstring, "on_disconnect"."""
        if self._superseded(conn):
            logger.info("[run_handlers] connection superseded, runs left as they are device=%s user=%s",
                        conn.device_id, conn.user_id)
            return
        try:
            open_runs = await _thread(runs.list_open_runs, conn.user_id, conn.device_id)
        except Exception as exc:
            logger.error("[run_handlers] could not list runs on disconnect device=%s user=%s error=%s",
                         conn.device_id, conn.user_id, type(exc).__name__)
            return
        for run in open_runs:
            if run.state not in runs.SLOT_STATES:
                continue                                    # already paused: nothing to close, nothing to pause
            await self._pause_after_disconnect(conn, run)

    async def _pause_after_disconnect(self, conn, run) -> None:
        em = self.emitter
        try:
            em.bind(run.id, conn.user_id, conn.device_id, last_seq=0)
            await self.stop_turns(run.id)
            await self.call_hooks(self._on_run_disconnect, conn, run, "run-disconnect")
            if run.state == "created":
                await _thread(runs.transition, conn.user_id, run.id, "running")
            await _thread(run_manager.pause_run, conn.user_id, run.id, "paused_disconnected")
        except (runs.IllegalTransition, runs.RunNotFound):
            logger.info("[run_handlers] run ended before it could be paused run=%s device=%s user=%s",
                        run.id, conn.device_id, conn.user_id)
            em.unbind(run.id)
            return
        except Exception as exc:
            logger.error("[run_handlers] could not pause run=%s device=%s user=%s error=%s",
                         run.id, conn.device_id, conn.user_id, type(exc).__name__)
            em.unbind(run.id)
            return
        try:
            await em.emit_state_changed(run.id, "paused_disconnected", DISCONNECT_REASON)
        except Exception as exc:                            # the pause itself stands
            logger.error("[run_handlers] pause not stored in the replay run=%s device=%s user=%s error=%s",
                         run.id, conn.device_id, conn.user_id, type(exc).__name__)
        finally:
            em.unbind(run.id)
        logger.info("[run_handlers] run paused run=%s device=%s user=%s state=%s",
                    run.id, conn.device_id, conn.user_id, "paused_disconnected")

    # -- run.resume ---------------------------------------------------------------------
    async def on_run_resume(self, conn, msg: dict) -> None:
        """run.resume: reattach the run, re-deliver what the device missed, announce
        `running`, call the on_resume hooks. See the module docstring, "run.resume"."""
        run = await self.owned_run(conn, msg)
        if run is None:
            return
        payload, em = msg["payload"], self.emitter
        was_bound = em.is_bound(run.id)
        em.bind(run.id, conn.user_id, conn.device_id, last_seq=0)
        step = _Resume()
        try:
            async with em.exclusive(run.id) as held:
                await self._resume_locked(conn, run.id, payload, held, step)
        except emitter_module.RunNotBound:
            await self._not_found(conn, msg)            # ended or released while we waited
            return
        except Exception as exc:
            await self._undo_resume(conn, run.id, step)
            if not was_bound:
                em.unbind(run.id)
            if await self.answer_error(conn, msg, exc):
                return
            raise
        if not was_bound and step.run.state not in runs.SLOT_STATES:
            em.unbind(run.id)                           # a paused run holds no emitter state
        logger.info("[run_handlers] run resumed run=%s device=%s user=%s state=%s rebuilt=%s",
                    run.id, conn.device_id, conn.user_id, step.run.state, step.rebuilt)
        if step.paused_from is not None:
            await self.call_hooks(self._on_resume, conn, step.run, "resume")

    async def _resume_locked(self, conn, run_id: str, payload: dict, held, step: "_Resume") -> None:
        """Steps 3 to 6 of run.resume, with the run's lock held. `step` records how far it got,
        for _undo_resume."""
        last_seq = payload["last_seq"]
        prior = await _thread(runs.get_run, conn.user_id, run_id)
        resumed = await _thread(
            run_manager.resume_run, user_id=conn.user_id, device_id=conn.device_id, run_id=run_id,
            last_seq=last_seq, state=payload.get("state"), max_parallel=self.max_parallel)
        step.run, step.rebuilt = resumed.run, resumed.rebuilt
        if prior.state in run_manager.RESUME_FROM and resumed.run.state == "running":
            step.paused_from = prior                    # this call took the run out of that pause
        if resumed.rebuilt:
            known = last_seq                            # the counter continues from it
        elif resumed.replay:
            known = resumed.replay[-1]["seq"]
        else:
            known = await self._stored_last_seq(run_id)
        self.emitter.bind(run_id, conn.user_id, conn.device_id, last_seq=known)
        await held.redeliver(resumed.replay)
        if step.paused_from is not None:
            await held.emit_state_changed("running", RESUME_REASON)
        elif prior.state == "created" and resumed.run.state == "created":
            step.run = await _thread(runs.transition, conn.user_id, run_id, "running")
            step.started = True
            await held.emit_state_changed("running")

    async def _stored_last_seq(self, run_id: str) -> int:
        """The highest seq in the run's replay (0 if it holds none): the exact last seq when
        the device's replay came back empty."""
        events = await _thread(buffer.replay_after, run_id, 0)
        return events[-1]["seq"] if events else 0

    async def _undo_resume(self, conn, run_id: str, step: "_Resume") -> None:
        """A resume failed after the run was moved: put it back, so the device can resume again."""
        prior = step.paused_from
        if prior is not None:
            try:
                await _thread(run_manager.pause_run, conn.user_id, run_id, prior.state,
                                        error_code=prior.error_code)
            except Exception as exc:
                logger.error("[run_handlers] could not put a run back after a failed resume run=%s device=%s user=%s error=%s",
                             run_id, conn.device_id, conn.user_id, type(exc).__name__)
        elif step.started:
            await self._abandon(conn, run_id)

    # -- registration -------------------------------------------------------------
    def registrations(self) -> tuple:
        """(message type, handler) pairs for the router: stable objects, one per type."""
        return tuple(self._table.items())

    def register_on(self, router) -> None:
        for message_type, handler in self.registrations():
            router.register(message_type, handler)
        router.register_disconnect(self._disconnect_listener)


# ---------------------------------------------------------------------
# The process-wide instance and its module-level integration points
# ---------------------------------------------------------------------

_HANDLERS = RunHandlers()


def get_handlers() -> RunHandlers:
    """The single RunHandlers the loop, the broker and the router share."""
    return _HANDLERS


def set_turn_runner(fn, *, replace: bool = False) -> None:
    get_handlers().set_turn_runner(fn, replace=replace)


def add_on_cancel(fn) -> None:
    get_handlers().add_on_cancel(fn)


def add_on_resume(fn) -> None:
    get_handlers().add_on_resume(fn)


def add_on_run_disconnect(fn) -> None:
    get_handlers().add_on_run_disconnect(fn)


def install(router=None, *, handlers: "RunHandlers | None" = None) -> RunHandlers:
    """Register the handlers on `router` (default: the process-wide router, which is then
    made the socket's handler). Idempotent. Returns the handlers."""
    handlers = handlers if handlers is not None else get_handlers()
    handlers.register_on(router if router is not None else router_module.get_router())
    if router is None:
        router_module.install()
    return handlers
