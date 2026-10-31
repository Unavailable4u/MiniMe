"""S09: the tool broker behind the REAL S14 pieces (router, run handlers, emitter) and S06's
run store and buffer (in-memory twins behind the real interfaces: run_manager_testkit.World),
with every frame the device receives checked against protocol/v1.

tests/unit/test_codeagent_broker.py proves the broker's own rules with a recording fake for
`emit`. This file proves the JOINTS (the work package's "register the broker like this"):

  * tool_proposed, tool_finished and the answers are real numbered, stored frames that pass the
    protocol validator ............................................................ test_router_*
  * a disconnect during a call: S14 calls the broker's hook BEFORE it pauses, so the replay reads
    tool_proposed, tool_finished(disconnected), state_changed(paused_disconnected), the order of
    protocol/fixtures/03_disconnect_resume.jsonl; S06 holds the run in paused_disconnected;
    the broker does not pause the run itself (a second pause would be an IllegalTransition and
    would cost the replay its paused_disconnected event) ......................... test_disconnect_*
  * a reconnect does not revive the old call ....................................... test_reconnect_*
  * cancel: tool_finished(cancelled) is announced before the run's end, through the emitter,
    outside the run's lock ......................................................... test_cancel_*
  * a superseded connection (S14 does nothing for it) is covered by the router listener ... test_superseded_*
  * the timeout `error` frame is a valid protocol message ......................... test_timeout_*
  * a scripted device over S04's real socket: a tool call end to end, a denial that finishes at
    once, a cut call and its resume, a foreign injection ......................... test_socket_*

Skipped as a module when jsonschema is not installed, like the other socket tests.
"""
import asyncio
import threading
import time
import types

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed; install protocol/requirements.txt to run the "
           "device-socket tests (see codeagent/ws.py, 'Runtime needs')",
)

from codeagent import broker as broker_module                                                   # noqa: E402
from codeagent import buffer, connections, emitter as emitter_module, router as router_module   # noqa: E402
from codeagent import run_handlers, run_manager, runs                                           # noqa: E402
from codeagent.connections import ConnectionClosed                                              # noqa: E402
from codeagent.emitter import RunEmitter                                                        # noqa: E402
from codeagent.router import MessageRouter                                                      # noqa: E402
from codeagent.run_handlers import RunHandlers                                                  # noqa: E402
from tests.unit import runs_testkit as rt                                                       # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope                                          # noqa: E402
from tests.unit.run_manager_testkit import (                                                    # noqa: E402
    DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, start_payload,
)

RUN = "run_broker_wiring_01"


def go(coro):
    return asyncio.run(coro)


async def until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("the condition was not reached in time")
        await asyncio.sleep(0.002)


def check_valid(env):
    issues = run_manager._protocol().validate_message(env, tolerant=False)
    assert not issues, [(i.path, i.validator) for i in issues]


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
    """A DeviceConnection stand-in that records the frames and errors it was sent."""

    def __init__(self, user_id=USER, device_id=DEVICE):
        self.user_id = user_id
        self.device_id = device_id
        self.sent = []
        self.errors = []
        self.closed = False

    async def send_json(self, env):
        if self.closed:
            raise ConnectionClosed("connection closed")
        check_valid(env)                                    # nothing invalid ever reaches a device
        self.sent.append(env)

    async def send_error(self, code, message, *, ref_id=None, details=None):
        if self.closed:
            raise ConnectionClosed("connection closed")
        check_valid(connections.make_envelope("error", connections.error_payload(
            code, message, ref_id=ref_id, details=details)))
        self.errors.append({"code": code, "message": message, "ref_id": ref_id, "details": details})


def online(registry, user=USER, device=DEVICE):
    conn = StubConn(user, device)
    registry.add(conn)
    return conn


class Stack:
    """The real S14 stack with the broker installed on it, the way api/server.py does it."""

    def __init__(self, **broker_kw):
        self.emitter = RunEmitter()
        self.handlers = RunHandlers(emitter=self.emitter, max_parallel=10)
        self.router = MessageRouter()
        run_handlers.install(self.router, handlers=self.handlers)
        broker_kw.setdefault("timeout_s", 5.0)
        self.broker = broker_module.ToolBroker(emit=self.emitter.emit, **broker_kw)
        broker_module.install(self.router, broker=self.broker, handlers=self.handlers)

    async def start(self, conn, run_id=RUN):
        await self.router.on_message(conn, envelope("run.start", start_payload(), run_id=run_id))

    def propose(self, conn, call_id="call_read_1", run_id=RUN, tool="read_file", **kw):
        kw.setdefault("tool_input", {"path": "src/a.py"})
        kw.setdefault("risk", "R0")
        return asyncio.ensure_future(self.broker.propose(
            run_id=run_id, user_id=conn.user_id, device_id=conn.device_id, call_id=call_id, tool=tool, **kw))

    async def proposed(self, conn, call_id="call_read_1"):
        await until(lambda: any(f["t"] == "tool_proposed" and f["payload"]["call_id"] == call_id for f in conn.sent))

    async def say(self, conn, t, payload, run_id=RUN):
        msg = envelope(t, payload, run_id=run_id)
        await self.router.on_message(conn, msg)
        return msg


def result(call_id="call_read_1", output="print('hi')\n", ok=True):
    return {"call_id": call_id, "ok": ok, "output": output, "truncated": False,
            "trust": "local_project", "duration_ms": 3}


def kinds(frames):
    out = []
    for f in frames:
        p = f["payload"]
        out.append((f["seq"], f["t"] if f["t"] != "run_event" else p["type"],
                    p.get("status") or p.get("state") or p.get("call_id")))
    return out


def replay_kinds(run_id=RUN, after=0):
    return kinds(buffer.replay_after(run_id, after))


# ---------------------------------------------------------------------
# The real router, emitter and protocol validator
# ---------------------------------------------------------------------

def test_router_a_whole_call_is_numbered_stored_and_protocol_valid(w, registry):
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        task = stack.propose(conn)
        await stack.proposed(conn)
        await stack.say(conn, "tool_result", result())
        out = await asyncio.wait_for(task, 2)
        assert (out.status, out.output, out.trust) == ("ok", "print('hi')\n", "untrusted")
        assert kinds(conn.sent) == [(1, "state_changed", "created"), (2, "state_changed", "running"),
                                    (3, "tool_proposed", "call_read_1"), (4, "tool_finished", "ok")]
        proposed = conn.sent[2]["payload"]
        assert proposed == {"call_id": "call_read_1", "tool": "read_file", "input": {"path": "src/a.py"}, "risk": "R0"}
        assert replay_kinds() == kinds(conn.sent)           # what the device saw is what a resume would replay
        assert conn.errors == []
    go(run())


def test_router_an_r1_proposal_is_a_valid_frame(w, registry):
    """The broker does not validate tool input (the loop does, S07). StubConn validates every frame
    it is sent against protocol/v1, so this R1 edit proposal is checked, input and risk included."""
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        task = stack.propose(conn, tool="edit_file", risk="R1",
                             tool_input={"path": "a.py", "old_string": "x", "new_string": "y"})
        await stack.proposed(conn)
        await stack.say(conn, "approval", {"call_id": "call_read_1", "decision": "deny", "scope": "once"})
        assert (await asyncio.wait_for(task, 2)).status == "denied"
    go(run())


def test_router_a_denied_call_finishes_at_once_and_the_run_goes_on(w, registry):
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        task = stack.propose(conn, "call_edit_1", tool="edit_file", risk="R1", timeout_s=60.0,
                             tool_input={"path": "a.py", "old_string": "x", "new_string": "y"})
        await stack.proposed(conn, "call_edit_1")
        t0 = time.monotonic()
        await stack.say(conn, "approval", {"call_id": "call_edit_1", "decision": "deny", "scope": "once"})
        out = await asyncio.wait_for(task, 2)
        assert time.monotonic() - t0 < 1.0 and out.status == "denied"
        assert kinds(conn.sent)[-1] == (4, "tool_finished", "denied")
        assert w.store.rows[RUN]["state"] == "running"       # the broker does not touch the run's state
        await stack.say(conn, "tool_result", result("call_edit_1"))     # a result after a deny: ignored
        assert len(conn.sent) == 4 and conn.errors == []
    go(run())


def test_router_a_foreign_device_cannot_inject_a_result_into_another_users_run(w, registry):
    stack = Stack()

    async def run():
        victim = online(registry)
        attacker = online(registry, OTHER_USER, OTHER_DEVICE)
        await stack.start(victim)
        task = stack.propose(victim)
        await stack.proposed(victim)
        before = len(victim.sent)
        await stack.say(attacker, "tool_result", result(output="CANARY-injected"), run_id=RUN)
        await stack.say(attacker, "approval", {"call_id": "call_read_1", "decision": "deny", "scope": "once"}, run_id=RUN)
        assert [e["code"] for e in attacker.errors] == ["invalid_message", "invalid_message"]
        assert attacker.sent == [] and len(victim.sent) == before and not task.done()
        await stack.say(victim, "tool_result", result(output="the real output"))
        assert (await asyncio.wait_for(task, 2)).output == "the real output"
    go(run())


def test_timeout_the_error_frame_and_the_event_are_valid_protocol_messages(w, registry):
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        out = await asyncio.wait_for(stack.propose(conn, timeout_s=0.05), 2)
        assert out.status == "timeout"
        assert kinds(conn.sent)[-1] == (4, "tool_finished", "timeout")
        assert conn.errors == [{"code": "tool_timeout", "message": broker_module.MSG_TOOL_TIMEOUT,
                                "ref_id": None, "details": {"call_id": "call_read_1"}}]
    go(run())


# ---------------------------------------------------------------------
# Disconnect: the hook runs before S14 pauses; S06 holds the pause
# ---------------------------------------------------------------------

def test_disconnect_during_a_call_closes_it_and_the_run_is_paused_through_s06(w, registry):
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        task = stack.propose(conn)
        await stack.proposed(conn)
        registry.remove(conn)                               # ws.py: out of the registry, closed, then on_disconnect
        conn.closed = True
        await stack.router.on_disconnect(conn)
        out = await asyncio.wait_for(task, 2)
        assert out.status == "disconnected" and out.output is None
        # the order of protocol/fixtures/03_disconnect_resume.jsonl: the call is closed, THEN the pause
        assert replay_kinds() == [(1, "state_changed", "created"), (2, "state_changed", "running"),
                                  (3, "tool_proposed", "call_read_1"), (4, "tool_finished", "disconnected"),
                                  (5, "state_changed", "paused_disconnected")]
        assert w.store.rows[RUN]["state"] == "paused_disconnected"
        assert buffer.replay_after(RUN, 4)[0]["payload"] == {
            "type": "state_changed", "state": "paused_disconnected", "reason": "device_disconnected"}
        assert stack.broker.in_flight(RUN) == 0
        assert not stack.emitter.is_bound(RUN)              # S14 unbinds a paused run, as before
    go(run())


def test_disconnect_the_broker_does_not_pause_the_run_itself():
    """If the hook paused the run, S14's own pause (which runs right after the hooks) would hit
    paused_disconnected -> paused_disconnected, which is not an edge of runs.TRANSITIONS, and S14
    would skip announcing the pause. S14 pauses through S06; the broker only closes its calls."""
    import ast
    assert "paused_disconnected" not in runs.TRANSITIONS["paused_disconnected"]
    tree = ast.parse(open(broker_module.__file__, encoding="utf-8").read())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names}
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {"run_manager", "runs", "buffer"} & imported
    assert not {"pause_run", "transition", "end_run", "cancel_run", "resume_run"} & called


def test_reconnect_resume_replays_the_cut_call_closed_and_the_old_call_stays_dead(w, registry):
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        task = stack.propose(conn)
        await stack.proposed(conn)
        registry.remove(conn)
        conn.closed = True
        await stack.router.on_disconnect(conn)
        await asyncio.wait_for(task, 2)
        # the device reconnects having seen up to seq 2 (it never got the proposal)
        back = online(registry)
        await stack.say(back, "run.resume", {"last_seq": 2})
        assert kinds(back.sent) == [(3, "tool_proposed", "call_read_1"), (4, "tool_finished", "disconnected"),
                                    (5, "state_changed", "paused_disconnected"), (6, "state_changed", "running")]
        # protocol README 5: a client runs a replayed tool_proposed only if no later tool_finished for it is
        # in the replay. There is one, so the old call must not run:
        proposed_at = next(s for s, k, _ in kinds(back.sent) if k == "tool_proposed")
        finished_at = next(s for s, k, _ in kinds(back.sent) if k == "tool_finished")
        assert finished_at > proposed_at
        frames_before = len(back.sent)
        # a client that ran it anyway answers, and nothing revives:
        await stack.say(back, "tool_result", result(output="CANARY-revived"))
        await stack.say(back, "approval", {"call_id": "call_read_1", "decision": "allow", "scope": "once"})
        assert len(back.sent) == frames_before and back.errors == []
        assert w.store.rows[RUN]["state"] == "running"
        again = await stack.broker.propose(run_id=RUN, user_id=USER, device_id=DEVICE, call_id="call_read_1",
                                           tool="read_file", tool_input={"path": "src/a.py"}, risk="R0")
        assert again.status == "disconnected"
        assert len(back.sent) == frames_before              # no second tool_proposed
        retry = stack.propose(back, "call_read_2")           # the loop's retry is a new call
        await stack.proposed(back, "call_read_2")
        await stack.say(back, "tool_result", result("call_read_2", "second try"))
        assert (await asyncio.wait_for(retry, 2)).output == "second try"
        assert kinds(back.sent)[-2:] == [(7, "tool_proposed", "call_read_2"), (8, "tool_finished", "ok")]
    go(run())


def test_superseded_a_connection_replaced_by_the_same_device_closes_its_calls_through_the_listener(w, registry):
    """S14 does nothing for a superseded connection (the device is back; no hook, no pause). The
    broker's router listener still closes the calls proposed on the dead connection, and the run
    keeps running on the new one."""
    stack = Stack()

    async def run():
        old = online(registry)
        await stack.start(old)
        task = stack.propose(old)
        await stack.proposed(old)
        new = online(registry)                              # same device, new connection: replaces `old`
        old.closed = True
        await stack.router.on_disconnect(old)
        out = await asyncio.wait_for(task, 2)
        assert out.status == "disconnected"
        assert kinds(new.sent) == [(4, "tool_finished", "disconnected")]     # the event goes to the live connection
        assert w.store.rows[RUN]["state"] == "running"      # S14 left the run alone: the device is back
        assert stack.emitter.is_bound(RUN)
    go(run())


# ---------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------

def test_cancel_announces_tool_finished_cancelled_before_the_end_of_the_run(w, registry):
    stack = Stack()

    async def run():
        conn = online(registry)
        await stack.start(conn)
        a = stack.propose(conn, "call_read_1")
        b = stack.propose(conn, "call_read_2", tool="grep", tool_input={"pattern": "x"})
        await stack.proposed(conn, "call_read_1")
        await stack.proposed(conn, "call_read_2")
        await stack.say(conn, "cancel", {})
        outs = await asyncio.wait_for(asyncio.gather(a, b), 2)
        assert [o.status for o in outs] == ["cancelled", "cancelled"]
        seen = kinds(conn.sent)
        assert [k for _, k, _ in seen] == ["state_changed", "state_changed", "tool_proposed", "tool_proposed",
                                           "tool_finished", "tool_finished", "state_changed"]
        assert [s for _, k, s in seen if k == "tool_finished"] == ["cancelled", "cancelled"]
        assert seen[-1][2] == "cancelled"                    # the run's own end comes last
        assert [s for s, _, _ in seen] == list(range(1, 8))  # gap-free numbering across broker and S14
        assert w.store.rows[RUN]["state"] == "cancelled"
        assert conn.errors == []
    go(run())


# ---------------------------------------------------------------------
# The other install hooks live side by side with the walking skeleton's
# ---------------------------------------------------------------------

def test_install_coexists_with_the_walking_skeletons_hooks(monkeypatch):
    import logging

    from codeagent import hello
    handlers = RunHandlers(emitter=RunEmitter())
    router = MessageRouter()
    monkeypatch.setattr(run_handlers, "_HANDLERS", handlers)
    sdk = logging.getLogger("openai")
    before = sdk.level
    try:
        run_handlers.install(router, handlers=handlers)
        hello.install()
        b = broker_module.ToolBroker(emit=handlers.emitter.emit)
        broker_module.install(router, broker=b, handlers=handlers)
        assert b.on_run_disconnect in handlers.on_run_disconnect_hooks()
        assert b.on_cancel in handlers.on_cancel_hooks()
        assert len(handlers.on_run_disconnect_hooks()) == 2 and len(handlers.on_cancel_hooks()) == 2
        assert handlers.get_turn_runner() is hello.run_turn  # the broker never takes the turn runner
    finally:
        sdk.setLevel(before)


# ---------------------------------------------------------------------
# A scripted device over S04's real socket
# ---------------------------------------------------------------------

@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def wait_for(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), what


class ToolTurnRunner:
    """A stand-in for the loop (S07): on a user_message it proposes ONE tool call through the
    broker, waits for the outcome, and streams a line saying how it ended."""

    def __init__(self, broker, emitter, **propose_kw):
        self.broker, self.emitter = broker, emitter
        self.propose_kw = propose_kw
        self.outcomes = []
        self.calls = 0

    async def __call__(self, conn, run, msg):
        self.calls += 1
        out = await self.broker.propose(
            run_id=run.id, user_id=run.user_id, device_id=run.device_id,
            call_id=f"call_{self.calls}", tool="read_file", tool_input={"path": "src/a.py"}, risk="R0",
            **self.propose_kw)
        self.outcomes.append(out)
        await self.emitter.emit(run.id, "run_event", {"type": "text_delta", "text": f"call {out.status}"})
        await self.emitter.emit(run.id, "run_event", {"type": "text_done", "stop_reason": "end_turn"})


@pytest.fixture
def wired(rig, w):
    stack = Stack()
    rig.use_handler(stack.router)
    rig.check = check_valid
    return stack


def start_over_the_wire(dev, run_id=RUN):
    dev.send(envelope("run.start", start_payload(), run_id=run_id))
    return [dev.recv(), dev.recv()]


def test_socket_a_tool_call_end_to_end_and_the_next_text_follows_tool_finished(rig, w, wired):
    runner = ToolTurnRunner(wired.broker, wired.emitter)
    wired.handlers.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "read it"}, run_id=RUN))
        proposed = dev.recv()
        assert proposed["t"] == "tool_proposed" and proposed["seq"] == 3
        assert proposed["payload"] == {"call_id": "call_1", "tool": "read_file",
                                       "input": {"path": "src/a.py"}, "risk": "R0"}
        dev.send(envelope("tool_result", result("call_1", "file text"), run_id=RUN))
        rest = [dev.recv(), dev.recv(), dev.recv()]
        assert [(m["seq"], m["payload"].get("type"), m["payload"].get("status") or m["payload"].get("text"))
                for m in rest] == [(4, "tool_finished", "ok"), (5, "text_delta", "call ok"), (6, "text_done", None)]
        dev.ping_pong()
    assert runner.outcomes[0].output == "file text" and runner.outcomes[0].trust == "untrusted"


def test_socket_a_denied_call_finishes_at_once_with_no_tool_result(rig, w, wired):
    runner = ToolTurnRunner(wired.broker, wired.emitter, timeout_s=60.0)
    wired.handlers.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "edit it"}, run_id=RUN))
        assert dev.recv()["t"] == "tool_proposed"
        t0 = time.monotonic()
        dev.send(envelope("approval", {"call_id": "call_1", "decision": "deny", "scope": "once"}, run_id=RUN))
        finished = dev.recv()
        assert finished["payload"] == {"type": "tool_finished", "call_id": "call_1", "tool": "read_file",
                                       "status": "denied", "duration_ms": finished["payload"]["duration_ms"]}
        assert dev.recv()["payload"]["text"] == "call denied"
        assert dev.recv()["payload"]["type"] == "text_done"
        assert time.monotonic() - t0 < 5.0                  # far from the 60 s timeout
        dev.ping_pong()
    assert runner.outcomes[0].status == "denied"


def test_socket_a_timeout_sends_the_error_and_closes_the_call(rig, w, wired):
    runner = ToolTurnRunner(wired.broker, wired.emitter, timeout_s=0.1)
    wired.handlers.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "read it"}, run_id=RUN))
        assert dev.recv()["t"] == "tool_proposed"
        finished = dev.recv()
        err = dev.recv_error()
        assert finished["payload"]["status"] == "timeout"
        assert err["code"] == "tool_timeout" and err["details"] == {"call_id": "call_1"} and "seq" not in err
        assert dev.recv()["payload"]["text"] == "call timeout"
        assert dev.recv()["payload"]["type"] == "text_done"
        dev.send(envelope("tool_result", result("call_1", "too late"), run_id=RUN))
        dev.ping_pong()                                     # the late result got no answer and broke nothing
    assert runner.outcomes[0].status == "timeout"


def test_socket_a_cut_call_is_closed_the_run_pauses_and_resume_replays_it(rig, w, wired):
    runner = ToolTurnRunner(wired.broker, wired.emitter)
    wired.handlers.set_turn_runner(runner)
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential) as (dev, _welcome):
        start_over_the_wire(dev)
        dev.send(envelope("user_message", {"text": "read it"}, run_id=RUN))
        assert dev.recv()["seq"] == 3                       # tool_proposed
    wait_for(lambda: w.store.rows[RUN]["state"] == "paused_disconnected", "the run was not paused")
    assert replay_kinds() == [(1, "state_changed", "created"), (2, "state_changed", "running"),
                              (3, "tool_proposed", "call_1"), (4, "tool_finished", "disconnected"),
                              (5, "state_changed", "paused_disconnected")]
    assert wired.broker.in_flight(RUN) == 0
    with rig.session(credential) as (dev, _welcome):
        dev.send(envelope("run.resume", {"last_seq": 3}, run_id=RUN))
        missed = [dev.recv(), dev.recv(), dev.recv()]
        assert [(m["seq"], m["payload"]["type"]) for m in missed] == [
            (4, "tool_finished"), (5, "state_changed"), (6, "state_changed")]
        assert missed[0]["payload"]["status"] == "disconnected"
        dev.send(envelope("tool_result", result("call_1", "CANARY-revived"), run_id=RUN))
        dev.ping_pong()                                     # ignored: nothing came back for it
    # S14 cancelled the turn when the device left, so the runner never saw an outcome; the broker's
    # record is what a retry (a new call id) and a resumed loop rely on
    assert runner.outcomes == []
    assert wired.broker.call_stats(RUN, "call_1")["status"] == "disconnected"


def test_socket_a_foreign_device_cannot_inject_into_another_users_call(rig, w, wired):
    runner = ToolTurnRunner(wired.broker, wired.emitter)
    wired.handlers.set_turn_runner(runner)
    cred_a, _ = rig.store.add_device("user-a")
    cred_b, _ = rig.store.add_device("user-b")
    with rig.session(cred_a) as (owner, _w1):
        start_over_the_wire(owner)
        owner.send(envelope("user_message", {"text": "read it"}, run_id=RUN))
        assert owner.recv()["t"] == "tool_proposed"
        with rig.session(cred_b) as (intruder, _w2):
            for t, payload in (("tool_result", result("call_1", "CANARY-injected")),
                               ("approval", {"call_id": "call_1", "decision": "deny", "scope": "once"}),
                               ("tool_stream", {"call_id": "call_1", "stream": "stdout", "chunk": "x"})):
                msg = envelope(t, payload, run_id=RUN)
                intruder.send(msg)
                err = intruder.recv_error()
                assert err["code"] == "invalid_message" and err["ref_id"] == msg["id"]
            intruder.ping_pong()
        assert runner.outcomes == []                         # the owner's call is untouched
        owner.send(envelope("tool_result", result("call_1", "legit"), run_id=RUN))
        assert owner.recv()["payload"]["status"] == "ok"
    assert runner.outcomes[0].output == "legit"
