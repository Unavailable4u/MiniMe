"""S14c: the run handlers' base (codeagent/run_handlers.py) -- the ownership guard, the error
map, the hook registries, `run.start`, and the disconnect handler.

Everything runs against the in-memory twins behind the real interfaces (run store, encrypted
Redis buffer, run_guard's Redis; tests/unit/run_manager_testkit.World) and the REAL protocol/v1
validator. Two layers:

  * unit tests call the handlers directly with a stand-in connection that records what it was
    sent (frames and errors) and sits in an in-memory registry, so "the device got seq 1 and 2"
    and "the registry holds a newer connection" are exact;
  * socket tests go through S04's real endpoint (tests/unit/codeagent_testkit.Rig) with the
    real MessageRouter, every frame the device receives checked against protocol/v1: a run is
    started over the wire, the device leaves, and the run is paused with the pause in its
    replay; a reconnect does NOT pause its own run.

Maps to the work package ("What" 3, first and last bullets, and 4, 5):
  * every handler first checks the run belongs to this user AND device; anything else is
    run_not_found and never reveals whether the run exists .......... test_guard_*
  * S06's errors are mapped with run_manager's table ................... test_error_map_*
  * run.start: start_run, a parallel-run cap that tests can inject, created -> running, the
    state_changed events (seq 1, 2) ...................................... test_run_start_*
  * on_disconnect: run-disconnect hooks first, then pause_run, then state_changed in the
    replay; a superseded connection pauses nothing ....................... test_disconnect_*
  * set_turn_runner / add_on_cancel / add_on_resume / add_on_run_disconnect  test_registry_*
  * the install is idempotent ............................................. test_install_*
  * no content in the log ................................................. test_no_content_*

What is NOT here: user_message and cancel (S14d, tests/unit/test_codeagent_run_turns.py) and
run.resume and the end-to-end test (S14e, tests/unit/test_codeagent_run_resume.py).

Skipped as a module when jsonschema is not installed, like the other socket tests.
"""
import asyncio
import functools
import logging
import re
import time

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed; install protocol/requirements.txt to run the "
           "device-socket tests (see codeagent/ws.py, 'Runtime needs')",
)

from codeagent import buffer, connections, emitter as emitter_module, router as router_module   # noqa: E402
from codeagent import run_handlers, run_manager, runs, ws as ws_module                           # noqa: E402
from codeagent.connections import ConnectionClosed                                               # noqa: E402
from codeagent.emitter import RunEmitter                                                         # noqa: E402
from codeagent.router import MessageRouter                                                       # noqa: E402
from codeagent.run_handlers import RunHandlers                                                   # noqa: E402
from tests.unit import runs_testkit as rt                                                        # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope                                           # noqa: E402
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET                                   # noqa: E402
from tests.unit.run_manager_testkit import (                                                     # noqa: E402
    CANARY, DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, history, start_payload,
)


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
    """A DeviceConnection stand-in: who it is, and everything it was sent."""

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


@pytest.fixture
def h(w, registry):
    return RunHandlers(emitter=RunEmitter(), max_parallel=10)


def online(registry, user=USER, device=DEVICE):
    conn = StubConn(user, device)
    registry.add(conn)
    return conn


def check_valid(env):
    issues = run_manager._protocol().validate_message(env, tolerant=False)
    assert not issues, [(i.path, i.validator) for i in issues]


def start_msg(run_id=None, payload=None, **over):
    return envelope("run.start", start_payload(history(1)) if payload is None else payload,
                    run_id=run_id or rt.new_run_id(), **over)


def do_start(h, conn, msg=None):
    msg = msg or start_msg()
    go(h.on_run_start(conn, msg))
    return msg


def stored(run_id, after=0):
    return buffer.replay_after(run_id, after)


def state_of(w, run_id):
    return w.store.rows[run_id]["state"]


def states_in(events):
    return [(e["seq"], e["payload"]["state"], e["payload"].get("reason")) for e in events
            if e["payload"].get("type") == "state_changed"]


# ======================================================== the integration points

def test_registry_turn_runner_is_one_async_function(h):
    async def runner(conn, run, msg):
        return None

    async def other(conn, run, msg):
        return None

    assert h.get_turn_runner() is None
    h.set_turn_runner(runner)
    assert h.get_turn_runner() is runner
    h.set_turn_runner(runner)                                  # the same one again: a no-op
    with pytest.raises(run_handlers.HandlerWiringError):
        h.set_turn_runner(other)                               # a second, different one is a wiring bug
    assert h.get_turn_runner() is runner
    h.set_turn_runner(other, replace=True)
    assert h.get_turn_runner() is other
    h.set_turn_runner(None)                                    # clearing
    assert h.get_turn_runner() is None


def test_registry_refuses_a_plain_function_anywhere(h):
    def sync_fn(conn, run, msg=None):
        return None

    for add in (h.set_turn_runner, h.add_on_cancel, h.add_on_resume, h.add_on_run_disconnect):
        with pytest.raises(TypeError):
            add(sync_fn)


@pytest.mark.parametrize("add_name,getter", [
    ("add_on_cancel", "on_cancel_hooks"),
    ("add_on_resume", "on_resume_hooks"),
    ("add_on_run_disconnect", "on_run_disconnect_hooks"),
])
def test_registry_hooks_keep_registration_order_and_one_copy(h, add_name, getter):
    async def first(conn, run):
        return None

    async def second(conn, run):
        return None

    add = getattr(h, add_name)
    add(first)
    add(second)
    add(first)                                                 # again: still one copy, still first
    assert getattr(h, getter)() == (first, second)


def test_registry_async_callable_objects_and_partials_are_accepted(h):
    class Hook:
        async def __call__(self, conn, run):
            return None

    async def with_arg(tag, conn, run):
        return None

    h.add_on_cancel(Hook())
    h.add_on_cancel(functools.partial(with_arg, "x"))
    assert len(h.on_cancel_hooks()) == 2


def test_the_module_level_functions_drive_the_process_wide_instance(monkeypatch):
    fresh = RunHandlers(emitter=RunEmitter())
    monkeypatch.setattr(run_handlers, "_HANDLERS", fresh)

    async def runner(conn, run, msg):
        return None

    async def hook(conn, run):
        return None

    assert run_handlers.get_handlers() is fresh
    run_handlers.set_turn_runner(runner)
    run_handlers.add_on_cancel(hook)
    run_handlers.add_on_resume(hook)
    run_handlers.add_on_run_disconnect(hook)
    assert fresh.get_turn_runner() is runner
    assert fresh.on_cancel_hooks() == fresh.on_resume_hooks() == fresh.on_run_disconnect_hooks() == (hook,)


# =========================================================== configuration

def test_the_parallel_cap_defaults_to_what_the_socket_advertises():
    assert run_handlers.MAX_PARALLEL_RUNS == ws_module.MAX_PARALLEL_RUNS
    assert run_handlers.MAX_PARALLEL_RUNS == ws_module.SETTINGS.max_parallel_runs   # advertised == enforced
    assert RunHandlers().max_parallel == ws_module.MAX_PARALLEL_RUNS


def test_the_parallel_cap_can_be_injected_by_constructor_or_by_constant(monkeypatch):
    assert RunHandlers(max_parallel=1).max_parallel == 1
    monkeypatch.setattr(run_handlers, "MAX_PARALLEL_RUNS", 7)
    assert RunHandlers().max_parallel == 7


@pytest.mark.parametrize("bad", [0, -1, True, "3", 1.5])
def test_a_bad_parallel_cap_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        RunHandlers(max_parallel=bad)


# ================================================================= error map

# Each case builds its exception when the test RUNS, not when the module is imported:
# tests/unit/test_codeagent_buffer.py reloads `buffer`, which makes new exception classes.
CASES = [
    ("RunNotFound", lambda: runs.RunNotFound(), "run_not_found"),
    ("RunNotResumable", lambda: run_manager.RunNotResumable("completed"), "run_not_found"),
    ("RunGone", lambda: run_manager.RunGone(), "run_not_found"),
    ("InvalidRunState", lambda: run_manager.InvalidRunState(["payload/mode: enum"]), "invalid_message"),
    ("RunIdCollision", lambda: runs.RunIdCollision(), "invalid_message"),
    ("InvalidRun", lambda: runs.InvalidRun("x"), "invalid_message"),
    ("InvalidBufferInput", lambda: buffer.InvalidBufferInput("x"), "invalid_message"),
    ("BufferFull", lambda: buffer.BufferFull("x"), "invalid_message"),
    ("IllegalTransition", lambda: runs.IllegalTransition("completed", "running"), "invalid_message"),
    ("ParallelRunLimit", lambda: runs.ParallelRunLimit(3), "rate_limited"),
    ("RunStoreUnavailable", lambda: runs.RunStoreUnavailable(), "internal"),
    ("TransitionConflict", lambda: runs.TransitionConflict("x"), "internal"),
    ("BufferUnavailable", lambda: buffer.BufferUnavailable("x"), "internal"),
    ("BufferNotConfigured", lambda: buffer.BufferNotConfigured("x"), "internal"),
    ("BufferMissing", lambda: buffer.BufferMissing("x"), "internal"),
    ("BufferExpired", lambda: buffer.BufferExpired("x"), "internal"),
    ("BufferExists", lambda: buffer.BufferExists("x"), "internal"),
    ("BufferCorrupt", lambda: buffer.BufferCorrupt("x"), "internal"),
    ("ProtocolSchemasUnavailable", lambda: run_manager.ProtocolSchemasUnavailable(), "internal"),
    ("CleanupIncomplete", lambda: run_manager.CleanupIncomplete(None), "internal"),
    ("RunNotEnded", lambda: run_manager.RunNotEnded("running"), "internal"),
]


@pytest.mark.parametrize("name,make,code", CASES, ids=[c[0] for c in CASES])
def test_error_map_follows_the_run_managers_table(name, make, code):
    mapped = run_handlers.map_error(make())
    assert mapped is not None and mapped[0] == code
    assert 1 <= len(mapped[1]) <= 500                         # error.json: message 1..500


def test_error_map_covers_every_error_class_the_run_manager_family_defines():
    """A new error class added to runs / run_manager / buffer must be mapped (or the router
    would answer it `internal` by accident, silently)."""
    mapped = {type(make()) for _, make, _ in CASES}
    for module in (runs, run_manager, buffer):
        for name in dir(module):
            cls = getattr(module, name)
            if (isinstance(cls, type) and issubclass(cls, Exception) and cls.__module__ == module.__name__
                    and name not in {"RunError", "RunManagerError", "RunBufferError"}):
                assert cls in mapped, f"{module.__name__}.{name} has no row in the error map test"


def test_error_map_still_matches_after_a_module_is_reloaded(monkeypatch):
    """The classes are looked up at call time: reloading buffer (what a restart test does)
    gives it new exception classes, and the map must follow them."""
    import importlib
    old = buffer.BufferUnavailable
    try:
        importlib.reload(buffer)
        assert buffer.BufferUnavailable is not old
        assert run_handlers.map_error(buffer.BufferUnavailable("x"))[0] == "internal"
        assert run_handlers.map_error(buffer.InvalidBufferInput("x"))[0] == "invalid_message"
    finally:
        importlib.reload(buffer)                               # leave a fresh, consistent module behind


def test_error_map_rate_limited_carries_retry_after_s():
    code, _, details = run_handlers.map_error(runs.ParallelRunLimit(3))
    assert code == "rate_limited"
    assert isinstance(details["retry_after_s"], int) and details["retry_after_s"] >= 0
    assert set(details) == {"retry_after_s"}                   # error.json's closed details object


def test_error_map_texts_are_fixed_and_never_the_exceptions_own():
    for exc in (runs.RunStoreUnavailable(CANARY), buffer.BufferUnavailable(CANARY),
                run_manager.InvalidRunState([CANARY]), runs.InvalidRun(CANARY), buffer.BufferFull(CANARY)):
        assert CANARY not in repr(run_handlers.map_error(exc))


def test_error_map_leaves_a_stranger_alone():
    assert run_handlers.map_error(RuntimeError("x")) is None
    assert run_handlers.map_error(KeyError("x")) is None
    assert run_handlers.map_error(ConnectionClosed("x")) is None


def test_answer_error_sends_the_mapped_error_with_the_ref_id_and_says_whether_it_was_ours(h):
    conn, msg = StubConn(), start_msg()
    assert go(h.answer_error(conn, msg, runs.ParallelRunLimit(3))) is True
    assert conn.errors[0]["code"] == "rate_limited" and conn.errors[0]["ref_id"] == msg["id"]
    assert conn.errors[0]["details"] == {"retry_after_s": run_handlers.RETRY_AFTER_SECONDS}
    assert go(h.answer_error(conn, msg, RuntimeError("x"))) is False
    assert len(conn.errors) == 1                               # a stranger sent nothing


# =============================================================== the guard

def started(h, w, registry, *, user=USER, device=DEVICE):
    conn = online(registry, user, device)
    msg = do_start(h, conn)
    return conn, msg["run_id"]


def guard_msg(run_id, t="user_message"):
    return envelope(t, {"text": "hi"}, run_id=run_id)


def test_guard_returns_the_run_for_its_own_user_and_device(h, w, registry):
    conn, run_id = started(h, w, registry)
    run = go(h.owned_run(conn, guard_msg(run_id)))
    assert run is not None and run.id == run_id and run.user_id == USER and run.device_id == DEVICE
    assert conn.errors == []


def test_guard_a_run_of_another_device_of_the_same_user_is_run_not_found(h, w, registry):
    _, run_id = started(h, w, registry)
    other = online(registry, USER, OTHER_DEVICE)
    msg = guard_msg(run_id)
    assert go(h.owned_run(other, msg)) is None
    assert [e["code"] for e in other.errors] == ["run_not_found"] and other.errors[0]["ref_id"] == msg["id"]


def test_guard_a_run_of_another_user_is_run_not_found(h, w, registry):
    _, run_id = started(h, w, registry)
    stranger = online(registry, OTHER_USER, DEVICE)            # even on a device with the same id
    assert go(h.owned_run(stranger, guard_msg(run_id))) is None
    assert [e["code"] for e in stranger.errors] == ["run_not_found"]


def test_guard_cannot_tell_a_foreign_run_from_a_missing_one_or_a_malformed_id(h, w, registry):
    """Same code, same text, same details: nothing reveals whether the run exists."""
    mine, run_id = started(h, w, registry)
    other_device = online(registry, USER, OTHER_DEVICE)
    other_user = online(registry, OTHER_USER, DEVICE)
    answers = []
    for conn, rid in ((other_device, run_id), (other_user, run_id), (other_device, rt.new_run_id()),
                      (other_device, "x"), (other_device, None), (other_device, 7), (other_device, "a b c d e f g h")):
        msg = guard_msg(run_id) if rid is None else guard_msg(rid)
        if rid is None or isinstance(rid, int):
            msg.pop("run_id", None)
        conn.errors.clear()
        assert go(h.owned_run(conn, msg)) is None
        answers.append({k: v for k, v in conn.errors[0].items() if k != "ref_id"})
    assert all(a == answers[0] for a in answers)
    assert answers[0]["code"] == "run_not_found"


def test_guard_an_ended_run_reads_as_missing_unless_asked_otherwise(h, w, registry):
    conn, run_id = started(h, w, registry)
    run_manager.end_run(USER, run_id, "completed")
    assert go(h.owned_run(conn, guard_msg(run_id))) is None
    assert conn.errors[-1]["code"] == "run_not_found"
    conn.errors.clear()
    run = go(h.owned_run(conn, guard_msg(run_id), open_only=False))
    assert run is not None and run.state == "completed" and conn.errors == []


def test_guard_a_paused_run_is_still_the_devices(h, w, registry):
    conn, run_id = started(h, w, registry)
    run_manager.pause_run(USER, run_id, "paused_budget")
    assert go(h.owned_run(conn, guard_msg(run_id))).state == "paused_budget"


def test_guard_a_store_outage_is_internal_not_run_not_found(h, w, registry, monkeypatch):
    conn, run_id = started(h, w, registry)
    monkeypatch.setattr(runs, "_STORE", rt.BoomStore())
    assert go(h.owned_run(conn, guard_msg(run_id))) is None
    assert conn.errors[-1]["code"] == "internal" and CANARY not in repr(conn.errors)


def test_guard_never_looks_at_the_payload(h, w, registry):
    conn, run_id = started(h, w, registry)
    msg = guard_msg(run_id)
    msg["payload"] = None                                      # would blow up anything that read it
    assert go(h.owned_run(conn, msg)).id == run_id


# ============================================================ run.start

def test_run_start_records_the_run_runs_it_and_announces_seq_1_and_2(h, w, registry):
    conn = online(registry)
    msg = do_start(h, conn)
    run_id = msg["run_id"]
    row = w.store.rows[run_id]
    assert row["state"] == "running" and row["user_id"] == USER and row["device_id"] == DEVICE
    assert conn.errors == []
    assert [(e["t"], e["seq"]) for e in conn.sent] == [("run_event", 1), ("run_event", 2)]
    assert [(e["payload"]["type"], e["payload"]["state"]) for e in conn.sent] == \
        [("state_changed", "created"), ("state_changed", "running")]
    for env in conn.sent:
        check_valid(env)
        assert env["run_id"] == run_id
    assert stored(run_id) == conn.sent                          # what was sent is what was stored
    assert buffer.exists(run_id) and buffer.read_conversation(run_id).items == history(1)
    assert h.emitter.is_bound(run_id)                           # the loop and the broker can emit now
    assert h.emitter.last_seq(run_id) == 2


def test_run_start_the_owner_is_the_connection_not_anything_in_the_message(h, w, registry):
    conn = online(registry, USER, DEVICE)
    msg = start_msg()
    msg["user_id"] = OTHER_USER                                 # extra fields a tolerant reader let through
    msg["device_id"] = OTHER_DEVICE
    go(h.on_run_start(conn, msg))
    row = w.store.rows[msg["run_id"]]
    assert (row["user_id"], row["device_id"]) == (USER, DEVICE)


def test_run_start_events_go_only_to_the_runs_device(h, w, registry):
    mine, other = online(registry), online(registry, USER, OTHER_DEVICE)
    do_start(h, mine)
    assert len(mine.sent) == 2 and other.sent == []


def test_run_start_with_the_device_gone_still_stores_the_events(h, w, registry):
    conn = StubConn()                                           # never added to the registry
    msg = do_start(h, conn)
    assert state_of(w, msg["run_id"]) == "running"
    assert [e["seq"] for e in stored(msg["run_id"])] == [1, 2] and conn.sent == []


def test_run_start_the_parallel_cap_is_enforced_per_device_and_answers_rate_limited(w, registry):
    h = RunHandlers(emitter=RunEmitter(), max_parallel=2)
    conn, other = online(registry), online(registry, USER, OTHER_DEVICE)
    do_start(h, conn)
    do_start(h, conn)
    third = start_msg()
    go(h.on_run_start(conn, third))
    assert [e["code"] for e in conn.errors] == ["rate_limited"]
    assert conn.errors[0]["ref_id"] == third["id"] and conn.errors[0]["details"]["retry_after_s"] >= 0
    assert third["run_id"] not in w.store.rows                  # nothing recorded
    assert not h.emitter.is_bound(third["run_id"]) and w.redis.keys() != []   # the others' buffers are intact
    do_start(h, other)                                          # another device has its own slots
    assert other.errors == []


def test_run_start_a_finished_run_frees_its_slot(w, registry):
    h = RunHandlers(emitter=RunEmitter(), max_parallel=1)
    conn = online(registry)
    first = do_start(h, conn)
    run_manager.end_run(USER, first["run_id"], "completed")
    do_start(h, conn)
    assert conn.errors == []


def test_run_start_the_cap_the_tests_inject_is_the_one_used(w, registry, monkeypatch):
    monkeypatch.setattr(run_handlers, "MAX_PARALLEL_RUNS", 1)
    h = RunHandlers(emitter=RunEmitter())
    conn = online(registry)
    do_start(h, conn)
    do_start(h, conn)
    assert [e["code"] for e in conn.errors] == ["rate_limited"]


def test_run_start_an_id_collision_is_invalid_message_and_touches_nothing(h, w, registry):
    conn = online(registry)
    first = do_start(h, conn)
    before = (list(w.store.rows), [e["seq"] for e in stored(first["run_id"])], w.store.rows[first["run_id"]]["state"])
    again = start_msg(run_id=first["run_id"])
    go(h.on_run_start(conn, again))
    assert [e["code"] for e in conn.errors] == ["invalid_message"] and conn.errors[0]["ref_id"] == again["id"]
    after = (list(w.store.rows), [e["seq"] for e in stored(first["run_id"])], w.store.rows[first["run_id"]]["state"])
    assert after == before                                      # no event, no state change


def test_run_start_another_users_run_id_is_a_collision_not_a_leak(h, w, registry):
    theirs = online(registry, OTHER_USER, OTHER_DEVICE)
    first = do_start(h, theirs)
    mine = online(registry)
    go(h.on_run_start(mine, start_msg(run_id=first["run_id"])))
    assert [e["code"] for e in mine.errors] == ["invalid_message"]
    assert w.store.rows[first["run_id"]]["user_id"] == OTHER_USER and len(theirs.sent) == 2 and mine.sent == []


def test_run_start_an_invalid_payload_is_invalid_message_and_never_echoed(h, w, registry):
    conn = online(registry)
    payload = start_payload(mode=CANARY)                        # not in the mode enum
    msg = start_msg(payload=payload)
    go(h.on_run_start(conn, msg))
    assert [e["code"] for e in conn.errors] == ["invalid_message"]
    assert CANARY not in repr(conn.errors)
    assert msg["run_id"] not in w.store.rows and w.redis.keys() == []


def test_run_start_without_the_buffer_secret_fails_closed_and_records_nothing(h, w, registry, monkeypatch):
    monkeypatch.delenv("RUN_BUFFER_SECRET", raising=False)
    conn = online(registry)
    msg = start_msg()
    go(h.on_run_start(conn, msg))
    assert [e["code"] for e in conn.errors] == ["internal"]
    assert msg["run_id"] not in w.store.rows and conn.sent == []


def test_run_start_a_buffer_fault_leaves_no_zombie_and_answers_internal(h, w, registry):
    conn = online(registry)
    w.redis.fail_if(lambda commands: True)
    msg = start_msg()
    go(h.on_run_start(conn, msg))
    assert [e["code"] for e in conn.errors] == ["internal"] and conn.sent == []
    assert state_of(w, msg["run_id"]) == "failed"               # S06 failed it; no `created` holding a slot
    assert not h.emitter.is_bound(msg["run_id"])


def test_run_start_a_store_outage_is_internal(h, w, registry, monkeypatch):
    monkeypatch.setattr(runs, "_STORE", rt.BoomStore())
    conn = online(registry)
    go(h.on_run_start(conn, start_msg()))
    assert [e["code"] for e in conn.errors] == ["internal"] and CANARY not in repr(conn.errors)


def test_run_start_failing_to_enter_running_fails_the_run_and_cleans_up(h, w, registry):
    conn = online(registry)

    def refuse_running(call):
        if call["to_state"] == "running":
            raise RuntimeError(f"db down {CANARY}")

    w.store.before_move = refuse_running
    msg = start_msg()
    go(h.on_run_start(conn, msg))
    assert [e["code"] for e in conn.errors] == ["internal"]
    assert state_of(w, msg["run_id"]) == "failed" and w.store.rows[msg["run_id"]]["error_code"] == "internal"
    assert w.redis.keys() == []                                 # the buffer went with the run
    assert not h.emitter.is_bound(msg["run_id"])
    assert CANARY not in repr(conn.errors)


def test_run_start_failing_to_emit_fails_the_run_and_cleans_up(h, w, registry, monkeypatch):
    conn = online(registry)

    async def boom(run_id, state_name, reason=None):
        raise buffer.BufferUnavailable("x")

    monkeypatch.setattr(h.emitter, "emit_state_changed", boom)
    msg = start_msg()
    go(h.on_run_start(conn, msg))
    assert [e["code"] for e in conn.errors] == ["internal"]
    assert state_of(w, msg["run_id"]) == "failed" and w.redis.keys() == []
    assert not h.emitter.is_bound(msg["run_id"])


def test_run_start_an_unexpected_bug_after_recording_still_fails_the_run_then_reaches_the_router(h, w, registry, monkeypatch):
    conn = online(registry)

    async def boom(run_id, state_name, reason=None):
        raise KeyError("a bug")

    monkeypatch.setattr(h.emitter, "emit_state_changed", boom)
    msg = start_msg()
    with pytest.raises(KeyError):
        go(h.on_run_start(conn, msg))                           # the router contains it and answers internal
    assert state_of(w, msg["run_id"]) == "failed" and w.redis.keys() == []


def test_run_start_a_cancelled_handler_leaves_a_run_the_disconnect_can_still_pause(h, w, registry, monkeypatch):
    """ws.py cancels the receive loop when the connection ends: a run.start interrupted
    between recording the run and moving it to running leaves the run in `created`."""
    conn = online(registry)
    msg = start_msg()

    async def interrupted(run_id, state_name, reason=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(h.emitter, "emit_state_changed", interrupted)
    with pytest.raises(asyncio.CancelledError):
        go(h.on_run_start(conn, msg))
    assert state_of(w, msg["run_id"]) == "created"              # not failed: cancellation is not an error
    del h.emitter.emit_state_changed                            # the real one again
    registry.remove(conn)
    go(h.on_disconnect(conn))
    assert state_of(w, msg["run_id"]) == "paused_disconnected"


# =========================================================== on_disconnect

def running(w, h, registry, n=1, *, user=USER, device=DEVICE):
    """n runs started through the handler for a connected device; returns (conn, [run ids])."""
    conn = online(registry, user, device)
    ids = [do_start(h, conn)["run_id"] for _ in range(n)]
    return conn, ids


def leave(registry, conn):
    """What ws.py does before it calls on_disconnect: forget the connection, mark it closed."""
    registry.remove(conn)
    conn.closed = True


def test_disconnect_pauses_the_run_and_puts_the_pause_in_the_replay(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert state_of(w, run_id) == "paused_disconnected"
    assert states_in(stored(run_id, 2)) == [(3, "paused_disconnected", "device_disconnected")]
    check_valid(stored(run_id, 2)[0])
    assert len(conn.sent) == 2                                  # the device was gone: it is not sent, it waits
    assert not h.emitter.is_bound(run_id)                       # a paused run holds no emitter state


def test_disconnect_runs_the_hooks_before_the_pause_in_registration_order(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    seen = []

    async def first(c, run):
        seen.append(("first", c, run.id, run.state, state_of(w, run_id), len(stored(run_id))))

    async def second(c, run):
        await asyncio.sleep(0)                                  # yielding must not let the pause overtake it
        seen.append(("second", c, run.id, run.state, state_of(w, run_id), len(stored(run_id))))

    h.add_on_run_disconnect(first)
    h.add_on_run_disconnect(second)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert seen == [("first", conn, run_id, "running", "running", 2),
                    ("second", conn, run_id, "running", "running", 2)]      # still running, no pause event yet
    assert state_of(w, run_id) == "paused_disconnected" and len(stored(run_id)) == 3


def test_disconnect_a_hook_may_emit_and_its_event_comes_before_the_pause(h, w, registry):
    """What the tool broker will do: close the open call with tool_finished `disconnected`."""
    conn, (run_id,) = running(w, h, registry)

    async def broker(c, run):
        await h.emitter.emit(run.id, "run_event", {"type": "tool_finished", "call_id": "call_x",
                                                    "tool": "grep", "status": "disconnected"})

    h.add_on_run_disconnect(broker)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    events = stored(run_id, 2)
    assert [(e["seq"], e["payload"]["type"]) for e in events] == [(3, "tool_finished"), (4, "state_changed")]
    assert events[1]["payload"]["state"] == "paused_disconnected"


def test_disconnect_a_failing_hook_does_not_stop_the_pause_or_the_other_hooks(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    called = []

    async def bad(c, run):
        raise RuntimeError(CANARY)

    async def good(c, run):
        called.append(run.id)

    h.add_on_run_disconnect(bad)
    h.add_on_run_disconnect(good)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert called == [run_id] and state_of(w, run_id) == "paused_disconnected"


def test_disconnect_a_hook_that_never_returns_is_given_up_on(w, registry):
    h = RunHandlers(emitter=RunEmitter(), max_parallel=10, hook_timeout=0.05)
    conn, (run_id,) = running(w, h, registry)
    cancelled = []

    async def stuck(c, run):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(run.id)
            raise

    h.add_on_run_disconnect(stuck)
    leave(registry, conn)
    started_at = time.monotonic()
    go(h.on_disconnect(conn))
    assert time.monotonic() - started_at < 5
    assert cancelled == [run_id] and state_of(w, run_id) == "paused_disconnected"


def test_disconnect_every_open_run_of_the_device_is_paused(h, w, registry):
    conn, ids = running(w, h, registry, n=3)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert [state_of(w, i) for i in ids] == ["paused_disconnected"] * 3
    for i in ids:
        assert states_in(stored(i, 2)) == [(3, "paused_disconnected", "device_disconnected")]


def test_disconnect_an_awaiting_approval_run_is_paused_too(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    runs.transition(USER, run_id, "awaiting_approval")
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert state_of(w, run_id) == "paused_disconnected"


def test_disconnect_leaves_runs_that_are_already_paused_or_over_alone(h, w, registry):
    conn, (budget, error, done, live) = running(w, h, registry, n=4)
    run_manager.pause_run(USER, budget, "paused_budget")
    run_manager.pause_run(USER, error, "paused_error", error_code="internal")
    run_manager.end_run(USER, done, "completed")
    hooked = []

    async def hook(c, run):
        hooked.append(run.id)

    h.add_on_run_disconnect(hook)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert [state_of(w, i) for i in (budget, error, done, live)] == \
        ["paused_budget", "paused_error", "completed", "paused_disconnected"]
    assert hooked == [live]                                     # nothing to close on a paused run
    assert len(stored(budget)) == 2 and len(stored(error)) == 2     # no stray event


def test_disconnect_does_not_touch_another_devices_or_users_runs(h, w, registry):
    conn, (mine,) = running(w, h, registry)
    _, (sibling,) = running(w, h, registry, device=OTHER_DEVICE)
    _, (stranger,) = running(w, h, registry, user=OTHER_USER)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert state_of(w, mine) == "paused_disconnected"
    assert state_of(w, sibling) == "running" and state_of(w, stranger) == "running"


def test_disconnect_a_run_still_in_created_is_moved_through_running_then_paused(h, w, registry):
    run = w.start(device=DEVICE)                                # recorded, never run
    assert run.state == "created"
    conn = StubConn()
    go(h.on_disconnect(conn))
    assert state_of(w, run.id) == "paused_disconnected"
    assert states_in(stored(run.id)) == [(1, "paused_disconnected", "device_disconnected")]


def test_disconnect_after_a_server_restart_needs_no_emitter_state(w, registry):
    """A fresh process knows nothing about the run (nothing is bound); the pause still
    reaches the replay, numbered by the buffer's counter."""
    first = RunHandlers(emitter=RunEmitter(), max_parallel=10)
    conn, (run_id,) = running(w, first, registry)
    leave(registry, conn)
    restarted = RunHandlers(emitter=RunEmitter(), max_parallel=10)
    go(restarted.on_disconnect(conn))
    assert states_in(stored(run_id, 2)) == [(3, "paused_disconnected", "device_disconnected")]


def test_disconnect_with_nothing_open_does_nothing(h, w, registry):
    go(h.on_disconnect(StubConn()))
    assert w.store.rows == {} and w.redis.keys() == []


def test_disconnect_a_run_that_was_ended_meanwhile_is_skipped_quietly(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    leave(registry, conn)

    def somebody_cancels(call):
        if call["to_state"] == "paused_disconnected":
            call["store"].force_state(run_id, "cancelled")      # another process won the race

    w.store.before_move = somebody_cancels
    go(h.on_disconnect(conn))                                   # no exception
    assert state_of(w, run_id) == "cancelled" and len(stored(run_id)) == 2     # no pause event for a run that was not paused
    assert not h.emitter.is_bound(run_id)


def test_disconnect_a_store_outage_is_survived(h, w, registry, monkeypatch):
    conn, (run_id,) = running(w, h, registry)
    leave(registry, conn)
    monkeypatch.setattr(runs, "_STORE", rt.BoomStore())
    go(h.on_disconnect(conn))                                   # logged by type, not raised


def test_disconnect_an_expired_buffer_does_not_undo_the_pause(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    leave(registry, conn)
    buffer.delete_buffer(run_id)                                # idle TTL ran out
    go(h.on_disconnect(conn))
    assert state_of(w, run_id) == "paused_disconnected"         # the event could not be stored; the state still holds
    assert not h.emitter.is_bound(run_id)


# ----------------------------------------------------- the superseded connection

def test_disconnect_of_a_superseded_connection_pauses_nothing(h, w, registry):
    """ws.py: the NEW connection is already registered when the OLD one's on_disconnect runs.
    Pausing then would pause the run of the device that just reconnected."""
    old, (run_id,) = running(w, h, registry)
    hooked = []

    async def hook(c, run):
        hooked.append(run.id)

    h.add_on_run_disconnect(hook)
    new = StubConn()
    registry.add(new)                                           # supersedes `old`
    old.closed = True
    go(h.on_disconnect(old))
    assert state_of(w, run_id) == "running" and hooked == []
    assert len(stored(run_id)) == 2 and h.emitter.is_bound(run_id)      # untouched, still attached


def test_disconnect_of_the_last_connection_pauses_after_a_supersede_too(h, w, registry):
    old, (run_id,) = running(w, h, registry)
    new = StubConn()
    registry.add(new)
    old.closed = True
    go(h.on_disconnect(old))                                    # skipped
    leave(registry, new)
    go(h.on_disconnect(new))                                    # the real end
    assert state_of(w, run_id) == "paused_disconnected"


def test_disconnect_a_newer_connection_that_is_already_closed_does_not_count(h, w, registry):
    old, (run_id,) = running(w, h, registry)
    new = StubConn()
    new.closed = True
    registry.add(new)
    old.closed = True
    go(h.on_disconnect(old))
    assert state_of(w, run_id) == "paused_disconnected"


def test_disconnect_a_connection_of_another_device_does_not_count(h, w, registry):
    conn, (run_id,) = running(w, h, registry)
    online(registry, USER, OTHER_DEVICE)
    online(registry, OTHER_USER, DEVICE)
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert state_of(w, run_id) == "paused_disconnected"


# ================================================================== install

@pytest.fixture
def fresh_router(monkeypatch):
    fresh = MessageRouter()
    monkeypatch.setattr(router_module, "_ROUTER", fresh)
    monkeypatch.setattr(ws_module, "_HANDLER", ws_module.UnavailableHandler())
    return fresh


def test_install_registers_the_handlers_and_the_disconnect_listener_and_installs_the_router(fresh_router):
    handlers = RunHandlers(emitter=RunEmitter())
    assert run_handlers.install(handlers=handlers) is handlers
    assert fresh_router.registered_types() == ("cancel", "run.resume", "run.start", "user_message")
    assert ws_module.get_message_handler() is fresh_router


def test_install_twice_changes_nothing_and_raises_nothing(fresh_router):
    handlers = RunHandlers(emitter=RunEmitter())
    run_handlers.install(handlers=handlers)
    run_handlers.install(handlers=handlers)
    assert fresh_router.registered_types() == ("cancel", "run.resume", "run.start", "user_message")
    assert len(fresh_router._disconnect_listeners) == 1
    assert ws_module.get_message_handler() is fresh_router


def test_install_with_a_router_of_its_own_leaves_the_sockets_handler_alone(monkeypatch):
    sentinel = ws_module.UnavailableHandler()
    monkeypatch.setattr(ws_module, "_HANDLER", sentinel)
    mine = MessageRouter()
    run_handlers.install(mine, handlers=RunHandlers(emitter=RunEmitter()))
    assert mine.registered_types() == ("cancel", "run.resume", "run.start", "user_message") and ws_module.get_message_handler() is sentinel


def test_install_defaults_to_the_process_wide_handlers(fresh_router, monkeypatch):
    shared = RunHandlers(emitter=RunEmitter())
    monkeypatch.setattr(run_handlers, "_HANDLERS", shared)
    assert run_handlers.install() is shared


def test_importing_the_module_installs_nothing(monkeypatch):
    sentinel = ws_module.UnavailableHandler()
    monkeypatch.setattr(ws_module, "_HANDLER", sentinel)
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_handlers_import_probe", run_handlers.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert ws_module.get_message_handler() is sentinel


# ====================================================== content hygiene (caplog)

def test_no_content_reaches_the_log(h, w, registry, caplog):
    caplog.set_level(logging.DEBUG)
    conn = online(registry)
    secret_payload = start_payload(history(1), instructions=f"be nice {CANARY}")
    first = start_msg(payload=secret_payload)
    go(h.on_run_start(conn, first))                                               # a good start
    go(h.on_run_start(conn, start_msg(run_id=first["run_id"], payload=secret_payload)))   # a collision
    go(h.on_run_start(conn, start_msg(payload=start_payload(mode=CANARY))))             # an invalid payload

    async def hook(c, run):
        raise RuntimeError(CANARY)

    h.add_on_run_disconnect(hook)
    w.redis.fail_if(lambda commands: True)                                         # and a buffer fault on the pause
    leave(registry, conn)
    go(h.on_disconnect(conn))
    assert CANARY not in caplog.text
    mine = [rec for rec in caplog.records if rec.name == run_handlers.logger.name]
    assert mine, "the handlers should log their own decisions (ids, codes, type names only)"
    assert any(first["run_id"] in rec.getMessage() for rec in mine)
    assert any("RuntimeError" in rec.getMessage() for rec in mine)


# ============================================================ through the socket

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


@pytest.fixture
def wired(rig, w):
    """The real router with the S14c handlers on S04's real socket."""
    router = MessageRouter()
    handlers = RunHandlers(emitter=RunEmitter())
    run_handlers.install(router, handlers=handlers)
    rig.use_handler(router)
    rig.check = check_valid                                     # every frame the device gets is protocol-valid
    return handlers


RUN = "run_handlers_socket_01"


def wait_for(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), what


def test_socket_a_device_starts_a_run_and_receives_created_then_running(rig, w, wired):
    credential, device_id = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start = envelope("run.start", start_payload(history(1)), run_id=RUN)
        dev.send(start)
        first, second = dev.recv(), dev.recv()
        dev.ping_pong()
    assert [(m["t"], m["seq"], m["payload"]["state"]) for m in (first, second)] == \
        [("run_event", 1, "created"), ("run_event", 2, "running")]
    row = w.store.rows[RUN]
    assert (row["user_id"], row["device_id"]) == ("user-a", device_id)


def test_socket_a_second_run_start_with_the_same_id_is_invalid_message_with_ref_id(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.start", start_payload(), run_id=RUN))
        dev.recv(), dev.recv()
        again = envelope("run.start", start_payload(), run_id=RUN)
        dev.send(again)
        err = dev.recv_error()
        assert err["code"] == "invalid_message" and err["ref_id"] == again["id"]
        dev.ping_pong()


def test_socket_the_parallel_cap_is_the_one_welcome_advertises(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, welcome):
        cap = welcome["payload"]["limits"]["max_parallel_runs"]
        assert cap == wired.max_parallel                       # advertised == enforced
        for i in range(cap):
            dev.send(envelope("run.start", start_payload(), run_id=f"run_cap_socket_{i:02d}"))
            dev.recv(), dev.recv()
        over = envelope("run.start", start_payload(), run_id="run_cap_socket_99")
        dev.send(over)
        err = dev.recv_error()
        assert err["code"] == "rate_limited" and err["ref_id"] == over["id"]
        assert "retry_after_s" in err["details"]


def test_socket_the_device_leaving_pauses_its_run_and_the_pause_waits_in_the_replay(rig, w, wired):
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.start", start_payload(), run_id=RUN))
        dev.recv(), dev.recv()
    assert state_of(w, RUN) == "paused_disconnected"
    assert states_in(stored(RUN, 2)) == [(3, "paused_disconnected", "device_disconnected")]
    assert not wired.emitter.is_bound(RUN)


def test_socket_a_reconnect_does_not_pause_its_own_run(rig, w, wired):
    """The new connection is registered before the old one's on_disconnect runs."""
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (first, _w1):
        first.send(envelope("run.start", start_payload(), run_id=RUN))
        first.recv(), first.recv()
        with rig.session(credential) as (second, _w2):         # the same device connects again
            assert first.expect_close() == 4409                 # superseded
            wait_for(lambda: 1 in rig.finished, "the superseded connection's cleanup did not finish")
            assert state_of(w, RUN) == "running"                # NOT paused by the old connection leaving
            assert len(stored(RUN)) == 2
            second.ping_pong()
    assert state_of(w, RUN) == "paused_disconnected"            # the last connection leaving does pause it
