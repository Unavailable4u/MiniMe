"""
utils/llm_errors.py -- Reliability & Smart Rate-Limiting Overhaul, Phase 1
(error taxonomy). Foundation module: every later phase (the rate ledger,
proactive gating, bounded concurrency, quota-aware chain construction,
graceful degradation, scoped chunking, observability, the final
exhaustive-wait tier) dispatches on the ErrorBucket this module returns,
so this is the one place that decides what kind of failure just happened.

This replaces llm_client.py's current single-signal
`_is_request_too_large_error()` (status_code == 413 or "request too
large" in the message) branching. That check conflated two genuinely
different failures: a request that is actually too large for a model's
context window, and Groq's rolling per-minute TPM quota (also surfaced
as a 413) being temporarily exhausted for the organization. The log that
motivated this plan showed a 229-character prompt shrunk twice and still
failing -- proof the old check was misclassifying an org-scoped,
time-windowed quota problem as a per-request size problem. classify_error()
below reads the provider's own wording first, and only falls back to a
bare status-code guess when the body doesn't say.

Recovery table (the contract every downstream phase must respect):

    CONTEXT_LENGTH_EXCEEDED -> trim/chunk *this* request only (scoped
        chunking, Phase 7). Never retry unchanged on the next provider --
        an unchanged oversized request fails identically everywhere.
    RATE_LIMIT_WINDOW -> check the rate ledger (Phase 2) for another
        provider/org with headroom; if none, wait for the shortest known
        reset and retry the SAME, unmodified request. Never shrink the
        prompt -- the prompt was never the problem.
    MALFORMED_REQUEST -> log loudly, do not retry unchanged, surface to
        the caller as a real bug. Never retry silently.
    PERMANENT_AUTH -> long/indefinite cooldown on that key, alert, pull
        it from rotation until manually fixed. Never retry at all.
    TRANSIENT_NETWORK -> standard short jittered backoff, retry the same
        request. Never treat as a size or auth problem.
"""

import re
from enum import Enum


class ErrorBucket(Enum):
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"  # genuine per-request size ceiling
    RATE_LIMIT_WINDOW = "rate_limit_window"               # TPM/RPM, time-windowed, org-scoped
    MALFORMED_REQUEST = "malformed_request"                # our payload is actually wrong
    PERMANENT_AUTH = "permanent_auth"                      # bad/revoked key, no amount of retrying helps
    TRANSIENT_NETWORK = "transient_network"                # timeout, 5xx, connection reset


# Bug fix (2026-09-01): a 429 whose body reports the quota limit itself
# as zero (e.g. Google's `'quota_limit_value': '0'`) is not a rolling
# window that will ever open on its own -- it means this key/project/
# region combination has NO allocation at all for the call type in
# question, a project/key configuration problem, not transient
# contention. The RATE_LIMIT_WINDOW recovery action (wait for the
# window to reset, retry the same request) can never succeed here: zero
# stays zero regardless of how long anything waits. Checked BEFORE
# _RATE_WINDOW_PHRASES below, since the same body also contains
# ordinary rate-window wording ("requests per minute") that would
# otherwise shadow this more specific and more actionable signal.
_ZERO_QUOTA_PATTERN = re.compile(r"quota_limit_value[\"']?\s*:\s*[\"']?0\b")


def _is_permanent_zero_quota(body: str) -> bool:
    """True when the provider's own error body states the applicable
    quota limit is zero -- see _ZERO_QUOTA_PATTERN's docstring above.
    `body` is expected already-lowercased text, same convention as
    every other phrase check in this module; the pattern itself has no
    letters that case-fold, so this is safe either way."""
    return bool(_ZERO_QUOTA_PATTERN.search(body))


# Wording providers actually use for a rolling per-minute/per-hour quota,
# as opposed to a genuine per-request size ceiling. Checked first,
# regardless of status code, because the same 413 status is reused by
# Groq for both meanings -- the body text is the only reliable signal.
_RATE_WINDOW_PHRASES = (
    "tokens per minute",
    "tpm",
    "requests per minute",
    "rpm",
    "tokens per day",
    "tpd",
    "requests per day",
    "rpd",
    "rate limit",
)

# Wording for a genuine, absolute per-request/context-window ceiling.
# Only trusted when none of _RATE_WINDOW_PHRASES also matched (see rule
# 1 in classify_error's docstring below) -- some providers mention both
# "context length" and a per-minute window in the same 413 body when the
# request would have overflowed either way, and the window is the more
# actionable classification in that overlap case (retry-elsewhere beats
# a same-provider trim that might not even be necessary once the window
# clears).
_CONTEXT_LENGTH_PHRASES = (
    "maximum context length",
    "context_length_exceeded",
    "context length exceeded",
    "too many tokens for model",
    "too many tokens for this model",
)

_MALFORMED_REQUEST_STATUS_CODES = {400}
_PERMANENT_AUTH_STATUS_CODES = {401, 403}
_TRANSIENT_NETWORK_STATUS_CODES = {408, 500, 502, 503, 504}


def _status_code_from_exc(exc, response=None) -> int | None:
    """Best-effort extraction of the HTTP status code an SDK exception
    (or an explicitly-passed response object) carries. Mirrors
    llm_client._status_code_from_exc()'s already-confirmed extraction
    order for Groq/Cerebras/OpenAI's APIStatusError subclasses --
    exc.status_code first, exc.response.status_code as fallback -- with
    an explicitly-passed `response` checked first of all, since a caller
    that already has the raw response object (e.g. a Cloudflare REST
    call, which raises a plain exception with no status code of its
    own) has more direct evidence than anything attached to exc."""
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            return status_code
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return status_code
    exc_response = getattr(exc, "response", None)
    if exc_response is not None:
        return getattr(exc_response, "status_code", None)
    return None


def _body_text(exc, response=None) -> str:
    """Best-effort extraction of the provider's own error body text, so
    the phrase checks below see the actual wording ("tokens per minute",
    "maximum context length", etc.) and not just str(exc), which for
    some SDK exception types is a generic wrapper message that doesn't
    include the body at all. Falls back to str(exc) when no richer body
    is available -- still correct for providers (Groq included) whose
    SDK exceptions DO put the raw body straight into the message."""
    parts = [str(exc)]
    if response is not None:
        for attr in ("text", "content", "body"):
            value = getattr(response, attr, None)
            if value:
                parts.append(str(value))
    exc_response = getattr(exc, "response", None)
    if exc_response is not None:
        for attr in ("text", "content", "body"):
            value = getattr(exc_response, attr, None)
            if value:
                parts.append(str(value))
    body = getattr(exc, "body", None)
    if body:
        parts.append(str(body))
    return " ".join(parts).lower()


def classify_error(exc, response=None) -> ErrorBucket:
    """
    Turns any provider exception into exactly one of five buckets, each
    with a single well-defined recovery action (see the recovery table
    in the module docstring). Classification order matters -- most
    specific signal first:

    1. Provider error body wording, checked before any status code:
       - a rate-window phrase ("tokens per minute", "TPM", "requests
         per minute", "RPM", etc.) present anywhere in the body ->
         RATE_LIMIT_WINDOW, unconditionally. This check runs first and
         wins over a context-length phrase appearing in the same body,
         because a window-scoped failure is what a bare per-request
         ceiling claim usually turns out to be in practice (see the
         module docstring's 229-character example) -- treating the
         window signal as authoritative when both are present is the
         conservative, empirically-correct choice.
       - otherwise, a context-length phrase ("maximum context length",
         "context_length_exceeded", "too many tokens for model") ->
         CONTEXT_LENGTH_EXCEEDED.
    2. HTTP status code fallback, only when body wording was ambiguous
       (neither phrase set matched):
       - 429 -> RATE_LIMIT_WINDOW always. A 429 is definitionally a rate
         limit, never a size problem, regardless of body wording.
       - 413 -> RATE_LIMIT_WINDOW by default. A bare per-request cap
         firing with no window language in the body at all is far more
         likely to be an unlabeled rate window than a genuine size
         problem -- this is exactly the SGA 229-character case in the
         log this plan is built on. (Pre-flight token-count-aware
         disambiguation -- treating an unexplained 413 as
         CONTEXT_LENGTH_EXCEEDED only when the original request's own
         estimated token count was already close to the model's known
         context window -- is Phase 2's job, once a pre-flight estimator
         exists; this module has no such estimate to consult yet, so it
         intentionally defaults toward the more common case instead of
         guessing blind.)
       - 400 -> MALFORMED_REQUEST.
       - 401 / 403 -> PERMANENT_AUTH.
       - 408 / 5xx -> TRANSIENT_NETWORK.
       - anything else (no recognized status code, e.g. a bare
         connection error or timeout with no HTTP response at all) ->
         TRANSIENT_NETWORK, the safest default: worth a short backoff
         retry, and never mistaken for a size/auth/malformed-payload
         problem it isn't.
    """
    body = _body_text(exc, response=response)

    # Bug fix (2026-09-01): checked before the rate-window phrases below
    # on purpose -- a zero-quota body also contains ordinary "requests
    # per minute" wording, which would otherwise misclassify this as an
    # ordinary, waitable RATE_LIMIT_WINDOW. See _is_permanent_zero_quota's
    # docstring: zero quota never opens on its own, no matter how long a
    # caller waits, so this needs PERMANENT_AUTH's "don't retry, cool
    # down, alert" recovery action instead.
    if _is_permanent_zero_quota(body):
        return ErrorBucket.PERMANENT_AUTH

    if any(phrase in body for phrase in _RATE_WINDOW_PHRASES):
        return ErrorBucket.RATE_LIMIT_WINDOW
    if any(phrase in body for phrase in _CONTEXT_LENGTH_PHRASES):
        return ErrorBucket.CONTEXT_LENGTH_EXCEEDED

    status_code = _status_code_from_exc(exc, response=response)

    if status_code == 429:
        return ErrorBucket.RATE_LIMIT_WINDOW
    if status_code == 413:
        # No window OR context-length wording found in the body -- see
        # rule 2 above. Default to RATE_LIMIT_WINDOW, not
        # CONTEXT_LENGTH_EXCEEDED: an unexplained 413 is far more often
        # an unlabeled org-scoped quota window than a genuine size
        # ceiling on a small request.
        return ErrorBucket.RATE_LIMIT_WINDOW
    if status_code in _MALFORMED_REQUEST_STATUS_CODES:
        return ErrorBucket.MALFORMED_REQUEST
    if status_code in _PERMANENT_AUTH_STATUS_CODES:
        return ErrorBucket.PERMANENT_AUTH
    if status_code in _TRANSIENT_NETWORK_STATUS_CODES:
        return ErrorBucket.TRANSIENT_NETWORK

    # No recognized status code at all -- bare connection error, timeout,
    # or an SDK exception type this function doesn't special-case. Treat
    # as transient: short backoff and retry is always a safe default,
    # and this is never mistaken for a bucket whose recovery action
    # (trimming, long cooldown, silent-retry ban) would be wrong here.
    return ErrorBucket.TRANSIENT_NETWORK
