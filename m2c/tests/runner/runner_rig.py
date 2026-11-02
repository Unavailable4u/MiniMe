"""
m2c/tests/runner/runner_rig.py -- Patch C24: the shared rig of the device tool runner tests.

Why a plain module and not a conftest.py: tests/test_doctor.py does ``from conftest import ...`` (see
tests/headless/headless_rig.py for the same reason).  Each test file imports what it needs from here.

WHAT IS REAL AND WHAT IS FAKE
-----------------------------
Real: the C05 tools (``run_tool`` on a temporary project directory), the C06 policy engine (real rule layers), the
C07 shell tool (real ``bash`` processes), the C08 checkpoints (real blobs under the hermetic ``$M2C_HOME``) and the C02
store (a real SQLite file).  Fake: the link (``FakeLink``: records what the runner sends and checks every payload
against the client's own copy of the protocol schemas, through the link's own strict validator), the person (``ask``
callables the test writes) and, for the ordering tests only, the executor (an injected ``run_tool`` stand-in).

Nothing here looks like a secret: markers are plain words, fake tokens are not needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from m2c.link import LinkEvent, LinkNotConnectedError
from m2c.link.client import _validate_outgoing
from m2c.policy import Context, RuleLayers, RuleSet
from m2c.policy.rules import parse_rule
from m2c.runner import ToolRunner
from m2c.store import create_session, open_db
from m2c.tools import ToolContext

RUN_ID = "run_runner_test_01"
MARKER = "quokka-needle-7431"          # text that must never reach a log line or the audit record


def run(coro, timeout: float = 30.0):
    """Run a scenario under a hard timeout so a bug hangs one test, not CI."""

    async def guarded():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(guarded())


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def proposed(call_id: str, tool: str, input: Any, risk: str = "R0", *, seq: int = 1, run_id: str = RUN_ID) -> LinkEvent:
    """A ``tool_proposed`` as the link hands it to callbacks."""
    return LinkEvent(run_id, seq, str(uuid.uuid4()), now_ts(), "tool_proposed",
                     {"call_id": call_id, "tool": tool, "input": input, "risk": risk})


def run_event(payload: dict, *, seq: int = 2, run_id: str = RUN_ID) -> LinkEvent:
    return LinkEvent(run_id, seq, str(uuid.uuid4()), now_ts(), "run_event", payload)


def finished(call_id: str, tool: str, status: str, *, seq: int = 2) -> LinkEvent:
    return run_event({"type": "tool_finished", "call_id": call_id, "tool": tool, "status": status}, seq=seq)


def state_changed(state: str, *, seq: int = 3) -> LinkEvent:
    return run_event({"type": "state_changed", "state": state}, seq=seq)


class FakeLink:
    """What the runner needs from ``DeviceLink``: ``send_approval``, ``send_tool_result``, ``send_tool_stream``, with
    the same signatures.  Every payload is checked against the protocol schema exactly as the real link would."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail_on: set[str] = set()                 # message types that raise LinkNotConnectedError
        self.fail_with: Callable[[str], BaseException] = lambda t: LinkNotConnectedError()
        self.attempts: list[str] = []
        self.on_send: Callable[[str, dict], None] | None = None

    async def _send(self, t: str, payload: dict) -> str:
        self.attempts.append(t)
        _validate_outgoing(t, payload)                  # ValueError on a payload the server would reject
        if t in self.fail_on:
            raise self.fail_with(t)
        self.sent.append((t, payload))
        if self.on_send is not None:
            self.on_send(t, payload)
        await asyncio.sleep(0)
        return str(uuid.uuid4())

    async def send_approval(self, run_id: str, call_id: str, *, decision: str, scope: str = "once") -> str:
        assert run_id == RUN_ID
        return await self._send("approval", {"call_id": call_id, "decision": decision, "scope": scope})

    async def send_tool_result(self, run_id: str, call_id: str, *, ok: bool, output: str, truncated: bool,
                               trust: str, exit_code: int | None = None, duration_ms: int | None = None) -> str:
        assert run_id == RUN_ID
        payload: dict[str, Any] = {"call_id": call_id, "ok": ok, "output": output, "truncated": truncated, "trust": trust}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return await self._send("tool_result", payload)

    async def send_tool_stream(self, run_id: str, call_id: str, *, stream: str, chunk: str) -> str:
        assert run_id == RUN_ID
        return await self._send("tool_stream", {"call_id": call_id, "stream": stream, "chunk": chunk})

    # -- reading what was sent --------------------------------------------------------------------------------
    def types(self) -> list[str]:
        return [t for t, _ in self.sent]

    def payloads(self, t: str) -> list[dict]:
        return [p for kind, p in self.sent if kind == t]

    def for_call(self, call_id: str, t: str | None = None) -> list[tuple[str, dict]]:
        return [(k, p) for k, p in self.sent if p.get("call_id") == call_id and (t is None or k == t)]

    def result(self, call_id: str) -> dict:
        (payload,) = [p for k, p in self.sent if k == "tool_result" and p["call_id"] == call_id]
        return payload

    def approval(self, call_id: str) -> dict:
        (payload,) = [p for k, p in self.sent if k == "approval" and p["call_id"] == call_id]
        return payload


def make_layers(allow=(), ask=(), deny=()) -> RuleLayers:
    rules = [parse_rule(r, "allow", "user") for r in allow]
    rules += [parse_rule(r, "ask", "user") for r in ask]
    rules += [parse_rule(r, "deny", "user") for r in deny]
    return RuleLayers(user=RuleSet(tuple(rules)))


@dataclass
class Rig:
    root: Path
    store: Any
    link: FakeLink
    runner: ToolRunner
    tool_context: ToolContext
    state: dict = field(default_factory=dict)     # mutable: the test changes mode / layers between calls
    asked: list = field(default_factory=list)     # every ApprovalRequest the fake person saw

    @property
    def session_id(self) -> str:
        return RUN_ID

    def propose(self, call_id: str, tool: str, input: Any, risk: str = "R0", *, seq: int = 1) -> None:
        self.runner.on_tool_proposed(proposed(call_id, tool, input, risk, seq=seq))

    async def call(self, call_id: str, tool: str, input: Any, risk: str = "R0") -> None:
        """Propose one call and wait until the runner has finished with everything."""
        self.propose(call_id, tool, input, risk)
        await self.runner.wait_idle()

    def approvals_rows(self) -> list[tuple]:
        return list(self.store.execute(
            "SELECT call_id, tool, decision, scope, decided_by, input_summary FROM approvals "
            "WHERE session_id = ? ORDER BY seq", (RUN_ID,)))


def make_rig(tmp_path: Path, *, mode: str = "ask", allow=(), ask=(), deny=(), ask_fn=None, trusted: bool = True,
             **runner_kwargs: Any) -> Rig:
    """A project directory, a store with its session, a fake link and a runner wired to the real C05/C06/C07/C08."""
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    store = open_db()
    create_session(store, project_path=str(root), mode=mode, session_id=RUN_ID)
    link = FakeLink()
    tool_context = ToolContext(root)
    state: dict = {"mode": mode, "layers": make_layers(allow, ask, deny)}
    home = os.environ["HOME"]

    def policy() -> Context:
        return Context(mode=state["mode"], layers=state["layers"], project_root=str(root),
                       project_trusted=trusted, home=home)

    runner_kwargs.setdefault("replay_settle_s", 0.0)
    rig = Rig(root=root, store=store, link=link, runner=None, tool_context=tool_context, state=state)  # type: ignore[arg-type]
    if ask_fn is not None:
        async def asking(request):
            rig.asked.append(request)
            result = ask_fn(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        runner_kwargs["ask"] = asking
    rig.runner = ToolRunner(link, run_id=RUN_ID, tool_context=tool_context, policy_context=policy, store=store,
                            **runner_kwargs)
    return rig


class LogCapture(logging.Handler):
    """Collects what ``m2c.runner`` logs, on its OWN handler.  caplog depends on propagation, and other tests in the
    suite (anything that calls ``configure_logging``) leave the ``m2c`` logger with ``propagate = False``."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def capture_runner_logs(monkeypatch) -> LogCapture:
    logger = logging.getLogger("m2c.runner")
    capture = LogCapture()
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, capture])
    return capture


def process_running(marker: str) -> bool:
    """True while some process has ``marker`` in its command line (a /proc scan: no pgrep needed)."""
    needle = marker.encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                if needle in handle.read().replace(b"\x00", b" "):
                    return True
        except OSError:
            continue
    return False
