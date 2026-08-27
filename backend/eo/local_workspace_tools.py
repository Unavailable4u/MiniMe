"""
eo/local_workspace_tools.py — F2 Part 3 + Part 4, the local-workspace
tool set an agent (or a route) can call, built on top of
eo/local_workspace.py's call_daemon() request/response bridge.

Part 3 shipped list_workspace_dir()/read_workspace_file() -- the
read-only path, which runs freely with no confirm step, per the F2
plan.

Part 4 adds write_file/delete/execute_command, which are NOT thin
call_daemon() wrappers the way the read tools are: per the F2 plan
("write/delete/execute require a confirm step by default -- same
propose-then-go-live split your Deploy Agent already uses"), nothing
here reaches the daemon on the first ask. Instead:
  - propose_action() validates a mutating tool + its params and stores
    a PendingAction, returning its action_id. Nothing has touched the
    daemon yet.
  - confirm_action() looks up a still-pending action by id and *then*
    calls call_daemon() for real -- this is the only path by which a
    write_file/delete/execute_command tool_call ever actually reaches
    a daemon.
  - deny_action() discards a pending action without ever calling the
    daemon at all.

The pending-action store below is an in-memory dict, same discipline
as eo/local_workspace.py's own `_connections`/`_pending` registries
one file over -- this is ephemeral coordination state tied to a live
process, not data that needs to survive a backend restart (a
mid-restart-pending write is just gone, and the frontend's propose
button is sitting right there to re-propose it, same as a page reload
losing an unsaved form).

Two other things live here, both from Part 3:
  - list_workspace_dir()/read_workspace_file(): unchanged from Part 3.
  - local_workspace_tools(): the OpenAI-tool-schema builder, same
    shape/convention as utils/capability_tools.py's
    study_progress_tools(). Still only covers the two read tools --
    Part 4 deliberately does NOT add write/delete/execute_command
    schemas here yet. An agent reaching for those needs the
    propose/confirm split surfaced through actual UI (Part 7's
    confirm/deny controls), not a tool call that appears to complete
    in one shot the way this list-of-schemas pattern implies; wiring
    a mutating local-workspace action into an agent's own tool list is
    left for whichever later part actually builds that human-in-the-
    loop UI path, not assumed here.

Patch A4 — Safety Gate Extension for Mutating MCP Tools -- extends
this SAME pending-action store and confirm/deny discipline to cover
MCP-sourced actions too, rather than writing a second, MCP-specific
confirm mechanism (see the implementation guide's own wording for this
patch). Concretely:
  - PendingAction now carries a `source` ("daemon" | "mcp"), so
    confirm_action()/deny_action() branch on where an action came from
    without needing two separate stores, two separate TTL sweeps, or
    two separate action_id namespaces.
  - propose_mcp_action() is the MCP-sourced analogue of propose_action()
    above: it validates that a given eo.mcp_agent_tools agent-tool name
    is well-formed AND classified "mutating" by eo.mcp_registry.
    classify_tool() (read-only MCP tools don't go through this gate at
    all -- they run freely via eo.mcp_agent_tools.call_agent_mcp_tool(),
    same as list_dir/read_file above never touch propose/confirm).
  - confirm_action()'s mcp branch hands the approved call to
    eo.mcp_agent_tools.call_agent_mcp_tool() rather than re-deriving
    the server_name/tool_name split or the MCP_TOOL_CALLED/_RESULT
    event-logging that module already does -- that module's own
    docstring asks exactly this of Patch A4 ("wrap THIS function...
    not duplicate the event-logging or name-parsing done here").

Place this file at: eo/local_workspace_tools.py
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import eo.mcp_agent_tools as mcp_agent_tools  # NEW — Patch A4
import eo.mcp_registry as mcp_registry  # NEW — Patch A4
from eo.local_workspace import (
    ToolCallError,
    call_daemon,
)
from eo.mcp_client import MCPClientError  # NEW — Patch A4
from relay.emitter import EventType, emit_workspace_event  # NEW — Part 5

__all__ = [
    "PENDING_ACTION_TTL_SECONDS",
    "MCPClientError",
    "PendingActionError",
    "ToolCallError",
    "confirm_action",
    "deny_action",
    "get_pending_action",
    "list_workspace_dir",
    "local_workspace_tools",
    "propose_action",
    "propose_mcp_action",
    "read_workspace_file",
]


# ---------------------------------------------------------------------
# Part 5 — event logging. Every local tool call (proposed/confirmed/
# denied/executed/result) emits onto the workspace's Pusher channel via
# relay/emitter.py's emit_workspace_event(), the same mechanism CO4's
# cache_hit/worker_pool_selection instrumentation already uses, so
# local-daemon activity shows up in the same decisionEvents-driven
# timeline (AgentStepList.jsx's chips, RoutingTraceGraph.jsx's nodes)
# instead of needing a bespoke log view of its own.
# ---------------------------------------------------------------------

# How much of a tool's own params/result to actually put in an event
# payload. This is a live-activity log, not the tool_result transport
# itself (that's still call_daemon()'s full, untruncated return value,
# used as-is by the HTTP route) -- a whole file's contents or a
# command's full stdout has no business riding on a Pusher event just
# to show a chip that says "wrote src/app.py" or "ran pytest -q".
_EVENT_FIELD_PREVIEW_CHARS = 200


def _preview(value: Any) -> Any:
    """Truncates a string field for event payloads; passes anything
    else (numbers, bools, None) through unchanged. Keeps this a
    one-line call at every emit site below instead of repeating the
    same isinstance/len check five times."""
    if isinstance(value, str) and len(value) > _EVENT_FIELD_PREVIEW_CHARS:
        return value[:_EVENT_FIELD_PREVIEW_CHARS] + "…"
    return value


def _tool_event_payload(tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """The small, display-oriented subset of a tool call's params worth
    putting on the wire for a log chip -- notably never `content` (
    write_file's full new file body), which is exactly the kind of
    payload emit_event()'s own docstring warns an event-emission
    failure must never be load-bearing for, and which would otherwise
    make every write_file proposal balloon the event payload with data
    the timeline UI has no use for."""
    payload: dict[str, Any] = {"tool": tool}
    if "path" in params:
        payload["path"] = _preview(params.get("path"))
    if "command" in params:
        payload["command"] = _preview(params.get("command"))
    if tool == "write_file":
        content = params.get("content")
        payload["content_bytes"] = len(content.encode("utf-8")) if isinstance(content, str) else None
    return payload


def _mcp_event_payload(agent_tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Same truncate-for-display purpose as _tool_event_payload() above,
    but shaped for an MCP-sourced action -- {server, tool, arguments}
    instead of {tool, path/command} -- matching the payload
    eo.mcp_agent_tools.call_agent_mcp_tool() already puts on
    MCP_TOOL_CALLED/_RESULT, so a chip built from a Patch A4 propose/
    confirm/deny event and one built from that module's own events
    look like the same kind of thing in the timeline."""
    try:
        server_name, tool_name = mcp_agent_tools._parse_agent_tool_name(agent_tool_name)
    except ValueError:
        # Shouldn't happen -- propose_mcp_action() already validated
        # this name before ever storing a PendingAction with it -- but
        # an event-payload helper is exactly the wrong place to raise,
        # per _emit_tool_event()'s own "never load-bearing" reasoning.
        server_name, tool_name = None, agent_tool_name
    return {
        "server": server_name,
        "tool": tool_name,
        "arguments": {k: _preview(v) for k, v in (arguments or {}).items()},
    }


def _emit_mcp_action_event(
    event_type: EventType,
    workspace_id: str,
    action: "PendingAction",
    ok: bool | None = None,
    error: str | None = None,
) -> None:
    payload = _mcp_event_payload(action.tool, action.params.get("arguments", {}))
    payload["action_id"] = action.action_id
    if ok is not None:
        payload["ok"] = ok
    if error is not None:
        payload["error"] = _preview(error)
    emit_workspace_event(event_type, workspace_id=workspace_id, agent="mcp_agent_tools", payload=payload)


def _emit_tool_event(
    event_type: EventType,
    workspace_id: str,
    tool: str,
    params: dict[str, Any],
    action_id: str | None = None,
    ok: bool | None = None,
    error: str | None = None,
) -> None:
    payload = _tool_event_payload(tool, params)
    if action_id is not None:
        payload["action_id"] = action_id
    if ok is not None:
        payload["ok"] = ok
    if error is not None:
        payload["error"] = _preview(error)
    # Fire-and-forget, same as every other emit_workspace_event() call
    # site (api/task_runner.py's PANEL_CONTENT_UPDATED/CODE_FILE_UPDATED)
    # -- a dead relay or unset Pusher env must never affect whether the
    # actual daemon call happens or what its result was.
    emit_workspace_event(event_type, workspace_id=workspace_id, agent="local_workspace", payload=payload)


async def list_workspace_dir(workspace_id: str, path: str = ".") -> dict[str, Any]:
    """Raises eo.local_workspace.ToolCallError if no daemon is
    connected for this workspace, the call times out, or the daemon
    reports a failure (bad path, not a directory, etc.) -- callers
    (api/routes/local_workspace.py) turn that into the HTTP error
    shape rather than handling it here, so this stays a thin,
    transport-agnostic wrapper any future caller (a route, an agent
    step) can reuse as-is."""
    params = {"path": path}
    _emit_tool_event(EventType.LOCAL_TOOL_EXECUTED, workspace_id, "list_dir", params)
    try:
        result = await call_daemon(workspace_id, "list_dir", params)
    except ToolCallError as exc:
        _emit_tool_event(EventType.LOCAL_TOOL_RESULT, workspace_id, "list_dir", params, ok=False, error=str(exc))
        raise
    _emit_tool_event(EventType.LOCAL_TOOL_RESULT, workspace_id, "list_dir", params, ok=True)
    return result


async def read_workspace_file(workspace_id: str, path: str) -> dict[str, Any]:
    """Same error contract as list_workspace_dir() above."""
    params = {"path": path}
    _emit_tool_event(EventType.LOCAL_TOOL_EXECUTED, workspace_id, "read_file", params)
    try:
        result = await call_daemon(workspace_id, "read_file", params)
    except ToolCallError as exc:
        _emit_tool_event(EventType.LOCAL_TOOL_RESULT, workspace_id, "read_file", params, ok=False, error=str(exc))
        raise
    _emit_tool_event(EventType.LOCAL_TOOL_RESULT, workspace_id, "read_file", params, ok=True)
    return result


# ---------------------------------------------------------------------
# Part 4 — propose / confirm / execute for write_file, delete, and
# execute_command.
# ---------------------------------------------------------------------

# Tool name -> the params it requires, checked at propose time so a
# malformed proposal is rejected immediately with a clear message
# rather than surfacing as a confusing daemon-side ToolError only once
# someone confirms it minutes later.
_MUTATING_TOOL_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "write_file": ("path", "content"),
    "delete": ("path",),
    "execute_command": ("command",),
}

# A proposal nobody confirms (someone opens the confirm dialog, then
# just... leaves) shouldn't sit forever -- same "don't hang around
# past usefulness" reasoning as call_daemon()'s own request timeout,
# just on a much longer, human timescale rather than a network one.
PENDING_ACTION_TTL_SECONDS = 15 * 60


class PendingActionError(Exception):
    """Raised by confirm_action()/deny_action() for: unknown action_id,
    an action_id that belongs to a different workspace than the one
    asked about, or an action that's expired past
    PENDING_ACTION_TTL_SECONDS. Callers (api/routes/local_workspace.py)
    turn this into a 404, distinct from ToolCallError's 409 (a
    well-formed, still-pending action whose *execution* failed)."""


@dataclass
class PendingAction:
    action_id: str
    workspace_id: str
    tool: str
    params: dict[str, Any]
    # Patch A4 — "daemon" (write_file/delete/execute_command, via
    # propose_action() below) or "mcp" (a mutating-classified MCP tool,
    # via propose_mcp_action() below). Defaults to "daemon" so every
    # call site that predates this patch keeps constructing
    # PendingAction the same way it always has. For a "mcp" action,
    # `tool` holds the full f"mcp__{server}__{tool}" agent-tool name
    # (not a bare tool name -- see propose_mcp_action()) and `params`
    # holds {"arguments": {...}} rather than the daemon tools' flat
    # path/content/command shape.
    source: Literal["daemon", "mcp"] = "daemon"
    created_at: float = field(default_factory=time.time)


# action_id -> PendingAction. Deliberately not keyed under workspace_id
# the way eo/local_workspace.py's `_connections` is -- an action_id is
# already globally unique (uuid4) and callers always have both ids
# available (the route path already carries workspace_id), so a single
# flat dict keeps confirm_action()/deny_action() O(1) without needing
# a nested structure just to mirror the connection registry's shape.
_pending_actions: dict[str, PendingAction] = {}


def _prune_expired() -> None:
    now = time.time()
    expired = [
        action_id
        for action_id, action in _pending_actions.items()
        if now - action.created_at > PENDING_ACTION_TTL_SECONDS
    ]
    for action_id in expired:
        _pending_actions.pop(action_id, None)


def propose_action(workspace_id: str, tool: str, params: dict[str, Any]) -> PendingAction:
    """Validates a mutating tool call and stores it as pending --
    nothing is sent to the daemon here. Raises ValueError for an
    unknown tool name or a missing required param, so a bad proposal
    fails fast with a clear message instead of storing something
    confirm_action() would only fail on later.
    """
    _prune_expired()

    required = _MUTATING_TOOL_REQUIRED_PARAMS.get(tool)
    if required is None:
        raise ValueError(
            f"{tool!r} is not a tool that goes through propose/confirm -- "
            f"use list_workspace_dir()/read_workspace_file() directly for "
            f"read tools, which run without confirmation"
        )
    missing = [name for name in required if name not in (params or {})]
    if missing:
        raise ValueError(f"missing required param(s) for {tool!r}: {', '.join(missing)}")

    action = PendingAction(
        action_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        tool=tool,
        params=dict(params or {}),
        source="daemon",
    )
    _pending_actions[action.action_id] = action
    _emit_tool_event(EventType.LOCAL_TOOL_PROPOSED, workspace_id, tool, action.params, action_id=action.action_id)
    return action


def propose_mcp_action(
    workspace_id: str, agent_tool_name: str, arguments: dict[str, Any] | None = None
) -> PendingAction:
    """Patch A4's MCP-sourced analogue of propose_action() above,
    added to the SAME _pending_actions store (see module docstring) --
    one propose->confirm->execute discipline, not a second,
    MCP-specific mechanism.

    `agent_tool_name` is the f"mcp__{server}__{tool}" name
    eo.mcp_agent_tools.mcp_tools_for_agent() hands an agent -- this
    reuses that module's own name parsing rather than re-deriving it.

    Raises ValueError if:
      - agent_tool_name isn't a well-formed "mcp__{server}__{tool}"
        name (same failure eo.mcp_agent_tools._parse_agent_tool_name()
        itself raises for a malformed name), or
      - eo.mcp_registry.classify_tool() classifies this tool
        "read_only", not "mutating" -- a read-only MCP tool has no
        business going through propose/confirm (mirrors
        propose_action()'s own rejection of list_dir/read_file above);
        callers should call eo.mcp_agent_tools.call_agent_mcp_tool()
        directly for those instead, same as they already do for
        list_workspace_dir()/read_workspace_file().
    """
    _prune_expired()

    server_name, tool_name = mcp_agent_tools._parse_agent_tool_name(agent_tool_name)

    trust = mcp_registry.classify_tool(server_name, tool_name)
    if trust != "mutating":
        raise ValueError(
            f"{agent_tool_name!r} is classified {trust!r} by mcp_servers.json, "
            f"not 'mutating' -- it runs freely via "
            f"eo.mcp_agent_tools.call_agent_mcp_tool(), it doesn't go "
            f"through propose/confirm"
        )

    action = PendingAction(
        action_id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        tool=agent_tool_name,
        params={"arguments": dict(arguments or {})},
        source="mcp",
    )
    _pending_actions[action.action_id] = action
    _emit_mcp_action_event(EventType.MCP_TOOL_PROPOSED, workspace_id, action)
    return action


def get_pending_action(workspace_id: str, action_id: str) -> PendingAction:
    """Raises PendingActionError if action_id is unknown, expired, or
    belongs to a different workspace -- the last case matters because
    action_id alone would otherwise let one workspace's caller poke at
    another workspace's pending action just by guessing/reusing an id."""
    _prune_expired()
    action = _pending_actions.get(action_id)
    if action is None or action.workspace_id != workspace_id:
        raise PendingActionError(f"no pending action {action_id!r} for this workspace")
    return action


async def confirm_action(workspace_id: str, action_id: str) -> dict[str, Any]:
    """The only path by which a write_file/delete/execute_command
    tool_call ever actually reaches call_daemon() (action.source ==
    "daemon"), or a mutating-classified MCP tool call ever actually
    reaches eo.mcp_agent_tools.call_agent_mcp_tool() (action.source ==
    "mcp", Patch A4). Pops the pending action (so it can't be confirmed
    twice) *before* executing it either way -- if execution itself then
    fails (ToolCallError for a daemon action, MCPClientError for an MCP
    one), the action is still gone; the caller re-proposes rather than
    this silently allowing a second confirm attempt to retry the exact
    same stale proposal.
    """
    action = get_pending_action(workspace_id, action_id)
    _pending_actions.pop(action_id, None)

    if action.source == "mcp":
        # Patch A4's mcp branch: emit the CONFIRMED lifecycle event,
        # then hand off to eo.mcp_agent_tools.call_agent_mcp_tool() --
        # that function already does the server_name/tool_name split
        # and its own MCP_TOOL_CALLED/_RESULT event pair around the
        # real eo.mcp_client.call_mcp_tool() round trip, so this branch
        # deliberately does NOT re-derive either (see module docstring
        # and that module's own "Patch A4 is expected to wrap THIS
        # function" note). No separate MCP_TOOL_RESULT emission is
        # needed here on the failure path either -- call_agent_mcp_tool()
        # already emits one before re-raising.
        _emit_mcp_action_event(EventType.MCP_TOOL_CONFIRMED, workspace_id, action)
        arguments = action.params.get("arguments", {})
        return await mcp_agent_tools.call_agent_mcp_tool(
            action.tool, arguments, workspace_id=workspace_id
        )

    _emit_tool_event(
        EventType.LOCAL_TOOL_CONFIRMED, workspace_id, action.tool, action.params, action_id=action_id
    )
    try:
        # action_id is threaded through to call_daemon() (Part 7) purely
        # so any tool_stream chunks execute_command produces while this
        # call is in flight (see eo/local_workspace.py's
        # _forward_stream_chunk) can be tagged with the action they
        # belong to -- write_file/delete never stream, so this is a
        # no-op for those two, just a slightly-unused kwarg.
        result = await call_daemon(workspace_id, action.tool, action.params, action_id=action_id)
    except ToolCallError as exc:
        _emit_tool_event(
            EventType.LOCAL_TOOL_RESULT, workspace_id, action.tool, action.params,
            action_id=action_id, ok=False, error=str(exc),
        )
        raise
    _emit_tool_event(
        EventType.LOCAL_TOOL_RESULT, workspace_id, action.tool, action.params,
        action_id=action_id, ok=True,
    )
    return result


def deny_action(workspace_id: str, action_id: str) -> None:
    """Discards a pending action without ever contacting the daemon or
    MCP server. Raises PendingActionError under the same conditions as
    get_pending_action() -- denying an already-gone action is still an
    error, not a silent no-op, so a caller can tell "you denied
    something real" apart from "that action_id was never valid"."""
    action = get_pending_action(workspace_id, action_id)
    _pending_actions.pop(action_id, None)
    if action.source == "mcp":
        _emit_mcp_action_event(EventType.MCP_TOOL_DENIED, workspace_id, action)
    else:
        _emit_tool_event(EventType.LOCAL_TOOL_DENIED, workspace_id, action.tool, action.params, action_id=action_id)


def local_workspace_tools() -> list[dict[str, Any]]:
    """Hand-written, not manifest-driven -- same reasoning
    utils/capability_tools.py's study_progress_tools() docstring
    already gives for its own non-manifest tools: this isn't a
    generation target, it's a read action against a specific machine's
    paired folder, so it doesn't belong in CAPABILITIES_MANIFEST."""
    return [
        {
            "type": "function",
            "function": {
                "name": "list_local_dir",
                "description": (
                    "List files and folders inside the user's locally "
                    "paired project folder (the F2 local daemon feature). "
                    "Only works if a daemon is currently connected for "
                    "this workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to the paired folder's root. "
                                "Omit, or use '.', for the root itself."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_local_file",
                "description": (
                    "Read the text contents of one file inside the user's "
                    "locally paired project folder (the F2 local daemon "
                    "feature). Only works if a daemon is currently "
                    "connected for this workspace, and only for UTF-8 "
                    "text files under the read-size limit -- binary or "
                    "oversized files come back as an error, not partial "
                    "or garbled content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the paired folder's root.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    ]
