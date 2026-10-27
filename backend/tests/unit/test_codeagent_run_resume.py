"""S14e: the `run.resume` handler (codeagent/run_handlers.py) and the whole path end to end.

Same setup as the S14c and S14d tests: in-memory twins behind the real interfaces (run store,
encrypted Redis buffer, run_guard's Redis; run_manager_testkit.World), the REAL protocol/v1
validator, a stand-in connection for the unit tests, and S04's real endpoint (codeagent_testkit.Rig)
with the real MessageRouter for the socket tests.

Maps to the work package ("What" 3 run.resume, and "Done when"):
  * the ownership guard first: another device's or user's run, a missing run, an ended run are
    all the same run_not_found, and nothing is changed, bound or sent for them ... test_resume_guard_*
  * run_manager.resume_run, then the events the device missed are re-delivered verbatim, in order,
    and not stored again; then the run is announced `running` (reason resumed) ..... test_resume_replays_*
  * the on_resume hooks run after that, outside the run's lock ........................ test_resume_hooks_*
  * a run that was never paused is reattached as it is; paused_budget stays paused ... test_resume_reattach_*
  * a rebuilt buffer (the server's copy expired and the device sent its state) ........ test_resume_rebuilt_*
  * every failure leaves the run where the device can try again ........................ test_resume_failure_*
  * resumes and live events do not interleave ......................................... test_resume_concurrent_*
  * no content in the log ............................................................. test_no_content_*
  * the install registers run.resume, idempotently ................................... test_install_*
  * a scripted device: start, message, streamed events, disconnect, reconnect, resume, exactly
    the missed events; invisible to every other device and user .................... test_socket_*

Skipped as a module when jsonschema is not installed, like the other socket tests.
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

from codeagent import buffer, connections, emitter as emitter_module, router as router_module   # noqa: E402
from codeagent import run_handlers, run_manager, runs                                          # noqa: E402
from codeagent import ws as ws_module                                                          # noqa: E402
from codeagent.connections import ConnectionClosed                                             # noqa: E402
from codeagent.emitter import RunEmitter                                                       # noqa: E402
from codeagent.router import MessageRouter                                                     # noqa: E402
from codeagent.run_handlers import RunHandlers                                                 # noqa: E402
from tests.unit import runs_testkit as rt                                                      # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope, hello                                  # noqa: E402
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET                                 # noqa: E402
from tests.unit.run_manager_testkit import (                                                   # noqa: E402
    CANARY, DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, history, start_payload,
)


def go(coro):
    return asyncio.run(coro)


async def in_thread(fn, *args):
    """Wait for a threading.Event (or any blocking call) without blocking the loop."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


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

    def seqs(self):
        return [m["seq"] for m in self.sent]


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


def leave(registry, conn):
    registry.remove(conn)
    conn.closed = True


def check_valid(env):
    issues = run_manager._protocol().validate_message(env, tolerant=False)
    assert not issues, [(i.path, i.validator) for i in issues]


def resume_msg(run_id, last_seq, state=None):
    payload = {"last_seq": last_seq}
    if state is not None:
        payload["state"] = state
    return envelope("run.resume", payload, run_id=run_id)


def um(run_id, text="hi"):
    return envelope("user_message", {"text": text}, run_id=run_id)


def delta(i=0):
    return {"type": "text_delta", "text": f"d{i}"}


def stored(run_id, after=0):
    return buffer.replay_after(run_id, after)


def state_of(w, run_id):
    return w.store.rows[run_id]["state"]


def kinds(events):
    """(seq, state or event type, reason) for each stored/sent event."""
    return [(e["seq"], e["payload"].get("state") or e["payload"]["type"], e["payload"].get("reason"))
            for e in events]


async def begin(h, conn, run_id=None):
    msg = envelope("run.start", start_payload(history(1)), run_id=run_id or rt.new_run_id())
    await h.on_run_start(conn, msg)
    assert conn.errors == [], conn.errors
    return msg["run_id"]


async def paused(h, registry, deltas=3, run_id=None):
    """A run that streamed `deltas` events and whose device then left. Stored seqs: 1 created,
    2 running, 3.. the deltas, then the pause (seq 3 + deltas). Returns its id."""
    conn = online(registry)
    run_id = await begin(h, conn, run_id)
    for i in range(deltas):
        await h.emitter.emit(run_id, "run_event", delta(i))
    leave(registry, conn)
    await h.on_disconnect(conn)
    return run_id


# ============================================================ the resume itself

def test_resume_replays_what_the_device_missed_verbatim_then_announces_running(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry)                          # stored: 1..5 + pause 6
        before = stored(run_id)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 4))          # the device kept up to seq 4
        return run_id, before, conn

    run_id, before, conn = go(scenario())
    assert conn.errors == []
    assert conn.seqs() == [5, 6, 7]
    assert conn.sent[:2] == before[4:6]                             # verbatim: same ids, same everything
    assert kinds(conn.sent) == [(5, "text_delta", None), (6, "paused_disconnected", "device_disconnected"),
                                (7, "running", "resumed")]
    assert all(m["run_id"] == run_id and m["t"] == "run_event" for m in conn.sent)
    for m in conn.sent:
        check_valid(m)
    assert state_of(w, run_id) == "running"
    assert [e["seq"] for e in stored(run_id)] == [1, 2, 3, 4, 5, 6, 7]       # the replay was not stored again
    assert stored(run_id, 4)[:2] == conn.sent[:2] and stored(run_id, 6) == [conn.sent[2]]


def test_resume_from_zero_replays_the_whole_stream(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry, deltas=2)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 0))
        return conn

    conn = go(scenario())
    assert conn.seqs() == [1, 2, 3, 4, 5, 6]
    assert kinds(conn.sent)[-1] == (6, "running", "resumed")


def test_resume_with_nothing_missed_sends_only_the_running_event(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry, deltas=1)               # 1, 2, 3, pause 4
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 4))
        return run_id, conn

    run_id, conn = go(scenario())
    assert kinds(conn.sent) == [(5, "running", "resumed")]
    assert state_of(w, run_id) == "running"


def test_resume_leaves_the_run_bound_with_the_exact_last_seq_so_the_stream_goes_on(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 2))
        bound = (h.emitter.is_bound(run_id), h.emitter.last_seq(run_id))
        await h.emitter.emit(run_id, "run_event", delta(9))
        return run_id, conn, bound

    run_id, conn, bound = go(scenario())
    assert bound == (True, 7)
    assert conn.seqs() == [3, 4, 5, 6, 7, 8]                       # contiguous into the live stream
    assert conn.sent[-1]["payload"]["text"] == "d9"


def test_resume_the_device_cannot_make_the_tracker_run_ahead_of_the_stream(h, w, registry):
    """A last_seq beyond anything the server issued must not make the emitter number the end of
    the run past a gap: the tracker follows what is stored, not what the device claims."""
    async def scenario():
        run_id = await paused(h, registry, deltas=1)               # stored 1..4
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 99))
        return run_id, conn

    run_id, conn = go(scenario())
    assert conn.errors == [] and kinds(conn.sent) == [(5, "running", "resumed")]
    assert h.emitter.last_seq(run_id) == 5


def test_resume_a_paused_error_run_is_resumed_like_a_disconnect_pause(h, w, registry):
    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        runs.transition(USER, run_id, "paused_error", error_code="internal")
        h.emitter.unbind(run_id)
        again = online(registry)
        await h.on_run_resume(again, resume_msg(run_id, 2))
        return run_id, again

    run_id, again = go(scenario())
    assert kinds(again.sent) == [(3, "running", "resumed")]
    assert state_of(w, run_id) == "running" and w.store.rows[run_id]["error_code"] is None


# ------------------------------------------------------------ reattach, budget, created

def test_resume_reattach_a_run_that_is_still_running_is_attached_as_it_is(h, w, registry):
    """The device came back before the server noticed it was gone (ws.py registers the new
    connection first; the old connection's on_disconnect does nothing)."""
    async def scenario():
        old = online(registry)
        run_id = await begin(h, old)
        await h.emitter.emit(run_id, "run_event", delta(0))        # seq 3, lost with the old connection
        new = online(registry)
        old.closed = True
        await h.on_disconnect(old)                                 # superseded: nothing happens
        await h.on_run_resume(new, resume_msg(run_id, 2))
        return run_id, new

    run_id, new = go(scenario())
    assert new.errors == [] and new.seqs() == [3]                  # the missed event, and no state change
    assert state_of(w, run_id) == "running"
    assert [e["seq"] for e in stored(run_id)] == [1, 2, 3]
    assert h.emitter.is_bound(run_id)                              # it was never unbound, and still is


def test_resume_reattach_does_not_call_the_resume_hooks(h, w, registry):
    calls = []

    async def hook(conn, run):
        calls.append(run.id)

    h.add_on_resume(hook)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        await h.on_run_resume(conn, resume_msg(run_id, 0))

    go(scenario())
    assert calls == []                                             # nothing was paused: the loop never stopped


def test_resume_reattach_a_budget_paused_run_stays_paused_and_holds_no_emitter_state(h, w, registry):
    calls = []

    async def hook(conn, run):
        calls.append(run.id)

    h.add_on_resume(hook)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        await h.emitter.emit(run_id, "run_event", {"type": "state_changed", "state": "paused_budget"})
        runs.transition(USER, run_id, "paused_budget")
        h.emitter.unbind(run_id)
        again = online(registry)
        await h.on_run_resume(again, resume_msg(run_id, 2))
        return run_id, again

    run_id, again = go(scenario())
    assert again.errors == [] and again.seqs() == [3]              # attached and told what it missed
    assert state_of(w, run_id) == "paused_budget"                  # the budget decides, not the device
    assert not h.emitter.is_bound(run_id) and calls == []
    assert [e["seq"] for e in stored(run_id)] == [1, 2, 3]         # no event was invented


def test_resume_a_created_run_is_completed_to_running(h, w, registry):
    """A run.start that was interrupted by a superseded connection leaves the run `created`;
    resuming it finishes what run.start was doing."""
    calls = []

    async def hook(conn, run):
        calls.append(run.id)

    h.add_on_resume(hook)
    run = w.run_at("created", "run_created_0001")

    async def scenario():
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run.id, 0))
        return conn

    conn = go(scenario())
    assert conn.errors == [] and kinds(conn.sent) == [(1, "running", None)]
    assert state_of(w, run.id) == "running" and calls == []        # no loop was ever started for it


# ------------------------------------------------------------ rebuilt buffer

def test_resume_rebuilt_from_the_devices_state_when_the_servers_buffer_is_gone(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry)                          # stored 1..6
        buffer.delete_buffer(run_id)                                # the buffer expired
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 6, start_payload(history(2, "device"))))
        return run_id, conn

    run_id, conn = go(scenario())
    assert conn.errors == []
    assert kinds(conn.sent) == [(7, "running", "resumed")]         # the counter continues from last_seq
    assert state_of(w, run_id) == "running"
    items = repr(buffer.read_conversation(run_id).items)
    assert "device 0 u" in items and "turn 0 u" not in items       # the device's log is the truth
    assert h.emitter.last_seq(run_id) == 7


def test_resume_rebuilt_but_the_state_is_ignored_while_the_buffer_is_alive(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry, deltas=1)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 2, start_payload(history(2, "device"))))
        return run_id, conn

    run_id, conn = go(scenario())
    assert conn.seqs() == [3, 4, 5]                                 # the replay, as usual
    items = repr(buffer.read_conversation(run_id).items)
    assert "turn 0 u" in items and "device 0 u" not in items       # the server's copy stays


def test_resume_without_state_when_the_buffer_is_gone_is_run_not_found_and_changes_nothing(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry)
        buffer.delete_buffer(run_id)
        conn = online(registry)
        msg = resume_msg(run_id, 6)
        await h.on_run_resume(conn, msg)
        return run_id, conn, msg

    run_id, conn, msg = go(scenario())
    assert [(e["code"], e["message"], e["ref_id"]) for e in conn.errors] == \
        [("run_not_found", run_handlers.MSG_RUN_NOT_FOUND, msg["id"])]
    assert conn.sent == [] and state_of(w, run_id) == "paused_disconnected"
    assert not buffer.exists(run_id) and not h.emitter.is_bound(run_id)


@pytest.mark.parametrize("bad", [
    {"profile": "code"},                                            # not a run.start payload
    start_payload(history(1), profile="chat"),                      # another profile than the run's
])
def test_resume_a_state_that_is_not_valid_is_invalid_message_and_changes_nothing(h, w, registry, bad):
    async def scenario():
        run_id = await paused(h, registry)
        buffer.delete_buffer(run_id)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 6, bad))
        return run_id, conn

    run_id, conn = go(scenario())
    assert [(e["code"], e["message"]) for e in conn.errors] == [("invalid_message", run_handlers.MSG_NOT_ACCEPTED)]
    assert state_of(w, run_id) == "paused_disconnected" and not buffer.exists(run_id)
    assert not h.emitter.is_bound(run_id) and conn.sent == []


def test_resume_rebuilt_for_a_running_run_the_server_forgot_numbers_the_end_from_last_seq(h, w, registry):
    """A restarted server knows nothing about a running run, and its buffer expired. The device's
    state rebuilds it; no state is announced (nothing was paused), so the emitter's last seq must
    already be the device's last_seq for the cancel's final event to be numbered after it."""
    async def scenario():
        old = online(registry)
        run_id = await begin(h, old)
        h.emitter.unbind(run_id)                                    # the restart
        buffer.delete_buffer(run_id)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 5, start_payload(history(1, "device"))))
        tracker = h.emitter.last_seq(run_id)
        await h.on_cancel_request(conn, envelope("cancel", {}, run_id=run_id))
        return run_id, conn, tracker

    run_id, conn, tracker = go(scenario())
    assert conn.errors == [] and tracker == 5
    assert kinds(conn.sent) == [(6, "cancelled", "cancelled_by_device")]
    assert state_of(w, run_id) == "cancelled"


def test_resume_a_run_released_while_the_resume_waited_for_its_lock_is_run_not_found(h, w, registry):
    """The loop finishing, or a cancel, at the same moment: what the guard would answer an
    instant later."""
    async def scenario():
        old = online(registry)
        run_id = await begin(h, old)
        conn = online(registry)
        msg = resume_msg(run_id, 0)
        async with h.emitter.exclusive(run_id):
            resuming = asyncio.create_task(h.on_run_resume(conn, msg))
            await asyncio.sleep(0.05)                               # it is waiting for the lock
            h.emitter.unbind(run_id)
        await resuming
        return run_id, conn, msg

    run_id, conn, msg = go(scenario())
    assert [(e["code"], e["message"], e["ref_id"]) for e in conn.errors] == \
        [("run_not_found", run_handlers.MSG_RUN_NOT_FOUND, msg["id"])]
    assert conn.sent == [] and not h.emitter.is_bound(run_id)


# ------------------------------------------------------------ hooks

def test_resume_hooks_run_after_the_events_are_delivered_with_the_resumed_run(h, w, registry):
    seen = []

    async def hook(conn, run):
        seen.append((conn.seqs(), run.state, state_of(w, run.id), run.id))

    h.add_on_resume(hook)

    async def scenario():
        run_id = await paused(h, registry)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 4))
        return run_id

    run_id = go(scenario())
    assert seen == [([5, 6, 7], "running", "running", run_id)]


def test_resume_hooks_may_emit_through_the_emitter_they_are_not_called_inside_the_runs_lock(h, w, registry):
    async def hook(conn, run):
        await h.emitter.emit(run.id, "run_event", delta(5))        # would deadlock inside the lock

    h.add_on_resume(hook)

    async def scenario():
        run_id = await paused(h, registry, deltas=1)
        conn = online(registry)
        await asyncio.wait_for(h.on_run_resume(conn, resume_msg(run_id, 4)), timeout=3)
        return conn

    conn = go(scenario())
    assert conn.seqs() == [5, 6]                                    # running, then what the hook emitted
    assert conn.sent[1]["payload"]["text"] == "d5"


def test_resume_a_hook_that_raises_or_hangs_does_not_undo_the_resume_and_the_next_one_runs(w, registry, caplog):
    h = make(hook_timeout=0.05)
    order = []

    async def broken(conn, run):
        order.append("broken")
        raise RuntimeError(CANARY)

    async def hangs(conn, run):
        order.append("hangs")
        await asyncio.sleep(30)

    async def fine(conn, run):
        order.append("fine")

    for hook in (broken, hangs, fine):
        h.add_on_resume(hook)

    async def scenario():
        run_id = await paused(h, registry)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 0))
        return run_id, conn

    with caplog.at_level(logging.DEBUG):
        run_id, conn = go(scenario())
    assert order == ["broken", "hangs", "fine"] and conn.errors == []
    assert state_of(w, run_id) == "running"
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert CANARY not in caplog.text


# ------------------------------------------------------------ the guard

def test_resume_guard_every_reason_is_the_same_run_not_found_and_nothing_is_touched(h, w, registry):
    async def scenario():
        mine = await paused(h, registry)
        ended = await paused(h, registry)
        runs.transition(USER, ended, "cancelled")
        answers = []
        before = (state_of(w, mine), stored(mine))
        for who, run_id in (
            (StubConn(USER, OTHER_DEVICE), mine),                  # a sibling device
            (StubConn(OTHER_USER, DEVICE), mine),                  # another user, same device id
            (StubConn(), "run_never_existed_1"),                   # no such run
            (StubConn(), ended),                                   # over
            (StubConn(), "not a run id!"),                         # not even well formed
        ):
            registry.add(who)
            msg = resume_msg(run_id, 0, start_payload(history(1, "intruder")))
            await h.on_run_resume(who, msg)
            assert who.sent == [] and len(who.errors) == 1
            err = who.errors[0]
            answers.append((err["code"], err["message"], err["details"], err["ref_id"] == msg["id"]))
        assert (state_of(w, mine), stored(mine)) == before          # nothing changed for the real run
        assert not h.emitter.is_bound(mine) and h.emitter.bound_runs() == ()
        return answers

    answers = go(scenario())
    assert set(answers) == {("run_not_found", run_handlers.MSG_RUN_NOT_FOUND, None, True)}


def test_resume_guard_a_foreign_run_is_never_rebuilt_from_the_foreign_state(h, w, registry):
    async def scenario():
        mine = await paused(h, registry)
        buffer.delete_buffer(mine)
        intruder = online(registry, USER, OTHER_DEVICE)
        await h.on_run_resume(intruder, resume_msg(mine, 0, start_payload(history(1, "intruder"))))
        return mine, intruder

    mine, intruder = go(scenario())
    assert intruder.errors[0]["code"] == "run_not_found"
    assert not buffer.exists(mine) and state_of(w, mine) == "paused_disconnected"


# ------------------------------------------------------------ failures

def test_resume_failure_the_parallel_run_cap_is_rate_limited_and_the_run_stays_paused(w, registry):
    h = make(max_parallel=1)

    async def scenario():
        paused_id = await paused(h, registry)                       # frees its slot
        other = online(registry)
        await begin(h, other)                                       # takes the device's only slot
        conn = online(registry)
        msg = resume_msg(paused_id, 0)
        await h.on_run_resume(conn, msg)
        return paused_id, conn, msg

    paused_id, conn, msg = go(scenario())
    assert [(e["code"], e["message"], e["ref_id"], e["details"]) for e in conn.errors] == \
        [("rate_limited", run_handlers.MSG_TOO_MANY_RUNS, msg["id"], {"retry_after_s": run_handlers.RETRY_AFTER_SECONDS})]
    assert conn.sent == [] and state_of(w, paused_id) == "paused_disconnected"
    assert not h.emitter.is_bound(paused_id)


@pytest.mark.parametrize("exc, code", [
    (lambda: runs.RunStoreUnavailable(), "internal"),
    (lambda: buffer.BufferUnavailable("redis down"), "internal"),
    (lambda: run_manager.RunGone(), "run_not_found"),
    (lambda: run_manager.InvalidRunState(["state: type"]), "invalid_message"),
])
def test_resume_failure_before_the_move_is_the_mapped_error_and_the_run_is_unbound(h, w, registry, monkeypatch, exc, code):
    async def scenario():
        run_id = await paused(h, registry)

        def broken(**kw):
            raise exc()

        monkeypatch.setattr(run_manager, "resume_run", broken)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 0))
        return run_id, conn

    run_id, conn = go(scenario())
    assert [e["code"] for e in conn.errors] == [code] and conn.sent == []
    assert state_of(w, run_id) == "paused_disconnected" and not h.emitter.is_bound(run_id)


def test_resume_failure_an_error_of_another_family_reaches_the_router(h, w, registry, monkeypatch):
    async def scenario():
        run_id = await paused(h, registry)

        def broken(**kw):
            raise RuntimeError("a bug")

        monkeypatch.setattr(run_manager, "resume_run", broken)
        conn = online(registry)
        with pytest.raises(RuntimeError):
            await h.on_run_resume(conn, resume_msg(run_id, 0))
        return run_id, conn

    run_id, conn = go(scenario())
    assert conn.errors == [] and not h.emitter.is_bound(run_id)
    assert state_of(w, run_id) == "paused_disconnected"


@pytest.mark.parametrize("pause", ["paused_disconnected", "paused_error"])
def test_resume_failure_after_the_move_puts_the_run_back_so_the_device_can_try_again(h, w, registry, monkeypatch, pause):
    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        for i in range(2):
            await h.emitter.emit(run_id, "run_event", delta(i))
        runs.transition(USER, run_id, pause, error_code="internal" if pause == "paused_error" else None)
        h.emitter.unbind(run_id)
        leave(registry, conn)

        real = emitter_module.ExclusiveRun.emit_state_changed

        async def broken(self, *a, **kw):
            raise buffer.BufferUnavailable("redis down")

        monkeypatch.setattr(emitter_module.ExclusiveRun, "emit_state_changed", broken)
        first = online(registry)
        await h.on_run_resume(first, resume_msg(run_id, 0))
        after_failure = (state_of(w, run_id), w.store.rows[run_id]["error_code"],
                         h.emitter.is_bound(run_id), len(stored(run_id)))
        monkeypatch.setattr(emitter_module.ExclusiveRun, "emit_state_changed", real)
        leave(registry, first)
        second = online(registry)
        await h.on_run_resume(second, resume_msg(run_id, 0))
        return run_id, first, after_failure, second

    run_id, first, after_failure, second = go(scenario())
    assert [e["code"] for e in first.errors] == ["internal"]
    assert after_failure == (pause, "internal" if pause == "paused_error" else None, False, 4)
    assert second.errors == [] and kinds(second.sent)[-1] == (5, "running", "resumed")   # exactly one `running`
    assert state_of(w, run_id) == "running"


def test_resume_failure_a_hook_does_not_run_when_the_resume_failed(h, w, registry, monkeypatch):
    calls = []

    async def hook(conn, run):
        calls.append(run.id)

    h.add_on_resume(hook)

    async def scenario():
        run_id = await paused(h, registry)

        async def broken(self, *a, **kw):
            raise buffer.BufferUnavailable("redis down")

        monkeypatch.setattr(emitter_module.ExclusiveRun, "emit_state_changed", broken)
        await h.on_run_resume(online(registry), resume_msg(run_id, 0))

    go(scenario())
    assert calls == []


def test_resume_failure_a_created_run_that_cannot_be_announced_is_failed_like_run_start(h, w, registry, monkeypatch):
    run = w.run_at("created", "run_created_0002")

    async def scenario():
        async def broken(self, *a, **kw):
            raise buffer.BufferUnavailable("redis down")

        monkeypatch.setattr(emitter_module.ExclusiveRun, "emit_state_changed", broken)
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run.id, 0))
        return conn

    conn = go(scenario())
    assert [e["code"] for e in conn.errors] == ["internal"]
    assert state_of(w, run.id) == "failed" and not buffer.exists(run.id) and not h.emitter.is_bound(run.id)


def test_resume_a_device_that_leaves_during_the_replay_is_not_an_error(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry)
        conn = online(registry)
        real = conn.send_json

        async def leaves_after_two(env):
            if len(conn.sent) == 2:
                registry.remove(conn)
                conn.closed = True
            await real(env)

        conn.send_json = leaves_after_two
        await h.on_run_resume(conn, resume_msg(run_id, 0))
        return run_id, conn

    run_id, conn = go(scenario())
    assert conn.errors == [] and conn.seqs() == [1, 2]
    assert state_of(w, run_id) == "running"                         # the disconnect handler pauses it (below)
    go(h.on_disconnect(conn))                                       # ws.py calls this after the receive loop ends
    assert state_of(w, run_id) == "paused_disconnected" and not h.emitter.is_bound(run_id)


# ------------------------------------------------------------ concurrency

def test_resume_concurrent_two_resumes_of_one_run_announce_running_once_and_call_the_hooks_once(h, w, registry):
    calls = []

    async def hook(conn, run):
        calls.append(run.id)

    h.add_on_resume(hook)

    async def scenario():
        run_id = await paused(h, registry)
        conn = online(registry)
        await asyncio.gather(h.on_run_resume(conn, resume_msg(run_id, 0)),
                             h.on_run_resume(conn, resume_msg(run_id, 0)))
        return run_id, conn

    run_id, conn = go(scenario())
    assert conn.errors == [] and calls == [calls[0]]
    resumed = [e for e in stored(run_id) if e["payload"].get("reason") == "resumed"]
    assert len(resumed) == 1
    assert [e["seq"] for e in stored(run_id)] == [1, 2, 3, 4, 5, 6, 7]
    by_seq = {}
    for m in conn.sent:
        assert by_seq.setdefault(m["seq"], m) == m                  # a duplicate is the same event
    assert sorted(by_seq) == [1, 2, 3, 4, 5, 6, 7]


def test_resume_concurrent_a_live_event_never_lands_between_the_replay_and_the_running_event(w, registry, monkeypatch):
    h = make()
    gate, entered = threading.Event(), threading.Event()
    real = run_manager.resume_run

    def slow(**kw):
        entered.set()
        gate.wait(5)
        return real(**kw)

    async def scenario():
        old = online(registry)
        run_id = await begin(h, old)                                # running and bound
        await h.emitter.emit(run_id, "run_event", delta(0))         # seq 3
        new = online(registry)
        old.closed = True
        monkeypatch.setattr(run_manager, "resume_run", slow)
        resuming = asyncio.create_task(h.on_run_resume(new, resume_msg(run_id, 0)))
        await in_thread(entered.wait, 5)                            # it holds the lock, waiting in the thread
        emitting = asyncio.create_task(h.emitter.emit(run_id, "run_event", delta(1)))
        await asyncio.sleep(0.05)
        assert not emitting.done() and new.sent == []               # the live event waits for the resume
        gate.set()
        await asyncio.gather(resuming, emitting)
        return new

    new = go(scenario())
    assert new.seqs() == [1, 2, 3, 4]
    assert new.sent[3]["payload"]["text"] == "d1"


def test_resume_concurrent_a_handler_cancelled_after_the_move_leaves_a_run_the_disconnect_pauses_again(w, registry, monkeypatch):
    """ws.py cancels the receive loop when the connection ends, then calls on_disconnect. The
    thread that moved the run cannot be cancelled, so the run IS running when the handler is
    cancelled; on_disconnect finds it open and pauses it, which is the only thing that can."""
    h = make()
    gate, entered = threading.Event(), threading.Event()
    real = run_manager.resume_run

    def slow(**kw):
        entered.set()
        gate.wait(5)
        return real(**kw)

    async def scenario():
        run_id = await paused(h, registry)
        conn = online(registry)
        monkeypatch.setattr(run_manager, "resume_run", slow)
        task = asyncio.create_task(h.on_run_resume(conn, resume_msg(run_id, 0)))
        await in_thread(entered.wait, 5)                            # the run is being moved, in its thread
        leave(registry, conn)
        task.cancel()
        threading.Timer(0.05, gate.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state_of(w, run_id) == "running"
        await h.on_disconnect(conn)
        return run_id

    run_id = go(scenario())
    assert state_of(w, run_id) == "paused_disconnected" and not h.emitter.is_bound(run_id)
    assert kinds(stored(run_id))[-1] == (7, "paused_disconnected", "device_disconnected")


# ------------------------------------------------------------ after the resume

def test_resume_then_a_message_reaches_the_runner_and_cancel_ends_the_run_with_the_next_seq(h, w, registry):
    seen = []

    async def runner(conn, run, msg):
        seen.append(msg["payload"]["text"])
        await h.emitter.emit(run.id, "run_event", delta(7))

    h.set_turn_runner(runner)

    async def scenario():
        run_id = await paused(h, registry, deltas=1)               # 1..3, pause 4
        conn = online(registry)
        await h.on_run_resume(conn, resume_msg(run_id, 4))         # running: 5
        await h.on_user_message(conn, um(run_id, "again"))
        await h.wait_idle(run_id)                                  # delta: 6
        await h.on_cancel_request(conn, envelope("cancel", {}, run_id=run_id))
        return run_id, conn

    run_id, conn = go(scenario())
    assert seen == ["again"] and conn.errors == []
    assert kinds(conn.sent) == [(5, "running", "resumed"), (6, "text_delta", None), (7, "cancelled", "cancelled_by_device")]
    assert state_of(w, run_id) == "cancelled" and w.redis.keys() == []


def test_resume_a_second_disconnect_pauses_the_resumed_run_again_and_a_second_resume_works(h, w, registry):
    async def scenario():
        run_id = await paused(h, registry, deltas=1)               # 1..3, pause 4
        first = online(registry)
        await h.on_run_resume(first, resume_msg(run_id, 4))        # 5 running
        leave(registry, first)
        await h.on_disconnect(first)                               # 6 pause
        second = online(registry)
        await h.on_run_resume(second, resume_msg(run_id, 5))
        return run_id, second

    run_id, second = go(scenario())
    assert kinds(second.sent) == [(6, "paused_disconnected", "device_disconnected"), (7, "running", "resumed")]
    assert state_of(w, run_id) == "running"


# ============================================================ no content in the log

def test_no_content_reaches_the_log(h, w, registry, caplog, monkeypatch):
    async def hook(conn, run):
        raise RuntimeError(CANARY)

    h.add_on_resume(hook)

    async def scenario():
        conn = online(registry)
        run_id = await begin(h, conn)
        await h.emitter.emit(run_id, "run_event", {"type": "text_delta", "text": CANARY})
        leave(registry, conn)
        await h.on_disconnect(conn)
        buffer.delete_buffer(run_id)                                # so the device's state is used
        again = online(registry)
        state = start_payload(history(1, CANARY), instructions=CANARY)
        await h.on_run_resume(again, resume_msg(run_id, 4, state))
        bad = online(registry)
        await h.on_run_resume(bad, resume_msg(run_id, 0, {"profile": CANARY}))      # refused path too
        return run_id

    with caplog.at_level(logging.DEBUG):
        go(scenario())
    assert CANARY not in caplog.text
    assert any("run resumed" in r.getMessage() for r in caplog.records)


# ============================================================ install

@pytest.fixture
def fresh_router(monkeypatch):
    fresh = MessageRouter()
    monkeypatch.setattr(router_module, "_ROUTER", fresh)
    monkeypatch.setattr(ws_module, "_HANDLER", ws_module.UnavailableHandler())
    return fresh


ALL_TYPES = ("cancel", "run.resume", "run.start", "user_message")


def test_install_registers_run_resume_with_the_other_run_handlers(fresh_router):
    handlers = RunHandlers(emitter=RunEmitter())
    run_handlers.install(handlers=handlers)
    assert fresh_router.registered_types() == ALL_TYPES
    assert dict(handlers.registrations())["run.resume"] == handlers.on_run_resume
    assert ws_module.get_message_handler() is fresh_router


def test_install_is_idempotent_for_the_process_wide_handlers_and_router(fresh_router):
    first = run_handlers.install()
    snapshot = (fresh_router.registered_types(), len(fresh_router._disconnect_listeners))
    again = run_handlers.install()
    assert first is again is run_handlers.get_handlers()
    assert (fresh_router.registered_types(), len(fresh_router._disconnect_listeners)) == snapshot
    assert snapshot == (ALL_TYPES, 1) and ws_module.get_message_handler() is fresh_router


def test_install_names_every_client_message_type_still_without_a_handler(fresh_router):
    """What the router would still answer `internal` for is listed here, so a new protocol
    message type cannot slip in unnoticed: approval, tool_result
    and tool_stream (S09); hello and ping are answered by ws.py itself."""
    run_handlers.install()
    client_types = set(ws_module._client_types(ws_module.load_protocol()))
    assert client_types - set(fresh_router.registered_types()) <= {"approval", "tool_result", "tool_stream", "hello", "ping"}


# ============================================================ through the socket

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def wait_for(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), what


class FakeTurnRunner:
    """The fake turn runner of the work package: streams two deltas and a text_done through
    the emitter, as the loop will. `hold` keeps a turn open; a cancellation is recorded."""

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


class Tape:
    """A device that writes down every frame it sends and receives, in order, for the
    protocol's own transcript checker."""

    def __init__(self, dev, tape):
        self.dev, self.tape = dev, tape

    def send(self, msg):
        self.tape.append(msg)
        self.dev.send(msg)

    def recv(self):
        msg = self.dev.recv()
        self.tape.append(msg)
        return msg

    def __getattr__(self, name):
        return getattr(self.dev, name)


RUN = "run_resume_socket_01"


@pytest.fixture
def wired(rig, w):
    handlers = RunHandlers(emitter=RunEmitter())
    router = MessageRouter()
    run_handlers.install(router, handlers=handlers)
    rig.use_handler(router)
    rig.check = check_valid
    return handlers


def start_over_the_wire(dev, run_id=RUN):
    dev.send(envelope("run.start", start_payload(), run_id=run_id))
    return [dev.recv(), dev.recv()]


def session_on_tape(rig, credential, tape):
    """rig.session(), with the hello/welcome of the connection written on the tape too."""
    class _Ctx:
        def __enter__(self_inner):
            tape.append(hello(credential))                          # what the device sent (same shape)
            self_inner.cm = rig.session(credential)
            dev, welcome = self_inner.cm.__enter__()
            tape.append(welcome)
            return Tape(dev, tape), welcome

        def __exit__(self_inner, *exc):
            return self_inner.cm.__exit__(*exc)
    return _Ctx()


def test_socket_a_scripted_device_start_message_disconnect_reconnect_resume_gets_exactly_the_missed_events(rig, w, wired):
    """THE DONE-WHEN: a scripted device starts a run, sends a message that reaches a fake turn
    runner, receives its streamed events with increasing seq, disconnects, reconnects, resumes
    and receives exactly the missed events. The whole conversation, both directions, is also
    handed to the protocol's own transcript checker."""
    runner = FakeTurnRunner(wired)
    wired.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    tape = []

    with session_on_tape(rig, credential, tape) as (dev, _welcome):
        first = start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "one"}, run_id=RUN))
        streamed = [dev.recv(), dev.recv(), dev.recv()]
        assert [m["seq"] for m in first + streamed] == [1, 2, 3, 4, 5]          # increasing, no gaps
        assert [m["payload"].get("text") or m["payload"]["type"] for m in streamed] == ["re: one", "done", "text_done"]
        runner.hold = {"two"}
        dev.send(envelope("user_message", {"text": "two"}, run_id=RUN))
        wait_for(lambda: runner.started == ["one", "two"], "the second turn did not start")
    # The device left with seq 5 as the last event it received; the turn in progress was stopped
    # and the server announced the pause (seq 6) while the device was gone.
    wait_for(lambda: runner.cancelled == ["two"], "the open turn was not stopped by the disconnect")
    assert w.store.rows[RUN]["state"] == "paused_disconnected"
    assert [e["seq"] for e in buffer.replay_after(RUN, 0)] == [1, 2, 3, 4, 5, 6]

    with session_on_tape(rig, credential, tape) as (dev, _welcome):
        dev.send(envelope("run.resume", {"last_seq": 5}, run_id=RUN))
        missed = dev.recv()                                                     # exactly what was missed ...
        resumed = dev.recv()                                                    # ... and then the run goes on
        assert missed["seq"] == 6
        assert missed["payload"] == {"type": "state_changed", "state": "paused_disconnected",
                                     "reason": "device_disconnected"}
        assert missed == buffer.replay_after(RUN, 5)[0]                         # verbatim: the stored envelope
        assert resumed["seq"] == 7
        assert resumed["payload"] == {"type": "state_changed", "state": "running", "reason": "resumed"}
        runner.hold = set()
        dev.send(envelope("user_message", {"text": "three"}, run_id=RUN))       # the resumed run takes turns again
        more = [dev.recv(), dev.recv(), dev.recv()]
        assert [m["seq"] for m in more] == [8, 9, 10]
        dev.ping_pong()
    assert runner.started == ["one", "two", "three"] and runner.finished == ["one", "three"]
    wait_for(lambda: w.store.rows[RUN]["state"] == "paused_disconnected", "the second disconnect did not pause the run")

    # The checker's "timestamps do not go backwards" rule is written for synthetic fixtures. A
    # real replay is verbatim (protocol README, 5), so the missed event carries the time it was
    # first made, which is earlier than the resume that asked for it. Only `ts` is renumbered
    # here; ids, seqs, types, run ids and payloads, which every other rule reads, are untouched.
    frames = [dict(m, ts=f"2026-01-01T00:00:{i // 1000:02d}.{i % 1000:03d}Z")
              for i, m in enumerate(m for m in tape if m["t"] not in ("ping", "pong"))]
    assert ws_module.load_protocol().check_transcript(frames) == []


def test_socket_a_device_that_never_read_the_events_gets_all_of_them_again_on_resume(rig, w, wired):
    """The events were stored and sent to a connection nobody read; last_seq says what the
    device really has."""
    runner = FakeTurnRunner(wired)
    wired.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "one"}, run_id=RUN))
        wait_for(lambda: runner.finished == ["one"], "the turn did not finish")
    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.resume", {"last_seq": 2}, run_id=RUN))
        got = [dev.recv() for _ in range(5)]                                    # 3, 4, 5 and the pause 6, running 7
    assert [m["seq"] for m in got] == [3, 4, 5, 6, 7]
    assert got[:4] == buffer.replay_after(RUN, 2)[:4]


def test_socket_resume_when_the_device_came_back_before_the_server_noticed(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (first, _w1):
        start_over_the_wire(first)
        with rig.session(credential) as (second, _w2):                          # the same device connects again
            assert first.expect_close() == 4409
            wait_for(lambda: 1 in rig.finished, "the superseded connection's cleanup did not finish")
            assert w.store.rows[RUN]["state"] == "running"
            second.send(envelope("run.resume", {"last_seq": 1}, run_id=RUN))
            got = second.recv()                                                 # seq 2 (running), which it missed
            assert got["seq"] == 2 and got["payload"]["state"] == "running"
            assert w.store.rows[RUN]["state"] == "running"                      # reattached, nothing invented
            second.ping_pong()
    assert [e["seq"] for e in buffer.replay_after(RUN, 0)] == [1, 2, 3]         # 3 = the pause when it left


def test_socket_resume_rebuilds_a_run_whose_buffer_expired_from_the_devices_state(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
    assert buffer.exists(RUN)
    buffer.delete_buffer(RUN)                                                   # the idle expiry
    with rig.session(credential) as (dev, _welcome):
        plain = envelope("run.resume", {"last_seq": 3}, run_id=RUN)
        dev.send(plain)
        err = dev.recv_error()
        assert err["code"] == "run_not_found" and err["ref_id"] == plain["id"]  # no state, no run
        dev.send(envelope("run.resume", {"last_seq": 3, "state": start_payload(history(2, "device"))}, run_id=RUN))
        resumed = dev.recv()
        assert resumed["seq"] == 4 and resumed["payload"]["reason"] == "resumed"
        dev.ping_pong()
    assert w.store.rows[RUN]["state"] in ("running", "paused_disconnected")


def test_socket_a_run_is_invisible_to_every_other_device_and_user_for_run_resume_too(rig, w, wired):
    runner = FakeTurnRunner(wired)
    wired.set_turn_runner(runner)
    cred_a, _ = rig.store.add_device("user-a")
    cred_sibling, _ = rig.store.add_device("user-a")
    cred_b, _ = rig.store.add_device("user-b")
    with rig.session(cred_a) as (owner, _w1):
        start_over_the_wire(owner)
        owner.send(envelope("user_message", {"text": "secret plans"}, run_id=RUN))
        [owner.recv() for _ in range(3)]
    for credential in (cred_sibling, cred_b):
        with rig.session(credential) as (other, _w2):
            answers = []
            for run_id in (RUN, "run_never_existed_1"):
                msg = envelope("run.resume", {"last_seq": 0, "state": start_payload(history(1, "mine"))}, run_id=run_id)
                other.send(msg)
                err = other.recv_error()                                        # an error, never an event
                answers.append((err["code"], err["message"], err["ref_id"] == msg["id"]))
            assert set(answers) == {("run_not_found", run_handlers.MSG_RUN_NOT_FOUND, True)}
            other.ping_pong()                                                   # and nothing else was queued for it
    assert w.store.rows[RUN]["state"] == "paused_disconnected"                  # the owner's run was not touched
    assert [e["seq"] for e in buffer.replay_after(RUN, 0)] == [1, 2, 3, 4, 5, 6]


def test_socket_resume_of_an_ended_run_is_run_not_found(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("cancel", {}, run_id=RUN))
        assert dev.recv()["payload"]["state"] == "cancelled"
        msg = envelope("run.resume", {"last_seq": 0}, run_id=RUN)
        dev.send(msg)
        err = dev.recv_error()
        assert err["code"] == "run_not_found" and err["ref_id"] == msg["id"]
        dev.ping_pong()


def test_socket_the_install_is_idempotent_and_the_real_router_serves_run_resume(rig, w, monkeypatch):
    """Installing twice (a reloaded app, a second import) must not double-register or double-pause."""
    fresh = MessageRouter()
    monkeypatch.setattr(router_module, "_ROUTER", fresh)
    handlers = RunHandlers(emitter=RunEmitter())
    run_handlers.install(handlers=handlers)
    run_handlers.install(handlers=handlers)
    assert ws_module.get_message_handler() is fresh and len(fresh._disconnect_listeners) == 1
    rig.check = check_valid
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.resume", {"last_seq": 2}, run_id=RUN))
        assert [dev.recv()["seq"], dev.recv()["seq"]] == [3, 4]                 # one pause, one `running`
    wait_for(lambda: w.store.rows[RUN]["state"] == "paused_disconnected", "the last disconnect did not pause the run")
    assert [e["seq"] for e in buffer.replay_after(RUN, 0)] == [1, 2, 3, 4, 5]   # never two pauses


# ============================================================ a cancelled handler waits for its thread

def test_a_cancelled_run_start_is_finished_in_the_store_before_the_disconnect_looks_at_it(w, registry, monkeypatch):
    """ws.py cancels the receive loop and only then calls on_disconnect. start_run is
    synchronous and runs in a thread that cannot be stopped: without _thread() the run would be
    recorded AFTER on_disconnect had found nothing to pause, and sit `created` for good."""
    h = make()
    gate, entered = threading.Event(), threading.Event()
    real = run_manager.start_run

    def slow(**kw):
        entered.set()
        gate.wait(5)
        return real(**kw)

    async def scenario():
        monkeypatch.setattr(run_manager, "start_run", slow)
        conn = online(registry)
        msg = envelope("run.start", start_payload(history(1)), run_id="run_cancelled_start_1")
        task = asyncio.create_task(h.on_run_start(conn, msg))
        await in_thread(entered.wait, 5)
        leave(registry, conn)
        task.cancel()
        threading.Timer(0.05, gate.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state_of(w, "run_cancelled_start_1") == "created"       # recorded before the cancellation completed
        await h.on_disconnect(conn)

    go(scenario())
    assert state_of(w, "run_cancelled_start_1") == "paused_disconnected"


def test_thread_waits_for_the_call_when_cancelled_and_does_not_leave_an_unretrieved_exception(recwarn):
    gate = threading.Event()

    def work():
        gate.wait(5)
        raise RuntimeError("late failure")

    async def scenario():
        task = asyncio.create_task(run_handlers._thread(work))
        await asyncio.sleep(0.02)
        task.cancel()
        threading.Timer(0.05, gate.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)

    go(scenario())
    assert not [x for x in recwarn if "never retrieved" in str(x.message)]


def test_thread_returns_the_result_and_raises_the_calls_own_exception_when_not_cancelled():
    async def scenario():
        assert await run_handlers._thread(lambda a, b=1: a + b, 2, b=3) == 5
        with pytest.raises(ValueError):
            await run_handlers._thread(int, "x")

    go(scenario())
