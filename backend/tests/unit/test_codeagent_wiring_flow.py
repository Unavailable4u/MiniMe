"""Patch S15 -- whole turns through the wired loop: the REAL router, run handlers, emitter, broker, S08 assembler and
S10 metering, joined by wiring.install() exactly as server.py joins them. Only the model (a scripted fake), the
usage ledger (metering_testkit.FakeLedger), the run store / buffer / run_guard Redis (World's in-memory twins) and the
device (a stub connection that checks every frame against protocol/v1, or S04's real socket for two tests) are fakes.

  * a turn with no tool calls: streamed, one ledger row, one usage event, the run stays running ............ test_a_plain_turn_*
  * a read round trip: the model proposes, the device answers, the model finishes ......................... test_a_read_*
  * the same model, a device WITHOUT "tools": no tools offered, no tool_proposed sent, still a chat ......... test_a_device_without_tools_*
  * numbers only in the ledger and the usage events; no content in the log ................................. test_*_numbers_only_*
  * the pool is used up: error, paused_budget, no model call, no row ...................................... test_pool_exhausted_*
  * the run ceiling is reached, S10's grant raises it, the turn carries on ................................. test_run_ceiling_*
  * disconnect mid reply, run.resume: the notice, the reply written again, one row for the finished call ... test_disconnect_*
  * cancel: the stream is closed, no task is left .......................................................... test_cancel_*
  * the kill switch and install twice ..................................................................... test_kill_switch_*, test_install_twice_*
  * server.py's own startup lines, executed: the loop is installed and hello is not ......................... test_server_startup_*
  * S04's real socket: "tools" in hello.capabilities is what turns tools on ................................ test_socket_*

Fake secrets are built at run time (gitleaks flags literals shaped like keys); nothing here is one.
"""
import ast
import asyncio
import logging
import pathlib
import time
import types

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed; install protocol/requirements.txt to run the "
           "device-socket tests (see codeagent/ws.py, 'Runtime needs')",
)

from api.routes import code_runs                                                                  # noqa: E402
from codeagent import broker as broker_module, buffer, connections, emitter as emitter_module     # noqa: E402
from codeagent import hello, loop as loop_module, metering, router as router_module               # noqa: E402
from codeagent import run_handlers, run_manager, runs, wiring                                     # noqa: E402
from codeagent.connections import ConnectionClosed                                                # noqa: E402
from codeagent.emitter import RunEmitter                                                          # noqa: E402
from codeagent.loop import LoopConfig, MSG_NO_TOOLS                                               # noqa: E402
from codeagent.model_adapter import TextDelta                                                     # noqa: E402
from codeagent.router import MessageRouter                                                        # noqa: E402
from codeagent.run_handlers import RunHandlers                                                    # noqa: E402
from eo import credits as credits_module, usage_ledger                                            # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope                                            # noqa: E402
from tests.unit.loop_testkit import (CANARY, HANG, STRONG, FakeBudget, Reply, ScriptedAdapter, call,   # noqa: E402
                                     go)
from tests.unit.metering_testkit import FakeLedger, priced                                        # noqa: E402
from tests.unit.run_manager_testkit import DEVICE, USER, World, start_payload                     # noqa: E402

RUN = "run_wiring_flow_01"
SERVER = pathlib.Path(__file__).resolve().parents[2] / "api" / "server.py"
LEDGER_KEYS = {"user_id", "surface", "run_id", "provider", "model", "input_tokens", "cached_input_tokens",
               "output_tokens", "credits"}
USAGE_KEYS = {"type", "model", "input_tokens", "cached_input_tokens", "output_tokens", "credits",
              "run_credits_total"}


def check_valid(env):
    issues = run_manager._protocol().validate_message(env, tolerant=False)
    assert not issues, [(i.path, i.validator) for i in issues]


async def until(predicate, timeout=5.0, what="the condition"):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.003)


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


class Device:
    """A DeviceConnection stand-in: it keeps what the real one keeps from `hello` (capabilities) and checks every
    frame it is sent against protocol/v1."""

    def __init__(self, capabilities=("tools",), user_id=USER, device_id=DEVICE):
        self.user_id, self.device_id = user_id, device_id
        self.capabilities = tuple(capabilities)
        self.sent, self.errors = [], []
        self.closed = False

    async def send_json(self, env):
        if self.closed:
            raise ConnectionClosed("connection closed")
        check_valid(env)                                           # nothing invalid ever reaches a device
        self.sent.append(env)

    async def send_error(self, code, message, *, ref_id=None, details=None):
        if self.closed:
            raise ConnectionClosed("connection closed")
        check_valid(connections.make_envelope("error", connections.error_payload(
            code, message, ref_id=ref_id, details=details)))
        self.errors.append({"code": code, "message": message, "ref_id": ref_id, "details": details})

    # what a test looks at
    def events(self, kind):
        return [f["payload"] for f in self.sent if f["t"] == "run_event" and f["payload"]["type"] == kind]

    def of_type(self, t):
        return [f for f in self.sent if f["t"] == t]

    def text(self):
        return "".join(p["text"] for p in self.events("text_delta"))

    def states(self):
        return [p["state"] for p in self.events("state_changed")]

    def seqs(self):
        return [f["seq"] for f in self.sent if "seq" in f]


def online(registry, **kw):
    conn = Device(**kw)
    registry.add(conn)
    return conn


class Stack:
    """The server's pieces, installed in the order api/server.py installs them: run handlers, then the wiring, then the
    broker. `env` is what the wiring reads (M2C_LOOP, provider keys): pass it through the environment of the test."""

    def __init__(self, monkeypatch, w, *, script=(), ledger=None, policy=None, credits=None, wire=True, env=None,
                 chain=None, config=None, **loop_kw):
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        self.w = w
        self.mp = monkeypatch
        self.emitter = RunEmitter()
        monkeypatch.setattr(emitter_module, "_EMITTER", self.emitter)
        self.handlers = RunHandlers(emitter=self.emitter, max_parallel=10, hook_timeout=5.0, stop_timeout=5.0,
                                    purge_delays=(0.01,))
        monkeypatch.setattr(run_handlers, "_HANDLERS", self.handlers)
        self.router = MessageRouter()
        monkeypatch.setattr(router_module, "_ROUTER", self.router)
        self.broker = broker_module.ToolBroker(emit=self.emitter.emit, timeout_s=5.0)
        monkeypatch.setattr(broker_module, "_BROKER", self.broker)
        monkeypatch.setattr(loop_module, "_LOOP", None)
        monkeypatch.setattr(wiring, "_BUILT", None)
        self.ledger = ledger or FakeLedger(pool=1000.0)
        self.adapter = ScriptedAdapter(*script)
        self.policy = policy or metering.RunCeilingPolicy(fixed=1000.0, chunk_fixed=5.0)
        self.store = metering.RunBudgetStore(redis=lambda: w.guard)
        run_handlers.install(self.router, handlers=self.handlers)
        self.loop = None
        if wire:
            self.loop = wiring.build_loop(
                environ={}, chain=[STRONG] if chain is None else chain, adapter=self.adapter, check=self.ledger.check,
                commit=self.ledger.commit, ledger_sum=self.ledger.run_credits, store=self.store, policy=self.policy,
                credits=credits, config=config or LoopConfig(cancel_poll_s=0.01, stop_timeout_s=2.0),
                tool_budget=FakeBudget(), **loop_kw)
            wiring.install(loop=self.loop)
        broker_module.install(self.router, broker=self.broker, handlers=self.handlers)

    # -- the device's side ------------------------------------------------------------------------
    async def say(self, conn, t, payload, run_id=RUN):
        msg = envelope(t, payload, run_id=run_id)
        await self.router.on_message(conn, msg)
        return msg

    async def start(self, conn, run_id=RUN, **over):
        await self.router.on_message(conn, envelope("run.start", start_payload(**over), run_id=run_id))
        assert conn.errors == [], conn.errors

    async def ask(self, conn, text="please help", run_id=RUN):
        return await self.say(conn, "user_message", {"text": text}, run_id)

    async def settled(self, run_id=RUN, timeout=5.0):
        await until(lambda: run_id not in self.handlers.turn_runs() and not self.loop_tasks(), timeout,
                    "the turn to settle")

    def loop_tasks(self):
        return dict(self.loop._tasks) if self.loop is not None else {}

    async def proposed(self, conn, n=1):
        await until(lambda: len(conn.of_type("tool_proposed")) >= n, what="a tool_proposed")
        return conn.of_type("tool_proposed")[n - 1]["payload"]

    def run(self, run_id=RUN):
        return runs.get_run(USER, run_id)

    def rows(self):
        return list(self.ledger.rows)


def read_result(call_id, output="print('hello')\n"):
    return {"call_id": call_id, "ok": True, "output": output, "truncated": False, "trust": "local_project",
            "duration_ms": 3}


def read_call(path="a.py", call_id="prov_1"):
    return call("read_file", {"path": path}, call_id=call_id)


# =============================================================================================================
# A turn with no tool calls
# =============================================================================================================

def test_a_plain_turn_streams_writes_one_row_one_usage_event_and_the_run_stays_running(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("Hello there.", input=1000, cached=200, output=100)])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "hi")
        await stack.settled()
        assert conn.text() == "Hello there."
        assert conn.events("text_done")[-1]["stop_reason"] == "end_turn"
        assert conn.of_type("tool_proposed") == []
        (row,) = stack.rows()
        expected = credits_module.credits_for("groq", "openai/gpt-oss-120b", 1000, 200, 100)
        assert row == {"user_id": USER, "surface": "m2c", "run_id": RUN, "provider": "groq",
                       "model": "openai/gpt-oss-120b", "input_tokens": 1000, "cached_input_tokens": 200,
                       "output_tokens": 100, "credits": expected}
        (usage,) = conn.events("usage")
        assert usage == {"type": "usage", "model": "openai/gpt-oss-120b", "input_tokens": 1000,
                         "cached_input_tokens": 200, "output_tokens": 100, "credits": expected,
                         "run_credits_total": expected}
        assert stack.run().state == "running"                      # a finished turn is not a completed run
        assert stack.run().credits == pytest.approx(expected)      # the run's informational counter moved too
        assert stack.adapter.requests[0].messages[0].role == "system"
    go(run())


def test_the_loops_text_and_the_usage_event_come_in_the_streams_order(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("ok")])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn)
        await stack.settled()
        kinds = [f["payload"]["type"] for f in conn.sent if f["t"] == "run_event"]
        assert kinds.index("text_delta") < kinds.index("text_done") < kinds.index("usage")
        assert conn.seqs() == sorted(conn.seqs()) and len(set(conn.seqs())) == len(conn.seqs())
    go(run())


# =============================================================================================================
# A read round trip, with a device that can answer tool calls
# =============================================================================================================

def test_a_read_the_model_proposes_the_device_approves_and_answers_the_model_finishes(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("Let me look.", [read_call("src/a.py")]),
                                          priced("It prints hello.")])

    async def run():
        conn = online(registry, capabilities=("sandbox", "tools"))
        await stack.start(conn)
        await stack.ask(conn, "what does src/a.py do?")
        proposal = await stack.proposed(conn)
        assert (proposal["tool"], proposal["input"], proposal["risk"]) == ("read_file", {"path": "src/a.py"}, "R0")
        assert proposal["call_id"].startswith("call_") and proposal["call_id"] != "prov_1"     # re-minted by the loop
        await stack.say(conn, "approval", {"call_id": proposal["call_id"], "decision": "allow", "scope": "once"})
        await stack.say(conn, "tool_result", read_result(proposal["call_id"]))
        await stack.settled()
        (finished,) = conn.events("tool_finished")
        assert (finished["call_id"], finished["status"]) == (proposal["call_id"], "ok")
        assert conn.text() == "Let me look.It prints hello."
        assert [p["stop_reason"] for p in conn.events("text_done")] == ["tool_use", "end_turn"]
        # the model's second request carries the tool message, and both requests offered the tools
        tool_msg = stack.adapter.requests[1].messages[-1]
        assert tool_msg.role == "tool" and "print('hello')" in tool_msg.content
        assert "read_file" in stack.adapter.tool_names(0) and "read_file" in stack.adapter.tool_names(1)
        assert len(stack.rows()) == 2 and len(conn.events("usage")) == 2        # one row per model call
        assert stack.run().state == "running"
        order = [f["payload"].get("type", f["t"]) for f in conn.sent if f["seq"] > 2]
        assert order.index("tool_proposed") < order.index("tool_finished") < len(order) - 1
    go(run())


def test_a_read_that_the_device_denies_ends_the_call_and_the_model_is_told(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("", [read_call("secret.txt")]), priced("Understood.")])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "read it")
        proposal = await stack.proposed(conn)
        await stack.say(conn, "approval", {"call_id": proposal["call_id"], "decision": "deny", "scope": "once"})
        await stack.settled()
        (finished,) = conn.events("tool_finished")
        assert finished["status"] == "denied"
        assert loop_module.MSG_DENIED in stack.adapter.requests[1].messages[-1].content
        assert conn.text() == "Understood."
    go(run())


# =============================================================================================================
# The same turn from a device that cannot answer tool calls (the old plain prompt loop)
# =============================================================================================================

def test_a_device_without_tools_gets_no_tools_and_no_tool_proposed_it_is_a_plain_chat(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("Let me look.", [read_call("src/a.py")]),
                                          priced("I cannot open files in this session.")])

    async def run():
        conn = online(registry, capabilities=("sandbox",))               # no "tools"
        await stack.start(conn)
        await stack.ask(conn, "what does src/a.py do?")
        await stack.settled()
        assert conn.of_type("tool_proposed") == [] and conn.events("tool_finished") == []
        assert stack.adapter.requests[0].tools == () and stack.adapter.requests[1].tools == ()
        assert stack.broker.in_flight(RUN) == 0
        assert MSG_NO_TOOLS in stack.adapter.requests[1].messages[-1].content        # the model is told, once
        assert conn.text() == "Let me look.I cannot open files in this session."
        assert len(stack.rows()) == 2 and stack.run().state == "running"
        assert conn.errors == []
    go(run())


def test_a_device_without_tools_answers_a_plain_message_and_a_second_one_in_the_same_run(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("first"), priced("second")])

    async def run():
        conn = online(registry, capabilities=())
        await stack.start(conn)
        await stack.ask(conn, "one")
        await stack.settled()
        await stack.ask(conn, "two")
        await stack.settled()
        assert conn.text() == "firstsecond" and len(stack.rows()) == 2
        assert all(r.tools == () for r in stack.adapter.requests)
    go(run())


def test_the_capability_of_the_connection_that_asks_decides_not_the_one_that_started_the_run(w, registry, monkeypatch):
    """A run started from a tools-capable device and continued from a plain one gets no tools on the plain one."""
    stack = Stack(monkeypatch, w, script=[priced("a"), priced("b")])

    async def run():
        first = online(registry, capabilities=("tools",))
        await stack.start(first)
        await stack.ask(first, "one")
        await stack.settled()
        assert "read_file" in stack.adapter.tool_names(0)
        first.closed = True
        registry.remove(first)
        second = online(registry, capabilities=())                      # the same device id, another client
        await stack.ask(second, "two")
        await stack.settled()
        assert stack.adapter.requests[1].tools == ()
    go(run())


# =============================================================================================================
# Numbers only
# =============================================================================================================

def test_ledger_rows_and_usage_events_are_numbers_only_and_no_content_reaches_the_log(w, registry, monkeypatch, caplog):
    stack = Stack(monkeypatch, w, script=[priced(CANARY + " reply", [read_call(CANARY + ".py")]),
                                          priced("done " + CANARY)])

    async def run():
        conn = online(registry)
        with caplog.at_level(logging.DEBUG):
            await stack.start(conn)
            await stack.ask(conn, "please read " + CANARY)
            proposal = await stack.proposed(conn)
            await stack.say(conn, "tool_result", read_result(proposal["call_id"], CANARY + " file text"))
            await stack.settled()
        assert len(stack.rows()) == 2
        for row in stack.rows():
            assert set(row) == LEDGER_KEYS
            assert all(isinstance(v, (int, float)) for k, v in row.items() if k.endswith("tokens") or k == "credits")
            assert CANARY not in repr(row)
        for usage in conn.events("usage"):
            assert set(usage) == USAGE_KEYS and CANARY not in repr(usage)
        log = "\n".join(r.getMessage() for r in caplog.records)
        assert CANARY not in log
    go(run())


# =============================================================================================================
# Quota: the pool, then the run ceiling and S10's grant
# =============================================================================================================

def test_pool_exhausted_sends_the_error_then_paused_budget_calls_no_model_and_writes_no_row(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[], ledger=FakeLedger(pool=0.0))

    async def run():
        conn = online(registry)
        await stack.start(conn)
        msg = await stack.ask(conn, "hi")
        await stack.settled()
        (error,) = conn.errors
        assert error["code"] == "quota_exhausted" and error["ref_id"] == msg["id"]
        assert error["details"] == {"reset_at": stack.ledger.resets_at}
        assert conn.states()[-1] == "paused_budget" and stack.run().state == "paused_budget"
        assert stack.run().error_code == "quota_exhausted"
        assert stack.adapter.opened == 0 and stack.rows() == [] and conn.events("usage") == []
        assert not stack.emitter.is_bound(RUN)                       # a paused run holds no emitter state
    go(run())


def test_run_ceiling_reached_pauses_the_run_and_s10s_grant_resumes_the_turn_with_exact_rows(w, registry, monkeypatch):
    policy = metering.RunCeilingPolicy(fixed=2.0, chunk_fixed=5.0)
    stack = Stack(monkeypatch, w, policy=policy, credits=lambda provider, model, i, c, o: 2.0,
                  script=[priced("", [read_call("a.py")]), priced("Finished after the grant.")])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "read a.py then answer")
        proposal = await stack.proposed(conn)
        await stack.say(conn, "tool_result", read_result(proposal["call_id"]))
        await until(lambda: stack.run().state == "paused_budget", what="the ceiling pause")
        await stack.settled()
        assert [e["code"] for e in conn.errors] == ["quota_exhausted"]
        assert conn.errors[0]["details"] is None                    # a run ceiling does not reset: no reset_at
        assert len(stack.rows()) == 1 and stack.rows()[0]["credits"] == 2.0
        assert len(stack.adapter.requests) == 1                    # the second call was refused BEFORE it was made

        services = code_runs.GrantServices(
            check=stack.ledger.check, run_credits=stack.ledger.run_credits, store=stack.store, policy=policy,
            registry=lambda: registry, handlers=lambda: stack.handlers, emitter=lambda: stack.emitter)
        granted = await code_runs._grant(RUN, types.SimpleNamespace(user_id=USER, device_id=DEVICE), services)
        assert granted.state == "running" and granted.run_credit_ceiling == 7.0

        await until(lambda: conn.text().endswith("Finished after the grant."), what="the resumed reply")
        await stack.settled()
        assert stack.run().state == "running"
        assert [r["credits"] for r in stack.rows()] == [2.0, 2.0]    # exactly one row per model call, none for the pause
        assert len(conn.events("usage")) == 2
        # the resumed turn did not repeat the user's message
        users = [i for i in buffer.read_conversation(RUN).items if i["role"] == "user"]
        assert len(users) == 1
        assert not conn.events("notice") or all(n["code"] != "reply_restarted" for n in conn.events("notice"))
    go(run())


# =============================================================================================================
# Disconnect, resume, cancel
# =============================================================================================================

def interrupted_reply():
    return Reply(events=[TextDelta("The conn"), HANG])


def test_disconnect_mid_reply_then_resume_restarts_the_reply_with_the_notice_and_one_row(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[interrupted_reply(), priced("The connection survived.")])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "tell me")
        await until(lambda: conn.text() == "The conn", what="the partial reply")
        seen = conn.seqs()[-1]
        registry.remove(conn)                                       # ws.py: out of the registry, closed, on_disconnect
        conn.closed = True
        await stack.router.on_disconnect(conn)
        await until(lambda: stack.run().state == "paused_disconnected", what="the pause")
        await stack.settled()
        assert stack.adapter.closed == stack.adapter.opened == 1     # the provider stream was closed
        assert stack.rows() == []                                    # a cut call is not charged (S10 limit, as hello's)

        back = online(registry)
        await stack.say(back, "run.resume", {"last_seq": seen})
        await until(lambda: back.events("text_done"), what="the restarted reply")
        await stack.settled()
        kinds = [(f["seq"], f["payload"].get("type")) for f in back.sent if f["t"] == "run_event"]
        notice_at = next(seq for seq, kind in kinds if kind == "notice")
        first_delta = next(seq for seq, kind in kinds if kind == "text_delta")
        (notice,) = back.events("notice")
        assert notice["code"] == "reply_restarted" and notice["severity"] == "info"
        assert notice_at < first_delta                                # the client drops the partial text BEFORE the new one
        assert back.text() == "The connection survived."
        assert len(stack.rows()) == 1                                # one finished call, one row
        messages = stack.adapter.requests[1].messages
        assert [m.role for m in messages] == ["system", "user"]       # the user's message was not added twice
        assert stack.run().state == "running"
    go(run())


def test_a_resume_after_a_disconnect_with_nothing_streamed_sends_no_restart_notice(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[Reply(events=[HANG]), priced("late answer")])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "hello?")
        await until(lambda: stack.adapter.opened == 1, what="the model call")
        registry.remove(conn)
        conn.closed = True
        await stack.router.on_disconnect(conn)
        await until(lambda: stack.run().state == "paused_disconnected", what="the pause")
        await stack.settled()
        back = online(registry)
        await stack.say(back, "run.resume", {"last_seq": 2})
        await until(lambda: back.events("text_done"), what="the answer")
        await stack.settled()
        assert back.events("notice") == [] and back.text() == "late answer"
    go(run())


def test_cancel_closes_the_stream_ends_the_run_and_leaves_no_task_behind(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[interrupted_reply()])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "go on")
        await until(lambda: conn.text() == "The conn", what="the partial reply")
        await stack.say(conn, "cancel", {})
        await until(lambda: stack.run().state == "cancelled", what="the cancel")
        await stack.settled()
        await asyncio.sleep(0.05)
        assert stack.adapter.closed == stack.adapter.opened == 1
        assert stack.rows() == [] and conn.states()[-1] == "cancelled"
        assert stack.loop_tasks() == {} and stack.handlers.turn_runs() == ()
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert leftovers == [], leftovers
    go(run())


def test_cancel_while_a_tool_call_is_in_flight_cancels_the_call_too(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[priced("", [read_call("a.py")])])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "read it")
        await stack.proposed(conn)
        await stack.say(conn, "cancel", {})
        await until(lambda: stack.run().state == "cancelled", what="the cancel")
        await stack.settled()
        assert stack.broker.in_flight(RUN) == 0
        assert [p["status"] for p in conn.events("tool_finished")] == ["cancelled"]
        assert conn.states()[-1] == "cancelled"
    go(run())


# =============================================================================================================
# No provider, a missing secret
# =============================================================================================================

def test_no_provider_key_answers_provider_unavailable_pauses_the_run_and_sends_no_content(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[], chain=[])

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "anyone there? " + CANARY)
        await stack.settled()
        assert [e["code"] for e in conn.errors] == ["provider_unavailable"]
        assert conn.text() == "" and conn.events("usage") == [] and stack.rows() == []
        assert stack.run().state == "paused_error" and conn.states()[-1] == "paused_error"
        assert CANARY not in repr(conn.errors) and CANARY not in repr(conn.sent)
    go(run())


def test_a_missing_run_buffer_secret_refuses_the_run_before_anything_is_stored(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[])
    monkeypatch.delenv("RUN_BUFFER_SECRET")

    async def run():
        conn = Device()
        registry.add(conn)
        await stack.router.on_message(conn, envelope("run.start", start_payload(), run_id=RUN))
        assert [e["code"] for e in conn.errors] == ["internal"]       # a fixed text, no content, no secret name
        assert stack.adapter.opened == 0 and stack.rows() == [] and conn.sent == []
        with pytest.raises(runs.RunNotFound):
            runs.get_run(USER, RUN)
    go(run())


# =============================================================================================================
# The kill switch and a second install
# =============================================================================================================

def test_kill_switch_hello_runs_the_old_turn_runner_and_never_builds_the_loop(w, registry, monkeypatch):
    ledger = FakeLedger(pool=1000.0)
    monkeypatch.setattr(usage_ledger, "check", ledger.check)
    monkeypatch.setattr(usage_ledger, "commit", ledger.commit)
    stack = Stack(monkeypatch, w, wire=False, env={"M2C_LOOP": "hello"})
    monkeypatch.setattr(hello, "_ADAPTER", ScriptedAdapter(priced("plain hello path")))
    wiring.install()
    broker_module.install(stack.router, broker=stack.broker, handlers=stack.handlers)

    async def run():
        conn = online(registry, capabilities=("tools",))
        assert stack.handlers.get_turn_runner() is hello.run_turn and wiring._BUILT is None
        await stack.start(conn)
        await stack.ask(conn, "hi")
        await until(lambda: conn.events("usage"), what="hello's usage event")
        assert conn.text() == "plain hello path" and len(ledger.rows) == 1
        assert conn.of_type("tool_proposed") == []
    go(run())


def test_install_twice_still_means_one_reply_one_row_and_one_restart(w, registry, monkeypatch):
    stack = Stack(monkeypatch, w, script=[interrupted_reply(), priced("only once")])
    wiring.install()                                                  # again: it must change nothing

    async def run():
        conn = online(registry)
        await stack.start(conn)
        await stack.ask(conn, "go")
        await until(lambda: conn.text() == "The conn", what="the partial reply")
        seen = conn.seqs()[-1]
        registry.remove(conn)
        conn.closed = True
        await stack.router.on_disconnect(conn)
        await until(lambda: stack.run().state == "paused_disconnected", what="the pause")
        await stack.settled()
        back = online(registry)
        await stack.say(back, "run.resume", {"last_seq": seen})
        await until(lambda: back.events("text_done"), what="the restarted reply")
        await stack.settled()
        await asyncio.sleep(0.05)
        assert len(back.events("notice")) == 1 and back.text() == "only once"
        assert len(stack.rows()) == 1 and len(stack.adapter.requests) == 2
    go(run())


# =============================================================================================================
# server.py's own startup lines
# =============================================================================================================

def _server_install_statements():
    """The module-level `codeagent_*.install()` statements of api/server.py, in file order."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "install"
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id.startswith("codeagent_")):
            out.append((node.value.func.value.id, node))
    return out


def test_server_startup_executes_the_real_install_lines_and_ends_with_the_loop_not_hello(w, monkeypatch):
    stack = Stack(monkeypatch, w, wire=False)
    monkeypatch.setattr(router_module, "_ROUTER", stack.router)
    namespace = {"codeagent_run_handlers": run_handlers, "codeagent_wiring": wiring, "codeagent_broker": broker_module}
    statements = _server_install_statements()
    assert [alias for alias, _ in statements] == ["codeagent_run_handlers", "codeagent_wiring", "codeagent_broker"]
    for _alias, node in statements:                                   # executed in the order the file has them
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVER), "exec"), namespace)

    handlers = run_handlers.get_handlers()
    active = loop_module.get_loop()
    assert handlers.get_turn_runner() is loop_module.run_turn and active is wiring._BUILT
    assert active._broker is broker_module.get_broker()               # the loop and the router share one broker
    for name, hooks in (("on_resume", handlers.on_resume_hooks()), ("on_cancel", handlers.on_cancel_hooks()),
                        ("on_run_disconnect", handlers.on_run_disconnect_hooks())):
        assert active.hooks[name] in hooks
    for hooks, fn in ((handlers.on_resume_hooks(), hello._on_resume), (handlers.on_cancel_hooks(), hello._on_cancel),
                      (handlers.on_run_disconnect_hooks(), hello._on_run_disconnect)):
        assert fn not in hooks
    assert broker_module.get_broker().on_cancel in handlers.on_cancel_hooks()


# =============================================================================================================
# S04's real socket: "tools" in hello.capabilities is what turns tools on
# =============================================================================================================

def recv_until(dev, predicate, limit=40):
    seen = []
    for _ in range(limit):
        frame = dev.recv()
        seen.append(frame)
        if predicate(frame):
            return seen
    raise AssertionError("the expected frame never came: " + repr([f["t"] for f in seen]))


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def wire_the_socket(rig, w, monkeypatch, script, ledger=None):
    stack = Stack(monkeypatch, w, script=script, ledger=ledger)
    rig.use_handler(stack.router)
    rig.check = check_valid
    return stack


def test_socket_a_device_that_declares_tools_gets_a_tool_proposed_and_a_round_trip(rig, w, monkeypatch):
    stack = wire_the_socket(rig, w, monkeypatch, [priced("Looking.", [read_call("a.py")]), priced("Done reading.")])
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential, capabilities=("sandbox", "tools")) as (dev, _welcome):
        dev.send(envelope("run.start", start_payload(), run_id=RUN))
        dev.recv(), dev.recv()
        dev.send(envelope("user_message", {"text": "read a.py"}, run_id=RUN))
        seen = recv_until(dev, lambda f: f["t"] == "tool_proposed")
        proposal = seen[-1]["payload"]
        dev.send(envelope("tool_result", read_result(proposal["call_id"]), run_id=RUN))
        assert [f for f in seen if f["t"] == "run_event" and f["payload"]["type"] == "usage"]   # call 1's usage came first
        recv_until(dev, lambda f: f["t"] == "run_event" and f["payload"]["type"] == "usage")      # then call 2's
        dev.ping_pong()
    assert "read_file" in stack.adapter.tool_names(0)
    assert len(stack.ledger.rows) == 2


def test_socket_a_device_that_declares_no_tools_is_never_sent_a_tool_proposed(rig, w, monkeypatch):
    stack = wire_the_socket(rig, w, monkeypatch,
                            [priced("Looking.", [read_call("a.py")]), priced("I cannot read files.")])
    credential, _ = rig.store.add_device("user-a")
    with rig.session(credential, capabilities=("sandbox",)) as (dev, _welcome):
        dev.send(envelope("run.start", start_payload(), run_id=RUN))
        dev.recv(), dev.recv()
        dev.send(envelope("user_message", {"text": "read a.py"}, run_id=RUN))
        counted = []
        seen = recv_until(dev, lambda f: counted.append(f) is None and len(
            [g for g in counted if g["t"] == "run_event" and g["payload"]["type"] == "usage"]) == 2)
        dev.ping_pong()
    frames = seen
    assert not [f for f in frames if f["t"] == "tool_proposed"]
    assert stack.adapter.requests[0].tools == () and stack.adapter.requests[1].tools == ()
    assert len(stack.ledger.rows) == 2
