"""
codeagent/model_adapter.py -- the provider-neutral model interface (patch S05).

WHAT THIS IS. The M2C agent loop (S07, a later patch) never talks to a model
provider. It builds a `ModelRequest`, hands it to a `ModelAdapter`, and reads a
stream of three kinds of event back:

    TextDelta   {text}                              a piece of the visible answer
    ToolCall    {id, name, arguments}               one complete tool call
    Usage       {input, cached_input, output}       what the call cost, always LAST

Everything provider-specific (wire format, SDKs, key selection, cooldowns, quirks)
lives behind that line, in codeagent/adapters/. The first adapter is OpenAI-
compatible chat completions with native tool use (decision D3):
codeagent/adapters/openai_compat.py.

This module is deliberately cheap to import. It pulls in only utils.llm_errors and
utils.error_sanitizer (both import nothing heavy): no OpenAI SDK, no Redis, no
utils.llm_client. A test in tests/unit/test_codeagent_adapter.py pins that.

WHAT AN ADAPTER GUARANTEES (the contract S06/S07 may rely on)
  * Events arrive as the provider produces them. Text is streamed; a tool call is
    emitted once, complete, after the provider has finished writing it (its
    arguments only exist as JSON once the last fragment is in).
  * `Usage` is the last event of every successful stream. If the provider reported
    nothing, it is zeros with reported=False: metering fails open (build plan 0.2),
    so a missing number never breaks a run, and S10 can tell "zero" from "unknown".
    `Usage.stop_reason` is one of end_turn / tool_use / max_tokens, the same words
    as the protocol's run_event text_done.
  * A ToolCall is either usable (`arguments` is a dict, `error` is None) or it is
    not (`arguments` is None, `error` says why). That is enforced in __post_init__,
    so there is no "half valid" call to dispatch by accident. Security checks fail
    closed (build plan 0.2, feature guide 5.2: "unknown tools and malformed
    arguments fail closed"). The adapter never completes a truncated value, never
    runs a call it could not parse, and never invents a tool name. Error codes:
        malformed_arguments   arguments are not a JSON object, even after repair
        truncated_arguments   the reply hit max_tokens and the JSON was not complete
        unknown_tool          the name is not one of the tools offered this turn
        missing_name          the provider sent a call with no name
    The loop answers such a call with an error tool_result so the model can try
    again; it never dispatches it. Schema validation of a *usable* call (required
    arguments, closed schemas) is S09/the client's job (protocol validate_tool_input);
    the adapter only guarantees the JSON shape.
  * `ToolCall.repair` says how a usable call was obtained when it was not a clean
    native call: "repaired" (cosmetic JSON damage fixed, or a namespaced name
    cleaned), "salvaged:<format>" (the model wrote the call as text), "rescued:<format>"
    (the provider rejected the call but handed back what the model wrote). Clean
    native calls have repair=None. The smoke script reports on this field.
  * Errors are raised as `ModelAdapterError`, never as a provider SDK exception.
    It carries the `utils.llm_errors.ErrorBucket` (via classify_error), the HTTP
    status, a retry-after if the provider gave one, whether any output had already
    been delivered (`emitted_output`: if so the caller must not silently retry on
    another provider), and a SANITIZED message. The original exception stays on
    `__cause__` for server-side logging only. Build plan 11.4: prompts, paths, tool
    output and provider error text never go into a message, a log line or a metric.
  * Cancelling the consumer (closing the async generator, or cancelling the task
    that iterates it) stops the provider call: the HTTP stream is closed and the
    worker thread exits.

CONTENT HANDLING. `TextDelta.text`, `ToolCall.arguments`, `ToolCall.raw_arguments`
and `Message.content` are CONTENT in the protocol's sense (protocol/README.md,
section 6): they may be held only in the run buffer and must never reach Postgres,
logs or metrics. `Usage` and the error fields are metadata.

TOOL DEFINITIONS ARE DATA. Nothing here (or in any adapter) names a tool. A
`ToolSpec` is built from a protocol/v1/tools/<name>.json definition
(`ToolSpec.from_definition`, `load_tool_definitions`) and the adapter converts it
to its provider's `tools` format. Adding a tool to the protocol needs no change here.

NOT HERE (other work packages): the agent loop and retries (S07), credit weighting
(S02) and ledger commits (S10: the adapter reports Usage, it does not write the
ledger and does not call utils.llm_client's generate_text/stream_completion, so it
does not appear in tests/static/test_usage_ledger_call_sites.py), the run buffer
(S06), the tool broker (S09), context assembly and compaction (S08, L03).
"""
from __future__ import annotations

import abc
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, ClassVar, Mapping, Union

from utils.error_sanitizer import user_facing_message
from utils.llm_errors import ErrorBucket, classify_error, is_zero_quota

# protocol/v1/common.json: call_id and tool_name. Kept in step by a test (test_codeagent_adapter.py reads the
# real schema); duplicated here as constants only because the server image does not ship protocol/.
PROTOCOL_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROTOCOL_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")

TOOL_CALL_ERRORS = ("malformed_arguments", "truncated_arguments", "unknown_tool", "missing_name")
STOP_REASONS = ("end_turn", "tool_use", "max_tokens")
ROLES = ("system", "user", "assistant", "tool")
TOOL_CHOICES = ("auto", "none", "required")


# ---------------------------------------------------------------------------------------------
# tool definitions (data, not code)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """What a model needs to know about one tool: name, description, JSON Schema for the input. The protocol's own
    fields `version`, `risk` and `risk_max` are M2C's, not the provider's, and are not carried here."""
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self):
        if not isinstance(self.name, str) or not PROTOCOL_TOOL_NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name: {self.name!r}")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError(f"tool {self.name}: description must be a non-empty string")
        if not isinstance(self.input_schema, Mapping):
            raise ValueError(f"tool {self.name}: input_schema must be an object")

    @classmethod
    def from_definition(cls, definition: Mapping[str, Any]) -> "ToolSpec":
        """From a protocol/v1/tools/<name>.json document (or anything shaped like one)."""
        if not isinstance(definition, Mapping):
            raise ValueError("a tool definition must be an object")
        return cls(name=definition.get("name"), description=definition.get("description"),
                   input_schema=definition.get("input_schema"))


def default_tools_dir() -> pathlib.Path:
    """protocol/v1/tools next to backend/ in a source checkout. The server image copies backend/ only, so
    deployment code (S04/S09) must pass the directory it ships; this default is for development and tests."""
    return pathlib.Path(__file__).resolve().parents[2] / "protocol" / "v1" / "tools"


def load_tool_definitions(directory: "str | pathlib.Path | None" = None) -> "tuple[ToolSpec, ...]":
    """Every *.json in `directory` as a ToolSpec, sorted by name. FileNotFoundError if the directory is missing
    or empty: an agent with no tools by accident must be loud, not a silent chat-only run."""
    root = pathlib.Path(directory) if directory is not None else default_tools_dir()
    files = sorted(root.glob("*.json")) if root.is_dir() else []
    if not files:
        raise FileNotFoundError(f"no tool definitions found in {root}")
    specs = [ToolSpec.from_definition(json.loads(p.read_text(encoding="utf-8"))) for p in files]
    return tuple(sorted(specs, key=lambda s: s.name))


# ---------------------------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TextDelta:
    """A piece of the visible answer (content)."""
    type: ClassVar[str] = "text_delta"
    text: str

    def as_dict(self) -> dict:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True)
class ToolCall:
    """One complete tool call (content). Usable iff `error` is None, in which case `arguments` is a dict; else
    `arguments` is None. `raw_arguments` is exactly what the model wrote, for the error message the loop sends
    back; treat it as content."""
    type: ClassVar[str] = "tool_call"
    id: str
    name: str
    arguments: "dict | None"
    raw_arguments: str = ""
    repair: "str | None" = None
    error: "str | None" = None

    def __post_init__(self):
        if self.error is not None and self.error not in TOOL_CALL_ERRORS:
            raise ValueError(f"unknown tool-call error code: {self.error!r}")
        if (self.arguments is None) != (self.error is not None):
            raise ValueError("a ToolCall has arguments exactly when it has no error")
        if self.arguments is not None and not isinstance(self.arguments, dict):
            raise ValueError("ToolCall.arguments must be a dict")

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        return {"type": self.type, "id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class Usage:
    """What one model call cost (metadata). Always the last event. `reported` is False when the provider sent no
    usage (then the three numbers are 0). `model` is the model that served the call, `provider` the adapter's
    provider name. `finish_reason` is the provider's own word; `stop_reason` is the neutral one."""
    type: ClassVar[str] = "usage"
    input: int = 0
    cached_input: int = 0
    output: int = 0
    reported: bool = True
    model: "str | None" = None
    provider: "str | None" = None
    stop_reason: "str | None" = None
    finish_reason: "str | None" = None

    def as_dict(self) -> dict:
        return {"type": self.type, "input": self.input, "cached_input": self.cached_input, "output": self.output}


ModelEvent = Union[TextDelta, ToolCall, Usage]


# ---------------------------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """One conversation message (content). An assistant message may carry the ToolCall events it produced; a
    tool message answers one call by id."""
    role: str
    content: str = ""
    tool_calls: "tuple[ToolCall, ...]" = ()
    tool_call_id: "str | None" = None

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"invalid message role: {self.role!r}")
        if not isinstance(self.content, str):
            raise ValueError("message content must be a string")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("a tool message needs tool_call_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only an assistant message carries tool_calls")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))

    @classmethod
    def system(cls, text: str) -> "Message":
        return cls("system", text)

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls("user", text)

    @classmethod
    def assistant(cls, text: str = "", tool_calls: "tuple[ToolCall, ...]" = ()) -> "Message":
        return cls("assistant", text, tuple(tool_calls))

    @classmethod
    def tool(cls, call_id: str, content: str) -> "Message":
        return cls("tool", content, (), call_id)


@dataclass(frozen=True)
class ModelRequest:
    """One model call. The adapter, not the request, decides which model answers (it may walk a chain of them).

    tool_choice     "auto", "none", "required", or the name of one offered tool.
    provider_options  extra provider-specific request fields, passed through as-is (e.g. {"reasoning_effort": "low"});
                    only for knobs the neutral request does not model.
    timeout_s       per-read timeout for the provider call (an idle timeout, not a total one)."""
    messages: "tuple[Message, ...]"
    tools: "tuple[ToolSpec, ...]" = ()
    max_tokens: "int | None" = None
    temperature: "float | None" = None
    tool_choice: str = "auto"
    provider_options: "Mapping[str, Any] | None" = None
    timeout_s: "float | None" = None

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.messages:
            raise ValueError("a request needs at least one message")
        if self.tool_choice not in TOOL_CHOICES and self.tool_choice not in {t.name for t in self.tools}:
            raise ValueError(f"tool_choice must be one of {TOOL_CHOICES} or an offered tool name, "
                             f"got {self.tool_choice!r}")


@dataclass(frozen=True)
class Completion:
    """A whole stream, collected (for tests, the smoke script and callers that do not need to stream)."""
    text: str = ""
    tool_calls: "tuple[ToolCall, ...]" = ()
    usage: "Usage | None" = None
    events: "tuple[ModelEvent, ...]" = field(default=(), repr=False)


# ---------------------------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------------------------

def _status_code(exc: BaseException) -> "int | None":
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after(exc: BaseException) -> "float | None":
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
        return max(float(value), 0.0) if value else None
    except (TypeError, ValueError, AttributeError):
        return None                                   # an HTTP-date, or something else we do not parse


class ModelAdapterError(RuntimeError):
    """A model call failed. See the module docstring for the contract.

    bucket           utils.llm_errors.ErrorBucket (the recovery table in that module's docstring applies unchanged)
    code             a more specific stable string where the bucket is not enough: "tool_use_failed" (the model
                     wrote an unusable call and the provider rejected it; sampling is random, so the loop may
                     retry once or twice with a nudge), "stream_incomplete" (the stream ended before the model
                     finished), "provider_unavailable" (no configured provider could be tried)
    status_code      HTTP status, if there was a response
    retry_after_s    the provider's Retry-After, if it sent a number
    zero_quota       a 429 whose body says the quota is zero: waiting never helps (llm_errors.is_zero_quota)
    emitted_output   True once any event had been yielded: the caller must not silently restart the answer elsewhere
    provider, model  which step failed (for metrics; both are configuration, not content)

    str(error) is a fixed, generic, user-safe sentence (utils.error_sanitizer). The provider's own text is NOT in
    it, because it can echo prompt content; the original exception is `__cause__`."""

    def __init__(self, bucket: ErrorBucket, *, code: "str | None" = None, status_code: "int | None" = None,
                 retry_after_s: "float | None" = None, zero_quota: bool = False, provider: "str | None" = None,
                 model: "str | None" = None, emitted_output: bool = False, message: "str | None" = None):
        self.bucket = bucket
        self.code = code
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.zero_quota = zero_quota
        self.provider = provider
        self.model = model
        self.emitted_output = emitted_output
        self.public_message = message or user_facing_message(_BucketOnly(bucket))
        super().__init__(self.public_message)

    @classmethod
    def from_exception(cls, exc: BaseException, *, provider: "str | None" = None, model: "str | None" = None,
                       emitted_output: bool = False, code: "str | None" = None) -> "ModelAdapterError":
        bucket = classify_error(exc)
        try:
            zero = bucket is ErrorBucket.RATE_LIMIT_WINDOW and is_zero_quota(exc)
        except Exception:
            zero = False
        return cls(bucket, code=code, status_code=_status_code(exc), retry_after_s=_retry_after(exc),
                   zero_quota=zero, provider=provider, model=model, emitted_output=emitted_output,
                   message=user_facing_message(exc))

    def __repr__(self) -> str:
        return (f"ModelAdapterError(bucket={self.bucket.name}, code={self.code!r}, status={self.status_code}, "
                f"provider={self.provider!r}, model={self.model!r}, emitted_output={self.emitted_output})")


class _BucketOnly(Exception):
    """Lets user_facing_message() pick the generic sentence for a bucket when there is no underlying exception.
    classify_error() reads status_code / body, so each bucket is given the marker that lands in it."""
    _STATUS = {ErrorBucket.CONTEXT_LENGTH_EXCEEDED: 400, ErrorBucket.RATE_LIMIT_WINDOW: 429,
               ErrorBucket.MALFORMED_REQUEST: 400, ErrorBucket.PERMANENT_AUTH: 401,
               ErrorBucket.TRANSIENT_NETWORK: 503}

    def __init__(self, bucket: ErrorBucket):
        text = {ErrorBucket.CONTEXT_LENGTH_EXCEEDED: "maximum context length"}.get(bucket, "")
        super().__init__(text)
        self.status_code = self._STATUS[bucket]


# ---------------------------------------------------------------------------------------------
# the interface
# ---------------------------------------------------------------------------------------------

class ModelAdapter(abc.ABC):
    """A model provider behind the neutral interface. Implementations are async generators:

        async for event in adapter.stream(request): ...

    and raise ModelAdapterError (only). One adapter instance may serve many concurrent streams."""

    @abc.abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise NotImplementedError


async def collect(adapter: ModelAdapter, request: ModelRequest) -> Completion:
    """Run one request to the end and gather it. Raises exactly what `adapter.stream` raises."""
    text, calls, usage, events = [], [], None, []
    async for ev in adapter.stream(request):
        events.append(ev)
        if isinstance(ev, TextDelta):
            text.append(ev.text)
        elif isinstance(ev, ToolCall):
            calls.append(ev)
        elif isinstance(ev, Usage):
            usage = ev
    return Completion("".join(text), tuple(calls), usage, tuple(events))
