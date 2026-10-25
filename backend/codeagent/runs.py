"""
codeagent/runs.py -- S06 (part a): the run record and its state machine.

A *run* is one conversation between a device and the model, from the device's
`run.start` to a terminal state. This module owns the content-free half of it:
the `runs` row (migration 0014) and the rules for moving that row between states.
Content -- the conversation and the replay events -- never touches this module or
that table; it lives only in the encrypted, expiring Redis buffer (codeagent/
buffer.py, the next part of S06; build plan section O1).

WHERE THIS SITS
---------------
    ws.py (S04)      authenticated, validated connection; hands every run-scoped
                     message to the installed MessageHandler
    S14 (next wave)  the router that IS that handler: maps messages to the calls here
    runs.py (here)   the run record, the state machine, the per-device run cap
    buffer.py        (S06, next part) the encrypted conversation + replay buffer
    S07 (later)      the loop that calls the model and drives the state machine

Nothing here installs a MessageHandler, talks to a socket or calls a model. Every
function is synchronous (db.cursor is); the async router calls them through
run_in_threadpool, as the rest of the backend does.

THE STATE MACHINE
-----------------
    created --> running <--> awaiting_approval --(via running)--> completed
                  |  ^               |
                  v  |               v
          paused_budget / paused_disconnected / paused_error  --(grant more /
                  |                                              reconnect / retry)--> running
                  v
          cancelled | failed | expired          (also from created, and from any
                                                 open state)

The edges are written out as an explicit table, TRANSITIONS, and nothing else
decides what is legal. The terminal states -- completed, cancelled, failed, expired
-- have no outgoing edges, and the database refuses to change them (a trigger in
migration 0014), so "final" holds even if this module has a bug.

Each transition is atomic in the database: it is one `UPDATE ... WHERE id = ... AND
state = <the state the caller read>`, so when two callers race, Postgres lets exactly
one UPDATE match. transition() reads the row, checks the edge against the table,
and issues that conditional UPDATE. If it matched nothing, somebody else moved the
run in between; transition() re-reads and judges the move again from the NEW state
(a bounded number of times, MAX_TRANSITION_ATTEMPTS), so the outcome is always a
valid serial history: two cancels -> one wins and the other gets IllegalTransition
(cancelled -> cancelled is not an edge); a pause racing a completion -> either the
pause lands first and completion is then refused, or completion lands first and the
pause is refused. A lost update is impossible; a move that is no longer legal is
reported, never forced. If the state never settles, TransitionConflict.

THE PER-DEVICE RUN CAP
----------------------
ws.py advertises welcome.limits.max_parallel_runs and says "enforced by the run
manager (S06)". create_run(max_parallel=N) and a resume out of a paused state
(transition(..., max_parallel=N)) take a per-device advisory lock inside the
transaction, count the device's runs in a SLOT state (created, running,
awaiting_approval) and refuse with ParallelRunLimit when the device is full, so two
simultaneous run.start messages cannot both take the last slot. A paused run holds no
slot (it holds no worker), so resuming one needs a free slot, which is why that path
checks too. Moves between slot states never change the count and never check. The
router passes the number it announced in welcome; None means "no cap".

WHAT IS STORED, AND WHAT IS NOT
-------------------------------
id (the client-minted opaque run id, the same string usage_ledger.run_id carries),
account, device, profile, mode, state, three timestamps, one protocol error CODE and
two counters (steps, credits). There is no column that could hold a prompt, a path
or a message, and validation here refuses anything that is not a vocabulary word, a
protocol error code or an id before it reaches the database. This module logs run
ids, account ids and state names -- never an exception's text (an infrastructure
error can name hosts, keys or values): only its TYPE.

FAIL CLOSED
-----------
These are not metering helpers; a run that cannot be recorded must not run. A
database fault of any kind -- pool exhausted, relation missing (migration not
applied), permission, timeout, a driver bug -- becomes RunStoreUnavailable, with fixed
text, no chained cause (a driver message can carry a value) and, for a pool timeout,
the retry hint. S07 turns it into paused_error. Domain refusals (RunNotFound,
RunIdCollision, IllegalTransition, ParallelRunLimit, InvalidRun) are never reported as
"unavailable".

DECISIONS THIS PATCH HAD TO MAKE (each is a one-line change if you disagree)
----------------------------------------------------------------------------
* awaiting_approval does NOT go straight to completed. The work package's chain reads
  `running <-> awaiting_approval -> completed`; protocol/README.md says awaiting_approval
  is left "when the approval or result arrives", i.e. the run returns to running first.
  Allowing the direct edge is one entry in TRANSITIONS (and in the test's copy).
* `expired` is reachable from every open state, created included: an abandoned run
  must be able to end wherever it stopped (S06's expire_stale will use it).
* A run id is unique across ALL accounts (it is the primary key and the ledger's key),
  so a collision is refused even when the existing run belongs to someone else. The
  existence of an id is not secret (clients mint UUIDs), and every other call is
  filtered by account.
* Each transition sets error_code to exactly what it is given (None clears it), and an
  error code may only be attached to a paused, failed, cancelled or expired target.
  A resumed run therefore carries no stale code.
* Counters (add_progress) keep working after a run has ended: a model call in flight
  when the run was cancelled still gets recorded. usage_ledger remains the metering truth.
* The cap counts SLOT states per device, not per account.
"""
from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from eo import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Vocabulary (kept equal to protocol/v1/common.json by a unit test)
# ---------------------------------------------------------------------

RUN_STATES = (
    "created", "running", "awaiting_approval", "completed",
    "paused_budget", "paused_disconnected", "paused_error",
    "cancelled", "failed", "expired",
)
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "expired"})
PAUSED_STATES = frozenset({"paused_budget", "paused_disconnected", "paused_error"})
# States in which a run occupies one of its device's parallel-run slots.
SLOT_STATES = frozenset({"created", "running", "awaiting_approval"})

ERROR_CODES = (
    "unauthorized", "device_revoked", "quota_exhausted", "run_not_found",
    "protocol_unsupported", "rate_limited", "provider_unavailable", "tool_timeout",
    "invalid_message", "internal",
)
# An error code may only describe a run that stopped, paused or was ended; it makes no
# sense on a healthy or successful state.
_CODE_TARGETS = PAUSED_STATES | {"failed", "cancelled", "expired"}

# THE table: what each state may move to. Read-only. Terminal states have no edges.
TRANSITIONS = MappingProxyType({
    "created": frozenset({"running", "cancelled", "failed", "expired"}),
    "running": frozenset({
        "awaiting_approval", "completed", "paused_budget", "paused_disconnected",
        "paused_error", "cancelled", "failed", "expired"}),
    "awaiting_approval": frozenset({
        "running", "paused_budget", "paused_disconnected", "paused_error",
        "cancelled", "failed", "expired"}),
    "paused_budget": frozenset({"running", "cancelled", "failed", "expired"}),
    "paused_disconnected": frozenset({"running", "cancelled", "failed", "expired"}),
    "paused_error": frozenset({"running", "cancelled", "failed", "expired"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
    "expired": frozenset(),
})

MAX_TRANSITION_ATTEMPTS = 5

# Same pattern as protocol/v1/common.json run_id (a unit test keeps them equal).
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_WORD_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")          # profile, mode
_MAX_USER_ID = 128


# ---------------------------------------------------------------------
# Errors -- the router translates these; none carries content
# ---------------------------------------------------------------------

class RunError(Exception):
    """Base class."""


class InvalidRun(RunError):
    """An argument is malformed (id, profile, mode, state, error code, counter, cap).
    Raised before the store is touched; the message never echoes the argument."""


class RunNotFound(RunError):
    """No such run FOR THIS ACCOUNT. Another account's run is indistinguishable from
    a missing one."""

    def __init__(self):
        super().__init__("no such run")


class RunIdCollision(RunError):
    """create_run: that run id is already taken (by anyone). The protocol answers
    invalid_message."""

    def __init__(self):
        super().__init__("run id already exists")


class IllegalTransition(RunError):
    """The move is not an edge of TRANSITIONS from the run's CURRENT state (or the
    current state is not one the caller said it expected). `current` and `target` are
    state names, safe to log."""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"illegal run transition {current} -> {target}")


class TransitionConflict(RunError):
    """The run's state changed under the caller MAX_TRANSITION_ATTEMPTS times in a
    row. Nothing was written."""


class ParallelRunLimit(RunError):
    """The device already has `limit` runs in a slot state."""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__("too many parallel runs on this device")


class RunStoreUnavailable(RunError):
    """The database could not be used. Fail closed. `retry_after` (seconds) is set when
    the cause was an exhausted connection pool."""

    def __init__(self, message: str = "run store unavailable", retry_after: "float | None" = None):
        self.retry_after = retry_after
        super().__init__(message)


# ---------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Run:
    """One row of `runs`: ids, vocabulary words, timestamps, a code, counters."""
    id: str
    user_id: str
    device_id: str
    profile: str
    mode: str
    state: str
    created_at: datetime
    updated_at: datetime
    ended_at: "datetime | None"
    error_code: "str | None"
    steps: int
    credits: float

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def _to_run(row: dict) -> Run:
    credits = row["credits"]
    return Run(
        id=row["id"], user_id=row["user_id"], device_id=str(row["device_id"]),
        profile=row["profile"], mode=row["mode"], state=row["state"],
        created_at=row["created_at"], updated_at=row["updated_at"], ended_at=row["ended_at"],
        error_code=row["error_code"], steps=int(row["steps"]),
        credits=float(credits) if isinstance(credits, Decimal) else credits,
    )


# ---------------------------------------------------------------------
# Validation (before anything reaches the store; messages never echo input)
# ---------------------------------------------------------------------

def _run_id(value) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise InvalidRun("malformed run id")
    return value


def _user_id(value) -> str:
    if (not isinstance(value, str) or not value or len(value) > _MAX_USER_ID
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise InvalidRun("malformed account id")
    return value


def _device_id(value) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise InvalidRun("malformed device id")
    try:
        return str(uuid.UUID(value))                       # lower-case canonical form
    except ValueError:
        raise InvalidRun("malformed device id") from None


def _word(value, what: str) -> str:
    if not isinstance(value, str) or not _WORD_RE.fullmatch(value):
        raise InvalidRun(f"malformed {what}")
    return value


def _state(value) -> str:
    if not isinstance(value, str) or value not in RUN_STATES:
        raise InvalidRun("unknown run state")
    return value


def _error_code(value, target: str) -> "str | None":
    if value is None:
        return None
    if not isinstance(value, str) or value not in ERROR_CODES:
        raise InvalidRun("not a protocol error code")
    if target not in _CODE_TARGETS:
        raise InvalidRun("an error code does not belong on this state")
    return value


def _cap(value) -> "int | None":
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRun("max_parallel must be a positive integer")
    return value


def _count(value, what: str, *, integer: bool):
    if isinstance(value, bool) or not isinstance(value, (int,) if integer else (int, float)):
        raise InvalidRun(f"{what} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidRun(f"{what} must be finite")
    if value < 0:
        raise InvalidRun(f"{what} cannot be negative")
    return value


def can_transition(current: str, target: str) -> bool:
    """True if `target` is an edge of TRANSITIONS from `current`."""
    return target in TRANSITIONS.get(current, frozenset())


# ---------------------------------------------------------------------
# The store: all the SQL, stateless, every method one atomic step
# ---------------------------------------------------------------------

_COLUMNS = ("id, user_id, device_id::text as device_id, profile, mode, state, "
            "created_at, updated_at, ended_at, error_code, steps, credits")

_INSERT_SQL = (
    "insert into runs (id, user_id, device_id, profile, mode) "
    "values (%s, %s, %s::uuid, %s, %s) "
    "on conflict (id) do nothing "
    "returning " + _COLUMNS
)

# One per-device lock for the whole transaction: creates and resumes of one device
# queue up behind each other, so the count below cannot go stale before the write.
_INSERT_LOCK_SQL = "select pg_advisory_xact_lock(hashtextextended(%s::text, 0))"
_INSERT_COUNT_SQL = (
    "select count(*) as n from runs "
    "where user_id = %s and device_id = %s::uuid "
    "and state in ('created', 'running', 'awaiting_approval')"
)

_GET_SQL = "select " + _COLUMNS + " from runs where id = %s and user_id = %s"

_MOVE_LOCK_SQL = (
    "select pg_advisory_xact_lock(hashtextextended(device_id::text, 0)) "
    "from runs where id = %s and user_id = %s"
)
_MOVE_COUNT_SQL = (
    "select count(*) as n from runs "
    "where user_id = %s and state in ('created', 'running', 'awaiting_approval') "
    "and device_id = (select device_id from runs where id = %s and user_id = %s)"
)
_MOVE_SQL = (
    "update runs "
    "set state = %s, updated_at = now(), "
    "ended_at = case when %s::boolean then now() else null end, "
    "error_code = %s "
    "where id = %s and user_id = %s and state = %s "
    "returning " + _COLUMNS
)

_COUNTERS_SQL = (
    "update runs "
    "set steps = steps + %s, credits = credits + %s::numeric, updated_at = now() "
    "where id = %s and user_id = %s "
    "returning " + _COLUMNS
)

_LIST_OPEN_SQL = (
    "select " + _COLUMNS + " from runs "
    "where user_id = %s and device_id = %s::uuid and ended_at is null "
    "order by created_at, id"
)


class PgRunStore:
    """All the SQL. Stateless. Every public method is one transaction.

    The guarantees -- exactly one winner per transition, a cap that two simultaneous
    creates cannot both slip under -- come from Postgres (a conditional UPDATE, an
    advisory lock), not from Python-side check-then-act. Per-run calls open the
    cursor AS the account (user_id=), so the RLS policies of migration 0014 apply on
    top of the explicit `user_id = %s` filter in every statement."""

    def insert(self, *, run_id, user_id, device_id, profile, mode, max_parallel) -> "dict | None":
        """The new row, or None if the id is taken. ON CONFLICT DO NOTHING rather than
        catching a unique violation: that error's DETAIL would print the id."""
        with db.cursor(user_id=user_id) as cur:
            if max_parallel is not None:
                cur.execute(_INSERT_LOCK_SQL, (device_id,))
                cur.execute(_INSERT_COUNT_SQL, (user_id, device_id))
                if cur.fetchone()["n"] >= max_parallel:
                    raise ParallelRunLimit(max_parallel)
            cur.execute(_INSERT_SQL, (run_id, user_id, device_id, profile, mode))
            row = cur.fetchone()
            return dict(row) if row else None

    def get(self, *, user_id, run_id) -> "dict | None":
        with db.cursor(user_id=user_id) as cur:
            cur.execute(_GET_SQL, (run_id, user_id))
            row = cur.fetchone()
            return dict(row) if row else None

    def move(self, *, user_id, run_id, from_state, to_state, error_code, max_parallel) -> "dict | None":
        """The conditional UPDATE. None when the run is gone or is no longer in
        `from_state` (somebody else moved it). With max_parallel, the device's slots
        are counted under the device lock first."""
        with db.cursor(user_id=user_id) as cur:
            if max_parallel is not None:
                cur.execute(_MOVE_LOCK_SQL, (run_id, user_id))
                if cur.fetchone() is None:
                    return None
                cur.execute(_MOVE_COUNT_SQL, (user_id, run_id, user_id))
                if cur.fetchone()["n"] >= max_parallel:
                    raise ParallelRunLimit(max_parallel)
            cur.execute(_MOVE_SQL, (to_state, to_state in TERMINAL_STATES, error_code,
                                    run_id, user_id, from_state))
            row = cur.fetchone()
            return dict(row) if row else None

    def add_counters(self, *, user_id, run_id, steps, credits) -> "dict | None":
        with db.cursor(user_id=user_id) as cur:
            cur.execute(_COUNTERS_SQL, (steps, Decimal(str(round(credits, 6))), run_id, user_id))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_open(self, *, user_id, device_id) -> list:
        with db.cursor(user_id=user_id) as cur:
            cur.execute(_LIST_OPEN_SQL, (user_id, device_id))
            return [dict(r) for r in cur.fetchall()]


_STORE = PgRunStore()


def _call(fn, **kwargs):
    """Run one store call; turn any infrastructure fault into RunStoreUnavailable.

    Domain refusals pass through untouched. Anything else is logged by TYPE only (a
    driver message can name a host, a key or a value) and re-raised with fixed text
    and no chained cause."""
    try:
        return fn(**kwargs)
    except RunError:
        raise
    except Exception as exc:                               # noqa: BLE001 - fail closed on anything
        logger.error("[runs] store error (%s)", type(exc).__name__)
        retry_after = exc.retry_after if isinstance(exc, db.DatabaseUnavailable) else None
        raise RunStoreUnavailable(retry_after=retry_after) from None


# ---------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------

def create_run(*, user_id: str, device_id: str, run_id: str, profile: str, mode: str,
               max_parallel: "int | None" = None) -> Run:
    """Record a new run in state `created`.

    `run_id` is the id the client minted in run.start. `profile` and `mode` are
    vocabulary words (shape-checked here; the router has already validated them against
    protocol/v1, and no profile is hard-coded: build plan O8). With `max_parallel`
    (welcome.limits.max_parallel_runs) the device's slot count is checked atomically
    with the insert.

    Raises InvalidRun, RunIdCollision (the id is taken, by anyone), ParallelRunLimit,
    RunStoreUnavailable."""
    user_id = _user_id(user_id)
    device_id = _device_id(device_id)
    run_id = _run_id(run_id)
    profile = _word(profile, "profile")
    mode = _word(mode, "mode")
    cap = _cap(max_parallel)
    row = _call(_STORE.insert, run_id=run_id, user_id=user_id, device_id=device_id,
                profile=profile, mode=mode, max_parallel=cap)
    if row is None:
        raise RunIdCollision()
    logger.info("[runs] created run=%s user=%s device=%s", run_id, user_id, device_id)
    return _to_run(row)


def get_run(user_id: str, run_id: str) -> Run:
    """The run, if it is this account's. RunNotFound otherwise."""
    user_id, run_id = _user_id(user_id), _run_id(run_id)
    row = _call(_STORE.get, user_id=user_id, run_id=run_id)
    if row is None:
        raise RunNotFound()
    return _to_run(row)


def transition(user_id: str, run_id: str, to_state: str, *, error_code: "str | None" = None,
               expected=None, max_parallel: "int | None" = None) -> Run:
    """Move a run to `to_state`, atomically, if TRANSITIONS allows it from the state
    the run is in right now. Returns the updated run.

    error_code: a protocol error code to record (paused / failed / cancelled / expired
        targets only). None clears whatever code the run carried.
    expected: optional collection of states the caller believes the run is in; a run
        in any other state is refused with IllegalTransition instead of being moved
        (for example: resume only out of paused_disconnected).
    max_parallel: the device's cap. It is only consulted when the move takes a slot
        (leaving a paused state for running); see the module docstring.

    Raises InvalidRun, RunNotFound, IllegalTransition (including: the run is terminal),
    ParallelRunLimit, TransitionConflict, RunStoreUnavailable."""
    user_id, run_id = _user_id(user_id), _run_id(run_id)
    target = _state(to_state)
    code = _error_code(error_code, target)
    cap = _cap(max_parallel)
    allowed = None if expected is None else frozenset(_state(s) for s in expected)

    for _ in range(MAX_TRANSITION_ATTEMPTS):
        row = _call(_STORE.get, user_id=user_id, run_id=run_id)
        if row is None:
            raise RunNotFound()
        current = row["state"]
        if (allowed is not None and current not in allowed) or not can_transition(current, target):
            raise IllegalTransition(current, target)
        takes_slot = current not in SLOT_STATES and target in SLOT_STATES
        moved = _call(_STORE.move, user_id=user_id, run_id=run_id, from_state=current,
                      to_state=target, error_code=code,
                      max_parallel=cap if takes_slot else None)
        if moved is not None:
            logger.info("[runs] run=%s user=%s %s -> %s", run_id, user_id, current, target)
            return _to_run(moved)
        # Somebody else moved (or removed) the run between our read and our update:
        # read it again and judge the move from where it really is.
    raise TransitionConflict("run state kept changing")


def add_progress(user_id: str, run_id: str, *, steps: int = 0, credits: float = 0.0) -> Run:
    """Add to the run's informational counters (and refresh its updated_at, so a call
    with zero for both is a heartbeat). Works on an ended run too. usage_ledger, not
    this, is the metering truth. Counters only go up (the migration's trigger)."""
    user_id, run_id = _user_id(user_id), _run_id(run_id)
    steps = _count(steps, "steps", integer=True)
    credits = _count(credits, "credits", integer=False)
    row = _call(_STORE.add_counters, user_id=user_id, run_id=run_id, steps=steps, credits=credits)
    if row is None:
        raise RunNotFound()
    return _to_run(row)


def list_open_runs(user_id: str, device_id: str) -> list:
    """This device's runs that have not ended (any state but a terminal one), oldest
    first. For the router: what to pause when the device disconnects, what a
    reconnecting device may resume."""
    user_id, device_id = _user_id(user_id), _device_id(device_id)
    rows = _call(_STORE.list_open, user_id=user_id, device_id=device_id)
    return [_to_run(r) for r in rows]
