"""
daemon/config.py — F2 Part 1: loads the daemon's two-value config
(pairing token, one allowed root folder) from daemon/.env, following
this repo's existing python-dotenv + os.environ convention (see
backend/eo/db.py, backend/eo/quota_sentinel.py).

Deliberately minimal: no backend URL, no session/workspace id, nothing
about the websocket connection yet -- that's Part 2. This module's only
job in Part 1 is proving config loads correctly and the root folder is
safe (see path_guard.assert_safe_root) before the daemon does anything
else.

Place this file at: daemon/config.py
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from daemon.path_guard import PathGuardError, assert_safe_root

logger = logging.getLogger("minime_daemon")

# Mirrors backend/.env.example's own path convention: config lives next
# to the module that reads it, not in the process's cwd, so `python
# daemon/minime_daemon.py` behaves the same regardless of where it's
# launched from.
_ENV_PATH = Path(__file__).resolve().parent / ".env"

_MIN_TOKEN_LENGTH = 16


class ConfigError(Exception):
    """Raised for any daemon.env problem: missing file, missing/weak
    token, or a root folder that fails path_guard's safety checks."""


@dataclass(frozen=True)
class DaemonConfig:
    pairing_token: str
    allowed_root: Path


def load_config(env_path: Path | None = None) -> DaemonConfig:
    """Load and validate daemon config. Raises ConfigError on anything
    that would make it unsafe or meaningless to start the daemon."""
    target_env = env_path or _ENV_PATH

    if not target_env.exists():
        raise ConfigError(
            f"no config found at {target_env} -- copy daemon/.env.example "
            "to daemon/.env and fill in MINIME_PAIRING_TOKEN and "
            "MINIME_ALLOWED_ROOT before running the daemon"
        )

    # override=True: rerunning load_config() in tests with a different
    # env_path shouldn't leak a previous test's values via os.environ.
    load_dotenv(dotenv_path=target_env, override=True)

    token = os.environ.get("MINIME_PAIRING_TOKEN", "").strip()
    root = os.environ.get("MINIME_ALLOWED_ROOT", "").strip()

    if not token:
        raise ConfigError("MINIME_PAIRING_TOKEN is not set in daemon/.env")
    if len(token) < _MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"MINIME_PAIRING_TOKEN is too short ({len(token)} chars, "
            f"need at least {_MIN_TOKEN_LENGTH}) -- generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`"
        )
    if not root:
        raise ConfigError("MINIME_ALLOWED_ROOT is not set in daemon/.env")

    try:
        resolved_root = assert_safe_root(root)
    except PathGuardError as exc:
        raise ConfigError(str(exc)) from exc

    logger.info("config loaded: allowed root = %s", resolved_root)
    return DaemonConfig(pairing_token=token, allowed_root=resolved_root)


def generate_pairing_token() -> str:
    """Small helper for the setup step (`python -m daemon.config
    --generate-token`) so users don't need a separate one-liner."""
    return secrets.token_urlsafe(32)


if __name__ == "__main__":
    import sys

    if "--generate-token" in sys.argv:
        print(generate_pairing_token())
    else:
        print(f"usage: python -m daemon.config --generate-token")
