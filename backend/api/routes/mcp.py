"""
api/routes/mcp.py -- Patch A8 (CLI + Skills + MCP implementation guide).

Patch A2 built eo/mcp_registry.py's list_mcp_servers() and (async)
mcp_server_status() specifically "for later patches (A8, the CLI
introspection commands) to call" (see that module's own docstrings),
but never wired an HTTP route to either -- the CLI is a separate
process that only ever speaks HTTP to the backend (same as the
frontend; see cli/minime_cli/api_client.py's own module docstring), so
a direct Python import isn't an option for `minime mcp list` /
`minime mcp status <name>`. This file is that missing HTTP surface:
purely additive, no new business logic -- both routes are thin
pass-throughs to functions A2 already wrote and already unit-tests.

New file rather than folding into api/routes/system.py: system.py's own
docstring scopes itself to health/quota/usage-history, and api/routes/
code.py's own docstring already establishes the "give a growing surface
its own file" precedent this repo follows.
"""
from fastapi import APIRouter, Depends

from api.deps import require_auth
from eo.mcp_registry import list_mcp_servers, mcp_server_status

router = APIRouter()


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
