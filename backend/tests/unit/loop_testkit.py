"""Shared helpers for the S07 agent-loop tests (not collected: no test_ prefix).

  ScriptedAdapter   a ModelAdapter that plays a script: one entry per model call. An entry is a Reply (text, tool
                    calls, usage), a list of raw events (HANG waits for ever, so a test can cancel mid-stream), or
                    an exception to raise. It records every ModelRequest it was given and how many streams were
                    closed (a cancelled loop must close the provider stream).
  FakeBroker        the loop's side of S09's ToolBroker: propose() / cancel_all(). Each call is answered by a
                    behaviour (ok, error, denied, timeout, disconnected, hold until cancelled, approve-then-ok),
                    and the fake records the calls, how many were in flight at once and the run's state at the
                    moment of each call. It emits nothing: the real-broker tests use codeagent.broker itself.
  Rig               one run in the real S06 modules (runs, buffer, run_manager, run_guard) over the in-memory
                    twins of run_manager_testkit.World, plus a recording emitter and a fake device connection.
                    Only the model, the broker (mostly), tool_budget and the connection are fakes.

Fake secrets are built at run time (gitleaks flags literals shaped like keys); nothing here is one.
"""
import asyncio
import copy
import json
import uuid
from pathlib import Path

from codeagent import broker as broker_module, buffer, run_manager, runs
from codeagent.connections import ConnectionClosed
from codeagent.loop import AgentLoop, LoopConfig, ModelChoice, ToolCatalog
from codeagent.model_adapter import ModelAdapter, TextDelta, ToolCall, Usage
from tests.unit.run_manager_testkit import USER, World, start_payload

ROOT = Path(__file__).resolve().parents[3]
TOOLS_DIR = ROOT / "protocol" / "v1" / "tools"
CANARY = "CANARY-loop-content-do-not-log-6b2f"

HANG = object()                      # a script event: wait for ever (the test cancels)


def catalog() -> ToolCatalog:
    return ToolCatalog({p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(TOOLS_DIR.glob("*.json"))})


# ---------------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------------

def call(name, arguments, call_id="prov_1"):
    return ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments))


def bad_call(error="malformed_arguments", name="read_file", call_id="prov_bad"):
    return ToolCall(id=call_id, name=name, arguments=None, raw_arguments="{" + CANARY, error=error)


class Reply:
    """One scripted model call."""

    def __init__(self, text="", calls=(), usage=True, events=None):
        self.text, self.calls = text, tuple(calls)
        self.usage = usage
        self.raw = events

    def events(self):
        if self.raw is not None:
            return list(self.raw)
        out = []
        if self.text:
            half = max(len(self.text) // 2, 1)
            out += [TextDelta(self.text[:half]), TextDelta(self.text[half:])] if len(self.text) > 1 else [TextDelta(self.text)]
        out += list(self.calls)
        if self.usage:
            out.append(Usage(input=100, cached_input=10, output=20, model="scripted-model", provider="scripted",
                             stop_reason="tool_use" if self.calls else "end_turn"))
        return out


class ScriptedAdapter(ModelAdapter):
    def __init__(self, *steps, name="adapter"):
        self.name = name
        self.steps = list(steps)
        self.requests = []
        self.opened = 0
        self.closed = 0

    def stream(self, request):
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("the model was called more often than the script has entries")
        return self._play(self.steps.pop(0))

    async def _play(self, step):
        self.opened += 1
        try:
            if isinstance(step, BaseException):
                raise step
            for event in (step.events() if isinstance(step, Reply) else step):
                if event is HANG:
                    await asyncio.Event().wait()
                if isinstance(event, BaseException):
                    raise event
                if callable(event):
                    result = event()
                    if asyncio.iscoroutine(result):
                        await result
                    continue
                await asyncio.sleep(0)
                yield event
        finally:
            self.closed += 1

    @property
    def last_messages(self):
        return self.requests[-1].messages

    def tool_names(self, index=-1):
        return {t.name for t in self.requests[index].tools}


# ---------------------------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------------------------

def outcome(status, output=None, *, tool="read_file", call_id="x", ok=None, truncated=False, origin="local_project"):
    ok = (status == "ok") if ok is None else ok
    return broker_module.ToolOutcome(call_id=call_id, tool=tool, status=status, ok=ok, output=output,
                                     truncated=truncated, omitted_bytes=0, trust="untrusted", origin=origin,
                                     exit_code=None, duration_ms=1)


class FakeBroker:
    """`behaviour(call) -> spec`; call is the propose() keyword arguments. A spec is a status word, or a dict
    {status, output, delay, approve, hold, truncated}. The default is ok with output "ok:<tool>"."""

    def __init__(self, behaviour=None, rig=None):
        self.behaviour = behaviour or (lambda c: "ok")
        self.rig = rig
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.cancel_all_calls = []
        self._held = {}
        self.cancelled = asyncio.Event()
        self.states_seen = []

    async def propose(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        spec = self.behaviour(kw)
        spec = {"status": spec} if isinstance(spec, str) else dict(spec)
        status = spec.get("status", "ok")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.rig is not None:
                self.states_seen.append(runs.get_run(USER, kw["run_id"]).state)
            if spec.get("gate") is not None:
                await spec["gate"].wait()
            if spec.get("approve"):
                await asyncio.sleep(0.01)
                await kw["on_approval"](kw["call_id"], "allow", "once")
                if self.rig is not None:
                    self.states_seen.append(runs.get_run(USER, kw["run_id"]).state)
            if spec.get("hold"):
                event = self._held[kw["call_id"]] = asyncio.Event()
                await event.wait()
                status = "cancelled"
            if spec.get("delay"):
                await asyncio.sleep(spec["delay"])
            if status == "denied":
                await kw["on_approval"](kw["call_id"], "deny", "once")
            output = spec.get("output", f"ok:{kw['tool']}") if status in ("ok", "error") else None
            return outcome(status, output, tool=kw["tool"], call_id=kw["call_id"],
                           truncated=spec.get("truncated", False))
        finally:
            self.active -= 1

    async def cancel_all(self, run_id):
        self.cancel_all_calls.append(run_id)
        self.cancelled.set()
        for event in self._held.values():
            event.set()
        return len(self._held)


# ---------------------------------------------------------------------------------------------
# The connection, the recorders
# ---------------------------------------------------------------------------------------------

class FakeConn:
    def __init__(self, user_id=USER, device_id="3f1c2d4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f", log=None):
        self.user_id, self.device_id = user_id, device_id
        self.errors = []
        self.closed = False
        self.log = log if log is not None else []

    async def send_error(self, code, message, *, ref_id=None, details=None):
        if self.closed:
            raise ConnectionClosed("connection closed")
        self.errors.append({"code": code, "message": message, "ref_id": ref_id, "details": details})
        self.log.append(("error", code))

    async def send_json(self, envelope):
        if self.closed:
            raise ConnectionClosed("connection closed")


class Recorder:
    """A recording RunEmitter.emit. The seq numbers are ITS job (the loop never sets one)."""

    def __init__(self, log=None):
        self.events = []
        self.finals = []
        self.unbound = []
        self.log = log if log is not None else []
        self._seq = {}
        self.fail_on = {}

    async def __call__(self, run_id, t, payload):
        await asyncio.sleep(0)
        kind = payload.get("type", t)
        if kind in self.fail_on:
            raise self.fail_on[kind]
        seq = self._seq[run_id] = self._seq.get(run_id, 0) + 1
        env = {"v": 1, "t": t, "id": str(uuid.uuid4()), "run_id": run_id, "seq": seq,
               "ts": "2026-11-02T09:00:00.000Z", "payload": copy.deepcopy(payload)}
        self.events.append(env)
        self.log.append(("emit", kind, payload.get("state") or payload.get("code")))
        return env

    async def final(self, run_id, state, reason=None):
        self.finals.append((run_id, state, reason))
        self.log.append(("final", state, reason))

    def unbind(self, run_id):
        self.unbound.append(run_id)

    def of(self, kind):
        return [e["payload"] for e in self.events if e["payload"].get("type", e["t"]) == kind]

    def states(self):
        return [(p["state"], p.get("reason")) for p in self.of("state_changed")]

    def notices(self):
        return self.of("notice")

    def text(self):
        return "".join(p["text"] for p in self.of("text_delta"))


class FakeBudget:
    """tool_budget twin: increment(run_id) -> the new count."""

    def __init__(self, start=0, fail=False):
        self.count = start
        self.fail = fail

    def increment(self, run_id):
        if self.fail:
            raise RuntimeError("redis is down " + CANARY)
        self.count += 1
        return self.count


class FakeMetering:
    def __init__(self, pause=None, fail_before=None, fail_after=None):
        self.before = 0
        self.after = []
        self.pause, self.fail_before, self.fail_after = pause, fail_before, fail_after

    async def before_call(self):
        self.before += 1
        if self.pause is not None:
            raise self.pause
        if self.fail_before is not None:
            raise self.fail_before

    async def after_call(self, usage):
        self.after.append(usage)
        if self.fail_after is not None:
            raise self.fail_after


# ---------------------------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------------------------

STRONG = ModelChoice("scripted", "model-strong", "strong")
LIGHT = ModelChoice("scripted", "model-light", "light")


def user_msg(run_id, text="please help"):
    return {"v": 1, "t": "user_message", "id": str(uuid.uuid4()), "run_id": run_id,
            "ts": "2026-11-02T09:00:00.000Z", "payload": {"text": text}}


class Rig:
    def __init__(self, monkeypatch, *script, mode="ask", chain=None, broker=None, adapters=None, history=None,
                 metering=None, config=None, budget=None, state_over=None, broker_behaviour=None, **loop_kw):
        self.world = World(monkeypatch)
        payload = start_payload(history if history is not None else [], mode=mode, **(state_over or {}))
        run = self.world.start(payload=payload)
        runs.transition(USER, run.id, "running")
        self.run_id = run.id
        self.log = []
        self.rec = Recorder(self.log)
        self.conn = FakeConn(log=self.log)
        self.adapter = ScriptedAdapter(*script)
        self.broker = broker or FakeBroker(broker_behaviour, rig=self)
        self.budget = budget or FakeBudget()
        self.metering = metering
        kw = dict(chain=chain or [STRONG], broker=self.broker, emit=self.rec, emit_final=self.rec.final,
                  unbind=self.rec.unbind, catalog=catalog(), tool_budget=self.budget, metering=metering,
                  config=config or LoopConfig(cancel_poll_s=0.01, step_limit=20))
        if adapters is not None:
            kw["adapter_factory"] = lambda choice: adapters[choice.model]
        else:
            kw["adapter"] = self.adapter
        kw.update(loop_kw)
        self.loop = AgentLoop(**kw)

    # -- the run, as the server sees it ---------------------------------------------------------
    @property
    def run(self):
        return runs.get_run(USER, self.run_id)

    @property
    def state(self):
        return self.run.state

    def conversation(self):
        return buffer.read_conversation(self.run_id)

    def items(self):
        return self.conversation().items

    def message(self, text="please help"):
        return user_msg(self.run_id, text)

    async def turn(self, text="please help"):
        return await self.loop.run_turn(self.conn, self.run, self.message(text))

    async def resume(self):
        """What S14's run.resume does before it calls the hook: bring the run out of a pause."""
        current = self.run
        if current.state in run_manager.RESUME_FROM:
            run_manager.resume_run(user_id=USER, device_id=current.device_id, run_id=self.run_id, last_seq=0)
        return await self.loop.resume(self.conn, self.run)

    def teardown(self):
        self.world.teardown()


def go(coro):
    return asyncio.run(coro)


def protocol_problems(events):
    """Every emitted event, as the envelope the device would get, checked against protocol/v1."""
    from codeagent import ws
    protocol = ws.load_protocol()
    problems = []
    for env in events:
        issues = protocol.validate_message(env)
        problems += [f"{env['payload'].get('type', env['t'])}: {issue}" for issue in issues]
    return problems
