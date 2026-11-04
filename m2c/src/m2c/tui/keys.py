"""
m2c/src/m2c/tui/keys.py -- Patch C09a: telling Esc from Esc Esc.

A terminal sends one key per press and nothing says "this is the first half of a double press", so the UI decides
itself: the FIRST Escape interrupts at once (a delay would make Esc feel broken), and a SECOND Escape within
``WINDOW_S`` seconds is the rewind gesture instead of a second interrupt.  Pure: the clock is injected.

Place this file at: m2c/src/m2c/tui/keys.py
"""
from __future__ import annotations

import time
from typing import Callable

__all__ = ["EscapeTracker", "WINDOW_S"]

WINDOW_S = 0.6


class EscapeTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic, window_s: float = WINDOW_S) -> None:
        self._clock = clock
        self._window = window_s
        self._last: float | None = None

    def press(self) -> str:
        """``interrupt`` for a first press, ``rewind`` for the second within the window (which resets the pair)."""
        now = self._clock()
        if self._last is not None and now - self._last <= self._window:
            self._last = None
            return "rewind"
        self._last = now
        return "interrupt"
