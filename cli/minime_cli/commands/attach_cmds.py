"""
minime_cli/commands/attach_cmds.py -- `minime attach`, Patch A7.

Reuses daemon/config.py's own generate_pairing_token() and
daemon/path_guard.py's own assert_safe_root() -- imported live from the
local MiniMe checkout the daemon actually lives in (see
../daemon_bridge.py), not reimplemented here, so this command can never
quietly drift from what the daemon itself will validate at its own
startup.

This command only ever WRITES daemon/.env. It never starts the daemon
process itself -- `python -m daemon.minime_daemon` (see
daemon/README.md), run separately, is still an explicit step a person
takes on their own, so they always see exactly what's about to run
against their filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path

import click

from .. import auth
from ..api_client import ApiClient, ApiError
from ..config import ConfigError, load_config
from ..daemon_bridge import DaemonBridgeError, load_daemon_modules, write_daemon_env


def _resolve_workspace_id(client: ApiClient, explicit: str | None) -> str:
    """An explicit --workspace-id wins outright, with no network call.
    Otherwise, ask the backend which workspaces this account can see
    and let the person pick -- MINIME_WORKSPACE_ID has to match a real
    workspace_id (daemon/.env.example's own words), so guessing one is
    worse than one extra round trip.
    """
    if explicit:
        return explicit

    workspaces = client.list_workspaces()
    if not workspaces:
        raise click.ClickException(
            "No workspaces found on this account. Create one in the web app "
            "first, or pass --workspace-id explicitly."
        )
    if len(workspaces) == 1:
        only = workspaces[0]
        click.echo(f"Using your only workspace: {only.get('name', '(untitled)')} [{only['id']}]")
        return only["id"]

    click.echo("Multiple workspaces found -- pick one:")
    for i, ws in enumerate(workspaces, start=1):
        click.echo(f"  {i}. {ws.get('name', '(untitled)')}  [{ws['id']}]")
    choice = click.prompt("Workspace number", type=click.IntRange(1, len(workspaces)))
    return workspaces[choice - 1]["id"]


def _default_backend_ws_url(api_url: str) -> str:
    """Derives ws(s):// from the CLI's own configured http(s):// api_url
    -- same host the browser already hits, just a different scheme --
    rather than asking the person to retype a URL they already gave us
    once via `minime configure` / MINIME_API_URL."""
    if api_url.startswith("https://"):
        return "wss://" + api_url[len("https://"):]
    if api_url.startswith("http://"):
        return "ws://" + api_url[len("http://"):]
    raise click.ClickException(
        f"Can't derive a ws(s):// URL from api_url={api_url!r} -- pass "
        "--backend-ws-url explicitly."
    )


@click.command()
@click.argument("project_path", required=False, type=click.Path(file_okay=False))
@click.option("--daemon-dir", default=None,
              help="Path to a local MiniMe checkout containing daemon/ "
                   "(overrides MINIME_DAEMON_DIR / `minime configure --daemon-dir`).")
@click.option("--workspace-id", default=None,
              help="Workspace to pair the daemon as. Prompts from your workspace list if omitted.")
@click.option("--backend-ws-url", default=None,
              help="Overrides the ws(s):// URL derived from your configured api_url.")
@click.option("--yes", "-y", is_flag=True,
              help="Skip the confirmation prompt (the values are still printed first).")
def attach(project_path, daemon_dir, workspace_id, backend_ws_url, yes):
    """Pair the local daemon to a project folder.

    Writes daemon/.env inside a MiniMe checkout so `python -m
    daemon.minime_daemon` (run separately -- see daemon/README.md) can
    pick it up. PROJECT_PATH is the one folder the daemon will be
    allowed to read/write/execute inside; defaults to the current
    directory, since the common case is running `minime attach` from
    inside the project you want paired.

    This never invents a new pairing mechanism -- it fills in the same
    daemon/.env the daemon has always read, using the daemon's own
    token generator and root-safety check.
    """
    try:
        cli_cfg = load_config()
        bridge = load_daemon_modules(daemon_dir or cli_cfg.daemon_dir)
    except (ConfigError, DaemonBridgeError) as e:
        raise click.ClickException(str(e))

    candidate_root = Path(project_path) if project_path else Path(os.getcwd())
    try:
        resolved_root = bridge.path_guard.assert_safe_root(candidate_root)
    except bridge.path_guard.PathGuardError as e:
        raise click.ClickException(str(e))

    try:
        resolved_workspace_id = _resolve_workspace_id(ApiClient(cli_cfg), workspace_id)
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))

    resolved_ws_url = backend_ws_url or _default_backend_ws_url(cli_cfg.api_url)
    token = bridge.config.generate_pairing_token()

    overwriting = bridge.env_path.exists()
    click.echo(("Overwriting" if overwriting else "About to write") + f" {bridge.env_path} with:")
    click.echo(f"  MINIME_ALLOWED_ROOT   = {resolved_root}")
    click.echo(f"  MINIME_WORKSPACE_ID   = {resolved_workspace_id}")
    click.echo(f"  MINIME_BACKEND_WS_URL = {resolved_ws_url}")
    click.echo("  MINIME_PAIRING_TOKEN  = <freshly generated>")

    # Never silently write a root the person hasn't actually looked at.
    # assert_safe_root() above only rejects the obviously unsafe cases
    # (/, the home directory) -- a folder that's merely too broad for
    # THIS particular pairing (e.g. a monorepo root instead of the one
    # package inside it) is still the person's own judgment call to
    # catch, which is exactly why this confirmation exists at all.
    if not yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted -- daemon/.env left unchanged.")
        return

    write_daemon_env(
        bridge.env_path,
        pairing_token=token,
        allowed_root=str(resolved_root),
        backend_ws_url=resolved_ws_url,
        workspace_id=resolved_workspace_id,
    )
    click.echo(f"Wrote {bridge.env_path}.")
    click.echo(
        "Next: `pip install -r daemon/requirements.txt` (if you haven't "
        f"already), then run `python -m daemon.minime_daemon` from inside "
        f"{bridge.daemon_dir} to start it."
    )
