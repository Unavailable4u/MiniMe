"""minime_cli/commands/auth_cmds.py -- login, logout, whoami, configure."""
from __future__ import annotations

import click

from .. import auth
from ..config import ConfigError, load_config, save_config


@click.command()
@click.option("--api-url", default=None, help="Backend base URL (e.g. http://localhost:8000).")
@click.option("--supabase-url", default=None, help="Supabase project URL.")
@click.option("--supabase-anon-key", default=None, help="Supabase project anon (public) key -- never the service role key.")
def configure(api_url, supabase_url, supabase_anon_key):
    """Save connection settings to ~/.minime/config.json.

    Every value is also settable via env var (MINIME_API_URL,
    MINIME_SUPABASE_URL, MINIME_SUPABASE_ANON_KEY) instead, which takes
    precedence over anything saved here -- this command exists purely
    so you don't have to export them in every shell session.
    """
    if not any([api_url, supabase_url, supabase_anon_key]):
        click.echo("Nothing to save -- pass at least one of --api-url / --supabase-url / --supabase-anon-key.")
        return
    cfg = save_config(api_url=api_url, supabase_url=supabase_url, supabase_anon_key=supabase_anon_key)
    click.echo(f"Saved. api_url={cfg.api_url}"
               + (f" supabase_url={cfg.supabase_url}" if cfg.supabase_url else "")
               + (" supabase_anon_key=<set>" if cfg.supabase_anon_key else ""))


@click.command()
@click.option("--email", prompt=True)
@click.option("--password", prompt=True, hide_input=True)
def login(email, password):
    """Sign in with your MiniMe account (same email/password as the web app)."""
    cfg = load_config()
    try:
        auth.login(cfg, email, password)
    except ConfigError as e:
        raise click.ClickException(str(e))
    except auth.AuthError as e:
        raise click.ClickException(str(e))
    click.echo(f"Logged in as {email}.")


@click.command()
def logout():
    """Forget the cached session."""
    auth.logout()
    click.echo("Logged out.")


@click.command()
def whoami():
    """Print the currently logged-in account, if any."""
    email = auth.current_user_email()
    if email:
        click.echo(email)
    else:
        click.echo("Not logged in. Run `minime login`.")
