"""
m2c/src/m2c/logging_setup.py -- Patch C01: logging that can never record
message content.

The rule (build plan §11.4, applied to the client): nothing the user typed,
nothing the model produced and nothing a tool returned may appear in a log
line -- not in the message, not in a %-style argument, not in an ``extra=``
field, not inside a traceback.  Logs are what people paste into bug reports,
so "we only meant to log the id" is not good enough; the logging setup
itself has to make the leak impossible rather than rely on every future
call site being careful.

How, in layers (all enforced by tests/test_logging_setup.py):

1. ``log_event(logger, level, "dotted.event_name", key=value, ...)`` is the
   sanctioned way to log anything with data in it.  Only keys in
   ``SAFE_KEYS`` are emitted -- ids, counts, codes, durations: the same
   things §11.4 says the server may log.  Values must be numbers, booleans
   or short identifier-like strings; anything else becomes ``<redacted>``.
   A key outside the allowlist is not printed at all, only counted
   (``_dropped=N``).  Adding a key to ``SAFE_KEYS`` is a reviewed decision:
   never add one that can carry user text.

2. ``ContentFreeFilter`` is attached to the handler, so it also covers
   records produced by plain ``logger.info(...)`` calls:
   * %-style / mapping arguments are discarded -- the literal template is
     logged, the values never are (``log.info("user said %s", text)``
     prints ``user said %s``);
   * every non-standard attribute (what ``extra={...}`` adds) is removed
     from the record, so a later handler that dumps ``record.__dict__``
     cannot see it either;
   * a non-string message object (whose ``__str__`` could embed anything)
     is replaced by a fixed placeholder.
   The filter sanitises; it never drops a record.

3. ``ContentFreeFormatter`` prints exceptions as their *type name only*.
   Exception messages routinely embed the offending input
   (``KeyError('<the prompt>')``), so neither the message nor the
   traceback text is printed.  It also ignores the traceback text another
   handler may already have cached on the record.

What this cannot do: stop a developer from writing
``logger.info(f"got {text}")`` -- by the time logging sees the string it is
already content.  The convention is therefore: log literals, or use
``log_event``.  Any additional handler attached to the ``m2c`` logger
must be made with ``build_handler`` so it carries the same filter and
formatter.

Logging fails open: ``log_event`` swallows its own errors so a logging bug
can never break a command.

Place this file at: m2c/src/m2c/logging_setup.py
"""
from __future__ import annotations

import copy
import logging
import re
import sys
from typing import IO, Any

LOGGER_NAME = "m2c"

# Keys log_event will print.  Mirrors build plan §11.4 "May log".  Extending
# this set is a reviewed change; never add a key that can hold user text.
SAFE_KEYS = frozenset(
    {
        "check",
        "status",
        "code",
        "count",
        "duration_ms",
        "exit_code",
        "exc_type",
        "run_id",
        "device_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "credits",
        "seq",
        "version",
    }
)

_EVENT_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
_SAFE_STR_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_REDACTED = "<redacted>"
_SUPPRESSED = "<non-string log message suppressed>"

# Marks the one handler configure_logging() installs, so it can be replaced.
_HANDLER_MARK = "_m2c_handler"

# Attributes every LogRecord has (plus the two the Formatter adds).  Anything
# else on a record came from ``extra=`` and is removed by the filter.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}


def _render_value(value: Any) -> str:
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and _SAFE_STR_RE.match(value):
        return value
    return _REDACTED


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log ``event key=value ...`` using only allowlisted, content-free data."""
    try:
        if not logger.isEnabledFor(level):
            return
        name = event if _EVENT_RE.match(event) else "invalid_event"
        parts = [name]
        dropped = 0
        for key, value in fields.items():
            if key in SAFE_KEYS:
                parts.append(f"{key}={_render_value(value)}")
            else:
                dropped += 1
        if dropped:
            parts.append(f"_dropped={dropped}")
        logger.log(level, " ".join(parts))
    except Exception:  # logging fails open, by design: never break a command over a log line
        pass


class ContentFreeFilter(logging.Filter):
    """Strip every route by which a value could reach a log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            record.msg = _SUPPRESSED
        record.args = ()
        for attr in [a for a in vars(record) if a not in _STANDARD_ATTRS]:
            delattr(record, attr)
        return True


class ContentFreeFormatter(logging.Formatter):
    """Fixed format; exceptions are shown as their type name only."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        # Work on a copy with the cached traceback text cleared: another
        # handler's default Formatter may already have stored the full
        # traceback (message included) in record.exc_text.
        safe = copy.copy(record)
        safe.exc_text = None
        return super().format(safe)

    def formatException(self, ei: Any) -> str:
        exc_type = ei[0] if ei else None
        return f"exception: {exc_type.__qualname__}" if exc_type else "exception: unknown"


def build_handler(stream: IO[str] | None = None) -> logging.Handler:
    """A StreamHandler carrying the content-free filter and formatter.

    ``stream=None`` means the *current* ``sys.stderr`` (looked up now, not at
    import time, so redirection in tests and wrappers works).
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(ContentFreeFilter())
    handler.setFormatter(ContentFreeFormatter())
    return handler


def configure_logging(level: int = logging.WARNING, stream: IO[str] | None = None) -> logging.Logger:
    """Configure the ``m2c`` logger.  Idempotent: replaces its own handler.

    Propagation to the root logger is switched off so a root handler set up
    by some embedding program (which would not have our filter) never sees
    m2c records.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for existing in [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]:
        logger.removeHandler(existing)
    handler = build_handler(stream)
    setattr(handler, _HANDLER_MARK, True)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
