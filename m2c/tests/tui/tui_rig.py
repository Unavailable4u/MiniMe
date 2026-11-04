"""
m2c/tests/tui/tui_rig.py -- Patch C09b: the shared rig of the terminal-chat tests that need a link.

Why a plain module and not a conftest.py: tests/test_doctor.py does ``from conftest import ...`` (see
tests/link/link_fakes.py and tests/runner/runner_rig.py for the same reason).

WHAT IS REAL AND WHAT IS FAKE
-----------------------------
Real: the C04 device link talking over a real websocket to C04's ``FakeServer``, the C24 tool runner, the C05/C07
tools on a temporary project, the C06 policy engine, the C08 checkpoints and the C02 store (all under the hermetic
``$M2C_HOME``), and the C09a pure modules.  Fake: the server's behavior (``Chat``, a scripted model), the person
(``FakeUI`` and the calls a test makes on the session) and, for the grant, the HTTP call.

Fake tokens are not needed; markers are plain words.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "link"))

from link_fakes import *  # noqa: E402,F401,F403  (the autouse fixtures come along, as in the other link-based tests)
from link_fakes import FakeServer, do_handshake, env, make_link, run_async, until  # noqa: E402

from m2c.store import open_db, read_events  # noqa: E402
from m2c.tui.session import Session  # noqa: E402


class FakeUI:
    """Everything the session shows, kept for assertions.  Implements the UI protocol without a terminal."""

    def __init__(self) -> None:
        self.blocks: list[Any] = []
        self.status: list[str] = []
        self.prompt: tuple[str, str] = ("", "")
        self.prompts: list[tuple[str, str]] = []

    def show(self, blocks) -> None:
        self.blocks.extend(blocks)

    def set_status(self, lines) -> None:
        self.status = list(lines)

    def set_prompt(self, label: str, text: str = "") -> None:
        self.prompt = (label, text)
        self.prompts.append((label, text))

    async def run(self, controller) -> None:
        await controller.exit_requested.wait()

    def text(self) -> str:
        """The shown lines (streamed pieces joined), one per line."""
        out: list[str] = []
        partial = ""
        for b in self.blocks:
            if b.kind == "text":
                partial += b.text
                while "\n" in partial:
                    first, partial = partial.split("\n", 1)
                    out.append(first)
            elif b.kind == "line":
                if partial:
                    out.append(partial)
                    partial = ""
                    if not b.text:
                        continue
                out.append(b.text)
            elif b.kind == "diff":
                out.append(f"DIFF {b.text} hidden={b.hidden}")
                out.extend(f"{l.kind}:{l.text}" for l in b.lines)
        if partial:
            out.append(partial)
        return "\n".join(out)

    def has(self, needle: str) -> bool:
        return needle in self.text()


class Chat:
    """A scripted server.  ``on_message(chat, msg)`` is called for every client message after the basics: run.start is
    answered with ``created``, cancel with ``cancelled``.  ``say`` and ``propose`` build valid server events."""

    def __init__(self, on_message: Callable[["Chat", dict], Awaitable[None]] | None = None) -> None:
        self.on_message = on_message
        self.conn = None
        self.seq: dict[str, int] = {}
        self.starts: list[dict] = []          # run.start envelopes
        self.user_messages: list[tuple[str, str]] = []
        self.approvals: list[dict] = []
        self.results: list[dict] = []
        self.cancels: list[str] = []
        self.answer_cancel = True

    async def script(self, conn) -> None:
        self.conn = conn
        await do_handshake(conn)
        while True:
            msg = await conn.recv(timeout=None)
            run_id = msg.get("run_id")
            kind = msg["t"]
            if kind == "run.start":
                self.starts.append(msg)
                await self.emit(run_id, type="state_changed", state="created")
            elif kind == "user_message":
                self.user_messages.append((run_id, msg["payload"]["text"]))
            elif kind == "approval":
                self.approvals.append(msg["payload"])
            elif kind == "tool_result":
                self.results.append(msg["payload"])
            elif kind == "cancel":
                self.cancels.append(run_id)
                if self.answer_cancel:
                    await self.emit(run_id, type="state_changed", state="cancelled")
            if self.on_message is not None:
                await self.on_message(self, msg)

    async def emit(self, run_id: str, **payload: Any) -> None:
        self.seq[run_id] = self.seq.get(run_id, 0) + 1
        await self.conn.send(env("run_event", payload, run_id=run_id, seq=self.seq[run_id]))

    async def say(self, run_id: str, text: str, *, stop: str = "end_turn", credits: float = 0.5,
                  total: float | None = None, input_tokens: int = 1000) -> None:
        """One model step: running, the text in two pieces, text_done, usage."""
        await self.emit(run_id, type="state_changed", state="running")
        half = max(len(text) // 2, 1)
        for piece in (text[:half], text[half:]):
            if piece:
                await self.emit(run_id, type="text_delta", text=piece)
        await self.emit(run_id, type="text_done", stop_reason=stop)
        usage = dict(type="usage", model="fake-model-1", input_tokens=input_tokens, cached_input_tokens=0,
                     output_tokens=10, credits=credits)
        if total is not None:
            usage["run_credits_total"] = total
        await self.emit(run_id, **usage)

    async def propose(self, run_id: str, call_id: str, tool: str, tool_input: dict, risk: str = "R1") -> None:
        self.seq[run_id] = self.seq.get(run_id, 0) + 1
        await self.conn.send(env("tool_proposed", {"call_id": call_id, "tool": tool, "input": tool_input, "risk": risk},
                                 run_id=run_id, seq=self.seq[run_id]))

    async def finish(self, run_id: str, call_id: str, tool: str, status: str = "ok") -> None:
        await self.emit(run_id, type="tool_finished", call_id=call_id, tool=tool, status=status, duration_ms=5)


def call_id() -> str:
    return "call_" + uuid.uuid4().hex[:24]


class Rig:
    """``async with Rig(tmp_path, chat) as rig`` gives a started session on a temporary project, a fake UI and the
    store.  ``rig.project`` is the project folder."""

    def __init__(self, tmp_path: Path, chat: Chat, *, mode: str = "ask", files: dict[str, str] | None = None,
                 grant_fn=None, executor=None, cancel_wait_s: float = 2.0, clock=None) -> None:
        self.chat = chat
        self.project = tmp_path / "proj"
        self.project.mkdir()
        for name, text in (files or {}).items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.mode = mode
        self.grant_fn = grant_fn
        self.executor = executor
        self.cancel_wait_s = cancel_wait_s
        self.clock = clock
        self.ui = FakeUI()
        self.server = FakeServer(chat.script)
        self.store = None
        self.session: Session | None = None

    async def __aenter__(self) -> "Rig":
        await self.server.__aenter__()
        self.store = open_db()
        options: dict[str, Any] = {}
        if self.grant_fn is not None:
            options["grant_fn"] = self.grant_fn
        if self.executor is not None:
            options["executor"] = self.executor
        if self.clock is not None:
            options["clock"] = self.clock
        self.session = Session(
            self.store, self.ui, project_path=str(self.project), mode=self.mode,
            link_factory=lambda store, **kw: make_link(store, self.server, **kw),
            cancel_wait_s=self.cancel_wait_s, **options)
        failed = await self.session.start(5.0)
        assert failed is None, self.ui.text()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await self.session.close()
        finally:
            self.store.close()
            await self.server.__aexit__(*exc)

    # -- helpers -----------------------------------------------------------------------------------------------
    async def send(self, text: str, *, wait: bool = True) -> None:
        self.session.submit(text)
        if wait:
            await self.session.wait_idle(5.0)

    async def until(self, predicate, what: str = "condition", timeout: float = 8.0) -> None:
        await until(predicate, timeout, what)

    def log(self, session_id: str | None = None) -> list[tuple[str, Any]]:
        return [(e.type, e.data) for e in read_events(self.store, session_id or self.session.conversation_id)]

    def log_types(self, session_id: str | None = None) -> list[str]:
        return [t for t, _ in self.log(session_id)]


async def ask_once(rig: Rig, key: str) -> None:
    """Wait for an approval prompt and press ``key``."""
    await rig.until(lambda: rig.session.approval_active, "an approval prompt")
    rig.session.on_key(key)


__all__ = [name for name in dir() if not name.startswith("__") and name not in {"annotations"}]
