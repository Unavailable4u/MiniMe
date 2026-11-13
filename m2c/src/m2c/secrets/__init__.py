"""
m2c/src/m2c/secrets/__init__.py -- Patch C15: secrets, redaction and environment.

    redact.py   find secrets in text and replace them with ``[REDACTED:<kind>]``: one-shot (``redact``), as a stream
                (``StreamRedactor``), and the strict profile C12's memory-write check uses (``scan_lines``)
    env.py      the environment a shell command gets: C07's rules as the floor, more deny patterns, an allow list,
                and named per-project exceptions read only from the person's own user settings

The runner (C24) redacts every outgoing ``tool_result`` and ``tool_stream`` with this package before the link sees it.
``redact`` the function is deliberately NOT re-exported here: a package attribute called ``redact`` would hide the
submodule of the same name (``import m2c.secrets.redact as r`` would hand back the function).  Import it as
``from m2c.secrets.redact import redact``.

Everything fails closed, logs kinds and counts only, and needs nothing but the standard library.

Place this file at: m2c/src/m2c/secrets/__init__.py
"""
from __future__ import annotations

from .env import ALLOWED_NAMES, granted_names, sanitize_environment
from .redact import (
    FAIL_CLOSED_TEXT,
    KIND_TEXT,
    Finding,
    Redaction,
    StreamRedactor,
    contains_secret,
    marker_for,
    redaction_note,
    scan_lines,
)

__all__ = [
    "ALLOWED_NAMES",
    "FAIL_CLOSED_TEXT",
    "KIND_TEXT",
    "Finding",
    "Redaction",
    "StreamRedactor",
    "contains_secret",
    "granted_names",
    "marker_for",
    "redaction_note",
    "sanitize_environment",
    "scan_lines",
]
