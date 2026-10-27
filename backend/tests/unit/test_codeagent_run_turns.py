"""S14d: the `user_message` turn queue and the `cancel` handler (codeagent/run_handlers.py).

Same setup as the S14c tests (tests/unit/test_codeagent_run_handlers.py): in-memory twins behind
the real interfaces (run store, encrypted Redis buffer, run_guard's Redis; run_manager_testkit.World),
the REAL protocol/v1 validator, a stand-in connection for the unit tests, and S04's real endpoint
(codeagent_testkit.Rig) with the real MessageRouter for the socket tests. A turn runs as a task, so
every unit test that needs one drives the handler and the assertions inside ONE event loop.

Maps to the work package ("What" 3, bullets 2 and 3):
  * user_message goes to the registered turn runner; with none installed the device gets
    `internal` with a fixed text ........................................... test_user_message_no_runner_*
  * turns on one run happen one at a time, in order; more queue up to a cap, beyond which the
    device gets rate_limited ................................................ test_user_message_*queue*
  * the ownership guard first: a run of another device or user is run_not_found, and a missing
    runner is never revealed for it ......................................... test_*_guard_*, test_socket_*invisible*
  * cancel calls the on_cancel hooks in order, then run_manager.cancel_run (cancel flag, state,
    buffer deleted), then announces the end ................................. test_cancel_*
  * a CleanupIncomplete means the run is over: success to the device, purge retried later
                                                                              test_cancel_cleanup_*
  * the final event has no buffer to number it from: it is numbered by the emitter's tracker and
    sent live only ......................................................... test_cancel_announces_*
  * disconnect and cancel stop a running turn and drop the queued ones ......... test_*_stops_turns_*
  * no content in the log ..................................................... test_no_content_*
"""
import asyncio
import logging
import threading
import time

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed; install protocol/requirements.txt to run the "
           "device-socket tests (see codeagent/ws.py, 'Runtime needs')",
)

from codeagent import buffer, connections, router as router_module, run_handlers, run_manager, runs   # noqa: E402
from codeagent import ws as ws_module                                                                  # noqa: E402
from codeagent.connections import ConnectionClosed                                                     # noqa: E402
from codeagent.emitter import RunEmitter                                                               # noqa: E402
from codeagent.router import MessageRouter                                                             # noqa: E402
from codeagent.run_handlers import RunHandlers                                                         # noqa: E402
from eo import run_guard                                                                               # noqa: E402
from tests.unit import runs_testkit as rt                                                              # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope                                                 # noqa: E402
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET                                         # noqa: E402
from tests.unit.run_manager_testkit import (                                                           # noqa: E402
    CANARY, DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, history, start_payload,
)

DELTA = {"type": "text_delta", "text": "x"}


def go(coro):
    return asyncio.run(coro)


@pytest.fixture
def w(monkeypatch):
    world = World(monkeypatch)
    try:
        run_manager._protocol()
    except run_manager.ProtocolSchemasUnavailable:
        pytest.skip("protocol/ or jsonschema is not available in this layout")
    yield world
    world.teardown()


@pytest.fixture
def registry(monkeypatch):
    reg = connections.InMemoryRegistry()
    monkeypatch.setattr(connections, "_REGISTRY", reg)
    return reg


class StubConn:
    def __init__(self, user_id=USER, device_id=DEVICE):
        self.user_id = user_id
        self.device_id = device_id
        self.sent = []
        self.errors = []
        self.closed = False

    async def send_json(self, env):
        if self.closed:
            raise ConnectionClosed("connection closed")
        self.sent.append(env)

    async def send_error(self, code, message, *, ref_id=None, details=None):
        if self.closed:
            raise ConnectionClosed("connection closed")
        self.errors.append({"code": code, "message": message, "ref_id": ref_id, "details": details})


def make(**kw):
    kw.setdefault("max_parallel", 10)
    kw.setdefault("hook_timeout", 2.0)
    kw.setdefault("stop_timeout", 2.0)
    kw.setdefault("purge_delays", (0.01,))
    return RunHandlers(emitter=RunEmitter(), **kw)


@pytest.fixture
def h(w, registry):
    return make()


def online(registry, user=USER, device=DEVICE):
    conn = StubConn(user, device)
    registry.add(conn)
    return conn


def check_valid(env):
    issues = run_manager._protocol().validate_message(env, tolerant=False)
    assert not issues, [(i.path, i.validator) for i in issues]


def um(run_id, text="hi"):
    return envelope("user_message", {"text": text}, run_id=run_id)


def cancel_msg(run_id):
    return envelope("cancel", {}, run_id=run_id)


async def begin(h, conn):
    """A started run (seq 1 created, seq 2 running); returns its id."""
    msg = envelope("run.start", start_payload(history(1)), run_id=rt.new_run_id())
    await h.on_run_start(conn, msg)
    assert conn.errors == [], conn.errors
    return msg["run_id"]


def stored(run_id, after=0):
    return buffer.replay_after(run_id, after)


def state_of(w, run_id):
    return w.store.rows[run_id]["state"]


def nothing_left(w, run_id):
    assert w.redis.keys() == [] and w.redis.all_strings() == []
    assert not [k for k in w.guard.data if run_id in k]
    assert run_guard.cancel_reason(run_id) is None


class Runner:
    """A turn runner that records what it saw. `gates[text]` (an asyncio.Event, made inside the
    loop) holds the turn open; `boom[text]` makes it raise."""

    def __init__(self):
        self.calls, self.finished, self.cancelled, self.seen_runs = [], [], [], []
        self.active = 0
        self.max_active = 0
        self.gates = {}
        self.boom = {}

    async def __call__(self, conn, run, msg):
        text = msg["payload"]["text"]
        self.calls.append(text)
        self.seen_runs.append(run)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            gate = self.gates.get(text)
            if gate is not None:
                await gate.wait()
            else:
                await asyncio.sleep(0)
            if text in self.boom:
                raise self.boom[text]
            self.finished.append(text)
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise
        finally:
            self.active -= 1


# ============================================================ user_message

def test_user_message_no_runner_answers_internal_with_a_fixed_text(h, w, registry):
    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        msg = um(run_id, CANARY)
        await h.on_user_message(conn, msg)
        return conn, msg, run_id

    conn, msg, run_id = go(scenario())
    assert conn.errors == [{"code": "internal", "message": run_handlers.MSG_NO_TURN_RUNNER,
                            "ref_id": msg["id"], "details": None}]
    assert CANARY not in repr(conn.errors)
    assert h.turn_runs() == ()


def test_user_message_guard_a_foreign_run_is_run_not_found_and_never_reveals_the_missing_runner(h, w, registry):
    """The runner is not installed here, yet a device that does not own the run must not
    learn anything about it: not even the `internal` of a server with no runner."""
    async def scenario():
        mine = online(registry)
        run_id = await begin(h, mine)
        sibling, stranger = online(registry, USER, OTHER_DEVICE), online(registry, OTHER_USER, DEVICE)
        for conn in (sibling, stranger):
            await h.on_user_message(conn, um(run_id))
        await h.on_user_message(sibling, um(rt.new_run_id()))
        return mine, sibling, stranger

    mine, sibling, stranger = go(scenario())
    assert [e["code"] for e in sibling.errors] == ["run_not_found", "run_not_found"]
    assert [e["code"] for e in stranger.errors] == ["run_not_found"]
    assert mine.errors == []


def test_user_message_guard_a_foreign_run_never_reaches_the_runner(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        run_id = await begin(h, online(registry))
        for conn in (online(registry, USER, OTHER_DEVICE), online(registry, OTHER_USER, DEVICE)):
            await h.on_user_message(conn, um(run_id))
        await h.wait_idle(run_id)

    go(scenario())
    assert runner.calls == []


def test_user_message_goes_to_the_runner_with_the_connection_the_current_run_and_the_message(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        msg = um(run_id, "hello")
        await h.on_user_message(conn, msg)
        await h.wait_idle(run_id)
        return conn, run_id, msg

    conn, run_id, msg = go(scenario())
    assert runner.calls == ["hello"] and runner.finished == ["hello"] and conn.errors == []
    assert runner.seen_runs[0].id == run_id and runner.seen_runs[0].state == "running"
    assert h.turn_runs() == ()                                  # nothing is kept for an idle run


def test_user_message_the_handler_returns_before_the_turn_is_over(h, w, registry):
    """ws.py awaits the handler inside the receive loop: a turn must run as a task."""
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await asyncio.wait_for(h.on_user_message(conn, um(run_id, "a")), 1)    # returns while "a" is open
        await asyncio.sleep(0.01)
        assert runner.calls == ["a"] and runner.finished == [] and h.turn_active(run_id)
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        assert runner.finished == ["a"] and not h.turn_active(run_id)

    go(scenario())


def test_user_message_turns_on_one_run_are_one_at_a_time_in_arrival_order(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        for text in ("one", "two", "three", "four", "five"):
            await h.on_user_message(conn, um(run_id, text))
        await h.wait_idle(run_id)
        return conn

    conn = go(scenario())
    assert runner.calls == ["one", "two", "three", "four", "five"] == runner.finished
    assert runner.max_active == 1                               # never two at once
    assert conn.errors == []


def test_user_message_a_queued_turn_waits_for_the_one_before_it(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(conn, um(run_id, "a"))
        await h.on_user_message(conn, um(run_id, "b"))
        await asyncio.sleep(0.02)
        assert runner.calls == ["a"] and h.pending_turns(run_id) == 1          # b is waiting, not running
        runner.gates["a"].set()
        await h.wait_idle(run_id)

    go(scenario())
    assert runner.calls == ["a", "b"]


def test_user_message_beyond_the_cap_the_device_gets_rate_limited(w, registry):
    h = make(max_queued_turns=2)
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        msgs = {t: um(run_id, t) for t in "abcde"}
        for t in "abcde":
            await h.on_user_message(conn, msgs[t])
        assert h.pending_turns(run_id) == 2                     # b and c wait; a runs; d and e were refused
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        return conn, msgs

    conn, msgs = go(scenario())
    assert runner.calls == ["a", "b", "c"]                      # d and e were never queued
    assert [(e["code"], e["ref_id"]) for e in conn.errors] == [("rate_limited", msgs["d"]["id"]),
                                                               ("rate_limited", msgs["e"]["id"])]
    assert conn.errors[0]["details"] == {"retry_after_s": run_handlers.RETRY_AFTER_SECONDS}
    assert CANARY not in repr(conn.errors)


def test_user_message_the_cap_counts_waiting_messages_not_the_running_one(w, registry):
    h = make(max_queued_turns=1)
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        for t in "abc":
            await h.on_user_message(conn, um(run_id, t))        # a runs, b waits, c is one too many
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        return conn

    conn = go(scenario())
    assert runner.calls == ["a", "b"] and [e["code"] for e in conn.errors] == ["rate_limited"]


def test_user_message_a_cap_of_zero_allows_one_turn_and_no_queue(w, registry):
    h = make(max_queued_turns=0)
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(conn, um(run_id, "a"))
        await h.on_user_message(conn, um(run_id, "b"))
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        return conn

    conn = go(scenario())
    assert runner.calls == ["a"] and [e["code"] for e in conn.errors] == ["rate_limited"]


def test_user_message_after_the_queue_drains_new_messages_are_accepted_again(w, registry):
    h = make(max_queued_turns=1)
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        for t in "abc":
            await h.on_user_message(conn, um(run_id, t))
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        await h.on_user_message(conn, um(run_id, "d"))
        await h.on_user_message(conn, um(run_id, "e"))
        await h.wait_idle(run_id)
        return conn

    conn = go(scenario())
    assert runner.calls == ["a", "b", "d", "e"] and [e["code"] for e in conn.errors] == ["rate_limited"]


def test_user_message_the_default_cap_is_the_module_constant_and_can_be_injected(monkeypatch):
    assert RunHandlers().max_queued_turns == run_handlers.MAX_QUEUED_TURNS
    assert 1 <= run_handlers.MAX_QUEUED_TURNS <= 20             # "a small cap"
    assert RunHandlers(max_queued_turns=7).max_queued_turns == 7
    monkeypatch.setattr(run_handlers, "MAX_QUEUED_TURNS", 4)
    assert RunHandlers().max_queued_turns == 4


@pytest.mark.parametrize("bad", [-1, True, "2", 1.5])
def test_user_message_a_bad_cap_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        RunHandlers(max_queued_turns=bad)


def test_user_message_runs_are_independent_one_runs_turn_does_not_hold_up_another(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        first, second = await begin(h, conn), await begin(h, conn)
        runner.gates["slow"] = asyncio.Event()
        await h.on_user_message(conn, um(first, "slow"))
        await h.on_user_message(conn, um(second, "fast"))
        await asyncio.wait_for(h.wait_idle(second), 2)          # not behind the first run's open turn
        assert runner.finished == ["fast"] and h.turn_active(first)
        runner.gates["slow"].set()
        await h.wait_idle(first)

    go(scenario())
    assert runner.finished == ["fast", "slow"]


def test_user_message_each_run_has_its_own_queue_cap(w, registry):
    h = make(max_queued_turns=1)
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        first, second = await begin(h, conn), await begin(h, conn)
        runner.gates["x"] = asyncio.Event()
        for run_id in (first, second):
            await h.on_user_message(conn, um(run_id, "x"))
            await h.on_user_message(conn, um(run_id, "y"))      # waits behind x, on its own run
        runner.gates["x"].set()
        await h.wait_idle(first)
        await h.wait_idle(second)
        return conn

    assert go(scenario()).errors == []


@pytest.mark.parametrize("state", ["created", "paused_budget", "paused_error", "paused_disconnected"])
def test_user_message_a_run_that_is_not_running_is_invalid_message(h, w, registry, state):
    runner = Runner()
    h.set_turn_runner(runner)
    conn = online(registry)
    run = w.run_at(state, user=USER, device=DEVICE)

    async def scenario():
        await h.on_user_message(conn, um(run.id))
        answered_by_the_handler = [e["code"] for e in conn.errors], h.turn_runs()   # before any worker could run
        await h.wait_idle(run.id)
        return answered_by_the_handler

    assert go(scenario()) == (["invalid_message"], ())          # refused at once, nothing queued
    assert [e["code"] for e in conn.errors] == ["invalid_message"] and runner.calls == []


def test_user_message_an_awaiting_approval_run_takes_the_message_it_is_a_running_turn(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)
    conn = online(registry)
    run = w.run_at("awaiting_approval", user=USER, device=DEVICE)

    async def scenario():
        await h.on_user_message(conn, um(run.id, "q"))
        await h.wait_idle(run.id)

    go(scenario())
    assert runner.calls == ["q"] and conn.errors == []


def test_user_message_an_ended_run_reads_as_missing(h, w, registry):
    h.set_turn_runner(Runner())

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        run_manager.end_run(USER, run_id, "completed")
        await h.on_user_message(conn, um(run_id))
        return conn

    assert [e["code"] for e in go(scenario()).errors] == ["run_not_found"]


def test_user_message_a_turn_that_raises_is_answered_internal_and_the_next_one_still_runs(h, w, registry):
    runner = Runner()
    runner.boom["bad"] = RuntimeError(CANARY)
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        bad, good = um(run_id, "bad"), um(run_id, "good")
        await h.on_user_message(conn, bad)
        await h.on_user_message(conn, good)
        await h.wait_idle(run_id)
        return conn, bad, run_id

    conn, bad, run_id = go(scenario())
    assert conn.errors == [{"code": "internal", "message": ws_module.MSG_INTERNAL,
                            "ref_id": bad["id"], "details": None}]
    assert runner.finished == ["good"] and CANARY not in repr(conn.errors)
    assert state_of(w, run_id) == "running"                     # the runner owns the run's state, not this handler


def test_user_message_a_turn_whose_device_left_is_not_an_error(h, w, registry):
    runner = Runner()
    runner.boom["bad"] = RuntimeError("x")
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        await h.on_user_message(conn, um(run_id, "bad"))
        conn.closed = True                                      # the answer cannot be delivered
        await h.wait_idle(run_id)

    go(scenario())                                              # no exception escapes the task


def test_user_message_the_runner_is_read_at_turn_time_and_a_removed_one_is_answered_internal(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(conn, um(run_id, "a"))
        queued = um(run_id, "b")
        await h.on_user_message(conn, queued)
        await asyncio.sleep(0.01)                               # a is open in the runner now
        h.set_turn_runner(None)                                 # uninstalled while b waits
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        return conn, queued

    conn, queued = go(scenario())
    assert runner.calls == ["a"]
    assert conn.errors == [{"code": "internal", "message": run_handlers.MSG_NO_TURN_RUNNER,
                            "ref_id": queued["id"], "details": None}]


def test_user_message_a_queued_turn_whose_run_was_paused_meanwhile_does_not_run(h, w, registry):
    """What a budget pause in the middle of a queue looks like: the queued messages must not
    call the model for a run that stopped."""
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(conn, um(run_id, "a"))
        queued = um(run_id, "b")
        await h.on_user_message(conn, queued)
        await asyncio.sleep(0.01)                               # a is open in the runner now
        run_manager.pause_run(USER, run_id, "paused_budget")
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        return conn, queued

    conn, queued = go(scenario())
    assert runner.calls == ["a"]
    assert [(e["code"], e["ref_id"]) for e in conn.errors] == [("invalid_message", queued["id"])]


def test_user_message_a_queued_turn_whose_run_ended_meanwhile_is_answered_run_not_found(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(conn, um(run_id, "a"))
        queued = um(run_id, "b")
        await h.on_user_message(conn, queued)
        await asyncio.sleep(0.01)                               # a is open in the runner now
        run_manager.end_run(USER, run_id, "completed")
        runner.gates["a"].set()
        await h.wait_idle(run_id)
        return conn, queued

    conn, queued = go(scenario())
    assert runner.calls == ["a"]
    assert [(e["code"], e["ref_id"]) for e in conn.errors] == [("run_not_found", queued["id"])]


# ---------------------------------------------------------------- stopping turns

def test_stop_turns_cancels_the_running_turn_and_drops_the_queue(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        for t in "abc":
            await h.on_user_message(conn, um(run_id, t))
        await asyncio.sleep(0.01)
        dropped = await h.stop_turns(run_id)
        assert dropped == 2 and h.turn_runs() == ()
        await asyncio.sleep(0.02)
        return conn

    go(scenario())
    assert runner.calls == ["a"] and runner.cancelled == ["a"]  # b and c never started


def test_stop_turns_a_runner_that_swallows_the_cancellation_still_does_not_start_the_queue(w, registry):
    """The queue is dropped by stop_turns itself, not left to the cancelled worker: a turn that
    ignores its cancellation and returns normally must not be followed by the waiting messages."""
    h = make(stop_timeout=0.05)
    calls = []

    async def stubborn(conn, run, msg):
        calls.append(msg["payload"]["text"])
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)                           # swallows it and returns normally

    h.set_turn_runner(stubborn)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        for t in "abc":
            await h.on_user_message(conn, um(run_id, t))
        await asyncio.sleep(0.01)
        await h.stop_turns(run_id)
        await asyncio.sleep(0.3)

    go(scenario())
    assert calls == ["a"]


def test_stop_turns_on_an_idle_run_is_a_no_op(h, w, registry):
    assert go(h.stop_turns("run-aaaaaaaa")) == 0


def test_stop_turns_a_new_message_afterwards_starts_a_fresh_worker(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(conn, um(run_id, "a"))
        await asyncio.sleep(0.01)
        await h.stop_turns(run_id)
        await h.on_user_message(conn, um(run_id, "z"))
        await h.wait_idle(run_id)

    go(scenario())
    assert runner.calls == ["a", "z"] and runner.finished == ["z"]


def test_stop_turns_a_runner_that_ignores_cancellation_does_not_hang_the_caller(w, registry):
    h = make(stop_timeout=0.05)

    async def stubborn(conn, run, msg):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)                            # carries on for a while

    h.set_turn_runner(stubborn)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        await h.on_user_message(conn, um(run_id, "a"))
        await asyncio.sleep(0.01)
        started = time.monotonic()
        await h.stop_turns(run_id)
        return time.monotonic() - started

    assert go(scenario()) < 0.35


# ----------------------------------------------------- disconnect stops the turns

def leave(registry, conn):
    registry.remove(conn)
    conn.closed = True


def test_disconnect_stops_the_running_turn_and_drops_the_queue_before_the_hooks_and_the_pause(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)
    seen = []

    async def hook(c, run):
        seen.append((runner.active, h.pending_turns(run.id), h.turn_active(run.id), state_of(w, run.id)))

    h.add_on_run_disconnect(hook)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        for t in "abc":
            await h.on_user_message(conn, um(run_id, t))
        await asyncio.sleep(0.01)
        leave(registry, conn)
        await h.on_disconnect(conn)
        await asyncio.sleep(0.02)
        return run_id

    run_id = go(scenario())
    assert seen == [(0, 0, False, "running")]                   # stopped before the hook; not yet paused
    assert runner.calls == ["a"] and runner.cancelled == ["a"]
    assert state_of(w, run_id) == "paused_disconnected"


def test_disconnect_of_a_superseded_connection_leaves_the_turns_running(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)

    async def scenario():
        old = online(registry)
        run_id = await begin(h, old)
        runner.gates["a"] = asyncio.Event()
        await h.on_user_message(old, um(run_id, "a"))
        await h.on_user_message(old, um(run_id, "b"))
        await asyncio.sleep(0.01)
        registry.add(StubConn())                                # the device came back
        old.closed = True
        await h.on_disconnect(old)
        assert h.turn_active(run_id) and h.pending_turns(run_id) == 1 and runner.cancelled == []
        runner.gates["a"].set()
        await h.wait_idle(run_id)

    go(scenario())
    assert runner.finished == ["a", "b"]


# ================================================================== cancel

def started_cancel(h, w, registry, *, deltas=3, **conn_kw):
    """A started run with `deltas` more events: returns (conn, run_id, last seq)."""
    async def scenario():
        conn = online(registry, **conn_kw)
        run_id = await begin(h, conn)
        for _ in range(deltas):
            await h.emitter.emit(run_id, "run_event", DELTA)
        return conn, run_id

    conn, run_id = go(scenario())
    return conn, run_id, 2 + deltas


def test_cancel_cancels_the_run_deletes_the_buffer_and_leaves_nothing_behind(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    w.guard_seed(run_id)                                        # every run_guard rail exists
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert state_of(w, run_id) == "cancelled" and conn.errors == []
    nothing_left(w, run_id)


def test_cancel_announces_the_end_with_the_next_seq_live_only(h, w, registry):
    """The buffer is deleted by cancel_run, and next_seq needs the buffer: the last event is
    numbered by the emitter's tracker, sent, and not stored."""
    conn, run_id, last = started_cancel(h, w, registry, deltas=3)
    msg = cancel_msg(run_id)
    go(h.on_cancel_request(conn, msg))
    final = conn.sent[-1]
    assert final["t"] == "run_event" and final["seq"] == last + 1 == 6 and final["run_id"] == run_id
    assert final["payload"] == {"type": "state_changed", "state": "cancelled", "reason": run_handlers.CANCEL_REASON}
    check_valid(final)
    assert [e["seq"] for e in conn.sent] == [1, 2, 3, 4, 5, 6]  # contiguous: the device sees no gap
    assert w.redis.keys() == []                                 # and nothing was stored or brought back
    assert not h.emitter.is_bound(run_id)                       # the run is released


def test_cancel_runs_the_hooks_in_order_before_the_run_is_cancelled(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    seen = []

    async def first(c, run):
        seen.append(("first", c, run.id, state_of(w, run_id), buffer.exists(run_id),
                     run_guard.cancel_reason(run_id)))

    async def second(c, run):
        await asyncio.sleep(0)                                  # yielding must not let the cancel overtake it
        seen.append(("second", c, run.id, state_of(w, run_id), buffer.exists(run_id),
                     run_guard.cancel_reason(run_id)))

    h.add_on_cancel(first)
    h.add_on_cancel(second)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert seen == [("first", conn, run_id, "running", True, None),
                    ("second", conn, run_id, "running", True, None)]      # nothing had happened yet
    assert state_of(w, run_id) == "cancelled"


def test_cancel_a_hook_may_emit_and_its_event_comes_before_the_end(h, w, registry):
    """What the tool broker's cancel_all will do: close open calls with tool_finished
    `cancelled`, through the emitter, while the handler is NOT holding the run's lock."""
    conn, run_id, last = started_cancel(h, w, registry, deltas=1)
    done = []

    async def broker(c, run):
        await h.emitter.emit(run.id, "run_event", {"type": "tool_finished", "call_id": "call_x",
                                                    "tool": "grep", "status": "cancelled"})
        done.append(True)

    h.add_on_cancel(broker)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert done == [True]                                       # no deadlock on the run's lock
    assert [(e["seq"], e["payload"]["type"]) for e in conn.sent[-2:]] == [(last + 1, "tool_finished"),
                                                                           (last + 2, "state_changed")]


def test_cancel_a_failing_hook_does_not_stop_the_cancel_or_the_other_hooks(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    called = []

    async def bad(c, run):
        raise RuntimeError(CANARY)

    async def good(c, run):
        called.append(run.id)

    h.add_on_cancel(bad)
    h.add_on_cancel(good)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert called == [run_id] and state_of(w, run_id) == "cancelled" and conn.errors == []


def test_cancel_a_hook_that_never_returns_is_given_up_on(w, registry):
    h = make(hook_timeout=0.05)
    conn, run_id, _ = started_cancel(h, w, registry)

    async def stuck(c, run):
        await asyncio.sleep(30)

    h.add_on_cancel(stuck)
    started = time.monotonic()
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert time.monotonic() - started < 5 and state_of(w, run_id) == "cancelled"


def test_cancel_stops_the_turns_after_the_hooks_and_before_the_run_is_cancelled(h, w, registry):
    runner = Runner()
    h.set_turn_runner(runner)
    order = []

    async def hook(c, run):
        order.append(("hook", runner.active))

    h.add_on_cancel(hook)

    def before_move(call):
        if call["to_state"] == "cancelled":
            order.append(("cancel_run", runner.active, h.turn_active(call["run_id"])))

    w.store.before_move = before_move

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runner.gates["a"] = asyncio.Event()
        for t in "abc":
            await h.on_user_message(conn, um(run_id, t))
        await asyncio.sleep(0.01)
        await h.on_cancel_request(conn, cancel_msg(run_id))
        await asyncio.sleep(0.02)
        return conn, run_id

    conn, run_id = go(scenario())
    assert order == [("hook", 1), ("cancel_run", 0, False)]     # the hook saw it running; the cancel did not
    assert runner.calls == ["a"] and runner.cancelled == ["a"]  # b and c were dropped, never started
    assert state_of(w, run_id) == "cancelled" and h.turn_runs() == ()


def test_cancel_guard_a_foreign_run_is_run_not_found_and_nothing_happens(h, w, registry):
    owner, run_id, _ = started_cancel(h, w, registry)
    hooked = []

    async def hook(c, run):
        hooked.append(run.id)

    h.add_on_cancel(hook)
    sibling, stranger = online(registry, USER, OTHER_DEVICE), online(registry, OTHER_USER, DEVICE)
    for conn in (sibling, stranger):
        go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert [e["code"] for e in sibling.errors] == ["run_not_found"] == [e["code"] for e in stranger.errors]
    assert hooked == [] and state_of(w, run_id) == "running" and buffer.exists(run_id)
    assert run_guard.cancel_reason(run_id) is None              # no flag was set for someone else's run
    assert h.emitter.is_bound(run_id) and len(stored(run_id)) == 5


def test_cancel_a_missing_or_ended_run_is_run_not_found(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    go(h.on_cancel_request(conn, cancel_msg(rt.new_run_id())))
    go(h.on_cancel_request(conn, cancel_msg(run_id)))           # a real cancel
    go(h.on_cancel_request(conn, cancel_msg(run_id)))           # the same again: the run is over
    assert [e["code"] for e in conn.errors] == ["run_not_found", "run_not_found"]


def test_cancel_then_user_message_is_run_not_found(h, w, registry):
    h.set_turn_runner(Runner())
    conn, run_id, _ = started_cancel(h, w, registry)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    go(h.on_user_message(conn, um(run_id)))
    assert [e["code"] for e in conn.errors] == ["run_not_found"]


def test_cancel_only_touches_its_own_run(h, w, registry):
    async def scenario():
        conn = online(registry)
        first, second = await begin(h, conn), await begin(h, conn)
        other = online(registry, USER, OTHER_DEVICE)
        theirs = await begin(h, other)
        await h.on_cancel_request(conn, cancel_msg(first))
        return first, second, theirs

    first, second, theirs = go(scenario())
    assert [state_of(w, i) for i in (first, second, theirs)] == ["cancelled", "running", "running"]
    assert buffer.exists(second) and buffer.exists(theirs) and not buffer.exists(first)


def test_cancel_a_paused_run_that_nothing_in_this_process_knows(w, registry):
    """A run paused by a disconnect is unbound; after a restart nothing remembers it either.
    The final event's seq must still continue the stream: it is found from the replay."""
    first = make()

    async def scenario():
        conn = online(registry)
        run_id = await begin(first, conn)
        await first.emitter.emit(run_id, "run_event", DELTA)                   # seq 3
        leave(registry, conn)
        await first.on_disconnect(conn)                                        # seq 4: paused_disconnected
        return run_id

    run_id = go(scenario())
    assert state_of(w, run_id) == "paused_disconnected" and not first.emitter.is_bound(run_id)
    restarted = make()                                                         # a fresh process
    back = online(registry)
    go(restarted.on_cancel_request(back, cancel_msg(run_id)))
    assert back.errors == [] and state_of(w, run_id) == "cancelled"
    assert [e["seq"] for e in back.sent] == [5]                                # 1..4 were stored; this is 5
    assert back.sent[0]["payload"]["state"] == "cancelled"
    assert w.redis.keys() == []


def test_cancel_with_the_buffer_gone_still_cancels_and_announces_nothing_it_cannot_number(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    buffer.delete_buffer(run_id)                                # idle TTL ran out
    h.emitter.unbind(run_id)                                    # and nothing is bound either
    sent_before = len(conn.sent)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert state_of(w, run_id) == "cancelled" and conn.errors == []
    assert len(conn.sent) == sent_before and not h.emitter.is_bound(run_id)


def test_cancel_when_the_device_is_offline_is_not_an_error(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    registry.remove(conn)                                       # the answer has nowhere to go
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert state_of(w, run_id) == "cancelled"


def test_cancel_an_ack_is_the_event_never_an_error(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert conn.errors == [] and conn.sent[-1]["payload"]["state"] == "cancelled"


# ---- cancel: the failures ----

def test_cancel_a_run_that_ends_under_it_is_run_not_found_and_leaves_no_flag(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)

    def somebody_completes(call):
        if call["to_state"] == "cancelled":
            call["store"].force_state(run_id, "completed")

    w.store.before_move = somebody_completes
    sent_before = len(conn.sent)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert [e["code"] for e in conn.errors] == ["run_not_found"]
    assert len(conn.sent) == sent_before                        # no `cancelled` announced for a completed run
    assert run_guard.cancel_reason(run_id) is None              # cancel_run cleared the flag it had set


def test_cancel_a_store_outage_is_internal_and_the_run_stays_open_with_its_flag(h, w, registry):
    conn, run_id, _ = started_cancel(h, w, registry)

    def down(call):
        if call["to_state"] == "cancelled":
            raise RuntimeError(f"db down {CANARY}")

    w.store.before_move = down
    sent_before = len(conn.sent)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert [e["code"] for e in conn.errors] == ["internal"] and CANARY not in repr(conn.errors)
    assert state_of(w, run_id) == "running" and buffer.exists(run_id)
    assert run_guard.cancel_reason(run_id) is not None          # the user's intent is kept (run_manager's rule)
    assert len(conn.sent) == sent_before and h.emitter.is_bound(run_id)     # so a retry can still number the end
    w.store.before_move = None
    go(h.on_cancel_request(conn, cancel_msg(run_id)))           # and the retry works
    assert state_of(w, run_id) == "cancelled" and conn.sent[-1]["seq"] == 6


def test_cancel_the_run_vanishing_from_the_emitter_meanwhile_is_answered_not_found(h, w, registry):
    """Between the hooks and the lock, someone else ended the run and released it."""
    conn, run_id, _ = started_cancel(h, w, registry)

    async def ender(c, run):
        run_manager.end_run(USER, run.id, "completed")
        h.emitter.unbind(run.id)

    h.add_on_cancel(ender)
    go(h.on_cancel_request(conn, cancel_msg(run_id)))
    assert [e["code"] for e in conn.errors] == ["run_not_found"] and state_of(w, run_id) == "completed"


# ---- cancel: CleanupIncomplete ----

def break_deletes(w):
    w.redis.fail_if(lambda commands: str(commands[0][0]).upper() == "DEL")


def test_cancel_cleanup_incomplete_is_success_for_the_device_and_the_purge_is_retried(w, registry):
    h = make(purge_delays=(0.01, 0.01))
    conn, run_id, _ = started_cancel(h, w, registry)
    break_deletes(w)

    async def scenario():
        await h.on_cancel_request(conn, cancel_msg(run_id))
        assert buffer.exists(run_id)                            # the buffer could not be deleted (yet)
        w.redis.fail_if(None)                                   # Redis is back
        await h.wait_purges()

    go(scenario())
    assert conn.errors == [] and state_of(w, run_id) == "cancelled"
    assert conn.sent[-1]["payload"]["state"] == "cancelled" and conn.sent[-1]["seq"] == 6
    nothing_left(w, run_id)                                     # the retry deleted the buffer and the rails


def test_cancel_cleanup_incomplete_keeps_trying_then_gives_up_and_leaves_the_ttl_to_finish(w, registry, monkeypatch):
    h = make(purge_delays=(0.01, 0.01, 0.01))
    conn, run_id, _ = started_cancel(h, w, registry)
    break_deletes(w)
    attempts = []
    real = run_manager.purge_run

    def counting(user_id, rid):
        attempts.append(rid)
        return real(user_id, rid)

    monkeypatch.setattr(run_manager, "purge_run", counting)

    async def scenario():
        await h.on_cancel_request(conn, cancel_msg(run_id))
        await h.wait_purges()

    go(scenario())
    assert attempts == [run_id] * 3 and conn.errors == []
    assert w.redis.keys() != []                                     # still there: only the TTL will remove it
    assert w.redis.ttl(buffer.buffer_keys(run_id)[1]) > 0           # and it has one


def test_cancel_a_purge_task_is_not_kept_once_it_has_finished(w, registry):
    h = make(purge_delays=(0.01,))
    conn, run_id, _ = started_cancel(h, w, registry)
    break_deletes(w)

    async def scenario():
        await h.on_cancel_request(conn, cancel_msg(run_id))
        w.redis.fail_if(None)
        await h.wait_purges()
        await asyncio.sleep(0.01)
        return h.purges_pending()

    assert go(scenario()) == 0


# ============================================================ install

@pytest.fixture
def fresh_router(monkeypatch):
    fresh = MessageRouter()
    monkeypatch.setattr(router_module, "_ROUTER", fresh)
    monkeypatch.setattr(ws_module, "_HANDLER", ws_module.UnavailableHandler())
    return fresh


def test_install_registers_user_message_and_cancel_too_and_still_only_once(fresh_router):
    handlers = RunHandlers(emitter=RunEmitter())
    run_handlers.install(handlers=handlers)
    run_handlers.install(handlers=handlers)
    assert fresh_router.registered_types() == ("cancel", "run.resume", "run.start", "user_message")
    assert len(fresh_router._disconnect_listeners) == 1


# ======================================================= content hygiene (caplog)

def test_no_content_reaches_the_log(h, w, registry, caplog):
    caplog.set_level(logging.DEBUG)
    runner = Runner()
    runner.boom["bad"] = RuntimeError(CANARY)
    h.set_turn_runner(runner)

    async def hook(c, run):
        raise RuntimeError(CANARY)

    h.add_on_cancel(hook)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        await h.on_user_message(conn, um(run_id, CANARY))
        await h.on_user_message(conn, um(run_id, "bad"))
        await h.wait_idle(run_id)
        h2 = make(max_queued_turns=0)
        h2.set_turn_runner(runner)
        runner.gates[CANARY] = asyncio.Event()
        conn.errors.clear()                                          # begin() wants a clean connection
        run2 = await begin(h2, conn)
        await h2.on_user_message(conn, um(run2, CANARY))
        await h2.on_user_message(conn, um(run2, CANARY))            # over the cap
        await h2.on_cancel_request(conn, cancel_msg(run2))
        await h.on_cancel_request(conn, cancel_msg(run_id))
        await h.on_cancel_request(conn, cancel_msg(run_id))         # over: run_not_found
        return run_id

    run_id = go(scenario())
    assert CANARY not in caplog.text
    mine = [rec for rec in caplog.records if rec.name == run_handlers.logger.name]
    assert any(run_id in rec.getMessage() for rec in mine)
    assert any("RuntimeError" in rec.getMessage() for rec in mine)


# ========================================================= through the socket

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


class SocketRunner:
    """A turn runner for the socket tests. It streams two deltas and a text_done through the
    emitter, as the loop will; `hold` keeps it open (released by a threading.Event, because the
    test thread is not the server's loop); a cancellation is recorded."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.release = threading.Event()
        self.hold = set()
        self.started, self.cancelled, self.finished = [], [], []

    async def __call__(self, conn, run, msg):
        text = msg["payload"]["text"]
        self.started.append(text)
        try:
            while text in self.hold and not self.release.is_set():
                await asyncio.sleep(0.005)
            em = self.handlers.emitter
            await em.emit(run.id, "run_event", {"type": "text_delta", "text": f"re: {text}"})
            await em.emit(run.id, "run_event", {"type": "text_delta", "text": "done"})
            await em.emit(run.id, "run_event", {"type": "text_done", "stop_reason": "end_turn"})
            self.finished.append(text)
        except asyncio.CancelledError:
            self.cancelled.append(text)
            raise


def wait_for(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), what


RUN = "run_turns_socket_01"


@pytest.fixture
def wired(rig, w):
    handlers = RunHandlers(emitter=RunEmitter(), max_queued_turns=1)
    router = MessageRouter()
    run_handlers.install(router, handlers=handlers)
    rig.use_handler(router)
    rig.check = check_valid
    return handlers


def start_over_the_wire(dev, run_id=RUN):
    dev.send(envelope("run.start", start_payload(), run_id=run_id))
    return [dev.recv(), dev.recv()]


def test_socket_a_message_reaches_the_runner_and_its_events_stream_back_with_increasing_seq(rig, w, wired):
    runner = SocketRunner(wired)
    wired.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        first = start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "hello"}, run_id=RUN))
        streamed = [dev.recv(), dev.recv(), dev.recv()]
        dev.ping_pong()
    assert [m["seq"] for m in first + streamed] == [1, 2, 3, 4, 5]
    assert [m["payload"].get("text") or m["payload"]["type"] for m in streamed] == ["re: hello", "done", "text_done"]
    assert runner.started == ["hello"] and runner.finished == ["hello"]


def test_socket_without_a_runner_the_device_gets_internal_with_the_ref_id(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        msg = envelope("user_message", {"text": "hello"}, run_id=RUN)
        dev.send(msg)
        err = dev.recv_error()
        assert err["code"] == "internal" and err["ref_id"] == msg["id"]
        assert err["message"] == run_handlers.MSG_NO_TURN_RUNNER
        dev.ping_pong()


def test_socket_messages_queue_in_order_and_the_one_past_the_cap_is_rate_limited(rig, w, wired):
    runner = SocketRunner(wired)
    runner.hold = {"one"}
    wired.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "one"}, run_id=RUN))
        wait_for(lambda: runner.started == ["one"], "the first turn did not start")
        dev.send(envelope("user_message", {"text": "two"}, run_id=RUN))          # waits (cap 1)
        over = envelope("user_message", {"text": "three"}, run_id=RUN)
        dev.send(over)
        err = dev.recv_error()                                                    # the receive loop was free to answer
        assert err["code"] == "rate_limited" and err["ref_id"] == over["id"] and "retry_after_s" in err["details"]
        runner.release.set()
        events = [dev.recv() for _ in range(6)]                                   # both turns, three events each
    assert [m["seq"] for m in events] == [3, 4, 5, 6, 7, 8]                       # contiguous across the two turns
    assert [m["payload"].get("text") for m in events if m["payload"]["type"] == "text_delta"] == \
        ["re: one", "done", "re: two", "done"]
    assert runner.started == ["one", "two"]


def test_socket_cancel_in_the_middle_of_a_turn_ends_the_run_and_the_device_is_told(rig, w, wired):
    runner = SocketRunner(wired)
    runner.hold = {"slow"}
    wired.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "slow"}, run_id=RUN))
        wait_for(lambda: runner.started == ["slow"], "the turn did not start")
        dev.send(envelope("cancel", {}, run_id=RUN))                              # while the turn is open
        final = dev.recv()
        assert final["seq"] == 3 and final["t"] == "run_event"
        assert final["payload"] == {"type": "state_changed", "state": "cancelled",
                                    "reason": run_handlers.CANCEL_REASON}
        again = envelope("user_message", {"text": "more"}, run_id=RUN)
        dev.send(again)
        err = dev.recv_error()
        assert err["code"] == "run_not_found" and err["ref_id"] == again["id"]
    wait_for(lambda: runner.cancelled == ["slow"], "the open turn was not cancelled")
    assert w.store.rows[RUN]["state"] == "cancelled" and w.redis.keys() == []
    assert not wired.emitter.is_bound(RUN)


def test_socket_a_run_is_invisible_to_every_other_device_and_user(rig, w, wired):
    runner = SocketRunner(wired)
    wired.set_turn_runner(runner)
    cred_a, _ = rig.store.add_device("user-a")
    cred_sibling, _ = rig.store.add_device("user-a")
    cred_b, _ = rig.store.add_device("user-b")
    with rig.session(cred_a) as (owner, _w1):
        start_over_the_wire(owner)
        for credential in (cred_sibling, cred_b):
            with rig.session(credential) as (other, _w2):
                answers = []
                for t, payload in (("user_message", {"text": "x"}), ("cancel", {})):
                    msg = envelope(t, payload, run_id=RUN)
                    other.send(msg)
                    err = other.recv_error()
                    answers.append((err["code"], err["message"], err["ref_id"] == msg["id"]))
                missing = envelope("user_message", {"text": "x"}, run_id="run_never_existed_1")
                other.send(missing)
                err = other.recv_error()
                answers.append((err["code"], err["message"], err["ref_id"] == missing["id"]))
                assert set(answers) == {("run_not_found", run_handlers.MSG_RUN_NOT_FOUND, True)}   # the same for a missing run
        owner.ping_pong()
    assert runner.started == [] and w.store.rows[RUN]["state"] in ("running", "paused_disconnected")


def test_socket_a_disconnect_mid_turn_stops_the_turn_and_pauses_the_run(rig, w, wired):
    runner = SocketRunner(wired)
    runner.hold = {"slow"}
    wired.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "slow"}, run_id=RUN))
        wait_for(lambda: runner.started == ["slow"], "the turn did not start")
    wait_for(lambda: runner.cancelled == ["slow"], "the open turn was not stopped by the disconnect")
    assert w.store.rows[RUN]["state"] == "paused_disconnected" and wired.turn_runs() == ()
