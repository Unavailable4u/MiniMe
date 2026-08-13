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

Place this file at: eo/local_workspace_tools.py
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

from eo.local_workspace import ToolCallError, call_daemon  # noqa: F401 -- re-exported for callers

__all__ = [
    "ToolCallError",
    "list_workspace_dir",
    "read_workspace_file",
    "local_workspace_tools",
    "PendingActionError",
    "PENDING_ACTION_TTL_SECONDS",
    "propose_action",
    "confirm_action",
    "deny_action",
    "get_pending_action",
]


async def list_workspace_dir(workspace_id: str, path: str = ".") -> Dict[str, Any]:
    """Raises eo.local_workspace.ToolCallError if no daemon is
    connected for this workspace, the call times out, or the daemon
    reports a failure (bad path, not a directory, etc.) -- callers
    (api/routes/local_workspace.py) turn that into the HTTP error
    shape rather than handling it here, so this stays a thin,
    transport-agnostic wrapper any future caller (a route, an agent
    step) can reuse as-is."""
    return await call_daemon(workspace_id, "list_dir", {"path": path})


async def read_workspace_file(workspace_id: str, path: str) -> Dict[str, Any]:
    """Same error contract as list_workspace_dir() above."""
    return await call_daemon(workspace_id, "read_file", {"path": path})


# ---------------------------------------------------------------------
# Part 4 — propose / confirm / execute for write_file, delete, and
# execute_command.
# ---------------------------------------------------------------------

# Tool name -> the params it requires, checked at propose time so a
# malformed proposal is rejected immediately with a clear message
# rather than surfacing as a confusing daemon-side ToolError only once
# someone confirms it minutes later.
_MUTATING_TOOL_REQUIRED_PARAMS: Dict[str, tuple[str, ...]] = {
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
    params: Dict[str, Any]
    created_at: float = field(default_factory=time.time)


# action_id -> PendingAction. Deliberately not keyed under workspace_id
# the way eo/local_workspace.py's `_connections` is -- an action_id is
# already globally unique (uuid4) and callers always have both ids
# available (the route path already carries workspace_id), so a single
# flat dict keeps confirm_action()/deny_action() O(1) without needing
# a nested structure just to mirror the connection registry's shape.
_pending_actions: Dict[str, PendingAction] = {}


def _prune_expired() -> None:
    now = time.time()
    expired = [
        action_id
        for action_id, action in _pending_actions.items()
        if now - action.created_at > PENDING_ACTION_TTL_SECONDS
    ]
    for action_id in expired:
        _pending_actions.pop(action_id, None)


def propose_action(workspace_id: str, tool: str, params: Dict[str, Any]) -> PendingAction:
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
    )
    _pending_actions[action.action_id] = action
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


async def confirm_action(workspace_id: str, action_id: str) -> Dict[str, Any]:
    """The only path by which a write_file/delete/execute_command
    tool_call ever actually reaches call_daemon(). Pops the pending
    action (so it can't be confirmed twice) *before* calling the
    daemon -- if the daemon call itself then fails (ToolCallError), the
    action is still gone; the caller re-proposes rather than this
    silently allowing a second confirm attempt to retry the exact same
    stale proposal.
    """
    action = get_pending_action(workspace_id, action_id)
    _pending_actions.pop(action_id, None)
    return await call_daemon(workspace_id, action.tool, action.params)


def deny_action(workspace_id: str, action_id: str) -> None:
    """Discards a pending action without ever contacting the daemon.
    Raises PendingActionError under the same conditions as
    get_pending_action() -- denying an already-gone action is still an
    error, not a silent no-op, so a caller can tell "you denied
    something real" apart from "that action_id was never valid"."""
    get_pending_action(workspace_id, action_id)
    _pending_actions.pop(action_id, None)


def local_workspace_tools() -> List[Dict[str, Any]]:
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
