"""
api/routes/mcp.py -- Patch A8 (CLI + Skills + MCP implementation guide)
+ Patch A4 (Safety Gate Extension for Mutating MCP Tools).

Patch A2 built eo/mcp_registry.py's list_mcp_servers() and (async)
mcp_server_status() specifically "for later patches (A8, the CLI
introspection commands) to call" (see that module's own docstrings),
but never wired an HTTP route to either -- the CLI is a separate
process that only ever speaks HTTP to the backend (same as the
frontend; see cli/minime_cli/api_client.py's own module docstring), so
a direct Python import isn't an option for `minime mcp list` /
`minime mcp status <name>`. The two GET routes below are that missing
HTTP surface: purely additive, no new business logic -- both are thin
pass-throughs to functions A2 already wrote and already unit-tests.

New file rather than folding into api/routes/system.py: system.py's own
docstring scopes itself to health/quota/usage-history, and api/routes/
code.py's own docstring already establishes the "give a growing surface
its own file" precedent this repo follows.

Patch A4 adds the propose -> confirm/deny -> execute HTTP surface for
MUTATING-classified MCP tools, workspace-scoped the same way
api/routes/local_workspace.py's own propose/confirm/deny trio already
is (same require_auth + chat_workspace.get_workspace ownership check,
same 400/404/409 split):
  - POST .../mcp/propose  body: {tool, arguments}  -> {action_id, tool,
    arguments, expires_in_seconds} -- nothing has touched the MCP
    server yet. `tool` is the f"mcp__{server}__{tool}" agent-tool name
    eo.mcp_agent_tools.mcp_tools_for_agent() hands an agent.
  - POST .../mcp/confirm  body: {action_id}  -> runs the proposed call
    for real (via eo.mcp_agent_tools.call_agent_mcp_tool()) and returns
    its result.
  - POST .../mcp/deny     body: {action_id}  -> discards the proposal;
    the MCP server is never contacted.
These three deliberately reuse eo/local_workspace_tools.py's SAME
propose/confirm/deny functions and pending-action store that
write_file/delete/execute_command already go through -- see that
module's own Patch A4 docstring section for why ("extend it, don't
duplicate it"). A read-only-classified MCP tool has no route here at
all -- it's called directly (no propose/confirm) through whatever
already calls eo.mcp_agent_tools.call_agent_mcp_tool(), same as
list_dir/read_file never touch api/routes/local_workspace.py's
propose/confirm/deny routes either.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from eo import chat_workspace
from eo.local_workspace_tools import (
    PENDING_ACTION_TTL_SECONDS,
    PendingActionError,
    confirm_action,
    deny_action,
    propose_mcp_action,
)
from eo.mcp_client import MCPClientError
from eo.mcp_registry import list_mcp_servers, mcp_server_status

router = APIRouter()


def _require_workspace(ws_id: str, owner_id: str) -> None:
    """Same check, same 404, as api/routes/local_workspace.py's own
    module-private helper of the same name -- not imported from there
    to keep these two route files independently deployable/testable,
    same posture that file's own docstring already documents for
    itself relative to its sibling routes."""
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")


@router.get("/api/mcp/servers")
def get_mcp_servers(owner_id: str = Depends(require_auth)):
    """Cheap, synchronous summary of every configured MCP server --
    same shape eo/mcp_registry.py's list_mcp_servers() already
    returns, unmodified. Doesn't attempt to connect anything; just
    reflects mcp_servers.json plus each server's current live-connection
    state.

    owner_id: accepted for parity with require_auth on every other
    route in this file (and the same pattern GET /api/skills already
    uses in api/routes/tasks.py), unused here -- MCP server config is a
    property of the deployment, not any one user or workspace.
    """
    return list_mcp_servers()


@router.get("/api/mcp/servers/{server_name}/status")
async def get_mcp_server_status(server_name: str, owner_id: str = Depends(require_auth)):
    """Detailed status for one server, including its live tool list
    when connected -- same shape eo/mcp_registry.py's
    mcp_server_status() already returns, unmodified. That function
    itself returns {"error": ...} for an unknown server name rather
    than raising, so this route deliberately does not translate that
    into an HTTP 404: an unrecognized server name is an ordinary
    result for a CLI status command to print, not an exceptional one
    worth a different status code (`minime mcp status` prints
    whatever comes back either way).

    owner_id: accepted for parity, unused, same reasoning as
    GET /api/mcp/servers above.
    """
    return await mcp_server_status(server_name)


# ---------------------------------------------------------------------
# Patch A4 — propose / confirm / deny for mutating-classified MCP tools.
# ---------------------------------------------------------------------

class ProposeMCPActionRequest(BaseModel):
    tool: str  # f"mcp__{server}__{tool}", e.g. "mcp__github__create_issue"
    arguments: dict[str, Any] = {}


class ProposeMCPActionResponse(BaseModel):
    action_id: str
    tool: str
    arguments: dict[str, Any]
    expires_in_seconds: int


class MCPActionIdRequest(BaseModel):
    action_id: str


@router.post("/api/workspaces/{ws_id}/mcp/propose", response_model=ProposeMCPActionResponse)
def mcp_propose_action(
    ws_id: str, req: ProposeMCPActionRequest, owner_id: str = Depends(require_auth)
):
    """Validates and stores the proposal; never contacts the MCP
    server. A 400 here means either `tool` isn't a well-formed
    f"mcp__{{server}}__{{tool}}" agent-tool name, or it's classified
    read_only rather than mutating -- see propose_mcp_action()'s own
    docstring for both cases. Distinct from the 404 below, which means
    "well-formed, but the action_id didn't check out."""
    _require_workspace(ws_id, owner_id)
    try:
        action = propose_mcp_action(ws_id, req.tool, req.arguments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return ProposeMCPActionResponse(
        action_id=action.action_id,
        tool=action.tool,
        arguments=action.params.get("arguments", {}),
        expires_in_seconds=PENDING_ACTION_TTL_SECONDS,
    )


@router.post("/api/workspaces/{ws_id}/mcp/confirm")
async def mcp_confirm_action(
    ws_id: str, req: MCPActionIdRequest, owner_id: str = Depends(require_auth)
):
    """Runs the proposed MCP tool call for real, via
    eo.local_workspace_tools.confirm_action() -- the SAME function
    api/routes/local_workspace.py's own confirm route calls; which
    server/tool it actually reaches depends on the pending action's
    `source`, not on which of these two HTTP routes was used to
    request it. 404 if action_id is unknown/expired/wrong-workspace;
    409 if the action was valid but execution itself failed (no
    connected MCP server, the server rejected the arguments, etc.) --
    same 409 convention every other propose/confirm route in this repo
    already uses for "well-formed request, execution-side failure.\""""
    _require_workspace(ws_id, owner_id)
    try:
        return await confirm_action(ws_id, req.action_id)
    except PendingActionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except MCPClientError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/workspaces/{ws_id}/mcp/deny")
def mcp_deny_action(
    ws_id: str, req: MCPActionIdRequest, owner_id: str = Depends(require_auth)
):
    """Discards a pending action. The MCP server is never contacted on
    this path. 404 under the same conditions as confirm above."""
    _require_workspace(ws_id, owner_id)
    try:
        deny_action(ws_id, req.action_id)
    except PendingActionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"action_id": req.action_id, "status": "denied"}
