"""
codeagent/loop.py -- Patch S07: the agent loop executor v0, the server-side brain of a run.

WHAT THIS IS. One user message in, a finished turn out. The loop assembles the model's context, calls the
model with the tools the run may use, streams the reply to the device, sends the model's tool calls to the
device through the broker (S09), appends what comes back and goes round again, until the model has nothing
more to ask for or something says stop. It is a LIBRARY with two entry points:

    run_turn(conn, run, msg)        the turn-runner contract S14 defines (run_handlers.set_turn_runner), so
                                    this can replace hello.py's text-only runner
    AgentLoop.resume(conn, run)     what S14's run.resume hook calls later (through on_resume, below)

    device                 S14 (router, handlers)            this module                     others
    ------                 ----------------------            -----------                     ------
    user_message   --->    queue, one worker per run  --->   run_turn
                                                               assemble context   ---------> ContextAssembler (S08)
                                                               model call         ---------> S05 adapter (chain)
                                                               meter              ---------> Metering (S10)
                                                               tool calls         ---------> ToolBroker (S09)
                                                                   <--- outcomes -----------  device policy decides
    cancel / disconnect --> cancels the worker task    --->   CancelledError, cleans up, re-raises

EVERYTHING IS INJECTED. The constructor takes the model chain and an adapter (or a factory per choice), the
broker, the run manager / runs / buffer modules (S06), `emit` (S14's RunEmitter.emit: it numbers, stores and
sends stream messages, so this module never assigns a seq), a Metering hook, a ContextAssembler hook, the
tool catalog, run_guard and tool_budget, and a LoopConfig. Tests pass fakes for all of it.

WIRING. install() (bottom of this file) registers the loop with S14. Patch S15 (codeagent/wiring.py) builds the process
loop from the real parts (broker, emitter, S08's assembler, S10's metering) and calls it from server.py in place of
hello.py's install; this module only gained `offer_tools` (below) for that patch.

TOOLS ONLY FOR CLIENTS THAT CAN ANSWER THEM (patch S15). `offer_tools(conn) -> bool` is asked ONCE per turn, with the
connection the turn runs on. False (or an exception: fail closed) means the turn offers NO tools to the model, and a
call the model makes anyway is answered with MSG_NO_TOOLS and never forwarded, so a client that cannot answer
(`hello.capabilities` without "tools") never receives a tool_proposed. The gate can only remove tools: plan mode still
filters what is left. With no predicate (the default) nothing changes.

THE STEP
    1. cancel check (run_guard flag). Step limit. Metering.before_call() (may raise BudgetPause).
    2. assemble the context (hook) with the tools VISIBLE in this mode: every protocol/v1/tools definition,
       and in plan mode only risk R0 tools (which includes plan_exit). "This mode" is the run's CURRENT mode:
       Patch S07b changes it once, when the person approves plan_exit (LEAVING PLAN MODE, below).
    3. call the model, walking the chain on provider errors (below). Every TextDelta becomes a `text_delta`
       event as it arrives; the reply ends with `text_done` (stop_reason tool_use or end_turn/max_tokens).
       The usage event goes to Metering.after_call and nothing about credits is emitted here (S10 owns it).
    4. the reply is appended to the conversation (buffer), then each tool call is CLASSIFIED:
         malformed (arguments None, or an error from S05's tolerant parser)   -> error result for the model
         a tool that is not visible (unknown, or hidden in plan mode)         -> error result, never forwarded
         arguments that do not fit the tool's input_schema (closed objects:
           an unknown argument is a failure too)                              -> error result, never forwarded
         valid                                                                -> broker.propose, concurrently
       The error text is bounded, fixed server text plus schema paths and keywords; never the model's values.
       A call id is re-minted per call (`call_<hex>`): providers reuse ids across replies and the broker is
       idempotent per (run, call id), so a reused id would be answered with an OLD outcome.
    5. results are appended in the ORIGINAL call order, as soon as the prefix that is complete is known (so a
       disconnect loses nothing that had finished). A denied call becomes a tool message saying the user
       denied it and it must not be retried unasked.
    6. the stop checks (below), then the next step.

STATES. The loop moves the run row through S06 (runs.transition / run_manager.pause_run / cancel_run) and
announces each move as a `state_changed` run_event through `emit`. awaiting_approval is ADVISORY (protocol
README 3): entered when a call above R0 is proposed, left (back to running) when its approval or its result
arrives (the broker's on_approval hook, or the outcome). The loop NEVER marks a run `completed`: whether a
conversation continues is the caller's decision, so a finished turn only returns `finished`.

HOW A TURN ENDS (TurnResult.outcome, .reason)
    finished             the model answered with no tool call. The run stays running.
    paused_budget        step_limit | tool_call_budget | whatever a BudgetPause from Metering says
    paused_error         repeated_identical_failure (same tool, same canonical input, failed 3 times; the
                         notice names the tool, never the arguments) | repeated_failures (run_guard's
                         breaker: three consecutive failing calls, role = the tool name) | provider_unavailable
                         | model_call_failed | internal
    paused_disconnected  the broker reported a call as disconnected (the device is gone)
    cancelled            run_guard's cancel flag was set (the loop finishes the cancel: broker.cancel_all, then
                         run_manager.cancel_run), or the broker cancelled the calls because S14 is cancelling
                         the run (then S14 finishes it and the loop touches nothing)
    ended                the run was ended elsewhere while the turn ran (its buffer or its row is gone)
"Failing" is a call that ended error, timeout or denied, or that the loop itself refused (malformed, hidden,
invalid). Tool calls are counted with tool_budget.increment, one per call that is about to be sent; the call
that takes the count ABOVE config.tool_call_threshold is not sent (and nothing after it), it gets a "not run"
result so the conversation stays well formed, and the run pauses paused_budget / tool_call_budget.

CANCEL (design note 6.9). run_guard.cancel_reason is checked between steps, at least every
config.cancel_poll_s while the model streams, and while tools run. A flag stops the adapter stream (closing the
generator closes the provider connection), calls broker.cancel_all(run_id) and moves the run to cancelled
through S06. S14's own cancel takes the other road: its hooks run broker.cancel_all and cancel the worker task,
which reaches us as CancelledError; we close the stream, cancel what we started and re-raise.

PROVIDER ERRORS. The chain is walked in order. Errors classified (utils.llm_errors.classify_error, which
ModelAdapterError already carries as .bucket) RATE_LIMIT_WINDOW and TRANSIENT_NETWORK fall through to the next
choice; PERMANENT_AUTH, MALFORMED_REQUEST and CONTEXT_LENGTH_EXCEEDED do not (the next provider would fail the
same way, or the key is ours to fix): the run pauses paused_error, error code `internal`. A provider that
rejected a call as tool_use_failed is retried up to config.tool_use_failed_retries times (sampling is random).
When the next choice has a different quality_class the device gets a `notice` model_changed (severity warning,
fixed text naming only the two classes): never a silent downgrade. If text had already been streamed when the
choice failed, a `notice` reply_restarted (hello.py's existing code) tells the client to drop the partial text.
Every choice failed: the error `provider_unavailable`, then paused_error. There is no sleeping or backoff in
v0, only the walk.

DISCONNECT AND RESUME. A call the broker reports as disconnected (the device is not connected at all, or the
connection ended) pauses the run paused_disconnected and ends the turn; it is left unanswered in the
conversation on purpose. On a real disconnect S14 cancels the worker first and pauses the run itself; our pause
then finds the run already paused and does nothing more. resume() reads the conversation back from the buffer
(S14's run.resume has already rebuilt it from the device's `state` when it had expired), adds for every call
that has no result a tool message saying the device disconnected, the call may or may not have completed and the
state should be checked before repeating it (protocol README 5: the model is told on resume), and continues
the loop. If the last thing in the conversation is a finished assistant reply there is nothing to continue.
S14 gives hooks HOOK_TIMEOUT_SECONDS (10 s), so the hook that is registered (on_resume) starts resume() as a
tracked task per run and returns; the cancel and disconnect hooks stop that task.

LEAVING PLAN MODE (Patch S07b). A run's mode reaches the server once, in run.start. In plan mode the model sees
only the read-only tools and plan_exit; when the person approves plan_exit the DEVICE leaves plan mode and answers
the call with a successful tool_result, and nothing in the protocol tells the server more than that. So the loop
reads that answer: a plan_exit that comes back with status ok AND ok true, in a run whose mode is plan, switches
the run to `ask` for the rest of its life. The very next model call in the same turn then gets the full tool set and
the normal system prompt. The server cannot know whether the person chose ask or accept-edits on the device; the two
differ only in the device's policy, so `ask` is the right word for the server, and it is the ONLY word this path
can write (never a stricter one, never one the device names: the answer's text is not read).
    * Nothing else switches it. A plan_exit that is denied, fails, times out, is refused before it is sent, or
      comes back ok false changes nothing; so does a plan_exit in a run that is not in plan mode (an accept-edits
      run stays accept-edits); and so does every other tool, whatever its output says.
    * The new mode is STORED, in the run row (runs.set_mode, a conditional UPDATE: plan -> ask, open runs only),
      BEFORE the result is appended to the conversation. The buffer's state record is sealed once when the buffer
      is created and has no update, so it keeps saying `plan`; every turn and every resume therefore starts from
      _settle_mode, which lets a stored mode that has left plan win over a state that still says plan. (A buffer
      rebuilt from a device's state is read the same way: once the server has left plan mode, a device cannot put
      it back. The device's own policy still decides what is allowed on the device.)
    * If the row cannot be written the switch is NOT made (fail closed, like every other run-store fault): the run
      pauses paused_error and the plan_exit result is left unanswered, so the resume tells the model it may or may
      not have worked and the model asks again. The log holds the run id and the two mode words, nothing else.

DECISIONS THIS PATCH HAD TO MAKE (each is a small change if you disagree)
    * notice severity is `warning`, not the work package's `warn`: protocol/v1/run_event.json has info|warning.
    * The consecutive-failure LIMIT is run_guard's (TASK_MAX_CONSECUTIVE_FAILURES, 3), not LoopConfig's: the
      breaker is shared state in Redis. The identical-failure limit is LoopConfig's.
    * run_guard's `tripped` flag survives successes until grant_more(). A resume after a repeated_failures
      pause would re-trip at once, so resume() calls run_guard.grant_more(run_id) when the breaker is tripped
      (the person resuming is the "grant"). Identical-failure counts are per turn and cleared at its end.
    * Step counts are per turn (a turn is one user message to its answer); resume() starts a fresh count.
    * The risk sent with a call is the tool's risk_max where it has one (protocol README 7), so delete_path goes
      out as R4; the device's policy may only raise it.
    * A broker deadline is max(config.call_timeout_s, input.timeout_s + config.call_grace_s) so a long bash
      command is not cut by the broker while the device still runs it.
    * BudgetPause is honoured from Metering.before_call only; after_call fails open like all metering.
    * model_pref other than "auto" is not applied in v0; the device gets an info notice once per turn.
    * runs.add_progress (the informational step counter) is not touched: S10 owns the run's counters.

LOGGING (build plan 11.4, O1). Run id, user id, device id, tool names from the catalog, counts, state names,
error codes and exception TYPES. Never a prompt, a reply, a tool argument or result, or an exception's text
(tests/static/test_loop_logging.py reads this file). notice.message and error.message are fixed server text.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from codeagent import broker as broker_module
from codeagent import buffer as buffer_module
from codeagent import emitter as emitter_module
from codeagent import router as router_module
from codeagent import run_handlers, run_manager as run_manager_module, runs as runs_module
from codeagent.connections import ConnectionClosed
from codeagent.model_adapter import ModelAdapter, ModelAdapterError, ModelRequest, TextDelta, ToolCall, ToolSpec, Usage
from codeagent.prompts import DefaultContextAssembler, call_assembler
from utils.llm_errors import ErrorBucket, classify_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# Fixed texts. Server-authored: nothing a person, a model or a device wrote is ever put in one
# (a catalog tool NAME and numbers from the config are the only variable parts).
# ---------------------------------------------------------------------------------------------

MSG_DENIED = ("The user denied this action. Do not retry it unless the user asks for it; "
              "ask them first if you still need it.")
MSG_TIMEOUT = ("The tool call timed out. It may or may not have completed; "
               "check the current state before repeating it.")
MSG_RESUME_DISCONNECTED = ("The device disconnected while this call was running. The call may or may not have "
                           "completed; check the current state before repeating it.")
MSG_TOOL_FAILED = "The tool reported an error."
MSG_NOT_RUN_BUDGET = "This call was not run because the run reached its tool-call budget."
MSG_TOO_MANY_IN_FLIGHT = "Too many tool calls were in flight at once. Call fewer tools at a time."
MSG_CALL_NOT_SENT = "The call could not be sent to the device. Try again."
MSG_NO_TOOLS = "No tools are available in this session. Answer from the conversation alone."
MSG_HIDDEN_IN_PLAN_MODE = "This tool is not available in plan mode. Only read-only tools are."
MSG_UNKNOWN_TOOL = "That tool does not exist. Use one of the tools you were given."
MSG_MALFORMED = {
    "malformed_arguments": "The call's arguments were not a valid JSON object. Send the call again with valid JSON.",
    "truncated_arguments": "The call was cut off before it was complete. Send it again, shorter if you can.",
    "unknown_tool": MSG_UNKNOWN_TOOL,
    "missing_name": "The call had no tool name. Send it again with the tool's name.",
}
MSG_MALFORMED_DEFAULT = "The call could not be used. Send it again."

MSG_PROVIDER_UNAVAILABLE = "No model could answer right now. You can retry."
MSG_INTERNAL = "The server could not continue this run. You can retry."
MSG_QUOTA_EXHAUSTED = "Your credit pool is used up for now."

NOTICE_MODEL_CHANGED = "model_changed"
NOTICE_REPLY_RESTARTED = "reply_restarted"        # hello.py's code and text: the client drops partial text
MSG_REPLY_RESTARTED = "The connection dropped while the reply was being written, so it is being written again."
NOTICE_STEP_LIMIT = "step_limit_reached"
NOTICE_TOOL_BUDGET = "tool_call_budget_reached"
NOTICE_IDENTICAL_FAILURE = "repeated_identical_failure"
NOTICE_FAILURE_BREAKER = "repeated_failures"
NOTICE_MODEL_PREF = "model_preference_not_applied"
MSG_MODEL_PREF = "The model you asked for cannot be chosen yet, so the default model is used."
MSG_FAILURE_BREAKER = "Several tool calls in a row failed, so the run paused."
# Patch S08: AssembledContext.notes are fixed machine codes from the context assembler; each one the loop knows
# becomes one notice with this fixed text (never any conversation content). An unknown code is not forwarded.
NOTICE_CONTEXT_COMPACTED = "context_compacted"
NOTICE_CONTEXT_THRASH = "context_thrash"
MSG_CONTEXT_COMPACTED = ("Older tool output in this conversation was cleared or condensed to keep it within the "
                         "model's context window.")
MSG_CONTEXT_THRASH = ("The conversation keeps filling the model's context window, so automatic compaction was "
                      "stopped for this run. Start a new session or narrow the task.")
CONTEXT_NOTICES = {NOTICE_CONTEXT_COMPACTED: ("info", MSG_CONTEXT_COMPACTED),
                   NOTICE_CONTEXT_THRASH: ("warning", MSG_CONTEXT_THRASH)}

# state_changed reasons (protocol: ^[a-z][a-z0-9_]{0,63}$)
REASON_STEP_LIMIT = "step_limit"
REASON_TOOL_BUDGET = "tool_call_budget"
REASON_IDENTICAL = "repeated_identical_failure"
REASON_BREAKER = "repeated_failures"
REASON_PROVIDER = "provider_unavailable"
REASON_MODEL_FAILED = "model_call_failed"
REASON_INTERNAL = "internal"
REASON_NEEDS_APPROVAL = "tool_needs_approval"
REASON_APPROVAL_DONE = "tool_resolved"
REASON_CANCELLED = "cancelled"
DISCONNECT_REASON = "device_disconnected"

_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_ARG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_DEVICE_TRUST = frozenset({"local_project", "local_other", "external"})

FALL_THROUGH = frozenset({ErrorBucket.RATE_LIMIT_WINDOW, ErrorBucket.TRANSIENT_NETWORK})
# What counts as a failing call for the breaker and the identical-failure rule.
FAILING = frozenset({"error", "timeout", "denied"})
# Outcomes that leave the call unanswered in the conversation (resume tells the model).
DANGLING = frozenset({"disconnected", "cancelled"})
# Patch S07b: the one mode change the loop makes. Fixed words, not read from the device or the model.
MODE_PLAN = "plan"
MODE_AFTER_PLAN_EXIT = "ask"
PLAN_EXIT_TOOL = "plan_exit"


# ---------------------------------------------------------------------------------------------
# Public small types
# ---------------------------------------------------------------------------------------------

class BudgetPause(Exception):
    """Metering.before_call raises this to pause the run (paused_budget). S10 supplies the real hook.

    reason       the state_changed reason (a lowercase word, e.g. "quota_exhausted", "run_ceiling")
    error_code   a protocol error code to record on the run, or None. "quota_exhausted" also sends the device
                 the `quota_exhausted` error first, then the state, the order of protocol fixture 04
    reset_at     when the pool resets, if known (details.reset_at of that error)
    """

    def __init__(self, reason: str = "budget", error_code: "str | None" = None, reset_at: "str | None" = None):
        self.reason = reason if isinstance(reason, str) and _REASON_RE.match(reason) else "budget"
        self.error_code = error_code
        self.reset_at = reset_at
        super().__init__("the run is paused for budget")


class Metering:
    """The hook S10 implements. The default does nothing. One instance serves one run (see AgentLoop's
    `metering_factory`), so budget state can live on it. Both may be sync or async."""

    async def before_call(self) -> None:
        """Called before every model call. Raise BudgetPause to stop. Any other exception fails open."""

    async def after_call(self, usage: Usage) -> None:
        """Called with the adapter's Usage after every completed model call. Never stops the run."""


@dataclass(frozen=True)
class ModelChoice:
    """One step of the model chain. quality_class is a lowercase word (for example "strong", "standard",
    "light"); a change of class between steps is announced, never silent."""
    provider: str
    model: str
    quality_class: str
    key_env: "str | None" = None
    timeout_s: "float | None" = None

    def __post_init__(self):
        if not self.provider or not self.model:
            raise ValueError("a model choice needs a provider and a model")
        if not isinstance(self.quality_class, str) or not _CLASS_RE.match(self.quality_class):
            raise ValueError("quality_class must be a short lowercase word")


@dataclass(frozen=True)
class LoopConfig:
    """Limits. step_limit counts model calls per turn (design note 6.3: 60). tool_call_threshold is the
    code-run budget (NOT the web chat's DEFAULT_TOOL_CALL_BUDGET of 15)."""
    step_limit: int = 60
    tool_call_threshold: int = 300
    identical_failure_limit: int = 3
    call_timeout_s: float = 120.0
    call_grace_s: float = 15.0
    model_timeout_s: "float | None" = 120.0
    max_parallel_calls: int = 8                 # the broker's own per-run cap (DEFAULT_MAX_IN_FLIGHT)
    cancel_poll_s: float = 0.5
    tool_use_failed_retries: int = 2
    error_text_max_chars: int = 600
    stop_timeout_s: float = 5.0

    def __post_init__(self):
        for name in ("step_limit", "tool_call_threshold", "identical_failure_limit", "max_parallel_calls",
                     "error_text_max_chars"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.tool_use_failed_retries, int) or self.tool_use_failed_retries < 0:
            raise ValueError("tool_use_failed_retries must be a non-negative integer")
        for name in ("call_timeout_s", "cancel_poll_s", "stop_timeout_s"):
            if not isinstance(getattr(self, name), (int, float)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive number")


@dataclass(frozen=True)
class TurnResult:
    """How a turn ended (module docstring). Counts and words only: no content."""
    outcome: str
    reason: "str | None" = None
    steps: int = 0
    tool_calls: int = 0


# ---------------------------------------------------------------------------------------------
# The tool catalog: what the model may see, how risky a call is, whether its arguments fit
# ---------------------------------------------------------------------------------------------

def _risk_rank(risk: str) -> int:
    return int(risk[1:]) if isinstance(risk, str) and re.fullmatch(r"R[0-4]", risk) else 4


class ToolCatalog:
    """The protocol/v1/tools definitions: ToolSpecs for the model, the risk a call carries, and schema
    validation. `definitions` maps name -> {name, description, input_schema, risk, [risk_max]}."""

    def __init__(self, definitions: Mapping[str, Mapping]):
        if not definitions:
            raise ValueError("a tool catalog needs at least one tool")
        self._defs = {name: dict(d) for name, d in definitions.items()}
        self._specs = {name: ToolSpec.from_definition(d) for name, d in self._defs.items()}
        self._validators: dict = {}

    @classmethod
    def from_protocol(cls) -> "ToolCatalog":
        """The catalog the device socket already loads (ws.load_protocol finds protocol/ in a checkout and in
        the image). Blocking the first time."""
        from codeagent import ws
        return cls(ws.load_protocol().load_tools())

    @property
    def names(self) -> frozenset:
        return frozenset(self._defs)

    def effective_risk(self, name: str) -> str:
        """The risk a call to this tool carries: risk_max where the tool has one, else risk."""
        d = self._defs[name]
        return d.get("risk_max") or d["risk"]

    def visible(self, mode: str) -> "tuple[ToolSpec, ...]":
        """Every tool; in plan mode only the read-only ones (effective risk R0) plus plan_exit."""
        out = []
        for name in sorted(self._defs):
            if mode != "plan" or name == "plan_exit" or _risk_rank(self.effective_risk(name)) == 0:
                out.append(self._specs[name])
        return tuple(out)

    def validate(self, name: str, tool_input: Any) -> "list[str]":
        """Why `tool_input` does not fit the tool's input_schema, as short bounded strings built from schema
        paths and keywords (and names the SCHEMA defines). Never the model's values. [] means it fits."""
        validator = self._validators.get(name)
        if validator is None:
            from jsonschema import Draft202012Validator
            validator = self._validators[name] = Draft202012Validator(self._defs[name]["input_schema"])
        problems = []
        for err in sorted(validator.iter_errors(tool_input), key=lambda e: (list(map(str, e.absolute_path)), e.validator)):
            where = "/".join(str(p) for p in err.absolute_path) or "arguments"
            if err.validator == "required" and isinstance(err.instance, dict):
                missing = [k for k in err.validator_value if k not in err.instance]
                problems.append("missing required argument(s): " + ", ".join(missing))
            elif err.validator == "additionalProperties" and isinstance(err.instance, dict):
                allowed = set((err.schema.get("properties") or {}))
                extra = [k for k in err.instance if k not in allowed and _ARG_NAME_RE.match(str(k))]
                problems.append("unexpected argument(s): " + (", ".join(extra[:5]) or "(unnamed)"))
            else:
                problems.append(f"{where}: {err.validator}")
            if len(problems) >= 6:
                break
        return problems


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

async def _blocking(fn, *args, **kwargs):
    """S06, run_guard and tool_budget are synchronous (database, Redis): off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def _tool_label(catalog: ToolCatalog, name: str) -> str:
    """A tool name safe to log, to put in a notice or to give the breaker as its role: a catalog name, else
    the word `unknown` (a model can write any string as a tool name)."""
    return name if name in catalog.names else "unknown"


def _history_tool_name(name: str) -> str:
    return name if isinstance(name, str) and _TOOL_NAME_RE.match(name) else "unknown_tool"


def _canonical_key(tool: str, tool_input: dict) -> str:
    """Identity of a call for the identical-failure rule: the tool and its canonical input, hashed so the
    input itself is not kept."""
    canon = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return tool + ":" + hashlib.sha256(canon.encode("utf-8", errors="replace")).hexdigest()


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def find_dangling(items: Sequence[Mapping]) -> "list[tuple[str, str]]":
    """(call_id, tool) of every assistant tool call in the conversation that has no tool result yet."""
    answered = {i.get("call_id") for i in items if i.get("role") == "tool"}
    out = []
    for item in items:
        if item.get("role") == "assistant":
            for call in item.get("tool_calls") or ():
                if call.get("call_id") not in answered:
                    out.append((call["call_id"], call.get("tool", "unknown_tool")))
    return out


def _tool_item(call_id: str, ok: bool, output: str, *, truncated: bool = False, trust: "str | None" = None) -> dict:
    item = {"role": "tool", "call_id": call_id, "ok": bool(ok), "output": output, "truncated": bool(truncated)}
    if trust in _DEVICE_TRUST:
        item["trust"] = trust
    return item


# ---------------------------------------------------------------------------------------------
# Defaults for the model side
# ---------------------------------------------------------------------------------------------

def default_adapter_factory(choice: ModelChoice) -> ModelAdapter:
    """The S05 OpenAI-compatible adapter for ONE choice (the loop walks the chain itself, so each adapter has a
    single endpoint). Imported on first use: it is not cheap."""
    from codeagent.adapters.openai_compat import OpenAICompatAdapter
    step = {"provider": choice.provider, "model": choice.model, "key_env": choice.key_env}
    if choice.timeout_s:
        step["timeout"] = choice.timeout_s
    return OpenAICompatAdapter.from_chain([step])


def default_chain(environ=None) -> "list[ModelChoice]":
    """The chain hello.py already builds (first provider with a key set), as one `standard` choice; empty when
    no key is set (then every turn answers provider_unavailable). Names variables, never key values."""
    from codeagent import hello
    return [ModelChoice(step["provider"], step["model"], "standard", step.get("key_env"))
            for step in hello.build_chain(environ)]


# ---------------------------------------------------------------------------------------------
# Internal control flow
# ---------------------------------------------------------------------------------------------

_END = object()                                    # the end of an adapter stream


class _Stop(Exception):
    """Unwinds to _drive with the turn's result."""

    def __init__(self, result: TurnResult):
        self.result = result
        super().__init__("stop")


class _CancelRequested(Exception):
    """run_guard's cancel flag is set: unwind (the stream gets closed on the way), then finish the cancel."""

    def __init__(self, why: str):
        self.why = why
        super().__init__("cancel requested")


class _ExternalCancel(Exception):
    """The broker cancelled the calls (S14 is cancelling the run): S14 finishes the cancel, we only stop."""


class _Ctx:
    """Everything one turn knows. Lives for the length of the turn."""

    def __init__(self, conn, run, msg, state: dict, items: list, metering):
        self.conn, self.run, self.msg = conn, run, msg
        self.user_id, self.run_id, self.device_id = run.user_id, run.id, run.device_id
        self.state, self.items, self.metering = state, items, metering
        self.mode = state.get("mode") or run.mode
        self.run_state = run.state if run.state in ("running", "awaiting_approval") else "running"
        self.state_lock = asyncio.Lock()
        self.steps = 0
        self.tool_calls = 0
        self.chain_pos = 0
        self.streamed_text = False
        self.cancel_sent = False
        self.identical: dict = {}
        self.breaker: "str | None" = None
        self.tools_on: "bool | None" = None          # patch S15: the offer_tools answer, asked once per turn (_tools_for)


@dataclass
class _Planned:
    """One tool call of a reply, classified."""
    call_id: str
    name: str
    tool_input: dict
    refusal: "str | None" = None         # set: not sent, this is the error the model gets
    key: "str | None" = None             # identity for the identical-failure rule
    risk: "str | None" = None
    not_run: bool = False                # over the tool-call budget


@dataclass
class _Result:
    planned: _Planned
    status: str                          # ok | error | timeout | denied | disconnected | cancelled | refused | not_run
    ok: bool = False
    output: str = ""
    truncated: bool = False
    trust: "str | None" = None
    exit_code: "int | None" = None

    @property
    def dangling(self) -> bool:
        return self.status in DANGLING

    def item(self) -> dict:
        return _tool_item(self.planned.call_id, self.ok, self.output, truncated=self.truncated, trust=self.trust)


@dataclass
class _Reply:
    text: str
    calls: "tuple[ToolCall, ...]"
    usage: "Usage | None"


class _Approvals:
    """The advisory awaiting_approval state of one step: on while any call above R0 has neither an approval nor
    a result."""

    def __init__(self, loop: "AgentLoop", ctx: _Ctx, call_ids):
        self._loop, self._ctx, self.pending = loop, ctx, set(call_ids)

    async def begin(self) -> None:
        if self.pending:
            await self._loop._set_run_state(self._ctx, "awaiting_approval", REASON_NEEDS_APPROVAL)

    async def resolve(self, call_id: str) -> None:
        if call_id in self.pending:
            self.pending.discard(call_id)
            if not self.pending:
                await self._loop._set_run_state(self._ctx, "running", REASON_APPROVAL_DONE)


# ---------------------------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------------------------

class AgentLoop:
    """See the module docstring.

    chain              list of ModelChoice, in order of preference (may be empty: every turn is then
                       provider_unavailable)
    adapter            one ModelAdapter used for every choice (tests, single-endpoint setups), or
    adapter_factory    callable(choice) -> ModelAdapter, called once per choice (default: S05's adapter)
    broker             S09's ToolBroker (propose, cancel_all)
    emit               async (run_id, t, payload) -> envelope: S14's RunEmitter.emit
    emit_final         optional async (run_id, state, reason): the live-only terminal state_changed
    unbind             optional callable(run_id): releases emitter state after a pause
    metering / metering_factory   a Metering for every run, or callable(run) -> Metering (S10); default no-op
    assembler          ContextAssembler (S08); default prompts.DefaultContextAssembler
    catalog            ToolCatalog; default ToolCatalog.from_protocol() on first use
    run_manager, runs, buffer      the S06 modules (or twins with the same functions)
    guard, tool_budget run_guard / tool_budget modules (or twins); defaults are the real ones
    offer_tools        optional callable(conn) -> bool (patch S15): may this connection be offered tools? default: yes
    """

    def __init__(self, *, chain: "Sequence[ModelChoice]", broker, emit, adapter: "ModelAdapter | None" = None,
                 adapter_factory: "Callable[[ModelChoice], ModelAdapter] | None" = None,
                 metering: "Metering | None" = None, metering_factory: "Callable[[Any], Metering] | None" = None,
                 assembler=None, config: "LoopConfig | None" = None, catalog: "ToolCatalog | None" = None,
                 run_manager=run_manager_module, runs=runs_module, buffer=buffer_module, guard=None,
                 tool_budget=None, emit_final=None, unbind=None, classify=classify_error, clock=time.monotonic,
                 offer_tools: "Callable[[Any], bool] | None" = None):
        if adapter is not None and adapter_factory is not None:
            raise ValueError("give an adapter or an adapter_factory, not both")
        for name, fn in (("emit", emit), ("emit_final", emit_final)):
            if fn is not None and not router_module._is_async_callable(fn):
                raise TypeError(f"{name} must be an async function")
        self.chain = tuple(chain)
        self.config = config or LoopConfig()
        self._broker = broker
        self._emit = emit
        self._emit_final = emit_final
        self._unbind = unbind
        self._adapter = adapter
        self._factory = adapter_factory or default_adapter_factory
        self._adapters: dict = {}
        self._metering = metering
        self._metering_factory = metering_factory
        self._assembler = assembler or DefaultContextAssembler()
        self._catalog = catalog
        self._rm, self._runs, self._buffer = run_manager, runs, buffer
        self._guard, self._tool_budget = guard, tool_budget
        self._classify, self._clock = classify, clock
        self._offer_tools = offer_tools
        self._locks: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()
        self._tasks: "dict[str, asyncio.Task]" = {}
        # S14 compares hooks by identity: one object per hook, made once (a bound method is new every time).
        self.hooks = {"on_resume": self.on_resume, "on_cancel": self.on_cancel,
                      "on_run_disconnect": self.on_run_disconnect}

    # -- lazy collaborators ------------------------------------------------------------------
    @property
    def catalog(self) -> ToolCatalog:
        if self._catalog is None:
            self._catalog = ToolCatalog.from_protocol()
        return self._catalog

    def _run_guard(self):
        if self._guard is None:
            from eo import run_guard
            self._guard = run_guard
        return self._guard

    def _budget(self):
        if self._tool_budget is None:
            from eo import tool_budget
            self._tool_budget = tool_budget
        return self._tool_budget

    def _metering_for(self, run) -> Metering:
        if self._metering_factory is not None:
            return self._metering_factory(run)
        return self._metering if self._metering is not None else Metering()

    def _adapter_for(self, choice: ModelChoice) -> ModelAdapter:
        if self._adapter is not None:
            return self._adapter
        key = (choice.provider, choice.model, choice.key_env)
        if key not in self._adapters:
            self._adapters[key] = self._factory(choice)
        return self._adapters[key]

    def _tools_for(self, ctx: "_Ctx") -> bool:
        """Patch S15: may this turn's connection be offered tools? The offer_tools gate is asked ONCE per turn (the answer
        is kept on the turn's context). No gate: yes. A gate that raises: no (fail closed); only the exception's type is
        logged."""
        if ctx.tools_on is None:
            ctx.tools_on = True
            if self._offer_tools is not None:
                try:
                    ctx.tools_on = bool(self._offer_tools(ctx.conn))
                except Exception as exc:
                    ctx.tools_on = False
                    logger.warning("[loop] offer_tools failed, offering no tools error=%s", type(exc).__name__)
        return ctx.tools_on

    def _lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = self._locks[run_id] = asyncio.Lock()
        return lock

    # =========================================================================================
    # Entry points
    # =========================================================================================

    async def run_turn(self, conn, run, msg) -> TurnResult:
        """S14's turn runner: one turn for one `user_message` (msg["payload"]["text"]). Owns the run's state."""
        text = msg["payload"]["text"]
        async with self._lock(run.id):
            return await self._turn(conn, run, msg, user_text=text, resuming=False)

    async def resume(self, conn, run) -> TurnResult:
        """Continue a run after run.resume took it out of a pause. See "DISCONNECT AND RESUME"."""
        async with self._lock(run.id):
            return await self._turn(conn, run, None, user_text=None, resuming=True)

    async def on_resume(self, conn, run) -> None:
        """run_handlers.add_on_resume hook. Starts resume() as a tracked task (S14 gives hooks 10 s)."""
        await self._stop_task(run.id)
        task = asyncio.create_task(self._resume_task(conn, run))
        self._tasks[run.id] = task
        task.add_done_callback(lambda t, rid=run.id: self._forget_task(rid, t))

    async def on_cancel(self, conn, run) -> None:
        """run_handlers.add_on_cancel hook: stop a continuation we started. (The broker's own hook cancels
        the calls; S14 cancels the worker task of an ordinary turn.)"""
        await self._stop_task(run.id)

    async def on_run_disconnect(self, conn, run) -> None:
        """run_handlers.add_on_run_disconnect hook: stop a continuation we started."""
        await self._stop_task(run.id)

    def _forget_task(self, run_id: str, task) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)

    async def _stop_task(self, run_id: str) -> None:
        task = self._tasks.pop(run_id, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=self.config.stop_timeout_s)

    async def _resume_task(self, conn, run) -> None:
        try:
            await self.resume(conn, run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[loop] resumed turn failed run=%s user=%s error=%s", run.id, run.user_id, type(exc).__name__)
            ctx = _Ctx(conn, run, None, {}, [], Metering())
            try:
                await self._pause(ctx, "paused_error", REASON_INTERNAL, error_code="internal")
            except Exception as inner:
                logger.error("[loop] could not pause run=%s error=%s", run.id, type(inner).__name__)

    # =========================================================================================
    # A turn
    # =========================================================================================

    async def _turn(self, conn, run, msg, *, user_text, resuming: bool) -> TurnResult:
        ctx = None
        try:
            conversation = await _blocking(self._buffer.read_conversation, run.id)
            ctx = _Ctx(conn, run, msg, dict(conversation.state), list(conversation.items), self._metering_for(run))
            await self._settle_mode(ctx)
            await self._heal(ctx)
            if resuming:
                if not self._should_continue(ctx.items):
                    return TurnResult("finished", "nothing_to_resume")
                await self._fresh_chance(ctx)
            else:
                await self._append(ctx, {"role": "user", "text": user_text})
            await self._model_pref_notice(ctx)
            return await self._steps(ctx)
        except _Stop as stop:
            return stop.result
        except _CancelRequested as cancel:
            return await self._finish_cancel(ctx, cancel.why)
        except _ExternalCancel:
            return self._result(ctx, "cancelled", "external")
        except asyncio.CancelledError:
            raise
        except (buffer_module.RunBufferError, emitter_module.EmitterError, runs_module.RunError,
                run_manager_module.RunManagerError) as exc:
            return await self._infrastructure_failure(ctx, run, exc)
        finally:
            if ctx is not None:
                ctx.identical.clear()

    @staticmethod
    def _should_continue(items: Sequence[Mapping]) -> bool:
        """A turn was cut short when the conversation ends with a user message or a tool result."""
        return bool(items) and not (items[-1].get("role") == "assistant" and not items[-1].get("tool_calls"))

    def _result(self, ctx, outcome: str, reason: "str | None" = None) -> TurnResult:
        return TurnResult(outcome, reason, ctx.steps if ctx else 0, ctx.tool_calls if ctx else 0)

    def _adopt_mode(self, ctx: _Ctx, mode: str) -> None:
        """The turn's mode, and the copy of it the assembler is handed in `state` (a private dict: the buffer
        itself is never written)."""
        ctx.mode = mode
        if "mode" in ctx.state:
            ctx.state["mode"] = mode

    async def _settle_mode(self, ctx: _Ctx) -> None:
        """Patch S07b. The mode this turn (or resume) starts in. The buffer's state is sealed once and still says
        `plan` after a run has left plan mode, so when it says plan the stored row is asked, and a row that has
        moved on wins. Only a `plan` state is ever looked up (every other run reads exactly what it read before),
        and the lookup can only RELAX plan, never tighten anything. If the row cannot be read the run stays in
        plan mode (the stricter side); the next call to the run store will report the fault itself."""
        if ctx.mode != MODE_PLAN:
            return
        try:
            current = await _blocking(self._runs.get_run, ctx.user_id, ctx.run_id)
        except runs_module.RunError as exc:
            logger.warning("[loop] could not read the stored mode, staying in plan mode run=%s error=%s",
                           ctx.run_id, type(exc).__name__)
            return
        if current.mode != MODE_PLAN:
            self._adopt_mode(ctx, current.mode)

    async def _leave_plan_mode(self, ctx: _Ctx, batch: "list[_Result]") -> None:
        """Patch S07b: the person approved plan_exit, so the run leaves plan mode (module docstring, LEAVING PLAN
        MODE). Called with a batch of results BEFORE they are appended, so a result is never recorded as an
        answer whose effect was not stored. Only an OK result of plan_exit (status ok and ok true) in a run that
        is in plan mode does anything; the target is the fixed word `ask`. A fault of the run store propagates
        (the turn pauses paused_error; see _turn)."""
        if ctx.mode != MODE_PLAN:
            return
        if not any(r.planned.name == PLAN_EXIT_TOOL and r.status == "ok" and r.ok is True for r in batch):
            return
        moved = await _blocking(self._runs.set_mode, ctx.user_id, ctx.run_id, MODE_AFTER_PLAN_EXIT,
                                expected=MODE_PLAN)
        if moved.mode != MODE_AFTER_PLAN_EXIT:             # not made: the run ended (or was changed) meanwhile
            return
        self._adopt_mode(ctx, moved.mode)
        logger.info("[loop] left plan mode run=%s mode=%s", ctx.run_id, ctx.mode)

    async def _heal(self, ctx: _Ctx) -> None:
        """Every tool call without a result gets one telling the model the device disconnected (P00). A
        provider refuses an assistant message whose calls are unanswered."""
        dangling = find_dangling(ctx.items)
        if dangling:
            await self._append(ctx, *[_tool_item(call_id, False, MSG_RESUME_DISCONNECTED) for call_id, _ in dangling])
            logger.info("[loop] told the model about interrupted calls run=%s count=%d", ctx.run_id, len(dangling))

    async def _fresh_chance(self, ctx: _Ctx) -> None:
        """The person resumed on purpose: a tripped breaker is cleared (run_guard keeps it past successes)."""
        guard = self._run_guard()
        try:
            if await _blocking(guard.breaker_reason, ctx.run_id):
                await _blocking(guard.grant_more, ctx.run_id)
        except Exception as exc:
            logger.warning("[loop] could not reset the failure breaker run=%s error=%s", ctx.run_id, type(exc).__name__)

    async def _model_pref_notice(self, ctx: _Ctx) -> None:
        pref = ctx.state.get("model_pref")
        if isinstance(pref, str) and pref not in ("", "auto"):
            await self._notice(ctx, NOTICE_MODEL_PREF, "info", MSG_MODEL_PREF)

    async def _append(self, ctx: _Ctx, *items: dict) -> None:
        await _blocking(self._buffer.append_conversation, ctx.run_id, *items)
        ctx.items.extend(items)

    # =========================================================================================
    # The steps
    # =========================================================================================

    async def _steps(self, ctx: _Ctx) -> TurnResult:
        cfg = self.config
        while True:
            await self._check_cancel(ctx)
            if ctx.steps >= cfg.step_limit:
                return await self._pause(ctx, "paused_budget", REASON_STEP_LIMIT, notice=(
                    NOTICE_STEP_LIMIT, "warning", f"The step limit of {cfg.step_limit} was reached, so the run paused."))
            await self._before_call(ctx)
            reply = await self._model_step(ctx)
            ctx.steps += 1
            planned = self._plan_calls(ctx, reply.calls)
            item: dict = {"role": "assistant"}
            if reply.text:
                item["text"] = reply.text
            if planned:
                item["tool_calls"] = [{"call_id": p.call_id, "tool": _history_tool_name(p.name),
                                       "input": p.tool_input} for p in planned]
            if len(item) > 1:
                await self._append(ctx, item)
            if not planned:
                return self._result(ctx, "finished")
            stop = await self._run_calls(ctx, planned)
            if stop is not None:
                return stop

    async def _before_call(self, ctx: _Ctx) -> None:
        try:
            await _maybe_await(ctx.metering.before_call())
        except BudgetPause as bp:
            error = None
            if bp.error_code == "quota_exhausted":
                error = ("quota_exhausted", MSG_QUOTA_EXHAUSTED, {"reset_at": bp.reset_at} if bp.reset_at else None)
            raise _Stop(await self._pause(ctx, "paused_budget", bp.reason, error_code=bp.error_code, error=error))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # metering fails open
            logger.warning("[loop] metering before_call failed, going on run=%s error=%s", ctx.run_id, type(exc).__name__)

    # -- the model call, walking the chain -------------------------------------------------------

    async def _model_step(self, ctx: _Ctx) -> _Reply:
        tools = self.catalog.visible(ctx.mode) if self._tools_for(ctx) else ()
        assembled = await call_assembler(self._assembler, ctx.state, ctx.items, tools, ctx.mode)
        for note in assembled.notes:
            known = CONTEXT_NOTICES.get(note) if isinstance(note, str) else None
            if known is not None:
                await self._notice(ctx, note, *known)
        request = ModelRequest(messages=assembled.messages, tools=assembled.tools, max_tokens=assembled.max_tokens,
                               provider_options=assembled.provider_options, timeout_s=self.config.model_timeout_s)
        retries = 0
        while True:
            if ctx.chain_pos >= len(self.chain):
                raise _Stop(await self._provider_unavailable(ctx))
            choice = self.chain[ctx.chain_pos]
            ctx.streamed_text = False
            try:
                reply = await self._stream_once(ctx, choice, request)
            except (_CancelRequested, _Stop, asyncio.CancelledError):
                raise
            except ModelAdapterError as exc:
                bucket, code = exc.bucket, exc.code
                self._log_model_failure(ctx, choice, bucket, code)
            except (buffer_module.RunBufferError, emitter_module.EmitterError):
                raise                                  # the stream is unreliable: not a provider problem
            except Exception as exc:                   # an adapter bug or a bare timeout
                bucket, code = self._classify(exc), None
                self._log_model_failure(ctx, choice, bucket, type(exc).__name__)
            else:
                await self._after_call(ctx, reply.usage)
                return reply
            if code == "tool_use_failed" and retries < self.config.tool_use_failed_retries:
                retries += 1
                continue
            if bucket not in FALL_THROUGH:
                raise _Stop(await self._model_failed(ctx))
            await self._advance_chain(ctx, choice)

    def _log_model_failure(self, ctx: _Ctx, choice: ModelChoice, bucket, code) -> None:
        logger.warning("[loop] model call failed run=%s provider=%s model=%s bucket=%s code=%s",
                       ctx.run_id, choice.provider, choice.model, getattr(bucket, "name", "?"), code)

    async def _advance_chain(self, ctx: _Ctx, failed: ModelChoice) -> None:
        """Move to the next choice. A different quality class is announced; so is a restarted reply."""
        ctx.chain_pos += 1
        if ctx.streamed_text:
            await self._notice(ctx, NOTICE_REPLY_RESTARTED, "info", MSG_REPLY_RESTARTED)
        if ctx.chain_pos < len(self.chain):
            nxt = self.chain[ctx.chain_pos]
            if nxt.quality_class != failed.quality_class:
                await self._notice(ctx, NOTICE_MODEL_CHANGED, "warning",
                                   f"The {failed.quality_class} model was not available, so a "
                                   f"{nxt.quality_class} model is answering instead.")

    async def _stream_once(self, ctx: _Ctx, choice: ModelChoice, request: ModelRequest) -> _Reply:
        gen = self._adapter_for(choice).stream(request)
        parts, calls, usage = [], [], None
        last_poll = self._clock()
        try:
            while True:
                event = await self._next_event(ctx, gen)
                if event is _END:
                    break
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    ctx.streamed_text = True
                    await self._emit(ctx.run_id, "run_event", {"type": "text_delta", "text": event.text})
                elif isinstance(event, ToolCall):
                    calls.append(event)
                elif isinstance(event, Usage):
                    usage = event
                now = self._clock()
                if now - last_poll >= self.config.cancel_poll_s:     # a steady stream never times out below
                    last_poll = now
                    await self._check_cancel(ctx)
        finally:
            await _close_stream(gen)
        calls = tuple(calls)
        if calls:
            stop_reason = "tool_use"
        else:
            stop_reason = usage.stop_reason if usage is not None and usage.stop_reason in ("end_turn", "max_tokens") \
                else "end_turn"
        await self._emit(ctx.run_id, "run_event", {"type": "text_done", "stop_reason": stop_reason})
        return _Reply("".join(parts), calls, usage)

    async def _next_event(self, ctx: _Ctx, gen):
        """The adapter's next event, or _END. While the provider is silent the cancel flag is still polled every
        config.cancel_poll_s, so a stalled stream can be cancelled (the pending read is cancelled, the caller
        closes the generator)."""
        pending = asyncio.ensure_future(gen.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=self.config.cancel_poll_s)
                if done:
                    try:
                        return pending.result()
                    except StopAsyncIteration:
                        return _END
                await self._check_cancel(ctx)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _after_call(self, ctx: _Ctx, usage: "Usage | None") -> None:
        try:
            await _maybe_await(ctx.metering.after_call(usage if usage is not None else Usage(reported=False)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[loop] metering after_call failed, going on run=%s error=%s", ctx.run_id, type(exc).__name__)

    async def _model_failed(self, ctx: _Ctx) -> TurnResult:
        """An error the next provider would not fix (auth, our payload, context size)."""
        return await self._pause(ctx, "paused_error", REASON_MODEL_FAILED, error_code="internal",
                                 error=("internal", MSG_INTERNAL, None))

    async def _provider_unavailable(self, ctx: _Ctx) -> TurnResult:
        return await self._pause(ctx, "paused_error", REASON_PROVIDER, error_code="provider_unavailable",
                                 error=("provider_unavailable", MSG_PROVIDER_UNAVAILABLE, None))

    # -- classifying the model's calls ---------------------------------------------------------------

    def _plan_calls(self, ctx: _Ctx, calls: "Sequence[ToolCall]") -> "list[_Planned]":
        catalog, visible = self.catalog, {s.name for s in self.catalog.visible(ctx.mode)}
        planned = []
        for call in calls:
            call_id = "call_" + uuid.uuid4().hex[:24]
            name = call.name if isinstance(call.name, str) else ""
            if not call.ok:
                refusal = MSG_MALFORMED.get(call.error, MSG_MALFORMED_DEFAULT)
                planned.append(_Planned(call_id, name, {}, refusal=refusal))
            elif name not in catalog.names:
                planned.append(_Planned(call_id, name, call.arguments, refusal=MSG_UNKNOWN_TOOL))
            elif not self._tools_for(ctx):
                planned.append(_Planned(call_id, name, call.arguments, refusal=MSG_NO_TOOLS))
            elif name not in visible:
                planned.append(_Planned(call_id, name, call.arguments, refusal=MSG_HIDDEN_IN_PLAN_MODE))
            else:
                problems = catalog.validate(name, call.arguments)
                key = _canonical_key(name, call.arguments)
                if problems:
                    text = f"Invalid arguments for {name}: " + "; ".join(problems) + ". Fix them and call again."
                    planned.append(_Planned(call_id, name, call.arguments, refusal=_bounded(text, self.config.error_text_max_chars),
                                            key=key))
                else:
                    planned.append(_Planned(call_id, name, call.arguments, key=key, risk=catalog.effective_risk(name)))
        return planned

    # =========================================================================================
    # Running the calls
    # =========================================================================================

    async def _run_calls(self, ctx: _Ctx, planned: "list[_Planned]") -> "TurnResult | None":
        await self._apply_tool_budget(ctx, planned)
        results: "list[_Result | None]" = [None] * len(planned)
        for i, p in enumerate(planned):
            if p.refusal is not None:
                results[i] = _Result(p, "refused", False, p.refusal)
            elif p.not_run:
                results[i] = _Result(p, "not_run", False, MSG_NOT_RUN_BUDGET)
        todo = [i for i, r in enumerate(results) if r is None]
        approvals = _Approvals(self, ctx, [planned[i].call_id for i in todo if _risk_rank(planned[i].risk) > 0])
        flushed, appended = 0, set()
        flush_lock = asyncio.Lock()

        async def flush(final: bool = False) -> None:
            """Append results in call order. Incrementally: the complete prefix, stopping at a call that ended
            dangling (disconnected / cancelled). At the end: everything that finished, still in call order;
            dangling calls stay unanswered on purpose (resume tells the model)."""
            nonlocal flushed
            async with flush_lock:
                if final:
                    batch = [r for r in results if r is not None and not r.dangling and r.planned.call_id not in appended]
                else:
                    batch = []
                    while flushed < len(results) and results[flushed] is not None and not results[flushed].dangling:
                        batch.append(results[flushed])
                        flushed += 1
                if batch:
                    await self._leave_plan_mode(ctx, batch)
                    await self._record(ctx, batch)
                    await self._append(ctx, *[r.item() for r in batch])
                    appended.update(r.planned.call_id for r in batch)

        await approvals.begin()
        sem = asyncio.Semaphore(self.config.max_parallel_calls)
        tasks = {asyncio.ensure_future(self._propose_one(ctx, planned[i], approvals, sem)): i for i in todo}
        watcher = asyncio.ensure_future(self._watch_cancel(ctx)) if tasks else None
        cancel_why = None
        try:
            pending = set(tasks)
            while pending:
                waiting = pending | ({watcher} if watcher is not None and not watcher.done() else set())
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if watcher is not None and watcher in done and cancel_why is None:
                    cancel_why = watcher.result()
                    await self._cancel_calls(ctx)
                for task in done:
                    if task in tasks:
                        pending.discard(task)
                        results[tasks[task]] = task.result()
                await flush()
        finally:
            for task in list(tasks) + ([watcher] if watcher is not None else []):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, *([watcher] if watcher is not None else []), return_exceptions=True)
        if cancel_why is not None:
            raise _CancelRequested(cancel_why)
        if any(r.status == "cancelled" for r in results):
            raise _ExternalCancel()
        await flush(final=True)
        return await self._after_calls(ctx, planned, results)

    async def _after_calls(self, ctx: _Ctx, planned, results) -> "TurnResult | None":
        """The stop checks, in order of how specific they are."""
        if any(r.status == "disconnected" for r in results):
            return await self._pause(ctx, "paused_disconnected", DISCONNECT_REASON)
        if any(r.status == "not_run" for r in results):
            return await self._pause(ctx, "paused_budget", REASON_TOOL_BUDGET, notice=(
                NOTICE_TOOL_BUDGET, "warning", "The run reached its tool-call budget, so it paused."))
        limit = self.config.identical_failure_limit
        for r in results:
            if r.planned.key is not None and ctx.identical.get(r.planned.key, 0) >= limit:
                label = _tool_label(self.catalog, r.planned.name)
                return await self._pause(ctx, "paused_error", REASON_IDENTICAL, notice=(
                    NOTICE_IDENTICAL_FAILURE, "warning",
                    f"The same call to {label} failed {limit} times, so the run paused."))
        if ctx.breaker:
            return await self._pause(ctx, "paused_error", REASON_BREAKER, notice=(
                NOTICE_FAILURE_BREAKER, "warning", MSG_FAILURE_BREAKER))
        return None

    async def _apply_tool_budget(self, ctx: _Ctx, planned: "list[_Planned]") -> None:
        """tool_budget.increment once per call about to be sent; the call that takes the count above the
        threshold, and every call after it, is marked not_run."""
        over = False
        for p in planned:
            if p.refusal is not None:
                continue
            if over:
                p.not_run = True
                continue
            try:
                count = await _blocking(self._budget().increment, ctx.run_id)
            except Exception as exc:                   # a counter that cannot be read never blocks the run
                logger.warning("[loop] tool budget unavailable run=%s error=%s", ctx.run_id, type(exc).__name__)
                count = 0
            if count > self.config.tool_call_threshold:
                over = p.not_run = True
            else:
                ctx.tool_calls += 1

    async def _record(self, ctx: _Ctx, batch: "list[_Result]") -> None:
        """Feed the breaker and the identical-failure counts, in call order."""
        guard = self._run_guard()
        for r in batch:
            if r.status in ("not_run",) or r.dangling:
                continue
            ok = r.status == "ok"
            role = _tool_label(self.catalog, r.planned.name)
            try:
                reason = await _blocking(guard.record_role_outcome, ctx.run_id, ok, role)
            except Exception as exc:
                reason = None
                logger.warning("[loop] failure breaker unavailable run=%s error=%s", ctx.run_id, type(exc).__name__)
            if reason:
                ctx.breaker = reason
            key = r.planned.key
            if key is not None:
                if ok:
                    ctx.identical.pop(key, None)
                elif r.status in FAILING or r.status == "refused":
                    ctx.identical[key] = ctx.identical.get(key, 0) + 1

    async def _propose_one(self, ctx: _Ctx, p: _Planned, approvals: _Approvals, sem: asyncio.Semaphore) -> _Result:
        async with sem:
            if ctx.cancel_sent:
                await approvals.resolve(p.call_id)
                return _Result(p, "cancelled")

            async def on_approval(call_id, decision, scope):
                await approvals.resolve(call_id)

            timeout = self.config.call_timeout_s
            extra = p.tool_input.get("timeout_s")
            if isinstance(extra, int) and not isinstance(extra, bool) and extra > 0:
                timeout = max(timeout, extra + self.config.call_grace_s)
            try:
                outcome = await self._broker.propose(
                    run_id=ctx.run_id, user_id=ctx.user_id, device_id=ctx.device_id, call_id=p.call_id, tool=p.name,
                    tool_input=p.tool_input, risk=p.risk, timeout_s=timeout, on_approval=on_approval)
            except broker_module.TooManyCalls:
                return _Result(p, "error", False, MSG_TOO_MANY_IN_FLIGHT)
            except (broker_module.InvalidCall, broker_module.CallConflict):
                return _Result(p, "error", False, MSG_CALL_NOT_SENT)
            finally:
                await approvals.resolve(p.call_id)
            return self._to_result(p, outcome)

    @staticmethod
    def _to_result(p: _Planned, outcome) -> _Result:
        status = outcome.status
        if status == "ok":
            return _Result(p, "ok", bool(outcome.ok), outcome.output or "", bool(outcome.truncated),
                           outcome.origin if outcome.origin in _DEVICE_TRUST else None, outcome.exit_code)
        if status == "error":
            return _Result(p, "error", False, outcome.output or MSG_TOOL_FAILED, bool(outcome.truncated),
                           outcome.origin if outcome.origin in _DEVICE_TRUST else None, outcome.exit_code)
        if status == "denied":
            return _Result(p, "denied", False, MSG_DENIED)
        if status == "timeout":
            return _Result(p, "timeout", False, MSG_TIMEOUT)
        return _Result(p, status if status in DANGLING else "disconnected")

    # -- cancel ----------------------------------------------------------------------------------------

    async def _cancel_reason(self, ctx: _Ctx) -> "str | None":
        try:
            return await _blocking(self._run_guard().cancel_reason, ctx.run_id) or None
        except Exception:
            return None

    async def _check_cancel(self, ctx: _Ctx) -> None:
        why = await self._cancel_reason(ctx)
        if why:
            raise _CancelRequested(why)

    async def _watch_cancel(self, ctx: _Ctx) -> str:
        while True:
            await asyncio.sleep(self.config.cancel_poll_s)
            why = await self._cancel_reason(ctx)
            if why:
                return why

    async def _cancel_calls(self, ctx: _Ctx) -> None:
        ctx.cancel_sent = True
        try:
            await self._broker.cancel_all(ctx.run_id)
        except Exception as exc:
            logger.error("[loop] cancel_all failed run=%s error=%s", ctx.run_id, type(exc).__name__)

    async def _finish_cancel(self, ctx: "_Ctx | None", why: str) -> TurnResult:
        """The cancel flag was set by someone other than S14's handler: end the calls and the run."""
        if ctx is None:
            return TurnResult("cancelled", REASON_CANCELLED)
        if not ctx.cancel_sent:
            await self._cancel_calls(ctx)
        moved = True
        try:
            await _blocking(self._rm.cancel_run, ctx.user_id, ctx.run_id)
        except run_manager_module.CleanupIncomplete:
            pass                                       # the run IS cancelled; its buffer has a TTL
        except (runs_module.IllegalTransition, runs_module.RunNotFound):
            moved = False                              # S14 (or a sweep) ended it first: it announces
        except Exception as exc:
            moved = False
            logger.error("[loop] could not cancel run=%s error=%s", ctx.run_id, type(exc).__name__)
        if moved and self._emit_final is not None:
            word = why if isinstance(why, str) and _REASON_RE.match(why) else REASON_CANCELLED
            try:
                await self._emit_final(ctx.run_id, "cancelled", word)
            except Exception as exc:
                logger.warning("[loop] final state not announced run=%s error=%s", ctx.run_id, type(exc).__name__)
        logger.info("[loop] turn cancelled run=%s user=%s", ctx.run_id, ctx.user_id)
        return self._result(ctx, "cancelled", REASON_CANCELLED)

    # =========================================================================================
    # State, notices, pauses
    # =========================================================================================

    async def _announce(self, run_id: str, to_state: str, why: "str | None") -> None:
        payload = {"type": "state_changed", "state": to_state}
        if why:
            payload["reason"] = why
        await self._emit(run_id, "run_event", payload)

    async def _notice(self, ctx: _Ctx, code: str, severity: str, text: str) -> None:
        await self._emit(ctx.run_id, "run_event",
                         {"type": "notice", "code": code, "severity": severity, "message": text})

    async def _set_run_state(self, ctx: _Ctx, to_state: str, why: str) -> None:
        """running <-> awaiting_approval, advisory: a refusal (the run was paused or ended meanwhile) or a
        store fault is not the turn's failure."""
        async with ctx.state_lock:
            if ctx.run_state == to_state:
                return
            try:
                # `expected`: the advisory moves are only ever running <-> awaiting_approval. Without it a run
                # that S14 paused meanwhile would be moved back to running by the late resolve.
                came_from = "running" if to_state == "awaiting_approval" else "awaiting_approval"
                await _blocking(self._runs.transition, ctx.user_id, ctx.run_id, to_state, expected=(came_from,))
            except runs_module.RunError as exc:
                logger.info("[loop] advisory state not set run=%s to=%s error=%s", ctx.run_id, to_state, type(exc).__name__)
                return
            ctx.run_state = to_state
            await self._announce(ctx.run_id, to_state, why)

    async def _send_error(self, ctx: _Ctx, code: str, text: str, details: "dict | None" = None) -> None:
        ref = router_module._ref_id(ctx.msg) if isinstance(ctx.msg, dict) else None
        try:
            await ctx.conn.send_error(code, text, ref_id=ref, details=details)
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.error("[loop] could not answer device=%s code=%s error=%s", ctx.device_id, code, type(exc).__name__)

    async def _pause(self, ctx: _Ctx, to_state: str, why: str, *, error_code: "str | None" = None,
                     notice: "tuple | None" = None, error: "tuple | None" = None) -> TurnResult:
        """Park the run: the row (S06), the error and notice for the device, the state in the stream, then
        release the emitter (a paused run holds no emitter state, as after a disconnect)."""
        result = self._result(ctx, to_state, why)
        try:
            await _blocking(self._rm.pause_run, ctx.user_id, ctx.run_id, to_state, error_code=error_code)
        except runs_module.IllegalTransition as refused:
            current = refused.current
            if current == to_state:
                logger.info("[loop] run already %s run=%s", to_state, ctx.run_id)
                return result                          # S14 paused it (a disconnect) and announced it
            if current in runs_module.TERMINAL_STATES:
                return self._result(ctx, "ended")
            logger.warning("[loop] pause refused run=%s from=%s", ctx.run_id, current)
        except runs_module.RunNotFound:
            return self._result(ctx, "ended")
        except Exception as exc:
            logger.error("[loop] could not pause run=%s to=%s error=%s", ctx.run_id, to_state, type(exc).__name__)
        if error is not None:
            await self._send_error(ctx, *error)
        try:
            if notice is not None:
                await self._notice(ctx, *notice)
            await self._announce(ctx.run_id, to_state, why)
        except Exception as exc:
            logger.error("[loop] pause not stored in the replay run=%s error=%s", ctx.run_id, type(exc).__name__)
        finally:
            if self._unbind is not None:
                try:
                    self._unbind(ctx.run_id)
                except Exception as exc:
                    logger.warning("[loop] could not release the emitter run=%s error=%s", ctx.run_id, type(exc).__name__)
        logger.info("[loop] run paused run=%s user=%s to=%s steps=%d", ctx.run_id, ctx.user_id, to_state, ctx.steps)
        return result

    async def _infrastructure_failure(self, ctx: "_Ctx | None", run, exc: BaseException) -> TurnResult:
        """The buffer, the emitter or the run store failed. A run that is over ends the turn quietly; for an open
        run the stream cannot be trusted (emitter docstring), so it goes to paused_error (S07's rule)."""
        logger.error("[loop] infrastructure failure run=%s user=%s error=%s", run.id, run.user_id, type(exc).__name__)
        ctx = ctx or _Ctx(None, run, None, {}, [], Metering())
        if isinstance(exc, (buffer_module.BufferMissing, emitter_module.RunNotBound)):
            try:
                current = await _blocking(self._runs.get_run, run.user_id, run.id)
                if current.terminal:
                    return self._result(ctx, "ended")
            except runs_module.RunNotFound:
                return self._result(ctx, "ended")
            except Exception:
                pass
        if ctx.conn is None:
            return await self._pause(ctx, "paused_error", REASON_INTERNAL, error_code="internal")
        return await self._pause(ctx, "paused_error", REASON_INTERNAL, error_code="internal",
                                 error=("internal", MSG_INTERNAL, None))


async def _close_stream(gen) -> None:
    """Close the adapter's async generator (which closes the provider connection), even when this task is being
    cancelled, then let the cancellation through."""
    close = getattr(gen, "aclose", None)
    if close is None:
        return
    work = asyncio.ensure_future(close())
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        if not work.done():
            await asyncio.wait({work})
        if not work.cancelled():
            work.exception()
        raise
    except Exception as exc:
        logger.warning("[loop] stream close failed error=%s", type(exc).__name__)


# ---------------------------------------------------------------------------------------------
# The process-wide loop and the (unwired) installation
# ---------------------------------------------------------------------------------------------

_LOOP: "AgentLoop | None" = None


def build_default_loop() -> AgentLoop:
    """The loop with the real collaborators: the broker (S09), the process emitter (S14), the hello.py chain.
    The emitter is looked up at every call, like the broker does, so a test or S11 may swap it."""

    async def emit(run_id, t, payload):
        return await emitter_module.get_emitter().emit(run_id, t, payload)

    async def emit_final(run_id, to_state, why=None):
        return await emitter_module.get_emitter().emit_final(run_id, to_state, why)

    def unbind(run_id):
        emitter_module.get_emitter().unbind(run_id)

    return AgentLoop(chain=default_chain(), broker=broker_module.get_broker(), emit=emit, emit_final=emit_final,
                     unbind=unbind)


def get_loop() -> AgentLoop:
    global _LOOP
    if _LOOP is None:
        _LOOP = build_default_loop()
    return _LOOP


def set_loop(loop: "AgentLoop | None") -> None:
    """Install the process-wide loop (tests, and S10's wiring with a real Metering); None forgets it."""
    global _LOOP
    _LOOP = loop


async def run_turn(conn, run, msg) -> TurnResult:
    """S14's turn runner, exactly its contract: fn(conn, run, msg). Delegates to the process-wide loop."""
    return await get_loop().run_turn(conn, run, msg)


def install(replace: bool = True, *, handlers=None, loop: "AgentLoop | None" = None) -> AgentLoop:
    """Register the loop with S14: the turn runner (replace=True, so it may take hello.py's place), and the
    loop's resume, cancel and disconnect hooks. Idempotent. Patch S15's wiring.install() is what server.py
    calls (module docstring, "WIRING"). `handlers` defaults to the process-wide run_handlers."""
    if loop is not None:
        set_loop(loop)
    active = get_loop()
    target = handlers if handlers is not None else run_handlers.get_handlers()
    target.set_turn_runner(run_turn, replace=replace)
    target.add_on_resume(active.hooks["on_resume"])
    target.add_on_cancel(active.hooks["on_cancel"])
    target.add_on_run_disconnect(active.hooks["on_run_disconnect"])
    return active
