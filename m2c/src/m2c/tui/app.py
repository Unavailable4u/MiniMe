"""
m2c/src/m2c/tui/app.py -- Patch C09b: the thin terminal layer (prompt_toolkit and rich) and the ``UI`` protocol.

THE SHAPE
---------
Everything that decides WHAT is shown or what a key MEANS is elsewhere and is tested with plain strings: render.py,
approval.py, diff.py, status.py, keys.py (C09a) and session.py (the controller).  This module only does terminal I/O
and is deliberately small.  ``UI`` is the protocol the controller talks to, so the library can be swapped:

    show(blocks)             print display blocks (streamed text pieces, whole lines, diffs)
    set_status(lines)        the status area under the input line
    set_prompt(label, text)  the label of the input line, and optionally what is in it (the editor of ``e``)
    run(controller)          read input until the controller says exit

PRINTED INLINE, SCROLLBACK-FRIENDLY (decision D2)
-------------------------------------------------
There is no full-screen takeover.  Finished lines are printed above the input line with prompt_toolkit's
``run_in_terminal`` (so the line being typed is erased, the text printed into the scrollback, and the line redrawn);
the status area is printed in the prompt's message, directly above the input line (a bottom toolbar would claim the
rest of the terminal's height and wait for the terminal's cursor-position reply).  A reply that is still being written is shown LIVE in the prompt's
message (above the input line) until its line is complete, then it moves into the scrollback: a partial line is never
printed into the scrollback, because a redraw of the prompt would start in the middle of it.  A very long line is moved
into the scrollback in pieces (``MAX_LIVE_CHARS``).  The input line is always active, so messages can be typed while
the agent works; ``erase_when_done`` removes the typed line (the controller prints it back as ``> text``, or as
``(queued) text``).  The history of typed lines is in memory only: no file is ever written (a transcript goes to the
C02 store and nowhere else).

SAFETY OF WHAT IS PRINTED
-------------------------
Blocks are already clean (render.py cleans at the door).  Here they are cleaned AGAIN, cheaply (a bug elsewhere must
not become a terminal escape), and they are never handed to a markup interpreter: rich gets ``Text`` objects with a
style (markup and highlighting off), the result is a string with only rich's own colour codes, and prompt_toolkit
parses that with ``ANSI``.  The live line is passed as plain formatted text.  ``NO_COLOR`` turns colours off.

KEYS (all forwarded to the controller)
--------------------------------------
Enter submits · Esc interrupts (a second Esc within the window opens the rewind list) · Shift+Tab cycles the mode ·
Ctrl-C stops the run, or leaves when nothing runs · Ctrl-D on an empty line leaves · while an approval waits, every
printable key and Tab go to the approval state machine and never into the line · ``g`` on an empty line answers a
budget pause · typing ``@`` completes project files (the controller decides what is offered).

This module imports prompt_toolkit and rich and is imported only when the terminal chat opens (cli.py imports it
lazily), so headless mode and every other command stay free of them.

Place this file at: m2c/src/m2c/tui/app.py
"""
from __future__ import annotations

import asyncio
import io
import os
from typing import Any, Iterable, Optional, Protocol, Sequence

from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.shortcuts import PromptSession, print_formatted_text
from rich.console import Console
from rich.text import Text

from .complete import mention_at
from .render import Block
from .safe import clean

__all__ = ["MAX_LIVE_CHARS", "PromptToolkitUI", "UI", "run_chat"]

MAX_LIVE_CHARS = 400
COMPLETION_ROWS = 6
ESCAPE_TIMEOUT_S = 0.1
STYLES = {
    "plain": "", "dim": "dim", "info": "cyan", "warning": "yellow", "error": "bold red", "user": "bold",
    "queued": "dim italic", "assistant": "", "tool": "bold blue", "ok": "green", "bad": "red",
}
DIFF_STYLES = {"add": "green", "del": "red", "ctx": "dim", "hunk": "cyan", "note": "italic"}
DIFF_MARKS = {"add": "+ ", "del": "- ", "ctx": "  ", "hunk": "", "note": "  "}


class UI(Protocol):
    """What the session controller needs from a terminal layer."""

    def show(self, blocks: Sequence[Block]) -> None: ...

    def set_status(self, lines: Sequence[str]) -> None: ...

    def set_prompt(self, label: str, text: str = "") -> None: ...

    async def run(self, controller: Any) -> None: ...


class _AtCompleter(Completer):
    def __init__(self, controller: Any) -> None:
        self._controller = controller

    def get_completions(self, document: Any, complete_event: Any) -> Iterable[Completion]:
        before = document.text_before_cursor
        found = mention_at(before)
        if found is None:
            return
        try:
            names = self._controller.complete(before)
        except Exception:  # noqa: BLE001 - completion is a convenience; never break typing
            return
        for name in names:
            yield Completion(name, start_position=-len(found[1]))


class PromptToolkitUI:
    """The prompt_toolkit and rich implementation of ``UI``.  ``input`` and ``output`` default to the real terminal;
    the tests pass a pipe input and a string-backed output."""

    def __init__(self, *, input: Any = None, output: Any = None, color: Optional[bool] = None) -> None:
        self._given_output = output
        self._real_output: Any = None
        self._input = input
        self._color = (not os.environ.get("NO_COLOR")) if color is None else color
        self._status: list[str] = []
        self._label = "> "
        self._partial = ""
        self._queue: list[str] = []
        self._flushing: "asyncio.Task[None] | None" = None
        self._controller: Any = None
        self._session: PromptSession | None = None

    @property
    def _output(self) -> Any:
        """The terminal output, made on first use (so building the UI never touches the terminal)."""
        if self._given_output is not None:
            return self._given_output
        if self._real_output is None:
            self._real_output = create_output()
        return self._real_output

    # -- UI --------------------------------------------------------------------------------------------------------
    def show(self, blocks: Sequence[Block]) -> None:
        out: list[str] = []
        for block in blocks:
            out.extend(self._render(block))
        if out:
            self._queue.extend(out)
        self._kick()

    def set_status(self, lines: Sequence[str]) -> None:
        self._status = [clean(line) for line in lines]
        self._invalidate()

    def set_prompt(self, label: str, text: str = "") -> None:
        previous, self._label = self._label, label
        buffer = self._session.default_buffer if self._session is not None else None
        if buffer is not None and (text or previous.startswith("edit")):
            buffer.text = text
            buffer.cursor_position = len(text)
        self._invalidate()

    async def run(self, controller: Any) -> None:
        self._controller = controller
        session = self._session = self._build_session(controller)
        session.app.ttimeoutlen = ESCAPE_TIMEOUT_S
        watcher = asyncio.get_running_loop().create_task(self._watch_exit(controller))
        try:
            while not controller.exit_requested.is_set():
                try:
                    text = await session.prompt_async()
                except EOFError:
                    controller.on_exit()
                    break
                except KeyboardInterrupt:
                    controller.on_interrupt()
                    continue
                controller.submit(text)
                await asyncio.sleep(0)                      # let the controller react before the prompt returns
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await self._finish()

    # -- the prompt ------------------------------------------------------------------------------------------------
    def _build_session(self, controller: Any) -> PromptSession:
        kb = KeyBindings()
        buffer_empty = lambda: not (self._session is not None and self._session.default_buffer.text)  # noqa: E731
        capturing = Condition(lambda: controller.captures_keys(buffer_empty()))
        approving = Condition(lambda: controller.approval_active)

        @kb.add(Keys.Any, filter=capturing)
        def _(event: Any) -> None:
            controller.on_key(event.data)

        @kb.add("tab", filter=approving)
        def _(event: Any) -> None:
            controller.on_key("tab")

        @kb.add("escape", eager=True)
        def _(event: Any) -> None:
            controller.on_escape()

        @kb.add("s-tab")
        def _(event: Any) -> None:
            controller.on_cycle_mode()

        @kb.add("c-c")
        def _(event: Any) -> None:
            controller.on_interrupt()

        @kb.add("c-d", filter=Condition(lambda: buffer_empty()))
        def _(event: Any) -> None:
            controller.on_exit()
            event.app.exit(exception=EOFError())

        typing_a_mention = Condition(
            lambda: self._session is not None and mention_at(self._session.default_buffer.document.text_before_cursor)
            is not None)
        return PromptSession(
            message=self._message, key_bindings=kb, history=InMemoryHistory(),
            completer=_AtCompleter(controller), complete_while_typing=typing_a_mention, erase_when_done=True,
            input=self._input, output=self._output, mouse_support=False, enable_open_in_editor=False,
            reserve_space_for_menu=COMPLETION_ROWS)

    def _message(self) -> FormattedText:
        parts: list[tuple[str, str]] = []
        if self._partial:
            parts.append(("", self._partial + "\n"))
        if self._status:
            parts.append(("ansibrightblack", "\n".join(self._status) + "\n"))
        parts.append(("bold", self._label))
        return FormattedText(parts)

    def _invalidate(self) -> None:
        session = self._session
        if session is not None and session.app.is_running:
            session.app.invalidate()

    async def _watch_exit(self, controller: Any) -> None:
        await controller.exit_requested.wait()
        session = self._session
        if session is not None and session.app.is_running:
            try:
                session.app.exit(exception=EOFError())
            except Exception:  # noqa: BLE001 - already finishing
                pass

    # -- rendering -------------------------------------------------------------------------------------------------
    def _console(self) -> Console:
        try:
            width = max(self._output.get_size().columns, 20)
        except Exception:  # noqa: BLE001
            width = 80
        return Console(file=io.StringIO(), force_terminal=self._color, color_system="standard" if self._color else None,
                       width=width, markup=False, highlight=False, emoji=False, legacy_windows=False)

    def _text_line(self, text: str, style: str) -> str:
        console = self._console()
        console.print(Text(clean(text), style=STYLES.get(style, "")), end="\n", soft_wrap=False)
        return console.file.getvalue()

    def _render(self, block: Block) -> list[str]:
        if block.kind == "text":
            return self._text_piece(block.text, block.style)
        if block.kind == "line":
            out = self._flush_partial()
            if block.text:                       # an empty line block only closes a half-written reply line
                out.append(self._text_line(block.text, block.style))
            return out
        if block.kind == "diff":
            out = self._flush_partial()
            out.append(self._text_line(f"  {block.text}", "user"))
            out.extend(self._diff_line(item.kind, item.text) for item in block.lines)
            if block.hidden:
                out.append(self._text_line(f"  … {block.hidden} more lines (press v or Tab to expand)", "dim"))
            return out
        return []

    def _diff_line(self, kind: str, text: str) -> str:
        console = self._console()
        console.print(Text(DIFF_MARKS.get(kind, "  ") + clean(text), style=DIFF_STYLES.get(kind, "")), end="\n",
                      soft_wrap=False)
        return console.file.getvalue()

    def _text_piece(self, text: str, style: str) -> list[str]:
        out: list[str] = []
        self._partial += clean(text)
        while "\n" in self._partial:
            first, self._partial = self._partial.split("\n", 1)
            out.append(self._text_line(first, style))
        while len(self._partial) > MAX_LIVE_CHARS:
            cut = self._partial.rfind(" ", 0, MAX_LIVE_CHARS)
            cut = cut if cut > 0 else MAX_LIVE_CHARS
            out.append(self._text_line(self._partial[:cut], style))
            self._partial = self._partial[cut:].lstrip(" ")
        return out

    def _flush_partial(self) -> list[str]:
        if not self._partial:
            return []
        text, self._partial = self._partial, ""
        return [self._text_line(text, "assistant")]

    # -- printing --------------------------------------------------------------------------------------------------
    def _kick(self) -> None:
        self._invalidate()
        if self._queue and (self._flushing is None or self._flushing.done()):
            try:
                self._flushing = asyncio.get_running_loop().create_task(self._flush())
            except RuntimeError:                              # no loop: print now
                self._write(self._take())

    def _take(self) -> list[str]:
        items, self._queue = self._queue, []
        return items

    def _write(self, items: list[str]) -> None:
        if items:
            print_formatted_text(ANSI("".join(items)), output=self._output, end="")
            try:
                self._output.flush()
            except Exception:  # noqa: BLE001
                pass

    async def _flush(self) -> None:
        while self._queue:
            items = self._take()
            session = self._session
            if session is not None and session.app.is_running:
                await run_in_terminal(lambda items=items: self._write(items), in_executor=False)
            else:
                self._write(items)
            await asyncio.sleep(0)

    async def _finish(self) -> None:
        """Print whatever is still waiting (a half-written line included) and let the printing task end."""
        self._queue.extend(self._flush_partial())
        if self._flushing is not None:
            await asyncio.gather(self._flushing, return_exceptions=True)
        self._write(self._take())


async def run_chat(store: Any, *, project_path: str, mode: str = "ask", connect_timeout_s: float = 15.0,
                   ui: Optional[PromptToolkitUI] = None, **session_options: Any) -> int:
    """The one call ``m2c`` makes for an interactive terminal: build the session, run it, return the exit status."""
    from .session import Session

    terminal = ui if ui is not None else PromptToolkitUI()
    session = Session(store, terminal, project_path=project_path, mode=mode, **session_options)
    return await session.run(connect_timeout_s)
