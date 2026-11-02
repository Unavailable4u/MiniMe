"""Patch S10 -- unit tests for codeagent/metering.py (metering inside the loop).

The real AgentLoop (S07) runs over the real S06 modules with a scripted fake model (loop_testkit.Rig); the ledger
is a list (metering_testkit.FakeLedger) and Redis is the in-memory twin run_manager_testkit.World installs. Only
the model, the broker and the ledger are fakes. Groups: exact charges, the per-run ceiling and the grant, the order
quota_exhausted -> paused_budget, warnings, fail-open metering, a new process, the ceiling policy, log hygiene.
"""
import asyncio
import logging

import pytest

from codeagent import metering, runs
from codeagent.loop import BudgetPause, Metering
from codeagent.model_adapter import Usage
from eo import credits as credits_module
from tests.unit.devices_testkit import BrokenRedis
from tests.unit.loop_testkit import LIGHT, STRONG, Reply, Rig, ScriptedAdapter, go, protocol_problems
from tests.unit.metering_testkit import (CANARY, PRICED, RESET_AT, FakeLedger, Harness, fake_run, priced, tool_call,
                                         usage)
from tests.unit.run_manager_testkit import USER, FakeGuardRedis

pytest.importorskip("jsonschema", reason="the loop validates tool arguments with jsonschema")


@pytest.fixture
def rigs(monkeypatch):
    made = []

    def make(harness, *script, **kw):
        """A real AgentLoop whose Metering factory is `harness`'s. The harness's recorder IS the loop's emit, so
        the loop's events and the metering's events share one stream and one order."""
        rig = Rig(monkeypatch, *script, metering_factory=harness.factory, emit=harness.rec,
                  emit_final=harness.rec.final, unbind=harness.rec.unbind, **kw)
        rig.rec = harness.rec
        rig.conn.log = harness.rec.log
        rig.log = harness.rec.log
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.teardown()


def credits_of(i, c, o):
    return credits_module.credits_for(*PRICED, i, c, o)


def warnings_of(rec):
    return [(e["payload"]["scope"], e["payload"]["spent_pct"]) for e in rec.events if e["t"] == "budget_warning"]


# =============================================================================================
# Exact charges
# =============================================================================================

TOKENS = [(1200, 0, 150), (1500, 900, 80), (1800, 1200, 300)]


def three_step_script():
    return [priced("looking", [tool_call(1)], input=TOKENS[0][0], cached=TOKENS[0][1], output=TOKENS[0][2]),
            priced("reading", [tool_call(2)], input=TOKENS[1][0], cached=TOKENS[1][1], output=TOKENS[1][2]),
            priced("done", input=TOKENS[2][0], cached=TOKENS[2][1], output=TOKENS[2][2])]


def test_charges_are_exact_for_a_multi_step_run(rigs):
    h = Harness(real_progress=True)
    rig = rigs(h, *three_step_script())
    result = go(rig.turn("fix the bug"))
    assert (result.outcome, result.steps) == ("finished", 3)

    expected = [credits_of(*t) for t in TOKENS]
    rows = h.ledger.rows
    assert [r["credits"] for r in rows] == expected                       # one row per call, S02's number exactly
    assert round(sum(r["credits"] for r in rows), 6) == pytest.approx(sum(expected), abs=1e-9)
    assert [(r["input_tokens"], r["cached_input_tokens"], r["output_tokens"]) for r in rows] == TOKENS
    # cached tokens are counted: the same call without the cache hit would have cost more
    assert expected[1] < credits_of(1500, 0, 80) and expected[2] < credits_of(1800, 0, 300)

    events = rig.rec.of("usage")
    assert len(events) == 3
    running = 0.0
    for event, (i, c, o), exp in zip(events, TOKENS, expected):
        running += exp
        assert (event["input_tokens"], event["cached_input_tokens"], event["output_tokens"]) == (i, c, o)
        assert event["credits"] == exp
        assert event["run_credits_total"] == pytest.approx(running, abs=1e-6)
        assert event["model"] == PRICED[1]
    assert rig.run.credits == pytest.approx(sum(expected), abs=1e-6)       # the run row's informational total
    assert rig.run.steps == 3
    assert protocol_problems(rig.rec.events) == []                          # every event fits protocol/v1


def test_every_ledger_row_is_numbers_and_opaque_ids_only(rigs):
    h = Harness(real_progress=True)
    rig = rigs(h, priced(CANARY + " reply", [tool_call()]), priced("done"),
               broker_behaviour=lambda c: {"status": "ok", "output": CANARY + " output"})
    go(rig.turn(CANARY + " user text"))
    assert len(h.ledger.rows) == 2
    for row in h.ledger.rows:
        assert set(row) == {"user_id", "surface", "run_id", "provider", "model", "input_tokens",
                            "cached_input_tokens", "output_tokens", "credits"}
        assert row["surface"] == "m2c" and row["run_id"] == rig.run_id and row["user_id"] == USER
        assert all(isinstance(row[k], int) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
        assert isinstance(row["credits"], float)
    assert CANARY not in repr(h.ledger.rows)
    assert all(CANARY not in repr(p) for p in rig.rec.of("usage"))         # (text_delta carries content on purpose)


def test_unreported_usage_writes_no_row_and_charges_nothing(rigs):
    h = Harness(real_progress=True)
    # the provider sent no usage at all: the loop hands Metering a Usage(reported=False)
    rig = rigs(h, Reply("hello", usage=False))
    result = go(rig.turn())
    assert result.outcome == "finished"
    assert h.ledger.rows == [] and h.ledger.commit_calls == 0
    assert rig.rec.of("usage") == []
    assert rig.run.credits == 0.0


def test_unreported_usage_object_directly():
    h = Harness(FakeGuardRedis())
    m = h.metering_for()

    async def scenario():
        await m.after_call(Usage(reported=False))
        await m.after_call(None)

    go(scenario())
    assert h.ledger.commit_calls == 0 and m.spent == 0.0
    assert h.rec.events == []
    assert [c[3] for c in h.progress.calls] == [0.0, 0.0]                   # a step happened, nothing was charged


# =============================================================================================
# The per-run ceiling and the grant
# =============================================================================================

def ceiling_script():
    return [priced("one", [tool_call(1)]), priced("two", [tool_call(2)]), priced("three")]


def test_a_run_pauses_at_the_ceiling_and_continues_after_a_grant(rigs):
    h = Harness(flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=2.0), real_progress=True)
    rig = rigs(h, *ceiling_script())

    first = go(rig.turn("go"))
    assert (first.outcome, first.reason) == ("paused_budget", "quota_exhausted")
    assert rig.adapter.opened == 2                                           # the third call was never made
    assert rig.state == "paused_budget" and rig.run.error_code == "quota_exhausted"
    assert rig.rec.states()[-1] == ("paused_budget", "quota_exhausted")
    assert h.ledger.total(rig.run_id) == 2.0

    # without a grant the run stays stopped: the ceiling is the ceiling
    runs.transition(USER, rig.run_id, "running", expected=("paused_budget",))
    again = go(rig.loop.resume(rig.conn, rig.run))
    assert (again.outcome, again.reason) == ("paused_budget", "quota_exhausted")
    assert rig.adapter.opened == 2

    # the grant (what the route does): raise the ceiling, move the run back
    h.store.set_ceiling(rig.run_id, 4.0)
    runs.transition(USER, rig.run_id, "running", expected=("paused_budget",))
    done = go(rig.loop.resume(rig.conn, rig.run))
    assert done.outcome == "finished"
    assert rig.adapter.opened == 3
    assert h.ledger.total(rig.run_id) == 3.0 and len(h.ledger.rows) == 3
    assert rig.state == "running"
    assert [p["run_credits_total"] for p in rig.rec.of("usage")] == [1.0, 2.0, 3.0]


def test_the_ceiling_stop_tells_the_device_why_and_gives_no_reset_time(rigs):
    h = Harness(flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=1.0))
    rig = rigs(h, priced("one", [tool_call(1)]))
    go(rig.turn())
    (error,) = rig.conn.errors
    assert error["code"] == "quota_exhausted" and error["details"] is None   # a run ceiling does not reset
    notices = rig.rec.notices()
    assert [n["code"] for n in notices] == [metering.NOTICE_RUN_CEILING] and notices[0]["severity"] == "warning"
    kinds = [entry[:2] for entry in rig.log]
    assert kinds.index(("emit", "notice")) < kinds.index(("error", "quota_exhausted")) \
        < kinds.index(("emit", "state_changed"))
    assert protocol_problems(rig.rec.events) == []


def test_pool_exhaustion_sends_the_error_first_then_paused_budget_and_never_downgrades(rigs):
    ledger = FakeLedger(pool=50.0, external_used=50.0)
    h = Harness(ledger=ledger)
    strong, light = ScriptedAdapter(name="strong"), ScriptedAdapter(name="light")
    rig = rigs(h, chain=[STRONG, LIGHT], adapters={"model-strong": strong, "model-light": light})
    result = go(rig.turn("hello"))
    assert (result.outcome, result.reason) == ("paused_budget", "quota_exhausted")
    assert strong.opened == 0 and light.opened == 0                           # no model of any class was called
    assert rig.rec.notices() == []                                            # no "model_changed": never silent, never at all
    (error,) = rig.conn.errors
    assert error["code"] == "quota_exhausted" and error["details"] == {"reset_at": RESET_AT}
    assert rig.log.index(("error", "quota_exhausted")) < rig.log.index(("emit", "state_changed", "paused_budget"))
    assert rig.rec.states() == [("paused_budget", "quota_exhausted")]
    assert rig.run.error_code == "quota_exhausted" and rig.state == "paused_budget"
    assert h.ledger.rows == []


def test_before_call_raises_a_budget_pause_with_the_protocols_words():
    h = Harness(FakeGuardRedis(), ledger=FakeLedger(pool=10.0, external_used=10.0))
    with pytest.raises(BudgetPause) as caught:
        go(h.metering_for().before_call())
    assert (caught.value.reason, caught.value.error_code, caught.value.reset_at) == \
        ("quota_exhausted", "quota_exhausted", RESET_AT)

    h2 = Harness(FakeGuardRedis(), flat_credits=3.0, policy=metering.RunCeilingPolicy(fixed=3.0))
    m = h2.metering_for()

    async def scenario():
        await m.before_call()
        await m.after_call(usage())
        await m.before_call()

    with pytest.raises(BudgetPause) as caught:
        go(scenario())
    assert (caught.value.reason, caught.value.error_code, caught.value.reset_at) == \
        ("quota_exhausted", "quota_exhausted", None)


def test_a_reset_time_that_is_not_a_protocol_timestamp_is_dropped():
    h = Harness(FakeGuardRedis(), ledger=FakeLedger(pool=10.0, external_used=10.0, resets_at="next tuesday"))
    with pytest.raises(BudgetPause) as caught:
        go(h.metering_for().before_call())
    assert caught.value.reset_at is None


def test_a_grant_made_by_another_process_is_seen_without_a_restart():
    redis = FakeGuardRedis()
    h = Harness(redis, flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=1.0))
    m = h.metering_for()

    async def spend():
        await m.before_call()
        await m.after_call(usage())

    go(spend())
    with pytest.raises(BudgetPause):
        go(m.before_call())
    h.store.set_ceiling("run-metering-0001", 2.5)                              # the route runs in another worker
    go(m.before_call())                                                        # now allowed, same Metering object


# =============================================================================================
# Warnings at 80%
# =============================================================================================

def run_calls(h, n, run=None):
    m = h.metering_for(run)

    async def scenario():
        for _ in range(n):
            await m.before_call()
            await m.after_call(usage())

    go(scenario())
    return m


def test_warnings_fire_at_80_percent_exactly_once_per_scope():
    ledger = FakeLedger(pool=100.0, external_used=70.0)
    h = Harness(FakeGuardRedis(), ledger=ledger, flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=10.0))
    run_calls(h, 7)
    assert warnings_of(h.rec) == []                                            # 70% of the run, 77% of the pool
    run_calls(h, 1)
    assert warnings_of(h.rec) == [("run", 80)]                                 # 8 of 10 credits
    run_calls(h, 2)                                                            # 9, 10 (the ceiling), 80 of the pool
    assert warnings_of(h.rec) == [("run", 80), ("account", 80)]                # each scope once, never repeated
    pool_warning = [e["payload"] for e in h.rec.events if e["payload"].get("scope") == "account"][0]
    run_warning = [e["payload"] for e in h.rec.events if e["payload"].get("scope") == "run"][0]
    assert pool_warning["reset_at"] == RESET_AT and "reset_at" not in run_warning
    assert protocol_problems(h.rec.events) == []


def test_a_warning_is_not_repeated_by_a_new_process_or_after_a_grant():
    redis = FakeGuardRedis()
    h = Harness(redis, ledger=FakeLedger(pool=1000.0), flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=10.0))
    run_calls(h, 8)
    assert warnings_of(h.rec) == [("run", 80)]
    h.new_process()
    m = h.metering_for()

    async def scenario():
        await m.before_call()                                                  # hydrates: spent 8, flags from Redis
        await m.after_call(usage())                                            # 9
        await m.after_call(usage())                                            # 10
        h.store.set_ceiling("run-metering-0001", 20.0)                         # a grant
        await m.before_call()
        for _ in range(8):
            await m.after_call(usage())                                        # 18 of 20: 90% of the new ceiling

    go(scenario())
    assert warnings_of(h.rec) == [("run", 80)]


def test_the_run_scope_is_measured_against_the_ceiling_not_the_pool():
    h = Harness(FakeGuardRedis(), ledger=FakeLedger(pool=100000.0), flat_credits=4.0,
                policy=metering.RunCeilingPolicy(fixed=10.0))
    run_calls(h, 2)                                                            # 8 credits: 0.008% of the pool, 80% of the run
    assert warnings_of(h.rec) == [("run", 80)]


def test_warning_percentages_are_integers_between_0_and_100():
    h = Harness(FakeGuardRedis(), ledger=FakeLedger(pool=100.0), flat_credits=9.5,
                policy=metering.RunCeilingPolicy(fixed=10.0))
    run_calls(h, 1)
    ((scope, pct),) = warnings_of(h.rec)
    assert (scope, pct) == ("run", 95) and isinstance(pct, int)


# =============================================================================================
# Fail open
# =============================================================================================

def test_a_failed_ledger_commit_fails_open_and_everything_else_still_happens(caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness(FakeGuardRedis(), flat_credits=2.0)
    h.ledger.commit_raises = RuntimeError("database down " + CANARY)
    m = h.metering_for()
    go(m.after_call(usage()))                                                  # does not raise
    assert h.ledger.rows == [] and h.ledger.commit_calls == 1
    assert m.spent == 2.0                                                      # the run's total moved anyway
    (event,) = h.rec.of("usage")
    assert event["credits"] == 2.0 and event["run_credits_total"] == 2.0       # the usage event was still sent
    assert [c[3] for c in h.progress.calls] == [2.0]                           # so was the run's counter
    assert CANARY not in caplog.text and "RuntimeError" in caplog.text         # the log names the type only


def test_a_degraded_check_allows_the_call():
    h = Harness(FakeGuardRedis())
    h.ledger.degraded = True
    go(h.metering_for().before_call())                                         # no BudgetPause
    assert h.ledger.check_calls == 1


def test_a_check_that_raises_allows_the_call_and_logs_the_type_only(caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness(FakeGuardRedis())
    h.ledger.check_raises = RuntimeError(CANARY)
    go(h.metering_for().before_call())
    assert CANARY not in caplog.text


def test_the_run_ceiling_still_holds_while_the_pool_cannot_be_read():
    h = Harness(FakeGuardRedis(), flat_credits=5.0, policy=metering.RunCeilingPolicy(fixed=5.0))
    h.ledger.degraded = True
    m = h.metering_for()

    async def scenario():
        await m.before_call()
        await m.after_call(usage())
        await m.before_call()

    with pytest.raises(BudgetPause):
        go(scenario())


def test_a_failing_usage_emit_does_not_stop_the_charge_or_the_warning():
    h = Harness(FakeGuardRedis(), flat_credits=9.0, policy=metering.RunCeilingPolicy(fixed=10.0))
    h.rec.fail_on = {"usage": RuntimeError(CANARY)}
    m = h.metering_for()
    go(m.after_call(usage()))
    assert len(h.ledger.rows) == 1 and m.spent == 9.0
    assert warnings_of(h.rec) == [("run", 90)]


def test_a_failing_run_counter_does_not_stop_the_usage_event():
    h = Harness(FakeGuardRedis(), flat_credits=1.0)
    h.progress.raises = RuntimeError(CANARY)
    go(h.metering_for().after_call(usage()))
    (event,) = h.rec.of("usage")
    assert event["run_credits_total"] == 1.0 and len(h.ledger.rows) == 1


def test_a_redis_that_cannot_be_read_keeps_the_base_ceiling(caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness(BrokenRedis(), flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=1.0))
    m = h.metering_for()

    async def scenario():
        await m.before_call()
        await m.after_call(usage())
        await m.before_call()

    with pytest.raises(BudgetPause):                                           # the stop does not need Redis
        go(scenario())
    assert CANARY not in caplog.text and BrokenRedis.CANARY not in caplog.text


def test_the_usage_event_never_carries_a_model_name_the_schema_would_refuse():
    h = Harness(FakeGuardRedis(), flat_credits=1.0)
    go(h.metering_for().after_call(usage(model="not a valid model!", provider="x")))
    assert h.rec.of("usage")[0]["model"] == "unknown"
    assert protocol_problems(h.rec.events) == []


# =============================================================================================
# A new process
# =============================================================================================

def test_a_new_process_starts_from_the_ledger_and_the_raised_ceiling():
    redis = FakeGuardRedis()
    h = Harness(redis, flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=2.0))
    run_calls(h, 2)
    with pytest.raises(BudgetPause):
        go(h.metering_for().before_call())

    h.new_process()                                                            # a restart: no memory, same ledger, same Redis
    fresh = h.metering_for()
    with pytest.raises(BudgetPause):                                           # spent 2.0 came back from the ledger
        go(fresh.before_call())
    assert fresh.spent == 2.0

    h.store.set_ceiling("run-metering-0001", 4.0)
    h.new_process()
    survivor = h.metering_for()
    go(survivor.before_call())                                                 # the raised ceiling survived the restart
    assert survivor.spent == 2.0 and survivor.ceiling == 4.0


def test_the_state_of_a_run_is_kept_between_turns_of_one_process():
    h = Harness(FakeGuardRedis(), flat_credits=1.0)
    first = h.metering_for()
    go(first.after_call(usage()))
    second = h.metering_for()                                                  # the loop asks for a Metering every turn
    assert second is not first and second.spent == 1.0
    go(second.after_call(usage()))
    assert h.metering_for().spent == 2.0


def test_an_evicted_run_is_rehydrated_from_the_ledger():
    h = Harness(FakeGuardRedis(), flat_credits=1.0)
    h.factory = metering.make_metering_factory(
        emit=h.rec, check=h.ledger.check, commit=h.ledger.commit, progress=h.progress,
        ledger_sum=h.ledger.run_credits, store=h.store, policy=h.policy, credits=lambda *a: 1.0, max_states=1)
    a, b = fake_run("run-aaaa-0001"), fake_run("run-bbbb-0001")

    async def scenario():
        ma = h.factory(a)
        await ma.before_call()
        await ma.after_call(usage())
        await h.factory(b).before_call()                                       # evicts a
        again = h.factory(a)
        await again.before_call()
        return again.spent

    assert go(scenario()) == 1.0


def test_a_ledger_sum_that_cannot_be_read_starts_the_run_at_zero_with_a_warning(caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness(FakeGuardRedis(), flat_credits=1.0)
    h.ledger.sum_raises = RuntimeError("no database " + CANARY)
    m = h.metering_for()
    go(m.before_call())                                                        # fails open
    assert m.spent == 0.0
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert CANARY not in caplog.text
    h.ledger.sum_raises = None
    h.ledger.commit(user_id=USER, run_id="run-metering-0001", credits=1.5)     # a row from another process
    go(m.before_call())                                                        # the read is retried, not given up on
    assert m.spent == 1.5


# =============================================================================================
# The ceiling policy
# =============================================================================================

def test_the_default_ceiling_is_a_quarter_of_the_pool_with_a_floor_of_one_credit():
    policy = metering.RunCeilingPolicy(environ={})
    assert policy.base(1690.0) == pytest.approx(422.5)
    assert policy.base(81.0) == pytest.approx(20.25)
    assert policy.base(2.0) == 1.0 and policy.base(0.0) == 1.0
    assert policy.base(None) is None
    assert (metering.RUN_CEILING_FRACTION, metering.RUN_CEILING_FLOOR) == (0.25, 1.0)


def test_the_ceiling_can_be_set_to_an_absolute_number_by_environment_or_injection():
    env = {metering.ENV_RUN_CEILING: "37.5"}
    assert metering.RunCeilingPolicy(environ=env).base(1690.0) == 37.5
    assert metering.RunCeilingPolicy(environ=env).base(None) == 37.5          # no pool needed
    assert metering.RunCeilingPolicy(fixed=3.0, environ=env).base(1690.0) == 3.0   # injection wins
    for bad in ("abc", "-1", "0", "nan", "inf", ""):
        assert metering.RunCeilingPolicy(environ={metering.ENV_RUN_CEILING: bad}).base(100.0) == 25.0


def test_a_grant_is_one_chunk_of_a_quarter_of_the_pool_with_a_floor_of_one_credit():
    policy = metering.RunCeilingPolicy(environ={})
    assert policy.chunk(1690.0) == pytest.approx(422.5) and policy.chunk(2.0) == 1.0
    assert metering.RunCeilingPolicy(chunk_fixed=7.0).chunk(1690.0) == 7.0


def test_the_ceiling_store_round_trips_and_forgets_bad_values():
    redis = FakeGuardRedis()
    store = metering.RunBudgetStore(redis=lambda: redis)
    assert store.ceiling("run-store-0001") is None
    store.set_ceiling("run-store-0001", 12.5)
    assert store.ceiling("run-store-0001") == 12.5
    key = next(iter(redis.data))
    assert key.startswith("run_guard:")                                        # next to run_guard's keys
    redis.data[key] = "not a number"
    assert store.ceiling("run-store-0001") is None
    store.set_ceiling("run-store-0001", 3.0)
    store.restore_ceiling("run-store-0001", None)
    assert store.ceiling("run-store-0001") is None
    for bad in (float("nan"), float("inf"), -1.0, 0.0):
        with pytest.raises(ValueError):
            store.set_ceiling("run-store-0001", bad)


def test_the_ceiling_keys_expire_like_run_guards():
    seen = {}

    class Spy(FakeGuardRedis):
        def set(self, key, value, ex=None):
            seen[key] = ex
            return super().set(key, value, ex=ex)

    metering.RunBudgetStore(redis=lambda: Spy()).set_ceiling("run-store-0002", 5.0)
    assert list(seen.values()) == [metering.CEILING_TTL_SECONDS] and metering.CEILING_TTL_SECONDS > 0


# =============================================================================================
# Wiring and hygiene
# =============================================================================================

def test_the_factory_gives_each_run_a_metering_the_loop_can_use():
    h = Harness(FakeGuardRedis())
    m = h.metering_for()
    assert isinstance(m, Metering)
    assert asyncio.iscoroutinefunction(m.before_call) and asyncio.iscoroutinefunction(m.after_call)


def test_no_content_reaches_the_log_from_a_whole_run(rigs, caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness(real_progress=True, flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=2.0))
    h.ledger.commit_raises = RuntimeError("write failed " + CANARY)
    h.ledger.sum_raises = RuntimeError("read failed " + CANARY)
    rig = rigs(h, priced(CANARY, [tool_call(1)]), priced(CANARY, [tool_call(2)]), priced("x"),
               broker_behaviour=lambda c: {"status": "ok", "output": CANARY})
    go(rig.turn(CANARY))
    text = caplog.text
    assert CANARY not in text
    assert rig.state == "paused_budget"                                        # failed writes still counted: 2 calls, 2 credits
