"""
minime_cli/auth.py

Patch A6's one real open design question, resolved: option (a) from the
guide -- a CLI login flow that obtains a real Supabase session/JWT the
same way the browser does, cached locally after first login. Full
reasoning in docs/decisions/0002-cli-auth-strategy.md; short version:
frontend/app/context/AuthContext.jsx already signs in with
`supabase.auth.signInWithPassword({email, password})`, i.e. this
project already relies on Supabase's password grant, not
magic-link/OAuth-only -- so the exact same grant is available to a
terminal client with no browser involved, via a plain POST to
Supabase's own Auth REST API. backend/scripts/get_test_jwt.py's
sign_in() already proves this exact call shape works against this
project's Supabase instance (it mints test JWTs the same way); the one
difference here is this module uses the public anon key, never
SUPABASE_SERVICE_ROLE_KEY -- a CLI is not a trusted backend process.

Once minted, the access_token round-trips through api/deps.py's
require_auth() exactly like a browser-obtained one -- same signature,
same `sub` claim, same everything. The backend needs zero changes for
this to work.

Credentials are cached at ~/.minime/credentials.json (0600) so
`minime ask` doesn't need a password on every invocation. A near-expiry
access_token is silently refreshed via Supabase's refresh_token grant,
mirroring what supabase-js's client already does for the browser
session in the background.
"""
from __future__ import annotations

import json
import os
import time

import requests

from .config import CREDENTIALS_FILE, Config, ConfigError

# Refresh a bit before the token's real expiry, not exactly at it --
# leaves room for the request that's about to use this token to
# actually complete before the server-side check would reject it.
_REFRESH_SKEW_SECONDS = 60


class AuthError(RuntimeError):
    """Login failed, or no cached session exists. Caught at the CLI's
    top level and turned into a one-line "run `minime login`" message."""


def _read_credentials() -> dict | None:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_credentials(payload: dict) -> None:
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    os.chmod(CREDENTIALS_FILE, 0o600)  # this one IS sensitive -- a live session token


def _token_request(cfg: Config, *, grant_type: str, body: dict) -> dict:
    supabase_url, anon_key = cfg.require_supabase()
    resp = requests.post(
        f"{supabase_url}/auth/v1/token?grant_type={grant_type}",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    if resp.status_code >= 400:
        # Supabase's error body is small and safe to surface directly
        # (e.g. {"error_description": "Invalid login credentials"}) --
        # more useful to the person than a bare status code.
        detail = resp.json().get("error_description") or resp.json().get("msg") or resp.text
        raise AuthError(f"Sign-in failed: {detail}")
    return resp.json()


def login(cfg: Config, email: str, password: str) -> str:
    """Signs in and caches the resulting session. Returns the user_id
    (the `sub` claim) purely so the calling command can print a
    friendly confirmation -- nothing else needs it, require_auth()
    re-derives it server-side from the token on every request."""
    data = _token_request(cfg, grant_type="password", body={"email": email, "password": password})
    _write_credentials({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        # Supabase returns `expires_at` as a Unix timestamp already;
        # fall back to now + expires_in for older gotrue versions that
        # only send the relative form.
        "expires_at": data.get("expires_at") or (time.time() + data.get("expires_in", 3600)),
        "user_id": data.get("user", {}).get("id"),
        "email": data.get("user", {}).get("email"),
    })
    return data.get("user", {}).get("id", "")


def logout() -> None:
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()


def current_user_email() -> str | None:
    creds = _read_credentials()
    return creds.get("email") if creds else None


def get_access_token(cfg: Config) -> str:
    """The one function every other command calls before hitting the
    backend. Refreshes transparently; never prompts -- if there's no
    usable session at all, it fails fast with AuthError rather than
    silently falling back to an anonymous request that require_auth()
    would just 401 on two hops later."""
    creds = _read_credentials()
    if creds is None:
        raise AuthError("Not logged in. Run `minime login` first.")

    if time.time() < creds["expires_at"] - _REFRESH_SKEW_SECONDS:
        return creds["access_token"]

    try:
        data = _token_request(
            cfg, grant_type="refresh_token", body={"refresh_token": creds["refresh_token"]},
        )
    except AuthError:
        # A dead/rotated-out refresh token, not a transient error --
        # the session is genuinely over, not a network hiccup. Only
        # option is a fresh login, not something to silently paper over.
        raise AuthError("Session expired. Run `minime login` again.")

    _write_credentials({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data.get("expires_at") or (time.time() + data.get("expires_in", 3600)),
        "user_id": data.get("user", {}).get("id") or creds.get("user_id"),
        "email": data.get("user", {}).get("email") or creds.get("email"),
    })
    return data["access_token"]
