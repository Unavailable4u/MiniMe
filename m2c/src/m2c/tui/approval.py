"""
m2c/src/m2c/tui/approval.py -- Patch C09a: the approval prompt as a state machine, and what it remembers.

C24 (runner.py) calls an ``ask`` function with an ``ApprovalRequest`` and waits for a ``Choice``.  This module is the
pure part of the terminal's ``ask``: what the prompt shows (``describe_request``), what each key means
(``ApprovalMachine``), and the per-session memory (``SessionMemory``).  The prompt_toolkit layer only forwards keys
and shows blocks.

KEYS
----
``y`` allow once · ``a`` allow this EXACT call for the rest of the session (``Choice(scope="session")``; for
``plan_exit`` it is only ever once, like C24) · ``n`` deny · ``d`` deny AND do not ask again for this exact call in
this session · ``e`` edit (bash only: the command text goes to an editable buffer and comes back as
``Choice(edited_input=...)``) · ``v`` or Tab show the whole diff · Esc leaves the editor.  Any other key does
nothing: no key ever answers by accident, and a prompt that is dismissed (C24 cancels the ask task) answers nothing.

``d`` is realized in the UI on purpose: the plan lists ``d`` as "deny", but C24's ``Choice`` has no session-wide
deny (a deny with scope session is treated as once).  So the answer sent is a plain deny, ``Done.remember_deny``
tells the app to put the call in ``SessionMemory``, and a later identical call is answered with a deny at once,
without a prompt.  The memory lives in this process only.  ``SessionMemory`` also remembers ``a`` because the app
starts a new ``ToolRunner`` for each run (C24's own session grants die with the runner) and the person's "for this
session" should survive an interrupt.  A deny from the policy engine never reaches ``ask`` at all.

EDITING
-------
The edited input keeps every other field of the call (``timeout_s``, ``reason``) and replaces ``command``; an empty
command is refused (the person can press ``n``); an unchanged one is a plain allow.  C06 decides the edited call
again, so an edit can never get past a deny rule.

Standard library plus runner.py (C24); no terminal.

Place this file at: m2c/src/m2c/tui/approval.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Union

from ..runner import ApprovalRequest, Choice
from .render import Block, line
from .safe import clean, clean_line, safe_name, truncate

__all__ = [
    "ApprovalMachine", "Done", "EditStart", "Expand", "KEY_HELP", "Pending", "SessionMemory", "Step",
    "canonical_key", "describe_request",
]

MAX_COMMAND_CHARS = 8000
MAX_COMMAND_LINES = 200
MAX_REMEMBERED = 1000
KEY_HELP = "[y] allow once  [a] allow this exact call this session  [n] deny  [d] deny, don't ask again"


@dataclass(frozen=True)
class Pending:
    """Nothing is decided; ``message`` (maybe empty) is a hint to show."""
    message: str = ""


@dataclass(frozen=True)
class Expand:
    """Show the whole diff."""


@dataclass(frozen=True)
class EditStart:
    """Open the editor with ``text``."""
    text: str


@dataclass(frozen=True)
class Done:
    choice: Choice
    remember_deny: bool = False
    remember_allow: bool = False


Step = Union[Pending, Expand, EditStart, Done]


def canonical_key(tool: str, args: Any) -> Optional[str]:
    """The same identity C24 uses for "this exact call": the tool and its canonical JSON input."""
    try:
        return tool + "\n" + json.dumps(args, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


class SessionMemory:
    """Exact calls the person allowed or denied for the rest of this session (bounded, in this process only)."""

    def __init__(self) -> None:
        self._allow: dict[str, None] = {}
        self._deny: dict[str, None] = {}

    def check(self, tool: str, args: Any) -> Optional[str]:
        key = canonical_key(tool, args)
        if key is None:
            return None
        if key in self._deny:
            return "deny"
        return "allow" if key in self._allow else None

    def record(self, tool: str, args: Any, done: Done) -> None:
        key = canonical_key(tool, args)
        if key is None:
            return
        if done.remember_deny:
            self._add(self._deny, key)
        elif done.remember_allow:
            self._add(self._allow, key)

    @staticmethod
    def _add(store: dict, key: str) -> None:
        store[key] = None
        while len(store) > MAX_REMEMBERED:
            store.pop(next(iter(store)))


class ApprovalMachine:
    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.editing = False

    @property
    def can_edit(self) -> bool:
        a = self.request.input
        return self.request.tool == "bash" and isinstance(a, dict) and isinstance(a.get("command"), str)

    def feed(self, key: str) -> Step:
        if self.editing:
            return self.cancel_edit() if key == "escape" else Pending()
        k = key.lower() if isinstance(key, str) and len(key) == 1 else key
        if k == "y":
            return Done(Choice("allow", "once"))
        if k == "a":
            if self.request.tool == "plan_exit":
                return Done(Choice("allow", "once"))
            return Done(Choice("allow", "session"), remember_allow=True)
        if k == "n":
            return Done(Choice("deny", "once"))
        if k == "d":
            return Done(Choice("deny", "once"), remember_deny=True)
        if k == "e":
            if not self.can_edit:
                return Pending("Editing is only available for bash commands.")
            self.editing = True
            return EditStart(self.request.input["command"])
        if k in ("v", "tab", "\t", "c-i"):
            return Expand()
        if isinstance(k, str) and len(k) == 1 and k.isprintable():
            return Pending("Press y, a, n, d" + (", e" if self.can_edit else "") + ", or v.")
        return Pending()

    def submit_edit(self, text: str) -> Step:
        if not self.editing:
            return Pending()
        if not isinstance(text, str) or not text.strip():
            return Pending("The command is empty. Edit it, press Esc to go back, or deny.")
        original = self.request.input["command"]
        if text == original:
            self.editing = False
            return Done(Choice("allow", "once"))
        self.editing = False
        return Done(Choice("allow", "once", edited_input={**self.request.input, "command": text}))

    def cancel_edit(self) -> Step:
        self.editing = False
        return Pending("Edit cancelled.")


def describe_request(request: ApprovalRequest) -> list[Block]:
    """What the prompt shows: the tool, its risk and mode, C06's reason, the REAL input, and the keys."""
    tool = safe_name(request.tool)
    a = request.input
    blocks = [
        line(f"Allow {tool}?   risk {safe_name(request.risk, '?')} · mode {safe_name(request.mode, '?')}", "warning"),
        line(f"why you are asked: {clean_line(request.reason, 300)}", "dim"),
    ]
    if tool == "bash" and isinstance(a, dict) and isinstance(a.get("command"), str):
        command = clean(a["command"])
        rows = command.split("\n")
        shown = "\n".join(rows[:MAX_COMMAND_LINES])
        cut = len(shown) > MAX_COMMAND_CHARS or len(rows) > MAX_COMMAND_LINES
        blocks.append(line("command:", "dim"))
        for row in truncate(shown, MAX_COMMAND_CHARS).split("\n"):
            blocks.append(line("  $ " + row, "plain"))
        if cut:
            blocks.append(line("  (the command is longer than this; the rest is not shown: press n unless you "
                               "trust it, or edit it)", "error"))
        if isinstance(a.get("reason"), str):
            blocks.append(line(f"the agent says: {clean_line(a['reason'], 300)}", "dim"))
        if isinstance(a.get("timeout_s"), (int, float)) and not isinstance(a.get("timeout_s"), bool):
            blocks.append(line(f"timeout: {a['timeout_s']} s", "dim"))
    elif tool in ("edit_file", "write_file", "delete_path") and isinstance(a, dict):
        blocks.append(line(f"path: {clean_line(a.get('path'), 300)}", "plain"))
        if tool != "delete_path":
            blocks.append(line("the changes are shown above (press v for the whole diff)", "dim"))
    elif tool == "plan_exit" and isinstance(a, dict):
        blocks.append(line("plan:", "dim"))
        for row in truncate(clean(a.get("plan")), 4000).split("\n"):
            blocks.append(line("  " + row, "plain"))
    else:
        try:
            raw = json.dumps(a, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = ""
        blocks.append(line("input: " + clean_line(raw, 1500), "plain"))
    legend = KEY_HELP
    if tool == "bash":
        legend += "  [e] edit"
    if tool in ("edit_file", "write_file"):
        legend += "  [v] expand diff"
    blocks.append(line(legend, "info"))
    return blocks
