"""
m2c/src/m2c/tui/session.py -- Patch C09b: the session controller, the client wiring behind the terminal chat.

WHAT THIS IS
------------
``Session`` joins the pieces C09a made (render, approval, status, history, complete, grant) to the pieces the earlier
patches made (the device link C04, the tool runner C24, the policy engine C06, tools C05/C07, checkpoints C08, the
local store C02, the context loader C12).  It has NO terminal code: everything it shows goes through the small
``UI`` protocol (app.py), so the whole flow is tested with a fake UI against C04's fake server, and the prompt_toolkit
layer stays thin.

ONE LOOP, ONE LINK.  Everything runs on the single asyncio loop that carries the ``DeviceLink``.  The store connection
belongs to that thread (C24 says so too).  Link callbacks are synchronous and quick; slow work is a task.

THE CONVERSATION AND ITS RUNS
-----------------------------
P00 has no softer interrupt than ``cancel``, and cancel ends the run; a rewind leaves the server's copy of the
conversation out of step with the files; a run parked for budget or an error takes no more messages.  So a
conversation is a CHAIN of runs, and the device keeps the conversation:

* the CONVERSATION SESSION (one row in the C02 ``sessions`` table, made at startup) holds the client events C09a's
  ``history.py`` reads -- ``user.message``, ``user.unsent``, ``assistant.message``, ``tool.call``, ``tool.result``,
  ``tool.closed``, ``checkpoint`` -- plus what C24 and C08 write for it (``approval``, the ``checkpoints`` rows,
  ``rewind``) and one ``run.start {run_id, mode, history_items}`` marker per run (ids and counts only);
* each RUN has its own session (its id is the ``run_id``, as C04 requires); C04 stores the server's events there;
* the next message after an interrupt, a rewind, a pause or the end of a run starts a NEW run whose ``history`` is
  ``rebuild_history`` of the conversation session (it honors ``rewind`` markers and the size bounds);
* a NEW ``ToolRunner`` is made for each run (it is bound to one ``run_id``); it is given ``session_id`` = the
  conversation session so checkpoint step numbers keep rising across runs and one rewind list covers the conversation;
* the per-session approvals (``a``, ``d``) live in ``SessionMemory`` here, because the runner's own grants die with it.

What the model is told is only what the device log holds: a reply that was cut off by an interrupt is not in it (the
reply is logged at ``text_done``), and a tool call without a result and without a closing status is dropped by the
rebuild.  The transcript is written to the C02 store and nowhere else; logs carry only types, ids, counts and codes.

THE RECORDING LINK
------------------
``ToolRunner`` sends ``approval`` and ``tool_result`` through the object given as its ``link``.  ``_RecordingLink``
wraps the real link and, AFTER a ``tool_result`` was accepted for sending, appends ``tool.result`` to the
conversation log (what the device sent back; the run's own events carry no outputs).  A result that could not be
sent is not logged: the server closes that call as ``disconnected``, the model never saw an output, and the rebuild
says so with its own fixed sentence.  After each result the new ``checkpoints`` rows are noticed and logged as
``checkpoint {step, call_id, tool}``.

MESSAGES, QUEUE AND ``rate_limited``
------------------------------------
``submit`` returns at once; a worker task sends the messages in order (starting a run first when none is open), so
typing while the agent works never blocks the prompt.  A message sent while the agent works is shown as ``queued``
(the server queues up to five per run).  A message beyond the cap gets ``rate_limited`` -- it arrives through
``LinkCallbacks.on_error`` (a ``ServerError``), not as an exception from ``send_user_message`` -- and the person is
told to send it again; the message is marked ``user.unsent`` in the log so it is not part of any later history.

APPROVALS
---------
``_ask`` is the ``ask`` of C24.  One prompt at a time (the runner serializes them); keys arrive through ``on_key``;
dismissal (the runner cancels the ask task) clears the prompt.  ``d`` is realized here: the answer to C24 is a plain
deny and the exact call goes into ``SessionMemory``, so a repeat is denied without a prompt (C24's ``Choice`` has no
session-wide deny).

MODES
-----
``policy_context`` reads ``status.mode`` on every call.  Shift+Tab cycles ask <-> accept-edits (device policy only);
plan can be chosen at startup (``--mode plan``) and is left through ``plan_exit`` (C24's ``on_mode_change``).  The
server learns the mode only at ``run.start``, so a mode changed during a run reaches the server with the next run.

PROJECT SETTINGS
----------------
Project settings are IGNORED in v0 (the trust prompt is not built): the rule layers are loaded with
``project_trusted=False`` (the project file is never parsed) and when ``.m2c/settings.toml`` exists one line in the
status area says it is being ignored.

Place this file at: m2c/src/m2c/tui/session.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from .. import checkpoints
from ..context import assemble_run_context
from ..link import DeviceLink, LinkCallbacks, LinkDeviceRevokedError, LinkError, LinkRateLimitedError
from ..logging_setup import log_event
from ..paths import config_dir
from ..policy import Context, load_layers
from ..repl import cwd_label_for
from ..runner import ApprovalRequest, Choice, ToolRunner
from ..store import append_event, create_session, new_session_id, read_events
from ..tools import ToolContext
from ..tools.paths import PathGuardError, resolve_for_read
from ..tools.shell import ShellConfig, configure_shell
from .approval import (
    ApprovalMachine, Done, EditStart, Expand, Pending, SessionMemory, describe_request,
)
from .complete import Scan, candidates, mention_at, scan_project
from .diff import Existing
from .grant import GrantResult, describe as describe_grant, request_grant
from .history import (
    EV_ASSISTANT, EV_CALL, EV_CHECKPOINT, EV_CLOSED, EV_RESULT, EV_UNSENT, EV_USER, checkpoint_tools, rebuild_history,
)
from .keys import EscapeTracker
from .render import Block, Renderer, error_blocks, line
from .safe import clean, clean_line, safe_name
from .status import StatusState, cycle_mode, observe as observe_status, status_lines, window_from_env

if TYPE_CHECKING:  # the UI protocol lives in app.py, which imports the terminal libraries; never import it here
    from .app import UI

__all__ = ["EXIT_REVOKED", "EXIT_UNREACHABLE", "Session", "StartupError"]

logger = logging.getLogger("m2c.tui.session")

EXIT_REVOKED = 4
EXIT_UNREACHABLE = 5
EV_RUN_START = "run.start"
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "expired"})
PAUSED_STATES = frozenset({"paused_budget", "paused_error"})
MAX_EXISTING_BYTES = 200_000
MAX_MESSAGE_CHARS = 100_000
REWIND_LIST_LIMIT = 15
HELP_TEXT = ("Type a message and press Enter. Esc stops the agent. Esc Esc rewinds. Shift+Tab switches between ask and "
             "accept-edits. @ completes a file name. /rewind, /help and /exit are commands.")


class StartupError(Exception):
    """The session cannot start; ``str(exc)`` is fixed text fit to print."""


@dataclass
class _Approval:
    request: ApprovalRequest
    machine: ApprovalMachine
    future: "asyncio.Future[Done]"


@dataclass
class _Modal:
    """A small question the next typed line answers (the rewind list, the confirmation)."""

    label: str
    handler: Callable[[str], Any]


class _RecordingLink:
    """The ``link`` of a ``ToolRunner``: passes everything through and logs the ``tool.result`` the device sent."""

    def __init__(self, session: "Session", link: DeviceLink) -> None:
        self._session = session
        self._link = link

    async def send_approval(self, run_id: str, call_id: str, *, decision: str, scope: str = "once") -> str:
        return await self._link.send_approval(run_id, call_id, decision=decision, scope=scope)

    async def send_tool_stream(self, run_id: str, call_id: str, *, stream: str, chunk: str) -> str:
        return await self._link.send_tool_stream(run_id, call_id, stream=stream, chunk=chunk)

    async def send_tool_result(self, run_id: str, call_id: str, *, ok: bool, output: str, truncated: bool,
                               trust: str, exit_code: int | None = None, duration_ms: int | None = None) -> str:
        sent = await self._link.send_tool_result(run_id, call_id, ok=ok, output=output, truncated=truncated,
                                                 trust=trust, exit_code=exit_code, duration_ms=duration_ms)
        self._session._record_result(call_id, ok=ok, output=output, truncated=truncated, trust=trust)
        return sent


class Session:
    """The terminal chat's controller.  See the module docstring.

    ui                 the ``UI`` (app.py): ``show``, ``set_status``, ``set_prompt``, ``run``
    project_path       the project folder (the session root)
    mode               the startup mode: ``plan``, ``ask`` or ``accept-edits``
    environ / home     the environment and the m2c home (user settings live in ``<home>/settings.toml``)
    link_factory       ``DeviceLink`` by default; tests pass one that points at the fake server
    link_options       extra ``DeviceLink`` keyword arguments
    executor           ``ToolRunner``'s ``executor`` (tests inject stand-ins); None means C05's ``run_tool``
    grant_fn           ``request_grant`` by default (tests inject a stand-in)
    clock              monotonic seconds (the Esc Esc window)
    cancel_wait_s      how long to wait for the server to confirm a cancel
    """

    def __init__(
        self,
        store: Any,
        ui: "UI",
        *,
        project_path: str,
        mode: str = "ask",
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
        link_factory: Callable[..., Any] = DeviceLink,
        link_options: Mapping[str, Any] | None = None,
        executor: Callable[..., Any] | None = None,
        grant_fn: Callable[..., Any] = request_grant,
        clock: Callable[[], float] | None = None,
        cancel_wait_s: float = 3.0,
    ) -> None:
        if mode not in ("plan", "ask", "accept-edits"):
            raise StartupError("the mode must be plan, ask or accept-edits")
        self._store = store
        self.ui = ui
        self._environ = os.environ if environ is None else environ
        self._home = home
        self._link_factory = link_factory
        self._link_options = dict(link_options or {})
        self._executor = executor
        self._grant_fn = grant_fn
        self._cancel_wait_s = cancel_wait_s
        self._project_path = str(project_path)

        try:
            self._tool_context = ToolContext(project_path)
        except PathGuardError as exc:
            raise StartupError("this folder cannot be used as a project: " + clean_line(str(exc), 200)) from None
        self._root = str(self._tool_context.root)
        configure_shell(self._tool_context, ShellConfig(warn=self._sandbox_warning))

        self.status = StatusState(mode=mode, window_tokens=window_from_env(self._environ))
        self.memory = SessionMemory()
        self.renderer = Renderer(read_existing=self._read_existing)
        self._escape = EscapeTracker(**({"clock": clock} if clock is not None else {}))
        self._config_home = Path(home) if home is not None else config_dir(self._environ)
        self._layers = load_layers(managed=None, user=str(self._config_home / "settings.toml"),
                                   project=None, project_trusted=False)
        project_settings = Path(self._root) / ".m2c" / "settings.toml"
        if project_settings.exists():
            self.status.notes.append("project settings (.m2c/settings.toml) are ignored in this version")
        self._run_context = assemble_run_context(self._root, self._root, self._config_home)

        conv = create_session(store, project_path=self._root, mode=mode, model_pref="auto")
        self.conversation_id = conv.id
        self._link: Any = None
        self._runner: ToolRunner | None = None
        self._run_id: str | None = None
        self._run_ended: asyncio.Event | None = None
        self._cancelling: dict[str, asyncio.Event] = {}
        self._pending_turns = 0
        self._outbox: "asyncio.Queue[str]" = asyncio.Queue()
        self._outstanding = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: "asyncio.Task[None] | None" = None
        self._tasks: set["asyncio.Task[Any]"] = set()
        self._approval: _Approval | None = None
        self._modal: _Modal | None = None
        self._last_checkpoint_step = 0
        self._sandbox_shown = False
        self._dead = False
        self._grant_busy = False
        self._scan: Scan | None = None
        self.exit_requested = asyncio.Event()
        self.exit_status = 0
        self.rewinds = 0

    # ------------------------------------------------------------------ start and stop
    async def start(self, connect_timeout_s: float = 15.0) -> int | None:
        """Open the link.  Returns None when ready, else the process exit status (a line was shown)."""
        callbacks = LinkCallbacks(on_event=self._on_event, on_tool_proposed=self._on_tool_proposed,
                                  on_error=self._on_error, on_state=self._on_link_state)
        self._link = self._link_factory(self._store, callbacks=callbacks, capabilities=("tools",),
                                        **self._link_options)
        self._link.start()
        try:
            await self._link.wait_connected(connect_timeout_s)
        except LinkDeviceRevokedError:
            self.ui.show([line("This device was revoked. Run `m2c login` to sign in again.", "error")])
            await self._link.aclose()
            return EXIT_REVOKED
        except (LinkError, asyncio.TimeoutError):
            self.ui.show([line("Could not reach the server. Check your connection and try again.", "error")])
            await self._link.aclose()
            return EXIT_UNREACHABLE
        self._worker = asyncio.get_running_loop().create_task(self._work())
        self._refresh_status()
        self.ui.set_prompt("> ")
        self.ui.show([line("m2c: type a message, or /help.", "dim")])
        return None

    async def run(self, connect_timeout_s: float = 15.0) -> int:
        """The whole interactive session: connect, let the UI read input until the person leaves, close."""
        failed = await self.start(connect_timeout_s)
        if failed is not None:
            return failed
        try:
            await self.ui.run(self)
        finally:
            await self.close()
        return self.exit_status

    async def close(self) -> None:
        """End the open run (cancel), stop the runner and the link."""
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._run_id is not None and not self._dead:
            await self._cancel_current(say=False)
        if self._runner is not None:
            await self._runner.aclose()
            self._runner = None
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._link is not None:
            await self._link.aclose()
        if self._dead and isinstance(getattr(self._link, "failure", None), LinkDeviceRevokedError):
            self.exit_status = EXIT_REVOKED

    def _spawn(self, coro: Any) -> "asyncio.Task[Any]":
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self, timeout: float | None = None) -> None:
        """Until every queued message has been handed to the link (the tests' join point)."""
        await asyncio.wait_for(self._idle.wait(), timeout)

    # ------------------------------------------------------------------ what the UI calls
    @property
    def approval_active(self) -> bool:
        return self._approval is not None

    @property
    def run_open(self) -> bool:
        return self._run_id is not None

    @property
    def grant_offered(self) -> bool:
        return self.status.paused == "paused_budget" and self._run_id is not None and not self._grant_busy

    def captures_keys(self, buffer_empty: bool) -> bool:
        """True when a typed key is a command, not text: an approval is waiting (and not being edited), or ``g`` is
        on offer and the input line is empty."""
        a = self._approval
        if a is not None:
            return not a.machine.editing
        return self.modal is None and buffer_empty and self.grant_offered

    @property
    def modal(self) -> Optional[_Modal]:
        return self._modal

    def on_key(self, key: str) -> None:
        a = self._approval
        if a is not None and not a.machine.editing:
            self._apply(a, a.machine.feed(key))
        elif key == "g" and self.grant_offered:
            self._spawn(self._grant())

    def submit(self, text: str) -> None:
        """The person pressed Enter on ``text``.  Returns at once."""
        text = text if isinstance(text, str) else ""
        a = self._approval
        if a is not None:
            if a.machine.editing:
                self._apply(a, a.machine.submit_edit(text))
            return
        if self._modal is not None:
            modal, self._modal = self._modal, None
            self.ui.set_prompt("> ")
            self._spawn(self._call_modal(modal, text.strip()))
            return
        stripped = text.strip()
        if not stripped:
            return
        if stripped.startswith("/") and "\n" not in stripped:
            self._command(stripped)
            return
        if len(stripped) > MAX_MESSAGE_CHARS:
            self.ui.show([line(f"That message is too long ({len(stripped)} characters; the limit is "
                               f"{MAX_MESSAGE_CHARS}). Shorten it or point to a file with @.", "error")])
            return
        self._outstanding += 1
        self._idle.clear()
        self._outbox.put_nowait(stripped)

    def on_escape(self) -> None:
        """Esc: stop the agent; a second Esc within the window opens the rewind list."""
        a = self._approval
        if a is not None and a.machine.editing:
            self._apply(a, a.machine.feed("escape"))
            self.ui.set_prompt("> ")
            return
        if self._modal is not None:
            self._modal = None
            self.ui.set_prompt("> ")
            self.ui.show([line("Cancelled.", "dim")])
            return
        if self._escape.press() == "rewind":
            self._spawn(self._rewind_list())
        else:
            self._spawn(self._cancel_current(say=True))

    def on_interrupt(self) -> None:
        """Ctrl-C: stop the open run (or the prompt in front of it); with nothing running, leave."""
        a = self._approval
        if a is not None and a.machine.editing:
            self.on_escape()
        elif self._run_id is not None:
            self._spawn(self._cancel_current(say=True))
        else:
            self.exit_requested.set()

    def on_cycle_mode(self) -> None:
        """Shift+Tab."""
        mode = self.status.mode
        new = cycle_mode(mode)
        if new == mode:
            self.ui.show([line("Plan mode is left when you approve the plan, not with Shift+Tab.", "dim")])
            return
        self.status.mode = new
        self._refresh_status()

    def on_exit(self) -> None:
        self.exit_requested.set()

    def complete(self, text_before_cursor: str) -> list[str]:
        """File names for ``@`` (never a protected file, never a symlink, bounded)."""
        found = mention_at(text_before_cursor)
        if found is None:
            return []
        if self._scan is None or not self._scan.complete:
            self._scan = scan_project(Path(self._root), self._tool_context.denylist)
        return candidates(self._scan.files, found[1])

    # ------------------------------------------------------------------ commands
    def _command(self, text: str) -> None:
        word = text.split()[0]
        if word == "/exit":
            self.exit_requested.set()
        elif word == "/rewind":
            self._spawn(self._rewind_list())
        elif word == "/help":
            self.ui.show([line(HELP_TEXT, "info")])
        else:
            self.ui.show([line("Unknown command. Try /help.", "dim")])

    # ------------------------------------------------------------------ policy and tools
    def policy_context(self) -> Context:
        """The CURRENT C06 context: the mode is read on every call."""
        return Context(mode=self.status.mode, layers=self._layers, project_root=self._root, project_trusted=False,
                       tainted=False, home=str(Path.home()), cwd=self._root)

    def _sandbox_warning(self, text: str) -> None:
        if not self._sandbox_shown:
            self._sandbox_shown = True
            self.ui.show([line(f"[warning] {clean_line(text, 200)}", "warning")])

    def _read_existing(self, path: str) -> Existing:
        """The current text of ``path`` for a write_file diff, through C05's path guard (a protected file is never
        read here); bounded."""
        try:
            resolved = resolve_for_read(self._tool_context.root, path, self._tool_context.denylist)
            target = resolved.path
            info = os.lstat(target)
        except FileNotFoundError:
            return Existing("missing")
        except (PathGuardError, OSError, ValueError):
            return Existing("unreadable")
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EXISTING_BYTES:
            return Existing("unreadable")
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as handle:
                return Existing("ok", handle.read(MAX_EXISTING_BYTES + 1).decode("utf-8"))
        except (OSError, UnicodeDecodeError):
            return Existing("unreadable")

    def _on_mode_change(self, mode: str) -> None:
        self.status.mode = mode
        self._refresh_status()
        self.ui.show([line(f"[mode] The plan was approved; the mode is now {safe_name(mode, '?')}.", "info")])

    # ------------------------------------------------------------------ the log
    def _log(self, kind: str, data: dict) -> None:
        try:
            append_event(self._store, self.conversation_id, kind, data)
        except Exception as exc:  # noqa: BLE001 - the log is the record, but a full disk must not kill the prompt
            log_event(logger, logging.ERROR, "tui.log_failed", exc_type=type(exc).__name__)
            self.ui.show([line("[could not write to the local log]", "error")])

    def _record_result(self, call_id: str, *, ok: bool, output: str, truncated: bool, trust: str) -> None:
        data: dict[str, Any] = {"call_id": call_id, "ok": ok, "output": output}
        if truncated:
            data["truncated"] = True
        if trust:
            data["trust"] = trust
        self._log(EV_RESULT, data)
        self._sync_checkpoints(call_id)

    def _sync_checkpoints(self, call_id: str | None = None) -> None:
        """Log the ``checkpoints`` rows made since the last look as ``checkpoint`` events (tied to the call that has
        just finished when known; the rebuild places an unattributed one conservatively)."""
        try:
            rows = self._store.execute(
                "SELECT DISTINCT step FROM checkpoints WHERE session_id = ? AND step > ? ORDER BY step",
                (self.conversation_id, self._last_checkpoint_step)).fetchall()
        except Exception:  # noqa: BLE001
            return
        tool = None
        if call_id is not None:
            for event in reversed(read_events(self._store, self.conversation_id)):
                if event.type == EV_CALL and isinstance(event.data, dict) and event.data.get("call_id") == call_id:
                    tool = event.data.get("tool")
                    break
        for (step,) in rows:
            data: dict[str, Any] = {"step": step}
            if call_id is not None:
                data["call_id"] = call_id
            if isinstance(tool, str):
                data["tool"] = tool
            self._log(EV_CHECKPOINT, data)
            self._last_checkpoint_step = step

    # ------------------------------------------------------------------ link callbacks
    def _on_tool_proposed(self, event: Any) -> None:
        try:
            if event.run_id != self._run_id or self._runner is None:
                return
            payload = event.payload
            if isinstance(payload.get("call_id"), str) and isinstance(payload.get("tool"), str):
                if isinstance(payload.get("input"), dict):
                    self._log(EV_CALL, {"call_id": payload["call_id"], "tool": payload["tool"],
                                        "input": payload["input"]})
            self._runner.on_tool_proposed(event)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "tui.callback_failed", exc_type=type(exc).__name__)

    def _on_event(self, event: Any) -> None:
        try:
            self._handle_event(event)
        except Exception as exc:  # noqa: BLE001 - a UI bug must not kill the connection
            log_event(logger, logging.ERROR, "tui.callback_failed", exc_type=type(exc).__name__)
        runner = self._runner
        if runner is not None and event.run_id == self._run_id:
            runner.observe(event)

    def _handle_event(self, event: Any) -> None:
        if event.run_id != self._run_id:
            waiter = self._cancelling.get(event.run_id)
            if waiter is not None and event.subtype == "state_changed" and \
                    event.payload.get("state") in TERMINAL_STATES:
                waiter.set()
            return
        payload = event.payload
        sub = event.subtype
        if sub == "text_done":
            self._log(EV_ASSISTANT, {"text": self.renderer.reply_text})
            if payload.get("stop_reason") != "tool_use":
                self._turn_over()
        elif sub == "tool_finished":
            call_id, status = payload.get("call_id"), payload.get("status")
            if isinstance(call_id, str) and isinstance(status, str):
                self._log(EV_CLOSED, {"call_id": call_id, "status": status})
            self._sync_checkpoints()
        elif sub == "state_changed":
            self._on_run_state(payload.get("state"))
        observe_status(self.status, event)
        blocks = self.renderer.event(event)
        self._show_stream(blocks)
        self._refresh_status()

    def _show_stream(self, blocks: Sequence[Block]) -> None:
        if blocks:
            self.ui.show(blocks)

    def _on_run_state(self, state: Any) -> None:
        if state in TERMINAL_STATES:
            self._pending_turns = 0
            self._end_run()
        elif state in PAUSED_STATES:
            self._pending_turns = 0
        elif state == "running":
            self.status.working = self._pending_turns > 0

    def _turn_over(self) -> None:
        self._pending_turns = max(self._pending_turns - 1, 0)

    def _end_run(self) -> None:
        """The open run reached a terminal state: the next message starts a new one."""
        run_id, self._run_id = self._run_id, None
        if self._run_ended is not None:
            self._run_ended.set()
        if run_id is not None and self._runner is not None:
            runner, self._runner = self._runner, None
            self._spawn(runner.aclose())

    def _on_error(self, error: Any) -> None:
        try:
            if error.run_id is not None and error.run_id != self._run_id:
                return
            code = error.code
            if code == "rate_limited" and self._pending_turns:
                self._pending_turns -= 1
                self._unsend_last()
            elif code == "run_not_found":
                self._pending_turns = 0
                self._end_run()
            self.ui.show(error_blocks(code, error.details))
            self._refresh_status()
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "tui.callback_failed", exc_type=type(exc).__name__)

    def _unsend_last(self) -> None:
        """The server refused the last message (``rate_limited``): it is not part of the conversation."""
        self._log(EV_UNSENT, {})

    def _on_link_state(self, state: str) -> None:
        if state in ("failed", "closed"):
            self._dead = True
            if state == "failed":
                self.ui.show([line("The connection to the server ended for good.", "error")])
            if isinstance(getattr(self._link, "failure", None), LinkDeviceRevokedError):
                self.ui.show([line("This device was revoked. Run `m2c login` to sign in again.", "error")])
                self.exit_status = EXIT_REVOKED
            self.exit_requested.set()
        elif state == "reconnecting":
            self.ui.show([line("[connection] The connection was lost; trying to reconnect.", "dim")])

    def _refresh_status(self) -> None:
        self.status.working = self._pending_turns > 0 and self.status.paused is None
        self.status.queued = max(self._pending_turns - 1, 0)
        self.ui.set_status(status_lines(self.status))

    # ------------------------------------------------------------------ approvals
    async def _ask(self, request: ApprovalRequest) -> Choice:
        """C24's ``ask``: one prompt at a time (the runner serializes them); a dismissed prompt answers nothing."""
        remembered = self.memory.check(request.tool, request.input) if request.tool != "plan_exit" else None
        if remembered == "deny":
            self.ui.show([line(f"[denied] {safe_name(request.tool)}: you chose not to be asked about this exact call "
                               "again.", "dim")])
            return Choice("deny", "once")
        if remembered == "allow":
            return Choice("allow", "session")
        machine = ApprovalMachine(request)
        approval = _Approval(request, machine, asyncio.get_running_loop().create_future())
        self._approval = approval
        self.ui.show(describe_request(request))
        self.ui.set_prompt("allow? [y/a/n/d] > ")
        try:
            done = await approval.future
        except asyncio.CancelledError:
            self.ui.show([line("[the request was withdrawn]", "dim")])
            raise
        finally:
            if self._approval is approval:
                self._approval = None
            self.ui.set_prompt("> ")
        self.memory.record(request.tool, request.input, done)
        if done.remember_deny:
            self.ui.show([line("[denied] You will not be asked about this exact call again in this session.", "dim")])
        return done.choice

    def _apply(self, approval: _Approval, step: Any) -> None:
        if isinstance(step, Pending):
            if step.message:
                self.ui.show([line(step.message, "dim")])
        elif isinstance(step, Expand):
            self.ui.show(self.renderer.expand(approval.request.call_id))
        elif isinstance(step, EditStart):
            self.ui.show([line("Edit the command, then press Enter to allow it, or Esc to go back.", "info")])
            self.ui.set_prompt("edit> ", step.text)
        elif isinstance(step, Done):
            if not approval.future.done():
                approval.future.set_result(step)

    # ------------------------------------------------------------------ sending messages
    async def _work(self) -> None:
        while True:
            text = await self._outbox.get()
            try:
                await self._deliver(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the worker
                log_event(logger, logging.ERROR, "tui.deliver_failed", exc_type=type(exc).__name__)
                self.ui.show([line("The message could not be sent.", "error")])
            finally:
                self._outstanding -= 1
                if self._outstanding <= 0:
                    self._outstanding = 0
                    self._idle.set()

    async def _deliver(self, text: str) -> None:
        if self._dead:
            self.ui.show([line("The connection is closed; the message was not sent.", "error")])
            return
        if self._run_id is not None and self.status.paused is not None:
            # A run parked for budget or an error takes no more messages: end it, start a new one with history.
            self.ui.show([line("[the paused run is over; this message starts a new one]", "dim")])
            await self._cancel_current(say=False)
        queued = self._pending_turns > 0
        try:
            if self._run_id is None:
                await self._start_run()
            self._log(EV_USER, {"text": text})
            self._pending_turns += 1
            self.status.working = True
            self.ui.show([line(("(queued) " if queued else "> ") + clean(text), "queued" if queued else "user")])
            self._refresh_status()
            assert self._run_id is not None
            await self._link.send_user_message(self._run_id, text)
        except LinkRateLimitedError:
            self._message_not_sent(text, "rate_limited")
        except LinkError as exc:
            self._message_not_sent(text, getattr(exc, "code", "internal"))
            if isinstance(exc, LinkDeviceRevokedError):
                self._dead = True
                self.exit_status = EXIT_REVOKED
                self.exit_requested.set()

    def _message_not_sent(self, text: str, code: str) -> None:
        self._log(EV_UNSENT, {})
        self._pending_turns = max(self._pending_turns - 1, 0)
        self.ui.show(error_blocks(code if code else "internal"))
        self.ui.show([line("That message was not sent.", "dim")])
        self._refresh_status()

    async def _start_run(self) -> None:
        events = [(e.type, e.data) for e in read_events(self._store, self.conversation_id)]
        history = rebuild_history(events)
        if history.omitted:
            self.ui.show([line(f"[notice] {history.omitted} older items of the conversation were left out to keep it "
                               "small.", "dim")])
        run_id = new_session_id()
        create_session(self._store, project_path=self._root, mode=self.status.mode, model_pref="auto",
                       session_id=run_id)
        runner = ToolRunner(
            _RecordingLink(self, self._link), run_id=run_id, tool_context=self._tool_context,
            policy_context=self.policy_context, store=self._store, session_id=self.conversation_id,
            ask=self._ask, on_mode_change=self._on_mode_change,
            **({"executor": self._executor} if self._executor is not None else {}))
        self._runner = runner
        self._run_id = run_id
        self._run_ended = asyncio.Event()
        self.status.start_run()
        self.status.paused = None
        self.renderer = Renderer(read_existing=self._read_existing)
        fields = self._run_context.run_start_fields()
        try:
            await self._link.start_run(
                run_id=run_id, mode=self.status.mode, cwd_label=cwd_label_for(self._root), profile="code",
                history=history.items or None, **fields)
        except BaseException:
            self._run_id = None
            self._runner = None
            await runner.aclose()
            raise
        self._log(EV_RUN_START, {"run_id": run_id, "mode": self.status.mode, "history_items": len(history.items)})

    # ------------------------------------------------------------------ interrupt
    async def _cancel_current(self, *, say: bool) -> None:
        """Stop the open run: kill running calls, dismiss a prompt, send cancel.  The conversation stays in the log."""
        run_id, runner, link = self._run_id, self._runner, self._link
        if run_id is None:
            return
        self._run_id = None
        self._runner = None
        self._pending_turns = 0
        waiter = asyncio.Event()
        self._cancelling[run_id] = waiter
        if say:
            self.ui.show([line("[stopped] The run is over; your next message starts a new one with the conversation so "
                               "far.", "dim")])
        try:
            if runner is not None:
                runner.interrupt()
            try:
                await link.send_cancel(run_id)
                if not self._dead:
                    try:
                        await asyncio.wait_for(waiter.wait(), self._cancel_wait_s)
                    except asyncio.TimeoutError:
                        pass
            except LinkError:
                pass
        finally:
            self._cancelling.pop(run_id, None)
            if runner is not None:
                await runner.aclose()
            self._sync_checkpoints()
            self.status.paused = None
            self._refresh_status()

    # ------------------------------------------------------------------ rewind
    def _rewind_steps(self) -> list[tuple[int, str, str, int, list[str]]]:
        """``(step, time, tool, file count, first paths)`` newest first, from the local store: never contents."""
        self._sync_checkpoints()
        tools = checkpoint_tools((e.type, e.data) for e in read_events(self._store, self.conversation_id))
        rows = self._store.execute(
            "SELECT step, ts, path FROM checkpoints WHERE session_id = ? ORDER BY step DESC, path",
            (self.conversation_id,)).fetchall()
        steps: dict[int, tuple[str, list[str]]] = {}
        for step, ts, path in rows:
            entry = steps.setdefault(step, (ts, []))
            entry[1].append(path)
        return [(step, ts, tools.get(step, "?"), len(paths), paths[:2]) for step, (ts, paths) in steps.items()]

    async def _rewind_list(self) -> None:
        if self._approval is not None:
            return
        steps = self._rewind_steps()[:REWIND_LIST_LIMIT]
        if not steps:
            self.ui.show([line("There is nothing to rewind: no file has been changed in this conversation.", "dim")])
            return
        rows = [line("Rewind to just before one of these changes (newest first):", "info")]
        for number, (step, ts, tool, count, paths) in enumerate(steps, 1):
            shown = ", ".join(clean_line(p, 60) for p in paths) + (f" +{count - len(paths)} more" if count > len(paths) else "")
            rows.append(line(f"  {number}. {clean_line(ts, 30)}  {safe_name(tool, '?')}  {shown}", "plain"))
        rows.append(line("Type a number and press Enter, or press Esc to cancel.", "dim"))
        self.ui.show(rows)

        def choose(answer: str) -> Any:
            return self._rewind_choose(steps, answer)

        self._modal = _Modal("rewind #> ", choose)
        self.ui.set_prompt("rewind #> ")

    async def _call_modal(self, modal: _Modal, answer: str) -> None:
        try:
            result = modal.handler(answer)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "tui.modal_failed", exc_type=type(exc).__name__)
            self.ui.show([line("That did not work.", "error")])

    def _rewind_choose(self, steps: list, answer: str) -> None:
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if not 1 <= index <= len(steps):
            self.ui.show([line("Nothing was rewound.", "dim")])
            return
        step, ts, tool, count, paths = steps[index - 1]
        self.ui.show([line(f"Rewind to just before step {step} ({safe_name(tool, '?')}, {count} file(s))? "
                           "Every change from that step on is undone. [y/N]", "warning")])
        self._modal = _Modal("confirm [y/N]> ", lambda a: self._rewind_confirm(step, a))
        self.ui.set_prompt("confirm [y/N]> ")

    async def _rewind_confirm(self, step: int, answer: str) -> None:
        if answer.lower() not in ("y", "yes"):
            self.ui.show([line("Nothing was rewound.", "dim")])
            return
        await self.rewind_to(step)

    async def rewind_to(self, step: int) -> checkpoints.RewindResult | None:
        """End the run, restore the files, and say what happened.  The next message starts a new run whose history
        is the conversation up to the rewind marker."""
        await self._cancel_current(say=False)
        try:
            # On this thread: the store connection belongs to it (a restore reads and writes project files; the
            # snapshot sizes are bounded by C08, so this is short).
            result = checkpoints.rewind(self.conversation_id, step, self._root, True, conn=self._store)
        except checkpoints.RewindIncompleteError as exc:
            self.ui.show([line("[rewind] Some files could not be restored; nothing was undone for good. Run the rewind "
                               "again.", "error")])
            self._show_rewind_result(exc.result)
            return None
        except checkpoints.CheckpointError as exc:
            self.ui.show([line(f"[rewind] The rewind could not be done ({type(exc).__name__}).", "error")])
            return None
        self.rewinds += 1
        self._last_checkpoint_step = min(self._last_checkpoint_step, max(step - 1, 0))
        self.ui.show([line(f"[rewind] The project was restored to just before step {step}.", "info")])
        self._show_rewind_result(result)
        self.ui.show([line(checkpoints.REWIND_NOTICE, "warning")])
        return result

    def _show_rewind_result(self, result: checkpoints.RewindResult) -> None:
        self.ui.show([line(f"  restored {len(result.restored)} file(s), removed {len(result.deleted)}, "
                           f"already right {len(result.unchanged)}.", "plain")])
        for item in (*result.skipped, *result.failed):
            self.ui.show([line(f"  not restored: {clean_line(str(item), 200)}", "warning")])

    # ------------------------------------------------------------------ grants
    async def _grant(self) -> None:
        run_id = self._run_id
        if run_id is None or self._grant_busy:
            return
        self._grant_busy = True
        self.ui.show([line("Asking the server for more credits...", "dim")])
        try:
            result: GrantResult = await self._grant_fn(run_id)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "tui.grant_failed", exc_type=type(exc).__name__)
            result = GrantResult("error")
        finally:
            self._grant_busy = False
        self.ui.show([line(describe_grant(result), "info" if result.ok else "warning")])
        if result.kind == "run_gone":
            self._end_run()
        self._refresh_status()
