"""
api/stream_tickets.py

Short-lived, single-use tickets for the two browser transports that can't
send an Authorization header: the /ws/{session_id} WebSocket and the
/api/task/{session_id}/stream EventSource (SSE).

Why this exists
---------------
Neither `new WebSocket(url)` nor `new EventSource(url)` lets the page set
request headers, so both used to carry the user's full Supabase access
token as `?token=<jwt>`. A query string is the one part of a request that
reliably ends up in places a header doesn't: uvicorn/proxy/CDN access
logs, Sentry breadcrumbs, browser history. A leaked access token is a
bearer credential for the *whole* API for as long as it lives (about an
hour), not just for the stream it was minted for.

How it works
------------
1. The page calls `POST /api/stream-ticket` (ordinary Authorization
   header, see api/routes/stream_ticket.py) saying which transport and
   which session_id it wants to open.
2. We mint a random ticket, store {user_id, kind, session_id} under the
   ticket's SHA-256 for TICKET_TTL_SECONDS, and hand the raw ticket back.
3. The page opens the socket/stream with `?ticket=<ticket>`.
4. The stream route redeems it with an atomic GETDEL: it is valid for one
   redemption only, even if two requests race, and expires on its own if
   nobody redeems it.

What a leaked ticket is worth: nothing after first use or ~30 seconds,
and it only ever opens the one transport/session it was minted for.

Design choices worth knowing
----------------------------
* Atomic redeem. memory.bus's read()-then-delete() is two round trips, so
  two concurrent requests could both read the ticket before either
  deleted it. GETDEL is a single command. A ticket is consumed even when
  it's presented for the wrong kind/session, so a misused ticket is dead.
* Only the hash is stored. The raw ticket exists in the browser and in
  flight; the Redis key can't be turned back into a usable ticket.
* Talks to Redis directly instead of through memory.bus.read()/write().
  Those prefix every key with the active app_slug (see
  memory/bus.py::_namespaced), and a ticket is minted in one request and
  redeemed in another -- the same cross-request namespace skew that
  _namespaced's own comments describe for workspace_facts. A ticket is a
  property of the user's session, not of whatever app_slug is current.
  (eo/run_guard.py does the same, for the same reason.)
* Fails closed. If Redis is unreachable, minting raises
  TicketStoreUnavailable (the route turns that into a 503) and redeeming
  returns None (the transport rejects it). Unlike run_guard's
  bookkeeping, an auth check must never fail open.
"""
import hashlib
import json
import logging
import re
import secrets

logger = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 30

KIND_WS = "ws"
KIND_SSE = "sse"
VALID_KINDS = frozenset({KIND_WS, KIND_SSE})

_KEY_PREFIX = "stream_ticket:"

# secrets.token_urlsafe(32) is always 43 chars of [A-Za-z0-9_-]. Accept a
# range so a future change of length doesn't need a coordinated deploy,
# but reject anything else (notably a JWT, which contains dots) before it
# costs a Redis round trip.
_TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


class TicketStoreUnavailable(Exception):
    """Redis could not be reached while minting a ticket."""


def _key(ticket: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _redis():
    # Deferred, like eo/run_guard.py: keeps this module importable in unit
    # tests without Upstash env, and lets tests swap memory.bus.redis.
    from memory.bus import redis
    return redis


def issue_ticket(user_id: str, kind: str, session_id: str) -> str:
    """Mint a ticket for `user_id` to open one `kind` transport on
    `session_id`. Raises ValueError for a bad kind/empty args and
    TicketStoreUnavailable if the store can't be written."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown ticket kind: {kind!r}")
    if not user_id or not session_id:
        raise ValueError("user_id and session_id are required")

    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"u": user_id, "k": kind, "s": session_id})
    try:
        _redis().set(_key(ticket), payload, ex=TICKET_TTL_SECONDS)
    except Exception as exc:
        logger.error("[stream_tickets] could not store ticket: %s", exc)
        raise TicketStoreUnavailable(str(exc)) from exc
    return ticket


def redeem_ticket(ticket: str, kind: str, session_id: str) -> str | None:
    """Atomically consume `ticket`. Returns the user_id it was minted for
    if -- and only if -- it exists, hasn't expired, hasn't been used, and
    was minted for this exact `kind` and `session_id`. Returns None in
    every other case, including any store error (fail closed).

    The ticket is burned by the attempt itself: a wrong-kind or
    wrong-session presentation still consumes it."""
    if not isinstance(ticket, str) or not _TICKET_RE.match(ticket):
        return None

    try:
        raw = _redis().getdel(_key(ticket))
    except Exception as exc:
        logger.error("[stream_tickets] could not redeem ticket: %s", exc)
        return None
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("k") != kind or data.get("s") != session_id:
        return None
    user_id = data.get("u")
    return user_id if isinstance(user_id, str) and user_id else None
