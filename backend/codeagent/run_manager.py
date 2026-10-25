"""
codeagent/run_manager.py -- S06 (part c): the run manager.

S06a (runs.py) is the content-free run row and its state machine. S06b (buffer.py) is the
encrypted, expiring conversation + replay buffer. This module is the thin layer that
makes the two behave as ONE run: it is what the router (S14) and the loop (S07) call.

    ws.py (S04)        authenticated, validated connection
    S14 (next wave)    the router: maps device messages to the calls below
    run_manager.py     (here) start, resume, pause, end, cancel, sweep
    runs.py (S06a)     the run row and the transition table
    buffer.py (S06b)   the encrypted conversation + replay buffer
    S07 (later)        the loop that calls the model

Nothing here installs a MessageHandler, opens a socket or calls a model. Every function
is synchronous (db.cursor and the Redis client are); the async router calls them through
run_in_threadpool, as the rest of the backend does.

THE RULE THIS MODULE EXISTS TO KEEP
-----------------------------------
A run's content (conversation, replay events) lives in Redis only while the run is
alive, and is gone when the run is over:

  * completed, failed, expired, cancelled   -> the buffer is deleted, and so are the
                                               run_guard rails keyed by the run id
  * paused_budget / paused_disconnected /
    paused_error                            -> the buffer is KEPT until its own TTL
                                               (idle 2 h, hard cap 24 h; D6)

Moving a run to a terminal state through runs.transition() directly would skip that
cleanup. S07 and the router therefore end runs through end_run() and cancel_run().
purge_run() repeats the cleanup for a run that is already over (it is idempotent).

THE CALLS
---------
start_run(user_id, device_id, run_id, payload, max_parallel)
    run.start. Validates the payload against protocol/v1, records the run (state
    `created`), then creates its buffer: the payload minus `history` is the buffer's
    state, `history` its first conversation items, the stream counter starts at 0. If
    the buffer cannot be created the run is moved to `failed` (code internal) so it does
    not sit in `created` holding one of the device's parallel-run slots, and the error
    is re-raised. A missing RUN_BUFFER_SECRET is detected BEFORE the row is written.

resume_run(user_id, device_id, run_id, last_seq, state, max_parallel)
    run.resume (protocol/README.md section 5). The run must be this account's AND this
    device's (anything else is RunNotFound, indistinguishable from a missing run), and
    not over (RunNotResumable). Then:
      1. buffer alive -> replay_after(last_seq): the events come back verbatim and the
         buffer's TTLs are refreshed. `state`, if sent, is ignored: the buffer is truth.
      2. buffer missing, expired or unreadable (BufferMissing / BufferExpired /
         BufferCorrupt) AND the device sent `state` -> the state is validated against
         protocol/v1 FIRST, then the buffer is rebuilt from the device's `history`
         (create_buffer(last_seq=last_seq): the counter continues at last_seq + 1) and
         the replay is empty (the device log is the truth, O1). An unreadable buffer is
         deleted only once a valid rebuild is in hand.
      3. buffer gone and no `state` -> RunGone. Nothing is changed.
    Only after the buffer is sorted out does the run leave a pause: paused_disconnected
    and paused_error -> running (the parallel-run cap is checked, as in runs.py).
    paused_budget is NOT resumed here: a run that stopped for budget waits for the
    budget (S07 / the plan owner), so the device is reattached and the run stays
    paused_budget. A run already running/awaiting_approval (the device came back before
    the server noticed it was gone) is reattached as it is.
    Returns Resumed(run, replay, rebuilt).

pause_run(user_id, run_id, to_state, error_code)
    Move to a paused_* state. The buffer is deliberately left alone.

end_run(user_id, run_id, to_state, error_code)     to_state: completed | failed | expired
cancel_run(user_id, run_id, error_code)
    Move to the terminal state (atomically, runs.transition), then delete the buffer and
    the run_guard rails. cancel_run first sets eo/run_guard's cancel flag
    (request_cancel(run_id)), so an in-flight worker is told to stop, and only for a run
    that is this account's and still open. If the move succeeded but the buffer could
    not be deleted, CleanupIncomplete (carrying the moved run) is raised: the run IS over,
    call purge_run() to retry; the buffer has a TTL anyway.

purge_run(user_id, run_id)
    For a run that is over: delete the buffer and the run_guard rails again. RunNotEnded
    for an open run.

expire_stale(now, ...)
    The housekeeping sweep (no scheduler here: something calls it every few minutes).
    Open runs idle for at least SWEEP_MIN_IDLE_SECONDS whose buffer is gone are moved to
    `expired` and cleaned up. "Gone" is decided by buffer.exists(), which also applies the
    24 h hard cap. Read below for what it will not do.

HOW EXPIRE_STALE DECIDES (and refuses to guess)
-----------------------------------------------
* It only looks at open runs whose row has been idle for SWEEP_MIN_IDLE_SECONDS (300 s):
  a run is recorded before its buffer is written, and a run that was just created, just
  resumed or just heartbeat must never be expired by a sweep that raced it.
* The run row's own age is NOT a hard cap. A resumed run gets a fresh buffer (and a fresh
  24 h) from the device's history; only the BUFFER's age is capped (D6).
* It never guesses. If it cannot tell whether the buffer is alive -- Redis is down
  (BufferUnavailable) or the secret is missing (BufferNotConfigured) -- the sweep stops
  with that error and expires nothing more. A buffer that is there but cannot be read
  (BufferCorrupt: a retired key) is left alone and counted as `unreadable`: it will go
  with its TTL.
* It pages through the open runs with a keyset on (updated_at, id), so a pile of
  healthy old paused runs at the front cannot starve the ones behind them, and it stops
  after max_runs. Runs it expires are moved by runs.transition() as the run's own
  account, so the trigger, RLS and the transition table all still apply; only the
  READ is cross-account (db.cursor(trusted=True), the path migration 0014's select
  policy names for "the housekeeping sweep"). It never writes to the table by itself.

WHAT THIS MODULE DOES NOT DO
----------------------------
No MessageHandler, no model call, no budget decisions, no "pause every run of a device
that disconnected" (S14 knows which events to send around that), no scheduler.

ERRORS, AND WHAT THE ROUTER MIGHT SAY (S14 decides; none of these carries content)
-----------------------------------------------------------------------------------
    runs.RunNotFound, RunNotResumable, RunGone ........ run_not_found
    InvalidRunState, runs.RunIdCollision, runs.InvalidRun,
      buffer.InvalidBufferInput, buffer.BufferFull ..... invalid_message
    runs.ParallelRunLimit ............................. rate_limited
    runs.IllegalTransition ............................ invalid_message (a stale or
                                                        duplicate request)
    runs.RunStoreUnavailable, buffer.BufferUnavailable,
      buffer.BufferNotConfigured, ProtocolSchemasUnavailable,
      CleanupIncomplete ............................... internal (and, for an open run,
                                                        S07 moves it to paused_error)
All of the infrastructure ones are fail closed: this is not metering, a run that cannot
be recorded or buffered must not run.

LOGGING
-------
Run ids, account ids, device ids, state names and the TYPE of an exception. Never the
payload, the state, the history, an event, a key name, a secret or an exception's text
(tests/static/test_run_manager_logging.py reads this file and fails if a logging call
mentions one). There is no logger.exception() and no exc_info anywhere.

DECISIONS THIS PATCH HAD TO MAKE (each is a small change if you disagree)
-------------------------------------------------------------------------
* A new module, not an edit of runs.py: S06a stays as it was reviewed, and runs.py's
  tests do not change.
* Tolerant schema checks (validate_message(tolerant=True)), exactly like the socket
  (ws.py): an additive field from a newer client is accepted, everything the schema
  requires is enforced, including every history item.
* paused_budget is not resumed by run.resume (see resume_run).
* `cancel` clears the run_guard rails as part of the terminal cleanup, as the work
  package says ("deleted when the run reaches a terminal state, and so are any run_guard
  cancel keys"). The cancel flag therefore lives only from request_cancel() until the end
  of cancel_run(); a loop that polls it more slowly than that will not see it, and must
  also treat "the run is over / the buffer is gone" (BufferMissing, a terminal row) as
  its stop signal. Keeping the flag for its 6 h TTL instead is one line (skip
  _clear_guard in cancel_run).
* run_guard is cleared with its public begin_run(run_id), which resets every rail (the
  cancel flag, tokens, budget, failure counters) both in Redis and in its in-process
  cache. It is fail-open by design (bookkeeping), so a failure there is not an error
  here; every one of those keys has a 6 h TTL and holds no content.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from codeagent import buffer, runs
from eo import db, run_guard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

SWEEP_MIN_IDLE_SECONDS = 300          # a row idle for less than this is never swept
SWEEP_PAGE_SIZE = 200
SWEEP_MAX_RUNS = 5000                 # one sweep looks at no more than this many rows

END_STATES = ("completed", "failed", "expired")      # end_run(); cancel has cancel_run()
RESUME_FROM = frozenset({"paused_disconnected", "paused_error"})


# ---------------------------------------------------------------------
# Errors (fixed text: never content, never an exception's text)
# ---------------------------------------------------------------------

class RunManagerError(Exception):
    """Base class of this module's own errors (runs.RunError and buffer.RunBufferError
    are the other two families the calls can raise)."""


class RunNotResumable(RunManagerError):
    """The run is over. `state` is its terminal state name."""

    def __init__(self, state: str):
        self.state = state
        super().__init__("the run is over and cannot be resumed")


class RunGone(RunManagerError):
    """run.resume: the server's buffer is gone AND the device sent no usable state."""

    def __init__(self):
        super().__init__("the run's buffer is gone and no state was sent to rebuild it")


class InvalidRunState(RunManagerError):
    """The run.start payload or the run.resume state is not acceptable. `issues` are
    "path: schema keyword" strings -- never the offending value."""

    def __init__(self, issues=()):
        self.issues = list(issues)[:20]
        super().__init__("the run state is not valid")


class ProtocolSchemasUnavailable(RunManagerError):
    """protocol/v1 or its validator could not be loaded. Fail closed."""

    def __init__(self):
        super().__init__("the protocol schemas are unavailable")


class RunNotEnded(RunManagerError):
    """purge_run() for a run that is still open."""

    def __init__(self, state: str):
        self.state = state
        super().__init__("the run has not ended")


class CleanupIncomplete(RunManagerError):
    """The run reached its end state but its buffer could not be deleted. `run` is the
    moved run. Retry with purge_run(); the buffer expires on its own anyway."""

    def __init__(self, run: "runs.Run"):
        self.run = run
        super().__init__("the run ended but its buffer could not be deleted")


# ---------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Resumed:
    """What resume_run returns. `replay` are the stored events after last_seq, verbatim
    and in seq order (empty after a rebuild); `rebuilt` says the buffer was rebuilt from
    the device's history."""
    run: "runs.Run"
    replay: list
    rebuilt: bool


@dataclass(frozen=True)
class SweepReport:
    """What expire_stale did. Counts only."""
    examined: int = 0
    expired: int = 0
    kept: int = 0
    unreadable: int = 0
    raced: int = 0
    cleanup_failed: int = 0


# ---------------------------------------------------------------------
# Protocol validation
# ---------------------------------------------------------------------

def _protocol():
    """P00's validate.py as a module, the way the socket loads it. Fail closed."""
    try:
        from codeagent import ws          # deferred: importing this module needs no FastAPI
        return ws.load_protocol()
    except Exception as exc:              # noqa: BLE001 - ProtocolUnavailable, ImportError, ...
        logger.error("[run_manager] protocol validator unavailable (%s)", type(exc).__name__)
        raise ProtocolSchemasUnavailable() from None


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _validate(run_id: str, message_type: str, payload) -> None:
    """Check `payload` as the payload of a `message_type` envelope with the same
    (tolerant) rules the socket applies. InvalidRunState lists paths and keywords only:
    a schema message can quote the offending value."""
    protocol = _protocol()
    envelope = {"v": 1, "t": message_type, "id": str(uuid.uuid4()), "run_id": run_id,
                "ts": _timestamp(), "payload": payload}
    try:
        issues = protocol.validate_message(envelope, tolerant=True)
    except Exception as exc:              # noqa: BLE001 - a validator bug is not a pass
        logger.error("[run_manager] protocol validation failed (%s)", type(exc).__name__)
        raise ProtocolSchemasUnavailable() from None
    if issues:
        raise InvalidRunState(f"{i.path or '<root>'}: {i.validator}" for i in issues)


def _split(state: dict) -> tuple:
    """(the buffer's state, the history list): the run.start shape minus `history`."""
    body = {k: v for k, v in state.items() if k != "history"}
    return body, list(state.get("history") or [])


# ---------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------

def _clear_guard(run_id: str) -> None:
    """Remove the run_guard rails (cancel flag, token budget, failure breaker) for the
    run, in Redis and in run_guard's in-process cache. begin_run() is that public reset;
    it is fail-open, and every key it clears has a TTL and holds no content."""
    run_guard.begin_run(run_id)


def _cleanup(run: "runs.Run") -> None:
    """Delete everything the run left in Redis. CleanupIncomplete if the buffer could
    not be deleted (the run_guard clearing is attempted regardless)."""
    failed = False
    try:
        buffer.delete_buffer(run.id)
    except buffer.RunBufferError as exc:
        logger.error("[run_manager] buffer cleanup failed run=%s (%s)", run.id, type(exc).__name__)
        failed = True
    _clear_guard(run.id)
    if failed:
        raise CleanupIncomplete(run) from None


def _drop_buffer_quietly(run_id: str) -> None:
    try:
        buffer.delete_buffer(run_id)
    except buffer.RunBufferError as exc:
        logger.error("[run_manager] buffer cleanup failed run=%s (%s)", run_id, type(exc).__name__)


# ---------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------

def start_run(*, user_id: str, device_id: str, run_id: str, payload: dict,
              max_parallel: "int | None" = None) -> "runs.Run":
    """Record a new run (state `created`) and create its buffer from the run.start
    `payload`. See the module docstring."""
    _validate(run_id, "run.start", payload)
    state, history = _split(payload)
    buffer.require_configured()                         # before a row that could never get a buffer
    run = runs.create_run(user_id=user_id, device_id=device_id, run_id=run_id,
                          profile=state["profile"], mode=state["mode"], max_parallel=max_parallel)
    try:
        buffer.create_buffer(run.id, state=state, history=history, last_seq=0)
    except Exception as exc:                            # noqa: BLE001 - any failure: do not leave a zombie
        logger.error("[run_manager] buffer creation failed run=%s (%s)", run.id, type(exc).__name__)
        try:
            runs.transition(user_id, run.id, "failed", error_code="internal")
        except runs.RunError as inner:
            logger.error("[run_manager] could not mark run=%s failed (%s)", run.id, type(inner).__name__)
        raise
    return run


# ---------------------------------------------------------------------
# resume_run
# ---------------------------------------------------------------------

def resume_run(*, user_id: str, device_id: str, run_id: str, last_seq: int,
               state: "dict | None" = None, max_parallel: "int | None" = None) -> Resumed:
    """run.resume. See the module docstring."""
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
        raise runs.InvalidRun("last_seq must be a non-negative integer")
    run = runs.get_run(user_id, run_id)
    if run.device_id != str(device_id).strip().lower():
        raise runs.RunNotFound()                        # another device's run: same as a missing one
    if run.terminal:
        raise RunNotResumable(run.state)

    replay, rebuilt = _attach(run, last_seq, state)

    if run.state in RESUME_FROM:
        try:
            run = runs.transition(user_id, run.id, "running", expected=RESUME_FROM,
                                  max_parallel=max_parallel)
        except runs.IllegalTransition as exc:
            if exc.current in runs.TERMINAL_STATES:
                # The run ended (a sweep, a cancel) while we were attaching: a buffer we
                # just built must not outlive it.
                if rebuilt:
                    _drop_buffer_quietly(run.id)
                raise
            if exc.current not in runs.SLOT_STATES:
                raise
            run = runs.get_run(user_id, run.id)         # another resume got there first: attached
    return Resumed(run=run, replay=replay, rebuilt=rebuilt)


def _attach(run: "runs.Run", last_seq: int, state) -> tuple:
    """(replay events, rebuilt?) -- the buffer as it is, or rebuilt from the device's state."""
    unreadable = False
    try:
        return buffer.replay_after(run.id, last_seq), False
    except buffer.BufferMissing:                        # includes BufferExpired
        pass
    except buffer.BufferCorrupt:
        unreadable = True
    if state is None:
        raise RunGone()
    _validate(run.id, "run.resume", {"last_seq": last_seq, "state": state})
    body, history = _split(state)
    if body["profile"] != run.profile:
        raise InvalidRunState(["state/profile: const"])
    if unreadable:
        buffer.delete_buffer(run.id)                    # only now that a valid rebuild is in hand
    try:
        buffer.create_buffer(run.id, state=body, history=history, last_seq=last_seq)
    except buffer.BufferExists:                         # a concurrent resume rebuilt it first
        return buffer.replay_after(run.id, last_seq), True
    logger.info("[run_manager] rebuilt buffer run=%s", run.id)
    return [], True


# ---------------------------------------------------------------------
# pause / end / cancel / purge
# ---------------------------------------------------------------------

def pause_run(user_id: str, run_id: str, to_state: str, *, error_code: "str | None" = None) -> "runs.Run":
    """Move a run to paused_budget / paused_disconnected / paused_error. The buffer is
    kept (until its own TTL), so the run can be resumed."""
    if to_state not in runs.PAUSED_STATES:
        raise runs.InvalidRun("to_state must be a paused state")
    return runs.transition(user_id, run_id, to_state, error_code=error_code)


def end_run(user_id: str, run_id: str, to_state: str, *, error_code: "str | None" = None) -> "runs.Run":
    """Move a run to completed / failed / expired, then delete its buffer and its
    run_guard rails. CleanupIncomplete if the buffer could not be deleted."""
    if to_state not in END_STATES:
        raise runs.InvalidRun("to_state must be completed, failed or expired")
    run = runs.transition(user_id, run_id, to_state, error_code=error_code)
    _cleanup(run)
    return run


def cancel_run(user_id: str, run_id: str, *, error_code: "str | None" = None) -> "runs.Run":
    """Cancel an open run: set run_guard's cancel flag (so an in-flight worker stops),
    move the run to cancelled, delete the buffer and the run_guard rails."""
    run = runs.get_run(user_id, run_id)                 # ownership first: no flag for someone else's run
    if run.terminal:
        raise runs.IllegalTransition(run.state, "cancelled")
    run_guard.request_cancel(run.id, "cancelled")
    try:
        moved = runs.transition(user_id, run.id, "cancelled", error_code=error_code)
    except runs.IllegalTransition:
        _clear_guard(run.id)                            # somebody ended it first: no flag left behind
        raise
    # Any other failure (the store is down) keeps the flag: the user asked to cancel, the
    # worker should stop, and the run is still open for a retry.
    _cleanup(moved)
    return moved


def purge_run(user_id: str, run_id: str) -> None:
    """Delete the buffer and the run_guard rails of a run that is over. Idempotent."""
    run = runs.get_run(user_id, run_id)
    if not run.terminal:
        raise RunNotEnded(run.state)
    _cleanup(run)


# ---------------------------------------------------------------------
# expire_stale
# ---------------------------------------------------------------------

_SWEEP_COLUMNS = "id, user_id, updated_at"
_SWEEP_FIRST_SQL = (
    "select " + _SWEEP_COLUMNS + " from runs "
    "where ended_at is null and updated_at < %s "
    "order by updated_at, id limit %s"
)
_SWEEP_NEXT_SQL = (
    "select " + _SWEEP_COLUMNS + " from runs "
    "where ended_at is null and updated_at < %s and (updated_at, id) > (%s, %s) "
    "order by updated_at, id limit %s"
)


class PgSweepStore:
    """The one cross-account READ: open runs idle since before a cutoff. Trusted
    internal path (migration 0014's select policy names it for the housekeeping sweep).
    Returns ids and timestamps only."""

    def list_open(self, *, updated_before, after, limit) -> list:
        with db.cursor(trusted=True) as cur:
            if after is None:
                cur.execute(_SWEEP_FIRST_SQL, (updated_before, limit))
            else:
                cur.execute(_SWEEP_NEXT_SQL, (updated_before, after[0], after[1], limit))
            return [dict(r) for r in cur.fetchall()]


_SWEEP = PgSweepStore()


def _sweep_page(**kwargs) -> list:
    try:
        return _SWEEP.list_open(**kwargs)
    except Exception as exc:                            # noqa: BLE001 - fail closed on anything
        logger.error("[run_manager] sweep read failed (%s)", type(exc).__name__)
        retry_after = exc.retry_after if isinstance(exc, db.DatabaseUnavailable) else None
        raise runs.RunStoreUnavailable(retry_after=retry_after) from None


def _as_utc(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.fromtimestamp(now, tz=timezone.utc)
    raise runs.InvalidRun("now must be a datetime or a unix time")


def expire_stale(now=None, *, min_idle_seconds: "int | None" = None, max_runs: "int | None" = None,
                 page_size: "int | None" = None) -> SweepReport:
    """Move open runs whose buffer is gone to `expired` and clean up after them. See the
    module docstring, "How expire_stale decides". Raises BufferUnavailable,
    BufferNotConfigured or RunStoreUnavailable when it cannot tell, after which nothing
    more is expired in this call."""
    now = _as_utc(now)
    idle = SWEEP_MIN_IDLE_SECONDS if min_idle_seconds is None else min_idle_seconds
    limit = SWEEP_MAX_RUNS if max_runs is None else max_runs
    size = SWEEP_PAGE_SIZE if page_size is None else page_size
    for value in (idle, limit, size):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise runs.InvalidRun("sweep limits must be non-negative integers")
    if size < 1:
        raise runs.InvalidRun("sweep limits must be non-negative integers")
    cutoff = now - timedelta(seconds=idle)

    counts = dict(examined=0, expired=0, kept=0, unreadable=0, raced=0, cleanup_failed=0)
    after = None
    while counts["examined"] < limit:
        want = min(size, limit - counts["examined"])
        page = _sweep_page(updated_before=cutoff, after=after, limit=want)
        for row in page:
            counts["examined"] += 1
            _sweep_one(row, counts)
        if len(page) < want:
            break                                       # the last page
        after = (page[-1]["updated_at"], page[-1]["id"])
    report = SweepReport(**counts)
    if report.expired:
        logger.info("[run_manager] sweep expired=%d kept=%d unreadable=%d raced=%d",
                    report.expired, report.kept, report.unreadable, report.raced)
    return report


def _sweep_one(row: dict, counts: dict) -> None:
    run_id, user_id = row["id"], row["user_id"]
    try:
        alive = buffer.exists(run_id)                   # applies the 24 h hard cap too
    except buffer.BufferCorrupt:
        counts["unreadable"] += 1                       # there, but not readable: not ours to expire
        return
    if alive:
        counts["kept"] += 1
        return
    try:
        run = runs.transition(user_id, run_id, "expired")
    except (runs.IllegalTransition, runs.RunNotFound):
        counts["raced"] += 1                            # somebody ended it (or resumed it) first
        return
    counts["expired"] += 1
    try:
        _cleanup(run)
    except CleanupIncomplete:
        counts["cleanup_failed"] += 1
