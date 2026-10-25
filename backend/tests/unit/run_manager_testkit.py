"""Shared fakes for the S06c run-manager tests (not collected: no test_ prefix).

The run manager glues three things that already have in-memory twins, so this file only
adds the missing pieces and ties them together:

  World             one object holding every stand-in, with ONE clock for all of them:
                      store     runs_testkit.FakeRunStore    (the `runs` table)
                      sweep     FakeSweepStore               (the cross-account read)
                      redis     buffer_testkit.FakeRunRedis  (the run buffer)
                      guard     FakeGuardRedis               (eo/run_guard's keys)
                    advance(seconds) moves all clocks together; `now` is the same
                    instant as a datetime.
  FakeGuardRedis    the few commands eo/run_guard uses (get/set/delete/incr/incrby/
                    expire), in memory, so a test can assert that nothing is left.
  FakeSweepStore    codeagent.run_manager.PgSweepStore over the FakeRunStore's rows:
                    open runs idle since before a cutoff, keyset-paged on (updated_at, id).
  start_payload()   a run.start payload that is valid against protocol/v1.
"""
from datetime import datetime, timezone

from codeagent import buffer, run_manager, runs
from eo import run_guard
from tests.unit import buffer_testkit as bt
from tests.unit import runs_testkit as rt

USER, OTHER_USER = rt.USER, rt.OTHER_USER
DEVICE, OTHER_DEVICE = rt.DEVICE, rt.OTHER_DEVICE
SECRET = bt.SECRET
CANARY = "CANARY-run-content-do-not-log-8d41"
_UNSET = object()


def start_payload(history=None, **over) -> dict:
    payload = {"profile": "code", "mode": "ask", "cwd_label": "proj", "instructions": "be helpful",
               "rules": [], "memory_index": "", "tool_catalog_version": "v0", "model_pref": "auto"}
    if history is not None:
        payload["history"] = history
    payload.update(over)
    return payload


def history(n=2, text="turn"):
    out = []
    for i in range(n):
        out.append({"role": "user", "text": f"{text} {i} u"})
        out.append({"role": "assistant", "text": f"{text} {i} a"})
    return out


class FakeGuardRedis:
    """What eo/run_guard needs from `memory.bus.redis`."""

    def __init__(self):
        self.data = {}

    def set(self, key, value, ex=None):
        self.data[key] = str(value)
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, *keys):
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    def incr(self, key):
        return self.incrby(key, 1)

    def incrby(self, key, n):
        self.data[key] = str(int(self.data.get(key, "0")) + int(n))
        return int(self.data[key])

    def expire(self, key, seconds):
        return 1 if key in self.data else 0


class FakeSweepStore:
    def __init__(self, store: rt.FakeRunStore):
        self.store = store
        self.calls = 0
        self.after_list = None

    def list_open(self, *, updated_before, after, limit):
        self.calls += 1
        with self.store.lock:
            rows = [dict(r) for r in self.store.rows.values()
                    if r["ended_at"] is None and r["updated_at"] < updated_before]
        rows.sort(key=lambda r: (r["updated_at"], r["id"]))
        if after is not None:
            rows = [r for r in rows if (r["updated_at"], r["id"]) > (after[0], after[1])]
        out = [{"id": r["id"], "user_id": r["user_id"], "updated_at": r["updated_at"]} for r in rows[:limit]]
        if self.after_list is not None:
            self.after_list(out)
        return out


class World:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.rt_clock = rt.Clock()
        self.bt_clock = bt.Clock(self.rt_clock.t)
        self.store = rt.FakeRunStore(self.rt_clock)
        self.sweep = FakeSweepStore(self.store)
        self.redis = bt.FakeRunRedis(self.bt_clock)
        self.guard = FakeGuardRedis()
        monkeypatch.setattr(runs, "_STORE", self.store)
        monkeypatch.setattr(run_manager, "_SWEEP", self.sweep)
        monkeypatch.setattr(buffer, "_REDIS", self.redis)
        monkeypatch.setattr(buffer, "_now", self.bt_clock.time)
        monkeypatch.setattr(run_guard, "_redis", lambda: self.guard)
        monkeypatch.setenv("RUN_BUFFER_SECRET", SECRET)
        monkeypatch.delenv("RUN_BUFFER_KEY_ID", raising=False)
        monkeypatch.delenv("RUN_BUFFER_PREVIOUS_SECRETS", raising=False)
        self._reset_guard_cache()

    @staticmethod
    def _reset_guard_cache():
        with run_guard._lock:
            run_guard._local_cancel.clear()
            run_guard._remote_seen.clear()
            run_guard._local_tokens.clear()

    def teardown(self):
        self._reset_guard_cache()

    def advance(self, seconds):
        self.rt_clock.advance(seconds)
        self.bt_clock.advance(seconds)

    @property
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.rt_clock.t, tz=timezone.utc)

    # -- conveniences ---------------------------------------------------
    def start(self, run_id=None, *, user=USER, device=DEVICE, payload=_UNSET, max_parallel=None):
        return run_manager.start_run(user_id=user, device_id=device, run_id=run_id or rt.new_run_id(),
                                     payload=start_payload(history()) if payload is _UNSET else payload,
                                     max_parallel=max_parallel)

    def run_at(self, state, run_id=None, **kw):
        """A started run driven to `state` through legal moves only (buffer kept)."""
        run = self.start(run_id, **kw)
        path = {
            "created": [], "running": ["running"], "awaiting_approval": ["running", "awaiting_approval"],
            "paused_budget": ["running", "paused_budget"],
            "paused_disconnected": ["running", "paused_disconnected"],
            "paused_error": ["running", "paused_error"],
        }[state]
        user = kw.get("user", USER)
        for step in path:
            run = runs.transition(user, run.id, step)
        return run

    def guard_seed(self, run_id):
        """Put every rail run_guard keeps for a run into the fake Redis."""
        for name in ("cancel", "tokens", "budget", "fail_streak", "fail_total", "tripped"):
            self.guard.data[run_guard._k(name, run_id)] = "1"
