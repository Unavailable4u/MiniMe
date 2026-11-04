"""
m2c/src/m2c/tui/diff.py -- Patch C09a: building a unified diff of a proposed edit and collapsing a large one.

A card for ``edit_file`` or ``write_file`` shows what would change.  This module decides WHAT (pure, no terminal, no
file access of its own): ``diff_for_call`` turns a call's input into a ``Diff``; ``collapse`` keeps the first few
lines of a long one and says how many are hidden; ``expand`` returns every line (still bounded).

* ``edit_file`` is shown as the change from ``old_string`` to ``new_string`` (the call carries no file text).
* ``write_file`` is shown against the file's current text when the caller can supply it (``read_existing``, which
  the app backs with C05's path guard so a protected file is never read here), else as all-new text.
* Work is bounded: inputs above ``MAX_INPUT_CHARS`` are not diffed line by line (difflib is quadratic in the worst
  case); a diff keeps at most ``MAX_DIFF_LINES`` lines and says so.  A huge change can therefore never stall the
  loop that also carries the device link.
* Every line is run through ``safe.clean`` (a file or a model can contain escape sequences).

Standard library only; pure.

Place this file at: m2c/src/m2c/tui/diff.py
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .safe import clean, clean_line

__all__ = [
    "Diff", "DiffLine", "Existing", "HEAD_LINES", "MAX_DIFF_LINES", "MAX_INPUT_CHARS",
    "collapse", "diff_for_call", "expand", "unified_diff",
]

HEAD_LINES = 12
MAX_INPUT_CHARS = 400_000
MAX_DIFF_LINES = 5000
MAX_LINE_CHARS = 400


@dataclass(frozen=True)
class DiffLine:
    """``kind`` is ``add``, ``del``, ``ctx``, ``hunk`` or ``note``."""

    kind: str
    text: str


@dataclass(frozen=True)
class Diff:
    path: str
    lines: tuple[DiffLine, ...]
    added: int
    removed: int
    note: str = ""

    @property
    def title(self) -> str:
        return f"{self.path}  +{self.added} -{self.removed}" + (f"  ({self.note})" if self.note else "")


@dataclass(frozen=True)
class Existing:
    """What the caller found at the path of a ``write_file``: ``status`` is ``missing``, ``ok`` (with ``text``) or
    ``unreadable`` (protected, outside the project, binary, too big)."""

    status: str
    text: str = ""


ReadExisting = Callable[[str], Existing]


def _lines(text: str) -> list[str]:
    if not text:
        return []
    lines = clean(text).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:MAX_LINE_CHARS] for line in lines]


def _count(text: str) -> int:
    return 0 if not text else text.count("\n") + (0 if text.endswith("\n") else 1)


def unified_diff(path: str, old: str, new: str, *, context: int = 3, note: str = "") -> Diff:
    path = clean_line(path, 200)
    if len(old) + len(new) > MAX_INPUT_CHARS:
        removed = _count(old)
        added = _count(new)
        return Diff(path, (DiffLine("note", "large change: the line-by-line diff is not computed"),),
                    added, removed, note)
    old_lines, new_lines = _lines(old), _lines(new)
    out: list[DiffLine] = []
    added = removed = 0
    for line in difflib.unified_diff(old_lines, new_lines, n=context, lineterm=""):
        if line.startswith(("--- ", "+++ ")) and not out:
            continue
        if line.startswith("@@"):
            out.append(DiffLine("hunk", line))
        elif line.startswith("+"):
            added += 1
            out.append(DiffLine("add", line[1:]))
        elif line.startswith("-"):
            removed += 1
            out.append(DiffLine("del", line[1:]))
        else:
            out.append(DiffLine("ctx", line[1:]))
    if len(out) > MAX_DIFF_LINES:
        extra = len(out) - MAX_DIFF_LINES
        out = out[:MAX_DIFF_LINES] + [DiffLine("note", f"{extra} more lines are not shown")]
    return Diff(path, tuple(out), added, removed, note)


def diff_for_call(tool: str, args: Any, read_existing: Optional[ReadExisting] = None) -> Optional[Diff]:
    """The diff to show for a proposed call, or None when the tool is not an edit or the input is malformed."""
    if not isinstance(args, dict) or not isinstance(args.get("path"), str):
        return None
    path = args["path"]
    if tool == "edit_file":
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        return unified_diff(path, old, new, note="every occurrence" if args.get("replace_all") is True else "")
    if tool == "write_file":
        content = args.get("content")
        if not isinstance(content, str):
            return None
        existing = read_existing(path) if read_existing is not None else Existing("unreadable")
        if existing.status == "ok":
            return unified_diff(path, existing.text, content)
        note = "new file" if existing.status == "missing" else "current contents not shown"
        return unified_diff(path, "", content, note=note)
    return None


def collapse(diff: Diff, head: int = HEAD_LINES) -> tuple[tuple[DiffLine, ...], int]:
    """The first ``head`` lines and how many are left out."""
    if len(diff.lines) <= head:
        return diff.lines, 0
    return diff.lines[:head], len(diff.lines) - head


def expand(diff: Diff) -> tuple[DiffLine, ...]:
    return diff.lines
