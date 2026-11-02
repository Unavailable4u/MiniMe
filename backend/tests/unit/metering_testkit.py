"""Shared helpers for the S10 metering and grant tests (not collected: no test_ prefix).

  FakeLedger     the three ledger calls S10 uses, over a list: check(user_id) -> usage_ledger.CheckResult,
                 commit(**row), run_credits(user_id, run_id). Switches make each one fail, so a test can prove
                 the fail-open and fail-closed rules. `external_used` is what the account spent elsewhere
                 (the web app): it counts against the pool but is not a row of this run.
  Harness        a metering factory wired to a FakeLedger, a recording emit, the in-memory run_guard Redis of
                 run_manager_testkit.World and the real S02 pricing (or a flat fake). `new_process()` builds a
                 second factory over the SAME ledger and Redis: what a restart looks like.
  priced()       a Reply whose usage names a model S02 really prices, with a cache discount.

Fake secrets are built at run time (gitleaks flags literals shaped like keys); nothing here is one.
"""
import types

from codeagent import metering
from codeagent.model_adapter import TextDelta, Usage
from eo import usage_ledger
from tests.unit.loop_testkit import Reply, call
from tests.unit.run_manager_testkit import DEVICE, USER

CANARY = "CANARY-metering-content-do-not-log-5c0e"
RESET_AT = "2026-12-01T00:00:00+00:00"

PRICED = ("groq", "openai/gpt-oss-120b")      # S02 prices it, and gives cached tokens a 50% discount


def usage(input=1000, cached=0, output=100, *, provider=PRICED[0], model=PRICED[1], reported=True,
          stop="end_turn"):
    return Usage(input=input, cached_input=cached, output=output, reported=reported, model=model,
                 provider=provider, stop_reason=stop)


def priced(text="", calls=(), **kw) -> Reply:
    """A scripted model call whose Usage carries `kw` (tokens), on a model S02 prices."""
    events = ([TextDelta(text)] if text else []) + list(calls)
    events.append(usage(stop="tool_use" if calls else "end_turn", **kw))
    return Reply(events=events)


def tool_call(n=1):
    return call("read_file", {"path": f"f{n}.py"}, call_id=f"prov_{n}")


def fake_run(run_id="run-metering-0001", user_id=USER, device_id=DEVICE):
    return types.SimpleNamespace(id=run_id, user_id=user_id, device_id=device_id, state="running")


class FakeLedger:
    def __init__(self, pool=1000.0, external_used=0.0, resets_at=RESET_AT):
        self.pool = pool
        self.external_used = external_used
        self.resets_at = resets_at
        self.rows = []
        self.check_calls = 0
        self.commit_calls = 0
        self.degraded = False
        self.check_raises = None
        self.commit_raises = None
        self.sum_raises = None

    # -- what the code under test is given ------------------------------------------------------
    def check(self, user_id):
        self.check_calls += 1
        if self.check_raises is not None:
            raise self.check_raises
        if self.degraded:
            return usage_ledger.CheckResult(status="ok", allowed=True, degraded=True)
        used = round(self.external_used + sum(r["credits"] for r in self.rows if r["user_id"] == user_id), 6)
        remaining = max(self.pool - used, 0.0)
        allowed = remaining > 0
        return usage_ledger.CheckResult(status="ok" if allowed else "quota_exhausted", allowed=allowed,
                                        pool=self.pool, used=used, remaining=remaining, resets_at=self.resets_at)

    def commit(self, **row):
        self.commit_calls += 1
        if self.commit_raises is not None:
            raise self.commit_raises
        self.rows.append(dict(row))
        return {"credits": row.get("credits")}

    def run_credits(self, user_id, run_id):
        if self.sum_raises is not None:
            raise self.sum_raises
        return round(sum(r["credits"] for r in self.rows if r["user_id"] == user_id and r["run_id"] == run_id), 6)

    # -- test conveniences ----------------------------------------------------------------------
    def total(self, run_id=None):
        return round(sum(r["credits"] for r in self.rows if run_id is None or r["run_id"] == run_id), 6)


class Progress:
    """runs.add_progress twin that records its calls and can fail."""

    def __init__(self):
        self.calls = []
        self.raises = None

    def __call__(self, user_id, run_id, *, steps=0, credits=0.0):
        if self.raises is not None:
            raise self.raises
        self.calls.append((user_id, run_id, steps, credits))


class Harness:
    """One metering factory over fakes. `emit` is a Recorder (loop_testkit) unless given."""

    def __init__(self, redis=None, *, ledger=None, emit=None, policy=None, flat_credits=None, progress=None,
                 real_progress=False):
        """`redis` None: the store reads run_guard's Redis lazily (the loop tests, where World patches it in)."""
        from tests.unit.loop_testkit import Recorder
        self.redis = redis
        self.ledger = ledger or FakeLedger()
        self.rec = emit or Recorder()
        self.policy = policy or metering.RunCeilingPolicy(fixed=10.0)
        self.flat_credits = flat_credits
        self.real_progress = real_progress
        self.progress = progress or Progress()
        self.store = metering.RunBudgetStore(redis=None if redis is None else (lambda: self.redis))
        self.factory = self._build()

    def _build(self):
        kw = {}
        if self.flat_credits is not None:
            flat = self.flat_credits
            kw["credits"] = lambda provider, model, i, c, o: flat
        if not self.real_progress:
            kw["progress"] = self.progress
        return metering.make_metering_factory(
            emit=self.rec, check=self.ledger.check, commit=self.ledger.commit,
            ledger_sum=self.ledger.run_credits, store=self.store, policy=self.policy, **kw)

    def new_process(self):
        """A second factory with no in-memory state, over the same ledger and Redis."""
        self.factory = self._build()
        return self.factory

    def metering_for(self, run=None):
        return self.factory(run or fake_run())
