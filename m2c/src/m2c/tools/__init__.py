"""
m2c/src/m2c/tools/__init__.py -- Patch C05: local tool executors v0 and the
``run_tool`` entry point.

What this package is
--------------------
The code that touches the user's files.  When the server proposes a tool call
(protocol/v1 ``tool_proposed``) and the approval flow allows it, the client
runs it here and sends back a ``tool_result``.  This patch implements the seven
file and search tools; C07 adds the shell tool, I02 wires the device link to
``run_tool``, and C06 turns the user's settings into the ``user_denylist``
argument.  Nothing here reads settings, talks to the network, or imports
``daemon/``.

    paths.py    the path guard: containment, symlinks, the two-form denylist,
                no-follow opening.  Ported from daemon/path_guard.py.
    files.py    read_file, list_dir, edit_file, write_file, delete_path.
                Ported from daemon/tools.py (the file tools only).
    search.py   glob and grep (a hardened ripgrep wrapper).
    __init__.py ``Caps``, ``ToolContext``, ``ToolResult``, the argument table
                and ``run_tool``.

Defense in depth, not the boundary
----------------------------------
The sandbox (C11) is the real security boundary.  These checks stop the model,
a hostile repository or a prompt injection from steering a *legitimate* tool
call at the wrong file; they are not a substitute for confinement.

How a call runs
---------------
``run_tool(context, name, args)``

1. fails closed on an unknown tool name or non-object arguments;
2. validates ``args`` against ``ARG_SPECS`` (a table that mirrors the P00 JSON
   schemas, and that a test compares with them): unknown, missing, mistyped and
   out-of-range arguments are refused, and the message never repeats a value the
   model supplied;
3. calls the executor, which returns ``(output, truncated)`` or raises
   ``ToolError`` (a stable ``code`` and a fixed message);
4. turns anything else that was raised into ``internal_error`` with no text;
5. clamps the output to the protocol's 200000-character ``bulk_text`` limit;
6. logs the tool name, status, error code, output size and duration, and
   nothing else (never a path, pattern, argument or file content).

The result is a ``ToolResult``; ``to_payload(call_id)`` gives the exact
``tool_result`` object.  Trust is always ``local_project``: the text came from
files under the project root, so it is data, never instructions.  The
per-tool error ``code`` is local (protocol v1 has no such field) and travels
only in the text of a failed result.

``delete_path`` has no ``recursive`` argument at this layer, because the
protocol's schema has none; ``files.delete_path`` supports it for code that
calls it directly.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..logging_setup import log_event
from ..paths import ConfigDirError, config_dir
from . import files, paths, search
from .files import SeenFiles
from .paths import ERROR_CODES, Denylist, ToolError, assert_safe_root

__all__ = [
    "ARG_SPECS", "Caps", "ToolContext", "ToolError", "ToolResult", "TOOLS",
    "paths", "run_tool", "tool_names",
]

logger = logging.getLogger("m2c.tools")

# protocol/v1 common.json bulk_text.maxLength
MAX_WIRE_CHARS = 200_000
# Output caps are kept a little under the wire limit so a marker always fits.
_MAX_OUTPUT_CAP = 190_000


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
def _positive_int(name: str, value: object, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")


def _positive_number(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not value > 0:
        raise ValueError(f"{name} must be a positive number")


@dataclass(frozen=True)
class Caps:
    """Size and time limits for one session's tool calls.

    Every field has a default, is validated (a bad value raises ValueError at
    construction, so a bad setting is found at startup, not mid-run), and the
    three that bound a result's text cannot exceed what the wire allows.
    """

    read_output_chars: int = 100_000      # text returned by one read_file
    max_line_chars: int = 2_000           # one line is cut here (read_file, grep)
    max_file_bytes: int = 1_000_000       # largest file read whole, edited or overwritten
    max_write_bytes: int = 2_000_000      # largest write_file content
    list_max_entries: int = 500           # list_dir entries
    glob_max_results: int = 2_000         # ceiling on glob's max_results
    glob_max_walk_entries: int = 200_000  # directory entries glob will look at
    glob_timeout_s: float = 5.0
    grep_timeout_s: float = 20.0
    grep_output_bytes: int = 60_000       # bytes of grep output kept
    grep_max_raw_bytes: int = 4_000_000   # bytes read from ripgrep before stopping it
    diff_max_chars: int = 8_000           # the diff returned by edit_file
    max_delete_entries: int = 10_000      # entries in a recursive delete
    stale_temp_age_s: float = 3_600.0     # age after which a leftover temp file is swept

    def __post_init__(self) -> None:
        _positive_int("read_output_chars", self.read_output_chars, _MAX_OUTPUT_CAP)
        _positive_int("max_line_chars", self.max_line_chars, _MAX_OUTPUT_CAP)
        _positive_int("max_file_bytes", self.max_file_bytes)
        _positive_int("max_write_bytes", self.max_write_bytes)
        _positive_int("list_max_entries", self.list_max_entries)
        _positive_int("glob_max_results", self.glob_max_results)
        _positive_int("glob_max_walk_entries", self.glob_max_walk_entries)
        _positive_number("glob_timeout_s", self.glob_timeout_s)
        _positive_number("grep_timeout_s", self.grep_timeout_s)
        _positive_int("grep_output_bytes", self.grep_output_bytes, _MAX_OUTPUT_CAP)
        _positive_int("grep_max_raw_bytes", self.grep_max_raw_bytes)
        _positive_int("diff_max_chars", self.diff_max_chars, _MAX_OUTPUT_CAP)
        _positive_int("max_delete_entries", self.max_delete_entries)
        _positive_number("stale_temp_age_s", self.stale_temp_age_s)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
def _state_dirs() -> list[Path]:
    """M2C's own state directories: wherever ``M2C_HOME`` points, and ``~/.m2c``.

    Resolved at call time.  Either may be unavailable (no HOME, a bad
    ``M2C_HOME``); the other still protects.
    """
    dirs: list[Path] = []
    try:
        dirs.append(config_dir())
    except ConfigDirError:
        pass
    try:
        dirs.append(Path.home() / ".m2c")
    except RuntimeError:
        pass
    return dirs


class ToolContext:
    """Everything one session's tool calls share.

    ``root``          the project directory, validated by ``assert_safe_root``
                      (raises ``PathGuardError`` for '/', the home directory,
                      other broad roots, credential directories, M2C's state
                      directory, or a missing directory).
    ``user_denylist`` extra patterns from settings (C06 supplies them); the hard
                      list is always on and cannot be loosened by this.  A bad
                      pattern raises ValueError.
    ``caps``          a ``Caps`` (defaults if omitted).
    ``rg_path``       an explicit ripgrep binary; otherwise ``rg`` is looked up
                      on a cleaned PATH when grep runs.

    ``seen`` is the session's read-before-write record.  Contexts do not share
    it.
    """

    def __init__(
        self,
        root: str | Path,
        user_denylist: Any = (),
        caps: Caps | None = None,
        rg_path: str | Path | None = None,
    ) -> None:
        state = _state_dirs()
        self.root: Path = assert_safe_root(root, protected_dirs=state)
        self.denylist = Denylist(user_denylist, root=self.root, protected_dirs=state)
        if caps is not None and not isinstance(caps, Caps):
            raise ValueError("caps must be a Caps")
        self.caps: Caps = caps if caps is not None else Caps()
        self.seen = SeenFiles()
        self.rg_path = None if rg_path is None else str(rg_path)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolResult:
    """The outcome of one call, in the shape protocol/v1 ``tool_result`` carries."""

    ok: bool
    output: str
    truncated: bool = False
    trust: str = "local_project"
    code: str | None = None  # local error code; never sent on the wire
    duration_ms: int = 0

    def to_payload(self, call_id: str) -> dict[str, Any]:
        """The ``tool_result`` payload (a closed object: no ``code`` here)."""
        return {
            "call_id": call_id,
            "ok": self.ok,
            "output": self.output,
            "truncated": self.truncated,
            "trust": self.trust,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# The argument table (mirrors protocol/v1/tools/*.json; a test compares them)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArgSpec:
    type: type
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None
    default: Any = None


_S = ArgSpec(str)

ARG_SPECS: dict[str, dict[str, ArgSpec]] = {
    "read_file": {
        "path": ArgSpec(str, required=True),
        "start_line": ArgSpec(int, minimum=1),
        "end_line": ArgSpec(int, minimum=1),
    },
    # The schema lists "path" as required while also giving it a default and
    # describing it as optional; the executor follows the description.
    "list_dir": {
        "path": ArgSpec(str, default="."),
        "max_depth": ArgSpec(int, minimum=1, maximum=8, default=1),
    },
    "glob": {
        "pattern": ArgSpec(str, required=True),
        "path": ArgSpec(str),
        "max_results": ArgSpec(int, minimum=1, maximum=2000, default=200),
    },
    "grep": {
        "pattern": ArgSpec(str, required=True),
        "path": ArgSpec(str),
        "glob": ArgSpec(str),
        "context_lines": ArgSpec(int, minimum=0, maximum=10, default=0),
        "case_insensitive": ArgSpec(bool, default=False),
        "max_matches": ArgSpec(int, minimum=1, maximum=1000, default=200),
    },
    "edit_file": {
        "path": ArgSpec(str, required=True),
        "old_string": ArgSpec(str, required=True),
        "new_string": ArgSpec(str, required=True),
        "replace_all": ArgSpec(bool, default=False),
    },
    "write_file": {
        "path": ArgSpec(str, required=True),
        "content": ArgSpec(str, required=True),
    },
    "delete_path": {
        "path": ArgSpec(str, required=True),
    },
}

TOOLS: dict[str, Callable[..., tuple[str, bool]]] = {
    "read_file": files.read_file,
    "list_dir": files.list_dir,
    "glob": search.glob,
    "grep": search.grep,
    "edit_file": files.edit_file,
    "write_file": files.write_file,
    "delete_path": files.delete_path,
}


def tool_names() -> list[str]:
    """The tools this executor implements (the shell tool is C07)."""
    return sorted(TOOLS)


def _validate(name: str, args: Any) -> dict[str, Any]:
    """Check ``args`` against the table; return the arguments to pass on.

    Raises ToolError(invalid_args).  Messages name the table's own argument
    names, never a value or key the model supplied.
    """
    spec = ARG_SPECS[name]
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object", code="invalid_args")
    for key in args:
        if not isinstance(key, str) or key not in spec:
            raise ToolError(
                "unknown argument; the allowed arguments are: " + ", ".join(spec), code="invalid_args"
            )
    for key, s in spec.items():
        if s.required and key not in args:
            raise ToolError(f"missing required argument: {key}", code="invalid_args")
    clean: dict[str, Any] = {}
    for key, value in args.items():
        s = spec[key]
        if type(value) is not s.type:  # exact type: a bool is not an int
            kind = {str: "a string", int: "an integer", bool: "a boolean"}[s.type]
            raise ToolError(f"argument {key} must be {kind}", code="invalid_args")
        if s.type is int:
            if s.minimum is not None and value < s.minimum:
                raise ToolError(f"argument {key} must be at least {s.minimum}", code="invalid_args")
            if s.maximum is not None and value > s.maximum:
                raise ToolError(f"argument {key} must be at most {s.maximum}", code="invalid_args")
        clean[key] = value
    return clean


# ---------------------------------------------------------------------------
# run_tool
# ---------------------------------------------------------------------------
def _failure(code: str, message: str, started: float) -> ToolResult:
    if code not in ERROR_CODES:
        code = "internal_error"
    text = f"error: {message}" if message else f"error: {code}"
    return ToolResult(False, text, False, "local_project", code, _elapsed_ms(started))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _log(check: str, result: ToolResult) -> None:
    fields: dict[str, Any] = {"check": check, "status": "ok" if result.ok else "error"}
    if result.code:
        fields["code"] = result.code
    fields["count"] = len(result.output)
    fields["duration_ms"] = result.duration_ms
    log_event(logger, logging.DEBUG if result.ok else logging.INFO, "tools.call", **fields)


def _clamp(output: str, truncated: bool) -> tuple[str, bool]:
    if len(output) <= MAX_WIRE_CHARS:
        return output, truncated
    marker = "\n[truncated: output exceeded the size limit]"
    return output[: MAX_WIRE_CHARS - len(marker)] + marker, True


def run_tool(context: ToolContext, name: Any, args: Any) -> ToolResult:
    """Run one tool call.  Never raises (except for KeyboardInterrupt and other
    control-flow exceptions); every failure is a ``ToolResult`` with a code.

    Security checks fail closed; the log line fails open (``log_event`` never
    raises).
    """
    started = time.monotonic()
    if not isinstance(name, str) or name not in TOOLS or name not in ARG_SPECS:
        result = _failure("unknown_tool", "unknown tool", started)
        _log("unknown", result)
        return result
    try:
        clean = _validate(name, args)
        output, truncated = TOOLS[name](context, **clean)
        output, truncated = _clamp(str(output), bool(truncated))
        result = ToolResult(True, output, truncated, "local_project", None, _elapsed_ms(started))
    except ToolError as exc:
        result = _failure(exc.code, exc.message, started)
    except Exception as exc:  # noqa: BLE001 -- anything unexpected is a fixed, content-free error
        log_event(logger, logging.WARNING, "tools.error", check=name, exc_type=type(exc).__name__)
        result = _failure("internal_error", "the tool failed unexpectedly", started)
    _log(name, result)
    return result
