"""
eo/integrations.py — Part 8.5: shared per-user OAuth credential storage.

One table, one module, for every third-party connector (Calendar, Gmail,
Slack, Jira/Asana/Linear, ...) — same reasoning as eo/audit_log.py being
one table for every mutating action rather than one table per domain.
Each connector agent (agents/calendar_agent.py being the first) calls
get_credentials()/refresh_if_needed() to get a live access token; it never
touches the user_integrations table directly.

Deliberately thin, same discipline as every other module in this
migration: plain functions, owner_id-scoped queries, no ORM. Callers
(api/server.py's OAuth routes) decide who's allowed to call these; this
module just stores and retrieves.

--- Token encryption -------------------------------------------------------
access_token/refresh_token are stored Fernet-encrypted, key from
INTEGRATIONS_ENCRYPTION_KEY (a standalone key — deliberately NOT reusing
SUPABASE_JWT_SECRET or SUPABASE_SERVICE_ROLE_KEY, so rotating one never
silently affects the other). Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and set it in the server's env. If this key is ever rotated, every stored
token becomes unreadable and every user must reconnect — there is no
re-encryption migration here; that's out of scope for this pass and
should be flagged if it ever becomes a real operational need.
-----------------------------------------------------------------------------
"""
import os
import time
from datetime import datetime, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken

from eo import db
from eo.audit_log import write_audit

_ENCRYPTION_KEY = os.getenv("INTEGRATIONS_ENCRYPTION_KEY")
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not _ENCRYPTION_KEY:
            raise RuntimeError(
                "INTEGRATIONS_ENCRYPTION_KEY is not set — required before any "
                "integration credential can be stored or read. See eo/integrations.py "
                "module docstring for how to generate one."
            )
        _fernet = Fernet(_ENCRYPTION_KEY.encode())
    return _fernet


def _encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # Key rotated, or row corrupted — treat as "no usable credential"
        # rather than raising into a connector's request path.
        return None


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None


# --- storage ----------------------------------------------------------------

def save_credentials(user_id: str, provider: str, access_token: str,
                      refresh_token: str | None = None, expires_in: int | None = None,
                      scope: str | None = None, account_label: str | None = None) -> None:
    """Upserts one (user_id, provider) row. Called at the end of the OAuth
    callback (initial connect) and by refresh_if_needed() below (token
    refresh) — same function for both, since a refresh IS just a new
    access_token (and sometimes a new refresh_token) for the same row."""
    expires_at = None
    if expires_in is not None:
        expires_at = _now().timestamp() + expires_in
        expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc)

    with db.cursor() as cur:
        cur.execute(
            """
            insert into user_integrations
                (user_id, provider, account_label, access_token, refresh_token, scope, expires_at, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (user_id, provider) do update set
                access_token = excluded.access_token,
                refresh_token = coalesce(excluded.refresh_token, user_integrations.refresh_token),
                scope = coalesce(excluded.scope, user_integrations.scope),
                expires_at = excluded.expires_at,
                account_label = coalesce(excluded.account_label, user_integrations.account_label),
                updated_at = excluded.updated_at
            """,
            (user_id, provider, account_label, _encrypt(access_token), _encrypt(refresh_token),
             scope, expires_at, _now()),
        )
    write_audit(user_id, "integration.connect", "integration", provider, {"account_label": account_label})


def get_credentials(user_id: str, provider: str) -> dict | None:
    """Raw stored row (decrypted), or None if not connected. Most callers
    want refresh_if_needed() instead — this is exposed for the
    GET /api/integrations listing endpoint, which needs account_label and
    connected-since but never needs the actual decrypted token."""
    with db.cursor() as cur:
        cur.execute(
            "select provider, account_label, access_token, refresh_token, scope, expires_at, "
            "created_at, updated_at from user_integrations where user_id = %s and provider = %s",
            (user_id, provider),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "provider": row["provider"],
        "account_label": row["account_label"],
        "access_token": _decrypt(row["access_token"]),
        "refresh_token": _decrypt(row["refresh_token"]),
        "scope": row["scope"],
        "expires_at": _iso(row["expires_at"]),
        "connected_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def list_connected(user_id: str) -> list[dict]:
    """For the frontend's integrations panel — never includes tokens."""
    with db.cursor() as cur:
        cur.execute(
            "select provider, account_label, expires_at, created_at from user_integrations "
            "where user_id = %s order by created_at",
            (user_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "provider": r["provider"],
            "account_label": r["account_label"],
            "expires_at": _iso(r["expires_at"]),
            "connected_at": _iso(r["created_at"]),
        }
        for r in rows
    ]


def disconnect(user_id: str, provider: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "delete from user_integrations where user_id = %s and provider = %s returning id",
            (user_id, provider),
        )
        found = cur.fetchone()
    if found:
        write_audit(user_id, "integration.disconnect", "integration", provider, {})


# --- refresh ------------------------------------------------------------

# Per-provider token-refresh endpoint + client credentials. Every provider
# added later just needs one more entry here — the refresh mechanics below
# are otherwise identical across standard OAuth2 providers.
_REFRESH_CONFIG = {
    "google_calendar": {
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
}


def refresh_if_needed(user_id: str, provider: str, skew_seconds: int = 60) -> str | None:
    """Returns a live, usable access_token, refreshing it first if it's
    expired (or about to, within skew_seconds) and a refresh_token exists.
    Returns None if there's no stored credential at all, or if a needed
    refresh fails (caller should treat that as 'not connected' and prompt
    reconnect, not retry blindly — a failed refresh usually means the
    user revoked access on the provider's side)."""
    creds = get_credentials(user_id, provider)
    if not creds or not creds["access_token"]:
        return None

    expires_at = creds.get("expires_at")
    if not expires_at:
        return creds["access_token"]  # provider never told us an expiry — assume still valid

    expiry_dt = datetime.fromisoformat(expires_at)
    if expiry_dt.timestamp() - skew_seconds > time.time():
        return creds["access_token"]

    if not creds.get("refresh_token"):
        return None  # expired, nothing to refresh with — caller must prompt reconnect

    config = _REFRESH_CONFIG.get(provider)
    if not config:
        return None

    client_id = os.getenv(config["client_id_env"])
    client_secret = os.getenv(config["client_secret_env"])
    if not client_id or not client_secret:
        return None

    resp = requests.post(config["token_url"], data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=15)
    if resp.status_code != 200:
        return None

    payload = resp.json()
    new_access_token = payload.get("access_token")
    if not new_access_token:
        return None

    save_credentials(
        user_id, provider, new_access_token,
        refresh_token=payload.get("refresh_token"),  # providers don't always rotate it
        expires_in=payload.get("expires_in"),
        scope=payload.get("scope") or creds.get("scope"),
        account_label=creds.get("account_label"),
    )
    return new_access_token