"""
m2c/src/m2c/link/reconnect.py -- Patch C04: when to try again, how long to
wait, and which failures mean "stop for good".

The device link (client.py) holds one WebSocket to the server for as long as the
person is working.  Networks drop, laptops sleep, servers restart, so the link
reconnects by itself -- but a loop that keeps retrying a revoked device is a
bug, so this module also decides what ends the loop.

THE BACKOFF: exponential, FULL jitter, capped
---------------------------------------------
After the n-th failure in a row the next wait is drawn uniformly from
``[0, min(cap, base * factor**n))`` -- "full jitter".  The usual alternative,
"the delay plus a little noise", leaves many clients that were dropped by the
same server restart retrying in near lock-step; spreading each wait over the
whole interval spreads the herd.  Defaults: base 1 s, factor 2, cap 30 s.

The sequence is reset only by a connection that *actually stayed up*
(``stable_after_s``, 10 s by default).  A server that accepts the handshake and
drops the socket at once must not hold the delay at its minimum: that would be
a tight retry loop with extra steps.  A connect that never got as far as
``welcome`` has not stayed up either.

The random source and the clock are parameters (``Backoff(rng=...)``, and the
link's ``clock=``/``sleep=``), so tests are instant and exact.

A server that says how long to wait (``error rate_limited``,
``details.retry_after_s``) sets a FLOOR for the next delay: the wait is the
larger of the jittered delay and what the server asked for, clamped to
``max_retry_after_s`` so a hostile or broken value cannot park the client for
days.

WHAT ENDS THE LOOP
------------------
``classify(exc)`` answers "fatal", "retry" or "unexpected":

* fatal -- ``unauthorized``, ``device_revoked``, ``protocol_unsupported`` (the
  server's ``min_protocol`` is above anything this client speaks), being
  superseded by a newer connection of the same device, a failing local store,
  an internal error while handling a message, a closed link, and every problem with the stored credential (not logged in,
  a file others can read, a credential for another server).  The link stops,
  and the exception reaches the caller of ``run()``/``wait_closed()``.
* retry -- network errors, timeouts, a closed or refused connection, a bad or
  missing ``welcome`` (a proxy or a deploy in flight), ``rate_limited``,
  ``internal``.
* unexpected -- anything else is a bug in this program.  It is not swallowed
  and not retried forever; it ends the loop like a fatal error and propagates.

THE ERRORS
----------
Every ``LinkError`` carries fixed, human-written text.  None is built from text
taken from the server, a library exception or a message: the only variable
parts are small validated integers (protocol numbers, byte limits) and protocol
error codes from the closed list in protocol/v1/common.json.  ``code`` is the
protocol's own string where one applies.  They are raised outside ``except``
blocks, so a traceback does not drag the original exception (and whatever its
frame held) along.

Place this file at: m2c/src/m2c/link/reconnect.py
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable

from websockets.exceptions import WebSocketException

from ..auth.credentials import AuthError

ERROR_CODES = (
    "unauthorized",
    "device_revoked",
    "quota_exhausted",
    "run_not_found",
    "protocol_unsupported",
    "rate_limited",
    "provider_unavailable",
    "tool_timeout",
    "invalid_message",
    "internal",
)  # protocol/v1/common.json error_code; a client treats an unknown code as "internal"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LinkError(Exception):
    """Base class of everything the device link raises on purpose.

    ``code`` is the protocol error code this corresponds to, when there is one.
    ``retryable`` says whether the same operation could succeed later (a
    dropped connection) or not (a revoked device)."""

    code: str | None = None
    retryable: bool = False
    default_message = "The device link failed."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.default_message)


class LinkUnauthorizedError(LinkError):
    """The server did not accept the device credential.  Final."""

    code = "unauthorized"
    default_message = (
        "The server did not accept this device's credential. "
        "Run 'm2c logout' and then 'm2c login' to sign in again."
    )


class LinkDeviceRevokedError(LinkError):
    """The device was revoked (on the Devices page of the web app).  Final."""

    code = "device_revoked"
    default_message = "This device was revoked. Run 'm2c login' to sign in again."


class LinkProtocolUnsupportedError(LinkError):
    """This client and the server share no protocol version.  Final.

    ``min_protocol`` is the lowest version the server would speak with this
    client (its own minimum, or the version it chose when that is one this
    client never offered); ``max_protocol`` is the newest version THIS client
    speaks.  Either can be None when the server did not say."""

    code = "protocol_unsupported"

    def __init__(self, min_protocol: int | None = None, max_protocol: int | None = None) -> None:
        self.min_protocol = min_protocol
        self.max_protocol = max_protocol
        if isinstance(min_protocol, int) and isinstance(max_protocol, int):
            message = (
                f"This server needs a newer m2c (it requires protocol {min_protocol} or newer; "
                f"this version speaks up to {max_protocol}). Update m2c and try again."
            )
        else:
            message = "This server and this version of m2c share no protocol version. Update m2c and try again."
        super().__init__(message)


class LinkSupersededError(LinkError):
    """Another connection for the same device replaced this one (the server
    closes the older one with 4409).  Final: reconnecting would start a tug of
    war between two processes."""

    default_message = (
        "Another m2c process connected as this same device and replaced this connection. "
        "Close the other one and try again."
    )


class LinkStoreError(LinkError):
    """The local store could not record a server event.  Final: carrying on
    without recording would let the device log fall behind the run."""

    default_message = (
        "The local session store could not be written, so the link was stopped to keep the log consistent. "
        "Check the disk space and the permissions of the m2c config directory."
    )


class LinkInternalError(LinkError):
    """The link failed while handling a server message, for a reason that is a
    bug in this program rather than a network problem.  Final, and carrying
    fixed text only: the message being handled is never part of it."""

    default_message = (
        "The device link hit an internal error and was stopped. "
        "Please report it (the message content is deliberately not included)."
    )


class LinkClosedError(LinkError):
    """The link was closed (``aclose()``), or never will be started."""

    default_message = "The device link is closed."


class LinkConnectionError(LinkError):
    """The connection failed, was lost, or the server's first answer was not
    usable.  Retried with backoff."""

    retryable = True
    default_message = "The connection to the server was lost."


class LinkRateLimitedError(LinkConnectionError):
    """The server asked this device to slow down.  ``retry_after_s`` is how long
    it asked for (0 when it did not say)."""

    code = "rate_limited"
    default_message = "The server is rate limiting this device; waiting before trying again."

    def __init__(self, retry_after_s: int | float = 0) -> None:
        self.retry_after_s = retry_after_s
        super().__init__()


class LinkNotConnectedError(LinkConnectionError):
    """A message was sent while there is no connection (never started, or between
    two connections).  Nothing is queued silently; the caller decides."""

    default_message = "The device link is not connected right now."


class LinkRequestError(LinkError):
    """The server answered one particular request with an ``error``.  ``code``
    is the protocol's error code, ``ref_id`` the id of the message it answers."""

    def __init__(self, code: str, ref_id: str | None = None, details: dict | None = None) -> None:
        self.code = code if code in ERROR_CODES else "internal"
        self.ref_id = ref_id
        self.details = details
        super().__init__(f"The server rejected the request ({self.code}).")


class LinkRequestTimeout(LinkError):
    """No answer to a request in time."""

    retryable = True
    default_message = "The server did not answer in time."


class LinkMessageTooLargeError(LinkError):
    """A message is bigger than the server's announced limit; it was not sent."""

    def __init__(self, limit_bytes: int | None = None) -> None:
        self.limit_bytes = limit_bytes
        suffix = f" (the limit is {limit_bytes} bytes)" if isinstance(limit_bytes, int) else ""
        super().__init__(f"The message is larger than the server accepts{suffix}; it was not sent.")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
_FATAL = (
    LinkUnauthorizedError,
    LinkDeviceRevokedError,
    LinkProtocolUnsupportedError,
    LinkSupersededError,
    LinkStoreError,
    LinkInternalError,
    LinkClosedError,
    AuthError,  # not logged in, loose credentials file, wrong server: only the person can fix these
)


def classify(exc: BaseException) -> str:
    """``"fatal"`` (stop, raise), ``"retry"`` (back off and reconnect) or
    ``"unexpected"`` (a bug: stop and propagate, never swallow)."""
    if isinstance(exc, _FATAL):
        return "fatal"
    if isinstance(exc, LinkError):
        return "retry" if exc.retryable else "fatal"
    if isinstance(exc, (OSError, TimeoutError, WebSocketException)):
        return "retry"
    return "unexpected"


def retry_after_of(exc: BaseException) -> float | None:
    """The wait the server asked for, if ``exc`` carries one."""
    if isinstance(exc, LinkRateLimitedError):
        return exc.retry_after_s
    return None


# ---------------------------------------------------------------------------
# The backoff
# ---------------------------------------------------------------------------
def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class BackoffPolicy:
    """Tunables of the reconnect delay (see the module docstring)."""

    base_s: float = 1.0
    factor: float = 2.0
    cap_s: float = 30.0
    stable_after_s: float = 10.0
    max_retry_after_s: float = 300.0

    def __post_init__(self) -> None:
        base = _finite(self.base_s, "base_s")
        factor = _finite(self.factor, "factor")
        cap = _finite(self.cap_s, "cap_s")
        stable = _finite(self.stable_after_s, "stable_after_s")
        retry = _finite(self.max_retry_after_s, "max_retry_after_s")
        if base <= 0:
            raise ValueError("base_s must be positive")
        if factor < 1:
            raise ValueError("factor must be at least 1")
        if cap < base:
            raise ValueError("cap_s must not be smaller than base_s")
        if stable < 0 or retry < 0:
            raise ValueError("stable_after_s and max_retry_after_s must not be negative")


class Backoff:
    """The delay sequence.  One instance per link; not thread-safe (the link
    uses it from its event loop only)."""

    def __init__(self, policy: BackoffPolicy | None = None, *, rng: Callable[[], float] = random.random) -> None:
        self.policy = policy or BackoffPolicy()
        self._rng = rng
        self.failures = 0
        p = self.policy
        # The exponent at which base * factor**e reaches the cap; past it the
        # ceiling no longer grows, so a long outage cannot overflow a float.
        if p.factor > 1 and p.cap_s > p.base_s:
            self._max_exponent = math.ceil(math.log(p.cap_s / p.base_s) / math.log(p.factor)) + 1
        else:
            self._max_exponent = 0

    def ceiling(self) -> float:
        """The upper end of the next delay's range."""
        p = self.policy
        exponent = min(self.failures, self._max_exponent)
        return min(p.cap_s, p.base_s * (p.factor ** exponent))

    def next_delay(self, retry_after_s: float | None = None) -> float:
        """The wait before the next attempt, and count one more failure."""
        draw = min(max(float(self._rng()), 0.0), 1.0)
        delay = draw * self.ceiling()
        self.failures += 1
        if retry_after_s is not None:
            asked = min(max(float(retry_after_s), 0.0), self.policy.max_retry_after_s)
            delay = max(delay, asked)
        return delay

    def record_connection(self, up_for_s: float) -> bool:
        """A connection that was up for ``up_for_s`` has just ended.  If it stayed
        up long enough to count as working, start the sequence over; returns
        whether it did."""
        if up_for_s >= self.policy.stable_after_s:
            self.reset()
            return True
        return False

    def reset(self) -> None:
        self.failures = 0
