"""
m2c/src/m2c/tui/history.py -- Patch C09a: the conversation rebuilt from the local log, for ``run.start``'s ``history``.

WHY THIS EXISTS
---------------
P00 has no softer interrupt than ``cancel``, and cancel ends the run.  The conversation is not lost, because the
device keeps it (C02): the next message starts a NEW run and passes ``history`` -- the protocol's history items --
rebuilt from the session's log.  The same happens after a rewind (C08), where the server's copy of the conversation
is out of step with the files.  This module is that rebuild.  It is pure: it reads a sequence of ``(type, data)``
pairs and returns history items; it touches no store and no terminal.

THE LOG CONVENTION (written by the app; ``repl.py`` already writes the first two)
--------------------------------------------------------------------------------
``user.message {text}`` · ``user.unsent {}`` (the last user message never reached the server) ·
``assistant.message {text}`` (one per model step, written at ``text_done``; a reply that was restarted is written
once, with its final text) · ``tool.call {call_id, tool, input}`` · ``tool.result {call_id, ok, output, truncated?,
trust?}`` (what the device sent back; the run's events carry no outputs) · ``tool.closed {call_id, status}`` (the
server's ``tool_finished``) · ``checkpoint {step, call_id?, tool?}`` (a C08 snapshot step, tied to its call) ·
``rewind {to_step}`` (written by ``checkpoints.rewind``).  Anything else in the log is ignored.

WHAT COMES OUT
--------------
* Well-formed: every assistant ``tool_calls`` entry has its tool result right after it.  A call with no result and no
  closing status (the run was cut) is DROPPED, never left dangling (a tool message without a call, or a call without
  a result, is a provider error).  A call that was closed without a recorded output (denied, cancelled, timed out,
  cut off) gets a fixed one-sentence result in this module's own words, never server text.
* Calls the server refused itself never reached the device, so they are not in the log and not in the history.
* The first item is a ``user`` item.
* REWIND: a ``rewind`` marker cuts the conversation to just BEFORE the model step whose tool call owns ``to_step``,
  and everything after that point (up to the marker) is ignored; what happens after the marker is kept.  A step the
  log cannot tie to a call (a snapshot taken by a call that was cut before its result) is placed conservatively:
  the cut goes before the first model step with a checkpointed tool call after the last step known to be earlier.
  Keeping too much would make the model believe in edits that were undone, so the rule cuts more, never less.
* SIZE: each tool output is shortened to ``MAX_OUTPUT_CHARS`` (head and tail, with a marker), the total is held to
  ``max_bytes`` by dropping the OLDEST whole turns, and to ``max_items`` (the protocol's 10000).  ``History.omitted``
  says how many items were left out so the app can tell the person.

Standard library only; pure.

Place this file at: m2c/src/m2c/tui/history.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

__all__ = [
    "EV_ASSISTANT", "EV_CALL", "EV_CHECKPOINT", "EV_CLOSED", "EV_RESULT", "EV_REWIND", "EV_UNSENT", "EV_USER",
    "History", "MAX_OUTPUT_CHARS", "checkpoint_tools", "rebuild_history",
]

EV_USER = "user.message"
EV_UNSENT = "user.unsent"
EV_ASSISTANT = "assistant.message"
EV_CALL = "tool.call"
EV_RESULT = "tool.result"
EV_CLOSED = "tool.closed"
EV_CHECKPOINT = "checkpoint"
EV_REWIND = "rewind"

CHECKPOINTED_TOOLS = frozenset({"edit_file", "write_file", "delete_path"})  # as runner.CHECKPOINTED_TOOLS
MAX_OUTPUT_CHARS = 30_000
_HEAD, _TAIL = 20_000, 8_000
MAX_TEXT_CHARS = 150_000
DEFAULT_MAX_BYTES = 600_000
MAX_ITEMS = 10_000
_NO_OUTPUT = {
    "denied": "The call was denied and did not run.",
    "cancelled": "The call was cancelled before it finished.",
    "timeout": "The call took too long and was stopped.",
    "disconnected": "The connection dropped before the call finished.",
    "error": "The call failed; its output was not kept.",
    "ok": "The call finished; its output was not kept.",
}


@dataclass(frozen=True)
class History:
    items: list[dict]
    omitted: int = 0          # items left out to stay within the size bound
    shortened: int = 0        # tool outputs that were shortened


@dataclass
class _Group:
    kind: str                                   # "user" or "step"
    text: str = ""
    calls: dict = field(default_factory=dict)   # call_id -> {"tool", "input"}
    results: dict = field(default_factory=dict)
    closed: dict = field(default_factory=dict)
    steps: set = field(default_factory=set)
    unmapped: int = 0                           # checkpointed calls without a known step




def _shorten_output(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    left = len(text) - _HEAD - _TAIL
    return f"{text[:_HEAD]}\n[... {left} characters left out to keep the conversation small ...]\n{text[-_TAIL:]}", True


def checkpoint_tools(events: Iterable[tuple[str, Any]]) -> dict[int, str]:
    """``step -> tool`` for the ``checkpoint`` events that name their tool (for a rewind list)."""
    out: dict[int, str] = {}
    for kind, data in events:
        if kind == EV_CHECKPOINT and isinstance(data, dict) and isinstance(data.get("step"), int) \
                and isinstance(data.get("tool"), str):
            out[data["step"]] = data["tool"]
    return out


def _cut_index(groups: list[_Group], n: int) -> int:
    for i, g in enumerate(groups):
        if n in g.steps:
            return i
    last = max((i for i, g in enumerate(groups) if any(s < n for s in g.steps)), default=-1)
    for i in range(last + 1, len(groups)):
        if groups[i].kind == "step" and (groups[i].unmapped or any(
                c["tool"] in CHECKPOINTED_TOOLS for c in groups[i].calls.values())):
            return i
    return len(groups)


def _build_items(group: _Group) -> tuple[list[dict], int]:
    if group.kind == "user":
        text = group.text[:MAX_TEXT_CHARS]
        return ([{"role": "user", "text": text}] if text.strip() else []), 0
    calls, tool_items, shortened = [], [], 0
    for call_id, call in group.calls.items():
        if call_id in group.results:
            r = group.results[call_id]
            output, cut = _shorten_output(r["output"])
            item = {"role": "tool", "call_id": call_id, "ok": r["ok"], "output": output}
            if r.get("truncated") is True or cut:
                item["truncated"] = True
            if r.get("trust") in ("local_project", "local_other", "external"):
                item["trust"] = r["trust"]
            shortened += 1 if cut else 0
        elif group.closed.get(call_id) in _NO_OUTPUT:
            status = group.closed[call_id]
            item = {"role": "tool", "call_id": call_id, "ok": status == "ok", "output": _NO_OUTPUT[status]}
        else:
            continue  # no result and not closed: cut off, dropped
        calls.append({"call_id": call_id, "tool": call["tool"], "input": call["input"]})
        tool_items.append(item)
    items: list[dict] = []
    head: dict = {"role": "assistant"}
    if group.text.strip():
        head["text"] = group.text[:MAX_TEXT_CHARS]
    if calls:
        head["tool_calls"] = calls
    if len(head) > 1:
        items.append(head)
    return items + (tool_items if calls else []), shortened


def _size(item: dict) -> int:
    return len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2


def rebuild_history(events: Iterable[tuple[str, Any]], *, max_bytes: int = DEFAULT_MAX_BYTES,
                    max_items: int = MAX_ITEMS) -> History:
    groups: list[_Group] = []
    index: dict[str, _Group] = {}

    def step_group() -> _Group:
        if not groups or groups[-1].kind != "step":
            groups.append(_Group("step"))
        return groups[-1]

    for kind, data in events:
        if not isinstance(data, dict):
            data = {}
        if kind == EV_USER:
            if isinstance(data.get("text"), str):
                groups.append(_Group("user", text=data["text"]))
        elif kind == EV_UNSENT:
            if groups and groups[-1].kind == "user":
                groups.pop()
        elif kind == EV_ASSISTANT:
            groups.append(_Group("step", text=data["text"] if isinstance(data.get("text"), str) else ""))
        elif kind == EV_CALL:
            call_id, tool, args = data.get("call_id"), data.get("tool"), data.get("input")
            if isinstance(call_id, str) and isinstance(tool, str) and isinstance(args, dict):
                g = step_group()
                g.calls[call_id] = {"tool": tool, "input": args}
                index[call_id] = g
                if tool in CHECKPOINTED_TOOLS:
                    g.unmapped += 1
        elif kind == EV_RESULT:
            g = index.get(data.get("call_id"))
            if g is not None and isinstance(data.get("ok"), bool) and isinstance(data.get("output"), str):
                g.results[data["call_id"]] = data
        elif kind == EV_CLOSED:
            g = index.get(data.get("call_id"))
            if g is not None and isinstance(data.get("status"), str):
                g.closed[data["call_id"]] = data["status"]
        elif kind == EV_CHECKPOINT:
            step = data.get("step")
            if not isinstance(step, int) or isinstance(step, bool):
                continue
            g = index.get(data.get("call_id")) if data.get("call_id") is not None else None
            if g is None:
                g = next((x for x in reversed(groups) if x.kind == "step" and x.unmapped), None)
            if g is not None:
                g.steps.add(step)
                g.unmapped = max(g.unmapped - 1, 0)
        elif kind == EV_REWIND:
            n = data.get("to_step")
            if isinstance(n, int) and not isinstance(n, bool):
                del groups[_cut_index(groups, n):]
                index = {cid: g for g in groups if g.kind == "step" for cid in g.calls}

    per_group, shortened = [], 0
    for g in groups:
        items, short = _build_items(g)
        shortened += short
        if items:
            per_group.append(items)
    while per_group and per_group[0][0]["role"] != "user":
        per_group.pop(0)  # the first item must be a user item
    total = sum(len(g) for g in per_group)
    kept: list[list[dict]] = []
    size = count = 0
    for items in reversed(per_group):
        add = sum(_size(i) for i in items)
        if size + add > max_bytes or count + len(items) > max_items:
            break
        kept.append(items)
        size += add
        count += len(items)
    kept.reverse()
    while kept and kept[0][0]["role"] != "user":
        count -= len(kept.pop(0))
    flat = [i for items in kept for i in items]
    return History(flat, omitted=total - len(flat), shortened=shortened)
