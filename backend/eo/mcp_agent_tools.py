"""
eo/mcp_agent_tools.py — Patch A3: Wire MCP Tools Into the Agent
Tool-Calling Loop.

Where this sits: Patch A1 (eo/mcp_client.py) proves the MCP transport
works and normalizes tool discovery into MCPTool. Patch A2
(eo/mcp_registry.py) decides which servers are configured/enabled and
connects them at startup. Neither patch's own docstring wants this
patch's job done in its file (see both modules' "explicitly NOT this
patch's job" sections) -- this module is the third, agent-facing
layer: turn A1/A2's live connections into (a) an OpenAI-style tool
list an agent's tool-calling loop can reason over, indistinguishable
in shape from the internal tools it already sees, and (b) a single
callable dispatch surface for actually invoking one of those tools,
logged the same way local_workspace_tools.py already logs a daemon
tool call.

What "the agent tool-calling loop" actually is in this repo today
(read this before extending anything): there is no generic_worker.py
tool-calling path -- agents/generic_worker.py's run() never passes a
`tools` kwarg to generate_text() at all. The one real, live tool-list
+ tool-choice call site is api/routes/notebooks.py's classify_intent()
route, which builds an OpenAI tools array (utils.capability_tools.
manifest_to_tools() + study_progress_tools()) and hands it to
utils.llm_client.classify_tool_intent(). That function's own docstring
is explicit that it is "log-only by design" -- it returns which tool
the model picked, but nothing in this codebase yet executes that pick
(that's the still-unbuilt "step 2.6" its docstring names). This is
true for internal tools as much as MCP ones; it is not a gap this
patch introduces or is scoped to close.

So, honestly scoped, this patch does exactly what A1's own docstring
asked of "later patches": normalize MCP tool discovery into the same
shape as internal tools, hand it to the same tool-list call site, and
give whatever eventually executes a chosen tool call (today, nothing;
tomorrow, "step 2.6") one function to call that does the right thing
-- routes to the right server, and event-logs the call the same way a
daemon tool call already is. It does not invent a new, MCP-only
auto-dispatch loop, since that would be a second, parallel path for
MCP tools only while internal tools still have none -- exactly the
kind of duplicate mechanism Patch A0's decision record and this
module's sibling patches keep warning against.

Tool naming: an internal tool name alone ("search_issues") doesn't say
which server to route a call to, and two configured servers could
plausibly expose a same-named tool (eo.mcp_client.MCPTool.server_name
exists for exactly this reason). Every tool this module hands to an
agent is therefore named f"mcp__{server_name}__{tool_name}" -- the
double-underscore separator keeps it visually distinct from this
repo's own f"generate_{key}" / f"mark_topic_done" naming, and
_parse_agent_tool_name() below is the exact inverse, so a caller that
only has the agent-facing name can always recover which server to
call without guessing or a second lookup.

Only CONNECTED servers are offered (mcp_registry.list_mcp_servers()'s
`connected` field) -- an enabled-but-not-yet-connected server (bad
token, `npx` not on PATH, still starting up) has no live tools/list
result to build real schemas from, and offering a tool an agent can
then never actually call is worse than just not offering it this
turn; the next call to mcp_tools_for_agent() will pick it up as soon
as it's actually connected.

Patch SX-25 (injection guard): an MCP server is third-party code, and
both what it says a tool does (the description) and what a tool call
returns can carry instructions aimed at the model that reads them. This
module is the one place both pass through on their way to a model, so
both are screened here with eo.injection_guard.score_snippet(). Flagged
text is replaced by a fixed notice (never forwarded) and the original is
printed to the log; clean text is forwarded unchanged. Wrapping the text
that IS forwarded as "untrusted tool output" is the job of whatever
eventually puts a tool result into a prompt -- nothing does yet (see "What
the agent tool-calling loop actually is" above).

Place this file at: eo/mcp_agent_tools.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from typing import Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eo.mcp_client as mcp_client
import eo.mcp_registry as mcp_registry
from eo import injection_guard
from eo.mcp_client import MCPClientError
from relay.emitter import EventType, emit_workspace_event

__all__ = [
    "AGENT_TOOL_NAME_SEPARATOR",
    "call_agent_mcp_tool",
    "mcp_tools_for_agent",
]

# See module docstring's "Tool naming" section.
AGENT_TOOL_NAME_SEPARATOR = "__"
_AGENT_TOOL_NAME_PREFIX = f"mcp{AGENT_TOOL_NAME_SEPARATOR}"

# Same reasoning, same number, as local_workspace_tools.py's own
# _EVENT_FIELD_PREVIEW_CHARS: this is a live-activity log payload, not
# the tool_result transport itself (that's call_agent_mcp_tool()'s
# actual return value) -- a large arguments blob (e.g. a full file
# body some MCP tool takes as a param) has no business riding on a
# Pusher event just to show a chip that says "called search_issues".
_EVENT_FIELD_PREVIEW_CHARS = 200


def _agent_tool_name(server_name: str, tool_name: str) -> str:
    return f"{_AGENT_TOOL_NAME_PREFIX}{server_name}{AGENT_TOOL_NAME_SEPARATOR}{tool_name}"


def _parse_agent_tool_name(agent_tool_name: str) -> tuple[str, str]:
    """Inverse of _agent_tool_name(). Raises ValueError for anything
    that isn't a well-formed f"mcp__{server}__{tool}" name -- a
    dispatcher handed a name this module didn't generate (a typo'd
    literal, an internal tool name passed here by mistake) should fail
    loudly and immediately, not silently route to the wrong server or
    unpack into the wrong number of parts.

    Split with maxsplit=2 because an MCP tool's own name is free to
    contain "__" itself (nothing in the MCP spec forbids it) -- only
    the prefix and the server-name boundary are this module's to
    parse; everything after the second separator is the tool name,
    whole."""
    if not agent_tool_name.startswith(_AGENT_TOOL_NAME_PREFIX):
        raise ValueError(
            f"{agent_tool_name!r} is not an MCP agent-tool name "
            f"(expected the {_AGENT_TOOL_NAME_PREFIX!r} prefix)"
        )
    remainder = agent_tool_name[len(_AGENT_TOOL_NAME_PREFIX):]
    parts = remainder.split(AGENT_TOOL_NAME_SEPARATOR, 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"{agent_tool_name!r} is not a well-formed "
            f"f'mcp__{{server}}__{{tool}}' agent-tool name"
        )
    server_name, tool_name = parts
    return server_name, tool_name


def _preview(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _EVENT_FIELD_PREVIEW_CHARS:
        return value[:_EVENT_FIELD_PREVIEW_CHARS] + "…"
    return value


def _tool_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """A server that returns no/empty inputSchema for a tool (allowed
    by the MCP spec -- some tools genuinely take no arguments) still
    needs a valid OpenAI-shape `parameters` object, not `None` --
    mirrors utils/capability_tools.py's own _parameters_for_scope()
    "whole" branch (empty properties/required, still a real object
    schema) rather than inventing a second empty-schema convention."""
    if not schema:
        return {"type": "object", "properties": {}, "required": []}
    return schema


# ---------------------------------------------------------------------
# Patch SX-25: injection guard on what an MCP server sends back
# ---------------------------------------------------------------------
#
# Unlike eo.injection_guard.filter_snippets() (flag-only: a false positive
# must not silently delete real product data), this is a hard block: flagged
# text is replaced, not marked. An MCP server's own text is not product data
# the user asked for, and a withheld tool description or result is easy to
# spot and re-run. Same fail-open rule as the guard itself: if the classifier
# is unavailable or errors, the text is forwarded (see injection_guard.py).
#
# score_snippet() looks at the first ~5,000 characters of each string it is
# given (its _MAX_CHUNKS_PER_SNIPPET), so every text block of a result is
# screened separately rather than one joined string.
RESULT_WITHHELD_NOTICE = (
    "[This MCP tool output was withheld: an automated check flagged it as "
    "possible prompt injection.]"
)
DESCRIPTION_WITHHELD_NOTICE = (
    "[This tool's description was withheld: an automated check flagged it as "
    "possible prompt injection.]"
)

# How much of a flagged original goes to the log (repr-escaped, so a newline in
# it cannot fake a log line).
_LOG_ORIGINAL_CHARS = 4000

# mcp_tools_for_agent() runs on every classify_intent request, and each
# classification is a local model pass, so a description's verdict is kept for
# a while, keyed on a hash of (server, tool, description): a changed
# description is screened again. The short expiry means a classifier that was
# briefly unavailable (and so fail-open) is not trusted for long.
_DESCRIPTION_CACHE_TTL_SECONDS = 600.0
_DESCRIPTION_CACHE_MAX_ENTRIES = 1024
_description_verdicts: dict[str, tuple[bool, float]] = {}


def _now() -> float:
    return time.monotonic()


def _log_flagged(what: str, label: str, reason: str, text: str) -> None:
    shown = text[:_LOG_ORIGINAL_CHARS]
    rest = len(text) - len(shown)
    tail = f" ... [{rest} more chars not logged]" if rest else ""
    print(f"  [mcp_agent_tools] injection guard flagged {what} from {label} ({reason}); "
          f"withheld from the model. Original ({len(text)} chars): {shown!r}{tail}")


async def _flagged(text: Any, label: str, what: str) -> bool | None:
    """True if `text` should be withheld, False if it is fine, None if the guard
    itself failed (treated as "fine" by callers, but not remembered). The
    classifier is a synchronous local model call, so it runs in a worker thread
    rather than on the event loop."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        verdict = await asyncio.to_thread(injection_guard.score_snippet, text, source_label=label)
    except Exception as exc:
        print(f"  [mcp_agent_tools] injection guard failed for {label} "
              f"(fail-open, forwarding): {exc.__class__.__name__}: {exc}")
        return None
    if not verdict.get("flagged"):
        return False
    _log_flagged(what, label, str(verdict.get("reason", "")), text)
    return True


async def _guard_result(server_name: str, tool_name: str, result: Any) -> Any:
    """Screens every piece of text in an MCP tool result: text blocks, the text
    of embedded resources, and `structuredContent`. Returns `result` itself when
    nothing was flagged, otherwise a copy with only the flagged pieces replaced
    by RESULT_WITHHELD_NOTICE (the original object is never modified). Images
    and other binary blocks are not text and are not sent to the classifier."""
    if not isinstance(result, dict):
        return result
    label = f"mcp:{server_name}/{tool_name}"
    changes: dict[str, Any] = {}

    content = result.get("content")
    if isinstance(content, list):
        new_content = list(content)
        changed = False
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if await _flagged(block.get("text"), label, "tool output"):
                new_content[i] = {**block, "text": RESULT_WITHHELD_NOTICE}
                changed = True
                continue
            resource = block.get("resource")
            if isinstance(resource, dict) and await _flagged(resource.get("text"), label, "tool output"):
                new_content[i] = {**block, "resource": {**resource, "text": RESULT_WITHHELD_NOTICE}}
                changed = True
        if changed:
            changes["content"] = new_content

    structured = result.get("structuredContent")
    if structured is not None:
        try:
            as_text = json.dumps(structured, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            as_text = ""
        if await _flagged(as_text, label, "structured tool output"):
            changes["structuredContent"] = {"withheld": RESULT_WITHHELD_NOTICE}

    return {**result, **changes} if changes else result


async def _guarded_description(server_name: str, tool_name: str, description: Any) -> Any:
    """`description` unchanged, or DESCRIPTION_WITHHELD_NOTICE if it was flagged."""
    if not isinstance(description, str) or not description.strip():
        return description
    key = hashlib.sha256(f"{server_name}\0{tool_name}\0{description}".encode("utf-8", "replace")).hexdigest()
    now = _now()
    cached = _description_verdicts.get(key)
    if cached is not None and cached[1] > now:
        flagged = cached[0]
    else:
        verdict = await _flagged(description, f"mcp:{server_name}/{tool_name} description", "tool description")
        flagged = bool(verdict)
        if verdict is not None:  # a failed check is not remembered
            if len(_description_verdicts) >= _DESCRIPTION_CACHE_MAX_ENTRIES:
                _description_verdicts.clear()
            _description_verdicts[key] = (flagged, now + _DESCRIPTION_CACHE_TTL_SECONDS)
    return DESCRIPTION_WITHHELD_NOTICE if flagged else description


async def mcp_tools_for_agent(*, path: str | None = None) -> list[dict[str, Any]]:
    """Builds the OpenAI-style tools array for every currently
    CONNECTED, enabled MCP server (see module docstring for why only
    connected servers are offered). Intended to be concatenated onto
    the same tools list the internal tools already build --
    api/routes/notebooks.py's classify_intent() route is today's one
    real call site (utils.capability_tools.manifest_to_tools() +
    study_progress_tools() + this).

    `path` is passed straight through to mcp_registry.list_mcp_servers()
    -- same test-override knob every mcp_registry function already
    takes, not a new convention.

    A single server's tools/list call failing mid-loop (transient MCP
    server hiccup) is logged and that one server is skipped, not fatal
    to every other connected server's tools still being offered this
    turn -- same "one down provider doesn't take out the others"
    posture mcp_registry.connect_configured_servers() already has for
    the connect step; this is that same posture one call later, at the
    discovery step.
    """
    tools: list[dict[str, Any]] = []
    for server in mcp_registry.list_mcp_servers(path):
        if not server["connected"]:
            continue
        try:
            mcp_tools = await mcp_client.list_tools(server["name"])
        except MCPClientError as exc:
            print(f"  [mcp_agent_tools] tools/list failed for {server['name']!r}, "
                  f"skipping this server this turn: {exc}")
            continue
        for tool in mcp_tools:
            description = await _guarded_description(tool.server_name, tool.name, tool.description)  # SX-25
            tools.append({
                "type": "function",
                "function": {
                    "name": _agent_tool_name(tool.server_name, tool.name),
                    # Server name prefixed into the description itself
                    # (not just the tool name) -- the tool-calling model
                    # never sees the "__" naming convention explained
                    # anywhere, so it needs the server's identity spelled
                    # out in plain language to reason about which tool
                    # is which when two servers expose similar-sounding
                    # tools.
                    "description": f"[{tool.server_name} MCP server] {description}",
                    "parameters": _tool_input_schema(tool.input_schema),
                },
            })
    return tools


async def call_agent_mcp_tool(
    agent_tool_name: str,
    arguments: dict[str, Any],
    *,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """The single call surface for actually invoking one of
    mcp_tools_for_agent()'s tools once an agent (today: a future
    dispatcher; see module docstring) has picked one. Parses the
    server_name back out of agent_tool_name, calls
    eo.mcp_client.call_mcp_tool(), and logs the call through
    emit_workspace_event() -- the SAME event-logging mechanism
    eo/local_workspace_tools.py's _emit_tool_event() already uses for
    daemon tool calls (Patch A3's own stated key requirement: an
    agent-triggered MCP tool call must show up in the same
    decisionEvents-driven activity timeline a write_file call does,
    not a second, invisible path).

    Raises MCPClientError on failure or ValueError on a malformed
    agent_tool_name -- never returns a partial/ambiguous result, same
    contract eo.mcp_client.call_mcp_tool() itself already documents
    and that eo/local_workspace_tools.py's confirm_action() mirrors for
    the daemon side.

    No propose/confirm safety gating happens here on purpose (see
    eo/mcp_client.py's own docstring, "out of scope" / Patch A4) --
    every MCP tool discovered by mcp_tools_for_agent() is callable
    immediately through this function. A4 is expected to wrap THIS
    function the same way eo/local_workspace_tools.py wraps
    call_daemon(), not to duplicate the event-logging or name-parsing
    done here.
    """
    server_name, tool_name = _parse_agent_tool_name(agent_tool_name)

    event_payload = {
        "server": server_name,
        "tool": tool_name,
        "arguments": {k: _preview(v) for k, v in (arguments or {}).items()},
    }
    emit_workspace_event(EventType.MCP_TOOL_CALLED, workspace_id=workspace_id,
                          agent="mcp_agent_tools", payload=event_payload)
    try:
        result = await mcp_client.call_mcp_tool(server_name, tool_name, arguments or {})
    except MCPClientError as exc:
        emit_workspace_event(EventType.MCP_TOOL_RESULT, workspace_id=workspace_id,
                              agent="mcp_agent_tools",
                              payload={"server": server_name, "tool": tool_name,
                                       "ok": False, "error": _preview(str(exc))})
        raise
    emit_workspace_event(EventType.MCP_TOOL_RESULT, workspace_id=workspace_id,
                          agent="mcp_agent_tools",
                          payload={"server": server_name, "tool": tool_name, "ok": True})
    return await _guard_result(server_name, tool_name, result)  # Patch SX-25
