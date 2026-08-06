"""
api/deps.py

Shared auth/access dependencies used across every route module — both
the ones still living directly in api/server.py and the ones already
split out into api/routes/*.

Pulled out during B6 (splitting server.py) for one reason: route modules
under api/routes/ need require_auth() and _resolve_chat_or_404(), but
they can't import those from api.server, because api/server.py itself
imports the route modules (to call app.include_router(...)) — that would
be a circular import. Putting the shared pieces here, with nothing
importing back from api.server, breaks the cycle.

Behavior is byte-for-byte unchanged from what used to live inline in
api/server.py — this is a pure move, not a rewrite.
"""
import os

import jwt
import requests
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from eo import chat_store

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")  # only used as a fallback for
                                                          # projects still on the legacy
                                                          # shared HS256 secret — most
                                                          # current Supabase projects sign
                                                          # with an asymmetric key instead
                                                          # (see require_auth() below).
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # admin key, used ONLY
                                                          # to resolve an invited
                                                          # collaborator's email to their
                                                          # user_id. Never sent to a
                                                          # client, never used for auth.

# Lazily-built JWKS client — fetches and caches Supabase's public signing
# keys from its well-known endpoint. This is what verifies the asymmetric
# (ES256/RS256) tokens that current Supabase projects issue by default.
# No secret involved on this path: these are public keys, safe to fetch
# over the network on every cold start.
_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not SUPABASE_URL:
            raise HTTPException(
                status_code=500,
                detail="Server misconfigured: SUPABASE_URL is not set.",
            )
        _jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _jwk_client


def _verify_supabase_jwt(token: str) -> str:
    """The actual decode-and-verify logic behind require_auth() below.
    Kept as a bare token->user_id function so the WebSocket handshake in
    api/server.py can share it: a browser's WebSocket API has no way to
    set an Authorization header on the handshake request, so that route
    takes the token as a query param instead of going through
    require_auth()'s Request-shaped dependency. Raises HTTPException on
    any failure — the WebSocket route catches that itself and translates
    it into a close code, since a socket has no response body to attach
    an HTTP error detail to."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    alg = header.get("alg", "")

    try:
        if alg == "HS256":
            # Legacy shared-secret projects only.
            if not SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status_code=500,
                    detail="Server misconfigured: SUPABASE_JWT_SECRET is not set.",
                )
            payload = jwt.decode(
                token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated",
            )
        else:
            # Current Supabase default: asymmetric signing (ES256/RS256).
            # get_signing_key_from_jwt looks up the right public key by the
            # token's own `kid`, so this works whether Supabase issued
            # ES256, RS256, or rotates keys later — nothing here is
            # hardcoded to one algorithm except what the token itself claims.
            signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token, signing_key.key, algorithms=[alg], audience="authenticated",
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")

    return user_id


def require_auth(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header[len("Bearer "):].strip()

    user_id = _verify_supabase_jwt(token)
    request.state.user_id = user_id  # kept for any code that reads it off the request directly
    return user_id


def _lookup_user_id_by_email(email: str) -> str | None:
    """Admin-API lookup, used only by the workspace-invite endpoint to
    turn 'alice@example.com' into a user_id. Paginates and matches
    exactly rather than trusting the API's own email filter — it did
    not reliably filter server-side during testing (see scripts/
    get_test_jwt.py's find_user_by_email, which hit the same issue and
    was fixed the same way)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: SUPABASE_SERVICE_ROLE_KEY is not set.",
        )
    page = 1
    per_page = 200
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            params={"page": page, "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("users", [])
        if not users:
            return None
        for u in users:
            if (u.get("email") or "").lower() == email.lower():
                return u["id"]
        if len(users) < per_page:
            return None
        page += 1


def _lookup_users_by_ids(user_ids: set[str]) -> dict:
    """Admin-API lookup, the reverse of _lookup_user_id_by_email — turns a
    set of user_ids into {id: {email, name, avatar_url}} so member rosters
    can show a real identity instead of a raw UUID. Same single-pass,
    early-exit-once-all-found pagination as the email lookup above; a
    workspace roster is small (partners/moderators/etc., not a whole
    user base), so this stays cheap even without caching.

    'name' falls back through user_metadata's common shapes (Supabase
    email/password signup doesn't set any of these — only OAuth
    providers or an app-side profile step would — so the final fallback
    is the local part of the email, then the raw id if even email is
    somehow missing).
    """
    if not user_ids:
        return {}
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: SUPABASE_SERVICE_ROLE_KEY is not set.",
        )
    remaining = set(user_ids)
    found = {}
    page = 1
    per_page = 200
    while remaining:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            params={"page": page, "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
        users = resp.json().get("users", [])
        if not users:
            break
        for u in users:
            if u["id"] in remaining:
                meta = u.get("user_metadata") or {}
                email = u.get("email")
                found[u["id"]] = {
                    "email": email,
                    "name": meta.get("full_name") or meta.get("name")
                            or (email.split("@")[0] if email else u["id"]),
                    "avatar_url": meta.get("avatar_url") or meta.get("picture"),
                }
                remaining.discard(u["id"])
        if len(users) < per_page:
            break
        page += 1
    return found


def _resolve_chat_or_404(chat_id: str, user_id: str, require_edit: bool = False) -> str:
    """Confirms user_id has access to chat_id — as its owner, or as a
    workspace collaborator — and returns the chat's REAL owner_id, which
    every chat_store function must be called with (never the requester's
    own id, unless they happen to be the same person). Raises 404 for no
    access at all (never distinguishes 'doesn't exist' from 'exists but
    isn't shared with you'), and 403 if the requester has viewer-only
    access but the route needs edit rights."""
    resolved = chat_store.resolve_chat_access(chat_id, user_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Unknown chat_id")
    real_owner_id, role = resolved
    if require_edit and role == "viewer":
        raise HTTPException(status_code=403, detail="Viewer access does not permit this action")
    return real_owner_id
