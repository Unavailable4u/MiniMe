"""
minime_cli/commands/mcp_cmds.py -- `minime mcp list`, `minime mcp
status <name>`, Patch A8.

Pure plumbing, per the implementation guide's own framing of A8: both
commands are thin wrappers over the GET /api/mcp/servers and GET
/api/mcp/servers/{name}/status routes A8 itself adds
(api/routes/mcp.py) as the HTTP surface over eo/mcp_registry.py's
list_mcp_servers()/mcp_server_status() -- functions Patch A2 already
wrote specifically for these two commands to call.

`minime mcp status <name>` never raises for an unknown server name --
eo/mcp_registry.py's mcp_server_status() returns {"error": ...} for
that case rather than raising (see that function's own docstring), and
this command mirrors that: an unrecognized name is an ordinary result
to print, not an exceptional one.
"""
from __future__ import annotations

import click

from .. import auth
from ..api_client import ApiClient, ApiError
from ..config import ConfigError, load_config


def _client() -> ApiClient:
    return ApiClient(load_config())


@click.group()
def mcp():
    """Inspect configured MCP servers (read-only)."""


@mcp.command(name="list")
def list_servers_cmd():
    """List every configured MCP server and whether it's connected."""
    try:
        servers = _client().list_mcp_servers()
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))
    if not servers:
        click.echo("No MCP servers configured.")
        return
    for s in servers:
        state = "connected" if s.get("connected") else ("enabled" if s.get("enabled") else "disabled")
        click.echo(f"{s.get('name')}\t{state}\t{s.get('transport')}")


@mcp.command(name="status")
@click.argument("server_name")
def server_status_cmd(server_name):
    """Show detailed status (and live tool list, if connected) for one server."""
    try:
        status = _client().mcp_server_status(server_name)
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))

    if "error" in status:
        click.echo(f"{server_name}: {status['error']}")
        return

    click.echo(f"name:               {status.get('name')}")
    click.echo(f"enabled:            {status.get('enabled')}")
    click.echo(f"transport:          {status.get('transport')}")
    click.echo(f"connected:          {status.get('connected')}")
    click.echo(f"default_tool_trust: {status.get('default_tool_trust')}")

    if "tools_error" in status:
        click.echo(f"tools_error:        {status['tools_error']}")
        return

    tools = status.get("tools")
    if tools:
        click.echo("tools:")
        for t in tools:
            click.echo(f"  {t.get('name')}\t[{t.get('trust')}]\t{t.get('description') or ''}")
