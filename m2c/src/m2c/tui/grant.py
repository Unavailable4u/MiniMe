"""
m2c/src/m2c/tui/grant.py -- Patch C09a: asking for more credits when a run is paused for budget (``g``).

``POST /v1/code/runs/{run_id}/grant`` (S10) raises the run's credit ceiling when the run is in ``paused_budget`` and
the account's pool has room.  The device authenticates with its short-lived access token (C03's
``get_access_token()``); the answer is numbers and a state word.  This module makes that one call with the standard
library and turns every outcome into a ``GrantResult`` and one fixed sentence (``describe``).

SAFETY
------
* The token is revealed only to build the ``Authorization`` header; it is never logged, printed or put in a result.
* The server address is the credential's own, run through ``normalize_server_url`` (https, or http to loopback
  only); redirects are NOT followed (a redirect would carry the token elsewhere).
* Nothing the server sends is shown except numbers it must have sent as numbers, a state word of lowercase letters
  and a ``reset_at`` timestamp that matches a strict pattern.  Error ``message`` text is never shown.
* Credits only; no dollars.
* Blocking work (the token fetch and the HTTP call) runs in a thread so the loop that carries the device link keeps
  running.

OUTCOMES (``GrantResult.kind``)
-------------------------------
``granted`` · ``pool_exhausted`` (shows ``reset_at``) · ``device_offline`` · ``not_paused`` · ``run_gone`` (the server
lost the run's buffer: start a new run) · ``rate_limited`` · ``unavailable`` (503) · ``unauthorized`` · ``network`` ·
``error``.

Place this file at: m2c/src/m2c/tui/grant.py
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..auth import AuthNetworkError, load_credential
from ..auth.login import get_access_token
from ..config import ServerUrlError, normalize_server_url
from .safe import clean_line
from .status import format_credits

__all__ = ["GrantResult", "describe", "request_grant"]

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WORD = re.compile(r"^[a-z_]{1,32}$")
_TIME = re.compile(r"^[0-9T:\-+.Z ]{10,40}$")
_TIMEOUT_S = 15.0
_MAX_BODY = 16_384


@dataclass(frozen=True)
class GrantResult:
    kind: str
    state: Optional[str] = None
    ceiling: Optional[float] = None
    spent: Optional[float] = None
    granted: Optional[float] = None
    reset_at: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.kind == "granted"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else None


def _word(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _WORD.match(value) else None


def _parse_error(status: int, body: Any) -> GrantResult:
    detail = body.get("detail") if isinstance(body, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    code = _word(detail.get("code"))
    if code == "quota_exhausted":
        reset = detail.get("reset_at")
        return GrantResult("pool_exhausted", reset_at=reset if isinstance(reset, str) and _TIME.match(reset) else None)
    if code == "device_offline":
        return GrantResult("device_offline")
    if code == "not_paused_budget":
        return GrantResult("not_paused", state=_word(detail.get("state")))
    if code == "buffer_gone" or status == 404:
        return GrantResult("run_gone")
    if code == "rate_limited" or status == 429:
        return GrantResult("rate_limited")
    if status in (401, 403):
        return GrantResult("unauthorized")
    if status == 503 or code in ("grant_unavailable", "usage_unavailable"):
        return GrantResult("unavailable")
    return GrantResult("error")


def _post(run_id: str, base: str, token: str, timeout: float) -> GrantResult:
    request = urllib.request.Request(
        f"{base}/v1/code/runs/{run_id}/grant", data=b"", method="POST",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Length": "0"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(_MAX_BODY)
    except (urllib.error.URLError, OSError, ValueError):
        return GrantResult("network")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        body = {}
    if status != 200:
        return _parse_error(status, body)
    if not isinstance(body, dict):
        return GrantResult("error")
    return GrantResult("granted", state=_word(body.get("state")), ceiling=_num(body.get("run_credit_ceiling")),
                       spent=_num(body.get("run_credits_spent")), granted=_num(body.get("granted_credits")))


def _blocking(run_id: str, token_getter: Callable[[], Any], server_url: Optional[str],
              timeout: float) -> GrantResult:
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        return GrantResult("error")
    try:
        base = normalize_server_url(server_url if server_url is not None else load_credential().server_url)
        token = token_getter()
        value = token.reveal() if hasattr(token, "reveal") else token
    except AuthNetworkError:
        return GrantResult("network")
    except ServerUrlError:
        return GrantResult("error")
    except Exception as exc:  # AuthError family and credential-store problems: all mean "cannot authenticate"
        return GrantResult("unauthorized" if type(exc).__module__.startswith("m2c.auth") else "error")
    if not isinstance(value, str) or not value:
        return GrantResult("error")
    return _post(run_id, base, value, timeout)


async def request_grant(run_id: str, *, token_getter: Callable[[], Any] = get_access_token,
                        server_url: Optional[str] = None, timeout: float = _TIMEOUT_S) -> GrantResult:
    """Ask the server for more credits for ``run_id``.  Never raises for an expected outcome."""
    return await asyncio.to_thread(_blocking, run_id, token_getter, server_url, timeout)


def describe(result: GrantResult) -> str:
    """One fixed sentence for the person (credits only)."""
    k = result.kind
    if k == "granted":
        parts = ["More credits were granted; the run continues."]
        if result.spent is not None and result.ceiling is not None:
            parts.append(f"This run has used {format_credits(result.spent)} of {format_credits(result.ceiling)} credits.")
        return " ".join(parts)
    if k == "pool_exhausted":
        when = f" It resets {clean_line(result.reset_at, 40)}." if result.reset_at else ""
        return "Your credit pool is used up, so no more credits can be granted." + when
    if k == "device_offline":
        return "The server says this device is offline. Wait for the connection to come back, then press g again."
    if k == "not_paused":
        return "The run is not waiting for credits any more."
    if k == "run_gone":
        return "The server no longer has this run. Type a message to start a new one."
    if k == "rate_limited":
        return "Too many requests for credits. Wait a moment and try again."
    if k == "unavailable":
        return "The server could not process the request right now. Try again in a moment."
    if k == "unauthorized":
        return "The server did not accept this device's login. Run `m2c login` again."
    if k == "network":
        return "Could not reach the server. Check the connection and press g again."
    return "The request for more credits failed."
