"""
eo/devices.py -- S03: M2C device pairing and device credentials.

`m2c login` links a terminal to a MiniMe account without ever typing a
password into it. This module is the whole server-side state machine
behind that, in the shape of the OAuth device-code flow (RFC 8628):

    terminal                                   web app (signed in)
    --------                                   -------------------
    start_pairing()  -> device_code (secret),
                        user_code  (BCDF-GHJK),
                        verification_url
    shows the user code
                                               person types the code
                                               approve_pairing(user, code)
    poll_pairing(device_code) ... pending ...
    poll_pairing(device_code) -> approved +
                        device_credential (ONCE) + device_id
    stores the credential in the OS keychain

    later, every ~15 minutes, and on every reconnect:
    issue_device_token(credential) -> short-lived JWT
    (sub = user id, device_id), which S04's socket handshake presents
    and checks with verify_device_token().

Tables are in migration 0013 (devices, device_pairings), which keeps what
must never change (a device's owner and credential hash, a pairing's codes
and expiry, a revocation) immutable with triggers rather than column grants.
The routes that expose this are api/routes/devices.py. Everything HTTP-shaped (status
codes, headers) lives there; everything that decides lives here.

SECRETS AND WHAT IS STORED
--------------------------
There are three secrets in flight: the device_code (the terminal's poll
handle), the user_code (typed by a person; short, so deliberately weak on
its own), and the device credential (256 random bits, long-lived, the
thing that actually authenticates the device). None is ever stored: the
database holds SHA-256 digests only. The credential is generated at the
instant the terminal collects it -- inside the poll that wins the atomic
'approved' -> 'consumed' update -- and its digest is written in the same
transaction, so no plaintext credential ever waits in a table, and a
replayed poll finds a consumed row and gets nothing. The price of that
design is honest and accepted: if the response carrying the credential is
lost in transit, the terminal cannot get it again and has to run
`m2c login` once more (the orphaned device shows in the device list and can
be revoked).

None of the three secrets, nor the JWTs, may appear in a log line, an
exception message or an error response. This module logs user ids and
device ids only (build plan section 11.4), and logs the TYPE of an
infrastructure error, never its text. api/routes/devices.py keeps the
secrets out of request-model reprs (SecretStr) for the same reason.

FAIL CLOSED
-----------
Metering helpers in this codebase fail open; this is authentication, so
nothing here does. A missing or weak signing secret, an unreachable
rate-limit store, an unreadable database: every one ends in a refusal
(503 / 401), never in a token or an approval being granted anyway.

ENVIRONMENT VARIABLES (new in S03; .env.example is owned by B07 in this wave
and is not edited here -- add these to it when B07 lands)
--------------------------------------------------------------------------
  DEVICE_JWT_SECRET        REQUIRED to pair or to issue tokens. At least 32
                           characters of random data, e.g.
                           `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
                           Signs the short-lived device JWT (HS256). It must
                           NOT be the Supabase JWT secret, and the module
                           refuses to use it if it equals SUPABASE_JWT_SECRET.
                           If it is missing, too short, or equal to the
                           Supabase secret, pair/start, pair/poll and
                           /v1/code/devices/token all answer 503 and issue
                           nothing.
  DEVICE_VERIFICATION_URL  Optional. The page where a person enters the
                           code, returned to the terminal as
                           `verification_url`. Defaults to the first
                           non-empty entry of ALLOWED_ORIGINS (the web app's
                           origin; http://localhost:3000 if that is unset),
                           since the web app is a single page with no
                           per-feature routes; set it once X01's page has a
                           stable URL.

DEVICE JWT: HOW IT CANNOT BE CONFUSED WITH A USER TOKEN
-------------------------------------------------------
A device JWT is signed with DEVICE_JWT_SECRET (never the Supabase secret),
carries audience "m2c-device" (Supabase user tokens carry
"authenticated") and an explicit token_type claim of "device", and is
verified with the algorithm pinned to HS256. So api/deps.py's require_auth
rejects it (wrong key, wrong audience) and verify_device_token() below
rejects a Supabase token (wrong key, wrong audience, no token_type) -- two
independent barriers each way. Neither deps.py nor llm_client.py is touched.

RATE LIMITS (module constants; counters live in Redis, fixed per key)
---------------------------------------------------------------------
  pair/start          10 per client address per 10 minutes, and 100 per
                      10 minutes across everyone, so the table cannot be
                      flooded. The address is the transport peer
                      (request.client): behind a proxy, set uvicorn's
                      FORWARDED_ALLOW_IPS to the proxy's address so it is
                      the real client and not the proxy -- this module
                      never reads X-Forwarded-For itself, since a header
                      the client can set is not something to rate-limit on.
  user-code attempts  5 WRONG codes per signed-in account, then the account
                      is locked out of approve/deny for 15 minutes (a right
                      code is refused too while locked, so the lock cannot
                      be probed). Codes that are not even the right shape
                      do not count: they cannot be guesses. Counting is
                      check-then-record, so a burst of parallel requests
                      can overshoot the limit by the server's concurrency;
                      with 8 characters from a 20-letter alphabet, a 10
                      minute lifetime and one lock per account that is not
                      a meaningful gain for a guesser.
  Redis trouble       fails closed (503), unlike utils/rate_ledger.py,
                      which fails open because it only paces LLM calls.
Redis is used directly (like api/stream_tickets.py), not through
memory.bus's namespaced read/write, because a counter must be shared by
every request regardless of the app slug in scope.

STORAGE SEAM
------------
All SQL is in PgStore, a stateless class. Its statements are plain literals
(or module-level string constants) passed straight to execute(), so
tests/static/test_sql_grants.py (B01) can read every one of them and check
it against what migration 0013 grants minime_app -- this module is listed in
that test's SQL_MODULES. The functions above it take
their store from the module-level _STORE, which unit tests replace with an
in-memory fake; the real-database test
(tests/integration/test_devices_pg.py) drives the same flow through PgStore
against Postgres, so the SQL, the grants and the RLS policies are all
exercised for real.

FOR S04 (device socket)
-----------------------
verify_device_token(jwt) -> DeviceIdentity(user_id, device_id, expires_at)
does the whole handshake check: signature, audience, token type, expiry,
and a live check that the device still exists, belongs to that user and has
not been revoked. Call it on connect and on each heartbeat/refresh.
DEVICE_JWT_TTL_SECONDS is the value for `welcome.jwt_ttl`.
"""
import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt

from eo import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------

PAIRING_TTL_SECONDS = 600          # a user code is good for about 10 minutes
POLL_INTERVAL_SECONDS = 5          # how often the terminal should poll
DEVICE_JWT_TTL_SECONDS = 900       # a device JWT lives about 15 minutes
DEVICE_JWT_AUDIENCE = "m2c-device"
DEVICE_JWT_TOKEN_TYPE = "device"
DEVICE_JWT_ALGORITHM = "HS256"
JWT_LEEWAY_SECONDS = 10
MIN_SECRET_LENGTH = 32

# RFC 8628 section 6.1's suggested set: consonants only (so no accidental
# words) and nothing that reads as another character -- no 0/O, 1/I, no
# vowels. 8 characters = about 34 bits.
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
USER_CODE_LENGTH = 8

MAX_WRONG_ATTEMPTS = 5
LOCK_SECONDS = 900
START_LIMIT_PER_IP = 10
START_LIMIT_GLOBAL = 100
START_WINDOW_SECONDS = 600

_CODE_RETRIES = 5                  # on a (vanishingly rare) user-code collision

_CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")   # token_urlsafe(32) is 43 chars
_USER_CODE_RE = re.compile(rf"^[{USER_CODE_ALPHABET}]{{{USER_CODE_LENGTH}}}$")

_WRONG_KEY = "device_pair:wrong:"
_START_IP_KEY = "device_pair:start:ip:"
_START_ALL_KEY = "device_pair:start:all"


# ---------------------------------------------------------------------
# Errors -- routes translate these; none carries a secret in its message
# ---------------------------------------------------------------------

class DeviceError(Exception):
    """Base class."""


class NotConfigured(DeviceError):
    """DEVICE_JWT_SECRET is missing, too short, or equals the Supabase secret."""


class RateLimited(DeviceError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("rate limited")


class StoreUnavailable(DeviceError):
    """The rate-limit store (Redis) could not be used. Fail closed."""


class InvalidCode(DeviceError):
    """approve/deny: the user code is malformed, unknown, expired or already used."""


class InvalidDeviceCode(DeviceError):
    """poll: the device code is malformed, unknown, or was already collected."""


class InvalidCredential(DeviceError):
    """token: the credential is malformed, unknown or revoked."""


class DeviceNotFound(DeviceError):
    """revoke: no such device for this user."""


class DeviceAuthError(DeviceError):
    """verify_device_token: the token is not a valid token for a live device."""


@dataclass(frozen=True)
class DeviceIdentity:
    user_id: str
    device_id: str
    expires_at: int


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value) -> "str | None":
    return value.isoformat() if value is not None else None


def _is_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _device_jwt_secret() -> str:
    """The signing secret, or NotConfigured. Read on every call (not at
    import) so a deploy that sets it needs no code change, and so tests can
    set it. Refuses a short secret and refuses the Supabase secret: a
    device token must never be signed with the key that signs user tokens."""
    secret = os.getenv("DEVICE_JWT_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise NotConfigured("DEVICE_JWT_SECRET is not set or is too short")
    supabase_secret = os.getenv("SUPABASE_JWT_SECRET", "")
    if supabase_secret and secrets.compare_digest(
            secret.encode("utf-8"), supabase_secret.encode("utf-8")):
        raise NotConfigured("DEVICE_JWT_SECRET must differ from the Supabase JWT secret")
    return secret


def verification_url() -> str:
    """Where a person enters the code: DEVICE_VERIFICATION_URL if set, else the
    web app's origin (the first non-empty ALLOWED_ORIGINS entry that looks like
    a URL; http://localhost:3000, B07's first dev default, if there is none)."""
    explicit = os.getenv("DEVICE_VERIFICATION_URL", "").strip()
    if explicit:
        return explicit
    for item in os.getenv("ALLOWED_ORIGINS", "").split(","):
        item = item.strip()
        if item.startswith(("http://", "https://")):
            return item.rstrip("/")
    return "http://localhost:3000"


def new_user_code() -> str:
    """8 characters from USER_CODE_ALPHABET, from the OS CSPRNG."""
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


def format_user_code(code: str) -> str:
    """BCDFGHJK -> BCDF-GHJK, the form shown to people."""
    return f"{code[:4]}-{code[4:]}"


def normalize_user_code(text: "str | None") -> "str | None":
    """What a person typed -> the canonical 8 characters, or None if it
    cannot be a user code. Case, spaces and hyphens do not matter."""
    if not isinstance(text, str) or not text.isascii() or len(text) > 64:
        return None
    cleaned = re.sub(r"[\s-]", "", text).upper()
    return cleaned if _USER_CODE_RE.match(cleaned) else None


# ---------------------------------------------------------------------
# Rate limiting (Redis, fail closed)
# ---------------------------------------------------------------------

def _redis():
    # Deferred, like api/stream_tickets.py: importable without Upstash env,
    # and tests swap memory.bus.redis.
    from memory.bus import redis
    return redis


def _store_failure(exc: Exception) -> StoreUnavailable:
    # The TYPE only: an infrastructure error's text can name keys or hosts.
    logger.error("[devices] rate-limit store error (%s)", type(exc).__name__)
    return StoreUnavailable("rate-limit store unavailable")


def _bump(key: str, limit: int, window: int) -> bool:
    """INCR `key`; True while the count is within `limit`. The TTL is
    (re)applied only while within the limit: a refused attempt does not
    extend the penalty, and a key whose first EXPIRE was lost still gets one
    on its next allowed hit, so a counter can never become permanent."""
    redis = _redis()
    count = int(redis.incr(key))
    if count <= limit:
        redis.expire(key, window)
        return True
    return False


def _check_start_limits(client_ip: "str | None") -> None:
    ip_key = _START_IP_KEY + _sha256(client_ip or "unknown")[:32]
    try:
        within_ip = _bump(ip_key, START_LIMIT_PER_IP, START_WINDOW_SECONDS)
        within_all = within_ip and _bump(_START_ALL_KEY, START_LIMIT_GLOBAL, START_WINDOW_SECONDS)
    except Exception as exc:
        raise _store_failure(exc) from None
    if not (within_ip and within_all):
        raise RateLimited(START_WINDOW_SECONDS)


def _assert_not_locked(user_id: str) -> None:
    try:
        raw = _redis().get(_WRONG_KEY + user_id)
        wrong = int(raw) if raw is not None else 0
    except Exception as exc:
        raise _store_failure(exc) from None
    if wrong >= MAX_WRONG_ATTEMPTS:
        raise RateLimited(LOCK_SECONDS)


def _record_wrong_attempt(user_id: str) -> None:
    try:
        _bump(_WRONG_KEY + user_id, MAX_WRONG_ATTEMPTS, LOCK_SECONDS)
    except Exception as exc:
        raise _store_failure(exc) from None
    logger.info("[devices] wrong user code user=%s", user_id)


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

# The newest 200 devices is far more than anyone pairs; the cap just keeps the
# response bounded.
_LIST_ACTIVE_SQL = """
    select id, device_name, os, client_version, created_at, last_seen_at, revoked_at
      from devices
     where user_id = %s and revoked_at is null
     order by created_at desc
     limit 200
"""
_LIST_ALL_SQL = """
    select id, device_name, os, client_version, created_at, last_seen_at, revoked_at
      from devices
     where user_id = %s
     order by created_at desc
     limit 200
"""


class PgStore:
    """All the SQL. Stateless. Every statement is one atomic step, so the
    guarantees (single collection of a credential, revocation checked in the
    same statement that stamps last_seen) come from Postgres, not from
    Python-side check-then-act. Callers hand in hashes, never secrets."""

    def create_pairing(self, *, device_code_hash, user_code_hash, device_name,
                       os_name, client_version, ttl_seconds) -> bool:
        """False on a hash collision. ON CONFLICT DO NOTHING rather than
        catching a unique violation: that error's DETAIL would print the
        conflicting hash, and a user-code hash is trivially reversible."""
        with db.cursor(trusted=True) as cur:
            cur.execute(
                """
                insert into device_pairings
                    (device_code_hash, user_code_hash, device_name, os,
                     client_version, expires_at)
                values (%s, %s, %s, %s, %s, now() + make_interval(secs => %s::double precision))
                on conflict do nothing
                returning id
                """,
                (device_code_hash, user_code_hash, device_name, os_name,
                 client_version, ttl_seconds),
            )
            return cur.fetchone() is not None

    def purge_expired(self) -> None:
        with db.cursor(trusted=True) as cur:
            # A dead pairing row is kept for a day after it expires (so a late
            # poll still gets a clean "expired"), then dropped.
            cur.execute(
                "delete from device_pairings where expires_at < now() - interval '1 day'"
            )

    def approve(self, *, user_code_hash, user_id) -> "dict | None":
        """Bind a pending, unexpired pairing to `user_id`. Idempotent for the
        same account (a double click is not an error); None for anything
        else, which the caller counts as a wrong attempt."""
        with db.cursor(user_id=user_id, trusted=True) as cur:
            cur.execute(
                """
                update device_pairings
                   set status = 'approved', user_id = %s, approved_at = now()
                 where user_code_hash = %s and status = 'pending' and expires_at > now()
             returning device_name, os, client_version
                """,
                (user_id, user_code_hash),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    select device_name, os, client_version from device_pairings
                     where user_code_hash = %s and user_id = %s
                       and status in ('approved', 'consumed') and expires_at > now()
                    """,
                    (user_code_hash, user_id),
                )
                row = cur.fetchone()
            return dict(row) if row else None

    def deny(self, *, user_code_hash) -> bool:
        with db.cursor(trusted=True) as cur:
            cur.execute(
                """
                update device_pairings set status = 'denied'
                 where user_code_hash = %s and status = 'pending' and expires_at > now()
             returning id
                """,
                (user_code_hash,),
            )
            return cur.fetchone() is not None

    def claim(self, *, device_code_hash, credential_hash) -> "dict | None":
        """The single-collection step. One transaction: flip the pairing
        approved -> consumed (only one concurrent caller can match that
        row), then create the device with the credential's hash. If the
        second statement fails the first rolls back and the pairing is still
        collectable -- the credential was never handed out."""
        with db.cursor(trusted=True) as cur:
            cur.execute(
                """
                update device_pairings
                   set status = 'consumed', consumed_at = now()
                 where device_code_hash = %s and status = 'approved' and expires_at > now()
             returning user_id, device_name, os, client_version
                """,
                (device_code_hash,),
            )
            pairing = cur.fetchone()
            if pairing is None:
                return None
            cur.execute(
                """
                insert into devices (user_id, device_name, os, client_version, credential_hash)
                values (%s, %s, %s, %s, %s)
                returning id
                """,
                (pairing["user_id"], pairing["device_name"], pairing["os"],
                 pairing["client_version"], credential_hash),
            )
            return {"device_id": str(cur.fetchone()["id"]), "user_id": pairing["user_id"]}

    def pairing_state(self, *, device_code_hash) -> "dict | None":
        with db.cursor(trusted=True) as cur:
            cur.execute(
                "select status, (expires_at <= now()) as expired "
                "from device_pairings where device_code_hash = %s",
                (device_code_hash,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def touch_active_by_credential(self, *, credential_hash) -> "dict | None":
        """Revocation check and last-seen stamp in ONE statement: there is no
        window between "was it revoked?" and "issue a token"."""
        with db.cursor(trusted=True) as cur:
            cur.execute(
                """
                update devices set last_seen_at = now()
                 where credential_hash = %s and revoked_at is null
             returning id, user_id
                """,
                (credential_hash,),
            )
            row = cur.fetchone()
            return {"id": str(row["id"]), "user_id": row["user_id"]} if row else None

    def is_active(self, *, user_id, device_id) -> bool:
        with db.cursor(trusted=True) as cur:
            cur.execute(
                "select 1 as ok from devices "
                "where id = %s::uuid and user_id = %s and revoked_at is null",
                (device_id, user_id),
            )
            return cur.fetchone() is not None

    def list_devices(self, *, user_id, include_revoked) -> list:
        # Two literal statements (not one chosen into a variable) so that
        # tests/static/test_sql_grants.py can read both.
        with db.cursor(user_id=user_id) as cur:    # RLS: own rows only
            if include_revoked:
                cur.execute(_LIST_ALL_SQL, (user_id,))
            else:
                cur.execute(_LIST_ACTIVE_SQL, (user_id,))
            return [dict(row) for row in cur.fetchall()]

    def revoke(self, *, user_id, device_id) -> "dict | None":
        """Idempotent: revoking an already-revoked device keeps its original
        revoked_at. None if the device is not this user's (or not there)."""
        with db.cursor(user_id=user_id) as cur:    # RLS: own rows only
            cur.execute(
                """
                update devices set revoked_at = coalesce(revoked_at, now())
                 where id = %s::uuid and user_id = %s
             returning id, revoked_at
                """,
                (device_id, user_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


_STORE = PgStore()


# ---------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------

def start_pairing(*, device_name: str, os_name: str, client_version: str,
                  client_ip: "str | None") -> dict:
    """Terminal step 1. Returns the device_code (secret), the user_code to
    show, where to enter it, how often to poll and how long it all lives."""
    _device_jwt_secret()                 # no point pairing if tokens can't be issued
    _check_start_limits(client_ip)
    try:
        _STORE.purge_expired()           # housekeeping; never blocks a login
    except Exception as exc:
        logger.warning("[devices] could not purge expired pairings (%s)", type(exc).__name__)

    for _ in range(_CODE_RETRIES):
        device_code = secrets.token_urlsafe(32)
        user_code = new_user_code()
        if _STORE.create_pairing(
            device_code_hash=_sha256(device_code),
            user_code_hash=_sha256(user_code),
            device_name=device_name, os_name=os_name, client_version=client_version,
            ttl_seconds=PAIRING_TTL_SECONDS,
        ):
            return {
                "device_code": device_code,
                "user_code": format_user_code(user_code),
                "verification_url": verification_url(),
                "interval": POLL_INTERVAL_SECONDS,
                "expires_in": PAIRING_TTL_SECONDS,
            }
    raise StoreUnavailable("could not allocate a pairing code")


def approve_pairing(user_id: str, user_code: str) -> dict:
    """Web step: the signed-in `user_id` approves the code the terminal is
    showing. Raises RateLimited while the account is locked, InvalidCode for
    a wrong/expired/used code."""
    _assert_not_locked(user_id)
    normalized = normalize_user_code(user_code)
    if normalized is None:
        raise InvalidCode("invalid code")     # wrong shape: not a guess, not counted
    row = _STORE.approve(user_code_hash=_sha256(normalized), user_id=user_id)
    if row is None:
        _record_wrong_attempt(user_id)
        raise InvalidCode("invalid code")
    logger.info("[devices] pairing approved user=%s", user_id)
    return {"status": "approved", "device_name": row["device_name"],
            "os": row["os"], "client_version": row["client_version"]}


def deny_pairing(user_id: str, user_code: str) -> dict:
    """Web step: refuse a code ("that is not my terminal"). The terminal's
    next poll answers `denied`. Shares approve's attempt limits."""
    _assert_not_locked(user_id)
    normalized = normalize_user_code(user_code)
    if normalized is None:
        raise InvalidCode("invalid code")
    if not _STORE.deny(user_code_hash=_sha256(normalized)):
        _record_wrong_attempt(user_id)
        raise InvalidCode("invalid code")
    logger.info("[devices] pairing denied user=%s", user_id)
    return {"status": "denied"}


def poll_pairing(device_code: str) -> dict:
    """Terminal step 2, repeated every `interval` seconds. Returns a dict
    with `status` in pending | approved | denied | expired; `approved` also
    carries device_credential and device_id, and does so exactly once.
    Raises InvalidDeviceCode for an unknown, malformed or already-collected
    code (the three are deliberately indistinguishable)."""
    _device_jwt_secret()    # don't burn a pairing we could not honour
    if not isinstance(device_code, str) or not _CREDENTIAL_RE.match(device_code):
        raise InvalidDeviceCode("invalid device code")
    code_hash = _sha256(device_code)

    credential = secrets.token_urlsafe(32)          # 256 bits
    claimed = _STORE.claim(device_code_hash=code_hash, credential_hash=_sha256(credential))
    if claimed is not None:
        logger.info("[devices] device paired id=%s user=%s", claimed["device_id"], claimed["user_id"])
        return {"status": "approved", "device_credential": credential,
                "device_id": claimed["device_id"]}

    state = _STORE.pairing_state(device_code_hash=code_hash)
    if state is None or state["status"] == "consumed":
        raise InvalidDeviceCode("invalid device code")
    if state["status"] == "denied":
        return {"status": "denied"}
    if state["expired"]:
        return {"status": "expired"}
    return {"status": "pending"}    # includes an approval that landed a moment ago


def issue_device_token(device_credential: str) -> dict:
    """Exchange the long-lived credential for a ~15 minute JWT. Refuses
    (InvalidCredential) for a malformed, unknown or revoked credential, and
    (NotConfigured) before touching the database if the secret is missing."""
    secret = _device_jwt_secret()
    if not isinstance(device_credential, str) or not _CREDENTIAL_RE.match(device_credential):
        raise InvalidCredential("invalid credential")
    device = _STORE.touch_active_by_credential(credential_hash=_sha256(device_credential))
    if device is None:
        raise InvalidCredential("invalid credential")
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": device["user_id"],
            "device_id": device["id"],
            "token_type": DEVICE_JWT_TOKEN_TYPE,
            "aud": DEVICE_JWT_AUDIENCE,
            "iat": now,
            "exp": now + DEVICE_JWT_TTL_SECONDS,
        },
        secret,
        algorithm=DEVICE_JWT_ALGORITHM,
    )
    return {"jwt": token, "expires_in": DEVICE_JWT_TTL_SECONDS}


def verify_device_token(token: str) -> DeviceIdentity:
    """Is `token` a currently valid token for a live, unrevoked device?
    Raises DeviceAuthError if not, for any reason, with a message that never
    echoes the token. For S04's handshake and heartbeat."""
    try:
        secret = _device_jwt_secret()
    except NotConfigured:
        raise DeviceAuthError("device tokens are not configured") from None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[DEVICE_JWT_ALGORITHM],      # pinned: never "none", never RS/ES
            audience=DEVICE_JWT_AUDIENCE,
            options={"require": ["exp", "iat", "sub", "aud"]},
            leeway=JWT_LEEWAY_SECONDS,                # clock skew between instances
        )
    except jwt.PyJWTError:
        raise DeviceAuthError("invalid device token") from None

    user_id, device_id = claims.get("sub"), claims.get("device_id")
    if (claims.get("token_type") != DEVICE_JWT_TOKEN_TYPE
            or not isinstance(user_id, str) or not user_id
            or not _is_uuid(device_id)):
        raise DeviceAuthError("invalid device token")
    if not _STORE.is_active(user_id=user_id, device_id=device_id):
        raise DeviceAuthError("device is revoked or unknown")
    return DeviceIdentity(user_id=user_id, device_id=device_id, expires_at=int(claims["exp"]))


def _device_view(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "device_name": row["device_name"],
        "os": row["os"],
        "client_version": row["client_version"],
        "created_at": _iso(row["created_at"]),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "revoked_at": _iso(row.get("revoked_at")),
    }


def list_devices(user_id: str, include_revoked: bool = False) -> list:
    """The account's devices, newest first. Revoked ones are left out unless
    asked for -- the Devices page shows the active list."""
    rows = _STORE.list_devices(user_id=user_id, include_revoked=include_revoked)
    return [_device_view(row) for row in rows]


def revoke_device(user_id: str, device_id: str) -> dict:
    """Revoke one of the account's own devices. DeviceNotFound for anything
    else -- another account's device is indistinguishable from a missing one."""
    if not _is_uuid(device_id):
        raise DeviceNotFound("no such device")
    row = _STORE.revoke(user_id=user_id, device_id=device_id)
    if row is None:
        raise DeviceNotFound("no such device")
    logger.info("[devices] device revoked id=%s user=%s", row["id"], user_id)
    return {"id": str(row["id"]), "revoked_at": _iso(row["revoked_at"])}
