"""
m2c/src/m2c/tui/safe.py -- Patch C09a: making untrusted text safe to show in a terminal.

EVERYTHING THE SERVER OR THE MODEL SENDS IS UNTRUSTED TEXT
----------------------------------------------------------
A model can be steered by a file it read, and a server is only as trustworthy as its last deploy.  Text that reaches
a terminal can recolor it, move the cursor, rewrite earlier lines, set the window title, make a hyperlink or hide
what a command really says (bidirectional overrides).  Every string that came from the server, the model or a file
therefore goes through ``clean`` before it is displayed, and is never handed to a markup interpreter
(``escape_markup`` exists for any caller that must).  The renderer (render.py) does this once, at the door, so no
display code can forget it.

What ``clean`` removes: ANSI/VT escape sequences (CSI, OSC, DCS/SOS/PM/APC, two-character escapes, and the 8-bit C1
forms), every control character except newline and tab (a lone carriage return becomes a newline: it would
overwrite the line), DEL, C1 controls, bidirectional controls and every other "format" character (zero-width
characters, joiners), and lone surrogates.  It never raises and never returns a character that a terminal could
act on.  An escape sequence split across two streamed pieces leaves at most harmless printable letters behind.

Standard library only; pure; no I/O.

Place this file at: m2c/src/m2c/tui/safe.py
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["clean", "clean_line", "escape_markup", "safe_name", "truncate"]

_SEQUENCES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"        # OSC ... BEL / ST (or unterminated: the rest up to the next ESC)
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"            # DCS, SOS, PM, APC
    r"|\x1b\[[0-?]*[ -/]*[@-~]?"                 # CSI
    r"|\x1b[ -/]*[0-~]?"                         # any other escape (nF, Fp, Fe, Fs)
    r"|\x9b[0-?]*[ -/]*[@-~]?"                   # 8-bit CSI
    r"|[\x90\x98\x9e\x9f][^\x9c]*\x9c?"          # 8-bit DCS/SOS/PM/APC
    r"|\x9d[^\x07\x9c]*[\x07\x9c]?"              # 8-bit OSC
)
_NEWLINES = re.compile(r"\r\n|\r|\u2028|\u2029|\x85")
_ALLOWED = {"\n", "\t"}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")
ELLIPSIS = "…"


def clean(text: object, max_chars: int | None = None) -> str:
    """``text`` made safe to print: no escape sequences, no control or format characters (newline and tab stay).
    Anything that is not a string is shown as the empty string.  ``max_chars`` cuts the result (with an ellipsis)."""
    if not isinstance(text, str):
        return ""
    text = _SEQUENCES.sub("", text)
    text = _NEWLINES.sub("\n", text)
    out = []
    for ch in text:
        if ch in _ALLOWED:
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs"):
            continue
        out.append(ch)
    result = "".join(out)
    return truncate(result, max_chars) if max_chars is not None else result


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return text if len(text) <= max_chars else text[: max(max_chars - 1, 0)] + ELLIPSIS


def clean_line(text: object, max_chars: int = 120) -> str:
    """One line: ``clean`` with every run of whitespace (newlines included) collapsed to one space, then cut."""
    return truncate(" ".join(clean(text).split()), max_chars)


def safe_name(name: object, default: str = "unknown") -> str:
    """A tool or code name fit to show: letters, digits, ``_`` and ``-`` only, else ``default``."""
    return name if isinstance(name, str) and _NAME.match(name) else default


_BRACKET = re.compile(r"(\\*)\[")
_TAG_TAIL = re.compile(r"[a-z#/@][^\[]*?]")


def escape_markup(text: object) -> str:
    """``text`` made inert for rich's markup parser: rendering the result never raises and never produces a style,
    link or other span, and its plain text equals ``clean(x)`` except that a run of backslashes directly in front
    of a non-tag ``[`` may lose one level (rich's own rule); backslashes in front of a ``[`` are doubled (rich eats one level of them) and a ``[`` that starts
    a tag-shaped token is escaped.  The UI builds ``Text`` objects and never parses untrusted text as markup; this
    is for any caller that has to."""
    text = clean(text)

    def fix(match: re.Match) -> str:
        slashes = match.group(1) * 2
        tag = _TAG_TAIL.match(text, match.end()) is not None
        return slashes + ("\\[" if tag else "[")

    return _BRACKET.sub(fix, text)
