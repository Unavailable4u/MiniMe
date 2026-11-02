"""
m2c/src/m2c/runner.py -- Patch C24: the device tool runner, the glue between a proposed tool call and its execution.

SECURITY-CRITICAL: THIS MODULE DECIDES WHAT RUNS ON THE PERSON'S MACHINE
-----------------------------------------------------------------------
The server proposes a call (``tool_proposed``).  Nothing the server says is an instruction: this module asks the policy
engine (C06), asks the person when the engine says ask, takes a checkpoint (C08) before a file is changed, runs the
tool (C05 files and search, C07 shell) and answers with ``approval`` and ``tool_result`` exactly as protocol/README.md
sections 2, 3 and 5 describe.  Everything fails closed: a policy that cannot be read, an ``ask`` that returns garbage
or raises, a decision that cannot be recorded locally, a checkpoint that cannot be saved -- each ends in "not run".

    DeviceLink (C04)                        ToolRunner                                   C05 / C06 / C07 / C08
    ----------------                        ----------                                   ---------------------
    LinkCallbacks.on_tool_proposed  ---->   on_tool_proposed(event)   decide(Request, Context)  (C06)
    LinkCallbacks.on_event          ---->   observe(event)            ask(ApprovalRequest) -> Choice     (C09, later)
    send_approval / send_tool_result <----  one task per call         checkpoints.snapshot  (C08)
    send_tool_stream                 <----  worker thread -> loop     run_tool(..., call_env=CallEnv)    (C05, C07)

Wiring it into the prompt loop or the terminal UI is a later patch (I02, C09): this patch provides the runner and the
two small interfaces they plug into, ``ask`` (below) and the two callbacks (``on_tool_proposed``, ``observe``) that go
into ``LinkCallbacks``.  The headless adapter (``decide`` / ``execute``) fits C10's ``install_tool_hooks``.

THE FLOW OF ONE CALL
--------------------
1. **Idempotency.**  A ``call_id`` is executed at most once.  A ``tool_proposed`` for a call that is running, finished,
   or already decided in the local store (after a restart) is ignored.  A call the server has already closed with a
   ``tool_finished`` is not executed either (protocol/README.md section 5: "a client executes a replayed
   ``tool_proposed`` only if no later ``tool_finished`` for that call is in the replay").  ``observe(event)`` records
   those ``tool_finished`` events; after a restart the link's own log (``server.run_event.tool_finished``) is read.
   The link delivers a replay frame by frame, so a call is decided only after one loop turn plus
   ``replay_settle_s`` (default 50 ms) -- a margin, not a guarantee; the guarantee is the closed check just before
   anything runs and the server ignoring the result of a call it closed.
2. **Decide** with C06: ``decide(Request(tool, input, server_risk), Context)``.  The effective risk is the higher of
   the server's and the client's (C06 computes it; ``ApprovalRequest.risk`` shows it).  Unknown tools and malformed
   arguments are denied by C06 and this module follows it.
3. **allow**: the decision is recorded locally, ``approval{allow, once}`` is sent, then the call runs.  **deny**:
   ``approval{deny, once}`` is sent and NO ``tool_result`` (a denied call gets an approval and no result).  **ask**:
   see below.  (protocol/fixtures/02 shows an auto-allowed read answered with a result alone, and C10 does the same;
   the work package says "allow: send approval".  The runner follows the work package; ``approve_auto_allowed=False``
   gives the fixture's behavior.  The server accepts both: an ``allow`` approval is recorded and does nothing else.)
4. **Checkpoint** (``edit_file``, ``write_file``, ``delete_path``): ``checkpoints.snapshot`` of the path, at a step number
   that rises with every checkpointed call of the session (continued from the store after a restart, because a second
   row for the same step would lose to the first).  A path that does not exist yet is fine ("did not exist").  If the
   snapshot reports that the path could not be saved (too large, a symlink, unreadable, ...) or raises, the call is
   REFUSED with a ``tool_result`` error carrying the fixed text ``MSG_CHECKPOINT_FAILED`` and does not run.
   **``bash`` cannot be checkpointed** and is not: nobody can say what a shell command will touch, so a rewind does not
   undo it.  The runner does not pretend otherwise; ``checkpoints.REWIND_NOTICE`` is the sentence the UI shows with every
   rewind.
5. **Execute.**  ``run_tool`` is synchronous and runs in a worker thread with a
   ``CallEnv(authorization, policy, cancel, on_chunk)``: ``AUTH_POLICY_ALLOWED`` when the engine allowed the call,
   ``AUTH_USER_APPROVED`` when the person (now or earlier in the session) did; ``policy`` is the C06 ``Context``, so the
   shell tool re-checks and a deny still wins; ``cancel`` is a ``threading.Event`` per call; ``on_chunk`` forwards
   stdout and stderr with ``send_tool_stream`` in order, from the worker thread to the event loop (live view only, capped).
   Read-only tools (``read_file``, ``list_dir``, ``glob``, ``grep``) run side by side.  Everything that changes
   something (``edit_file``, ``write_file``, ``delete_path``, ``bash``, ``plan_exit``) runs ALONE and in ARRIVAL order, and
   is decided only when its turn comes, so a call is always judged in the state the earlier ones left (``plan_exit``
   changes the mode; the edit behind it is judged in the new one).
6. **Answer** with ``tool_result{ok, output, truncated, trust, exit_code?, duration_ms}``.  ``trust`` is what C05/C07 say:
   ``local_project`` for files, ``external`` for the shell.  (The work package says "untrusted"; protocol/v1 ``trust``
   allows only ``local_project``, ``local_other`` and ``external``, protocol/ wins, and C07 already documents
   ``external`` as the most distrusted value.)  A send that fails because the link is down is NOT retried later: the
   server closes the call as ``disconnected`` and the model is told, so the result is dropped.  If the ``approval`` cannot
   be sent the call is not run at all (it would run a second time when the model retries).

``ask`` (the interface the terminal UI implements)
--------------------------------------------------
``ask`` is an ``async`` callable ``ask(ApprovalRequest) -> Choice``.  ``ApprovalRequest`` holds the call id, the tool,
the effective risk, the REAL input to show, C06's reason and the mode.  ``Choice`` holds ``decision`` (allow/deny),
``scope`` (once/session) and optionally ``edited_input``.  Prompts are shown ONE AT A TIME, in arrival order, however
many calls arrive together.  The runner cancels the ``ask`` task (``asyncio.CancelledError`` inside it) when the call is
closed under it (the server timed out, the run was cancelled or ended): the UI must dismiss the prompt then.  With no
``ask`` (a non-interactive run) an ``ask`` decision is a deny, and ``policy_denials`` / ``would_ask`` count it, so
headless mode can exit with its policy-denial code.  A malformed answer, or an ``ask`` that raises, is a deny.

* ``scope="session"`` means "allow THIS EXACT call for the rest of this session": the tool and its canonical input are
  remembered in memory only (never written anywhere, never a pattern, never for another input or tool) and a repeat is
  treated as an explicit allow by the person.  A ``deny`` rule still wins over it.  ``plan_exit`` is never remembered.
  A deny with ``scope="session"`` is reported (and recorded) as ``once``.
* ``edited_input``: the person changed the call before allowing it.  It must be an object; it is run through C06 again
  (a deny wins and nothing runs; C06 also rejects malformed arguments, and ``run_tool`` checks them once more).  When it
  runs, the ``tool_result`` output STARTS with ``EDITED_NOTICE``, then the command or input that actually ran, so the
  model is never misled about what happened.

``plan_exit``
-------------
C05 has no ``plan_exit`` executor; the runner handles it.  C06 always asks for it.  When the person allows it the runner
leaves plan mode itself (to ``plan_exit_mode``, default ``ask``) and tells the host through ``on_mode_change``; the host's
own mode (what ``policy_context`` returns) wins as soon as it is no longer ``plan``.  FINDING for the server side: the
server cannot learn about this.  backend/codeagent/loop.py takes the run's mode from ``run.start`` / ``run.resume`` state
only (``_Ctx.mode``), protocol/README.md section 12 lists "changing mode mid-run" as not covered by the nine client message
types, and no message was invented here.  Until an additive protocol change exists, the model keeps seeing the plan-mode
tool list on the server while the device already allows edits.

Audit and hygiene
-----------------
Each decision is recorded locally in ONE transaction: an event of type ``approval`` ({call_id, tool, decision, scope,
decided_by, edited}) and its row in the ``approvals`` table (C02), with an EMPTY ``input_summary``: never the input, the
output, a path or a command.  If the record cannot be written the call is not run.  Logs carry only a tool name, a call
id, a decision, a scope and an error code through a local allowlist (the shared ``SAFE_KEYS`` of logging_setup.py has no
``tool``, ``call_id`` or ``decision`` key and extending it is a separate, reviewed change): every value must match a safe
pattern, and a tool name that is not in the catalog is logged as ``unknown``.

Threads
-------
The runner, the store connection and ``ask`` live on the event loop's thread (a SQLite connection belongs to one thread);
only ``run_tool`` and its ``on_chunk`` run in worker threads.  ``snapshot`` runs on the loop thread (a file of up to 10 MB
is read there; calls that change files are serialized anyway).  ``cancel_run`` must be called from the loop thread.

Headless adapter
----------------
``decide(proposal)`` and ``execute(proposal)`` are shaped for ``headless.install_tool_hooks`` (C10): ``decide`` answers
exactly ``"allow"``, ``"ask"`` or ``"deny"`` (a would-ask is reported as ``"ask"`` and C10 fails the run closed), counts
the denial and records it; ``execute`` re-decides (an ``ask`` is not an allow) and returns a ``headless.ToolOutcome``.
C10 sends the results itself, so neither sends anything.  The headless run's id is minted inside ``run_headless``: call
``bind_run(run_id)`` once it is known (I02) or edits are refused, because they cannot be checkpointed (fail closed); the
audit record is skipped while no run is bound.  There is no streaming and no cancel handle in headless mode.

Place this file at: m2c/src/m2c/runner.py
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import checkpoints
from .headless import ToolOutcome as HeadlessOutcome
from .link import LinkError, LinkMessageTooLargeError
from .policy import MODES, TOOLS as POLICY_TOOLS, Context, Decision, Request
from .policy.decide import decide as policy_decide
from .store import append_event
from .store.db import normalize_ts, transaction
from .tools import MAX_WIRE_CHARS, CallEnv, ToolContext, run_tool
from .tools.shell import AUTH_POLICY_ALLOWED, AUTH_USER_APPROVED

logger = logging.getLogger("m2c.runner")

__all__ = [
    "ApprovalRequest",
    "Choice",
    "EDITED_NOTICE",
    "MSG_CHECKPOINT_FAILED",
    "ToolRunner",
]

# ---------------------------------------------------------------------------
# Fixed texts (what the model reads; nothing a person, a model or a file wrote is ever put in one)
# ---------------------------------------------------------------------------
EDITED_NOTICE = "[The person edited this call before it ran. This is what actually ran:]"
MSG_CHECKPOINT_FAILED = (
    "error: not run. A checkpoint of the file could not be saved first, so the change was refused. Nothing was changed."
)
MSG_EXEC_FAILED = "error: the tool could not be run."
MSG_TOO_LARGE = "error: the tool output could not be sent (too large, or not text)."
MSG_NOT_ALLOWED = "error: not run. The policy does not allow this call."
MSG_ALREADY_RAN = "error: not run. This call id was already used."
MSG_PLAN_EXITED = "The person approved the plan. Plan mode is off on this device."
MSG_NOT_IN_PLAN = "Plan mode was not on. Nothing changed."

READ_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep"})
CHECKPOINTED_TOOLS = frozenset({"edit_file", "write_file", "delete_path"})
SERIAL_TOOLS = CHECKPOINTED_TOOLS | {"bash", "plan_exit"}
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed", "expired"})
_MODE_STRICTNESS = {"plan": 0, "ask": 1, "accept-edits": 2}

DEFAULT_REPLAY_SETTLE_S = 0.05
MAX_PARALLEL_READS = 8
MAX_STREAM_BYTES = 1_000_000
STREAM_PIECE_CHARS = 60_000
MAX_ECHO_CHARS = 4_000
MAX_GRANTS = 1_000

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_LOG_KEYS = frozenset({"tool", "call_id", "decision", "scope", "code", "status", "duration_ms", "count"})


# ---------------------------------------------------------------------------
# The interface with the terminal UI
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalRequest:
    """What the person is asked.  ``input`` is the REAL input of the call (a copy: changing it changes nothing), never a
    summary; ``risk`` is the effective risk (the higher of the server's and the client's); ``reason`` is C06's text;
    ``mode`` is the permission mode the decision was made in."""

    call_id: str
    tool: str
    risk: str
    input: Any
    reason: str
    mode: str


@dataclass(frozen=True)
class Choice:
    """The person's answer.  ``decision`` is ``allow`` or ``deny``; ``scope`` is ``once`` or ``session`` (the exact call,
    for the rest of the session); ``edited_input`` is the call's input as the person changed it (an object), or None."""

    decision: str
    scope: str = "once"
    edited_input: Any = None


AskCallable = Callable[[ApprovalRequest], Awaitable[Choice]]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _log(level: int, event: str, **fields: Any) -> None:
    """One content-free log line: allowlisted keys, values that match a safe pattern (anything else is ``invalid``)."""
    try:
        if not logger.isEnabledFor(level):
            return
        parts = [event]
        for key, value in fields.items():
            if key not in _LOG_KEYS:
                continue
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, int):
                text = str(value)
            elif isinstance(value, str) and _SAFE_VALUE.match(value):
                text = value
            else:
                text = "invalid"
            parts.append(f"{key}={text}")
        logger.log(level, " ".join(parts))
    except Exception:  # noqa: BLE001 - logging fails open
        pass


def _safe_tool(tool: Any) -> str:
    """A tool name fit for a log line: only names of the catalog, anything else is ``unknown``."""
    return tool if isinstance(tool, str) and tool in POLICY_TOOLS else "unknown"


def _canonical(value: Any) -> str | None:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


def _stricter(a: Any, b: Any) -> Any:
    """The stricter of two modes (plan < ask < accept-edits); an unknown mode is kept so that C06 denies it."""
    if a not in _MODE_STRICTNESS:
        return a
    if b not in _MODE_STRICTNESS:
        return b
    return a if _MODE_STRICTNESS[a] <= _MODE_STRICTNESS[b] else b


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    output: str
    truncated: bool = False
    trust: str = "local_project"
    exit_code: int | None = None
    duration_ms: int = 0


def _outcome_of(result: Any) -> _Outcome:
    """A validated ``_Outcome`` from an executor's ``ToolResult`` (whatever it says is checked, not trusted)."""
    try:
        ok, output = result.ok, result.output
        trust = result.trust if result.trust in ("local_project", "local_other", "external") else "external"
        exit_code = result.exit_code if type(result.exit_code) is int else None
        duration = result.duration_ms if type(result.duration_ms) is int and result.duration_ms >= 0 else 0
        if type(ok) is not bool or not isinstance(output, str):
            raise TypeError
        return _Outcome(ok, output, bool(result.truncated), trust, exit_code, duration)
    except Exception:  # noqa: BLE001 - an unusable result is a failed call, not a crash
        return _Outcome(False, MSG_EXEC_FAILED, False, "local_project", None, 0)


class _Call:
    """One proposed call while the runner is working on it."""

    __slots__ = ("call_id", "tool", "input", "risk", "mutating", "cancel", "cancel_async", "predecessor", "finished",
                 "task", "answered")

    def __init__(self, call_id: str, tool: Any, input: Any, risk: Any, mutating: bool) -> None:
        loop = asyncio.get_running_loop()
        self.call_id = call_id
        self.tool = tool
        self.input = input
        self.risk = risk
        self.mutating = mutating
        self.cancel = threading.Event()              # read by the worker thread (the shell tool kills the command)
        self.cancel_async = asyncio.Event()          # read by the loop (a prompt is dismissed)
        self.predecessor: "asyncio.Future[None] | None" = None
        self.finished: "asyncio.Future[None]" = loop.create_future()
        self.task: "asyncio.Task[None] | None" = None
        self.answered = False


class _Streamer:
    """Forwards a call's stdout/stderr chunks, in order, from the worker thread to ``send_tool_stream``.  Live view only:
    capped, and the first failed send stops it (the final result is what the model sees)."""

    def __init__(self, link: Any, run_id: str, call_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self._link, self._run_id, self._call_id, self._loop = link, run_id, call_id, loop
        self._queue: "asyncio.Queue[tuple[str, str] | None]" = asyncio.Queue()
        self._closed = False
        self._dead = False
        self._bytes = 0
        self._task = loop.create_task(self._pump())

    def on_chunk(self, stream: str, text: str) -> None:           # called from the worker thread
        try:
            self._loop.call_soon_threadsafe(self._put, stream, text)
        except RuntimeError:                                       # the loop is closed: nobody is watching
            pass

    def _put(self, stream: str, text: str) -> None:
        if not self._closed and stream in ("stdout", "stderr") and isinstance(text, str) and text:
            self._queue.put_nowait((stream, text))

    async def _pump(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if self._dead:
                continue
            stream, text = item
            for start in range(0, len(text), STREAM_PIECE_CHARS):
                piece = text[start:start + STREAM_PIECE_CHARS]
                self._bytes += len(piece.encode("utf-8", "replace"))
                if self._bytes > MAX_STREAM_BYTES:
                    self._dead = True
                    break
                try:
                    await self._link.send_tool_stream(self._run_id, self._call_id, stream=stream, chunk=piece)
                except (LinkError, ValueError):
                    self._dead = True
                    break

    async def close(self) -> None:
        self._closed = True
        self._queue.put_nowait(None)
        await self._task


_DISMISSED = object()
_FAILED = object()


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class ToolRunner:
    """Decide, ask, checkpoint, run and answer the tool calls the server proposes.  See the module docstring.

    link               an object with ``send_approval``, ``send_tool_result`` and ``send_tool_stream`` (a ``DeviceLink``);
                       None for the headless adapter, which sends nothing
    run_id             the run's id; the local session has the same id (``DeviceLink.start_run`` makes it so); None until
                       ``bind_run`` for a headless run
    tool_context       C05's ``ToolContext`` (the project root, the denylist, the read-before-write record)
    policy_context     ``() -> Context``: the CURRENT C06 context (mode, rule layers, project root, trust); called for
                       every decision, so a mode change takes effect at once
    store              the local store connection (C02): checkpoints and the audit record; belongs to the loop's thread
    session_id         the session in the store when it is not ``run_id``
    ask                ``async (ApprovalRequest) -> Choice``, or None for a non-interactive run
    clock              ``() -> aware datetime`` (the audit timestamps); default now in UTC
    executor           ``run_tool``'s signature; default C05's ``run_tool`` (tests inject slow or counting stand-ins)
    plan_exit_mode     the mode ``plan_exit`` switches to: ``ask`` (default) or ``accept-edits``
    on_mode_change     ``(mode)`` called when ``plan_exit`` changes the mode; its failure changes nothing
    approve_auto_allowed   send ``approval{allow}`` also for calls the engine allowed without asking (default True)
    max_snapshot_bytes     the size above which a file is not checkpointed (and its change refused)
    replay_settle_s    how long a call waits before it is decided, so a replay's ``tool_finished`` can arrive first
    max_parallel_reads reads that may run at once
    """

    def __init__(
        self,
        link: Any,
        *,
        run_id: str | None,
        tool_context: ToolContext,
        policy_context: Callable[[], Context],
        store: sqlite3.Connection,
        session_id: str | None = None,
        ask: AskCallable | None = None,
        clock: Callable[[], datetime] = _utc_now,
        executor: Callable[..., Any] = run_tool,
        plan_exit_mode: str = "ask",
        on_mode_change: Callable[[str], Any] | None = None,
        approve_auto_allowed: bool = True,
        max_snapshot_bytes: int = checkpoints.MAX_SNAPSHOT_BYTES,
        replay_settle_s: float = DEFAULT_REPLAY_SETTLE_S,
        max_parallel_reads: int = MAX_PARALLEL_READS,
    ) -> None:
        if plan_exit_mode not in MODES or plan_exit_mode == "plan":
            raise ValueError("plan_exit_mode must be a mode other than plan")
        if not callable(policy_context):
            raise ValueError("policy_context must be a function returning the current policy Context")
        self._link = link
        self._run_id = run_id
        self._session_id = session_id if session_id is not None else run_id
        self._tool_context = tool_context
        self._get_policy = policy_context
        self._store = store
        self._ask = ask
        self._clock = clock
        self._executor = executor
        self._plan_exit_target = plan_exit_mode
        self._on_mode_change = on_mode_change
        self._approve_auto_allowed = bool(approve_auto_allowed)
        self._max_snapshot_bytes = max_snapshot_bytes
        self._settle = max(0.0, float(replay_settle_s))
        self._read_sem = asyncio.Semaphore(max(1, int(max_parallel_reads)))
        self._prompt_lock = asyncio.Lock()

        self._plan_exit_mode: str | None = None
        self._calls: dict[str, _Call] = {}
        self._seen: set[str] = set()
        self._server_closed: set[str] = set()
        self._adapter_done: set[str] = set()
        self._tasks: set["asyncio.Task[None]"] = set()
        self._tail: "asyncio.Future[None] | None" = None
        self._grants: set[tuple[str, str]] = set()
        self._step: int | None = None
        self._ended = False
        self._denials = 0
        self._would_ask = 0

    # -- observable state ------------------------------------------------------------------------------------------
    @property
    def policy_denials(self) -> int:
        """Calls the POLICY refused (a deny, a would-ask with nobody to ask, an edited call C06 now denies).  A person
        saying no is not a policy denial.  Headless mode exits with its policy-denial code when this is above 0."""
        return self._denials

    @property
    def would_ask(self) -> int:
        """Calls that needed an approval nobody could give (no ``ask``)."""
        return self._would_ask

    @property
    def in_flight(self) -> int:
        return sum(1 for call in self._calls.values() if not call.finished.done())

    @property
    def mode(self) -> str | None:
        """The permission mode decisions are made in right now (None if the policy cannot be read)."""
        context, _ = self._context()
        return None if context is None else context.mode

    def bind_run(self, run_id: str, session_id: str | None = None) -> None:
        """Tell a runner built before the run existed (headless mode) which run it serves."""
        self._run_id = run_id
        self._session_id = session_id if session_id is not None else run_id

    # -- callbacks for LinkCallbacks ---------------------------------------------------------------------------------
    def on_tool_proposed(self, event: Any) -> None:
        """``LinkCallbacks.on_tool_proposed``: a ``tool_proposed``.  Synchronous and quick; the work is a task."""
        try:
            if not self._mine(event) or self._ended:
                return
            payload = event.payload
            call_id = payload.get("call_id")
        except Exception:  # noqa: BLE001 - a callback must not raise into the link
            return
        if not isinstance(call_id, str) or not _SAFE_VALUE.match(call_id):
            return
        if call_id in self._seen:
            _log(logging.DEBUG, "runner.skipped", call_id=call_id, code="duplicate")
            return
        self._seen.add(call_id)
        if self._decided_in_store(call_id):
            _log(logging.INFO, "runner.skipped", call_id=call_id, code="already_decided")
            return
        tool = payload.get("tool")
        call = _Call(call_id, tool, payload.get("input"), payload.get("risk"), tool in SERIAL_TOOLS)
        if call.mutating:                                    # arrival order: queue behind the previous change
            call.predecessor = self._tail
            self._tail = call.finished
        self._calls[call_id] = call
        _log(logging.DEBUG, "runner.proposed", tool=_safe_tool(tool), call_id=call_id)
        call.task = asyncio.get_running_loop().create_task(self._process(call))
        self._tasks.add(call.task)
        call.task.add_done_callback(self._tasks.discard)

    def observe(self, event: Any) -> None:
        """``LinkCallbacks.on_event``: every accepted stream event.  Records ``tool_finished`` (a call the server closed is
        not executed, and one still running is cancelled) and ends the runner's work when the run reaches a terminal state."""
        try:
            if not self._mine(event) or event.type != "run_event":
                return
            payload = event.payload
            kind = payload.get("type")
            if kind == "tool_finished":
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    return
                self._server_closed.add(call_id)
                call = self._calls.get(call_id)
                if call is not None and not call.finished.done():
                    _log(logging.INFO, "runner.closed_by_server", call_id=call_id, status=str(payload.get("status")))
                    self._cancel_call(call)
            elif kind == "state_changed" and payload.get("state") in TERMINAL_STATES:
                self._ended = True
                self.cancel_run()
        except Exception:  # noqa: BLE001 - a callback must not raise into the link
            return

    def cancel_run(self, run_id: str | None = None) -> int:
        """Cancel every call of the run that has not finished: a running shell command is killed, a prompt is dismissed,
        a queued change is dropped.  Call it when the run ends, when the person interrupts and on ``aclose``.  Nothing is
        sent for a cancelled call (the server closes it).  Returns the number of calls cancelled."""
        if run_id is not None and run_id != self._run_id:
            return 0
        count = 0
        for call in list(self._calls.values()):
            if not call.finished.done():
                self._cancel_call(call)
                count += 1
        return count

    def interrupt(self) -> int:
        """The person pressed Esc: ``cancel_run`` for this run."""
        return self.cancel_run(self._run_id)

    async def wait_idle(self, timeout: float | None = None) -> None:
        """Wait until no call is being worked on (the tests' and the shutdown's join point)."""

        async def drain() -> None:
            while self._tasks:
                await asyncio.wait(set(self._tasks))

        await (drain() if timeout is None else asyncio.wait_for(drain(), timeout))

    async def aclose(self) -> None:
        """Cancel everything and wait for the tasks (a killed command takes up to the shell's grace period)."""
        self._ended = True
        self.cancel_run()
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=15)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # -- one call ---------------------------------------------------------------------------------------------------
    def _mine(self, event: Any) -> bool:
        return self._run_id is None or getattr(event, "run_id", None) == self._run_id

    @staticmethod
    def _cancel_call(call: _Call) -> None:
        call.cancel.set()
        call.cancel_async.set()

    async def _process(self, call: _Call) -> None:
        try:
            await self._pipeline(call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unexpected bug ends this call only, closed
            _log(logging.WARNING, "runner.error", tool=_safe_tool(call.tool), call_id=call.call_id,
                 code=type(exc).__name__)
            if not call.answered and not call.cancel.is_set():
                await self._send_approval(call, "deny", "once")
        finally:
            if not call.finished.done():
                call.finished.set_result(None)
            self._calls.pop(call.call_id, None)

    def _dropped(self, call: _Call) -> bool:
        if call.cancel.is_set():
            return True
        if call.call_id in self._server_closed or self._closed_in_store(call.call_id):
            _log(logging.INFO, "runner.skipped", call_id=call.call_id, code="closed_by_server")
            return True
        return False

    async def _pipeline(self, call: _Call) -> None:
        if call.predecessor is not None:
            await call.predecessor
        if self._settle > 0:
            await asyncio.sleep(self._settle)
        if self._dropped(call):
            return
        tool = _safe_tool(call.tool)

        context, failure = self._context()
        decision = self._decide(call.tool, call.input, call.risk, context, failure)
        action = decision.action
        scope, decided_by, authorization = "once", "policy", AUTH_POLICY_ALLOWED
        effective: Any = call.input
        edited = False

        if action == "ask":
            grant = self._grant_key(call.tool, call.input)
            if grant is not None and grant in self._grants:
                action, scope, decided_by, authorization = "allow", "session", "user", AUTH_USER_APPROVED
            elif self._ask is None:
                self._would_ask += 1
                action = "deny"                                   # approval is needed but nobody can be asked
            else:
                status, choice = await self._prompt(call, decision, context)
                if status is _DISMISSED:
                    return                                       # the call was closed under the prompt: nothing to answer
                if status is _FAILED or not self._valid_choice(choice):
                    action, decided_by = "deny", "policy"
                    _log(logging.WARNING, "runner.bad_answer", tool=tool, call_id=call.call_id)
                elif choice.decision == "deny":
                    action, decided_by = "deny", "user"
                else:
                    action, decided_by, authorization, scope = "allow", "user", AUTH_USER_APPROVED, choice.scope
                    if choice.edited_input is not None and _canonical(choice.edited_input) != _canonical(call.input):
                        again = self._decide(call.tool, choice.edited_input, call.risk, context, failure)
                        if again.action == "deny":           # a deny wins over the person's own edit
                            action, decided_by, scope = "deny", "policy", "once"
                        else:
                            effective, edited = copy.deepcopy(choice.edited_input), True
                    if action == "allow" and scope == "session":
                        self._remember(call.tool, effective)

        if action != "allow":
            if decided_by == "policy":
                self._denials += 1
            _log(logging.INFO, "runner.decision", tool=tool, call_id=call.call_id, decision="deny", scope="once")
            self._record(call, "deny", "once", decided_by, False)
            await self._send_approval(call, "deny", "once")
            return

        _log(logging.INFO, "runner.decision", tool=tool, call_id=call.call_id, decision="allow", scope=scope)
        if not self._record(call, "allow", scope, decided_by, edited):
            await self._send_approval(call, "deny", "once")      # no record, no run
            return
        if self._approve_auto_allowed or authorization == AUTH_USER_APPROVED:
            if not await self._send_approval(call, "allow", scope):
                return                                           # the link is down: the server closes the call
        if call.cancel.is_set():
            return
        outcome = await self._execute(call, effective, authorization, context, edited, stream=True)
        if call.cancel.is_set():
            return                                               # closed meanwhile: nobody waits for this result
        await self._send_result(call, outcome)

    # -- policy -----------------------------------------------------------------------------------------------------
    def _context(self) -> tuple[Context | None, str]:
        """The current policy context, with the plan-exit override applied; ``(None, reason)`` if it cannot be had."""
        try:
            context = self._get_policy()
        except Exception:  # noqa: BLE001 - fail closed
            return None, "the policy could not be read"
        if not isinstance(context, Context):
            return None, "the policy is malformed"
        if context.mode != "plan":
            self._plan_exit_mode = None                          # the host follows (or changed) the mode itself
        elif self._plan_exit_mode is not None:
            context = dataclasses.replace(context, mode=self._plan_exit_mode)
        return context, ""

    @staticmethod
    def _decide(tool: Any, args: Any, risk: Any, context: Context | None, failure: str = "") -> Decision:
        if context is None:
            return Decision("deny", failure or "the policy could not be read")
        return policy_decide(Request(tool, args, risk), context)

    # -- asking -----------------------------------------------------------------------------------------------------
    @staticmethod
    def _valid_choice(choice: Any) -> bool:
        return (isinstance(choice, Choice) and choice.decision in ("allow", "deny") and choice.scope in ("once", "session")
                and (choice.edited_input is None or isinstance(choice.edited_input, dict)))

    async def _prompt(self, call: _Call, decision: Decision, context: Context | None) -> tuple[Any, Any]:
        """Show one prompt (one at a time).  Returns ``(status, choice)``; ``status`` is ``_DISMISSED`` when the call was
        closed under the prompt, ``_FAILED`` when ``ask`` raised, else None."""
        assert self._ask is not None
        request = ApprovalRequest(call.call_id, str(call.tool), decision.risk or str(call.risk),
                                  copy.deepcopy(call.input), decision.reason, context.mode if context else "")

        async def shown() -> Any:
            async with self._prompt_lock:
                if call.cancel_async.is_set():
                    return _DISMISSED
                return await self._ask(request)

        task = asyncio.ensure_future(shown())
        waiter = asyncio.ensure_future(call.cancel_async.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return _DISMISSED, None
            if task.cancelled():
                return _DISMISSED, None
            if task.exception() is not None:
                _log(logging.WARNING, "runner.ask_failed", tool=_safe_tool(call.tool), call_id=call.call_id,
                     code=type(task.exception()).__name__)
                return _FAILED, None
            result = task.result()
            return (_DISMISSED, None) if result is _DISMISSED else (None, result)
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()

    def _grant_key(self, tool: Any, args: Any) -> tuple[str, str] | None:
        text = _canonical(args)
        if text is None or not isinstance(tool, str) or tool == "plan_exit":
            return None
        return tool, text

    def _remember(self, tool: Any, args: Any) -> None:
        key = self._grant_key(tool, args)
        if key is not None and len(self._grants) < MAX_GRANTS:
            self._grants.add(key)                                # memory only: never written anywhere

    # -- execution ----------------------------------------------------------------------------------------------------
    async def _execute(self, call: _Call, args: Any, authorization: str, context: Context | None, edited: bool,
                       *, stream: bool) -> _Outcome:
        if call.tool == "plan_exit":
            outcome = self._plan_exit(context)
        elif call.tool in CHECKPOINTED_TOOLS and not self._checkpoint(args):
            return _Outcome(False, MSG_CHECKPOINT_FAILED)
        else:
            outcome = await self._run_in_thread(call, args, authorization, context, stream)
        return self._with_edit_notice(call.tool, args, outcome) if edited else outcome

    async def _run_in_thread(self, call: _Call, args: Any, authorization: str, context: Context | None,
                             stream: bool) -> _Outcome:
        streamer = None
        if stream and call.tool == "bash" and self._link is not None and self._run_id is not None:
            streamer = _Streamer(self._link, self._run_id, call.call_id, asyncio.get_running_loop())
        env = CallEnv(authorization=authorization, policy=context, cancel=call.cancel,
                      on_chunk=None if streamer is None else streamer.on_chunk)
        try:
            if call.tool in SERIAL_TOOLS:
                result = await asyncio.to_thread(self._executor, self._tool_context, call.tool, args, call_env=env)
            else:
                async with self._read_sem:
                    result = await asyncio.to_thread(self._executor, self._tool_context, call.tool, args, call_env=env)
            return _outcome_of(result)
        except Exception as exc:  # noqa: BLE001 - the tool failing is the model's business
            _log(logging.WARNING, "runner.exec_failed", tool=_safe_tool(call.tool), call_id=call.call_id,
                 code=type(exc).__name__)
            return _Outcome(False, MSG_EXEC_FAILED)
        finally:
            if streamer is not None:
                await streamer.close()

    def _plan_exit(self, context: Context | None) -> _Outcome:
        if context is None or context.mode != "plan":
            return _Outcome(True, MSG_NOT_IN_PLAN)
        self._plan_exit_mode = self._plan_exit_target
        if self._on_mode_change is not None:
            try:
                self._on_mode_change(self._plan_exit_target)
            except Exception as exc:  # noqa: BLE001 - the UI's problem; the mode did change
                _log(logging.WARNING, "runner.mode_callback_failed", code=type(exc).__name__)
        return _Outcome(True, MSG_PLAN_EXITED)

    @staticmethod
    def _with_edit_notice(tool: Any, args: Any, outcome: _Outcome) -> _Outcome:
        """The output starts with one fixed line, then what actually ran, so the model is never misled."""
        if tool == "bash" and isinstance(args, dict) and isinstance(args.get("command"), str):
            shown, label = args["command"], "command: "
        else:
            shown, label = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str), "input: "
        if len(shown) > MAX_ECHO_CHARS:
            shown = shown[:MAX_ECHO_CHARS] + f" [... {len(shown) - MAX_ECHO_CHARS} more characters not shown]"
        head = f"{EDITED_NOTICE}\n{label}{shown}\n\n"
        output, truncated = outcome.output, outcome.truncated
        if len(head) + len(output) > MAX_WIRE_CHARS:
            marker = "\n[truncated: output exceeded the size limit]"
            output, truncated = output[: MAX_WIRE_CHARS - len(head) - len(marker)] + marker, True
        return dataclasses.replace(outcome, output=head + output, truncated=truncated)

    # -- checkpoints ----------------------------------------------------------------------------------------------
    def _checkpoint(self, args: Any) -> bool:
        """Snapshot the path before it changes; False (the call is refused) unless it is saved.  Fails closed."""
        path = args.get("path") if isinstance(args, dict) else None
        if not isinstance(path, str) or self._session_id is None:
            return False
        try:
            if self._step is None:
                (last,) = self._store.execute(
                    "SELECT COALESCE(MAX(step), 0) FROM checkpoints WHERE session_id = ?", (self._session_id,)).fetchone()
                self._step = int(last)
            self._step += 1
            result = checkpoints.snapshot(self._session_id, self._step, [path], self._tool_context.root,
                                          conn=self._store, max_file_bytes=self._max_snapshot_bytes)
        except Exception as exc:  # noqa: BLE001 - any failure to save the checkpoint refuses the call
            _log(logging.WARNING, "runner.checkpoint_failed", code=type(exc).__name__)
            return False
        if not result.complete or result.already_recorded:
            _log(logging.WARNING, "runner.checkpoint_failed", code="incomplete", count=len(result.skipped))
            return False
        return True

    # -- the answers ------------------------------------------------------------------------------------------------
    async def _send_approval(self, call: _Call, decision: str, scope: str) -> bool:
        """True if sent.  A link that is down, or a payload the protocol refuses, is False (and never retried)."""
        call.answered = True
        if self._link is None or self._run_id is None:
            return False
        try:
            await self._link.send_approval(self._run_id, call.call_id, decision=decision, scope=scope)
        except (LinkError, ValueError) as exc:
            _log(logging.INFO, "runner.send_failed", call_id=call.call_id, code=type(exc).__name__)
            return False
        return True

    async def _send_result(self, call: _Call, outcome: _Outcome) -> None:
        if self._link is None or self._run_id is None:
            return
        try:
            try:
                await self._link.send_tool_result(
                    self._run_id, call.call_id, ok=outcome.ok, output=outcome.output, truncated=outcome.truncated,
                    trust=outcome.trust, exit_code=outcome.exit_code, duration_ms=outcome.duration_ms)
            except (LinkMessageTooLargeError, ValueError):
                await self._link.send_tool_result(
                    self._run_id, call.call_id, ok=False, output=MSG_TOO_LARGE, truncated=True, trust=outcome.trust,
                    exit_code=None, duration_ms=outcome.duration_ms)
        except LinkError as exc:                                 # the link is down: the server closes the call; drop it
            _log(logging.INFO, "runner.send_failed", call_id=call.call_id, code=type(exc).__name__)
        else:
            _log(logging.DEBUG, "runner.result", tool=_safe_tool(call.tool), call_id=call.call_id,
                 status="ok" if outcome.ok else "error", duration_ms=outcome.duration_ms)

    # -- the local record ---------------------------------------------------------------------------------------------
    def _record(self, call: _Call | Any, decision: str, scope: str, decided_by: str, edited: bool) -> bool:
        """Write the decision: an ``approval`` event and its ``approvals`` row in one transaction.  Never the input or the
        output.  False when it could not be written (the caller then does not run the call).  With no session bound
        (headless, before ``bind_run``) there is nothing to write to: True."""
        if self._session_id is None:
            return True
        tool = call.tool if isinstance(call.tool, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", call.tool) else "invalid"
        try:
            stamp = normalize_ts(self._clock())
            data = {"call_id": call.call_id, "tool": tool, "decision": decision, "scope": scope,
                    "decided_by": decided_by, "edited": edited}
            with transaction(self._store):
                event = append_event(self._store, self._session_id, "approval", data, ts=stamp)
                self._store.execute(
                    "INSERT INTO approvals (session_id, seq, call_id, tool, input_summary, decision, scope, decided_by, ts) "
                    "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)",
                    (self._session_id, event.seq, call.call_id, tool, decision, scope, decided_by, stamp))
        except Exception as exc:  # noqa: BLE001 - fail closed: no record, no run
            _log(logging.ERROR, "runner.audit_failed", call_id=call.call_id, code=type(exc).__name__)
            return False
        return True

    def _decided_in_store(self, call_id: str) -> bool:
        if self._session_id is None:
            return False
        try:
            return self._store.execute("SELECT 1 FROM approvals WHERE session_id = ? AND call_id = ? LIMIT 1",
                                       (self._session_id, call_id)).fetchone() is not None
        except sqlite3.Error:
            return False                                         # the audit write will fail and deny the call

    def _closed_in_store(self, call_id: str) -> bool:
        """True if the link's own log holds a ``tool_finished`` for the call (the server closed it, before a restart)."""
        if self._session_id is None:
            return False
        kind = "server.run_event.tool_finished"
        try:
            try:
                row = self._store.execute(
                    "SELECT 1 FROM events WHERE session_id = ? AND type = ? "
                    "AND json_extract(json, '$.payload.call_id') = ? LIMIT 1", (self._session_id, kind, call_id)).fetchone()
                return row is not None
            except sqlite3.OperationalError:                     # a SQLite without JSON functions
                for (text,) in self._store.execute("SELECT json FROM events WHERE session_id = ? AND type = ?",
                                                   (self._session_id, kind)):
                    if f'"call_id":{json.dumps(call_id)}' in text:
                        return True
                return False
        except sqlite3.Error:
            return False

    # -- the headless adapter (C10's install_tool_hooks) ----------------------------------------------------------------
    def _headless_context(self, proposal: Any) -> tuple[Context | None, str]:
        context, failure = self._context()
        if context is not None:
            context = dataclasses.replace(context, mode=_stricter(context.mode, getattr(proposal, "mode", context.mode)))
        return context, failure

    async def decide(self, proposal: Any) -> str:
        """``decide(proposal) -> "allow" | "ask" | "deny"`` for ``headless.install_tool_hooks``.  A would-ask is ``"ask"``:
        C10 fails the run closed on it.  Counts and records the denial."""
        call_id = getattr(proposal, "call_id", None)
        tool = getattr(proposal, "tool", None)
        context, failure = self._headless_context(proposal)
        decision = self._decide(tool, getattr(proposal, "input", None), getattr(proposal, "risk", None), context, failure)
        verdict = decision.action if decision.action in ("allow", "ask", "deny") else "deny"
        stub = _Call.__new__(_Call)
        stub.call_id, stub.tool = str(call_id), tool
        if verdict == "allow":
            if not self._record(stub, "allow", "once", "policy", False):
                verdict = "deny"
        if verdict != "allow":
            self._denials += 1
            if verdict == "ask":
                self._would_ask += 1
            self._record(stub, "deny", "once", "policy", False)
        _log(logging.INFO, "runner.decision", tool=_safe_tool(tool), call_id=str(call_id), decision=verdict, scope="once")
        return verdict

    async def execute(self, proposal: Any) -> HeadlessOutcome:
        """``execute(proposal) -> headless.ToolOutcome`` for ``headless.install_tool_hooks``.  Decides again (only an
        ``allow`` runs; an ``ask`` here is a refusal), never runs a call id twice, checkpoints changes (refused until
        ``bind_run``) and sends nothing: C10 sends the result."""
        call_id = getattr(proposal, "call_id", None)
        if not isinstance(call_id, str) or call_id in self._adapter_done:
            return HeadlessOutcome(False, MSG_ALREADY_RAN)
        self._adapter_done.add(call_id)
        tool = getattr(proposal, "tool", None)
        args = getattr(proposal, "input", None)
        context, failure = self._headless_context(proposal)
        if self._decide(tool, args, getattr(proposal, "risk", None), context, failure).action != "allow":
            return HeadlessOutcome(False, MSG_NOT_ALLOWED)
        call = _Call(call_id, tool, args, getattr(proposal, "risk", None), tool in SERIAL_TOOLS)
        self._calls[call_id] = call
        try:
            outcome = await self._execute(call, args, AUTH_POLICY_ALLOWED, context, False, stream=False)
        finally:
            call.finished.set_result(None)
            self._calls.pop(call_id, None)
        return HeadlessOutcome(outcome.ok, outcome.output, outcome.truncated, outcome.trust, outcome.exit_code,
                               outcome.duration_ms)
