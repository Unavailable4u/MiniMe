"""
m2c/repl.py -- the walking skeleton's prompt loop (patch I01, "hello run").

WHAT THIS IS. The smallest terminal front end that joins C02 (local store), C03 (login) and
C04 (device link) to the server's text-only turn runner: after login, ``m2c`` shows a prompt;
each line you type is one message; the reply is printed as it streams. The real terminal UI is
C09 and replaces this module; nothing here adds a dependency (plain ``input()``, ``print``,
asyncio and threading from the standard library).

THE FLOW
  * The first message of a conversation starts a run: ``DeviceLink.start_run`` (profile
    "code", mode "ask", ``cwd_label`` = the folder's NAME, never a path). C04 creates the
    session in the local store in the same call. Every later message is
    ``DeviceLink.send_user_message``. Before it goes out, the message is appended to the local
    log as the client event ``user.message`` (the device log is the record; C04 skips non-server
    events when it recovers its resume cursor).
  * ``LinkCallbacks.on_event`` prints each ``text_delta`` as it arrives. C04 has already
    appended every server event to the local store before the callback runs.
  * A ``notice`` with code ``reply_restarted`` means the connection dropped in the middle of a
    reply and the server is writing it again from the start: the partial reply is discarded
    (it stays in the log, but it is not shown again, not counted and not stored as the reply)
    and one short line says so. A terminal cannot un-print what it already showed, so the
    discarded text stays on screen above that line.
  * When the reply completes (``text_done``) the COMPLETE reply is appended to the local log as
    the client event ``assistant.message``: the log therefore holds the final reply once, even
    when it holds the discarded partial server deltas as well.
  * The turn is over at the ``usage`` event (the last event of a turn), at an ``error`` that
    answers the message (quota_exhausted, provider_unavailable, ...) or at a ``state_changed``
    into a state that waits for a person. ``paused_disconnected`` is NOT an end: the link
    reconnects and resumes by itself, and the reply arrives after the resume.

PAUSED RUNS. quota_exhausted and provider_unavailable park the run (paused_budget / paused_error)
and a parked run takes no further messages. The REPL therefore prints the fixed error line, ends that
run with cancel and lets the next message start a new run. The new run does not carry the old
conversation's context (history restore comes with a later patch).

LEAVING
  * ``/exit`` (or end of input) sends ``cancel`` for the open run, waits briefly for the
    server to confirm, and returns. P00 has no other way to end a run; cancel also deletes the
    server's buffer.
  * Ctrl-C while a reply is streaming sends ``cancel`` and returns to the prompt. That ends the
    run: the next message you type starts a NEW run (a new session). Ctrl-C at the prompt
    leaves like ``/exit``.
  * A revoked device ends the loop with a clear line (the link has stopped for good).

INPUT. ``input()`` blocks, so it runs in a daemon thread of its own, not in the event loop and
not in asyncio's default executor (whose shutdown would wait for a thread stuck in ``input()``
and hang the exit). The reading function is injectable for tests.

CONTENT HYGIENE. This module logs nothing about what was typed or answered; it prints replies
to the terminal and writes them to the local store, which is on this machine by design.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import threading
from pathlib import Path
from typing import Any, Callable

from m2c.link import DeviceLink, LinkCallbacks, LinkDeviceRevokedError, LinkError
from m2c.store import append_event

PROMPT = "> "
EXIT_UNREACHABLE = 5      # run_prompt's status when the server cannot be reached (cli.EXIT_UNREACHABLE)
EXIT_COMMAND = "/exit"

# Turn ends: the `state_changed` states that wait for a person (not paused_disconnected).
_ENDING_STATES = frozenset({"paused_budget", "paused_error", "completed", "cancelled", "failed", "expired"})
_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "expired"})

RESTART_NOTICE_CODE = "reply_restarted"
CANCEL_WAIT_S = 3.0
ERROR_STATE_GRACE_S = 2.0   # quota_exhausted / provider_unavailable are always followed by a state_changed; wait this long for it
_PAUSING_ERRORS = frozenset({"quota_exhausted", "provider_unavailable"})

# Fixed lines for the errors the walking skeleton can meet (never the server's own text).
_ERROR_LINES = {
    "quota_exhausted": "Your credit pool is used up for now.",
    "provider_unavailable": "No model could answer right now. Try again in a moment.",
    "rate_limited": "Too many requests. Wait a moment and try again.",
    "run_not_found": "The server no longer has this conversation. Your next message starts a new one.",
    "invalid_message": "The server could not take that message right now.",
}
_GENERIC_ERROR = "Something went wrong on the server."


def cwd_label_for(project_path: str) -> str:
    """A label for the working directory: the folder's NAME, never a path. Control characters and
    separators are removed and the length is capped, so it always satisfies P00's cwd_label."""
    name = Path(project_path).name
    name = re.sub(r"[/\\\x00-\x1f]", "", name)[:128]
    return name if name and name not in (".", "..") else "project"


class Repl:
    """The prompt loop. Build it, hand ``repl.callbacks()`` to the DeviceLink, then ``await
    repl.run(link)``. Everything that touches the terminal goes through ``read_line`` and
    ``write``, which tests replace."""

    def __init__(
        self,
        store,
        *,
        project_path: str | None = None,
        read_line: Callable[[str], str] = input,
        write: Callable[[str], Any] | None = None,
        install_sigint: bool = True,
    ) -> None:
        self._store = store
        self._project_path = project_path or os.getcwd()
        self._read_line = read_line
        self._write = write or self._default_write
        self._install_sigint = install_sigint
        self._link: DeviceLink | None = None
        self.run_id: str | None = None
        self._paused = False                             # the open run went to paused_budget / paused_error
        self._cancelling: str | None = None              # a run whose cancel is awaiting its confirmation
        self._parts: list[str] = []
        self._turn_over: asyncio.Event | None = None
        self._ended: asyncio.Event | None = None         # the open run reached a terminal state
        self._interrupt: asyncio.Event | None = None
        self._dead_event: asyncio.Event | None = None    # the link stopped for good (revoked, closed)
        self._dead = False
        self.last_error: str | None = None
        self.replies: list[str] = []                     # every complete reply shown (for tests and /history later)
        self.restarts = 0
        self.sigint_installed = False

    @property
    def idle(self) -> bool:
        """True when no turn is in flight (before the first message, or after the turn's last event)."""
        return self._turn_over is None or self._turn_over.is_set()

    # -- terminal ---------------------------------------------------------
    @staticmethod
    def _default_write(text: str) -> None:
        print(text, end="", flush=True)

    def _line(self, text: str = "") -> None:
        self._write(text + "\n")

    # -- callbacks (called by the link, synchronously, on the event loop) -----
    def callbacks(self) -> LinkCallbacks:
        return LinkCallbacks(on_event=self._on_event, on_error=self._on_error, on_state=self._on_state)

    def _on_event(self, event) -> None:
        kind = event.subtype
        payload = event.payload
        if event.run_id != self.run_id:
            # Only the confirmation of a cancel we sent is of interest for a run we let go of.
            if (event.run_id == self._cancelling and kind == "state_changed"
                    and payload.get("state") in _TERMINAL_STATES and self._ended is not None):
                self._ended.set()
            return
        if kind == "text_delta":
            text = payload.get("text", "")
            self._parts.append(text)
            self._write(text)
        elif kind == "notice":
            if payload.get("code") == RESTART_NOTICE_CODE:
                self._parts.clear()                      # discard the partial reply
                self.restarts += 1
                self._line()
                self._line("[connection dropped: the reply is being written again]")
            else:
                self._line(f"[{payload.get('severity', 'info')}] {payload.get('message', '')}")
        elif kind == "text_done":
            reply = "".join(self._parts)
            self._parts.clear()
            self.replies.append(reply)
            self._line()
            self._store_event("assistant.message", {"text": reply})
        elif kind == "usage":
            self._line(
                f"[{payload.get('model')} | in {payload.get('input_tokens')} "
                f"(cached {payload.get('cached_input_tokens')}) | out {payload.get('output_tokens')} "
                f"| {payload.get('credits')} credits]"
            )
            self._finish_turn()
        elif kind == "state_changed":
            state = payload.get("state")
            if state in _TERMINAL_STATES and self._ended is not None:
                self._ended.set()
            if state in ("paused_budget", "paused_error"):
                self._paused = True
            if state in _ENDING_STATES:
                if state == "paused_budget":
                    self._line("[paused: out of credits]")
                elif state == "paused_error":
                    self._line("[paused: the model could not answer]")
                self._finish_turn()

    def _on_error(self, error) -> None:
        if error.run_id is not None and error.run_id != self.run_id:
            return
        self.last_error = error.code
        line = _ERROR_LINES.get(error.code, _GENERIC_ERROR)
        reset = (error.details or {}).get("reset_at") if error.code == "quota_exhausted" else None
        self._line(f"error: {line}" + (f" It resets at {reset}." if reset else ""))
        if error.code in _PAUSING_ERRORS and self._turn_over is not None:
            # The state_changed that parks the run follows; end the turn on it (or after a grace period).
            asyncio.get_running_loop().call_later(ERROR_STATE_GRACE_S, self._turn_over.set)
        else:
            self._finish_turn()

    def _on_state(self, state: str) -> None:
        if state in ("failed", "closed"):
            self._dead = True
            if self._dead_event is not None:
                self._dead_event.set()
            self._finish_turn()
            if self._ended is not None:
                self._ended.set()
        elif state == "reconnecting":
            self._line("[connection lost: reconnecting]")

    def _finish_turn(self) -> None:
        if self._turn_over is not None:
            self._turn_over.set()

    def _store_event(self, event_type: str, data: dict) -> None:
        if self.run_id is None:
            return
        try:
            append_event(self._store, self.run_id, event_type, data)
        except Exception:                                # the log is a convenience here; never kill the prompt
            self._line("[could not write to the local log]")

    # -- input --------------------------------------------------------------
    async def _ask(self) -> str | None:
        """One line from the person, or None at end of input or Ctrl-C at the prompt."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def reader() -> None:
            try:
                value: Any = self._read_line(PROMPT)
            except EOFError:
                value = None
            except BaseException as exc:                 # noqa: BLE001 - delivered to the loop, not raised in a thread
                loop.call_soon_threadsafe(_deliver, None, exc)
                return
            loop.call_soon_threadsafe(_deliver, value, None)

        def _deliver(value, exc) -> None:
            if future.done():
                return
            if exc is not None:
                future.set_exception(exc)
            else:
                future.set_result(value)

        threading.Thread(target=reader, name="m2c-input", daemon=True).start()
        interrupted = asyncio.ensure_future(self._interrupt.wait())
        dead = asyncio.ensure_future(self._dead_event.wait())
        try:
            await asyncio.wait({future, interrupted, dead}, return_when=asyncio.FIRST_COMPLETED)
            if future.done() and not dead.done():
                return future.result()
            if interrupted.done():
                self._interrupt.clear()
                self._line()
            return None                                  # Ctrl-C at the prompt, or the link stopped for good
        finally:
            interrupted.cancel()
            dead.cancel()

    # -- the loop -----------------------------------------------------------
    async def run(self, link: DeviceLink) -> int:
        """Run the prompt until /exit, end of input, Ctrl-C at the prompt or a revoked device.
        Returns the process exit status (0 for a normal leave, 4 when the device was revoked)."""
        self._link = link
        self._interrupt = asyncio.Event()
        self._dead_event = asyncio.Event()
        if getattr(link, "state", None) in ("failed", "closed"):
            self._dead = True
            self._dead_event.set()
        loop = asyncio.get_running_loop()
        self._hook_sigint(loop)
        try:
            while not self._dead:
                text = await self._ask()
                if text is None:
                    break
                text = text.strip()
                if not text:
                    continue
                if text == EXIT_COMMAND:
                    break
                if text.startswith("/"):
                    self._line(f"Unknown command. Type a message, or {EXIT_COMMAND} to leave.")
                    continue
                await self._message(text)
            await self._leave()
        finally:
            self._unhook_sigint(loop)
        if self._dead and isinstance(getattr(link, "failure", None), LinkDeviceRevokedError):
            self._line("This device was revoked. Run `m2c login` to sign in again.")
            return 4
        return 0

    async def _message(self, text: str) -> None:
        link = self._link
        assert link is not None
        self._turn_over = asyncio.Event()
        self._parts.clear()
        self.last_error = None
        self._paused = False
        try:
            if self.run_id is None:
                self._ended = asyncio.Event()
                self.run_id = await link.start_run(
                    project_path=self._project_path, mode="ask", cwd_label=cwd_label_for(self._project_path),
                    instructions="", profile="code",
                )
            self._store_event("user.message", {"text": text})
            await link.send_user_message(self.run_id, text)
        except LinkError as exc:
            self._line(f"error: {_ERROR_LINES.get(getattr(exc, 'code', ''), _GENERIC_ERROR)}")
            if isinstance(exc, LinkDeviceRevokedError):
                self._dead = True
            return
        await self._await_reply()

    async def _await_reply(self) -> None:
        """Wait for the turn to end; Ctrl-C meanwhile cancels the run."""
        assert self._turn_over is not None and self._interrupt is not None
        self._interrupt.clear()
        over = asyncio.ensure_future(self._turn_over.wait())
        interrupted = asyncio.ensure_future(self._interrupt.wait())
        try:
            await asyncio.wait({over, interrupted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            over.cancel()
            interrupted.cancel()
        if self._interrupt.is_set() and not self._turn_over.is_set():
            self._interrupt.clear()
            self._line()
            self._line("[stopped: the run is over; your next message starts a new one]")
            await self._cancel_run()
            return
        if self._paused and self.run_id is not None and not self._dead:
            # A run parked for budget or a model failure takes no more messages. End it (cancel also
            # deletes its server buffer); the next message starts a new run, without this one's context.
            self._line("[this run is over; your next message starts a new one]")
            await self._cancel_run()
            return
        if self.run_id is not None and self._ended is not None and self._ended.is_set():
            self.run_id = None                           # the run ended by itself: start a new one next time
        elif self.last_error == "run_not_found":
            self.run_id = None

    async def _cancel_run(self) -> None:
        """Send cancel for the open run and wait briefly for the server to confirm it."""
        link, run_id = self._link, self.run_id
        if link is None or run_id is None:
            return
        self.run_id = None
        self._cancelling = run_id
        try:
            await link.send_cancel(run_id)
            if self._ended is not None and not self._dead:
                try:
                    await asyncio.wait_for(self._ended.wait(), CANCEL_WAIT_S)
                except asyncio.TimeoutError:
                    pass
        except LinkError:
            return
        finally:
            self._cancelling = None

    async def _leave(self) -> None:
        if self.run_id is not None and not self._dead:
            await self._cancel_run()

    # -- Ctrl-C ---------------------------------------------------------------
    def interrupt(self) -> None:
        """What Ctrl-C does (public so a test, or C09, can raise it without a signal)."""
        if self._interrupt is not None:
            self._interrupt.set()

    def _hook_sigint(self, loop) -> None:
        if not self._install_sigint:
            return
        try:
            loop.add_signal_handler(signal.SIGINT, self.interrupt)
            self.sigint_installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            self.sigint_installed = False                # not the main thread, or no signal support: Ctrl-C keeps its default

    def _unhook_sigint(self, loop) -> None:
        if self.sigint_installed:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            self.sigint_installed = False


async def run_prompt(store, *, project_path: str | None = None, link_factory=DeviceLink, link_options: dict | None = None,
                     read_line: Callable[[str], str] = input, write: Callable[[str], Any] | None = None,
                     connect_timeout_s: float = 15.0) -> int:
    """Open the link, run the prompt, close the link. The one call ``m2c`` makes."""
    repl = Repl(store, project_path=project_path, read_line=read_line, write=write)
    link = link_factory(store, callbacks=repl.callbacks(), **(link_options or {}))
    link.start()
    try:
        try:
            await link.wait_connected(connect_timeout_s)
        except LinkDeviceRevokedError:
            repl._line("This device was revoked. Run `m2c login` to sign in again.")
            return 4
        except (LinkError, asyncio.TimeoutError):
            repl._line("Could not reach the server. Check your connection and try again.")
            return EXIT_UNREACHABLE
        return await repl.run(link)
    finally:
        await link.aclose()
