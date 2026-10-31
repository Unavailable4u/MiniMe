"""
m2c/src/m2c/headless.py -- Patch C10: headless mode, ``m2c -p "task"``.

WHAT THIS IS.  One task, no terminal interaction: ``m2c -p "fix the failing test"`` signs in with the saved
credential, connects the device link (C04), starts a run (profile "code", the chosen mode, ``cwd_label`` = the folder's
NAME, never a path), sends the task as the user message and streams the reply.  It never prompts, it works without a
TTY (CI, cron, a pipe, another program such as Menux), and the task may come from stdin: ``m2c -p -``.  It is the
non-interactive sibling of the prompt loop in repl.py (I01); cli.py only parses the options and calls
``run_headless``.

    m2c -p "task" [--output-format text|json] [--mode plan|ask|accept-edits]
    echo "task" | m2c -p -

OUTPUT
  text (default)  the model's text goes to stdout as it streams, and NOTHING else does: progress (the usage line, notices,
                  budget warnings, which tool was allowed or denied) and errors go to stderr.  The output ends with a
                  newline.  When a run has several assistant messages (text, a tool step, more text) they are separated
                  by a line break.
  json            stdout carries exactly ONE JSON object, one line, ASCII, printed at the end, and nothing else (stderr
                  gets the same progress lines as text mode)::

                    {"ok": true, "result": "<the complete reply>", "session_id": "...", "run_id": "...",
                     "usage": {"model", "input_tokens", "cached_input_tokens", "output_tokens", "credits", "calls"},
                     "exit_code": 0, "error": null}

                  ``usage`` is null when the server sent no usage event; otherwise the numbers are summed over the
                  model calls of the run (``model`` is the last one's).  ``session_id`` is the id of the local session,
                  which is also the run id (null when no run was started: a usage error, not signed in, no
                  connection).  On failure ``ok`` is false, ``result`` holds whatever text had arrived, and ``error`` is
                  ``{"code", "message"}``; the message is fixed text and never contains the task, a reply or tool input.

EXIT CODES (``m2c --help`` prints this table; cli.py takes it from ``EXIT_CODES_HELP`` below)

    0  success
    1  general failure (the server could not answer, the connection was lost and did not come back, a run failed or
       expired, stdout was closed, an unexpected error)
    2  usage error (unknown --mode, an empty or too long task, -p with a command, stdin is a terminal)
    3  policy denial (a tool would ask, was denied, or no policy / executor is installed to allow it)
    4  quota exhausted (the shared credit pool is used up)
    5  not logged in, or this device was revoked (or the credential is not accepted)

    130 / 143   ended by Ctrl-C (SIGINT) / SIGTERM: the run is cancelled first (see SIGNALS).

The protocol's error codes (protocol/v1/common.json) map onto them in ``exit_code_for_error``: quota_exhausted is 4,
unauthorized and device_revoked are 5, every other code (and a code this client does not know) is 1.  A test fails when
the protocol gains a code, so that someone decides what a headless run does with it.

THE TURN ENDS (the server has no "reply finished" message of its own): at ``state_changed`` completed (success); at a
``usage`` event that follows a ``text_done`` whose stop_reason is not ``tool_use`` (success: the I01 server runner leaves
the run ``running``, so the run is then CANCELLED to end it and delete the server's buffer, as the prompt loop does; a run
the server completed itself is not cancelled); at ``paused_budget`` (exit 4) or ``paused_error`` (exit 1); at a terminal
``cancelled`` / ``failed`` / ``expired`` state the device did not cause (exit 1); at an ``error`` that answers the message
(exit by code).  ``paused_disconnected`` is not an end: the link reconnects and resumes by itself.  If the link cannot get
back within ``reconnect_give_up_s`` the run fails with exit 1 instead of hanging for ever.

NON-INTERACTIVE POLICY (tools)
  The server may propose a tool call (``tool_proposed``).  A headless run can never ask anybody, so the rule is: ONLY AN
  EXPLICIT ``allow`` RUNS A TOOL.  Anything that would ask, was denied, failed to decide, or cannot be decided fails the run
  as a policy denial (exit 3), and the device reports the denial to the server FIRST, as
  ``approval{call_id, decision: "deny", scope: "once"}`` and with no ``tool_result`` (P00: a denied call gets an approval and
  no tool_result); only then is the run cancelled.  An allowed call is answered with a ``tool_result`` alone (an
  auto-allowed call needs no approval message, protocol/fixtures/02_edit_with_approval.jsonl).

  THE POLICY ENGINE (C06) AND THE TOOL EXECUTORS (C05) ARE NOT ON THIS PATCH'S BASE.  They are built in parallel, and this
  module imports neither (it does not import the ``policy`` or ``tools`` packages).  The join is two small injected
  callables, installed with ``install_tool_hooks`` or passed to ``run_headless``:

      decide(proposal) -> "allow" | "ask" | "deny"      (or an object with such a ``.decision``; may be ``async``)
      execute(proposal) -> ToolOutcome                  (or a dict with the same fields; may be ``async``)

  where ``proposal`` is a ``ToolProposal`` (call_id, tool, input, risk, plus the run's mode and project path, which the
  policy needs).  THE DEFAULTS ARE CONSERVATIVE: with no policy and no executor installed, every ``tool_proposed`` from the
  server fails the run as a policy denial.  The integration patch (I02) wires the real engine and the real executors in
  with ``install_tool_hooks``; until then a headless run is a chat that cannot touch the machine.  Every way of getting an
  answer wrong is a denial: no hook, an exception, ``None``, any value that is not exactly "allow", "ask" or "deny", an
  ``allow`` with no executor.  (An executor that fails, or returns something unusable, produces a failed ``tool_result``
  for the model; that is the tool failing, not the policy.)  A synchronous executor runs in a daemon thread so a slow tool
  does not stall the link; calls are handled one at a time, in order, once per call_id, and a call the server has already
  closed with ``tool_finished`` is not run.

reply_restarted
  After a reconnect in the middle of a reply the server sends ``notice reply_restarted`` (backend/codeagent/hello.py): the
  reply is being written again from the start.  json mode DISCARDS the partial text, so the final object holds only the
  complete reply.  Text mode cannot: the partial text is already on stdout and a stream cannot be taken back, so a short
  note goes to stderr and the stdout text contains the partial reply followed by the complete one.  A consumer that needs
  exactly one reply should use ``--output-format json``.

SIGNALS.  Ctrl-C (SIGINT) and SIGTERM send ``cancel`` for the run, wait briefly for the server to confirm, close the link
and exit 130 / 143, with one line on stderr and no traceback; in json mode the one JSON object is still printed.  A second
signal while cleaning up skips the wait.  The handlers are installed only on the main thread and the process's earlier
handlers are put back afterwards (``run_headless`` is a function other code may call).

CONTENT HYGIENE (build plan 11.4).  Nothing here writes the task, a reply, a tool's input or output, the credential or
a token to a log line, and error texts never contain them; the user's own output on stdout is not a log.  Log lines go
through ``log_event`` with allowlisted keys only (codes, exit codes, run id, counts).  Security decisions fail closed
(the tool policy above, credentials); metering is the server's, and nothing here counts credits except to show them.

NOT IN THIS PATCH: ``--resume`` / ``--continue``, ``--max-usd``, ``--model`` (Appendix E lists them for later patches), reading
settings, instructions or memory (C12), checkpoints, the sandbox.  Interrupted runs are not resumed by a new process.

Place this file at: m2c/src/m2c/headless.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable

from .logging_setup import log_event

logger = logging.getLogger("m2c.headless")

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_POLICY_DENIED = 3
EXIT_QUOTA_EXHAUSTED = 4
EXIT_NOT_LOGGED_IN = 5
EXIT_SIGINT = 130   # the shell convention for "ended by Ctrl-C"
EXIT_SIGTERM = 143  # 128 + 15

# One table, two readers: ``m2c --help`` (cli.py) prints exactly this, and the module docstring above says the same
# (tests/headless/test_headless_process.py compares them).
EXIT_CODES_HELP = """\
  0  success
  1  general failure
  2  usage error
  3  policy denial (a tool would ask, or was denied)
  4  quota exhausted
  5  not logged in, or this device was revoked
"""

# ---------------------------------------------------------------------------
# Modes and limits
# ---------------------------------------------------------------------------
# The three permission modes a run may start in.  They are the enum of ``mode`` in protocol/v1/common.json (the run.start
# schema), and tests/headless fails if the two drift apart.  C06's policy/modes.py is the long-term source of this list;
# until it lands on main, this is the one place headless mode defines them.
MODES = ("plan", "ask", "accept-edits")
DEFAULT_MODE = "ask"
OUTPUT_FORMATS = ("text", "json")

MAX_TASK_CHARS = 200_000          # common.json bulk_text; the server's welcome may announce a lower message limit
DEFAULT_CONNECT_TIMEOUT_S = 15.0
DEFAULT_SETTLE_S = 1.0            # how long to wait for the state_changed that follows quota_exhausted / provider_unavailable
DEFAULT_CANCEL_WAIT_S = 3.0       # how long to wait for the server to confirm our cancel
DEFAULT_RECONNECT_GIVE_UP_S = 120.0

RESTART_NOTICE_CODE = "reply_restarted"
_TRUSTS = ("local_project", "local_other", "external")      # common.json trust
_VERDICTS = ("allow", "ask", "deny")
_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "expired"})

# Fixed texts for the protocol's error codes: never the server's own message.
_ERROR_TEXT = {
    "quota_exhausted": "Your credit pool is used up for now.",
    "provider_unavailable": "No model could answer right now. Try again in a moment.",
    "rate_limited": "Too many requests. Wait a moment and try again.",
    "run_not_found": "The server no longer knows this run.",
    "invalid_message": "The server could not take the message.",
    "tool_timeout": "A tool call timed out on the server side.",
    "unauthorized": "The server did not accept this device's credential. Run 'm2c logout' and then 'm2c login'.",
    "device_revoked": "This device was revoked. Run 'm2c login' to sign in again.",
    "protocol_unsupported": "This server and this version of m2c share no protocol version. Update m2c and try again.",
    "internal": "Something went wrong on the server.",
}
_RUN_ENDED_TEXT = {
    "cancelled": "The run was cancelled on the server.",
    "failed": "The run failed on the server.",
    "expired": "The run expired on the server.",
}
_NOT_LOGGED_IN_TEXT = "Not signed in. Run `m2c login` first."
_UNREACHABLE_TEXT = "Could not reach the server. Check your connection and try again."

_RESET_AT_RE = re.compile(r"^[0-9T:.+\-Z]{10,40}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def exit_code_for_error(code: str) -> int:
    """The exit status for a protocol error code (see the module docstring)."""
    if code == "quota_exhausted":
        return EXIT_QUOTA_EXHAUSTED
    if code in ("unauthorized", "device_revoked"):
        return EXIT_NOT_LOGGED_IN
    return EXIT_FAILURE


# ---------------------------------------------------------------------------
# Public value types and the injected tool hooks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HeadlessOptions:
    """What the command line asked for.  ``task`` is the text, or ``"-"`` for "read it from stdin"."""

    task: str
    mode: str = DEFAULT_MODE
    output_format: str = "text"


@dataclass(frozen=True)
class ToolProposal:
    """One ``tool_proposed`` as handed to the injected hooks: the protocol's fields, plus the run's mode and project
    path (what a policy engine needs to decide; neither is sent anywhere by this module)."""

    call_id: str
    tool: str
    input: dict
    risk: str
    mode: str = DEFAULT_MODE
    project_path: str = ""


@dataclass(frozen=True)
class ToolOutcome:
    """What an executor returns; the fields of a ``tool_result`` (protocol/v1/tool_result.json).  The executor is
    responsible for redacting and truncating ``output`` (C05)."""

    ok: bool
    output: str
    truncated: bool = False
    trust: str = "local_project"
    exit_code: int | None = None
    duration_ms: int | None = None


_HOOKS: dict[str, Callable[..., Any] | None] = {"decide": None, "execute": None}


def install_tool_hooks(*, decide: Callable[[ToolProposal], Any] | None = None,
                       execute: Callable[[ToolProposal], Any] | None = None) -> None:
    """Install the policy and the executor a headless run uses when ``run_headless`` is not given its own.  The
    integration patch (I02) calls this once with C06's engine and C05's executors; ``None`` means "not installed", and the
    defaults then deny every tool call (fail closed)."""
    _HOOKS["decide"] = decide
    _HOOKS["execute"] = execute


def clear_tool_hooks() -> None:
    """Back to the conservative defaults: no policy, no executor."""
    _HOOKS["decide"] = None
    _HOOKS["execute"] = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def cwd_label_for(project_path: str) -> str:
    """The run's ``cwd_label``: the folder's NAME, never a path (common.json forbids separators and control characters).
    (repl.py has the same rule; it is repeated here because C09 replaces repl.py and this module must not depend on it.)"""
    name = re.sub(r"[/\\\x00-\x1f]", "", Path(project_path).name)[:128]
    return name if name and name not in (".", "..") else "project"


def _one_line(text: Any, limit: int = 300) -> str:
    return _CONTROL_RE.sub(" ", str(text))[:limit]


def _safe_tool_name(name: Any) -> str:
    return name if isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name) else "<invalid name>"


def _error_text(code: str, details: Mapping[str, Any] | None = None) -> str:
    text = _ERROR_TEXT.get(code, _ERROR_TEXT["internal"])
    reset = (details or {}).get("reset_at") if code == "quota_exhausted" else None
    if isinstance(reset, str) and _RESET_AT_RE.fullmatch(reset):
        text += f" It resets at {reset}."
    return text


@dataclass(frozen=True)
class _Failure:
    exit_code: int
    code: str
    message: str


def _coerce_outcome(raw: Any) -> ToolOutcome | None:
    """An executor's result as a validated ``ToolOutcome``, or None when it is unusable (the caller then reports a failed
    tool_result).  Accepts a ``ToolOutcome`` or a mapping with the same keys; ``ok`` and ``output`` are required."""
    if isinstance(raw, ToolOutcome):
        outcome = raw
    elif isinstance(raw, Mapping):
        if "ok" not in raw or "output" not in raw:
            return None
        known = {"ok", "output", "truncated", "trust", "exit_code", "duration_ms"}
        outcome = ToolOutcome(**{key: value for key, value in raw.items() if key in known})
    else:
        return None
    if (type(outcome.ok) is not bool or not isinstance(outcome.output, str) or type(outcome.truncated) is not bool
            or outcome.trust not in _TRUSTS):
        return None
    for number in (outcome.exit_code, outcome.duration_ms):
        if number is not None and (type(number) is not int):
            return None
    if outcome.duration_ms is not None and outcome.duration_ms < 0:
        return None
    return outcome


def _in_daemon_thread(loop: asyncio.AbstractEventLoop, fn: Callable[[], Any]) -> "asyncio.Future[Any]":
    """Run ``fn`` in a daemon thread and return a future for its result.  A daemon thread, not ``to_thread``: when the
    run is cancelled by a signal the process must exit now, not wait for a slow tool in the default executor."""
    future: "asyncio.Future[Any]" = loop.create_future()

    def deliver(value: Any, exc: BaseException | None) -> None:
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(value)

    def work() -> None:
        try:
            value, error = fn(), None
        except Exception as exc:  # noqa: BLE001 - delivered to the loop, which reports its type only
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:  # the loop is already closed: the run is over, nobody is waiting
            pass

    threading.Thread(target=work, name="m2c-tool", daemon=True).start()
    return future


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class _HeadlessRun:
    """Everything one headless run needs.  One instance per ``run_headless`` call; not reusable."""

    def __init__(self, options: HeadlessOptions, *, stdout: IO[str], stderr: IO[str], stdin: IO[str] | None,
                 decide: Callable[..., Any] | None, execute: Callable[..., Any] | None, project_path: str,
                 settle_s: float, cancel_wait_s: float, reconnect_give_up_s: float) -> None:
        self.options = options
        self.json_mode = options.output_format == "json"
        self._stdout, self._stderr, self._stdin = stdout, stderr, stdin
        self._decide_hook, self._execute_hook = decide, execute
        self.project_path = project_path
        self.settle_s, self.cancel_wait_s, self.reconnect_give_up_s = settle_s, cancel_wait_s, reconnect_give_up_s

        self.failure: _Failure | None = None
        self.run_id: str | None = None
        self.task = ""

        # the reply
        self._done_text = ""            # complete assistant messages so far (joined)
        self._cur: list[str] = []       # deltas of the message being written
        self._cur_started = False       # the separator before this message has been written
        self._wrote_any = False
        self._last_char = ""
        self._stdout_dead = False
        self._usage: dict[str, Any] | None = None
        self._restarts = 0

        # the turn
        self._tools_expected = False    # the last text_done said tool_use
        self._seen_text_done = False
        self._finished = False          # the outcome is decided (success or failure); later events are ignored
        self._terminal_seen = False
        self._cancelling = False
        self._run_unknown = False
        self._hurry_signals = 0

        # asyncio objects, created inside the loop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._done: asyncio.Event | None = None
        self._ended: asyncio.Event | None = None
        self._hurry: asyncio.Event | None = None
        self._link: Any = None
        self._store: Any = None
        self._tool_queue: "asyncio.Queue[dict] | None" = None
        self._worker: "asyncio.Task[Any] | None" = None
        self._give_up_handle: asyncio.TimerHandle | None = None
        self._seen_calls: set[str] = set()
        self._closed_calls: set[str] = set()
        self._run_started = False
        self._signal_handlers: list[int] = []

    # -- output -----------------------------------------------------------------------------------------------------
    def _note(self, text: str, *, prefix: str = "[m2c] ") -> None:
        """Progress and errors: stderr only."""
        try:
            self._stderr.write(f"{prefix}{_one_line(text)}\n")
            self._stderr.flush()
        except (OSError, ValueError):
            pass  # a closed stderr must never stop the run

    def _write_out(self, text: str) -> None:
        """The model's text: stdout only.  A closed stdout fails the run (exit 1) and the run is cancelled, so that nobody
        keeps spending credits on an answer no one can read."""
        if self._stdout_dead or not text:
            return
        try:
            try:
                self._stdout.write(text)
            except UnicodeEncodeError:
                encoding = getattr(self._stdout, "encoding", None) or "ascii"
                self._stdout.write(text.encode(encoding, "backslashreplace").decode(encoding))
            self._stdout.flush()
        except (OSError, ValueError):                          # BrokenPipeError is an OSError; a closed file a ValueError
            self._stdout_dead = True
            self._silence_stdout()
            self._fail(EXIT_FAILURE, "stdout_closed", "Could not write to stdout (it was closed).")
            return
        self._wrote_any = True
        self._last_char = text[-1]

    def _silence_stdout(self) -> None:
        """After a broken pipe Python would print "Exception ignored ... BrokenPipeError" when it flushes stdout at exit
        (the recipe in the Python documentation): point the real stdout at /dev/null."""
        if self._stdout is not sys.stdout:
            return
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass

    def _assemble(self) -> str:
        current = "".join(self._cur)
        if not self._done_text:
            return current
        if not current:
            return self._done_text
        return self._done_text + ("" if self._done_text.endswith("\n") else "\n") + current

    # -- the outcome ------------------------------------------------------------------------------------------------
    def _fail(self, exit_code: int, code: str, message: str, *, settle: bool = False) -> None:
        """Record the first failure and end the wait.  ``settle``: the server follows this error with the state change that
        parks the run; wait a moment for it so the local log gets it too."""
        if self._finished and self.failure is None:
            return                                              # the run already succeeded: stay successful
        if self.failure is None:
            self.failure = _Failure(exit_code, code, message)
            log_event(logger, logging.INFO, "headless.failed", code=code, exit_code=exit_code)
        if settle and self.settle_s > 0 and not self._finished and self._loop is not None:
            self._loop.call_later(self.settle_s, self._finish)
        else:
            self._finish()

    def _finish(self) -> None:
        self._finished = True
        if self._done is not None:
            self._done.set()

    # -- callbacks from the link (synchronous, on the event loop) ---------------------------------------------------------
    def _on_event(self, event: Any) -> None:
        try:
            self._handle_event(event)
        except Exception as exc:  # noqa: BLE001 - the link swallows callback errors; a bug here must not become a hang
            self._fail(EXIT_FAILURE, "internal_error", f"Unexpected error ({type(exc).__name__}).")

    def _handle_event(self, event: Any) -> None:
        if event.run_id != self.run_id:
            return
        kind = event.subtype
        payload = event.payload
        if kind == "state_changed":
            self._on_state_changed(payload.get("state"))
            return
        if self._finished:
            return                                              # late events of a decided run are not shown
        if kind == "text_delta":
            self._on_text(payload.get("text", ""))
        elif kind == "text_done":
            self._on_text_done(payload.get("stop_reason"))
        elif kind == "usage":
            self._on_usage(payload)
        elif kind == "notice":
            self._on_notice(payload)
        elif kind == "tool_finished":
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                self._closed_calls.add(call_id)
        elif event.type == "tool_proposed":
            if self._tool_queue is not None:
                self._tool_queue.put_nowait(dict(payload))
        elif event.type == "budget_warning":
            self._note(f"{payload.get('spent_pct')}% of your credit pool is used")

    def _on_text(self, text: str) -> None:
        if not text:
            return
        if not self._cur_started:
            self._cur_started = True
            if self._done_text and not self.json_mode and not self._done_text.endswith("\n") and self._last_char != "\n":
                self._write_out("\n")                           # a line break between two assistant messages
        self._cur.append(text)
        if not self.json_mode:
            self._write_out(text)

    def _on_text_done(self, stop_reason: Any) -> None:
        message = "".join(self._cur)
        self._done_text = self._assemble()
        self._cur, self._cur_started = [], False
        self._seen_text_done = True
        self._tools_expected = stop_reason == "tool_use"
        if message:
            self._store_event("assistant.message", {"text": message})
        if stop_reason == "max_tokens":
            self._note("the reply was cut off (the model reached its output limit)")

    def _on_usage(self, payload: Mapping[str, Any]) -> None:
        use = self._usage or {"model": None, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                              "credits": 0.0, "calls": 0}
        use["model"] = payload.get("model", use["model"])
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            use[key] += int(payload.get(key, 0) or 0)
        use["credits"] = round(use["credits"] + float(payload.get("credits", 0) or 0), 6)
        use["calls"] += 1
        self._usage = use
        self._note(f"model {_one_line(payload.get('model'), 80)} | in {payload.get('input_tokens')} "
                   f"(cached {payload.get('cached_input_tokens')}) | out {payload.get('output_tokens')} "
                   f"| {payload.get('credits')} credits")
        if self._seen_text_done and not self._tools_expected:
            self._finish()                                      # the last event of a turn (see "THE TURN ENDS")

    def _on_notice(self, payload: Mapping[str, Any]) -> None:
        if payload.get("code") == RESTART_NOTICE_CODE:
            self._restarts += 1
            self._cur = []                                      # the partial reply is discarded (it stays in the local log)
            if self.json_mode:
                self._note("the connection dropped mid-reply and the reply is being written again; "
                           "the partial text was discarded")
            else:
                self._note("the connection dropped mid-reply and the reply is being written again; "
                           "the partial text already on stdout cannot be taken back (use --output-format json "
                           "for exactly one reply)")
            return
        label = "warning: " if payload.get("severity") == "warning" else ""
        self._note(f"{label}{payload.get('message', '')}")

    def _on_state_changed(self, state: Any) -> None:
        if state in _TERMINAL_STATES:
            self._terminal_seen = True
            if self._ended is not None:
                self._ended.set()
        if self._finished:
            return
        if state == "completed":
            self._finish()
        elif state == "paused_budget":
            self._fail(EXIT_QUOTA_EXHAUSTED, "quota_exhausted", _error_text("quota_exhausted"))
        elif state == "paused_error":
            self._fail(EXIT_FAILURE, "provider_unavailable", _error_text("provider_unavailable"))
        elif state in ("cancelled", "failed", "expired") and not self._cancelling:
            self._fail(EXIT_FAILURE, f"run_{state}", _RUN_ENDED_TEXT[state])

    def _on_error(self, error: Any) -> None:
        try:
            if (error.run_id is not None and error.run_id != self.run_id) or self._finished:
                return
            code = error.code
            if code == "tool_timeout":
                self._note(_error_text(code))                   # the call is closed by tool_finished; the run goes on
                return
            if code == "run_not_found":
                self._run_unknown = True
            self._fail(exit_code_for_error(code), code, _error_text(code, error.details),
                       settle=code in ("quota_exhausted", "provider_unavailable"))
        except Exception as exc:  # noqa: BLE001
            self._fail(EXIT_FAILURE, "internal_error", f"Unexpected error ({type(exc).__name__}).")

    def _on_state(self, state: str) -> None:
        try:
            if state == "reconnecting" and self._run_started and self._give_up_handle is None and self._loop is not None:
                self._give_up_handle = self._loop.call_later(self.reconnect_give_up_s, self._give_up)
            elif state == "connected":
                self._cancel_give_up()
            elif state in ("failed", "closed") and not self._finished:
                failure = getattr(self._link, "failure", None)
                if failure is not None:
                    self._fail_from_exception(failure)
                else:
                    self._fail(EXIT_FAILURE, "unreachable", _UNREACHABLE_TEXT)
            if state in ("failed", "closed") and self._ended is not None:
                self._ended.set()
        except Exception as exc:  # noqa: BLE001
            self._fail(EXIT_FAILURE, "internal_error", f"Unexpected error ({type(exc).__name__}).")

    def _give_up(self) -> None:
        self._give_up_handle = None
        if not self._finished:
            self._fail(EXIT_FAILURE, "unreachable", "The connection to the server was lost and did not come back.")

    def _cancel_give_up(self) -> None:
        if self._give_up_handle is not None:
            self._give_up_handle.cancel()
            self._give_up_handle = None

    # -- exceptions -> failures -------------------------------------------------------------------------------------
    def _fail_from_exception(self, exc: BaseException) -> None:
        """Map what the credential store, the link or the server raised onto an exit status.  Messages are the library's
        own fixed texts (they never contain content) or ours; a bug shows only its exception type."""
        from .auth.credentials import AuthError, NotLoggedInError
        from .link import (LinkDeviceRevokedError, LinkMessageTooLargeError, LinkProtocolUnsupportedError,
                           LinkRateLimitedError, LinkRequestError, LinkRequestTimeout, LinkStoreError,
                           LinkSupersededError, LinkUnauthorizedError, LinkError, LinkConnectionError)

        if isinstance(exc, LinkDeviceRevokedError):
            self._fail(EXIT_NOT_LOGGED_IN, "device_revoked", str(exc))
        elif isinstance(exc, LinkUnauthorizedError):
            self._fail(EXIT_NOT_LOGGED_IN, "unauthorized", str(exc))
        elif isinstance(exc, NotLoggedInError):
            self._fail(EXIT_NOT_LOGGED_IN, "not_logged_in", _NOT_LOGGED_IN_TEXT)
        elif isinstance(exc, AuthError):
            self._fail(EXIT_NOT_LOGGED_IN, "credential_unusable", str(exc))
        elif isinstance(exc, LinkMessageTooLargeError):
            self._fail(EXIT_USAGE, "usage", str(exc))
        elif isinstance(exc, LinkProtocolUnsupportedError):
            self._fail(EXIT_FAILURE, "protocol_unsupported", str(exc))
        elif isinstance(exc, LinkSupersededError):
            self._fail(EXIT_FAILURE, "superseded", str(exc))
        elif isinstance(exc, LinkStoreError):
            self._fail(EXIT_FAILURE, "store_error", str(exc))
        elif isinstance(exc, LinkRateLimitedError):
            self._fail(EXIT_FAILURE, "rate_limited", _error_text("rate_limited"))
        elif isinstance(exc, LinkRequestError):
            code = exc.code or "internal"
            self._fail(exit_code_for_error(code), code, _error_text(code))
        elif isinstance(exc, (LinkRequestTimeout, TimeoutError, LinkConnectionError)):
            self._fail(EXIT_FAILURE, "unreachable", _UNREACHABLE_TEXT)
        elif isinstance(exc, LinkError):
            self._fail(EXIT_FAILURE, "link_error", str(exc))
        else:
            self._fail(EXIT_FAILURE, "internal_error", f"Unexpected error ({type(exc).__name__}).")

    # -- the local log -----------------------------------------------------------------------------------------------
    def _store_event(self, event_type: str, data: dict) -> None:
        """The device log is the record: the task and each complete reply.  Fails open (a log that cannot be written must
        not kill the run; the link itself stops if it cannot store a server event)."""
        if self._store is None or self.run_id is None:
            return
        try:
            from .store import append_event

            append_event(self._store, self.run_id, event_type, data)
        except Exception:  # noqa: BLE001
            log_event(logger, logging.WARNING, "headless.log_write_failed", run_id=self.run_id)

    # -- tools --------------------------------------------------------------------------------------------------------
    async def _tool_worker(self) -> None:
        assert self._tool_queue is not None
        while True:
            payload = await self._tool_queue.get()
            try:
                await self._handle_tool(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._fail(EXIT_FAILURE, "internal_error", f"Unexpected error ({type(exc).__name__}).")

    async def _handle_tool(self, payload: Mapping[str, Any]) -> None:
        call_id = payload.get("call_id")
        if (not isinstance(call_id, str) or call_id in self._seen_calls or call_id in self._closed_calls
                or self._finished):
            return                                              # a re-delivery, a call the server already closed, or too late
        self._seen_calls.add(call_id)
        tool = payload.get("tool") if isinstance(payload.get("tool"), str) else ""
        proposal = ToolProposal(call_id=call_id, tool=tool, input=dict(payload.get("input") or {}),
                                risk=str(payload.get("risk", "")), mode=self.options.mode,
                                project_path=self.project_path)
        verdict, reason = await self._ask_policy(proposal)
        if verdict == "allow" and self._execute_hook is None:
            verdict, reason = "deny", "no_executor"
        if verdict != "allow":
            await self._deny(proposal, "ask" if verdict == "ask" else reason)
            return
        self._note(f"tool {_safe_tool_name(tool)} ({proposal.risk}): allowed")
        await self._run_tool(proposal)

    async def _ask_policy(self, proposal: ToolProposal) -> tuple[str, str]:
        """``(verdict, reason)``.  Only an exact "allow" is allow; every irregularity is a deny (fail closed)."""
        hook = self._decide_hook
        if hook is None:
            return "deny", "no_policy"
        try:
            answer = hook(proposal)
            if inspect.isawaitable(answer):
                answer = await answer
            verdict = answer if isinstance(answer, str) else getattr(answer, "decision", None)
        except Exception:  # noqa: BLE001 - the exception's text could hold anything; it is not shown
            return "deny", "policy_error"
        if isinstance(verdict, str) and verdict in _VERDICTS:
            return verdict, "denied" if verdict == "deny" else ""
        return "deny", "policy_malformed"

    async def _deny(self, proposal: ToolProposal, reason: str) -> None:
        """Report the denial to the server FIRST (approval deny/once, and no tool_result: P00), then fail the run."""
        name = _safe_tool_name(proposal.tool)
        message = {
            "no_policy": f"Tool {name!r} was refused: no tool policy is installed, and headless mode only runs a tool "
                         "that an explicit allow rule permits.",
            "ask": f"Tool {name!r} would need approval, and headless mode never asks. Add an allow rule for it, "
                   "or run m2c interactively.",
            "denied": f"The policy denies tool {name!r}.",
            "policy_error": f"The policy failed while deciding on tool {name!r}; treated as a denial.",
            "policy_malformed": f"The policy gave no usable answer for tool {name!r}; treated as a denial.",
            "no_executor": f"Tool {name!r} was allowed, but no tool executor is installed, so it was refused.",
        }.get(reason, f"Tool {name!r} was refused by the headless policy.")
        self._note(f"tool {name} ({proposal.risk}): denied")
        try:
            await self._link.send_approval(self.run_id, proposal.call_id, decision="deny", scope="once")
        except Exception:  # noqa: BLE001 - best effort: the run fails either way, and a dead link is reported by its state
            log_event(logger, logging.WARNING, "headless.denial_not_sent", run_id=self.run_id)
        self._fail(EXIT_POLICY_DENIED, "policy_denied", message)

    async def _run_tool(self, proposal: ToolProposal) -> None:
        try:
            raw = await self._call_executor(proposal)
            outcome = _coerce_outcome(raw)
            if outcome is None:
                outcome = ToolOutcome(False, "The tool returned an unusable result.")
        except Exception as exc:  # noqa: BLE001 - the tool failing is the model's business; only the type is reported
            outcome = ToolOutcome(False, f"The tool failed ({type(exc).__name__}).")
        if self._finished:
            return
        await self._send_result(proposal, outcome)

    async def _call_executor(self, proposal: ToolProposal) -> Any:
        hook = self._execute_hook
        assert hook is not None and self._loop is not None
        if inspect.iscoroutinefunction(hook):
            return await hook(proposal)
        result = await _in_daemon_thread(self._loop, lambda: hook(proposal))
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _send_result(self, proposal: ToolProposal, outcome: ToolOutcome) -> None:
        from .link import LinkError, LinkMessageTooLargeError

        link, run_id = self._link, self.run_id
        try:
            try:
                await link.send_tool_result(run_id, proposal.call_id, ok=outcome.ok, output=outcome.output,
                                            truncated=outcome.truncated, trust=outcome.trust,
                                            exit_code=outcome.exit_code, duration_ms=outcome.duration_ms)
            except (ValueError, LinkMessageTooLargeError):
                await link.send_tool_result(run_id, proposal.call_id, ok=False, truncated=True, trust=outcome.trust,
                                            output="The tool output could not be sent (too large, or not text).")
        except LinkError:
            return                                              # the link is down; its state callback decides what that means

    # -- signals ----------------------------------------------------------------------------------------------------
    def _hook_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        for signum, name, code in ((signal.SIGINT, "SIGINT", EXIT_SIGINT), (signal.SIGTERM, "SIGTERM", EXIT_SIGTERM)):
            try:
                loop.add_signal_handler(signum, self._on_signal, name, code)
                self._signal_handlers.append(signum)
            except (NotImplementedError, RuntimeError, ValueError):
                pass                                            # not the main thread: the signal keeps its default

    def _unhook_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        for signum in self._signal_handlers:
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        self._signal_handlers = []

    def _on_signal(self, name: str, code: int) -> None:
        self._hurry_signals += 1
        if self._hurry_signals > 1 and self._hurry is not None:
            self._hurry.set()                                   # a second signal: stop waiting for the server
        if self._finished:
            return                                              # already decided: just clean up
        self._fail(code, "cancelled", f"Cancelled by {name}.")

    # -- the async part ----------------------------------------------------------------------------------------------
    async def _race(self, awaitable: Any) -> tuple[bool, Any]:
        """Await ``awaitable``, but give up when the outcome is decided meanwhile (a signal, a failure).  Returns
        ``(True, value)`` or ``(False, None)``; the awaitable's own exception propagates."""
        assert self._done is not None
        task = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self._done.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return True, task.result()
            return False, None
        finally:
            stop.cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def run(self, store: Any, link_factory: Callable[..., Any], link_options: dict, connect_timeout_s: float) -> None:
        """Connect, run the one task, clean up.  Never raises for a failure of the run: it is recorded in ``failure``."""
        from .link import LinkCallbacks
        from .store import create_session, new_session_id

        self._loop = asyncio.get_running_loop()
        self._done, self._ended, self._hurry = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self._tool_queue = asyncio.Queue()
        self._store = store
        self._hook_signals(self._loop)
        link: Any = None
        try:
            link = link_factory(store, callbacks=LinkCallbacks(on_event=self._on_event, on_error=self._on_error,
                                                                on_state=self._on_state), **link_options)
            self._link = link
            link.start()
            try:
                finished, _ = await self._race(link.wait_connected(connect_timeout_s))
            except Exception as exc:  # noqa: BLE001 - mapped, never printed raw
                self._fail_from_exception(exc)
                return
            if not finished:
                return
            run_id = new_session_id()
            create_session(store, project_path=self.project_path, mode=self.options.mode, model_pref="auto",
                           session_id=run_id)
            self.run_id = run_id                                # known before the first event, which start_run waits for
            self._worker = asyncio.ensure_future(self._tool_worker())
            try:
                finished, _ = await self._race(link.start_run(
                    run_id=run_id, mode=self.options.mode, cwd_label=cwd_label_for(self.project_path),
                    instructions="", profile="code"))
            except Exception as exc:  # noqa: BLE001
                self.run_id = None                              # the server never accepted this run
                self._fail_from_exception(exc)
                return
            if not finished:
                return                                          # a signal during the start: cancelled below
            self._run_started = True
            self._store_event("user.message", {"text": self.task})
            if not await self._send_task(link):
                return
            await self._done.wait()
        finally:
            await self._cleanup(link)
            self._unhook_signals(self._loop)

    async def _send_task(self, link: Any) -> bool:
        """Send the task; one retry after a reconnect.  False when the run failed or ended meanwhile."""
        from .link import LinkNotConnectedError

        for attempt in (1, 2):
            try:
                finished, _ = await self._race(link.send_user_message(self.run_id, self.task))
                return finished
            except LinkNotConnectedError as exc:
                if attempt == 2:
                    self._fail_from_exception(exc)
                    return False
                try:
                    await self._race(link.wait_connected(self.reconnect_give_up_s))
                except Exception as inner:  # noqa: BLE001
                    self._fail_from_exception(inner)
                    return False
            except ValueError:
                self._fail(EXIT_USAGE, "usage", "The task contains text that cannot be sent.")
                return False
            except Exception as exc:  # noqa: BLE001 - LinkMessageTooLargeError and the rest, mapped
                self._fail_from_exception(exc)
                return False
        return False

    async def _cleanup(self, link: Any) -> None:
        """End the run on the server (cancel, wait briefly for the confirmation), stop the worker, close the link."""
        from .link import LinkError

        assert self._ended is not None and self._hurry is not None
        self._cancel_give_up()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if (link is not None and self.run_id is not None and not self._terminal_seen
                and not self._run_unknown and getattr(link, "state", None) == "connected"):
            self._cancelling = True
            try:
                await link.send_cancel(self.run_id)
            except (LinkError, ValueError):
                pass
            else:
                waiter = asyncio.ensure_future(self._ended.wait())
                hurry = asyncio.ensure_future(self._hurry.wait())
                try:
                    await asyncio.wait({waiter, hurry}, timeout=self.cancel_wait_s, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                    hurry.cancel()
        try:
            if link is not None:
                await link.aclose()
        except Exception:  # noqa: BLE001 - nothing left to report; the outcome is already decided
            pass

    # -- the synchronous shell ---------------------------------------------------------------------------------------
    def _resolve_task(self) -> str | None:
        """The task text, or None after recording a usage error."""
        raw = self.options.task
        if raw == "-":
            stdin = self._stdin if self._stdin is not None else sys.stdin
            try:
                if stdin is None or stdin.isatty():
                    self._fail(EXIT_USAGE, "usage", "-p - reads the task from stdin, but stdin is a terminal. "
                               "Pipe the task in, or pass it as the argument of -p.")
                    return None
                raw = stdin.read(MAX_TASK_CHARS + 1)
            except (UnicodeDecodeError, OSError, ValueError):
                self._fail(EXIT_USAGE, "usage", "The task on stdin could not be read as text.")
                return None
        task = raw.rstrip()
        if not task.strip():
            self._fail(EXIT_USAGE, "usage", "The task is empty.")
            return None
        if len(task) > MAX_TASK_CHARS:
            self._fail(EXIT_USAGE, "usage", f"The task is too long (the limit is {MAX_TASK_CHARS} characters).")
            return None
        return task

    def _validate(self) -> bool:
        options = self.options
        if options.output_format not in OUTPUT_FORMATS:
            self._fail(EXIT_USAGE, "usage", f"--output-format must be one of: {', '.join(OUTPUT_FORMATS)}.")
            return False
        if options.mode not in MODES:
            self._fail(EXIT_USAGE, "usage", f"--mode must be one of: {', '.join(MODES)}.")
            return False
        return True

    def execute(self, credential_loader: Callable[[], Any] | None, link_factory: Callable[..., Any] | None,
                link_options: dict | None, connect_timeout_s: float) -> int:
        """The whole run, synchronously; returns the exit status.  Order: usage checks, the task, the credential (so that
        "not signed in" never touches the network or the store), the store, the run, the output."""
        started = time.monotonic()
        saved = self._save_signals()
        try:
            if self._validate():
                task = self._resolve_task()
                if task is not None:
                    self.task = task
                    self._run(credential_loader, link_factory, link_options, connect_timeout_s)
        except KeyboardInterrupt:                               # no signal handler could be installed (not the main thread)
            self._fail(EXIT_SIGINT, "cancelled", "Cancelled by SIGINT.")
        except Exception as exc:  # noqa: BLE001 - one line with the type; a traceback prints local variables
            self._fail(EXIT_FAILURE, "internal_error", f"Unexpected error ({type(exc).__name__}).")
        finally:
            self._restore_signals(saved)
        code = self._emit()
        log_event(logger, logging.INFO, "headless.finished", exit_code=code, duration_ms=int((time.monotonic() - started) * 1000))
        return code

    def _run(self, credential_loader: Callable[[], Any] | None, link_factory: Callable[..., Any] | None,
             link_options: dict | None, connect_timeout_s: float) -> None:
        from .auth import credentials
        from .auth.credentials import AuthError
        from .paths import ConfigDirError

        loader = credential_loader or credentials.load_credential
        try:
            loader()                                            # fail fast, offline: not signed in is exit 5
        except AuthError as exc:
            self._fail_from_exception(exc)
            return
        from . import store as store_mod
        from .link import DeviceLink

        try:
            store = store_mod.open_db()
        except ConfigDirError as exc:
            self._fail(EXIT_FAILURE, "config_dir", f"{exc}.")
            return
        except store_mod.StoreError as exc:
            self._fail(EXIT_FAILURE, "store_error", f"The local store could not be opened ({type(exc).__name__}).")
            return
        options = dict(link_options or {})
        options.setdefault("credential_loader", loader)
        try:
            asyncio.run(self.run(store, link_factory or DeviceLink, options, connect_timeout_s))
        finally:
            store.close()

    @staticmethod
    def _save_signals() -> dict[int, Any] | None:
        """The process's SIGINT / SIGTERM handlers as they are now.  ``asyncio.run`` replaces SIGINT's, so they are put back
        by hand afterwards.  Only the main thread can do this."""
        if threading.current_thread() is not threading.main_thread():
            return None
        return {signal.SIGINT: signal.getsignal(signal.SIGINT), signal.SIGTERM: signal.getsignal(signal.SIGTERM)}

    @staticmethod
    def _restore_signals(saved: dict[int, Any] | None) -> None:
        for signum, handler in (saved or {}).items():
            try:
                if handler is not None:
                    signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    def _emit(self) -> int:
        """The final output and the exit status."""
        failure = self.failure
        code = failure.exit_code if failure is not None else EXIT_OK
        if failure is not None:
            self._note(failure.message, prefix="m2c: ")
        if self.json_mode:
            document = {
                "ok": failure is None,
                "result": self._assemble(),
                "session_id": self.run_id,
                "run_id": self.run_id,
                "usage": self._usage,
                "exit_code": code,
                "error": None if failure is None else {"code": failure.code, "message": failure.message},
            }
            self._write_out(json.dumps(document, ensure_ascii=True, separators=(", ", ": ")) + "\n")
        elif self._wrote_any and self._last_char != "\n":
            self._write_out("\n")
        if self._stdout_dead and code == EXIT_OK:               # the output could not be delivered: not a success
            self._note("Could not write to stdout (it was closed).", prefix="m2c: ")
            code = EXIT_FAILURE
        return code


def run_headless(
    options: HeadlessOptions,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    stdin: IO[str] | None = None,
    credential_loader: Callable[[], Any] | None = None,
    link_options: dict | None = None,
    link_factory: Callable[..., Any] | None = None,
    decide: Callable[[ToolProposal], Any] | None = None,
    execute: Callable[[ToolProposal], Any] | None = None,
    project_path: str | None = None,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    settle_s: float = DEFAULT_SETTLE_S,
    cancel_wait_s: float = DEFAULT_CANCEL_WAIT_S,
    reconnect_give_up_s: float = DEFAULT_RECONNECT_GIVE_UP_S,
) -> int:
    """Run one task headlessly and return the exit status (see the module docstring).  Never raises for a failure of the
    run: it ends in an exit status, and in json mode in the one JSON object.

    Everything but ``options`` is for tests and for the integration patch: the streams (default: the process's), the
    stdin the task is read from for ``-p -``, ``credential_loader`` (default: C03's ``load_credential``),
    ``link_options`` / ``link_factory`` (default: C04's ``DeviceLink``), ``decide`` / ``execute`` (default: the hooks
    installed with ``install_tool_hooks``, else none: every tool call is then a policy denial), ``project_path`` (default:
    the current directory), and the timings."""
    run = _HeadlessRun(
        options,
        stdout=stdout if stdout is not None else sys.stdout,
        stderr=stderr if stderr is not None else sys.stderr,
        stdin=stdin,
        decide=decide if decide is not None else _HOOKS["decide"],
        execute=execute if execute is not None else _HOOKS["execute"],
        project_path=project_path if project_path is not None else os.getcwd(),
        settle_s=settle_s,
        cancel_wait_s=cancel_wait_s,
        reconnect_give_up_s=reconnect_give_up_s,
    )
    return run.execute(credential_loader, link_factory, link_options, connect_timeout_s)
