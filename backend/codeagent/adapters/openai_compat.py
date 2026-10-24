"""
codeagent/adapters/openai_compat.py -- OpenAI-compatible chat completions with native tool use (patch S05,
decision D3). Streams `text_delta`, `tool_call` and `usage` events (codeagent/model_adapter.py).

WHICH PROVIDERS. Anything that speaks OpenAI's /chat/completions with `tools` and `stream=True`: Groq, Mistral,
Gemini's OpenAI layer, the Hugging Face router, OpenRouter, or any other base URL. These are exactly the providers
utils/llm_client.py already has client factories for, and this adapter reuses those (see KEY SELECTION).

HOW A CALL RUNS
  * The request goes out with `stream=True` and `stream_options={"include_usage": True}`; if an SDK version rejects
    the second kwarg (TypeError) it is retried without, and usage then comes from wherever the provider puts it or
    is reported as unreported (never an error: metering fails open).
  * The provider SDKs here are synchronous. A daemon worker thread iterates the SDK stream and hands chunks to the
    event loop through an asyncio.Queue (the same bridge utils/llm_client.py's _walk_chain_once_stream uses, but one
    dedicated thread per stream instead of the shared executor, so a long stream can never starve other work and
    cannot keep the interpreter alive at shutdown). All parsing happens on the loop side, single-threaded.
  * Closing the async generator, or cancelling the task that iterates it, sets a stop flag and closes the HTTP
    stream; the worker exits. That is how a run's `cancel` reaches the provider.
  * SDK-level retries are switched off (`with_options(max_retries=0)`): the SDK would otherwise sleep and retry on
    429/5xx inside the call, hiding the failure from the cooldown logic below. Retry policy belongs to the loop (S07).

TOOL-CALL ASSEMBLY. Fragments are accumulated per tool call and emitted once, complete, after the stream has ended.
  * Calls are keyed by the delta's `index`, so parallel calls whose fragments interleave stay apart. A fragment that
    arrives at an index already holding a call with a *different* id starts a new call: Gemini's OpenAI layer sends
    every parallel call whole at index 0.
  * Provider ids are kept when they already satisfy the protocol's call_id pattern and are unique within the reply;
    otherwise (missing, empty, duplicate, too long, odd characters) a fresh `call_<hex>` id is minted.
  * Name and arguments go through codeagent/adapters/toolcalls.py (ported from MiniLocker): cosmetic JSON damage is
    repaired (and marked `repair="repaired"`), anything it cannot recover safely becomes a ToolCall with an `error`
    and no arguments. See model_adapter.py for the error codes and why this fails closed. One extra rule here, which
    MiniLocker did not need because it never streamed: when the reply ended with finish_reason "length" (max_tokens),
    arguments that are not complete JSON as sent are refused as `truncated_arguments`, even if the parser could close
    the braces. The model did not finish writing that call; closing it would run something it never wrote.
  * The names the model may call are the tools in *this* request. Anything else is `unknown_tool`.

CALLS WRITTEN AS TEXT. Some models write the call as text (Hermes `<tool_call>`, `<function=...>`, harmony, bare JSON,
ReAct) instead of using the tool-calls field. When no native call was made, tools were offered, and
`salvage_text_calls` is on (default), the reply text is run through toolcalls.salvage() once the stream ends, under
MiniLocker's conservative rules (only text that *consists of* markup counts; prose quoting a call never does; reasoning
blocks are ignored; all-or-nothing). To keep the markup out of the user's view, text that could be the start of such a
call is held back until the stream ends: a reply that begins with `{`, `[`, a code fence, `Thought:` or `Action:`, and
anything from the first `<tool_call`, `<function=`, `<|` or `to=functions.` on. If it turns out not to be a call, the
held text is delivered in full at the end, so nothing is ever lost, only delayed. Recovered calls carry
`repair="salvaged:<format>"`.

GROQ'S tool_use_failed. Groq answers HTTP 400 `tool_use_failed` with a `failed_generation` field when a model writes a
malformed call. If that text is a clear call (parsed with strict=False, because the provider has already said it was an
attempted call) it is returned as a ToolCall with `repair="rescued:<format>"` and an unreported Usage. If not, a
ModelAdapterError with code "tool_use_failed" is raised so the loop can retry with a nudge. (Same approach as
MiniLocker 54520a2, client.py `_rescue`; written fresh here for the streaming path.)

KEY SELECTION AND COOLDOWNS (reused from utils/llm_client.py, imported read-only, never copied or edited)
  * A chain step is {"provider", "model", "key_env", optional "timeout"}, the shape every CHAIN in the repo uses.
    For each stream() the steps are tried in order. A step is skipped, without a request, when its key is cooling
    down (llm_client._is_cooling_down(provider, key_env, model)) or its environment variable is unset (the
    per-provider factory, _get_groq / _get_mistral / _get_gemini / _get_huggingface / _get_openrouter, returns None).
  * On a failure BEFORE any output was delivered, the policy mirrors llm_client._handle_transient_error:
        PERMANENT_AUTH        _set_cooldown (the 6 h key-wide cooldown), next step
        RATE_LIMIT_WINDOW     per-(key, model) cooldown for the provider's retry-after, clamped to the same
                              LLM_RATE_LIMIT_COOLDOWN_MIN/MAX bounds (or the zero-quota duration), next step
        TRANSIENT_NETWORK     next step, no cooldown (a blip is not a verdict on the key; phase 3e)
        MALFORMED_REQUEST     raise: our payload is wrong (or the model wrote a bad call); the next provider would
        CONTEXT_LENGTH_EXCEEDED  fail the same way. Trimming is the context assembler's job (S08), not this adapter's.
    Once output has been delivered nothing falls through: ModelAdapterError(emitted_output=True) is raised.
  * Cooldown writes fail open (llm_client's helpers already swallow their own errors; anything else is logged and
    ignored). A single-endpoint adapter built from a client or base URL has no key_env and therefore no cooldown
    bookkeeping.

NOT DONE HERE: no prompt-size shrinking, no "sleep and retry", no continuation of truncated text (all loop-level
decisions, S07/S08), no ledger write (S10 commits what Usage reports). Logging never includes prompts, arguments,
paths, tool output or provider error text; only provider, model, status, bucket and counts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Sequence

from codeagent.adapters import toolcalls
from codeagent.model_adapter import (
    PROTOCOL_CALL_ID_RE,
    Message,
    ModelAdapter,
    ModelAdapterError,
    ModelEvent,
    ModelRequest,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)
from utils.llm_errors import ErrorBucket, is_zero_quota

log = logging.getLogger("codeagent.adapter")

# provider name (as used in every CHAIN step) -> name of llm_client's client factory. Resolved with getattr at call
# time, so the factories stay llm_client's (no copy) and a renamed one fails a test instead of silently going stale.
PROVIDER_FACTORIES = {
    "groq": "_get_groq",
    "mistral": "_get_mistral",
    "gemini": "_get_gemini",
    "huggingface": "_get_huggingface",
    "openrouter": "_get_openrouter",
}

# Buckets for which the next provider would fail exactly like this one (llm_errors.py recovery table).
_NO_FALLBACK = (ErrorBucket.MALFORMED_REQUEST, ErrorBucket.CONTEXT_LENGTH_EXCEEDED)

_THREAD_NAME = "codeagent-stream"


def _llm_client():
    """utils.llm_client, imported on first use: it drags in Redis, the DB layer and every provider SDK, which the
    neutral modules and the tests of pure helpers must not pay for."""
    from utils import llm_client
    return llm_client


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-key read that tolerates SDK models, SimpleNamespaces and plain dicts."""
    if obj is None:
        return default
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, Mapping):
        value = obj.get(name)
    return default if value is None else value


# ---------------------------------------------------------------------------------------------
# request conversion
# ---------------------------------------------------------------------------------------------

def to_openai_tool(spec: ToolSpec) -> dict:
    """ToolSpec -> the provider's `tools` entry. The protocol's input_schema is handed over unchanged: it uses only
    the keyword subset every provider accepts (protocol/README.md, section 7)."""
    return {"type": "function",
            "function": {"name": spec.name, "description": spec.description, "parameters": dict(spec.input_schema)}}


def wire_call_id(provider: str, call_id: str) -> str:
    """The id as the provider wants to see it in a conversation history. Mistral insists on exactly nine
    alphanumeric characters; an id of that shape passes through, any other is mapped to a stable nine-character
    digest (the same input always gives the same output, so an assistant call and its tool result still match).
    Every other provider gets the id untouched. (Mistral's rule is documented behaviour that could not be exercised
    against the live API in the environment this patch was written in; remapping is harmless if a provider is lenient.)"""
    if provider == "mistral" and not re.fullmatch(r"[A-Za-z0-9]{9}", call_id):
        return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:9]
    return call_id


def _echo_arguments(call: ToolCall) -> str:
    """Arguments of a past call as a JSON string. A call that never parsed is echoed as {} rather than as the broken
    text it was: strict providers reject a history containing invalid JSON, and the loop has already told the model
    what went wrong in the tool result."""
    if call.arguments is not None:
        return json.dumps(call.arguments, ensure_ascii=False)
    return "{}"


def to_openai_messages(messages: Sequence[Message], provider: str = "openai_compat") -> list:
    out = []
    for m in messages:
        if m.role == "assistant":
            d: dict = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                d["tool_calls"] = [{"id": wire_call_id(provider, c.id), "type": "function",
                                    "function": {"name": c.name, "arguments": _echo_arguments(c)}}
                                   for c in m.tool_calls]
            out.append(d)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": wire_call_id(provider, m.tool_call_id or ""),
                        "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _tool_choice(choice: str):
    return choice if choice in ("auto", "none", "required") else {"type": "function", "function": {"name": choice}}


# ---------------------------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------------------------

def cached_tokens(usage: Any) -> int:
    """Cached prompt tokens from a provider usage object or dict; 0 when the provider does not say. Reads the same
    two places eo.usage_ledger.cached_tokens_from_usage does (S01) -- `prompt_tokens_details.cached_tokens`, then a
    flat `cached_tokens` -- and a test pins the two to agree."""
    if usage is None:
        return 0
    details = _g(usage, "prompt_tokens_details")
    cached = _g(details, "cached_tokens") if details is not None else None
    if cached is None:
        cached = _g(usage, "cached_tokens")
    try:
        return max(int(cached), 0) if cached is not None else 0
    except (TypeError, ValueError):
        return 0


def _int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _usage_of(chunk: Any) -> Any:
    """The usage object on a stream chunk: `usage` (OpenAI, Mistral, most others) or `x_groq.usage` (Groq)."""
    u = _g(chunk, "usage")
    if u is None:
        u = _g(_g(chunk, "x_groq"), "usage")
    return u


# ---------------------------------------------------------------------------------------------
# text held back while it might be a tool call
# ---------------------------------------------------------------------------------------------

_MARKER_RE = re.compile(r"<tool_call|<function=|<\||to=functions\.", re.I)
_MARKER_LITERALS = ("<tool_call", "<function=", "<|", "to=functions.")
_START_TOKENS = ("{", "[", "```", "Thought:", "Action:")


class _Holdback:
    """Decides which streamed text can go to the user now and which must wait for the end of the stream.

    Two triggers (see the module docstring): the reply as a whole starts like a call (a bare JSON object or array, a
    code fence, a ReAct `Thought:`/`Action:` line), or a call marker turns up later in the text. From the trigger on,
    everything is held. Text that merely *might* turn into a marker (a chunk ending in `<tool_`) is held back only as
    long as it could still become one. `held_text()` is what is still withheld at the end."""

    def __init__(self):
        self.pending = ""        # not yet emitted, not yet condemned (an undecided prefix)
        self.held = ""           # withheld for good until the end of the stream
        self.holding = False
        self.emitted = False

    def feed(self, s: str) -> str:
        if self.holding:
            self.held += s
            return ""
        self.pending += s
        if not self.emitted:
            lead = self.pending.lstrip()
            if not lead:
                return ""
            if lead.startswith(_START_TOKENS):
                return self._hold_all()
            if any(tok.startswith(lead) for tok in _START_TOKENS):      # "Tho": might still become "Thought:"
                return ""
        m = _MARKER_RE.search(self.pending)
        if m:
            out, self.held, self.pending, self.holding = self.pending[:m.start()], self.pending[m.start():], "", True
            return self._release(out)
        keep = self._partial_marker_len(self.pending)
        cut = len(self.pending) - keep
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return self._release(out)

    def _hold_all(self) -> str:
        self.held, self.pending, self.holding = self.pending, "", True
        return ""

    def _release(self, out: str) -> str:
        if out:
            self.emitted = True
        return out

    @staticmethod
    def _partial_marker_len(text: str) -> int:
        tail = text[-(max(map(len, _MARKER_LITERALS)) - 1):].lower()
        best = 0
        for lit in _MARKER_LITERALS:
            for k in range(min(len(lit) - 1, len(tail)), 0, -1):
                if tail.endswith(lit[:k]):
                    best = max(best, k)
                    break
        return best

    def held_text(self) -> str:
        return self.pending + self.held


# ---------------------------------------------------------------------------------------------
# one stream's state
# ---------------------------------------------------------------------------------------------

@dataclass
class _Slot:
    seq: int
    index: int
    id: str = ""
    name: str = ""
    args: list = field(default_factory=list)
    args_obj: Any = None


class _IdIssuer:
    """Hands out call ids that satisfy the protocol and are unique within one reply."""

    def __init__(self):
        self.used: set = set()

    def issue(self, proposed: "str | None") -> str:
        if proposed and PROTOCOL_CALL_ID_RE.match(proposed) and proposed not in self.used:
            self.used.add(proposed)
            return proposed
        while True:
            fresh = "call_" + uuid.uuid4().hex[:24]
            if fresh not in self.used:
                self.used.add(fresh)
                return fresh


def _strict_json_object(raw: str) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except (TypeError, ValueError):
        return False


class _StreamState:
    """Accumulates one provider stream and turns it into neutral events. Not thread-safe; used from the loop only."""

    def __init__(self, endpoint: "Endpoint", request: ModelRequest, salvage_text_calls: bool):
        self.endpoint = endpoint
        self.offered = tuple(t.name for t in request.tools)
        self.openai_tools = [to_openai_tool(t) for t in request.tools]
        self.holdback = _Holdback() if (salvage_text_calls and request.tools) else None
        self.text_parts: list = []
        self.slots: list = []
        self.by_index: dict = {}
        self.finish_reason: "str | None" = None
        self.usage: Any = None
        self.served_model: "str | None" = None
        self.saw_chunk = False
        self.ids = _IdIssuer()
        self.native_seen = False

    # -- chunks ---------------------------------------------------------------------------------

    def feed(self, chunk: Any) -> list:
        self.saw_chunk = True
        out: list = []
        u = _usage_of(chunk)
        if u is not None:
            self.usage = u
        model = _g(chunk, "model")
        if model and not self.served_model:
            self.served_model = str(model)
        choices = _g(chunk, "choices") or []
        if not choices:
            return out
        choice = choices[0]
        fr = _g(choice, "finish_reason")
        if fr:
            self.finish_reason = str(fr)
        delta = _g(choice, "delta")
        text = _g(delta, "content")
        if isinstance(text, str) and text:
            self.text_parts.append(text)
            shown = self.holdback.feed(text) if self.holdback else text
            if shown:
                out.append(TextDelta(shown))
        for frag in _g(delta, "tool_calls") or []:
            self._tool_fragment(frag)
        return out

    def _tool_fragment(self, frag: Any) -> None:
        self.native_seen = True
        idx, tid = _g(frag, "index"), _g(frag, "id")
        fn = _g(frag, "function")
        name, args = _g(fn, "name"), _g(fn, "arguments")
        slot = None
        if isinstance(idx, int):
            slot = self.by_index.get(idx)
            if slot is not None and tid and slot.id and tid != slot.id:
                slot = None                                   # same index, new id: a new call (Gemini)
        elif not tid and self.slots:
            slot = self.slots[-1]                             # no index and no id: continues the latest call
        if slot is None:
            slot = _Slot(seq=len(self.slots), index=idx if isinstance(idx, int) else len(self.slots))
            self.slots.append(slot)
            if isinstance(idx, int):
                self.by_index[idx] = slot
        if tid and not slot.id:
            slot.id = str(tid)
        if isinstance(name, str) and name:
            if not slot.name:
                slot.name = name
            elif name != slot.name and not slot.name.endswith(name):
                slot.name += name                             # a name split over fragments
        if isinstance(args, str):
            slot.args.append(args)
        elif isinstance(args, Mapping):
            slot.args_obj = dict(args)

    # -- end of stream --------------------------------------------------------------------------

    def finish(self) -> list:
        """Everything that was waiting for the end: held text, complete tool calls, usage."""
        out: list = []
        withheld = self.holdback.held_text() if self.holdback else ""
        calls: list = []
        if self.slots:
            calls = [self._native_call(s) for s in sorted(self.slots, key=lambda s: (s.index, s.seq))]
            if withheld:
                out.append(TextDelta(withheld))
        elif self.holdback is not None and self.holdback.holding and withheld:
            got = toolcalls.salvage("".join(self.text_parts), self.openai_tools)
            if got:
                calls = [self._recovered(c, "salvaged", got.how) for c in got.calls]
            else:
                out.append(TextDelta(withheld))
        elif withheld:
            out.append(TextDelta(withheld))
        out.extend(calls)
        out.append(self.usage_event(any(isinstance(c, ToolCall) for c in calls)))
        return out

    def _native_call(self, slot: _Slot) -> ToolCall:
        call_id = self.ids.issue(slot.id)
        raw = json.dumps(slot.args_obj, ensure_ascii=False) if slot.args_obj is not None and not slot.args else "".join(slot.args)
        raw_name = (slot.name or "").strip()
        if not raw_name:
            return ToolCall(call_id, "", None, raw, error="missing_name")
        name = toolcalls.norm_name(raw_name, self.offered)
        if name is None:
            return ToolCall(call_id, toolcalls.norm_name(raw_name, None) or raw_name, None, raw, error="unknown_tool")
        if self.finish_reason == "length" and not _strict_json_object(raw):
            return ToolCall(call_id, name, None, raw, error="truncated_arguments")
        args, repaired = toolcalls.parse_arguments(raw)
        if args is None:
            return ToolCall(call_id, name, None, raw, error="malformed_arguments")
        changed = repaired or name != raw_name
        return ToolCall(call_id, name, args, raw, repair="repaired" if changed else None)

    def _recovered(self, call: "toolcalls.Call", kind: str, how: str) -> ToolCall:
        return ToolCall(self.ids.issue(call.id), call.name, dict(call.args),
                        json.dumps(call.args, ensure_ascii=False), repair=f"{kind}:{how}")

    def rescue(self, failed_generation: str) -> "list | None":
        """Events for a provider-rejected generation that is a clear call, else None (see tool_use_failed above)."""
        got = toolcalls.salvage(failed_generation, self.openai_tools, strict=False)
        if not got:
            return None
        calls = [self._recovered(c, "rescued", got.how) for c in got.calls]
        self.finish_reason = "tool_use_failed"
        return [*calls, self.usage_event(True, reported=False)]

    def usage_event(self, emitted_calls: bool, *, reported: "bool | None" = None) -> Usage:
        u = self.usage
        prompt, completion = _g(u, "prompt_tokens"), _g(u, "completion_tokens")
        known = u is not None and (prompt is not None or completion is not None)
        if reported is None:
            reported = known
        if self.finish_reason == "length":
            stop = "max_tokens"
        elif emitted_calls:
            stop = "tool_use"
        else:
            stop = "end_turn"
        if not reported:
            return Usage(0, 0, 0, False, self.served_model or self.endpoint.model, self.endpoint.provider, stop,
                         self.finish_reason)
        return Usage(_int(prompt), cached_tokens(u), _int(completion), True,
                     self.served_model or self.endpoint.model, self.endpoint.provider, stop, self.finish_reason)


def _failed_generation(exc: BaseException) -> "str | None":
    """What the model wrote, when a provider rejected it as a malformed tool call (Groq: 400 tool_use_failed plus a
    failed_generation field). The OpenAI SDK hands over the inner `error` object as `.body`; look one level down
    as well in case a provider does not unwrap it."""
    body = getattr(exc, "body", None)
    for d in (body, body.get("error") if isinstance(body, Mapping) else None):
        if isinstance(d, Mapping) and isinstance(d.get("failed_generation"), str):
            return d["failed_generation"]
    return None


def _is_tool_use_failed(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) != 400:
        return False
    body = getattr(exc, "body", None)
    inner = body.get("error") if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping) else body
    if isinstance(inner, Mapping) and inner.get("code") == "tool_use_failed":
        return True
    return "tool_use_failed" in str(exc)


# ---------------------------------------------------------------------------------------------
# endpoints and the adapter
# ---------------------------------------------------------------------------------------------

@dataclass
class Endpoint:
    """One candidate provider/model. The client comes from, in order: `client` (given), `base_url` (built here, with
    SDK retries off), or llm_client's factory for `provider` keyed by `key_env`. `key_env` also switches on cooldown
    bookkeeping. `resolve_client()` returns None when the key is not set."""
    provider: str
    model: str
    key_env: "str | None" = None
    timeout_s: "float | None" = None
    client: Any = None
    base_url: "str | None" = None
    api_key: "str | None" = None
    _built: Any = field(default=None, repr=False, compare=False)

    def resolve_client(self) -> Any:
        if self.client is not None:
            return self.client
        if self.base_url:
            if self._built is None:
                from openai import OpenAI
                kwargs = {"base_url": self.base_url, "api_key": self.api_key or "not-needed", "max_retries": 0}
                if self.timeout_s is not None:
                    kwargs["timeout"] = self.timeout_s
                self._built = OpenAI(**kwargs)
            return self._built
        factory = PROVIDER_FACTORIES.get(self.provider)
        if factory is None or not self.key_env:
            return None
        return getattr(_llm_client(), factory)(self.key_env, self.timeout_s)

    def cooling_down(self) -> bool:
        if not self.key_env:
            return False
        try:
            return bool(_llm_client()._is_cooling_down(self.provider, self.key_env, self.model))
        except Exception:                                    # fails open, like the helper itself
            return False

    def note_failure(self, exc: BaseException, bucket: ErrorBucket) -> None:
        """Cooldown policy, mirroring llm_client._handle_transient_error. Never raises."""
        if not self.key_env:
            return
        try:
            llm = _llm_client()
            if bucket is ErrorBucket.PERMANENT_AUTH:
                llm._set_cooldown(self.provider, self.key_env, exc)
            elif bucket is ErrorBucket.RATE_LIMIT_WINDOW:
                if is_zero_quota(exc):
                    seconds = llm._ZERO_QUOTA_COOLDOWN_SECONDS
                else:
                    seconds = max(llm._RATE_LIMIT_COOLDOWN_MIN_SECONDS,
                                  min(float(llm._retry_after_seconds(exc)), llm._RATE_LIMIT_COOLDOWN_MAX_SECONDS))
                llm._set_ledger_cooldown(self.provider, llm._model_cooldown_id(self.key_env, self.model), seconds)
        except Exception as e:                               # noqa: BLE001 -- bookkeeping must never fail a call
            log.warning("cooldown bookkeeping failed (non-fatal): %s", type(e).__name__)


class OpenAICompatAdapter(ModelAdapter):
    """See the module docstring. Build one with `single(...)` (one client or base URL) or `from_chain(...)` (the
    repo's CHAIN steps)."""

    def __init__(self, endpoints: Sequence[Endpoint], *, salvage_text_calls: bool = True):
        if not endpoints:
            raise ValueError("an adapter needs at least one endpoint")
        self.endpoints = tuple(endpoints)
        self.salvage_text_calls = salvage_text_calls

    @classmethod
    def single(cls, model: str, *, client: Any = None, base_url: "str | None" = None, api_key: "str | None" = None,
               provider: str = "openai_compat", key_env: "str | None" = None, timeout_s: "float | None" = None,
               salvage_text_calls: bool = True) -> "OpenAICompatAdapter":
        """One endpoint. Pass a ready `client` (anything with .chat.completions.create) or a `base_url`. Pass
        `provider`/`key_env` too if cooldown bookkeeping should apply."""
        if client is None and not base_url:
            raise ValueError("single() needs a client or a base_url")
        return cls([Endpoint(provider, model, key_env, timeout_s, client, base_url, api_key)],
                   salvage_text_calls=salvage_text_calls)

    @classmethod
    def from_chain(cls, chain: Sequence[Mapping[str, Any]], *, salvage_text_calls: bool = True) -> "OpenAICompatAdapter":
        """From CHAIN steps ({"provider", "model", "key_env", optional "timeout"}), in order of preference. A
        provider that is not OpenAI-compatible (cloudflare) is a configuration error, not a silent skip."""
        eps = []
        for s in chain:
            provider = s.get("provider")
            if provider not in PROVIDER_FACTORIES:
                raise ValueError(f"provider {provider!r} is not OpenAI-compatible here "
                                 f"(supported: {', '.join(sorted(PROVIDER_FACTORIES))})")
            eps.append(Endpoint(provider, s["model"], s.get("key_env"), s.get("timeout")))
        return cls(eps, salvage_text_calls=salvage_text_calls)

    # -- the interface --------------------------------------------------------------------------

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        last: "ModelAdapterError | None" = None
        for ep in self.endpoints:
            if ep.cooling_down():
                log.info("skipping %s:%s (cooling down)", ep.provider, ep.model)
                continue
            client = ep.resolve_client()
            if client is None:
                log.info("skipping %s:%s (no key)", ep.provider, ep.model)
                continue
            try:
                async for ev in self._stream_one(ep, client, request):
                    yield ev
                return
            except ModelAdapterError as err:
                last = err
                if err.emitted_output or err.bucket in _NO_FALLBACK or err.code == "tool_use_failed":
                    raise
                log.warning("%s:%s failed before any output (%s, status=%s); trying the next step",
                            ep.provider, ep.model, err.bucket.name, err.status_code)
        if last is not None:
            raise last
        raise ModelAdapterError(ErrorBucket.TRANSIENT_NETWORK, code="provider_unavailable",
                                message="No model is available right now. Please try again in a moment.")

    # -- one endpoint ---------------------------------------------------------------------------

    def _create_kwargs(self, ep: Endpoint, request: ModelRequest) -> dict:
        kw: dict = {"model": ep.model, "messages": to_openai_messages(request.messages, ep.provider), "stream": True}
        if request.tools:
            kw["tools"] = [to_openai_tool(t) for t in request.tools]
            kw["tool_choice"] = _tool_choice(request.tool_choice)
        if request.max_tokens:
            kw["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kw["temperature"] = request.temperature
        if request.provider_options:
            kw["extra_body"] = dict(request.provider_options)
        return kw

    @staticmethod
    def _configured(client: Any, timeout: "float | None") -> Any:
        opts: dict = {"max_retries": 0}
        if timeout is not None:
            opts["timeout"] = timeout
        with_options = getattr(client, "with_options", None)
        return with_options(**opts) if callable(with_options) else client

    async def _stream_one(self, ep: Endpoint, client: Any, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        holder: dict = {}
        kwargs = self._create_kwargs(ep, request)
        bound = self._configured(client, request.timeout_s if request.timeout_s is not None else ep.timeout_s)

        def put(item):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:                              # the loop is gone: nobody is listening
                pass

        def work():
            try:
                try:
                    response = bound.chat.completions.create(**kwargs, stream_options={"include_usage": True})
                except TypeError:                             # an SDK that does not know stream_options
                    response = bound.chat.completions.create(**kwargs)
                holder["stream"] = response
                for chunk in response:
                    if stop.is_set():
                        break
                    put(("chunk", chunk))
                put(("end", None))
            except BaseException as exc:                     # noqa: BLE001 -- handed to the loop side to classify
                if not stop.is_set():
                    put(("error", exc))
            finally:
                _close(holder.get("stream"))

        thread = threading.Thread(target=work, name=_THREAD_NAME, daemon=True)
        thread.start()
        state = _StreamState(ep, request, self.salvage_text_calls)
        emitted = False
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "chunk":
                    for ev in state.feed(payload):
                        emitted = True
                        yield ev
                elif kind == "end":
                    if not state.saw_chunk or state.finish_reason is None:
                        raise self._incomplete(ep, emitted)
                    for ev in state.finish():
                        yield ev
                    return
                else:
                    rescued = self._try_rescue(payload, state)
                    if rescued is not None:
                        for ev in rescued:
                            yield ev
                        return
                    raise self._wrap(ep, payload, emitted) from payload
        except ModelAdapterError as err:
            if err.__cause__ is not None and not err.emitted_output:
                ep.note_failure(err.__cause__, err.bucket)
            raise
        finally:
            stop.set()
            _close(holder.get("stream"))

    @staticmethod
    def _incomplete(ep: Endpoint, emitted: bool) -> ModelAdapterError:
        return ModelAdapterError(ErrorBucket.TRANSIENT_NETWORK, code="stream_incomplete", provider=ep.provider,
                                 model=ep.model, emitted_output=emitted,
                                 message="A temporary connection issue interrupted this step. Please try again.")

    @staticmethod
    def _wrap(ep: Endpoint, exc: BaseException, emitted: bool) -> ModelAdapterError:
        code = "tool_use_failed" if _is_tool_use_failed(exc) else None
        return ModelAdapterError.from_exception(exc, provider=ep.provider, model=ep.model,
                                                emitted_output=emitted, code=code)

    @staticmethod
    def _try_rescue(exc: BaseException, state: _StreamState) -> "list | None":
        if not state.openai_tools or not _is_tool_use_failed(exc):
            return None
        failed = _failed_generation(exc)
        return state.rescue(failed) if failed else None


def _close(stream: Any) -> None:
    """Hang up on an SDK stream from any thread; never raises.

    Closing an httpx response does NOT wake a thread that is blocked reading from it (on Linux, close() on a
    socket another thread is in recv() on leaves that thread asleep), so a cancelled run would only reach the
    provider at its next chunk, which for a model that is thinking silently can be a long time. The socket is
    therefore shut down first, found through httpx's documented `network_stream` response extension; the blocked
    read then fails at once and the worker exits. If the extension is not there (a fake client, another HTTP
    stack) this degrades to a plain close()."""
    if stream is None:
        return
    try:
        net = getattr(getattr(stream, "response", None), "extensions", {}).get("network_stream")
        sock = net.get_extra_info("socket") if net is not None else None
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:                                        # noqa: BLE001 -- best effort
        pass
    try:
        stream.close()
    except Exception:                                        # noqa: BLE001
        pass
