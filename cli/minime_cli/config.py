"""
minime_cli/config.py

Where the CLI gets its coordinates from. Three independent values, each
resolved env-var-first, then a local config file, then a hardcoded
default — same override order as everything else in this repo that
reads from `.env` (backend/eo/db.py, daemon/config.py, etc.):

  MINIME_API_URL           -- the backend's own base URL. Mirrors the
                               frontend's NEXT_PUBLIC_API_URL
                               (frontend/app/context/SessionContext.jsx)
                               and defaults to the same
                               http://localhost:8000 docker-compose.yml
                               exposes it on.

  MINIME_SUPABASE_URL       -- the Supabase project the backend's own
  MINIME_SUPABASE_ANON_KEY     SUPABASE_URL / (frontend)
                               NEXT_PUBLIC_SUPABASE_ANON_KEY point at.
                               The CLI needs these to sign in directly
                               against Supabase's own Auth REST API
                               (see auth.py) — it does NOT send a
                               password to the backend, because the
                               backend has no password-login route of
                               its own (see docs/decisions/0002).
                               The anon key is a public, RLS-scoped
                               key by design (the same one Supabase's
                               own docs say is safe to embed in a
                               browser bundle) — safe to put in a CLI
                               config file too, unlike
                               SUPABASE_SERVICE_ROLE_KEY.

Precedence per value: real env var > `~/.minime/config.json` > built-in
default (API URL only — the two Supabase values have no safe default
and are required before `minime login` can do anything).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MINIME_CONFIG_DIR", Path.home() / ".minime"))
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

_DEFAULT_API_URL = "http://localhost:8000"


class ConfigError(RuntimeError):
    """Raised when a required config value is missing. Caught at the
    CLI's top level (main.py) and turned into a short, actionable
    stderr message instead of a traceback."""


@dataclass(frozen=True)
class Config:
    api_url: str
    supabase_url: str | None
    supabase_anon_key: str | None

    def require_supabase(self) -> tuple[str, str]:
        """Fail loudly, with the actual fix, rather than a bare
        `NoneType` error three calls deep in auth.py."""
        missing = [
            name for name, val in
            [("MINIME_SUPABASE_URL", self.supabase_url),
             ("MINIME_SUPABASE_ANON_KEY", self.supabase_anon_key)]
            if not val
        ]
        if missing:
            raise ConfigError(
                f"Missing: {', '.join(missing)}. Set them as environment "
                f"variables, or run `minime configure` to save them to "
                f"{CONFIG_FILE}. Both are the same public project URL / "
                f"anon key the web frontend uses (frontend/.env.local's "
                f"NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY) "
                f"-- never the service role key."
            )
        return self.supabase_url, self.supabase_anon_key  # type: ignore[return-value]


def _read_config_file() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt/unreadable config file shouldn't crash every CLI
        # invocation -- fall back to env-vars-only, same posture as a
        # missing file.
        return {}


def load_config() -> Config:
    file_cfg = _read_config_file()
    return Config(
        api_url=os.environ.get("MINIME_API_URL") or file_cfg.get("api_url") or _DEFAULT_API_URL,
        supabase_url=os.environ.get("MINIME_SUPABASE_URL") or file_cfg.get("supabase_url"),
        supabase_anon_key=os.environ.get("MINIME_SUPABASE_ANON_KEY") or file_cfg.get("supabase_anon_key"),
    )


def save_config(*, api_url: str | None = None, supabase_url: str | None = None,
                 supabase_anon_key: str | None = None) -> Config:
    """Merges into the existing file rather than overwriting it, so
    `minime configure` can be re-run to fix one value without
    clobbering the others."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = _read_config_file()
    if api_url:
        current["api_url"] = api_url
    if supabase_url:
        current["supabase_url"] = supabase_url
    if supabase_anon_key:
        current["supabase_anon_key"] = supabase_anon_key
    CONFIG_FILE.write_text(json.dumps(current, indent=2) + "\n")
    # 0600: this file holds a project's anon key (public-ish, but still
    # no reason to leave it group/world-readable) and sits right next
    # to credentials.json, which is NOT public -- match its permissions
    # so a casual `ls -la ~/.minime` doesn't visually suggest one is
    # sensitive and the other isn't.
    os.chmod(CONFIG_FILE, 0o600)
    return load_config()
