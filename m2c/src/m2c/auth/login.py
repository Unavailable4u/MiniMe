"""
m2c/src/m2c/auth/login.py -- Patch C03: ``m2c login``, ``m2c logout`` and the
access-token helper.

THE FLOW (OAuth device authorisation, RFC 8628; server side is S03,
backend/api/routes/devices.py and backend/eo/devices.py -- these are the exact
shapes this module talks to)::

    login(server_url)
      POST /v1/code/devices/pair/start  {device_name, os, client_version}
        -> {device_code (secret), user_code, verification_url, interval, expires_in}
      print user_code and verification_url        (never opens a browser)
      every `interval` seconds:
        POST /v1/code/devices/pair/poll {device_code}
          -> {status: pending}                     keep waiting
             {status: approved, device_credential, device_id}   (ONCE)
             {status: denied} | {status: expired}  stop, say so
      store the credential (credentials.py), tell the person where

    get_access_token()
      POST /v1/code/devices/token {device_credential} -> {jwt, expires_in}
      cached in memory, refreshed before it expires

No browser is launched: from WSL2, and from any ssh session, that usually
fails, and the page works from any device anyway (build plan §11.1).

PATIENCE AND LIMITS
-------------------
* The server's ``interval`` is honoured, clamped to 1..60 seconds, and the
  first poll waits one interval (RFC 8628 §3.5).  ``expires_in`` is clamped to
  30 minutes; S03 uses 10.
* Temporary trouble -- a dropped connection, a 5xx, a 429, a reply that is not
  JSON (a captive portal) -- is retried with exponential backoff (doubling,
  capped at 60 s, with jitter, and honouring ``Retry-After``) until the code
  expires, then reported as what it was: "could not reach the server",
  "the server kept failing", not a generic timeout.  The person is told once,
  on stderr, that there is trouble; the log is not spammed.
* Permanent trouble -- a 4xx other than 429, a status word we do not know, a
  reply with the wrong shape -- stops at once.  Waiting cannot fix a bug.
* ``pair/start`` is tried up to three times on network errors and 5xx.  A 429
  from it is never retried (the limit is 10 per 10 minutes per address; the
  message says how long to wait).  Nothing is stored unless the server says
  ``approved``.
* Redirects are never followed: a redirect could move the pairing to another
  host, or downgrade it to cleartext.
* Replies are read up to 64 KiB.

THE ONE AWKWARD CASE
--------------------
S03 returns the credential exactly once (see eo/devices.py): if the poll
answer that carried it is lost, a retry gets 400 "already used" and the device
exists server-side with nobody holding its credential.  This module tracks
whether any earlier poll's outcome is *unknown* (connection dropped, 5xx,
unreadable reply) and, if so, makes the 400 message say a device may have been
created and must be revoked on the Devices page.  A proven-unclaimed code
(a later clean ``pending``) clears the suspicion.

WHAT LOGOUT DOES
----------------
``logout()`` deletes the local credential, in both the keyring and the file,
and empties the in-memory token.  It makes NO network call.  The build plan
says logout "calls revoke", but S03 only offers ``POST /api/devices/{id}/revoke``
for a *signed-in web user*; there is no endpoint a device can call with its own
credential to revoke itself, and this patch does not invent one.  So the
device stays registered server-side until it is revoked on the web Devices
page, and ``m2c logout`` says so.  Proposed small follow-up for S03: a
``POST /v1/code/devices/revoke {device_credential}`` that revokes the device the
credential belongs to (same hashing and lookup as ``/v1/code/devices/token``,
same fixed 401 for unknown credentials); logout would call it first and fall
back to the message when the server is unreachable.

REDACTION
---------
See credentials.py's docstring for the rules (Secret wrapper, no secret in a
local of a frame an exception can pass through, fixed messages, raise outside
``except``).  Specific to this file: every string in a server reply is turned
into a ``Secret`` by the JSON parser itself (``_wrap_strings``), so a reply
dict printed by anything shows ``<redacted>`` for every text value, and the
reply's own words never reach a message -- not even an "unknown status" one.
Only validated values (a user code of letters and digits, a URL of printable
ASCII) are ever printed, and only after validation.  Transport failures are
reduced to a *kind*; the library exception's text is never read.
``urllib`` is the only HTTP client (standard library, no new dependency).

Place this file at: m2c/src/m2c/auth/login.py
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import platform
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import IO, Any, Callable, Mapping
from urllib.parse import urlsplit

from .. import __version__, paths
from ..config import normalize_server_url, server_url_from_env
from ..logging_setup import log_event
from . import credentials as creds
from .credentials import (
    AuthNetworkError,
    AuthProtocolError,
    AuthServerError,
    Credential,
    CredentialStoreError,
    DeviceRevokedError,
    LoginDeniedError,
    LoginExpiredError,
    NotLoggedInError,
    Secret,
    ServerMismatchError,
)

logger = logging.getLogger("m2c.auth.login")

# S03 paths (api/routes/devices.py)
START_PATH = "/v1/code/devices/pair/start"
POLL_PATH = "/v1/code/devices/pair/poll"
TOKEN_PATH = "/v1/code/devices/token"

# Limits and tunables
REQUEST_TIMEOUT = 15.0            # seconds per HTTP request
MAX_RESPONSE_BYTES = 64 * 1024
MIN_INTERVAL = 1
MAX_INTERVAL = 60
MAX_EXPIRES_IN = 1800             # S03 uses 600
MAX_BACKOFF = 60                  # seconds between retries, at most
START_ATTEMPTS = 3
START_BACKOFF = 2                 # first wait between start attempts
TOKEN_REFRESH_MARGIN = 60         # refresh this long before the JWT expires...
MAX_TOKEN_LIFETIME = 3600         # ...and never trust a longer lifetime than this
MAX_JWT_LENGTH = 8192
MAX_LABEL_LENGTH = 64             # S03's PairStartRequest caps
DEFAULT_DEVICE_NAME = "m2c-device"

# Shapes of the values we accept from the server (S03: token_urlsafe(32) for the
# device code, consonants and a hyphen for the user code).
_DEVICE_CODE_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")
_USER_CODE_RE = re.compile(r"[A-Za-z0-9-]{4,32}")
_URL_RE = re.compile(r"[\x21-\x7e]{1,512}")
_JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_STATUSES = frozenset({"pending", "approved", "denied", "expired"})

Sleep = Callable[[float], None]
Clock = Callable[[], float]
Jitter = Callable[[float], float]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _default_jitter(delay: float) -> float:
    return delay * (1.0 + random.random() * 0.25)


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _printable_label(raw: str, fallback: str) -> str:
    cleaned = "".join(ch for ch in raw.strip() if ch.isprintable())[:MAX_LABEL_LENGTH].strip()
    return cleaned or fallback


def default_device_name() -> str:
    """The machine's host name, cleaned up to what S03 accepts (non-empty,
    printable, at most 64 characters)."""
    try:
        raw = socket.gethostname()
    except OSError:
        raw = ""
    return _printable_label(raw, DEFAULT_DEVICE_NAME)


def normalize_device_name(name: str) -> str:
    """Validate a name the person chose.  Raises ValueError with fixed text."""
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned or len(cleaned) > MAX_LABEL_LENGTH or not cleaned.isprintable():
        raise ValueError(f"the device name must be 1 to {MAX_LABEL_LENGTH} printable characters")
    return cleaned


def _os_label() -> str:
    return _printable_label(f"{platform.system()} {platform.release()}", "unknown")


# ---------------------------------------------------------------------------
# Transport: one POST, reduced to plain values
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it could move the pairing to another host or
    downgrade it.  Returning None makes urllib raise HTTPError(3xx) instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class _Outcome:
    """What one POST amounted to.  ``kind`` is ``"network"`` (nothing usable
    came back) or ``"response"`` (an HTTP status did; ``body`` is its payload,
    wrapped in a Secret, for a 2xx)."""

    kind: str
    status: int = 0
    body: Secret | None = None
    retry_after: int | None = None


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    return min(int(text), 86400) if text.isdigit() else None


def _encode_body(fields: Mapping[str, Any]) -> Secret:
    plain = {key: (val.reveal() if isinstance(val, Secret) else val) for key, val in fields.items()}
    return Secret(json.dumps(plain).encode("utf-8"))


def _post(base: str, path: str, fields: Mapping[str, Any], timeout: float = REQUEST_TIMEOUT) -> _Outcome:
    """POST ``fields`` as JSON to ``base + path``.

    Never raises for a network or HTTP problem: it returns an ``_Outcome``
    holding only the *kind* of failure, the status code and ``Retry-After``.
    The library's exception text is never read (it can echo what was being
    sent), and the exception object is gone before this function returns, so
    nothing downstream can reach its traceback.
    """
    body = _encode_body(fields)
    request = urllib.request.Request(
        base + path,
        data=body.reveal(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"m2c/{__version__}",
        },
    )
    outcome = _Outcome(kind="network")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            outcome = _Outcome(
                kind="response",
                status=response.status,
                body=Secret(response.read(MAX_RESPONSE_BYTES + 1)),
            )
    except urllib.error.HTTPError as http_error:  # before URLError/OSError: it is a subclass of both
        outcome = _Outcome(
            kind="response",
            status=http_error.code,
            retry_after=_parse_retry_after(
                http_error.headers.get("Retry-After") if http_error.headers else None
            ),
        )
        with contextlib.suppress(Exception):
            http_error.close()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        log_event(logger, logging.DEBUG, "auth.http.network_error", exc_type=type(exc).__name__)
    return outcome


def _wrap_strings(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook: every text value becomes a Secret, so a reply dict
    can be shown by anything (a debugger, a traceback with locals) without
    exposing a word the server said."""
    return {key: (Secret(val) if isinstance(val, str) else val) for key, val in pairs}


def _parse_object(body: Secret | None) -> dict[str, Any] | None:
    """The reply as a dict whose text values are Secrets, or None if it is not
    a JSON object (or is too large).  Catches its own failures."""
    if body is None:
        return None
    try:
        raw = body.reveal()
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        data = json.loads(raw, object_pairs_hook=_wrap_strings)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _matches(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, Secret) and isinstance(value.reveal(), str) and bool(pattern.fullmatch(value.reveal()))


def _status_word(reply: Mapping[str, Any]) -> str | None:
    """The reply's ``status`` if it is one of the four S03 words, else None.
    Returns text equal to a known constant, never arbitrary server text."""
    word = reply.get("status")
    if isinstance(word, Secret) and word.reveal() in _STATUSES:
        return word.reveal()
    return None


# ---------------------------------------------------------------------------
# pair/start
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _StartReply:
    device_code: Secret
    user_code: str          # validated: letters, digits, hyphen
    verification_url: str   # validated: printable ASCII, http(s), a host
    interval: int           # clamped
    lifetime: int           # clamped


def _validated_url(value: Any) -> str | None:
    if not _matches(value, _URL_RE):
        return None
    try:
        parts = urlsplit(value.reveal())
        usable = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return None
    return value.reveal() if usable else None


def _validate_start(reply: dict[str, Any] | None) -> _StartReply | None:
    if reply is None:
        return None
    url = _validated_url(reply.get("verification_url"))
    interval, expires_in = reply.get("interval"), reply.get("expires_in")
    if (
        not _matches(reply.get("device_code"), _DEVICE_CODE_RE)
        or not _matches(reply.get("user_code"), _USER_CODE_RE)
        or url is None
        or not _is_int(interval)
        or not _is_int(expires_in)
        or expires_in <= 0
    ):
        return None
    return _StartReply(
        device_code=reply["device_code"],
        user_code=reply["user_code"].reveal(),
        verification_url=url,
        interval=max(MIN_INTERVAL, min(interval, MAX_INTERVAL)),
        lifetime=min(expires_in, MAX_EXPIRES_IN),
    )


def _start_failure(outcome: _Outcome) -> AuthNetworkError | AuthServerError:
    """The error for a start attempt that did not succeed (network or HTTP)."""
    if outcome.kind == "network":
        return AuthNetworkError(
            "Could not reach the server. Check your connection and M2C_SERVER_URL, then run 'm2c login' again."
        )
    if outcome.status == 429:
        wait = "a little while" if outcome.retry_after is None else _human_duration(outcome.retry_after)
        return AuthServerError(f"Too many sign-in attempts. Try again in {wait}.", 429)
    if outcome.status >= 500:
        return AuthServerError(
            f"The server is not available right now (HTTP {outcome.status}). Try again shortly.", outcome.status
        )
    return AuthServerError(
        f"The server refused the sign-in request (HTTP {outcome.status}). "
        "Check that M2C_SERVER_URL points at your MiniMe server.",
        outcome.status,
    )


def _start(base: str, fields: dict[str, Any], sleep: Sleep, jitter: Jitter) -> _StartReply:
    delay = START_BACKOFF
    for attempt in range(1, START_ATTEMPTS + 1):
        outcome = _post(base, START_PATH, fields)
        if outcome.kind == "response" and 200 <= outcome.status < 300:
            started = _validate_start(_parse_object(outcome.body))
            if started is None:
                break
            return started
        retryable = outcome.kind == "network" or (outcome.status >= 500)
        if not retryable or attempt == START_ATTEMPTS:
            raise _start_failure(outcome)
        sleep(jitter(delay))
        delay = min(delay * 2, MAX_BACKOFF)
    raise AuthProtocolError(
        "The server's reply was not in the expected format. "
        "Check that M2C_SERVER_URL points at your MiniMe server."
    )


# ---------------------------------------------------------------------------
# pair/poll
# ---------------------------------------------------------------------------
def _poll_400_error(maybe_issued: bool) -> AuthServerError:
    if maybe_issued:
        return AuthServerError(
            "The sign-in code was not accepted (unknown or already used). Because an earlier reply may have "
            "been lost, a device may have been created that this machine never received a credential for: "
            "open the Devices page in the web app and revoke any new device you do not recognise, then run "
            "'m2c login' again.",
            400,
        )
    return AuthServerError("The sign-in code is no longer valid. Run 'm2c login' to start again.", 400)


def _login_expired() -> LoginExpiredError:
    return LoginExpiredError("The sign-in code expired before it was approved. Run 'm2c login' to get a new code.")


def _terminal_failure(kind: str, status: int) -> AuthNetworkError | AuthServerError | AuthProtocolError:
    """What to report when the code ran out while the last thing that went
    wrong was ``kind`` (rather than plain waiting)."""
    if kind == "network":
        return AuthNetworkError(
            "Could not reach the server before the sign-in code expired. "
            "Check your connection, then run 'm2c login' again."
        )
    if kind == "server":
        return AuthServerError(
            f"The server kept answering with an error (HTTP {status}) until the sign-in code expired. "
            "Try again later.",
            status,
        )
    return AuthProtocolError(
        "The server's replies could not be understood before the sign-in code expired. "
        "Run 'm2c login' again, and check that M2C_SERVER_URL points at your MiniMe server."
    )


@dataclass
class LoginResult:
    device_id: str
    device_name: str
    storage: str  # "keyring" or "file"


def _wait_for_approval(
    base: str,
    started: _StartReply,
    sleep: Sleep,
    clock: Clock,
    jitter: Jitter,
    err: IO[str],
) -> dict[str, Any]:
    """Poll until approved (returns the reply) or raise the right error."""
    deadline = clock() + started.lifetime
    interval = started.interval
    delay: float = interval
    backing_off = False
    maybe_issued = False   # is some earlier poll's outcome unknown?
    warned = False
    last_failure: tuple[str, int] | None = None   # (kind, status) of the most recent trouble
    polls = 0

    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        wait = jitter(delay) if backing_off else delay
        sleep(min(wait, remaining))

        polls += 1
        outcome = _post(base, POLL_PATH, {"device_code": started.device_code})
        trouble: tuple[str, int] | None = None
        retry_after: int | None = None

        if outcome.kind == "network":
            trouble = ("network", 0)
            maybe_issued = True
        elif outcome.status == 200:
            reply = _parse_object(outcome.body)
            if reply is None:
                trouble = ("protocol", 200)
                maybe_issued = True
            else:
                word = _status_word(reply)
                if word is None:
                    raise AuthProtocolError(
                        "The server answered the sign-in check with a status this version of m2c does not "
                        "know. Update m2c, or check that M2C_SERVER_URL points at your MiniMe server."
                    )
                log_event(logger, logging.DEBUG, "auth.login.poll", status=word, count=polls)
                if word == "approved":
                    return reply
                if word == "denied":
                    raise LoginDeniedError(
                        "The sign-in was denied in the web app. Nothing was stored. "
                        "Run 'm2c login' to try again."
                    )
                if word == "expired":
                    raise _login_expired()
                maybe_issued = False   # a clean 'pending' proves the code is still unclaimed
                last_failure = None
                backing_off = False
                delay = interval
        elif outcome.status == 429:
            trouble = ("server", 429)
            retry_after = outcome.retry_after
        elif outcome.status >= 500:
            trouble = ("server", outcome.status)
            maybe_issued = True
        elif outcome.status == 400:
            raise _poll_400_error(maybe_issued)
        else:
            raise AuthServerError(
                f"The server rejected the sign-in check (HTTP {outcome.status}). "
                "Run 'm2c login' to start again.",
                outcome.status,
            )

        if trouble is not None:
            last_failure = trouble
            backing_off = True
            delay = retry_after if retry_after else min(delay * 2, MAX_BACKOFF)
            log_event(logger, logging.DEBUG, "auth.login.retry", code=trouble[1], count=polls)
            if not warned:
                warned = True
                print(
                    "m2c: having trouble reaching the server; will keep trying until the code expires.",
                    file=err,
                )
                err.flush()

    if last_failure is not None:
        raise _terminal_failure(*last_failure)
    raise _login_expired()


# ---------------------------------------------------------------------------
# login / logout
# ---------------------------------------------------------------------------
def _has_stored_credential() -> bool:
    try:
        creds.load_credential()
    except NotLoggedInError:
        return False
    except Exception:  # unreadable or insecure: something is there, and login will replace it
        return True
    return True


def _unusable_approval() -> AuthProtocolError:
    return AuthProtocolError(
        "The server approved this device, but its reply could not be used, so nothing was stored. "
        "Open the Devices page in the web app, revoke any new device you do not recognise, "
        "and run 'm2c login' again."
    )


def _credential_from_reply(reply: dict[str, Any], server_url: str) -> Credential | None:
    """Build the Credential from an approved reply, or None if it is unusable.
    Catches its own failures (see the redaction notes in the module docstring)."""
    try:
        return Credential(
            device_credential=reply["device_credential"],
            device_id=reply["device_id"].reveal(),
            server_url=server_url,
        )
    except Exception:
        return None


def login(
    server_url: str,
    *,
    device_name: str | None = None,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
    sleep: Sleep | None = None,
    clock: Clock | None = None,
    jitter: Jitter | None = None,
) -> LoginResult:
    """Run the device-code flow against ``server_url`` and store the result.

    Prints the user code and verification URL to ``out`` (stdout by default)
    and one warning at most to ``err``.  Raises ``config.ServerUrlError`` for an
    unsafe address, ``ValueError`` for an unusable ``device_name``,
    ``paths.ConfigDirError`` if the config directory cannot be used (checked
    *before* a pairing is started, so an approval is never wasted), and an
    ``AuthError`` subclass for everything that can go wrong in the flow.
    ``sleep``, ``clock`` and ``jitter`` exist so tests run on fake time.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    sleep = time.sleep if sleep is None else sleep
    clock = time.monotonic if clock is None else clock
    jitter = _default_jitter if jitter is None else jitter

    base = normalize_server_url(server_url)
    name = default_device_name() if device_name is None else normalize_device_name(device_name)
    paths.prepare_config_dir()
    replacing = _has_stored_credential()

    log_event(logger, logging.INFO, "auth.login.started")
    started = _start(
        base,
        {"device_name": name, "os": _os_label(), "client_version": __version__},
        sleep,
        jitter,
    )

    print("To sign in, open this page in a browser (on any device):", file=out)
    print(f"\n    {started.verification_url}\n", file=out)
    print("and enter this code:", file=out)
    print(f"\n    {started.user_code}\n", file=out)
    print(
        f"The code expires in {_human_duration(started.lifetime)}. "
        "Waiting for you to approve it... (Ctrl+C to cancel)",
        file=out,
    )
    out.flush()

    reply = _wait_for_approval(base, started, sleep, clock, jitter, err)

    credential = _credential_from_reply(reply, base)
    if credential is None:
        raise _unusable_approval()
    try:
        storage = creds.save_credential(credential)
        saved = True
    except (CredentialStoreError, paths.ConfigDirError, OSError):
        saved = False
    if not saved:
        raise CredentialStoreError(
            "You approved this device, but its credential could not be saved on this machine. "
            "Open the Devices page in the web app and revoke the new device, then run 'm2c login' again."
        )

    reset_token_cache()
    log_event(logger, logging.INFO, "auth.login.done", device_id=credential.device_id, status=storage)
    where = (
        "your OS keyring"
        if storage == "keyring"
        else f"{creds.credentials_path()} (permissions 0600)"
    )
    print(f"\nSigned in. This device is registered as {name!r}.", file=out)
    print(f"Credential saved in {where}.", file=out)
    if replacing:
        print(
            "A previous credential on this machine was replaced. If that device is no longer in use, "
            "revoke it on the Devices page of the web app.",
            file=out,
        )
    out.flush()
    return LoginResult(device_id=credential.device_id, device_name=name, storage=storage)


def logout() -> bool:
    """Delete the local credential (keyring and file) and the in-memory token.
    Returns True if a credential was found and removed.  Makes no network call:
    S03 has no device-authenticated revoke endpoint (see the module docstring),
    so the device must also be revoked on the web Devices page.  Raises
    CredentialStoreError if a copy could not be removed."""
    reset_token_cache()
    return creds.delete_credential()


# ---------------------------------------------------------------------------
# The access token helper
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _CachedToken:
    key: str            # digest of (credential, server): never the credential itself
    token: Secret
    refresh_at: float   # on the caller's clock


_token_lock = threading.Lock()
_token_cache: _CachedToken | None = None


def reset_token_cache() -> None:
    """Forget the cached JWT (logout, login, revocation)."""
    global _token_cache
    with _token_lock:
        _token_cache = None


def _clear_cache_locked() -> None:
    global _token_cache
    _token_cache = None


def _cache_key(credential: Credential) -> str:
    material = credential.device_credential.reveal() + "\0" + credential.server_url
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _server_for(credential: Credential, explicit: str | None) -> str:
    """The base URL to talk to.  A credential is only ever sent to the server
    that issued it: if the configured (or explicitly passed) server differs,
    refuse before sending anything."""
    wanted = normalize_server_url(explicit) if explicit is not None else server_url_from_env()
    if wanted is not None and wanted != credential.server_url:
        raise ServerMismatchError(
            "The configured server is not the one this device signed in to, so the credential was not "
            "sent. Unset M2C_SERVER_URL, or run 'm2c logout' and then 'm2c login' for the new server."
        )
    return credential.server_url


def _token_failure(outcome: _Outcome) -> AuthNetworkError | AuthServerError | DeviceRevokedError:
    if outcome.kind == "network":
        return AuthNetworkError("Could not reach the server to refresh this device's access token.")
    if outcome.status == 401:
        return DeviceRevokedError(
            "This device is no longer accepted by the server: it was revoked, or its credential is "
            "unknown. Run 'm2c logout' and then 'm2c login' to sign in again."
        )
    return AuthServerError(
        f"The server could not issue an access token (HTTP {outcome.status}).", outcome.status
    )


def get_access_token(
    *,
    server_url: str | None = None,
    clock: Clock | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Secret:
    """A valid device JWT, as a ``Secret`` (``.reveal()`` for the text).

    Loads the stored credential (with the permission check, so a loose file
    stops here), exchanges it at ``POST /v1/code/devices/token``, caches the
    JWT in memory and returns the cached one until it is within a safety margin
    of expiring (60 s, or half the lifetime for very short tokens; lifetimes
    over an hour are not trusted).  Concurrent callers share one request.

    Raises NotLoggedInError / CredentialStoreError (nothing usable stored),
    ServerMismatchError (wrong server for this credential), DeviceRevokedError
    (the server says this device is revoked or unknown: not retryable),
    AuthNetworkError / AuthServerError (``retryable`` says whether to try again;
    this function does not retry -- the link client has its own backoff) and
    AuthProtocolError (malformed reply).  A revoked device clears the cache but
    leaves the credential file for the person to remove with ``m2c logout``.
    """
    global _token_cache
    clock = time.monotonic if clock is None else clock
    with _token_lock:
        credential = creds.load_credential()
        base = _server_for(credential, server_url)
        key = _cache_key(credential)
        now = clock()
        cached = _token_cache
        if cached is not None and cached.key == key and now < cached.refresh_at:
            return cached.token

        outcome = _post(base, TOKEN_PATH, {"device_credential": credential.device_credential}, timeout)
        if outcome.kind != "response" or not 200 <= outcome.status < 300:
            if outcome.kind == "response" and outcome.status == 401:
                _clear_cache_locked()
                log_event(logger, logging.WARNING, "auth.token.revoked", device_id=credential.device_id)
            raise _token_failure(outcome)

        reply = _parse_object(outcome.body)
        lifetime = reply.get("expires_in") if reply is not None else None
        token = reply.get("jwt") if reply is not None else None
        if (
            not _matches(token, _JWT_RE)
            or len(token.reveal()) > MAX_JWT_LENGTH
            or not _is_int(lifetime)
            or lifetime <= 0
        ):
            raise AuthProtocolError("The server's access-token reply was not in the expected format.")

        usable = min(lifetime, MAX_TOKEN_LIFETIME)
        margin = min(TOKEN_REFRESH_MARGIN, usable / 2)
        _token_cache = _CachedToken(key=key, token=token, refresh_at=now + usable - margin)
        log_event(logger, logging.DEBUG, "auth.token.issued", device_id=credential.device_id)
        return token
