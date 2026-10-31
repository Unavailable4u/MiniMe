"""
m2c/src/m2c/tools/shell.py -- Patch C07: the shell tool v0 (``bash``).

SECURITY-CRITICAL, AND THERE IS NO SANDBOX YET
----------------------------------------------
This module runs a command the model proposed, as the user, with the user's
full privileges: it can read and write anything the user can, use the network
and start other programs.  The sandbox (C11, Wave 9) is the real boundary and
does not exist yet.  Until it does, the only things between the model and the
user's account are the approval flow (C06 decides, the device runner and the
terminal UI ask) and the checks in this file, which exist to make sure that

* nothing runs unless the caller says, explicitly, that it was authorized;
* a policy ``deny`` still wins over that authorization;
* the command cannot outlive its time limit or its cancel handle;
* the command does not inherit the user's credentials from the environment;
* its output cannot flood memory or inject escape codes into the terminal UI.

``SANDBOX_WARNING`` is the text the terminal UI (C09) shows, visibly, before the
first command of a session; ``ShellConfig.warn`` is how it receives it.  Nothing
in this patch gives a mode that skips approval.

How a call runs
---------------
``run_tool(context, "bash", args, call_env=CallEnv(...))`` (C05's entry point)
validates ``args`` against ``ARG_SPECS["bash"]`` (a copy of
protocol/v1/tools/bash.json) and calls ``bash(...)`` here, which calls
``run_command``:

1. **Authorization, fail closed.**  ``authorization`` must be exactly
   ``AUTH_POLICY_ALLOWED`` (C06 said allow) or ``AUTH_USER_APPROVED`` (the user
   said yes).  Anything else, including ``None`` and ``True``, refuses with
   ``ToolError(code="refused")`` before anything is looked at or started.  This
   patch never decides whether to ask; the later device runner does.
2. **Deny always wins.**  When a policy context (C06's ``Context``) is passed,
   ``decide(Request("bash", {"command": ...}, "R2"), policy)`` is run again, and
   a ``deny`` refuses even though the call is authorized.  A policy that cannot
   be evaluated (it raises, or answers something other than allow, ask or deny)
   also refuses.  The refusal message is fixed text: it never repeats the
   command.
3. **Arguments**: ``timeout_s`` must be an integer from 1 to 3600 (default 120,
   from bash.json); ``reason`` at most 200 characters (it is for the approval
   prompt and is not used here); the command must not contain a NUL byte.
4. The working directory must still exist (``not_found`` otherwise, nothing is
   started).
5. The command is started with ``bash -c`` (``sh -c`` if there is no bash on
   PATH), working directory pinned to the session root, standard input closed
   (``/dev/null``: a command that reads input fails fast instead of hanging),
   in a NEW SESSION (so it leads its own process group), with a sanitized
   environment.  Never a login or interactive shell, so rc files are not
   sourced; ``BASH_ENV`` and ``ENV``, which a non-interactive shell would
   source, are removed from the environment.
6. Output is read as it arrives, decoded as UTF-8 (invalid bytes become U+FFFD),
   stripped of terminal escape sequences and control characters, streamed to
   ``on_chunk(stream, text)`` and collected, bounded, for the final result.

The result
----------
A ``ToolOutcome`` (see below) with the fields of protocol/v1/tool_result.json:

* ``ok`` is true only for exit status 0 (never for a timeout or a cancel);
* ``exit_code`` is the command's status (128+N when a signal N ended it, the
  shell's own convention); None when we killed it (timeout, cancel);
* ``output`` combines stdout and stderr in the order they arrived.  A line
  ``[stdout]`` or ``[stderr]`` is inserted wherever the stream changes (and at
  the start), and the text always ends with a status line (``[exit status: 0]``,
  ``[timed out after 5s; ...]``, ``[cancelled; ...]``).  The ``exit_code`` and
  ``ok`` fields are authoritative; the text lines are for the model and can be
  imitated by the command's own output;
* ``truncated``: the total is capped (``ShellConfig.max_output_bytes``, default
  ``DEFAULT_MAX_OUTPUT_BYTES``).  The head and the tail are kept and a line
  ``[... N bytes omitted ...]`` says how much was dropped.  Memory stays bounded
  however much the command writes, because only the head and the tail are ever
  stored and the pipes are read in 64 KiB pieces;
* ``trust`` is ``SHELL_TRUST`` = ``"external"``.  The work package says
  "untrusted", but protocol/v1/common.json allows only ``local_project``,
  ``local_other`` and ``external``; protocol/ wins, and ``external`` is its most
  distrusted value (what a command printed may have come from anywhere: a
  fetched page, a file written by a stranger).  The server's broker labels it
  "untrusted" on its side.  It is data, never instructions;
* ``code`` is local and never on the wire: ``timeout``, ``cancelled`` or
  ``exit_nonzero`` for a failed run.

Timeout, cancel and the process group
-------------------------------------
On timeout or cancel the whole process group is sent SIGTERM; when the shell has
exited (or after ``ShellConfig.kill_grace_s``, default 2 s) every member still
left gets SIGKILL.  When a command ends by itself, anything still running in its
group is killed too (SIGKILL), so a call never leaves a stray process behind:
background jobs (``cmd &``) do not survive the call; there is no background shell
in v0 (that is C16).

LIMIT: a process that leaves the group (``setsid``, ``nohup setsid``, a daemon
that double-forks and calls ``setsid``) cannot be killed this way and keeps
running after the call.  Only a sandbox that confines the whole tree (C11) can
close that gap.  Signals are sent to the group only while its leader has not been
reaped (we detect its exit without reaping it), so the group id cannot have been
recycled.

The environment
---------------
Copied from the current environment, then every variable is removed whose name
contains KEY, TOKEN, SECRET, PASSWORD, PASSWD or CREDENTIAL (case-insensitive:
this also catches harmless names such as ``KEYBOARD``, on purpose), or starts
with ``AWS_``, ``AZURE_``, ``GOOGLE_``, ``GCP_``, ``GCLOUD_``, ``M2C_`` or
``BASH_FUNC_`` (exported shell functions), or is ``SSH_AUTH_SOCK``,
``KUBECONFIG``, ``DATABASE_URL``, ``BASH_ENV`` or ``ENV``.  ``ShellConfig.
extra_strip_names`` adds names (exact, or ``PREFIX*``).  ``PATH`` and ``HOME`` are
always kept.  Then ``TERM=dumb``, ``NO_COLOR=1``, ``PAGER=cat``, ``GIT_PAGER=cat``
and ``GIT_TERMINAL_PROMPT=0`` are set.  A project cannot name exceptions here;
per-project exceptions are C15's.  This is hygiene against accidental secret
inheritance, not a boundary: the command can still read files that hold secrets
(``~/.aws/credentials``), which is what the sandbox is for.

Logging
-------
One line per run through ``log_event``: exit status, duration and a size, and a
code for a failure.  Never the command, the reason, an argument or any output.

Configuration lives on the session's ``ToolContext`` without changing that class:
``configure_shell(context, ShellConfig(...))`` stores a ``ShellConfig`` as
``context.shell_config``; the "warned once" record is kept per context, so each
session is warned once.

Standard library only (plus ``m2c.policy``, which is too).

Place this file at: m2c/src/m2c/tools/shell.py
"""
from __future__ import annotations

import codecs
import logging
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..logging_setup import log_event
from ..policy.decide import Request, decide
from .paths import ToolError

__all__ = [
    "AUTHORIZATIONS", "AUTH_POLICY_ALLOWED", "AUTH_USER_APPROVED", "CallEnv", "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S", "SANDBOX_WARNING", "SHELL_TRUST", "ShellConfig", "TerminalSanitizer", "ToolOutcome",
    "bash", "configure_shell", "run_command", "sanitized_env",
]

logger = logging.getLogger("m2c.tools.shell")

SANDBOX_WARNING = "commands run with your full privileges; there is no sandbox yet"

# protocol/v1/common.json trust enum; see the module docstring for why this is not "untrusted".
SHELL_TRUST = "external"

# protocol/v1/tools/bash.json
DEFAULT_TIMEOUT_S = 120
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 3600
REASON_MAX_CHARS = 200

# What the caller may pass as ``authorization``: that C06 allowed the call, or that the user approved it.
AUTH_POLICY_ALLOWED = "policy_allowed"
AUTH_USER_APPROVED = "user_approved"
AUTHORIZATIONS = (AUTH_POLICY_ALLOWED, AUTH_USER_APPROVED)

# Output.  The cap is in bytes of UTF-8 and stays under C05's 190000 ceiling so the markers and the status line
# always fit in the protocol's 200000-character limit.
DEFAULT_MAX_OUTPUT_BYTES = 100_000
_MAX_OUTPUT_BYTES_LIMIT = 190_000
_MIN_OUTPUT_BYTES = 64

# Streaming and timing (all but the read size are overridable through ShellConfig).
READ_CHUNK = 65_536
FLUSH_BYTES = 4096            # a pending chunk is delivered once it reaches this many characters ...
FLUSH_INTERVAL_S = 0.1        # ... or once its first character is this old
POLL_INTERVAL_S = 0.05        # how often the cancel handle is looked at
KILL_GRACE_S = 2.0            # SIGTERM, then SIGKILL after this long
DRAIN_GRACE_S = 0.5           # after the shell exits, how long to wait for pipes that something else still holds
_DRAIN_AFTER_KILL_S = 1.0     # after a kill, how long to keep reading what is already in the pipes
_REAP_TIMEOUT_S = 5.0
_AFTER_EXIT_PAUSE_S = 0.1     # after the shell is gone, a moment for the others to react to SIGTERM

_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


# ---------------------------------------------------------------------------
# The two small types that C05's run_tool understands
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolOutcome:
    """What an executor returns when ``(output, truncated)`` is not enough.

    ``run_tool`` turns it into a ``ToolResult`` field for field: a FAILED result
    that still carries the command's output, a trust label other than
    ``local_project``, the command's ``exit_code`` and a local ``code``.  (It is
    defined here, next to its only user, so that C05's tools/paths.py stays
    untouched and the package has no import cycle.)
    """

    output: str
    truncated: bool = False
    ok: bool = True
    trust: str = "local_project"
    exit_code: int | None = None
    code: str | None = None


@dataclass(frozen=True)
class CallEnv:
    """What one call brings with it, handed by ``run_tool`` only to executors that declare ``call_env``.

    ``authorization``  ``AUTH_POLICY_ALLOWED`` or ``AUTH_USER_APPROVED``; anything else means "not authorized".
    ``policy``         optionally C06's ``Context``; when given, ``decide`` is run again and a deny refuses.
    ``cancel``         a handle with ``is_set()`` (a ``threading.Event``); when set the process group is killed.
    ``on_chunk``       ``callable(stream, text)`` for ``"stdout"`` / ``"stderr"`` text as it arrives (tool_stream).

    The model can never supply this: it is not an argument of the tool, and ``run_tool`` refuses unknown arguments.
    """

    authorization: Any = None
    policy: Any = None
    cancel: Any = None
    on_chunk: Callable[[str, str], None] | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _positive_number(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not value > 0:
        raise ValueError(f"{name} must be a positive number")


def _normalize_names(names: object) -> tuple[str, ...]:
    if isinstance(names, str) or not isinstance(names, (tuple, list, frozenset, set)):
        raise ValueError("extra_strip_names must be a tuple of names")
    out = []
    for name in names:
        if not isinstance(name, str) or not name.strip("*") or "=" in name or "\x00" in name:
            raise ValueError("extra_strip_names holds a bad name")
        out.append(name.upper())
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class ShellConfig:
    """Settings for one session's shell calls (all optional, all validated at construction).

    ``max_output_bytes``   cap on the output text (bytes of UTF-8), head plus tail; 64 to 190000.
    ``extra_strip_names``  more environment names to remove: exact names or ``PREFIX*``, case-insensitive.
    ``warn``               ``callable(text)`` given ``SANDBOX_WARNING`` once per session before the first command.
    ``kill_grace_s``       how long a SIGTERM'd group gets before SIGKILL.
    ``drain_grace_s``      how long to wait, after the shell exits, for output pipes something else still holds.
    ``flush_bytes`` / ``flush_interval_s``  the streaming coalescing thresholds.
    """

    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    extra_strip_names: tuple[str, ...] = ()
    warn: Callable[[str], None] | None = None
    kill_grace_s: float = KILL_GRACE_S
    drain_grace_s: float = DRAIN_GRACE_S
    flush_bytes: int = FLUSH_BYTES
    flush_interval_s: float = FLUSH_INTERVAL_S

    def __post_init__(self) -> None:
        m = self.max_output_bytes
        if isinstance(m, bool) or not isinstance(m, int) or not _MIN_OUTPUT_BYTES <= m <= _MAX_OUTPUT_BYTES_LIMIT:
            raise ValueError(f"max_output_bytes must be an integer from {_MIN_OUTPUT_BYTES} to {_MAX_OUTPUT_BYTES_LIMIT}")
        object.__setattr__(self, "extra_strip_names", _normalize_names(self.extra_strip_names))
        if self.warn is not None and not callable(self.warn):
            raise ValueError("warn must be callable")
        _positive_number("kill_grace_s", self.kill_grace_s)
        _positive_number("drain_grace_s", self.drain_grace_s)
        _positive_number("flush_interval_s", self.flush_interval_s)
        if isinstance(self.flush_bytes, bool) or not isinstance(self.flush_bytes, int) or self.flush_bytes <= 0:
            raise ValueError("flush_bytes must be a positive integer")


_DEFAULT_CONFIG = ShellConfig()
_WARNED: "weakref.WeakSet[Any]" = weakref.WeakSet()
_WARN_LOCK = threading.Lock()


def configure_shell(context: Any, config: ShellConfig) -> None:
    """Attach a ``ShellConfig`` to a session's ``ToolContext`` (as ``context.shell_config``)."""
    if not isinstance(config, ShellConfig):
        raise TypeError("config must be a ShellConfig")
    context.shell_config = config


def _config_for(context: Any) -> ShellConfig:
    cfg = getattr(context, "shell_config", None)
    if cfg is None:
        return _DEFAULT_CONFIG
    if not isinstance(cfg, ShellConfig):
        raise ToolError("the shell configuration is invalid", code="internal_error")
    return cfg


def _warn_once(context: Any, cfg: ShellConfig) -> None:
    """Give the sandbox warning to the UI the first time a command is about to start in this session.

    A failing display is logged by type and tried again with the next command; it never stops the command.
    """
    if cfg.warn is None:
        return
    try:
        with _WARN_LOCK:
            if context in _WARNED:
                return
            _WARNED.add(context)
    except TypeError:  # a context that cannot be weakly referenced: warn every time rather than never
        pass
    try:
        cfg.warn(SANDBOX_WARNING)
    except Exception as exc:  # noqa: BLE001 -- a broken display must not break the command
        try:
            with _WARN_LOCK:
                _WARNED.discard(context)
        except TypeError:
            pass
        log_event(logger, logging.WARNING, "tools.shell_warn", check="bash", exc_type=type(exc).__name__)


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------
_STRIP_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_STRIP_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GCLOUD_", "M2C_", "BASH_FUNC_")
_STRIP_NAMES = frozenset({"SSH_AUTH_SOCK", "KUBECONFIG", "DATABASE_URL", "BASH_ENV", "ENV"})
_KEEP_NAMES = frozenset({"PATH", "HOME"})
_FORCED = {"TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"}


def sanitized_env(environ: Mapping[str, str], extra: Any = ()) -> dict[str, str]:
    """The environment a command gets: ``environ`` minus credentials, plus the fixed terminal settings.

    See the module docstring for the rules.  ``extra`` is a sequence of names (exact, or ending in ``*`` for a
    prefix), matched case-insensitively.  ``PATH`` and ``HOME`` are never removed; a ``PATH`` is added if there is none.
    """
    exact, prefixes = set(), []
    for name in _normalize_names(tuple(extra)):
        if name.endswith("*"):
            prefixes.append(name.rstrip("*"))
        else:
            exact.add(name)
    prefix_tuple = _STRIP_PREFIXES + tuple(prefixes)
    out: dict[str, str] = {}
    for name, value in environ.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        upper = name.upper()
        if upper not in _KEEP_NAMES and (
            upper in _STRIP_NAMES or upper in exact or upper.startswith(prefix_tuple)
            or any(s in upper for s in _STRIP_SUBSTRINGS)
        ):
            continue
        out[name] = value
    out.update(_FORCED)
    if not out.get("PATH"):
        out["PATH"] = _DEFAULT_PATH
    return out


def _find_shell(env: Mapping[str, str]) -> str | None:
    """bash if it is on PATH, otherwise sh, otherwise the usual absolute paths; None if there is none."""
    path = env.get("PATH") or _DEFAULT_PATH
    for name in ("bash", "sh"):
        found = shutil.which(name, path=path)
        if found:
            return found
    for fallback in ("/bin/bash", "/bin/sh"):
        if os.access(fallback, os.X_OK):
            return fallback
    return None


# ---------------------------------------------------------------------------
# Terminal escape sequences and control characters
# ---------------------------------------------------------------------------
# C0 controls except tab, newline and carriage return; DEL; and the C1 controls (U+0080..U+009F, which some
# terminals treat as 8-bit escape introducers).  ESC (0x1b) is among them: it starts a sequence to remove.
_SPECIAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MAX_STRING_SEQUENCE = 4096   # an OSC/DCS/APC/PM/SOS string longer than this is given up, not allowed to eat the output
_MAX_CSI_LENGTH = 64

_NORMAL, _ESC, _ESC_INTER, _CSI, _STRING, _STRING_ESC = range(6)


class TerminalSanitizer:
    """Remove terminal escape sequences and control characters from a stream of text, across chunk boundaries.

    What is kept: everything except the C0 controls other than newline, carriage return and tab, DEL and the C1
    controls.  What is removed: those characters, and complete escape sequences with their payload: CSI (ESC [ ...
    final byte), OSC / DCS / SOS / PM / APC strings (up to BEL or ST), and the two- and three-character ``ESC x`` /
    ``ESC ( B`` forms.  A sequence cut by the end of a chunk is continued by the next ``feed``.  A sequence that
    never ends is given up after a bounded length so it cannot hide the rest of the output.  Clean text is returned
    as it came (a regex scan, no per-character work).
    """

    def __init__(self) -> None:
        self._state = _NORMAL
        self._count = 0

    def feed(self, text: str) -> str:
        if self._state == _NORMAL and not _SPECIAL.search(text):
            return text
        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            if self._state == _NORMAL:
                m = _SPECIAL.search(text, i)
                if m is None:
                    out.append(text[i:])
                    break
                if m.start() > i:
                    out.append(text[i:m.start()])
                i = m.start()
                if text[i] == "\x1b":
                    self._state = _ESC
                i += 1  # any other control character is simply dropped
                continue
            if self._step(text[i]):
                i += 1
        return "".join(out)

    def _step(self, ch: str) -> bool:
        """Advance inside a sequence.  False means "this character was not part of it: handle it again"."""
        st = self._state
        if st == _ESC:
            if ch == "[":
                self._state, self._count = _CSI, 0
            elif ch in "]PX^_":
                self._state, self._count = _STRING, 0
            elif "\x20" <= ch <= "\x2f":
                self._state = _ESC_INTER
            elif "\x30" <= ch <= "\x7e":
                self._state = _NORMAL
            elif ch != "\x1b":
                self._state = _NORMAL
                return False
            return True
        if st == _ESC_INTER:
            if "\x20" <= ch <= "\x2f":
                return True
            self._state = _NORMAL
            return "\x30" <= ch <= "\x7e"
        if st == _CSI:
            self._count += 1
            if "\x20" <= ch <= "\x3f":
                if self._count > _MAX_CSI_LENGTH:
                    self._state = _NORMAL
                return True
            self._state = _NORMAL
            return "\x40" <= ch <= "\x7e"
        if st == _STRING:
            self._count += 1
            if ch in ("\x07", "\x9c"):
                self._state = _NORMAL
            elif ch == "\x1b":
                self._state = _STRING_ESC
            elif self._count > _MAX_STRING_SEQUENCE:
                self._state = _NORMAL
                return False
            return True
        # _STRING_ESC: ESC inside a string; ESC \ is the string terminator, anything else starts a new sequence
        if ch == "\\":
            self._state = _NORMAL
            return True
        self._state = _ESC
        return False


class _StreamFilter:
    """One pipe's bytes to clean text: an incremental UTF-8 decoder (bad bytes become U+FFFD) and a sanitizer."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._sanitizer = TerminalSanitizer()

    def feed(self, data: bytes) -> str:
        return self._sanitizer.feed(self._decoder.decode(data))

    def close(self) -> str:
        return self._sanitizer.feed(self._decoder.decode(b"", final=True))


# ---------------------------------------------------------------------------
# Bounded output
# ---------------------------------------------------------------------------
class _OutputBuffer:
    """Collects the combined output of both streams in memory-bounded form: the first half of the cap, the last
    half of the cap, and a count of what lay between.

    ``add(stream, text)`` inserts a ``[stdout]`` / ``[stderr]`` line wherever the stream changes (and at the start).
    Memory is at most ``max_bytes`` for the head plus twice the tail cap (the tail is trimmed lazily), whatever is
    added.  ``result()`` gives ``(text, truncated, total_bytes)``; the text is exact UTF-8 (a cut inside a character
    drops the partial character and counts it as omitted).
    """

    def __init__(self, max_bytes: int) -> None:
        self._head_cap = max_bytes // 2
        self._tail_cap = max_bytes - self._head_cap
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._last: str | None = None
        self._at_line_start = True

    def add(self, stream: str, text: str) -> None:
        if not text:
            return
        if stream != self._last:
            text = ("" if self._at_line_start else "\n") + f"[{stream}]\n" + text
            self._last = stream
        self._at_line_start = text.endswith("\n")
        self._append(text.encode("utf-8"))

    def _append(self, data: bytes) -> None:
        self._total += len(data)
        room = self._head_cap - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if not data:
            return
        if len(data) >= self._tail_cap:
            self._tail = bytearray(data[len(data) - self._tail_cap:])
            return
        self._tail += data
        if len(self._tail) > 2 * self._tail_cap:
            del self._tail[: len(self._tail) - self._tail_cap]

    def result(self) -> tuple[str, bool, int]:
        tail = bytes(self._tail[-self._tail_cap:]) if self._tail_cap else b""
        head = bytes(self._head)
        if self._total - len(head) - len(tail) <= 0:
            return (head + tail).decode("utf-8"), False, self._total
        head_text = head.decode("utf-8", "ignore")
        tail_text = tail.decode("utf-8", "ignore")
        omitted = self._total - len(head_text.encode("utf-8")) - len(tail_text.encode("utf-8"))
        gap = "" if (not head_text or head_text.endswith("\n")) else "\n"
        return f"{head_text}{gap}[... {omitted} bytes omitted ...]\n{tail_text}", True, self._total


class _Streamer:
    """Delivers text to ``on_chunk`` in coalesced pieces, in the order it arrived.

    Text is held until it reaches ``flush_bytes`` characters or its first character is ``flush_interval`` seconds
    old; a change of stream delivers what is held first, so the callback sees stdout and stderr in arrival order.  A
    callback that raises is logged by exception type and not called again.
    """

    def __init__(self, callback: Callable[[str, str], None] | None, flush_bytes: int, flush_interval: float) -> None:
        self._callback = callback
        self._flush_bytes = flush_bytes
        self._flush_interval = flush_interval
        self._pending: list[str] = []
        self._stream = ""
        self._size = 0
        self._since = 0.0
        self._dead = False

    def feed(self, stream: str, text: str, now: float) -> None:
        if self._callback is None or self._dead or not text:
            return
        if self._pending and stream != self._stream:
            self.flush()
        if not self._pending:
            self._since, self._stream = now, stream
        self._pending.append(text)
        self._size += len(text)
        if self._size >= self._flush_bytes:
            self.flush()

    def due_in(self, now: float) -> float | None:
        """Seconds until held text must be delivered, or None when nothing is held."""
        return max(0.0, self._since + self._flush_interval - now) if self._pending else None

    def flush_if_due(self, now: float) -> None:
        if self._pending and now - self._since >= self._flush_interval:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        text, stream = "".join(self._pending), self._stream
        self._pending, self._size = [], 0
        if self._dead or self._callback is None:
            return
        try:
            self._callback(stream, text)
        except Exception as exc:  # noqa: BLE001 -- a broken display must not break the command
            self._dead = True
            log_event(logger, logging.WARNING, "tools.shell_callback", check="bash", exc_type=type(exc).__name__)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------
def _is_cancelled(cancel: Any) -> bool:
    """True when the handle is set.  A handle that raises counts as set: the command is stopped, not run on."""
    if cancel is None:
        return False
    try:
        return bool(cancel.is_set())
    except Exception:  # noqa: BLE001
        return True


def _leader_exited(proc: subprocess.Popen) -> bool:
    """Has the shell exited?  Looks without reaping, so its zombie keeps the process-group id reserved."""
    try:
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True
    except (AttributeError, OSError):
        return proc.poll() is not None


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:  # no such group (nothing left) or not permitted: nothing more can be done
        pass


def _terminate_group(proc: subprocess.Popen, grace_s: float) -> None:
    """SIGTERM the whole group, wait for the shell to go (at most ``grace_s``), then SIGKILL whatever is left."""
    pgid = proc.pid
    _signal_group(pgid, signal.SIGTERM)
    end = time.monotonic() + grace_s
    while time.monotonic() < end and not _leader_exited(proc):
        time.sleep(0.02)
    if _leader_exited(proc):
        time.sleep(min(_AFTER_EXIT_PAUSE_S, max(0.0, end - time.monotonic())))
    _signal_group(pgid, signal.SIGKILL)


def _reap(proc: subprocess.Popen) -> int | None:
    try:
        return proc.wait(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log_event(logger, logging.WARNING, "tools.shell_reap", check="bash", status="timeout")
        return None


def _exit_status(returncode: int | None) -> tuple[int | None, str]:
    """(exit code, the text of the status line) for a command that ended by itself."""
    if returncode is None:
        return None, "[exit status: unknown]"
    if returncode >= 0:
        return returncode, f"[exit status: {returncode}]"
    return 128 - returncode, f"[exit status: {128 - returncode} (terminated by signal {-returncode})]"


def _spawn(argv: list[str], cwd: str, env: dict[str, str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, NotADirectoryError):
        raise ToolError("the project directory or the shell could not be found", code="not_found") from None
    except (OSError, ValueError):
        raise ToolError("the command could not be started", code="io_error") from None


def _execute(context: Any, command: str, timeout_s: int, cfg: ShellConfig, cancel: Any,
             on_chunk: Callable[[str, str], None] | None) -> ToolOutcome:
    root = os.fspath(context.root)
    if not os.path.isdir(root):
        raise ToolError("the project directory no longer exists", code="not_found")
    if _is_cancelled(cancel):
        return ToolOutcome("[cancelled before the command started]", False, False, SHELL_TRUST, None, "cancelled")
    env = sanitized_env(os.environ, cfg.extra_strip_names)
    shell_path = _find_shell(env)
    if shell_path is None:
        raise ToolError("no shell (bash or sh) was found", code="io_error")
    _warn_once(context, cfg)

    started = time.monotonic()
    proc = _spawn([shell_path, "-c", command], root, env)
    pgid = proc.pid  # start_new_session made the shell a group leader
    filters = {"stdout": _StreamFilter(), "stderr": _StreamFilter()}
    buf = _OutputBuffer(cfg.max_output_bytes)
    streamer = _Streamer(on_chunk, cfg.flush_bytes, cfg.flush_interval_s)
    sel = selectors.DefaultSelector()
    for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        sel.register(pipe, selectors.EVENT_READ, name)

    def pump(wait: float) -> None:
        """One round of reading whatever is ready (waiting at most ``wait`` seconds)."""
        for key, _ in sel.select(max(0.0, wait)):
            stream = key.data
            try:
                data = os.read(key.fd, READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if data:
                text = filters[stream].feed(data)
            else:
                sel.unregister(key.fileobj)
                key.fileobj.close()
                text = filters[stream].close()
            if text:
                buf.add(stream, text)
                streamer.feed(stream, text, time.monotonic())

    kind, leftovers, exit_seen = "exited", False, None
    deadline = started + timeout_s
    returncode: int | None = None
    try:
        try:
            while True:
                now = time.monotonic()
                if _is_cancelled(cancel):
                    kind = "cancelled"
                    break
                if now >= deadline:
                    kind = "timeout"
                    break
                if exit_seen is None and _leader_exited(proc):
                    exit_seen = now
                wait = min(POLL_INTERVAL_S, deadline - now)
                if exit_seen is not None:
                    if not sel.get_map():
                        break
                    if now - exit_seen >= cfg.drain_grace_s:
                        leftovers = True
                        break
                    wait = min(wait, exit_seen + cfg.drain_grace_s - now)
                due = streamer.due_in(now)
                if due is not None:
                    wait = min(wait, due)
                pump(wait)
                streamer.flush_if_due(time.monotonic())

            if kind == "exited":
                _signal_group(pgid, signal.SIGKILL)  # whatever the command left running in its group
            else:
                _terminate_group(proc, cfg.kill_grace_s)
            drain_end = time.monotonic() + _DRAIN_AFTER_KILL_S
            while sel.get_map() and time.monotonic() < drain_end:
                pump(min(POLL_INTERVAL_S, max(0.0, drain_end - time.monotonic())))
                streamer.flush_if_due(time.monotonic())
        except BaseException:
            _signal_group(pgid, signal.SIGKILL)  # never leave the command running behind an error or an interrupt
            raise
        finally:
            for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                if not pipe.closed:
                    text = filters[name].close()
                    if text:
                        buf.add(name, text)
                        streamer.feed(name, text, time.monotonic())
                    pipe.close()
            sel.close()
            streamer.flush()
    finally:
        returncode = _reap(proc)

    text, truncated, total = buf.result()
    if text and not text.endswith("\n"):
        text += "\n"
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    if kind == "timeout":
        outcome = ToolOutcome(text + f"[timed out after {timeout_s}s; the command's process group was killed]",
                              truncated, False, SHELL_TRUST, None, "timeout")
    elif kind == "cancelled":
        outcome = ToolOutcome(text + "[cancelled; the command's process group was killed]",
                              truncated, False, SHELL_TRUST, None, "cancelled")
    else:
        exit_code, status_line = _exit_status(returncode)
        if leftovers:
            text += "[processes that kept the output open after the command ended were stopped]\n"
        ok = exit_code == 0
        outcome = ToolOutcome(text + status_line, truncated, ok, SHELL_TRUST, exit_code, None if ok else "exit_nonzero")

    fields: dict[str, Any] = {"check": "bash", "status": "ok" if outcome.ok else "error"}
    if outcome.code:
        fields["code"] = outcome.code
    if outcome.exit_code is not None:
        fields["exit_code"] = outcome.exit_code
    fields["count"] = total
    fields["duration_ms"] = duration_ms
    log_event(logger, logging.DEBUG if outcome.ok else logging.INFO, "tools.shell", **fields)
    return outcome


# ---------------------------------------------------------------------------
# The entry points
# ---------------------------------------------------------------------------
def _policy_denies(command: str, policy: Any) -> bool:
    """True unless C06 answers allow or ask.  A policy that raises or answers anything else is a refusal."""
    try:
        action = decide(Request("bash", {"command": command}, "R2"), policy).action
    except Exception:  # noqa: BLE001 -- fail closed
        return True
    return action not in ("allow", "ask")


def run_command(
    context: Any,
    command: Any,
    timeout_s: Any = DEFAULT_TIMEOUT_S,
    *,
    authorization: Any = None,
    policy: Any = None,
    cancel: Any = None,
    on_chunk: Callable[[str, str], None] | None = None,
    reason: Any = None,
    config: ShellConfig | None = None,
) -> ToolOutcome:
    """Run one shell command and return its ``ToolOutcome``.  See the module docstring for everything it does.

    Raises ``ToolError`` for a refusal (``refused``: not authorized, or the policy denies it), a bad argument
    (``invalid_args``), a missing working directory (``not_found``) or a command that could not be started
    (``io_error``); a command that ran and failed, timed out or was cancelled is a ``ToolOutcome`` with ``ok`` False.
    """
    if not (isinstance(authorization, str) and authorization in AUTHORIZATIONS):
        raise ToolError("the shell tool did not run: the call was not authorized", code="refused")
    if policy is not None and _policy_denies(command if isinstance(command, str) else "", policy):
        raise ToolError("the shell tool did not run: the policy denies this command", code="refused")
    if not isinstance(command, str):
        raise ToolError("argument command must be a string", code="invalid_args")
    if "\x00" in command:
        raise ToolError("argument command must not contain a NUL byte", code="invalid_args")
    if type(timeout_s) is not int or not MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S:
        raise ToolError(f"argument timeout_s must be an integer from {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S}", code="invalid_args")
    if reason is not None and (not isinstance(reason, str) or len(reason) > REASON_MAX_CHARS):
        raise ToolError(f"argument reason must be a string of at most {REASON_MAX_CHARS} characters", code="invalid_args")
    if on_chunk is not None and not callable(on_chunk):
        raise ToolError("on_chunk must be callable", code="invalid_args")
    if cancel is not None and not callable(getattr(cancel, "is_set", None)):
        raise ToolError("cancel must have an is_set method", code="invalid_args")
    cfg = config if config is not None else _config_for(context)
    return _execute(context, command, timeout_s, cfg, cancel, on_chunk)


def bash(context: Any, command: Any, timeout_s: Any = DEFAULT_TIMEOUT_S, reason: Any = None,
         call_env: Any = None) -> ToolOutcome:
    """The executor C05's ``TOOLS`` table calls: ``bash(context, **clean_args, call_env=...)``.

    ``call_env`` is a ``CallEnv`` (absent means "not authorized": nothing runs).  Anything that is not a ``CallEnv``
    is refused rather than guessed at.
    """
    env = CallEnv() if call_env is None else call_env
    if not isinstance(env, CallEnv):
        raise ToolError("the shell tool did not run: the call environment is invalid", code="refused")
    return run_command(context, command, timeout_s, authorization=env.authorization, policy=env.policy,
                       cancel=env.cancel, on_chunk=env.on_chunk, reason=reason)
