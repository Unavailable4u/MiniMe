"""
m2c/src/m2c/tui/status.py -- Patch C09a: the status line, and the live mode cycle.

``StatusState`` is what the status line knows; ``observe`` folds one server event into it; ``format_status`` and
``status_lines`` turn it into text.  Pure: no terminal, no clock, no I/O.

WHAT THE LINE SHOWS
-------------------
mode · the model (its quality class when known, else the model id of the last usage event) · the TAINTED flag, only
when it is true (C17 comes later, so it is always false for now) · the context percentage · the credits spent in this
run (and the session total when it differs) · "working" and the number of queued messages.  Credits only: no
dollars anywhere.

THE CONTEXT PERCENTAGE IS AN ESTIMATE
-------------------------------------
It is the ``input_tokens`` of the LAST usage event over a window size (a constant, ``M2C_CONTEXT_WINDOW_TOKENS`` can
override it, an unusable value is ignored).  The text says ``~`` and the second line says "approximate".  The server
compacts the context itself, so this is a hint, not a guarantee.

THE QUALITY CLASS
-----------------
The protocol has no quality-class field.  The class reaches a client only inside the fixed text of the
``model_changed`` notice ("... so a <class> model is answering instead."); ``observe`` reads it with a strict
pattern (lowercase letters, at most 24).  Anything else leaves it unknown.  A protocol field would be better; that
is a note for the protocol owner, not something this patch invents.

MODES
-----
``cycle_mode`` is what Shift+Tab does: ask <-> accept-edits, which differ only in the device policy.  It NEVER goes
to plan, and it does not leave plan either: the server learns the mode only at ``run.start``, so plan is chosen at
startup and left through the ``plan_exit`` tool.

Standard library only; pure.

Place this file at: m2c/src/m2c/tui/status.py
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .safe import clean_line, safe_name

__all__ = [
    "CYCLE_MODES", "DEFAULT_WINDOW_TOKENS", "StatusState", "context_percent", "cycle_mode", "format_credits",
    "format_status", "observe", "status_lines", "window_from_env",
]

DEFAULT_WINDOW_TOKENS = 128_000
CYCLE_MODES = ("ask", "accept-edits")
_CLASS = re.compile(r"\ba ([a-z]{1,24}) model is answering instead")


@dataclass
class StatusState:
    mode: str = "ask"
    quality_class: Optional[str] = None
    model: Optional[str] = None
    tainted: bool = False
    input_tokens: Optional[int] = None
    window_tokens: int = DEFAULT_WINDOW_TOKENS
    run_credits: float = 0.0
    session_credits: float = 0.0
    working: bool = False
    queued: int = 0
    paused: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def start_run(self) -> None:
        """A new run: its own credits and context start over (the session total keeps counting)."""
        self.run_credits = 0.0
        self.input_tokens = None
        self.working = False
        self.queued = 0
        self.paused = None


def window_from_env(environ: Mapping[str, str]) -> int:
    raw = environ.get("M2C_CONTEXT_WINDOW_TOKENS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WINDOW_TOKENS
    return value if 1000 <= value <= 10_000_000 else DEFAULT_WINDOW_TOKENS


def context_percent(input_tokens: Optional[int], window_tokens: int) -> Optional[int]:
    if input_tokens is None or window_tokens <= 0 or input_tokens < 0:
        return None
    return min(100, round(100 * input_tokens / window_tokens))


def cycle_mode(mode: str) -> str:
    """What Shift+Tab switches to.  Plan (and anything unknown) is returned unchanged."""
    if mode == "ask":
        return "accept-edits"
    if mode == "accept-edits":
        return "ask"
    return mode


def format_credits(value: float) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "?"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def observe(state: StatusState, event: Any) -> None:
    """Fold one ``LinkEvent`` (duck-typed: ``.type``, ``.subtype``, ``.payload``) into ``state``."""
    if getattr(event, "type", None) != "run_event":
        return
    payload = event.payload if isinstance(event.payload, dict) else {}
    kind = payload.get("type")
    if kind == "usage":
        tokens = payload.get("input_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            state.input_tokens = tokens
        model = payload.get("model")
        if isinstance(model, str) and model:
            state.model = clean_line(model, 32)
        credits = payload.get("credits")
        total = payload.get("run_credits_total")
        if isinstance(total, (int, float)) and not isinstance(total, bool) and math.isfinite(total) and total >= 0:
            delta = max(total - state.run_credits, 0.0)
            state.run_credits = float(total)
            state.session_credits += delta
        elif isinstance(credits, (int, float)) and not isinstance(credits, bool) and math.isfinite(credits) \
                and credits >= 0:
            state.run_credits += float(credits)
            state.session_credits += float(credits)
    elif kind == "notice" and payload.get("code") == "model_changed":
        found = _CLASS.search(payload.get("message") if isinstance(payload.get("message"), str) else "")
        if found:
            state.quality_class = found.group(1)
    elif kind == "state_changed":
        value = payload.get("state")
        state.paused = value if value in ("paused_budget", "paused_error", "paused_disconnected") else None


def format_status(state: StatusState) -> str:
    parts = [f"mode: {safe_name(state.mode, '?')}"]
    if state.quality_class:
        parts.append(f"model: {state.quality_class}")
    elif state.model:
        parts.append(f"model: {state.model}")
    if state.tainted:
        parts.append("TAINTED")
    pct = context_percent(state.input_tokens, state.window_tokens)
    parts.append(f"ctx ~{pct}%" if pct is not None else "ctx -")
    credits = f"credits: {format_credits(state.run_credits)}"
    if state.session_credits - state.run_credits > 0.004:
        credits += f" (session {format_credits(state.session_credits)})"
    parts.append(credits)
    if state.paused:
        parts.append(state.paused.replace("_", " "))
    elif state.working:
        parts.append("working")
    if state.queued:
        parts.append(f"{state.queued} queued")
    return " | ".join(parts)


def status_lines(state: StatusState) -> list[str]:
    """The status line, then one line per note (such as "project settings are ignored"), then the footnote."""
    lines = [format_status(state)]
    lines.extend(clean_line(note, 200) for note in state.notes)
    lines.append("ctx is an approximate estimate; credits are for this run")
    return lines
