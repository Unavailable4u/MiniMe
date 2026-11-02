"""
codeagent/context.py -- Patch S08: the context assembler v0 (budgets, cache-stable order, mechanical compaction).

WHAT THIS IS. `BudgetedContextAssembler` is the object the agent loop (loop.py, S07) is given in place of
prompts.DefaultContextAssembler. It keeps the same interface, `assemble(state, items, tools, mode) ->
AssembledContext`, synchronous, and adds what the default says it leaves out (prompts.py, "NOT HERE"): a size
budget, a stable order, a cap on tool results, and a way to make room when the conversation outgrows the window.

THIS IS THE MECHANICAL v0. Nothing here calls a model, reads a file, opens a socket or logs. A summary is built
from counts, tool names and the person's own words, never from anything a model wrote, and it invents nothing.
The build plan's later work package L03 adds model-written summaries, user "compact instructions" and a
scratchpad; it replaces `_summary_text` and nothing else here.

AN ESTIMATE, NOT A COUNT. There is no tokenizer in this repo's dependencies and this patch adds none. Tokens are
ESTIMATED as characters / 4 (rounded up) plus a small overhead per message and per tool call
(estimate_tokens, estimate_message_tokens, estimate_tool_tokens). That is close enough for English and code to
decide when to make room, and it errs on the safe side for code. It will be wrong for some text (CJK, dense
minified code). The default window is therefore conservative (DEFAULT_WINDOW_TOKENS), and an operator can set
the real one: the constructor argument wins, then the environment variable M2C_CONTEXT_WINDOW_TOKENS, then the
default. A bad environment value is ignored (never a crash, never a silent tiny window).

THE ORDER (always the same, for the provider's prompt cache)
    tools      the tool schemas, SORTED BY NAME, so their order never changes between calls
    system     ONE system message: the system prompt first (its text is prompts.system_prompt(mode), not touched
               here), then the person's instructions, the matched rules (in the order the client sent them) and
               the memory index. It is built by DefaultContextAssembler itself, so the text cannot drift.
    summary    only after a compaction: one message standing in for the oldest part of the conversation
    history    the conversation, oldest first; each tool result follows the call it answers
A request is "tools, system, conversation"; the parts that change least come first. Nothing before the first
change is re-ordered, so a cache hit survives every step that only appends.

WHAT EACH ASSEMBLE DOES
  1. Cap every large tool result (TOOL_RESULT_CAP_CHARS): the head and the tail are kept, the middle is replaced
     by a marker that says how many characters were left out. This comes before anything else, so one huge
     result can never be the reason to compact. The stored conversation is never shortened: only the copy that
     goes into this request is.
  2. Estimate the whole request (system message + tool schemas + conversation).
  3. Over the HIGH-water mark (80% of the window): make room until the estimate is under the LOW-water mark
     (60%), stopping the moment it is. In this order:
       a. CLEAR the OLDEST tool outputs, one at a time, oldest first: the output becomes STUB_TEXT. The
          assistant text and the tool CALL record stay, and the result keeps its call id and its error mark, so
          the model still sees what it did and that it failed. An output whose clearing would save fewer than
          MIN_CLEAR_SAVING_TOKENS is left alone (it frees nothing and the model would lose it for no gain).
       b. If that is not enough, REPLACE the oldest rounds with one deterministic summary message (SUMMARY_HEADER
          ...): how many messages, from whom, which tools were called how often, how many calls the person
          denied, and the person's own messages repeated verbatim (each cut to USER_TEXT_CAP_CHARS, all of them
          to USER_TEXT_BUDGET_CHARS, newest first; it says so when it had to leave some out). Only as much as
          needed is dropped.
     A "round" is a user message, or an assistant message with the tool results that answer it.

WHAT IS NEVER TOUCHED
    * The system message: system prompt, instructions, rules, memory index. Never cut, never summarised.
    * The newest KEEP_RECENT_ROUNDS rounds: never cleared, never summarised. (If those alone are bigger than the
      window nothing more can be done mechanically; the request goes out and the provider's refusal is handled
      by the loop like any model failure.)
    * A call and its result are never separated: a cut is only made where no call has its result on the other
      side of it (a tool message with no call before it is a provider error).
    * A call that is still UNANSWERED (same meaning as loop.find_dangling: an assistant tool call with no tool
      result yet), nor the assistant message that made it, nor the results of its siblings, and nothing after
      the oldest such message is ever summarised away.
    * A denied call's result ("The user denied this action ...", loop.MSG_DENIED) is never cleared, and a denial
      inside a summarised part is stated in the summary. Feature guide 5.4: compaction must not silently
      discard an approval decision, a protected-path rule or a user constraint. The same sentence is why the
      person's messages are repeated in the summary instead of being dropped.

COMPACTION IS REMEMBERED PER RUN (a decision this patch had to make). The loop assembles from the FULL
conversation in the run buffer on every step; it never replaces the buffer's items with a compacted version. A
stateless "compact when over 80%" would therefore run again on every later step of a long run (the full
conversation stays over the mark), and the thrash guard would fire after three steps of any long run. So the
assembler remembers, per run, two small integers: how many of the oldest eligible tool outputs it has cleared,
and where the summary cut is. Every later assemble RE-APPLIES those decisions first (same stubs, same summary,
same bytes: the prefix of the request does not move, which is what the provider cache needs) and only compacts
further if the result is STILL over the high-water mark. Only that further compaction counts as "compaction ran"
and only it produces a note.

THE THRASH GUARD. If compaction has to run again on THRASH_LIMIT (3) consecutive assembles, the context is
refilling as fast as it is emptied (one big result per step). Only a compaction that changed something counts (a
step that is over the mark with nothing old enough to touch is not a thrash). The next consecutive need after
three stops auto-compaction for that run: the context is returned uncompacted (tool results are still capped), the note `context_thrash` is
added ONCE, and every later assemble of that run returns uncompacted with no note. A step that does not need
compaction resets the count. Letting the request go out over the window is deliberate: the provider refuses it,
the loop pauses the run with a clear error, and the person starts a smaller task, instead of the server looping.

PER-RUN MEMORY IS BOUNDED. `_runs` is an LRU map of at most MAX_TRACKED_RUNS entries, keyed by `state["run_id"]`
when the caller supplies one. TODAY loop.py hands the assembler the run's buffer state, which is run.start's
payload without `history` and has NO run id; the key is then a short hash of what identifies the conversation
(profile, cwd label, instructions, rules, memory index and the person's first message). Two runs with identical
settings and an identical first message share one entry; the consequence is a mis-counted thrash streak, never a
wrong context (the remembered cut is re-validated against the items on every call and dropped when it does not
fit). Passing the run id in `state` is a one-line change in loop.py that this patch does not make (scope); the
assembler uses it as soon as it is there. A run that never compacts takes no slot. An evicted run starts over,
which can only re-enable auto-compaction for a run that had been stopped.

NOTES. `AssembledContext.notes` carries fixed machine codes, never text and never content: NOTE_COMPACTED
("context_compacted") when a compaction happened in THIS assemble (not when it was merely re-applied, so the
person gets one notice per compaction, not one per step) and NOTE_THRASH ("context_thrash"). The loop turns each
into a `notice` run_event with fixed text.

NEVER RAISES. Items the assembler does not understand (not a mapping, an unknown role, a tool message with no
call id, malformed tool calls) are never modified and never counted; they come through exactly as
DefaultContextAssembler would have sent them, which is not at all when the default would skip them. An item
whose conversion fails is skipped. If anything unexpected goes wrong inside assemble, `_fallback` returns the
plain context (system message, every convertible item, sorted tools, no notes) instead of an exception.

CONTENT. Everything passing through is CONTENT in the protocol's sense (protocol README 6): held in memory for
one call and never logged. This module has no logger on purpose (tests pin it), imports only the standard library
and codeagent, and its per-run memory holds integers and flags, never text.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from codeagent.model_adapter import Message, ToolSpec
from codeagent.prompts import (AssembledContext, ContextAssembler, DefaultContextAssembler, items_to_messages,
                               system_prompt)

# ---------------------------------------------------------------------------------------------
# Settings and fixed texts (server text only: nothing a person, a model or a device wrote is ever one)
# ---------------------------------------------------------------------------------------------

WINDOW_ENV = "M2C_CONTEXT_WINDOW_TOKENS"
DEFAULT_WINDOW_TOKENS = 100_000          # conservative: under the common 128k windows, with room for the reply
MIN_WINDOW_TOKENS = 512                  # below this the guard rails would be nonsense
MAX_WINDOW_TOKENS = 10_000_000           # above this a setting is a typo
HIGH_WATER = 0.80                        # compact when the estimate passes this share of the window ...
LOW_WATER = 0.60                         # ... and stop as soon as it is under this one
TOOL_RESULT_CAP_CHARS = 20_000           # about 5 000 tokens per tool result
MIN_TOOL_RESULT_CAP_CHARS = 100
THRASH_LIMIT = 3                         # consecutive assembles that needed compaction before it is stopped
KEEP_RECENT_ROUNDS = 4                   # never cleared, never summarised
MIN_CLEAR_SAVING_TOKENS = 16             # an output that would save less than this is not worth hiding
MAX_TRACKED_RUNS = 512

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4              # role, separators and framing of one message
CALL_OVERHEAD_TOKENS = 4                 # id and framing of one tool call
FALLBACK_JSON_TOKENS = 64                # when a tool input cannot be serialised

NOTE_COMPACTED = "context_compacted"
NOTE_THRASH = "context_thrash"

STUB_TEXT = "[tool output cleared to save space]"
DENIED_PREFIX = "The user denied this action."      # the start of loop.MSG_DENIED (a test keeps them equal)
SUMMARY_HEADER = "[Earlier part of this conversation, condensed by the server without a model]"

USER_TEXT_CAP_CHARS = 1500               # one of the person's messages, repeated in a summary
USER_TEXT_BUDGET_CHARS = 6000            # all of them
MAX_NAMES_IN_SUMMARY = 20

_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")      # protocol/v1/common.json tool_name
_MAX_RUN_KEY_CHARS = 200


# ---------------------------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------------------------

def estimate_tokens(text: Any) -> int:
    """ESTIMATED tokens of a string: characters / 4, rounded up. Anything that is not a string is 0. O(1)."""
    if not isinstance(text, str):
        return 0
    return -(-len(text) // CHARS_PER_TOKEN)


def _json_tokens(value: Any) -> int:
    try:
        return estimate_tokens(json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False))
    except Exception:                                    # a cycle, a value that will not serialise
        return FALLBACK_JSON_TOKENS


def estimate_message_tokens(message: Any) -> int:
    """ESTIMATED tokens of one S05 Message: its text, a per-message overhead, and for each tool call its name,
    its serialised arguments and a per-call overhead. Never raises."""
    try:
        total = MESSAGE_OVERHEAD_TOKENS + estimate_tokens(getattr(message, "content", ""))
        for call in getattr(message, "tool_calls", ()) or ():
            total += CALL_OVERHEAD_TOKENS + estimate_tokens(getattr(call, "name", "")) \
                + _json_tokens(getattr(call, "arguments", None))
        return total
    except Exception:
        return MESSAGE_OVERHEAD_TOKENS


def estimate_tool_tokens(tool: Any) -> int:
    """ESTIMATED tokens of one tool schema as the provider sees it (name, description, input schema). 0 for
    anything that is not a ToolSpec."""
    if not isinstance(tool, ToolSpec):
        return 0
    return (MESSAGE_OVERHEAD_TOKENS + estimate_tokens(tool.name) + estimate_tokens(tool.description)
            + _json_tokens(tool.input_schema))


def estimate_context_tokens(context: AssembledContext) -> int:
    """ESTIMATED tokens of a whole request: every message plus every tool schema."""
    return (sum(estimate_message_tokens(m) for m in context.messages)
            + sum(estimate_tool_tokens(t) for t in context.tools))


def truncate_middle(text: str, cap: int) -> str:
    """`text` unchanged when it is at most `cap` characters; else its first 60% of `cap` and last 40% of `cap`
    around a marker that says how many characters were left out (the tail matters most for command output)."""
    if not isinstance(text, str) or len(text) <= cap:
        return text
    head = (cap * 3) // 5
    tail = cap - head
    omitted = len(text) - head - tail
    marker = f"\n[... {omitted} characters omitted from the middle of this tool result to save space ...]\n"
    return text[:head] + marker + text[len(text) - tail:]


# ---------------------------------------------------------------------------------------------
# Reading items without ever raising
# ---------------------------------------------------------------------------------------------

def _as_list(value: Any) -> list:
    """`value` as a list when it is a sequence or an iterator; else empty. A string is not a list of items."""
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except Exception:
        return []


def _kind(item: Any) -> "str | None":
    """user | assistant | tool when `item` has the protocol's shape for it; None for anything else."""
    if not isinstance(item, Mapping):
        return None
    role = item.get("role")
    if role == "user" and isinstance(item.get("text"), str):
        return "user"
    if role == "assistant":
        return "assistant"
    if role == "tool" and isinstance(item.get("call_id"), str):
        return "tool"
    return None


def _well_formed_calls(item: Mapping) -> "tuple[tuple[str, str], ...]":
    calls = item.get("tool_calls")
    if not isinstance(calls, (list, tuple)):
        return ()
    return tuple((c["call_id"], c["tool"]) for c in calls
                 if isinstance(c, Mapping) and isinstance(c.get("call_id"), str) and isinstance(c.get("tool"), str))


def find_unanswered(items: Sequence) -> "list[tuple[str, str]]":
    """(call_id, tool) of every assistant tool call with no tool result anywhere in `items`: the same meaning
    as loop.find_dangling (a test keeps them equal), but it ignores items it cannot read instead of raising."""
    answered, calls = set(), []
    for item in _as_list(items):
        if not isinstance(item, Mapping):
            continue
        role = item.get("role")
        if role == "tool" and isinstance(item.get("call_id"), str):
            answered.add(item["call_id"])
        elif role == "assistant":
            calls.extend(_well_formed_calls(item))
    return [(call_id, tool) for call_id, tool in calls if call_id not in answered]


def _is_denial(item: Mapping) -> bool:
    output = item.get("output")
    return (not item.get("ok", False)) and isinstance(output, str) and output.startswith(DENIED_PREFIX)


def _safe_tool_name(name: Any) -> str:
    return name if isinstance(name, str) and _TOOL_NAME_RE.match(name) else "unknown_tool"


def _tool_sort_key(tool: Any) -> str:
    return str(getattr(tool, "name", ""))


def _convert(item: Any) -> "tuple[Message, ...]":
    """The messages of one item, exactly as the default assembler sends them; () when it cannot be converted."""
    try:
        return tuple(items_to_messages([item]))
    except Exception:
        return ()


@dataclass(frozen=True)
class _Row:
    """One conversation item as this assemble sees it (after the cap on tool results)."""
    item: Any
    kind: "str | None"
    messages: "tuple[Message, ...]"
    cost: int
    calls: "tuple[tuple[str, str], ...]" = ()
    call_id: "str | None" = None


def _make_row(item: Any, cap: "int | None" = None) -> _Row:
    kind = _kind(item)
    if kind is None:
        return _Row(item, None, (), 0)
    calls: tuple = ()
    call_id = None
    if kind == "tool":
        call_id = item["call_id"]
        output = item.get("output")
        if cap is not None and isinstance(output, str) and len(output) > cap:
            item = dict(item)
            item["output"] = truncate_middle(output, cap)
    elif kind == "assistant":
        calls = _well_formed_calls(item)
    messages = _convert(item)
    return _Row(item, kind, messages, sum(estimate_message_tokens(m) for m in messages), calls, call_id)


def _stub_row(row: _Row) -> _Row:
    """The row with its output cleared. The call id, the error mark and the trust label stay."""
    item = dict(row.item)
    item["output"] = STUB_TEXT
    item["truncated"] = False
    return _make_row(item)


# ---------------------------------------------------------------------------------------------
# The layout of one conversation: where a cut may go, what is protected
# ---------------------------------------------------------------------------------------------

class _Layout:
    """Facts about the rows that decide what compaction may touch. Built once per compaction.

    round_starts   indices of user and assistant items
    blocked[c]     True when a cut BEFORE row c would separate a call from one of its results
    protected      the oldest assistant message with an unanswered call (everything from it on is kept) and the
                   ids of every call in such messages (their results are never cleared)
    boundary       start of the newest `keep` rounds: from there on nothing is cleared or summarised
    cut_limit      the largest cut allowed: the smaller of boundary and the first protected message
    eligible       indices of tool results older than the boundary that may be cleared, oldest first
    """

    def __init__(self, rows: "list[_Row]", keep: int):
        n = len(rows)
        self.n = n
        call_at: dict = {}
        result_at: dict = {}
        for index, row in enumerate(rows):
            if row.kind == "assistant":
                for call_id, _ in row.calls:
                    call_at.setdefault(call_id, []).append(index)
            elif row.kind == "tool":
                result_at.setdefault(row.call_id, []).append(index)
        unanswered = {call_id for call_id in call_at if call_id not in result_at}
        self.protected_first = n
        self.protected_ids: set = set()
        for index, row in enumerate(rows):
            if row.kind == "assistant" and any(call_id in unanswered for call_id, _ in row.calls):
                self.protected_first = min(self.protected_first, index)
                self.protected_ids.update(call_id for call_id, _ in row.calls)
        diff = [0] * (n + 2)
        for call_id, positions in call_at.items():
            spread = positions + result_at.get(call_id, [])
            low, high = min(spread), max(spread)
            if high > low:
                diff[low + 1] += 1
                diff[high + 1] -= 1
        self.blocked: "list[bool]" = []
        running = 0
        for index in range(n + 1):
            running += diff[index]
            self.blocked.append(running > 0)
        self.round_starts = [i for i, row in enumerate(rows) if row.kind in ("user", "assistant")]
        self.round_set = set(self.round_starts)
        self.boundary = self.round_starts[-keep] if len(self.round_starts) >= keep else 0
        self.cut_limit = min(self.boundary, self.protected_first)
        self.eligible = [i for i in range(self.boundary)
                         if rows[i].kind == "tool" and rows[i].call_id not in self.protected_ids
                         and not _is_denial(rows[i].item)]

    def valid_cut(self, cut: int) -> bool:
        return 0 < cut <= self.cut_limit and cut in self.round_set and not self.blocked[cut]


# ---------------------------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------------------------

class _Aggregate:
    """Counts over the rows a summary stands in for, absorbed in order."""

    def __init__(self):
        self.messages = 0
        self.assistants = 0
        self.results = 0
        self.users: "list[str]" = []
        self.calls: dict = {}
        self.denied: dict = {}
        self._tool_of: dict = {}

    def add(self, row: _Row) -> None:
        if row.kind == "user":
            self.messages += 1
            self.users.append(row.item["text"])
        elif row.kind == "assistant":
            self.messages += 1
            self.assistants += 1
            for call_id, tool in row.calls:
                name = _safe_tool_name(tool)
                self._tool_of[call_id] = name
                self.calls[name] = self.calls.get(name, 0) + 1
        elif row.kind == "tool":
            self.messages += 1
            self.results += 1
            if _is_denial(row.item):
                name = self._tool_of.get(row.call_id, "unknown_tool")
                self.denied[name] = self.denied.get(name, 0) + 1

    def render(self) -> str:
        users = len(self.users)
        span = f" (turns 1 to {users})" if users else ""
        lines = [
            SUMMARY_HEADER,
            "This note comes from the server, not from the person.",
            f"The first {self.messages} messages of this conversation{span} were replaced by this note to fit the "
            f"model's context window: {users} from the person, {self.assistants} from you and {self.results} tool "
            "results. Your earlier replies and the tool outputs from that part are no longer shown; run a tool "
            "again if you need something from them.",
        ]
        if self.calls:
            lines.append("Tool calls made in that part: " + _count_list(self.calls) + ".")
        if self.denied:
            total = sum(self.denied.values())
            lines.append(f"The person denied {total} call{'s' if total != 1 else ''} in that part "
                         f"({_count_list(self.denied)}). Do not retry a denied action unless the person asks "
                         "for it.")
        if users:
            shown, used = [], 0
            for text in reversed(self.users):
                piece = text if len(text) <= USER_TEXT_CAP_CHARS else text[:USER_TEXT_CAP_CHARS] + " [cut]"
                if used + len(piece) > USER_TEXT_BUDGET_CHARS:
                    break
                shown.append(piece)
                used += len(piece)
            lines.append("The person's messages in that part, oldest first:")
            for number, piece in enumerate(reversed(shown), 1):
                lines.append(f"{number}. {piece}")
            if len(shown) < users:
                left_out = users - len(shown)
                lines.append(f"({left_out} earlier message{'s' if left_out != 1 else ''} from the person "
                             "were too long to repeat here.)")
        return "\n".join(lines)


def _count_list(counts: dict) -> str:
    names = sorted(counts)
    shown = ", ".join(f"{name} x{counts[name]}" for name in names[:MAX_NAMES_IN_SUMMARY])
    if len(names) > MAX_NAMES_IN_SUMMARY:
        shown += f", and {len(names) - MAX_NAMES_IN_SUMMARY} more"
    return shown


# ---------------------------------------------------------------------------------------------
# What is remembered per run
# ---------------------------------------------------------------------------------------------

@dataclass
class _RunMemory:
    """Integers and flags only (never text). See "COMPACTION IS REMEMBERED PER RUN" in the module docstring.

    cleared   the oldest `cleared` eligible tool outputs are stubbed
    cut       rows before this index are replaced by the summary (0: none)
    streak    consecutive assembles that needed a fresh compaction
    halted    auto-compaction is stopped for this run (thrash)
    length    how many items the last assemble saw; a shorter list means the conversation was rebuilt
    """
    cleared: int = 0
    cut: int = 0
    streak: int = 0
    halted: bool = False
    length: int = 0

    @property
    def active(self) -> bool:
        return bool(self.cleared or self.cut or self.streak or self.halted)


def _run_key(state: Mapping, rows: "list[_Row]") -> str:
    run_id = state.get("run_id")
    if isinstance(run_id, str) and 0 < len(run_id) <= _MAX_RUN_KEY_CHARS:
        return "run:" + run_id
    digest = hashlib.blake2b(digest_size=16)

    def feed(value: Any) -> None:
        if isinstance(value, str):
            digest.update(value.encode("utf-8", errors="replace"))
        digest.update(b"\x00")

    for name in ("profile", "cwd_label", "instructions", "memory_index"):
        feed(state.get(name))
    rules = state.get("rules")
    for rule in rules if isinstance(rules, (list, tuple)) else ():
        if isinstance(rule, Mapping):
            feed(rule.get("name") if isinstance(rule.get("name"), str) else None)
            feed(rule.get("text"))
    for row in rows:
        if row.kind == "user":
            feed(row.item["text"][:2000])
            break
    return "fp:" + digest.hexdigest()


def _setting_ratio(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _positive_int(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _env_window(environ: Mapping) -> "int | None":
    """The window from the environment, or None when it is unset or not a sane whole number."""
    raw = environ.get(WINDOW_ENV)
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if MIN_WINDOW_TOKENS <= value <= MAX_WINDOW_TOKENS else None


# ---------------------------------------------------------------------------------------------
# The assembler
# ---------------------------------------------------------------------------------------------

class BudgetedContextAssembler(ContextAssembler):
    """See the module docstring.

    window_tokens       the model window in (estimated) tokens; default: the environment, then
                        DEFAULT_WINDOW_TOKENS
    high_water          share of the window above which compaction starts (0 < low_water < high_water <= 1)
    low_water           share of the window compaction stops at
    tool_result_cap_chars  longest tool result sent as it is
    thrash_limit        consecutive compactions allowed before auto-compaction is stopped for the run
    keep_recent_rounds  rounds at the end that are never cleared or summarised
    max_tracked_runs    size of the per-run memory (least recently used runs are forgotten)
    environ             where to read M2C_CONTEXT_WINDOW_TOKENS (default: os.environ; tests pass a dict)
    """

    def __init__(self, window_tokens: "int | None" = None, *, high_water: float = HIGH_WATER,
                 low_water: float = LOW_WATER, tool_result_cap_chars: int = TOOL_RESULT_CAP_CHARS,
                 thrash_limit: int = THRASH_LIMIT, keep_recent_rounds: int = KEEP_RECENT_ROUNDS,
                 max_tracked_runs: int = MAX_TRACKED_RUNS, environ: "Mapping | None" = None):
        if window_tokens is None:
            window_tokens = _env_window(os.environ if environ is None else environ) or DEFAULT_WINDOW_TOKENS
        high = _setting_ratio(high_water, "high_water")
        low = _setting_ratio(low_water, "low_water")
        if not 0.0 < low < high <= 1.0:
            raise ValueError("water marks must satisfy 0 < low_water < high_water <= 1")
        self._window = _positive_int(window_tokens, "window_tokens", MIN_WINDOW_TOKENS)
        self._high = math.floor(self._window * high + 1e-9)
        self._low = math.floor(self._window * low + 1e-9)
        self._cap = _positive_int(tool_result_cap_chars, "tool_result_cap_chars", MIN_TOOL_RESULT_CAP_CHARS)
        self._thrash_limit = _positive_int(thrash_limit, "thrash_limit")
        self._keep = _positive_int(keep_recent_rounds, "keep_recent_rounds")
        self._max_runs = _positive_int(max_tracked_runs, "max_tracked_runs")
        self._runs: "OrderedDict[str, _RunMemory]" = OrderedDict()
        self._lock = threading.Lock()
        self._base = DefaultContextAssembler()

    # -- read-only facts ---------------------------------------------------------------------
    @property
    def window_tokens(self) -> int:
        return self._window

    @property
    def high_tokens(self) -> int:
        """Compaction starts when the estimate is above this."""
        return self._high

    @property
    def low_tokens(self) -> int:
        """Compaction stops when the estimate is at or below this."""
        return self._low

    @property
    def keep_recent_rounds(self) -> int:
        return self._keep

    def tracked_runs(self) -> int:
        """How many runs the per-run memory holds right now (never more than max_tracked_runs)."""
        with self._lock:
            return len(self._runs)

    # -- the interface -----------------------------------------------------------------------
    def assemble(self, state, items, tools, mode) -> AssembledContext:
        try:
            return self._assemble(state, items, tools, mode)
        except Exception:                                # never raise into the loop: send the plain context
            return self._fallback(state, items, tools, mode)

    # -- the work ----------------------------------------------------------------------------
    def _assemble(self, state, items, tools, mode) -> AssembledContext:
        state = state if isinstance(state, Mapping) else {}
        sorted_tools = tuple(sorted(_as_list(tools), key=_tool_sort_key))
        system = self._system_message(state, mode)
        fixed = estimate_message_tokens(system) + sum(estimate_tool_tokens(t) for t in sorted_tools)
        base = [_make_row(item, self._cap) for item in _as_list(items)]
        n = len(base)

        key, mem = None, _RunMemory()
        if self._runs:                                   # only look a run up when something is remembered at all
            key = _run_key(state, base)
            mem = self._recall(key) or _RunMemory()
        if mem.length > n:                               # a shorter conversation: it was rebuilt, start over
            mem = _RunMemory()
        if mem.halted:
            return self._build(system, sorted_tools, base, 0, "", ())
        if not mem.active and fixed + sum(r.cost for r in base) <= self._high:
            return self._build(system, sorted_tools, base, 0, "", ())      # the common case: nothing to do

        layout = _Layout(base, self._keep)
        if mem.cut and not layout.valid_cut(mem.cut):    # the remembered cut does not fit these items: start over
            mem = _RunMemory()

        # 1. re-apply what was decided before, the same way, so the prefix of the request does not move
        rows = list(base)
        for index in layout.eligible[: mem.cleared]:
            if index >= mem.cut:
                self._clear(rows, index)
        cut = mem.cut
        summary = self._summary_text(rows, cut) if cut else ""
        total = fixed + (estimate_message_tokens(Message.user(summary)) if cut else 0) \
            + sum(r.cost for r in rows[cut:])
        if total <= self._high:
            mem.streak, mem.length = 0, n
            self._remember(key or _run_key(state, base), mem)
            return self._build(system, sorted_tools, rows, cut, summary, ())

        # 2. it is still too big: this is a compaction, and the thrash guard counts it
        if mem.streak >= self._thrash_limit:
            stopped = _RunMemory(halted=True, length=n)
            self._remember(key or _run_key(state, base), stopped)
            return self._build(system, sorted_tools, base, 0, "", (NOTE_THRASH,))

        changed = False
        ordinal = mem.cleared
        eligible = layout.eligible
        while total > self._low and ordinal < len(eligible):         # a. clear the oldest outputs first
            index = eligible[ordinal]
            ordinal += 1
            if index < cut:
                continue
            before = rows[index].cost
            if self._clear(rows, index):
                total -= before - rows[index].cost
                changed = True
        mem.cleared = ordinal

        if total > self._low:                                        # b. replace the oldest rounds by a summary
            chosen = self._choose_cut(rows, layout, cut, fixed)
            if chosen > cut:
                cut = chosen
                summary = self._summary_text(rows, cut)
                changed = True
        mem.cut = cut
        if changed:                                      # only a compaction that did something counts as one
            mem.streak += 1
        mem.length = n
        self._remember(key or _run_key(state, base), mem)
        return self._build(system, sorted_tools, rows, cut, summary, (NOTE_COMPACTED,) if changed else ())

    @staticmethod
    def _clear(rows: "list[_Row]", index: int) -> bool:
        """Replace rows[index] by its cleared version when that saves at least MIN_CLEAR_SAVING_TOKENS. True
        when it changed. (A small output is left as it is: hiding it frees nothing and the model loses it.)"""
        stub = _stub_row(rows[index])
        if rows[index].cost - stub.cost < MIN_CLEAR_SAVING_TOKENS:
            return False
        rows[index] = stub
        return True

    def _choose_cut(self, rows: "list[_Row]", layout: _Layout, old_cut: int, fixed: int) -> int:
        """The smallest later cut that brings the estimate under the low-water mark, else the largest allowed
        one; `old_cut` when no later cut is allowed. A cut is always at the start of a round and never between a
        call and a result."""
        candidates = [c for c in layout.round_starts if old_cut < c <= layout.cut_limit and not layout.blocked[c]]
        if not candidates:
            return old_cut
        n = len(rows)
        suffix = [0] * (n + 1)
        for index in range(n - 1, -1, -1):
            suffix[index] = suffix[index + 1] + rows[index].cost
        current = fixed + suffix[old_cut]
        if old_cut:
            current += estimate_message_tokens(Message.user(self._summary_text(rows, old_cut)))
        aggregate, absorbed = _Aggregate(), 0
        best, best_total = old_cut, current
        for cut in candidates:
            for index in range(absorbed, cut):
                aggregate.add(rows[index])
            absorbed = cut
            estimate = fixed + estimate_message_tokens(Message.user(aggregate.render())) + suffix[cut]
            if estimate <= self._low:
                return cut                               # the smallest cut that is enough
            if estimate < best_total:                    # a cut that does not shrink the request is no cut
                best, best_total = cut, estimate
        return best

    @staticmethod
    def _summary_text(rows: "list[_Row]", cut: int) -> str:
        aggregate = _Aggregate()
        for row in rows[:cut]:
            aggregate.add(row)
        return aggregate.render()

    @staticmethod
    def _build(system: Message, tools: tuple, rows: "list[_Row]", cut: int, summary: str,
               notes: tuple) -> AssembledContext:
        messages = [system]
        if cut:
            messages.append(Message.user(summary))
        for row in rows[cut:]:
            messages.extend(row.messages)
        return AssembledContext(messages=tuple(messages), tools=tools, notes=notes)

    def _system_message(self, state: Mapping, mode: Any) -> Message:
        """The default assembler's own system message (so its text can never drift), from the parts of `state`
        that have the right shape."""
        rules = state.get("rules")
        safe = {
            "instructions": state.get("instructions") if isinstance(state.get("instructions"), str) else "",
            "memory_index": state.get("memory_index") if isinstance(state.get("memory_index"), str) else "",
            "rules": [r for r in rules if isinstance(r, Mapping)] if isinstance(rules, (list, tuple)) else [],
        }
        try:
            return self._base.assemble(safe, (), (), mode).messages[0]
        except Exception:
            return Message.system(system_prompt(mode))

    def _fallback(self, state, items, tools, mode) -> AssembledContext:
        """The plain context, built with every step guarded. This must not raise."""
        try:
            system = self._system_message(state if isinstance(state, Mapping) else {}, mode)
        except Exception:
            system = Message.system(system_prompt("ask"))
        messages = [system]
        try:
            for item in _as_list(items):
                messages.extend(_convert(item))
        except Exception:
            pass
        try:
            sorted_tools = tuple(sorted(_as_list(tools), key=_tool_sort_key))
        except Exception:
            sorted_tools = ()
        return AssembledContext(messages=tuple(messages), tools=sorted_tools)

    # -- the per-run memory ------------------------------------------------------------------
    def _recall(self, key: str) -> "_RunMemory | None":
        with self._lock:
            found = self._runs.get(key)
            if found is None:
                return None
            self._runs.move_to_end(key)
            return replace(found)                        # a copy: a failure half way leaves the stored one alone

    def _remember(self, key: str, mem: _RunMemory) -> None:
        with self._lock:
            if not mem.active:                           # a run that is quiet takes no slot
                self._runs.pop(key, None)
                return
            self._runs[key] = mem
            self._runs.move_to_end(key)
            while len(self._runs) > self._max_runs:
                self._runs.popitem(last=False)
