"""
minime_cli/commands/skills_cmds.py -- `minime skills list`, `minime
skills show <id>`, Patch A8.

Pure plumbing, per the implementation guide's own framing of A8: both
commands are thin wrappers over Patch A5's read-only GET /api/skills
and GET /api/skills/{skill_id} routes (api/routes/tasks.py), themselves
a read-only mirror of eo/skill_library.py's single global store. No
write/edit command here by design, mirroring A5's own "explicitly not
in scope" note for a write endpoint -- skills are written only by the
hand-written seed and the self-improvement loop, never by a person
directly, from the CLI or otherwise.
"""
from __future__ import annotations

import click

from .. import auth
from ..api_client import ApiClient, ApiError
from ..config import ConfigError, load_config


def _client() -> ApiClient:
    return ApiClient(load_config())


@click.group()
def skills():
    """Browse the skill library (read-only)."""


@skills.command(name="list")
def list_skills_cmd():
    """List every skill in the library (id and title)."""
    try:
        entries = _client().list_skills()
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))
    if not entries:
        click.echo("No skills in the library yet.")
        return
    for entry in entries:
        click.echo(f"{entry.get('skill_id')}\t{entry.get('title')}")


@skills.command(name="show")
@click.argument("skill_id")
def show_skill_cmd(skill_id):
    """Show the full record for one skill (doc text, source, match count)."""
    try:
        entry = _client().get_skill(skill_id)
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))
    click.echo(f"id:            {entry.get('skill_id')}")
    click.echo(f"title:         {entry.get('title')}")
    click.echo(f"source:        {entry.get('source')}")
    click.echo(f"updated_at:    {entry.get('updated_at')}")
    click.echo(f"times_matched: {entry.get('times_matched')}")
    click.echo()
    click.echo(entry.get("doc") or "(no doc text)")
