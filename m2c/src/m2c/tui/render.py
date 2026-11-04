"""
m2c/src/m2c/tui/render.py -- Patch C09a: turning a server event into display blocks.

This is the "what is shown" half of the terminal chat; app.py (a later patch) is the thin prompt_toolkit/rich layer
that prints the blocks.  Nothing here touches a terminal, a file or a clock, so every event type can be tested with
plain strings.

BLOCKS
------
``Block.kind`` is ``text`` (append to the current line: a streamed piece of the reply, no newline of its own),
``line`` (one whole line with a ``style``) or ``diff`` (the lines of a diff, collapsed or not).  Styles are names
(``plain dim info warning error user queued assistant tool ok bad``); the UI maps them to colors.

EVERY STRING IS CLEANED HERE
----------------------------
Text from the server, the model or a file goes through ``safe.clean`` before it is put in a block, so a UI that
prints blocks cannot show an escape sequence by forgetting to.  Tool and code names are restricted to a safe
alphabet.  Server error messages are never shown (fixed text per code, plus the code), because a message is free
text.

THE REPLY AND ``reply_restarted``
---------------------------------
``text_delta`` pieces are returned as they arrive.  ``reply_restarted`` (a notice from the server: the reply is
being written again after a dropped connection) closes the current line, forgets the partial reply and returns one
short line.  Terminal scrollback cannot be un-printed, so the discarded text stays above that line; what is
discarded is the renderer's own record of it (``reply_text`` is empty afterwards) so nothing partial reaches the
conversation history.

TOOL CARDS AND DIFFS
--------------------
``tool_proposed`` gives one card line (tool and a short summary of the real input) and, for ``edit_file`` and
``write_file``, a COLLAPSED diff.  ``tool_finished`` gives a status line.  ``expand`` returns the whole diff of a
call (or of the most recent collapsed one).  The renderer remembers a bounded number of diffs and calls.

Standard library only; pure.

Place this file at: m2c/src/m2c/tui/render.py
"""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from .diff import HEAD_LINES, Diff, DiffLine, ReadExisting, collapse, diff_for_call, expand as expand_diff
from .safe import clean, clean_line, safe_name, truncate

__all__ = ["Block", "Renderer", "STYLES", "error_blocks", "line", "summarize_call"]

STYLES = ("plain", "dim", "info", "warning", "error", "user", "queued", "assistant", "tool", "ok", "bad")
_MEMORY = 200
_FINISHED = {
    "ok": ("ok", "ok"), "error": ("bad", "failed"), "denied": ("bad", "denied"), "timeout": ("bad", "timed out"),
    "disconnected": ("bad", "cut off by a lost connection"), "cancelled": ("dim", "cancelled"),
}
_ERRORS = {
    "unauthorized": "The server did not accept this device's login. Run `m2c login` again.",
    "device_revoked": "This device was removed from the account. Run `m2c login` to link it again.",
    "quota_exhausted": "Your credit pool is used up.",
    "run_not_found": "The server no longer has this run. Your next message starts a new one.",
    "protocol_unsupported": "This version of m2c and the server do not speak the same protocol version.",
    "rate_limited": "Too many messages are waiting.",
    "provider_unavailable": "The model service is not available right now.",
    "tool_timeout": "A tool call took too long and was stopped.",
    "invalid_message": "The server rejected a message from this device.",
    "internal": "The server reported an internal error.",
}


@dataclass(frozen=True)
class Block:
    kind: str
    text: str = ""
    style: str = "plain"
    lines: tuple[DiffLine, ...] = ()
    hidden: int = 0
    ref: str = ""


def line(text: object, style: str = "plain") -> Block:
    return Block("line", clean(text), style if style in STYLES else "plain")


def summarize_call(tool: str, args: Any) -> str:
    """A short, clean, one-line summary of the REAL input of a call (never the whole input)."""
    a = args if isinstance(args, dict) else {}
    def s(key: str, n: int = 100) -> str:
        v = a.get(key)
        return clean_line(v, n) if isinstance(v, str) else ""
    if tool == "bash":
        command = a.get("command") if isinstance(a.get("command"), str) else ""
        first = clean_line(command.strip().split("\n", 1)[0], 100)
        return first + (" …" if "\n" in command.strip() else "")
    if tool == "read_file":
        text = s("path")
        if isinstance(a.get("start_line"), int) and isinstance(a.get("end_line"), int):
            text += f":{a['start_line']}-{a['end_line']}"
        return text
    if tool == "grep":
        return f"{s('pattern', 60)} in {s('path', 60) or '.'}"
    if tool == "glob":
        return f"{s('pattern', 60)} in {s('path', 60) or '.'}"
    if tool in ("list_dir", "edit_file", "write_file", "delete_path"):
        return s("path")
    if tool == "plan_exit":
        return "plan ready for review"
    return ""


class Renderer:
    def __init__(self, *, read_existing: Optional[ReadExisting] = None, head_lines: int = HEAD_LINES) -> None:
        self._read_existing = read_existing
        self._head = head_lines
        self._open = False
        self._reply: list[str] = []
        self._calls: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._diffs: "OrderedDict[str, Diff]" = OrderedDict()
        self._collapsed: list[str] = []

    # -- the reply ---------------------------------------------------------------------------------------------
    @property
    def reply_text(self) -> str:
        """The cleaned text of the reply shown so far (empty after text_done or reply_restarted)."""
        return "".join(self._reply)

    def _close_line(self) -> list[Block]:
        if self._open:
            self._open = False
            return [Block("line", "", "plain")]
        return []

    # -- events ------------------------------------------------------------------------------------------------
    def event(self, event: Any) -> list[Block]:
        kind = getattr(event, "type", None)
        payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
        if kind == "tool_proposed":
            return self._proposed(payload)
        if kind == "budget_warning":
            return self._budget(payload)
        if kind != "run_event":
            return []
        sub = payload.get("type")
        if sub == "text_delta":
            text = clean(payload.get("text"))
            if not text:
                return []
            self._reply.append(text)
            self._open = not text.endswith("\n")
            return [Block("text", text, "assistant")]
        if sub == "text_done":
            blocks = self._close_line()
            self._reply = []
            return blocks
        if sub == "notice":
            return self._notice(payload)
        if sub == "tool_finished":
            return self._finished(payload)
        if sub == "state_changed":
            return self._state(payload)
        return []  # tool_started, usage: nothing to print (the status line carries usage)

    def _notice(self, payload: dict) -> list[Block]:
        if payload.get("code") == "reply_restarted":
            blocks = self._close_line()
            self._reply = []
            blocks.append(line("[the connection dropped: the reply is being written again; the text above is discarded]",
                               "dim"))
            return blocks
        severity = "warning" if payload.get("severity") == "warning" else "info"
        return self._close_line() + [line(f"[{severity}] {clean_line(payload.get('message'), 300)}", severity)]

    def _proposed(self, payload: dict) -> list[Block]:
        call_id = clean_line(payload.get("call_id"), 64)
        tool = safe_name(payload.get("tool"))
        args = payload.get("input")
        summary = summarize_call(tool, args)
        self._remember(self._calls, call_id, (tool, summary))
        blocks = self._close_line()
        risk = safe_name(payload.get("risk"), "")
        card = f"● {tool}" + (f"  {summary}" if summary else "") + (f"   [{risk}]" if risk else "")
        blocks.append(line(card, "tool"))
        diff = diff_for_call(tool, args, self._read_existing)
        if diff is not None:
            self._remember(self._diffs, call_id, diff)
            shown, hidden = collapse(diff, self._head)
            if hidden and call_id not in self._collapsed:
                self._collapsed.append(call_id)
            blocks.append(Block("diff", diff.title, "plain", shown, hidden, call_id))
        return blocks

    def _finished(self, payload: dict) -> list[Block]:
        call_id = clean_line(payload.get("call_id"), 64)
        tool = safe_name(payload.get("tool"))
        style, word = _FINISHED.get(payload.get("status"), ("dim", "ended"))
        ms = payload.get("duration_ms")
        took = ""
        if isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0:
            took = f" ({ms} ms)" if ms < 1000 else f" ({ms / 1000:.1f} s)"
        mark = "✓" if style == "ok" else ("✗" if style == "bad" else "·")
        return self._close_line() + [line(f"  {mark} {tool} {word}{took}", style)]

    def _state(self, payload: dict) -> list[Block]:
        state = payload.get("state")
        if state == "paused_budget":
            return self._close_line() + [line(
                "[paused] The run stopped because its credit limit was reached. Press g to ask for more credits, "
                "or type a message to start a new run.", "warning")]
        if state == "paused_error":
            return self._close_line() + [line(
                "[paused] The run stopped after an error. Type a message to start a new run.", "warning")]
        if state == "paused_disconnected":
            return [line("[connection] The connection was lost; trying to reconnect.", "dim")]
        if state in ("failed", "expired"):
            return self._close_line() + [line(
                f"[run ended] The run {'failed' if state == 'failed' else 'expired'}. "
                "Your next message starts a new run.", "warning")]
        if state == "completed":
            return self._close_line() + [line("[run ended] The run is complete.", "dim")]
        return []  # created, running, awaiting_approval (advisory), cancelled (the app says its own line)

    def _budget(self, payload: dict) -> list[Block]:
        pct = payload.get("spent_pct")
        pct_text = f"{pct}%" if isinstance(pct, int) and not isinstance(pct, bool) else "much"
        if payload.get("scope") == "account":
            reset = clean_line(payload.get("reset_at"), 40)
            text = f"[warning] You have used {pct_text} of your credit pool" + (f" (it resets {reset})." if reset else ".")
        else:
            text = f"[warning] This run has used {pct_text} of its credit limit."
        return self._close_line() + [line(text, "warning")]

    # -- diffs -------------------------------------------------------------------------------------------------
    def expand(self, call_id: Optional[str] = None) -> list[Block]:
        """The whole diff of ``call_id``, or of the most recent collapsed one; a one-line answer when none."""
        target = call_id if call_id is not None else (self._collapsed[-1] if self._collapsed else None)
        diff = self._diffs.get(target) if target is not None else None
        if diff is None:
            return [line("There is no collapsed diff to expand.", "dim")]
        if target in self._collapsed:
            self._collapsed.remove(target)
        return [Block("diff", diff.title, "plain", expand_diff(diff), 0, target)]

    def diff_for(self, call_id: str) -> Optional[Diff]:
        return self._diffs.get(call_id)

    def summary_for(self, call_id: str) -> Optional[tuple[str, str]]:
        return self._calls.get(call_id)

    @staticmethod
    def _remember(store: "OrderedDict", key: str, value: Any) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > _MEMORY:
            store.popitem(last=False)


def error_blocks(code: object, details: Optional[dict] = None) -> list[Block]:
    """One error as blocks: fixed text for the code, with the numbers the server may attach."""
    code_text = safe_name(code, "internal")
    text = _ERRORS.get(code_text, _ERRORS["internal"])
    d = details if isinstance(details, dict) else {}
    if code_text == "rate_limited" and isinstance(d.get("retry_after_s"), int):
        text += f" Wait {d['retry_after_s']} seconds and send it again."
    if code_text == "quota_exhausted" and isinstance(d.get("reset_at"), str):
        text += f" It resets {clean_line(d['reset_at'], 40)}."
    return [line(f"[error: {code_text}] {text}", "error")]
