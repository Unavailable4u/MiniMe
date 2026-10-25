"""Shared fakes for the S06 run-manager tests (not collected: no test_ prefix).

  Clock          a controllable clock, so timestamps can be told apart without
                 sleeping.
  FakeRunStore   an in-memory codeagent.runs.PgRunStore with the same atomic
                 semantics: one lock makes every method atomic, like a single SQL
                 statement; `move` only succeeds if the row is still in the state
                 the caller read (the conditional UPDATE); the parallel-run cap is
                 counted inside the same critical section as the insert / resume
                 (the advisory lock). It also enforces the two things the
                 migration's trigger enforces, so a test that tries to cheat the
                 state machine through the store fails the way the database would:
                 a terminal row never changes state, and counters never decrease.
                 The SQL itself, the trigger and the policies are covered by
                 tests/integration/test_runs_pg.py.

                 Two hooks make races deterministic instead of lucky:
                   after_get(user_id, run_id, row)   called after every `get`, outside
                                                     the lock -- put a barrier here so
                                                     every racer has READ before any
                                                     of them WRITES;
                   before_move(call)                 called at the start of every
                                                     `move`, outside the lock -- mutate
                                                     the row here to simulate another
                                                     process winning in between.
  BoomStore      every call raises, with a canary in the message, to prove the
                 module fails closed and never logs or re-raises an error's text.
"""
import threading
import uuid
from datetime import datetime, timezone

from codeagent import runs

CANARY = "CANARY-secret-do-not-log-7f3a"
USER = "user-0001"
OTHER_USER = "user-0002"
DEVICE = "3f1c2d4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
OTHER_DEVICE = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:20]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def advance(self, seconds):
        self.t += seconds

    def dt(self):
        return datetime.fromtimestamp(self.t, tz=timezone.utc)


class FakeRunStore:
    def __init__(self, clock: Clock = None):
        self.clock = clock or Clock()
        self.lock = threading.Lock()
        self.rows = {}                  # id -> dict
        self.after_get = None
        self.before_move = None
        self.move_calls = 0
        self.insert_calls = 0

    # -- helpers -------------------------------------------------------
    def _view(self, row):
        return dict(row)

    def _slots_used(self, user_id, device_id):
        return sum(1 for r in self.rows.values()
                   if r["user_id"] == user_id and r["device_id"] == device_id
                   and r["state"] in runs.SLOT_STATES)

    # -- the store interface -------------------------------------------
    def insert(self, *, run_id, user_id, device_id, profile, mode, max_parallel):
        with self.lock:
            self.insert_calls += 1
            if run_id in self.rows:
                return None
            if max_parallel is not None and self._slots_used(user_id, device_id) >= max_parallel:
                raise runs.ParallelRunLimit(max_parallel)
            now = self.clock.dt()
            self.rows[run_id] = {
                "id": run_id, "user_id": user_id, "device_id": device_id,
                "profile": profile, "mode": mode, "state": "created",
                "created_at": now, "updated_at": now, "ended_at": None,
                "error_code": None, "steps": 0, "credits": 0.0,
            }
            return self._view(self.rows[run_id])

    def get(self, *, user_id, run_id):
        with self.lock:
            row = self.rows.get(run_id)
            out = self._view(row) if row and row["user_id"] == user_id else None
        if self.after_get is not None:
            self.after_get(user_id, run_id, out)
        return out

    def move(self, *, user_id, run_id, from_state, to_state, error_code, max_parallel):
        self.move_calls += 1
        if self.before_move is not None:
            self.before_move({"user_id": user_id, "run_id": run_id, "from_state": from_state,
                              "to_state": to_state, "store": self})
        with self.lock:
            row = self.rows.get(run_id)
            if row is None or row["user_id"] != user_id or row["state"] != from_state:
                return None
            if row["state"] in runs.TERMINAL_STATES:          # the migration's trigger
                raise AssertionError("a terminal run cannot change state")
            if max_parallel is not None and self._slots_used(user_id, row["device_id"]) >= max_parallel:
                raise runs.ParallelRunLimit(max_parallel)
            now = self.clock.dt()
            row.update(state=to_state, updated_at=now, error_code=error_code,
                       ended_at=now if to_state in runs.TERMINAL_STATES else None)
            return self._view(row)

    def add_counters(self, *, user_id, run_id, steps, credits):
        with self.lock:
            row = self.rows.get(run_id)
            if row is None or row["user_id"] != user_id:
                return None
            if steps < 0 or credits < 0:                      # the migration's trigger
                raise AssertionError("counters cannot decrease")
            row["steps"] += steps
            row["credits"] = round(row["credits"] + credits, 6)
            row["updated_at"] = self.clock.dt()
            return self._view(row)

    def list_open(self, *, user_id, device_id):
        with self.lock:
            rows = [self._view(r) for r in self.rows.values()
                    if r["user_id"] == user_id and r["device_id"] == device_id and r["ended_at"] is None]
        return sorted(rows, key=lambda r: (r["created_at"], r["id"]))

    # -- test conveniences (not part of the interface) ------------------
    def force_state(self, run_id, state):
        """Move a row without going through the state machine (what 'another
        process won the race' looks like from this caller's side)."""
        with self.lock:
            row = self.rows[run_id]
            row["state"] = state
            row["ended_at"] = self.clock.dt() if state in runs.TERMINAL_STATES else None


class BoomStore:
    """Every method raises an error whose text carries a canary."""

    def _boom(self, *a, **k):
        raise RuntimeError(f"connection to {CANARY} failed")

    insert = get = move = add_counters = list_open = _boom
