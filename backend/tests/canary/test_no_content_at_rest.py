"""
backend/tests/canary/test_no_content_at_rest.py -- Patch Q06: the "no content at rest" canary test.

This is the test that backs the product's trust statement (build plan O1): the server keeps no prompt, no file content,
no path, no command and no tool output once a run is over, writes none of them to Postgres, to a log, to an error
report or to a metric, and while a run is open keeps its content only in the encrypted run buffer.

HOW IT WORKS (details in harness.py and README.md next to it)
-------------------------------------------------------------
Every scenario plants fresh random canaries in the prompt, in the project's files, in tool output, in the model's reply,
in tool arguments (a path and a command), and in an instruction file (M2C.md, loaded into run.start).  It then runs one
scripted session end to end in this process: the real server code over a real WebSocket, the real device code in a
temporary project, a recorder at every storage place, the real logging configuration, and the error-reporting SDK
initialised with a transport that keeps what it would send.  Then every place content could rest is scanned for every
canary in every common encoded form (scanners.py).  A hit anywhere fails the test.

The scenarios: a normal finish (and `completed`), a cancel in the middle of a model answer, a disconnect during a tool
call and the resume, a failure of the model, a tool error, a quota pause, and a ledger failure (metering fails open).
A separate test forces error reports with canaries in the exception messages.

Each scenario also asserts that it really exercised its path (the run reached the state it is named for and the canaries
really went through the system: they are on the device's own store, which MAY hold content), so a scenario that quietly
did nothing cannot pass.

Real leaks, if the test finds any, are NOT fixed here.  They are listed in KNOWN_LEAKS with the exact place, and the
scenario that hits one is a STRICT xfail naming it, so CI stays green, the leak stays visible, and fixing it turns the
xfail into a failure that says "remove this entry".  At the time of writing the list is empty.
"""
from __future__ import annotations

import asyncio

import pytest

from codeagent import buffer, run_manager
from codeagent.model_adapter import ModelAdapterError, TextDelta
from tests.canary import harness as h
from tests.canary.scanners import sql_traffic_parts
from tests.unit.loop_testkit import HANG, Reply
from tests.unit.metering_testkit import priced
from utils.llm_errors import ErrorBucket

# {scenario name: "where exactly the leak is, and what it leaks"}.  A scenario listed here is a strict xfail.
KNOWN_LEAKS: dict[str, str] = {}

# FINDING (not a content leak, but a breach of the rule "nothing remains in Redis after a terminal state"): a quota
# pause leaves ``run_guard:m2c_warned:account:<run_id>`` (value "1", TTL 6 h) behind after the run is cancelled.
# Place: codeagent/metering.py, RunBudgetStore.mark_warned() writes it; run_manager._clear_guard() ->
# run_guard.begin_run() does not know the name, and metering.py's own docstring ("REDIS KEYS") says they "expire by
# TTL".  It holds no content and names only a run id, so the content scan stays strict for this scenario; the
# "nothing remains" check is a separate strict xfail (test_no_keys_remain_after_end) and any OTHER leftover key
# still fails the scenario.  Fix = delete the two m2c_* keys when a run ends (run_manager or metering), then delete this.
KNOWN_LEFTOVER_KEYS: dict[str, tuple[str, str]] = {
    "quota_pause": ("run_guard:m2c_warned:", "metering.py RunBudgetStore.mark_warned leaves run_guard:m2c_warned:<scope>:<run_id> "
                    "(value '1', TTL 6 h) after the run ends; run_manager._clear_guard does not clear it"),
}

SCENARIOS = ["finish", "cancel", "disconnect_resume", "model_failure", "tool_error", "quota_pause", "ledger_failure"]


# ---------------------------------------------------------------------------------------------
# The checks every scenario ends with
# ---------------------------------------------------------------------------------------------

def check_server_side(r: h.Result) -> None:
    """Nothing the server may not keep, in any place it could keep it."""
    s = r.canaries.scanner()
    s.assert_clean("server logs", r.server_logs, joined=True)
    s.assert_clean("device logs (the device's real logging configuration)", r.device_logs.splitlines(), joined=True)
    s.assert_clean("recorded SQL: statements and parameters (" + r.mode + " mode)", sql_traffic_parts(r.statements))
    s.assert_clean("usage ledger rows (numbers only)", r.ledger_rows)
    s.assert_clean("run store and device tables", r.twin_tables)
    s.assert_clean("redis: every key and value left after the run ended", r.redis_after, joined=True)
    s.assert_clean("redis: every command argument ever sent, deleted or not", r.redis_written, joined=True)
    s.assert_clean("redis (run guard and metering): every value left", r.guard_after, joined=True)
    s.assert_clean("redis (run guard and metering): every write", r.guard_written, joined=True)
    s.assert_clean("error-reporting payloads (what would have left the process)", r.sentry)
    if r.db_scan is not None:
        hits, counts = r.db_scan
        assert counts, "the database scan found no tables: it looked in the wrong place"
        assert counts.get("public.runs", 0) >= 1, f"the database scan did not see the run's row: {counts}"
        assert not hits, "CONTENT AT REST in Postgres:\n  " + "\n  ".join(str(x) for x in hits[:12])


def check_after_end(r: h.Result, *, allow_prefix: str | None = None) -> None:
    """Nothing may remain in Redis for a run in a terminal state (`allow_prefix`: the one documented exception, which
    has its own strict xfail; a leftover key of any other name still fails here)."""
    for run_id, state in r.run_states.items():
        assert state in ("completed", "cancelled", "failed", "expired"), f"run {run_id} is {state}, not terminal"
    left = [k for k in r.leftover_keys if not (allow_prefix and k.startswith(allow_prefix))]
    assert left == [], f"Redis keys remain for ended runs: {left}"


def check_midrun(mid: dict, canaries: h.Canaries) -> None:
    """While a run is active: content only in the encrypted buffer, and the buffer's values are not readable."""
    assert mid, "no snapshot of Redis was taken while the run was active"
    s = canaries.scanner()
    assert mid["strings"], "the active run left nothing in Redis: the snapshot looked in the wrong place"
    s.assert_clean("redis while active: keys and stored values", mid["strings"], joined=True)
    s.assert_clean("redis while active: every command argument so far", mid["written"], joined=True)
    assert mid["unsealed"] == [], f"unsealed (plaintext) values in the run buffer: {mid['unsealed']}"
    cap = buffer.HARD_CAP_SECONDS
    for key, ttl in mid["ttls"].items():
        assert ttl is not None and 0 < ttl <= cap, f"{key} has ttl {ttl}: every buffer key must expire (cap {cap}s)"


def check_canaries_went_through(r: h.Result, *labels: str) -> None:
    """Proof that the scenario really moved the canaries: the device's own store MAY hold content and here it does.
    Without this a scenario that never sent the content would pass for the wrong reason."""
    s = r.canaries.scanner()
    found = {x.canary for x in s.scan("device store", r.local_store)}
    missing = [lab for lab in labels if lab not in found]
    assert not missing, f"the device's store does not hold the canaries {missing}: the scenario did not run as intended"


# ---------------------------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------------------------

async def run_scenario(stack: h.Stack, name: str) -> h.Result:
    c = stack.c
    async with stack.device() as d:
        d.say(c.prompt.value)
        await until_run(d)
        run_id = d.run_id
        mid = None
        if name == "finish":
            await d.screen_has(c.reply2.token, 40)
            mid = stack.midrun_snapshot()
            await h.until(lambda: d.session._pending_turns == 0, 20, "the turn to end")
            # the product ends a conversation by cancel; `completed` is the other clean ending: take it as well, on a
            # second run, so both cleanup paths are scanned.
            d.interrupt()
            await stack.ended(run_id)
        elif name == "cancel":
            await d.screen_has("edit_file ok", 40)
            mid = stack.midrun_snapshot()
            d.interrupt()
            await stack.ended(run_id)
        elif name == "disconnect_resume":
            await d.screen_has("● bash", 40)
            await h.until(lambda: stack.run_state(run_id) in ("running", "awaiting_approval"), 10, "the call")
            mid = stack.midrun_snapshot()
            d.drop_connection()
            await h.until(lambda: stack.run_state(run_id) == "paused_disconnected", 20, "the pause")
            d.allow_reconnect()
            await h.until(lambda: stack.run_state(run_id) == "running", 30, "the resume")
            await d.screen_has(c.reply2.token, 40)
            mid = stack.midrun_snapshot()
            d.interrupt()
            await stack.ended(run_id)
        elif name == "model_failure":
            await h.until(lambda: stack.run_state(run_id) == "paused_error", 40, "the pause after the failure")
            mid = stack.midrun_snapshot()
            d.interrupt()
            await stack.ended(run_id)
        elif name == "tool_error":
            await d.screen_has(c.reply2.token, 40)
            mid = stack.midrun_snapshot()
            d.interrupt()
            await stack.ended(run_id)
        elif name == "quota_pause":
            await h.until(lambda: stack.run_state(run_id) == "paused_budget", 40, "the quota pause")
            mid = stack.midrun_snapshot()
            d.interrupt()
            await stack.ended(run_id)
        elif name == "ledger_failure":
            await d.screen_has(c.reply2.token, 40)
            mid = stack.midrun_snapshot()
            d.interrupt()
            await stack.ended(run_id)
        run_ids = [run_id]
        if name == "finish":
            run_ids.append(await second_run_completed(stack, d))
        result = stack.collect(d, run_ids, mid)
    return result


async def until_run(d: h.Device) -> None:
    await h.until(lambda: d.run_id is not None, 20, "the run to start")


async def second_run_completed(stack: h.Stack, d: h.Device) -> str:
    """A second conversation on the same device that the server ends as `completed` (run_manager.end_run, the other
    clean terminal state), so that path's cleanup is scanned too."""
    c = stack.c
    stack.adapter.steps.append(priced(f"second run {c.reply.value}"))
    d.say(f"again {c.prompt.value}")
    await until_run(d)
    run_id = d.run_id
    await d.screen_has("second run", 30)
    await h.until(lambda: d.session._pending_turns == 0, 20, "the second turn to end")
    await asyncio.to_thread(run_manager.end_run, stack.user_id, run_id, "completed")
    await h.until(lambda: stack.run_state(run_id) == "completed", 10, "completed")
    return run_id


def script_for(name: str, c: h.Canaries):
    if name in ("finish", "ledger_failure"):
        return h.full_script(c)
    if name == "cancel":
        return [priced(f"Let me look. {c.reply.value}", [h.read_call(c)]),
                priced("", [h.edit_call(c)]),
                Reply(events=[TextDelta(f"Still thinking {c.reply2.value}"), HANG])]
    if name == "disconnect_resume":
        slow = f"sleep 2; echo {c.command.value}; cat tool_output.txt"
        return [priced(f"Let me look. {c.reply.value}", [h.read_call(c)]),
                priced("", [h.bash_call(c, command=slow)]),
                priced(f"Back again. {c.reply2.value}"),
                priced(f"Back again. {c.reply2.value}")]
    if name == "model_failure":
        failure = ModelAdapterError(ErrorBucket.TRANSIENT_NETWORK, provider="scripted", model="model-strong")
        failure.__cause__ = RuntimeError(f"upstream echoed the request: {c.prompt.value} {c.fault.value}")
        return [priced(f"Let me look. {c.reply.value}", [h.read_call(c)]), failure]
    if name == "tool_error":
        missing = f"missing_{c.path.token}.txt"
        fail_cmd = f"echo {c.tool_output.value} >&2; echo {c.command.value}; exit 3"
        return [priced(f"Let me look. {c.reply.value}", [h.call("read_file", {"path": missing}, call_id="prov_missing")]),
                priced("", [h.bash_call(c, command=fail_cmd)]),
                priced(f"It failed. {c.reply2.value}")]
    if name == "quota_pause":
        return [priced("never reached")]
    raise AssertionError(name)


@pytest.fixture
def stack_factory(monkeypatch, tmp_path):
    made = []

    def make(name: str) -> h.Stack:
        canaries = h.Canaries()                         # the script and the stack must share them
        kw: dict = {}
        if name == "quota_pause":
            kw["quota"] = True
        if name == "ledger_failure":
            kw["ledger_faults"] = RuntimeError("ledger down " + canaries.fault.value)
        stack = h.Stack(monkeypatch, tmp_path, canaries=canaries, script=script_for(name, canaries), **kw)
        made.append(stack)
        return stack

    yield make
    for stack in made:
        stack.close()


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_content_at_rest(name, stack_factory):
    if name in KNOWN_LEAKS:
        pytest.xfail(KNOWN_LEAKS[name])
    stack = stack_factory(name)
    result = asyncio.run(run_scenario(stack, name))

    assert result.run_states[result.run_ids[0]] == "cancelled"      # the person ended the session
    check_after_end(result, allow_prefix=KNOWN_LEFTOVER_KEYS.get(name, (None, ""))[0])
    check_midrun(result.midrun, result.canaries)
    check_server_side(result)
    if name == "finish":
        check_canaries_went_through(result, "prompt", "path", "filetext", "toolout", "reply", "replytwo", "command",
                                    "edit")
        assert result.run_states[result.run_ids[1]] == "completed"
        assert result.mode in ("fake", "real")
    elif name == "tool_error":
        check_canaries_went_through(result, "prompt", "toolout", "command", "replytwo")
    elif name == "model_failure":
        check_canaries_went_through(result, "prompt", "reply")
    elif name == "quota_pause":
        check_canaries_went_through(result, "prompt")


@pytest.mark.parametrize("name", sorted(KNOWN_LEFTOVER_KEYS))
@pytest.mark.xfail(strict=True, reason="recorded finding: metering's run_guard:m2c_warned:* key outlives the run "
                                       "(see KNOWN_LEFTOVER_KEYS); fixing it makes this XPASS, which fails: remove the entry")
def test_no_keys_remain_after_end(name, stack_factory):
    stack = stack_factory(name)
    result = asyncio.run(run_scenario(stack, name))
    assert result.leftover_keys == [], f"{KNOWN_LEFTOVER_KEYS[name][1]}: {result.leftover_keys}"


# ---------------------------------------------------------------------------------------------
# Error reports
# ---------------------------------------------------------------------------------------------

def test_error_reports_carry_no_content(stack_factory):
    """Force every error path that puts a canary in an exception message (a model adapter bug, a ledger failure, a
    broken tool call) and require (a) that the error-reporting SDK really captured error events, so the check is not
    vacuous, and (b) that nothing it would send carries a canary: not the event, not its breadcrumbs, not its extras."""
    stack = stack_factory("ledger_failure")
    c = stack.c
    stack.adapter.steps[:] = [                                       # one good call (its ledger write fails: an ERROR
        priced(f"Let me look. {c.reply.value}", [h.read_call(c)]),   # log, so an error report), then a bare exception
        RuntimeError(f"adapter bug with {c.prompt.value} and {c.fault.value}")]   # (an adapter bug)

    async def go():
        async with stack.device() as d:
            d.say(c.prompt.value)
            await until_run(d)
            run_id = d.run_id
            await h.until(lambda: stack.run_state(run_id) in ("paused_error", "paused_budget"), 40, "the pause")
            await asyncio.sleep(0.3)
            d.interrupt()
            await stack.ended(run_id)
            return stack.collect(d, [run_id])

    result = asyncio.run(go())
    assert stack.sentry.error_events > 0, "no error event was captured: the forced error path did not reach the SDK"
    result.canaries.scanner().assert_clean("error-reporting payloads", result.sentry)
    check_server_side(result)
