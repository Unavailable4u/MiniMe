"""
m2c/src/m2c/trust/prompt.py -- Patch C26: the folder-trust prompt as a pure key state machine, what it shows, and the status
note for a folder that is not trusted.

Like tui/approval.py, this is the pure part: what the prompt shows (``describe_prompt``), what each key means
(``TrustPromptMachine``), and the one-line note (``ignored_note``).  The terminal layer (tui/session.py) only forwards keys
and shows blocks.

KEYS
----
``t`` trust this folder and remember it (until its files change) · ``s`` trust it for this session only, remember nothing ·
``n`` do not trust it, and remember that (until its files change) · Esc the same as ``n``.  Any other key does nothing: no
key ever answers by accident (Enter, Space and Tab included), and the machine answers at most once.  Closing m2c with the
question open answers nothing and remembers nothing, so the person is asked again next time.

WHAT IS SHOWN
-------------
The folder, how many files and which (the names come from the project, so every one is cleaned of terminal control
characters and cut), a plain-language warning, and the keys.  The list is cut at MAX_FILES_SHOWN names with a count of the rest.

Standard library plus the display helpers of tui/render.py and tui/safe.py (no terminal library, no I/O).

Place this file at: m2c/src/m2c/trust/prompt.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from ..tui.render import Block, line
from ..tui.safe import clean_line
from .store import TrustState

__all__ = ["Answer", "MAX_FILES_SHOWN", "Pending", "TrustPromptMachine", "describe_prompt", "ignored_note"]

MAX_FILES_SHOWN = 20
SETTINGS_FILE = ".m2c/settings.toml"


@dataclass(frozen=True)
class Pending:
    """Nothing is decided; ``message`` (maybe empty) is a hint to show."""

    message: str = ""


@dataclass(frozen=True)
class Answer:
    """``trust``: use the project's settings now.  ``remember``: write the answer to the trust file."""

    trust: bool
    remember: bool


Step = Union[Pending, Answer]


class TrustPromptMachine:
    """One question, one answer.  ``feed`` takes a key name as the terminal layer sends it ("t", "escape", "enter" ...)."""

    def __init__(self, state: TrustState) -> None:
        self.state = state
        self.answer: Optional[Answer] = None

    @property
    def done(self) -> bool:
        return self.answer is not None

    def feed(self, key: object) -> Step:
        if self.answer is not None:
            return Pending()
        k = key.lower() if isinstance(key, str) and len(key) == 1 else key
        if k == "t":
            self.answer = Answer(trust=True, remember=True)
        elif k == "s":
            self.answer = Answer(trust=True, remember=False)
        elif k == "n" or k == "escape":
            self.answer = Answer(trust=False, remember=True)
        else:
            if isinstance(k, str) and len(k) == 1 and k.isprintable():
                return Pending("Press t, s or n (Esc is the same as n).")
            return Pending()
        return self.answer


def describe_prompt(state: TrustState) -> list[Block]:
    """The prompt, as display lines (short enough not to wrap in an ordinary terminal, except the folder name)."""
    count = len(state.files)
    if state.reason == "changed":
        head = "The project files that can change what m2c may do have changed since you last answered for this folder:"
    else:
        head = "This folder has project files that can change what m2c may do, or what the model is told:"
    out = [
        line(""),
        line("Do you trust this folder?", "warning"),
        line(head),
        line(f"folder: {clean_line(state.root, 300)}", "info"),
        line(f"{count} file{'' if count == 1 else 's'}:", "dim"),
    ]
    for name in state.files[:MAX_FILES_SHOWN]:
        out.append(line("  " + clean_line(name, 200), "dim"))
    if count > MAX_FILES_SHOWN:
        out.append(line(f"  ... and {count - MAX_FILES_SHOWN} more", "dim"))
    out += [
        line("If you trust it:", "warning"),
        line("- project settings can allow commands without asking"),
        line("- hooks run commands on your machine"),
        line("- skills add instructions the model follows"),
        line("Only trust folders you got from someone you trust. If you do not, m2c ignores these files."),
        line("[t] trust this folder and remember it   [s] trust for this session only", "info"),
        line("[n] do not trust (remembered until the files change)   [Esc] same as n", "info"),
    ]
    return out


def ignored_note(state: TrustState) -> Optional[str]:
    """The one status line for a folder whose project files are not used, or None when there is nothing to say."""
    reason = state.reason
    if reason in ("trusted", "nothing_to_trust"):
        return None
    if reason in ("never_asked", "declined", "changed"):
        if SETTINGS_FILE in state.files:
            return f"project settings ({SETTINGS_FILE}) are ignored: this folder is not trusted"
        return "project files in .m2c/, .agents/ and .claude/ are ignored: this folder is not trusted"
    if reason == "symlink":
        return ("project settings are ignored: a symlink in .m2c/, .agents/skills or .claude/skills makes this folder "
                "untrustworthy")
    if reason == "too_large":
        return ("project settings are ignored: .m2c/, .agents/skills and .claude/skills hold too many or too large files "
                "to trust automatically")
    if reason == "unreadable":
        return "project settings are ignored: a project file could not be read, so this folder is not trusted"
    if reason == "store_unusable":
        return "project settings are ignored: the trust file cannot be used (see the message above)"
    return "project settings are ignored: this folder is not trusted"
