"""S14b: the run emitter (codeagent/emitter.py) -- numbered, stored, delivered server events.

Three layers of tests, all against in-memory twins behind the real interfaces (the run
store, the encrypted Redis buffer and run_guard's Redis from tests/unit/run_manager_testkit.py,
the real protocol/v1 validator):

  * unit tests with a stand-in connection in an in-memory registry (what one emit does:
    number, store, deliver, in that order; what it refuses; the per-run lock; verbatim
    re-delivery; the terminal event that has no buffer to number it from);
  * the same claims through S04's real socket (tests/unit/codeagent_testkit.Rig), with every
    frame the device receives checked against protocol/v1, so "valid envelope", "contiguous
    seq" and "the resumed device gets exactly what it missed" are proven on the transport;
  * content hygiene: a canary in an event's text and in a Redis fault never reaches the log.

Maps to the work package ("What" 2 and "Content hygiene" 5):
  * emit builds a valid server envelope, seq from the buffer, appended to the replay,
    sent to the run's device if it is connected ........................ test_emit_*
  * an offline device is not an error: the event waits in the replay .... test_an_offline_*
  * sequence, storage and delivery are atomic per run ..................... test_concurrent_*, test_exclusive_*
  * emit_state_changed(run_id, state, reason=None) ........................ test_emit_state_changed_*
  * re-delivery is verbatim and is NOT stored a second time ............... test_redeliver_*
  * the terminal event (the buffer is gone) ................................ test_final_*
  * nothing a run carries reaches the log ................................... test_no_content_*

Skipped as a module when jsonschema is not installed, like the other socket tests.
"""
import asyncio
import logging
import random
import re
import uuid

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed; install protocol/requirements.txt to run the "
           "device-socket tests (see codeagent/ws.py, 'Runtime needs')",
)

from codeagent import buffer, connections, emitter as emitter_module, run_manager, runs   # noqa: E402
from codeagent import ws as ws_module                                                     # noqa: E402
from codeagent.connections import ConnectionClosed                                        # noqa: E402
from codeagent.emitter import (                                                           # noqa: E402
    BindingConflict, EmitterError, InvalidEvent, RunEmitter, RunNotBound,
)
from tests.unit import buffer_testkit as bt                                               # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope                                    # noqa: E402
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET                            # noqa: E402
from tests.unit.run_manager_testkit import (                                              # noqa: E402
    CANARY, DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, start_payload,
)

DELTA = {"type": "text_delta", "text": "hello"}


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
    """Just enough of a DeviceConnection for the emitter: who it is, send_json, and what
    was sent. `gate` holds a send until it is set; `jitter` makes every send yield to other
    tasks (so an unlocked emitter would interleave); `on_send` sees each envelope first."""

    def __init__(self, user_id=USER, device_id=DEVICE, *, jitter=False):
        self.user_id = user_id
        self.device_id = device_id
        self.sent = []
        self.closed = False
        self.jitter = jitter
        self.gate = None
        self.on_send = None

    async def send_json(self, env):
        if self.closed:
            raise ConnectionClosed("connection closed")
        if self.gate is not None:
            await self.gate.wait()
        if self.jitter:
            await asyncio.sleep(random.random() * 0.002)
        if self.on_send is not None:
            self.on_send(env)
        if self.closed:
            raise ConnectionClosed("connection closed")
        self.sent.append(env)


def check_valid(env):
    """The envelope passes protocol/v1, STRICT (not the tolerant reader)."""
    issues = run_manager._protocol().validate_message(env, tolerant=False)
    assert not issues, [(i.path, i.validator) for i in issues]


def attach(w, registry, *, device=DEVICE, user=USER, last_seq=0, conn=True, jitter=False, run_state=None):
    """A started run, an emitter bound to it, and (optionally) its device's connection."""
    r = w.run_at(run_state, user=user, device=device) if run_state else w.start(user=user, device=device)
    em = RunEmitter()
    em.bind(r.id, user, device, last_seq=last_seq)
    c = None
    if conn:
        c = StubConn(user, device, jitter=jitter)
        registry.add(c)
    return r, em, c


def stored(run_id, after=0):
    return buffer.replay_after(run_id, after)


# =============================================================== one emit

@pytest.mark.parametrize("t,payload", [
    ("run_event", {"type": "text_delta", "text": "hello"}),
    ("run_event", {"type": "text_done", "stop_reason": "end_turn"}),
    ("run_event", {"type": "state_changed", "state": "running", "reason": "resumed"}),
    ("tool_proposed", {"call_id": "call_grep_1", "tool": "grep", "input": {"pattern": "x"}, "risk": "R0"}),
    ("budget_warning", {"scope": "run", "spent_pct": 80}),
])
def test_emit_builds_a_valid_envelope_stores_it_and_sends_it(w, registry, t, payload):
    r, em, conn = attach(w, registry)
    env = go(em.emit(r.id, t, payload))
    assert set(env) == {"v", "t", "id", "run_id", "seq", "ts", "payload"}
    assert env["v"] == 1 and env["t"] == t and env["run_id"] == r.id and env["seq"] == 1
    assert env["payload"] == payload
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", env["id"])
    check_valid(env)
    assert stored(r.id) == [env]                  # in the replay, verbatim
    assert conn.sent == [env]                     # and on the wire


def test_seq_runs_1_2_3_with_fresh_ids_and_what_is_sent_is_what_is_stored(w, registry):
    r, em, conn = attach(w, registry)

    async def scenario():
        return [await em.emit(r.id, "run_event", {"type": "text_delta", "text": f"part {i}"}) for i in range(5)]

    envs = go(scenario())
    assert [e["seq"] for e in envs] == [1, 2, 3, 4, 5]
    assert len({e["id"] for e in envs}) == 5
    assert conn.sent == envs
    assert stored(r.id) == envs


def test_the_buffer_counter_decides_the_seq_not_the_emitter(w, registry):
    """A rebuilt buffer continues from the device's last_seq; the emitter just follows."""
    r, em, conn = attach(w, registry)
    buffer.delete_buffer(r.id)
    buffer.create_buffer(r.id, state={"profile": "code"}, history=[], last_seq=41)
    env = go(em.emit(r.id, "run_event", DELTA))
    assert env["seq"] == 42


def test_the_event_is_stored_before_it_is_sent(w, registry):
    r, em, conn = attach(w, registry)
    seen = []
    conn.on_send = lambda env: seen.append([e["seq"] for e in buffer.replay_after(r.id, 0)])

    async def scenario():
        for _ in range(3):
            await em.emit(r.id, "run_event", DELTA)

    go(scenario())
    assert seen == [[1], [1, 2], [1, 2, 3]]       # at send time the event was already stored


def test_an_offline_device_is_not_an_error_the_event_waits_in_the_replay(w, registry):
    r, em, _ = attach(w, registry, conn=False)    # nobody connected
    env = go(em.emit(r.id, "run_event", DELTA))
    assert env["seq"] == 1
    assert stored(r.id) == [env]


def test_a_connection_that_closed_under_us_is_not_an_error_either(w, registry):
    r, em, conn = attach(w, registry)
    conn.closed = True
    env = go(em.emit(r.id, "run_event", DELTA))
    assert conn.sent == [] and stored(r.id) == [env]


def test_a_connection_that_closes_during_the_send_is_not_an_error_either(w, registry):
    r, em, conn = attach(w, registry)

    def leave(env):
        conn.closed = True

    conn.on_send = leave
    env = go(em.emit(r.id, "run_event", DELTA))
    assert conn.sent == [] and stored(r.id) == [env]


def test_only_the_runs_own_device_receives_it(w, registry):
    r, em, mine = attach(w, registry)
    other_device = StubConn(USER, OTHER_DEVICE)
    other_user = StubConn(OTHER_USER, DEVICE)
    registry.add(other_device)
    registry.add(other_user)
    go(em.emit(r.id, "run_event", DELTA))
    assert len(mine.sent) == 1
    assert other_device.sent == [] and other_user.sent == []


def test_after_a_reconnect_events_go_to_the_new_connection(w, registry):
    r, em, old = attach(w, registry)
    new = StubConn()
    registry.add(new)                             # the same device, superseding the old connection
    go(em.emit(r.id, "run_event", DELTA))
    assert old.sent == [] and len(new.sent) == 1


# ====================================================== what emit refuses

def test_an_unbound_run_is_refused_and_no_seq_is_used(w, registry):
    r = w.start()
    em = RunEmitter()
    with pytest.raises(RunNotBound):
        go(em.emit(r.id, "run_event", DELTA))
    assert stored(r.id) == []
    assert buffer.next_seq(r.id) == 1             # nothing was numbered


@pytest.mark.parametrize("t", ["error", "pong", "welcome", "user_message", "run.start", "nope", "", None, 7])
def test_only_the_three_stream_types_go_through_this_path(w, registry, t):
    r, em, conn = attach(w, registry)
    with pytest.raises(InvalidEvent):
        go(em.emit(r.id, t, DELTA))
    assert conn.sent == [] and buffer.next_seq(r.id) == 1


@pytest.mark.parametrize("payload", [
    None, "text", ["list"], 7,
    {"x": {1, 2}},                                # a set is not JSON
    {"x": float("nan")},                          # nor is NaN
    {"x": object()},
    {"text": "x" * buffer.MAX_ITEM_BYTES},        # too big for one replay message
])
def test_a_payload_that_could_not_be_stored_is_refused_before_a_seq_is_used(w, registry, payload):
    """next_seq cannot be given back: a payload the buffer would refuse must be caught
    BEFORE it, or the stream would have a hole."""
    r, em, conn = attach(w, registry)
    with pytest.raises(InvalidEvent):
        go(em.emit(r.id, "run_event", payload))
    assert conn.sent == []
    assert buffer.next_seq(r.id) == 1


def test_a_run_whose_buffer_is_gone_raises_and_nothing_is_sent(w, registry):
    r, em, conn = attach(w, registry)
    buffer.delete_buffer(r.id)
    with pytest.raises(buffer.BufferMissing):
        go(em.emit(r.id, "run_event", DELTA))
    assert conn.sent == []
    assert em.is_bound(r.id)                      # the caller decides what happens to the run


def test_a_redis_fault_raises_with_fixed_text_and_nothing_is_sent(w, registry):
    r, em, conn = attach(w, registry)
    w.redis.fail_if(lambda commands: True)
    with pytest.raises(buffer.BufferUnavailable) as caught:
        go(em.emit(r.id, "run_event", {"type": "text_delta", "text": CANARY}))
    assert bt.CANARY not in str(caught.value) and CANARY not in str(caught.value)
    assert conn.sent == []


def test_a_fault_on_the_send_that_is_not_a_closed_connection_propagates(w, registry):
    r, em, conn = attach(w, registry)

    def boom(env):
        raise RuntimeError("the transport broke")

    conn.on_send = boom
    with pytest.raises(RuntimeError):
        go(em.emit(r.id, "run_event", DELTA))
    assert len(stored(r.id)) == 1                 # it is stored: a device that resumes gets it


# ================================================= emit_state_changed

def test_emit_state_changed_builds_the_run_event_subtype(w, registry):
    r, em, conn = attach(w, registry)

    async def scenario():
        a = await em.emit_state_changed(r.id, "running")
        b = await em.emit_state_changed(r.id, "paused_disconnected", "device_disconnected")
        return a, b

    a, b = go(scenario())
    assert a["t"] == b["t"] == "run_event"
    assert a["payload"] == {"type": "state_changed", "state": "running"}
    assert b["payload"] == {"type": "state_changed", "state": "paused_disconnected", "reason": "device_disconnected"}
    check_valid(a)
    check_valid(b)
    assert conn.sent == [a, b] and stored(r.id) == [a, b]


@pytest.mark.parametrize("state", ["nope", "", None, 7, "RUNNING"])
def test_emit_state_changed_refuses_an_unknown_state(w, registry, state):
    r, em, conn = attach(w, registry)
    with pytest.raises(InvalidEvent):
        go(em.emit_state_changed(r.id, state))
    assert buffer.next_seq(r.id) == 1


@pytest.mark.parametrize("reason", ["", "Has Capitals", "1starts_with_digit", "x" * 65, 7, "with space", "dot.ted"])
def test_emit_state_changed_refuses_a_reason_the_schema_would_refuse(w, registry, reason):
    r, em, conn = attach(w, registry)
    with pytest.raises(InvalidEvent):
        go(em.emit_state_changed(r.id, "running", reason))
    assert buffer.next_seq(r.id) == 1


@pytest.mark.parametrize("state", sorted(runs.TERMINAL_STATES))
def test_a_terminal_state_is_not_announced_through_the_stored_path(w, registry, state):
    """The buffer is deleted when a run ends, so a terminal event must not be numbered
    from it: emit_final is the one way."""
    r, em, conn = attach(w, registry)
    with pytest.raises(InvalidEvent):
        go(em.emit_state_changed(r.id, state))
    assert buffer.next_seq(r.id) == 1


# ============================================================ bind / unbind

def test_bind_is_idempotent_and_keeps_the_highest_last_seq(w, registry):
    em = RunEmitter()
    em.bind("run-aaaaaaaa", USER, DEVICE, last_seq=5)
    em.bind("run-aaaaaaaa", USER, DEVICE, last_seq=2)
    assert em.last_seq("run-aaaaaaaa") == 5
    em.bind("run-aaaaaaaa", USER, DEVICE, last_seq=9)
    assert em.last_seq("run-aaaaaaaa") == 9
    assert em.bound_runs() == ("run-aaaaaaaa",)


def test_a_run_cannot_be_rebound_to_another_device_or_user(w, registry):
    em = RunEmitter()
    em.bind("run-aaaaaaaa", USER, DEVICE, last_seq=0)
    with pytest.raises(BindingConflict):
        em.bind("run-aaaaaaaa", USER, OTHER_DEVICE, last_seq=0)
    with pytest.raises(BindingConflict):
        em.bind("run-aaaaaaaa", OTHER_USER, DEVICE, last_seq=0)
    assert em.last_seq("run-aaaaaaaa") == 0


@pytest.mark.parametrize("kwargs", [
    dict(run_id="short", user_id=USER, device_id=DEVICE, last_seq=0),
    dict(run_id="has space in it", user_id=USER, device_id=DEVICE, last_seq=0),
    dict(run_id="run-aaaaaaaa\n", user_id=USER, device_id=DEVICE, last_seq=0),
    dict(run_id=None, user_id=USER, device_id=DEVICE, last_seq=0),
    dict(run_id="run-aaaaaaaa", user_id="", device_id=DEVICE, last_seq=0),
    dict(run_id="run-aaaaaaaa", user_id=USER, device_id="", last_seq=0),
    dict(run_id="run-aaaaaaaa", user_id=None, device_id=DEVICE, last_seq=0),
    dict(run_id="run-aaaaaaaa", user_id=USER, device_id=DEVICE, last_seq=-1),
    dict(run_id="run-aaaaaaaa", user_id=USER, device_id=DEVICE, last_seq=True),
    dict(run_id="run-aaaaaaaa", user_id=USER, device_id=DEVICE, last_seq="1"),
])
def test_bind_refuses_malformed_arguments(kwargs):
    with pytest.raises(ValueError):
        RunEmitter().bind(**kwargs)


def test_the_run_id_rule_is_the_buffers_own():
    """emitter._RUN_ID_RE is a copy of the buffer's pattern (the buffer's is private);
    this pins that the two cannot drift apart."""
    assert emitter_module._RUN_ID_RE.pattern.strip("^$") == buffer._RUN_ID_RE.pattern.strip("^$")


def test_unbind_forgets_the_run_and_a_later_bind_starts_over(w, registry):
    r, em, conn = attach(w, registry)
    go(em.emit(r.id, "run_event", DELTA))
    assert em.unbind(r.id) is True and em.unbind(r.id) is False
    assert not em.is_bound(r.id) and em.last_seq(r.id) is None
    with pytest.raises(RunNotBound):
        go(em.emit(r.id, "run_event", DELTA))
    em.bind(r.id, USER, DEVICE, last_seq=1)
    assert go(em.emit(r.id, "run_event", DELTA))["seq"] == 2      # the counter lives in the buffer


def test_a_waiting_emit_fails_if_its_run_was_unbound_and_bound_again_meanwhile(w, registry):
    """The lock belongs to a binding. After unbind + bind the run has a NEW lock, so an
    emit still waiting on the old one must not carry on under it: two emits holding two
    different locks would break the order."""
    r, em, conn = attach(w, registry)

    async def scenario():
        gate = asyncio.Event()

        async def holder():
            async with em.exclusive(r.id):
                await gate.wait()
                em.unbind(r.id)
                em.bind(r.id, USER, DEVICE, last_seq=0)

        h = asyncio.ensure_future(holder())
        await asyncio.sleep(0.01)                  # the holder has the lock
        waiter = asyncio.ensure_future(em.emit(r.id, "run_event", DELTA))
        await asyncio.sleep(0.01)                  # the waiter is queued on it
        gate.set()
        await h
        with pytest.raises(RunNotBound):
            await waiter
        return await em.emit(r.id, "run_event", DELTA)   # the new binding works

    assert go(scenario())["seq"] == 1
    assert len(conn.sent) == 1


# ============================================ order under concurrency

def test_concurrent_emits_are_stored_and_delivered_in_seq_order(w, registry):
    """Sixty tasks emit at once to a connection whose every send yields to the others.
    Without the per-run lock the sends would interleave and seq would arrive out of order."""
    r, em, conn = attach(w, registry, jitter=True)

    async def scenario():
        return await asyncio.gather(*[
            em.emit(r.id, "run_event", {"type": "text_delta", "text": f"t{i}"}) for i in range(60)])

    envs = go(scenario())
    assert [e["seq"] for e in conn.sent] == list(range(1, 61))          # delivery order == seq order
    assert sorted(e["seq"] for e in envs) == list(range(1, 61))         # no gaps, no duplicates
    assert [e["seq"] for e in stored(r.id)] == list(range(1, 61))
    assert len({e["id"] for e in conn.sent}) == 60
    for env in conn.sent:
        assert stored(r.id, env["seq"] - 1)[0] == env                   # what was sent is what was stored


def test_runs_are_independent_one_slow_device_does_not_hold_up_another(w, registry):
    a, b = w.start(device=DEVICE), w.start(device=OTHER_DEVICE)
    em = RunEmitter()
    em.bind(a.id, USER, DEVICE, last_seq=0)
    em.bind(b.id, USER, OTHER_DEVICE, last_seq=0)
    conn_a, conn_b = StubConn(USER, DEVICE), StubConn(USER, OTHER_DEVICE)
    registry.add(conn_a)
    registry.add(conn_b)

    async def scenario():
        conn_a.gate = asyncio.Event()
        slow = asyncio.ensure_future(em.emit(a.id, "run_event", DELTA))
        await asyncio.sleep(0.01)
        fast = await asyncio.wait_for(em.emit(b.id, "run_event", DELTA), 2)   # not behind A's send
        assert not slow.done()
        conn_a.gate.set()
        return fast, await slow

    fast, slow = go(scenario())
    assert fast["seq"] == 1 and slow["seq"] == 1
    assert conn_b.sent == [fast] and conn_a.sent == [slow]


def test_exclusive_lets_the_holder_emit_and_makes_others_wait(w, registry):
    r, em, conn = attach(w, registry)

    async def scenario():
        order = []
        gate = asyncio.Event()

        async def holder():
            async with em.exclusive(r.id) as run:
                order.append("holder in")
                await run.emit("run_event", {"type": "text_delta", "text": "h1"})   # no deadlock on itself
                await gate.wait()
                await run.emit_state_changed("running")
                order.append("holder out")

        async def other():
            await em.emit(r.id, "run_event", {"type": "text_delta", "text": "o1"})
            order.append("other done")

        h = asyncio.ensure_future(holder())
        await asyncio.sleep(0.01)
        o = asyncio.ensure_future(other())
        await asyncio.sleep(0.01)
        assert order == ["holder in"]              # the other emit is waiting, not interleaved
        gate.set()
        await asyncio.gather(h, o)
        return order

    order = go(scenario())
    assert order == ["holder in", "holder out", "other done"]
    texts = [e["payload"].get("text") or e["payload"]["state"] for e in conn.sent]
    assert texts == ["h1", "running", "o1"]
    assert [e["seq"] for e in conn.sent] == [1, 2, 3]


def test_the_exclusive_handle_is_dead_once_the_block_has_ended(w, registry):
    r, em, conn = attach(w, registry)

    async def scenario():
        async with em.exclusive(r.id) as run:
            pass
        with pytest.raises(EmitterError):
            await run.emit("run_event", DELTA)
        with pytest.raises(EmitterError):
            await run.redeliver([])

    go(scenario())
    assert conn.sent == []


def test_exclusive_on_an_unbound_run_is_refused(w, registry):
    async def scenario():
        async with RunEmitter().exclusive("run-aaaaaaaa"):
            pass

    with pytest.raises(RunNotBound):
        go(scenario())


def test_an_exception_inside_exclusive_releases_the_lock(w, registry):
    r, em, conn = attach(w, registry)

    async def scenario():
        with pytest.raises(KeyError):
            async with em.exclusive(r.id):
                raise KeyError("x")
        return await asyncio.wait_for(em.emit(r.id, "run_event", DELTA), 2)

    assert go(scenario())["seq"] == 1


# ===================================================== verbatim re-delivery

def fill(w, registry, n=5):
    r, em, conn = attach(w, registry)

    async def scenario():
        for i in range(n):
            await em.emit(r.id, "run_event", {"type": "text_delta", "text": f"t{i}"})

    go(scenario())
    return r, em, conn


def replay_list_length(w, run_id):
    return len(w.redis.get_list(buffer.buffer_keys(run_id)[1]))


def test_redeliver_sends_the_stored_events_verbatim_and_stores_nothing_again(w, registry):
    r, em, old = fill(w, registry)
    missed = stored(r.id, 2)                        # seq 3, 4, 5
    assert [e["seq"] for e in missed] == [3, 4, 5]
    before = replay_list_length(w, r.id)
    new = StubConn()
    registry.add(new)                               # the device came back on a new connection
    assert go(em.redeliver(r.id, missed)) == 3
    assert new.sent == missed                       # the same ids, the same seqs, the same ts
    assert [e["id"] for e in new.sent] == [e["id"] for e in missed]
    assert replay_list_length(w, r.id) == before    # NOT appended a second time
    assert [e["seq"] for e in stored(r.id)] == [1, 2, 3, 4, 5]


def test_redeliver_with_nothing_to_send_is_fine(w, registry):
    r, em, conn = attach(w, registry)
    assert go(em.redeliver(r.id, [])) == 0
    assert conn.sent == []


def test_redeliver_to_an_offline_device_sends_nothing_and_says_so(w, registry):
    r, em, conn = fill(w, registry)
    registry.remove(conn)
    assert go(em.redeliver(r.id, stored(r.id))) == 0


def test_redeliver_stops_quietly_when_the_connection_leaves_part_way(w, registry):
    r, em, conn = fill(w, registry)
    events = stored(r.id)
    conn.sent.clear()

    def leave_after_two(env):
        if len(conn.sent) == 2:
            conn.closed = True

    conn.on_send = leave_after_two
    assert go(em.redeliver(r.id, events)) == 2
    assert [e["seq"] for e in conn.sent] == [1, 2]


def test_redeliver_refuses_a_bad_list_before_sending_anything(w, registry):
    r, em, conn = fill(w, registry)
    conn.sent.clear()
    good = stored(r.id)
    foreign = dict(good[1], run_id="run-bbbbbbbb")
    for bad in (
        [good[0], foreign],                                         # another run's event
        [good[1], good[0]],                                         # out of order
        [good[0], good[0]],                                         # a duplicate seq
        [good[0], dict(good[1], t="error")],                        # not a stream message
        [good[0], dict(good[1], seq=0)],                            # seq below 1
        [good[0], "not a dict"],
    ):
        with pytest.raises(InvalidEvent):
            go(em.redeliver(r.id, bad))
        assert conn.sent == []                                      # nothing went out, not even the good first one


def test_redeliver_moves_the_tracker_so_a_restarted_server_can_end_the_run(w, registry):
    """A fresh emitter (a restarted process) binds with the device's last_seq; the replay it
    re-delivers moves its idea of the last seq to the end of the stream, so the terminal
    event that follows is numbered right even though the buffer is gone by then."""
    r, em, conn = fill(w, registry)
    fresh = RunEmitter()
    fresh.bind(r.id, USER, DEVICE, last_seq=2)
    assert fresh.last_seq(r.id) == 2
    go(fresh.redeliver(r.id, stored(r.id, 2)))
    assert fresh.last_seq(r.id) == 5


# ============================================ the terminal event (no buffer)

def test_final_continues_the_numbering_after_the_buffer_is_gone(w, registry):
    r, em, conn = attach(w, registry, run_state="running")

    async def before():
        for i in range(3):
            await em.emit(r.id, "run_event", {"type": "text_delta", "text": f"t{i}"})

    go(before())
    run_manager.cancel_run(USER, r.id)              # deletes the buffer: next_seq is impossible now
    with pytest.raises(buffer.BufferMissing):
        buffer.next_seq(r.id)
    env = go(em.emit_final(r.id, "cancelled"))
    assert env["seq"] == 4 and env["t"] == "run_event"
    assert env["payload"] == {"type": "state_changed", "state": "cancelled"}
    check_valid(env)
    assert conn.sent[-1] == env
    assert [e["seq"] for e in conn.sent] == [1, 2, 3, 4]
    assert w.redis.keys() == []                     # live only: nothing was stored, nothing brought back
    assert not em.is_bound(r.id)
    with pytest.raises(RunNotBound):
        go(em.emit(r.id, "run_event", DELTA))


def test_final_never_touches_the_buffer_not_even_a_live_one(w, registry):
    """The terminal event is live only. If the buffer is still there (the caller announced
    before ending the run) it is neither written to nor numbered from."""
    r, em, conn = attach(w, registry)

    async def scenario():
        await em.emit(r.id, "run_event", DELTA)
        await em.emit(r.id, "run_event", DELTA)
        return await em.emit_final(r.id, "completed")

    final = go(scenario())
    assert final["seq"] == 3 and conn.sent[-1] == final
    assert [e["seq"] for e in stored(r.id)] == [1, 2]       # the final one is not in the replay
    assert buffer.next_seq(r.id) == 3                       # and it did not use the counter


def test_final_carries_a_reason_and_works_for_every_terminal_state(w, registry):
    for state in sorted(runs.TERMINAL_STATES):
        r, em, conn = attach(w, registry, conn=True, run_state="running")
        env = go(em.emit_final(r.id, state, "because_why_not"))
        assert env["payload"] == {"type": "state_changed", "state": state, "reason": "because_why_not"}
        check_valid(env)
        registry.remove(conn)


@pytest.mark.parametrize("state", ["running", "paused_disconnected", "nope", None])
def test_final_refuses_a_state_that_is_not_terminal_and_keeps_the_run_bound(w, registry, state):
    r, em, conn = attach(w, registry)
    with pytest.raises(InvalidEvent):
        go(em.emit_final(r.id, state))
    assert em.is_bound(r.id) and conn.sent == []


def test_final_to_an_offline_device_is_not_an_error_and_still_releases_the_run(w, registry):
    r, em, conn = attach(w, registry, conn=False, run_state="running")
    run_manager.end_run(USER, r.id, "completed")
    env = go(em.emit_final(r.id, "completed"))
    assert env["seq"] == 1
    assert not em.is_bound(r.id)


def test_final_on_an_unbound_run_is_refused(w, registry):
    with pytest.raises(RunNotBound):
        go(RunEmitter().emit_final("run-aaaaaaaa", "cancelled"))


def test_final_waits_for_an_emit_in_flight_and_comes_after_it(w, registry):
    r, em, conn = attach(w, registry, run_state="running")

    async def scenario():
        conn.gate = asyncio.Event()
        first = asyncio.ensure_future(em.emit(r.id, "run_event", DELTA))
        await asyncio.sleep(0.01)
        last = asyncio.ensure_future(em.emit_final(r.id, "cancelled"))
        await asyncio.sleep(0.01)
        assert not last.done()
        conn.gate.set()
        return await first, await last

    first, last = go(scenario())
    assert [e["seq"] for e in conn.sent] == [1, 2]
    assert last["seq"] == first["seq"] + 1


def test_final_with_a_restarted_emitter_uses_the_seeded_last_seq(w, registry):
    r, em, conn = attach(w, registry, run_state="running")
    fresh = RunEmitter()
    fresh.bind(r.id, USER, DEVICE, last_seq=7)       # what a resume handler passes: the device's last_seq
    run_manager.cancel_run(USER, r.id)
    assert go(fresh.emit_final(r.id, "cancelled"))["seq"] == 8


# ====================================== process-wide instance and registry

def test_get_emitter_returns_the_one_instance():
    assert emitter_module.get_emitter() is emitter_module.get_emitter()
    assert isinstance(emitter_module.get_emitter(), RunEmitter)


def test_importing_the_emitter_installs_nothing(monkeypatch):
    sentinel = ws_module.UnavailableHandler()
    monkeypatch.setattr(ws_module, "_HANDLER", sentinel)
    import importlib.util
    spec = importlib.util.spec_from_file_location("emitter_import_probe", emitter_module.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert ws_module.get_message_handler() is sentinel


def test_an_injected_registry_is_used_instead_of_the_process_wide_one(w):
    mine = connections.InMemoryRegistry()
    conn = StubConn()
    mine.add(conn)
    r = w.start()
    em = RunEmitter(registry=lambda: mine)
    em.bind(r.id, USER, DEVICE, last_seq=0)
    go(em.emit(r.id, "run_event", DELTA))
    assert len(conn.sent) == 1


# ====================================================== content hygiene

def test_no_content_reaches_the_log(w, registry, caplog):
    caplog.set_level(logging.DEBUG)
    r, em, conn = attach(w, registry, conn=False)
    go(em.emit(r.id, "run_event", {"type": "text_delta", "text": CANARY}))       # offline: logged
    registry.add(StubConn())
    go(em.emit(r.id, "run_event", {"type": "text_delta", "text": CANARY}))
    go(em.redeliver(r.id, stored(r.id)))
    w.redis.fail_if(lambda commands: True)
    with pytest.raises(buffer.BufferUnavailable):
        go(em.emit(r.id, "run_event", {"type": "text_delta", "text": CANARY}))
    assert CANARY not in caplog.text and bt.CANARY not in caplog.text
    mine = [rec for rec in caplog.records if rec.name == emitter_module.logger.name]
    assert mine, "the emitter should log its own offline / terminal decisions (ids only)"
    assert any(r.id in rec.getMessage() for rec in mine)


def test_error_texts_are_fixed_and_name_no_run(w, registry):
    r, em, conn = attach(w, registry)
    cases = [
        (InvalidEvent, lambda: go(em.emit(r.id, "error", DELTA))),
        (RunNotBound, lambda: go(em.emit("run-zzzzzzzz", "run_event", DELTA))),
        (BindingConflict, lambda: em.bind(r.id, OTHER_USER, DEVICE, last_seq=0)),
    ]
    for expected, call in cases:
        with pytest.raises(expected) as caught:
            call()
        assert r.id not in str(caught.value) and "run-zzzzzzzz" not in str(caught.value)
        assert USER not in str(caught.value) and DEVICE not in str(caught.value)


def test_the_errors_are_the_right_kinds():
    assert issubclass(RunNotBound, EmitterError) and issubclass(BindingConflict, EmitterError)
    assert issubclass(InvalidEvent, EmitterError) and issubclass(InvalidEvent, ValueError)


# ==================================================== through the real socket

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


class EmittingHandler(ws_module.MessageHandler):
    """A scripted stand-in for S14c-e that uses ONLY the emitter and the run manager:
    run.start creates the run and announces it, user_message streams two deltas,
    run.resume re-delivers what the device missed, and a disconnect pauses the run and
    unbinds it. It runs inside the server's event loop, like the real handlers."""

    def __init__(self, em, world):
        self.em, self.w = em, world

    async def on_message(self, conn, msg):
        run_id, t = msg["run_id"], msg["t"]
        if t == "run.start":
            self.w.start(run_id, user=conn.user_id, device=conn.device_id)
            self.em.bind(run_id, conn.user_id, conn.device_id, last_seq=0)
            await self.em.emit_state_changed(run_id, "created")
            runs.transition(conn.user_id, run_id, "running")
            await self.em.emit_state_changed(run_id, "running")
        elif t == "user_message":
            for part in ("one", "two"):
                await self.em.emit(run_id, "run_event", {"type": "text_delta", "text": part})
        elif t == "run.resume":
            last_seq = msg["payload"]["last_seq"]
            self.em.bind(run_id, conn.user_id, conn.device_id, last_seq=last_seq)
            async with self.em.exclusive(run_id) as run:
                resumed = run_manager.resume_run(user_id=conn.user_id, device_id=conn.device_id,
                                                 run_id=run_id, last_seq=last_seq)
                await run.redeliver(resumed.replay)
                await run.emit_state_changed("running", "resumed")

    async def on_disconnect(self, conn):
        for r in runs.list_open_runs(conn.user_id, conn.device_id):
            run_manager.pause_run(conn.user_id, r.id, "paused_disconnected")
            await self.em.emit_state_changed(r.id, "paused_disconnected", "device_disconnected")
            self.em.unbind(r.id)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


RUN = "run_emitter_socket_01"


def test_socket_a_device_receives_valid_contiguous_events(w, rig):
    rig.check = check_valid                          # every frame the device gets is protocol-valid
    em = RunEmitter()
    rig.use_handler(EmittingHandler(em, w))
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.start", start_payload(), run_id=RUN))
        got = [dev.recv(), dev.recv()]
        dev.send(envelope("user_message", {"text": "go"}, run_id=RUN))
        got += [dev.recv(), dev.recv()]
    assert [m["seq"] for m in got] == [1, 2, 3, 4]
    assert [m["payload"].get("state") or m["payload"]["text"] for m in got] == ["created", "running", "one", "two"]
    assert all(m["t"] == "run_event" and m["run_id"] == RUN for m in got)


def test_socket_disconnect_reconnect_resume_gets_exactly_the_missed_events(w, rig):
    rig.check = check_valid
    em = RunEmitter()
    rig.use_handler(EmittingHandler(em, w))
    credential, device_id = rig.store.add_device("user-a")

    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.start", start_payload(), run_id=RUN))
        seen = [dev.recv(), dev.recv()]
        dev.send(envelope("user_message", {"text": "go"}, run_id=RUN))
        seen += [dev.recv(), dev.recv()]
        assert [m["seq"] for m in seen] == [1, 2, 3, 4]
    # The device left after seq 4. The server announced the pause while it was gone: seq 5.
    assert not em.is_bound(RUN)                      # a paused run holds no emitter state
    assert [e["seq"] for e in stored(RUN)] == [1, 2, 3, 4, 5]

    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.resume", {"last_seq": 2}, run_id=RUN))   # the device only kept up to seq 2
        replayed = [dev.recv(), dev.recv(), dev.recv()]                 # 3, 4 (missed) and 5 (paused)
        resumed = dev.recv()                                            # 6: running, resumed
    assert [m["seq"] for m in replayed] == [3, 4, 5]
    assert replayed[0]["id"] == seen[2]["id"] and replayed[1]["id"] == seen[3]["id"]   # verbatim: same ids
    assert replayed[0] == seen[2] and replayed[1] == seen[3]
    assert replayed[2]["payload"] == {"type": "state_changed", "state": "paused_disconnected",
                                      "reason": "device_disconnected"}
    assert resumed["seq"] == 6
    assert resumed["payload"] == {"type": "state_changed", "state": "running", "reason": "resumed"}
    # The re-delivery was not stored again: the replay holds each seq once. Seq 6 (resumed)
    # is new; seq 7 is the pause the server announced when the device left the second time.
    assert [e["seq"] for e in stored(RUN)] == [1, 2, 3, 4, 5, 6, 7]
    assert stored(RUN, 2)[:3] == replayed                 # and what is stored is what was sent
