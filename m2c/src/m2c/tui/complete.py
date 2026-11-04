"""
m2c/src/m2c/tui/complete.py -- Patch C09a: ``@`` file mentions.

Typing ``@`` in the message line completes project file paths.  It only INSERTS the relative path into the message;
it never reads or attaches contents.  What is offered is decided here (pure logic plus a bounded directory scan);
the prompt_toolkit ``Completer`` that calls it lives in app.py.

WHAT IS NEVER OFFERED
---------------------
* Anything C05's denylist blocks (``Denylist.blocks`` on the root-relative parts: ``.env``, keys, M2C's own state
  directory, the user's patterns), and the directory is not entered either.
* ``.git`` (and ``node_modules``, ``__pycache__``: they would use up the scan budget).
* Symbolic links of any kind (a link can point outside the project or at a protected file; the scan neither offers
  nor follows them).  Each candidate is also run through C05's ``resolve_for_read`` as a last check.
* Names with control or format characters (they would put hidden text into the message line).

BOUNDS
------
The scan stops after ``MAX_FILES`` files or ``TIME_BUDGET_S`` seconds, whichever comes first (the loop it runs on
also carries the device link), and a query returns at most ``MAX_CANDIDATES`` paths.  ``Scan.complete`` says whether
the whole tree was seen.

Standard library plus C05's path guard.

Place this file at: m2c/src/m2c/tui/complete.py
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..tools.paths import Denylist, PathGuardError, resolve_for_read
from .safe import clean

__all__ = ["MAX_CANDIDATES", "MAX_FILES", "SKIP_DIRS", "Scan", "TIME_BUDGET_S", "candidates", "mention_at",
           "scan_project"]

MAX_FILES = 5000
MAX_CANDIDATES = 50
TIME_BUDGET_S = 0.25
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__"})
_MENTION = re.compile(r"(?:^|\s)@([^\s@]*)$")


@dataclass(frozen=True)
class Scan:
    files: tuple[str, ...]
    complete: bool


def mention_at(text_before_cursor: str) -> Optional[tuple[int, str]]:
    """``(index of the prefix, prefix)`` when the cursor is right after ``@something``, else None."""
    m = _MENTION.search(text_before_cursor)
    return (m.start(1), m.group(1)) if m else None


def _plain(name: str) -> bool:
    return bool(name) and clean(name) == name and "\n" not in name and "\t" not in name


def scan_project(root: Path, denylist: Denylist, *, max_files: int = MAX_FILES, time_budget_s: float = TIME_BUDGET_S,
                 clock: Callable[[], float] = time.monotonic) -> Scan:
    deadline = clock() + time_budget_s
    files: list[str] = []
    queue: list[tuple[str, ...]] = [()]
    complete = True
    while queue:
        parts = queue.pop(0)
        if clock() > deadline:
            complete = False
            break
        try:
            with os.scandir(root.joinpath(*parts)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            try:
                if entry.is_symlink() or not _plain(name):
                    continue
                child = parts + (name,)
                if denylist.blocks(child):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name not in SKIP_DIRS:
                        queue.append(child)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        if resolve_for_read(root, "/".join(child), denylist).rel != child:
                            continue
                    except PathGuardError:
                        continue
                    files.append("/".join(child))
                    if len(files) >= max_files:
                        return Scan(tuple(files), False)
            except OSError:
                continue
    return Scan(tuple(files), complete)


def candidates(files: tuple[str, ...], prefix: str, limit: int = MAX_CANDIDATES) -> list[str]:
    """Paths that match what has been typed after the ``@``: path start first, then name start, then anywhere."""
    p = clean(prefix).lower()
    rank: list[tuple[int, int, str]] = []
    for f in files:
        low = f.lower()
        base = low.rsplit("/", 1)[-1]
        if low.startswith(p):
            rank.append((0, len(f), f))
        elif base.startswith(p):
            rank.append((1, len(f), f))
        elif p in low:
            rank.append((2, len(f), f))
    rank.sort()
    return [f for _, _, f in rank[:limit]]
