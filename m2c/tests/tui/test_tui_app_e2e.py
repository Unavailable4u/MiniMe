"""Patch C09b: the whole terminal chat end to end -- prompt_toolkit's scripted input and a string-backed output, the
real session, runner, tools and policy, and C04's fake server.  What is asserted is what reaches the terminal."""
import asyncio
import io
import re

import pytest

pytest.importorskip("prompt_toolkit")
pytest.importorskip("rich")

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from tui_rig import Chat, Rig, call_id, run_async

from m2c.tui.app import MAX_LIVE_CHARS, PromptToolkitUI

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]")
ESC = "\x1b"


class Terminal:
    """A scripted terminal: ``await term.type("text\\r")`` and ``term.plain`` (the output without prompt_toolkit's own
    cursor and colour codes).  ``term.raw`` is every byte written."""

    def __init__(self, tmp_path, chat, **rig_options):
        self.out = io.StringIO()
        self.rig = Rig(tmp_path, chat, **rig_options)
        self.task = None

    @property
    def raw(self) -> str:
        return self.out.getvalue()

    @property
    def plain(self) -> str:
        return ANSI_RE.sub("", self.raw).replace("\r\n", "\n")

    async def __aenter__(self):
        self.pipe_cm = create_pipe_input()
        self.pipe = self.pipe_cm.__enter__()
        output = Vt100_Output(self.out, lambda: Size(24, 100), term="xterm")
        self.ui = PromptToolkitUI(input=self.pipe, output=output, color=False)
        self.rig.ui = self.ui
        await self.rig.__aenter__()
        self.task = asyncio.get_running_loop().create_task(self.ui.run(self.rig.session))
        await asyncio.sleep(0.2)
        return self

    async def __aexit__(self, *exc):
        self.rig.session.on_exit()
        if self.task is not None:
            await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)
        try:
            await self.rig.__aexit__(*exc)
        finally:
            self.pipe_cm.__exit__(None, None, None)

    async def type(self, text: str, settle: float = 0.05) -> None:
        self.pipe.send_text(text)
        await asyncio.sleep(settle)

    async def wait_for(self, needle: str, timeout: float = 8.0) -> None:
        await self.rig.until(lambda: needle in self.plain, f"{needle!r} on the terminal\n{self.plain[-1500:]}", timeout)

    async def wait_prompt(self) -> None:
        await self.rig.until(lambda: self.rig.session.approval_active, "an approval prompt")


def test_a_whole_conversation_streamed_reply_read_edit_diff_denied_call(tmp_path):
    read_id, edit_id, bash_id = call_id(), call_id(), call_id()
    old = "".join(f"line {i}\n" for i in range(40))
    new = old.replace("line 3\n", "line three\n").replace("line 30\n", "line thirty\n")

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.say(run, "Let me look first.\n", stop="tool_use")
            await chat.propose(run, read_id, "read_file", {"path": "notes.txt"}, "R0")
        elif msg["t"] == "tool_result":
            cid = msg["payload"]["call_id"]
            if cid == read_id:
                await chat.finish(run, read_id, "read_file")
                await chat.propose(run, edit_id, "edit_file", {"path": "notes.txt", "old_string": "line 3\n",
                                                                "new_string": "line three\n"}, "R2")
            elif cid == edit_id:
                await chat.finish(run, edit_id, "edit_file")
                await chat.propose(run, bash_id, "bash", {"command": "rm -rf build"}, "R3")
        elif msg["t"] == "approval" and msg["payload"]["call_id"] == bash_id:
            await chat.finish(run, bash_id, "bash", "denied")
            await chat.say(run, "All done.")

    async def scenario():
        async with Terminal(tmp_path, Chat(handler), files={"notes.txt": old}) as term:
            await term.type("please fix notes\r")
            await term.wait_for("● read_file  notes.txt")
            await term.wait_prompt()
            await term.wait_for("Allow edit_file?")
            assert "path: notes.txt" in term.plain and "+1 -1" in term.plain
            assert "- line 3" in term.plain and "+ line three" in term.plain
            assert "allow? [y/a/n/d]" in term.plain
            await term.type("x")                              # an unknown key answers nothing
            await term.wait_for("Press y, a, n, d, or v.")
            assert term.rig.session.approval_active
            await term.type("y")
            await term.wait_for("✓ edit_file ok")
            assert (term.rig.project / "notes.txt").read_text() == new.replace("line thirty\n", "line 30\n")
            await term.wait_prompt()
            await term.wait_for("Allow bash?")
            assert "$ rm -rf build" in term.plain
            await term.type("n")
            await term.wait_for("All done.")
            assert "✗ bash denied" in term.plain
            assert "Let me look first." in term.plain
            assert "mode: ask" in term.ui._status[0] and "credits:" in term.ui._status[0]
            assert "mode: ask" in term.plain                  # the status area is printed above the input line
            decisions = [(a["call_id"], a["decision"]) for a in term.rig.chat.approvals]
            assert decisions == [(read_id, "allow"), (edit_id, "allow"), (bash_id, "deny")]
    run_async(scenario())


def test_a_large_diff_is_collapsed_and_v_expands_it(tmp_path):
    cid = call_id()
    content = "".join(f"row {i}\n" for i in range(60))

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "write_file", {"path": "big.txt", "content": content}, "R2")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "write_file")
            await chat.say(run, "written")

    async def scenario():
        async with Terminal(tmp_path, Chat(handler)) as term:
            await term.type("write\r")
            await term.wait_prompt()
            await term.wait_for("more lines (press v or Tab to expand)")
            assert "+ row 10" in term.plain and "+ row 59" not in term.plain
            await term.type("v")
            await term.wait_for("+ row 59")
            assert term.rig.session.approval_active           # v shows the diff and keeps asking
            await term.type("y")
            await term.wait_for("written")
    run_async(scenario())


def test_hostile_text_never_reaches_the_terminal_as_an_escape_or_markup(tmp_path):
    esc = chr(27)
    hostile = f"hi {esc}]0;pwned-title\x07 {esc}[2J {esc}]8;;http://evil.invalid\x07link{esc}]8;;\x07 [bold red]x[/bold red] \x0d overwrite"

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            await chat.say(msg["run_id"], hostile + "\nend")

    async def scenario():
        async with Terminal(tmp_path, Chat(handler)) as term:
            await term.type("go\r")
            await term.wait_for("end")
            assert "pwned-title" not in term.raw and "evil.invalid" not in term.raw
            assert "\x1b]0;" not in term.raw and "\x1b]8;" not in term.raw and "\x1b[2J" not in term.raw
            assert "[bold red]x[/bold red]" in term.plain      # shown literally, not interpreted
            assert "\r overwrite" not in term.raw
    run_async(scenario())


def test_the_reply_restarted_notice_is_one_short_line(tmp_path):
    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            await chat.emit(run, type="state_changed", state="running")
            await chat.emit(run, type="text_delta", text="a half written sentence")
            await chat.emit(run, type="notice", code="reply_restarted", severity="info", message="restart")
            await chat.emit(run, type="text_delta", text="The real answer.\n")
            await chat.emit(run, type="text_done", stop_reason="end_turn")

    async def scenario():
        async with Terminal(tmp_path, Chat(handler)) as term:
            await term.type("go\r")
            await term.wait_for("The real answer.")
            assert term.plain.count("the reply is being written again") == 1
            assert "a half written sentence" in term.plain.split("The real answer.")[0]
    run_async(scenario())


def test_a_partial_line_is_live_not_scrolled_and_a_long_line_is_cut_into_pieces(tmp_path):
    long_text = ("word " * 200).strip()

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            await chat.emit(run, type="state_changed", state="running")
            await chat.emit(run, type="text_delta", text="unfinished sentence")
            if msg["payload"]["text"] == "long":
                await chat.emit(run, type="text_delta", text=" " + long_text)

    async def scenario():
        async with Terminal(tmp_path, Chat(handler)) as term:
            await term.type("short\r")
            await term.wait_for("unfinished sentence")
            assert term.ui._partial == "unfinished sentence"
            assert term.ui._queue == [] or "unfinished sentence" not in "".join(term.ui._queue)
            await term.type("long\r")
            await term.rig.until(lambda: len(term.ui._partial) <= MAX_LIVE_CHARS and term.rig.chat.user_messages[-1][1] == "long"
                                 and "word word" in term.plain, "the long line in pieces")
    run_async(scenario())


def test_esc_interrupts_and_esc_esc_opens_the_rewind_list(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            if msg["payload"]["text"] == "write":
                await chat.propose(run, cid, "write_file", {"path": "w.txt", "content": "w\n"}, "R2")
            else:
                await chat.emit(run, type="text_delta", text="thinking")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "write_file")
            await chat.say(run, "ok")

    async def scenario():
        async with Terminal(tmp_path, Chat(handler)) as term:
            await term.type("write\r")
            await term.wait_prompt()
            await term.type("y")
            await term.wait_for("ok")
            await term.rig.until(lambda: "working" not in term.rig.ui.status[0] if False else True)
            await term.type("more\r")
            await term.wait_for("thinking")
            await term.type(ESC)
            await term.wait_for("[stopped]")
            assert len(term.rig.chat.cancels) == 1
            await asyncio.sleep(0.8)                          # past the Esc Esc window
            await term.type(ESC)
            await asyncio.sleep(0.15)
            await term.type(ESC)
            await term.wait_for("Rewind to just before one of these changes")
            assert "w.txt" in term.plain.split("Rewind to just before")[1]
            await term.type(ESC)
            await term.wait_for("Cancelled.")
    run_async(scenario())


def test_shift_tab_cycles_the_mode_in_the_status_area(tmp_path):
    async def scenario():
        async with Terminal(tmp_path, Chat()) as term:
            await term.type("\x1b[Z")
            await term.rig.until(lambda: "mode: accept-edits" in term.ui._status[0], "the new mode in the status")
            await term.type("\x1b[Z")
            await term.rig.until(lambda: "mode: ask" in term.ui._status[0], "the old mode again")
    run_async(scenario())


def test_at_completes_project_files_and_never_offers_a_protected_one(tmp_path):
    async def scenario():
        files = {"src/app.py": "x", ".env": "SECRET=1"}
        async with Terminal(tmp_path, Chat(), files=files) as term:
            await term.type("see @sr")
            await term.wait_for("src/app.py")
            await term.type("\t\r")
            await term.rig.until(lambda: term.rig.chat.user_messages, "the message")
            assert term.rig.chat.user_messages[0][1] == "see @src/app.py"
            await term.type("@.en")
            await asyncio.sleep(0.3)
            assert ".env" not in term.plain.split("see @src/app.py")[-1]
    run_async(scenario())


def test_g_on_an_empty_line_asks_for_more_credits_when_paused_for_budget(tmp_path):
    from m2c.tui.grant import GrantResult
    calls = []

    async def grant(run_id):
        calls.append(run_id)
        return GrantResult("pool_exhausted", reset_at="2026-12-01T00:00:00Z")

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            await chat.emit(msg["run_id"], type="state_changed", state="running")
            await chat.emit(msg["run_id"], type="state_changed", state="paused_budget")

    async def scenario():
        async with Terminal(tmp_path, Chat(handler), grant_fn=grant) as term:
            await term.type("g")                               # nothing is paused: this is just text
            assert calls == [] and term.rig.session.status.paused is None
            await term.type("\x7f")
            await term.type("go\r")
            await term.wait_for("Press g to ask for more credits")
            await term.type("g")
            await term.wait_for("Your credit pool is used up")
            assert "It resets 2026-12-01T00:00:00Z." in term.plain
            assert len(calls) == 1
    run_async(scenario())


def test_ctrl_d_and_ctrl_c_with_nothing_running_leave(tmp_path):
    async def scenario():
        async with Terminal(tmp_path, Chat()) as term:
            await term.type("\x04")
            await asyncio.wait_for(term.task, 5)
            assert term.rig.session.exit_requested.is_set()
        async with Terminal(tmp_path / "second", Chat()) as term:
            await term.type("\x03")
            await asyncio.wait_for(term.task, 5)
    (tmp_path / "second").mkdir()
    run_async(scenario())
