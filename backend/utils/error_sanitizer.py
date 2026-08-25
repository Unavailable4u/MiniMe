"""
utils/error_sanitizer.py -- Reliability & Smart Rate-Limiting Overhaul,
Patch I.1 (REVISED Phase I).

Problem this closes: raw provider exceptions (Groq/Cerebras/OpenAI
`APIStatusError` bodies, Cloudflare REST error text, etc.) were being
f-string-interpolated straight into `emit_event("error", ...)` payloads
and API response `message` fields -- both of which are read directly by
the frontend chat bubble / API caller. Those bodies can carry things
like the provider org ID, a billing/console URL, or a verbatim
`Error code: ...` blob that has no business leaving the server.

This module is the one place that turns "some exception" into "a short,
safe, user-facing string." It never changes what gets logged -- callers
keep doing `traceback.print_exc()` / Sentry breadcrumbs / etc. exactly
as before; this only changes what goes out over the wire.

Classification reuses `llm_errors.classify_error()` when the exception
looks like an LLM-provider error (i.e. `classify_error` doesn't have to
guess blind -- it already knows how to read a provider body), so a
RATE_LIMIT_WINDOW failure reads differently to the user than a
PERMANENT_AUTH one. Anything that isn't recognizable as a provider error
at all (a bare KeyError from application code, for instance) falls back
to one generic message -- we don't try to classify non-provider
exceptions, we just decline to repeat their text.
"""
from utils.llm_errors import ErrorBucket, classify_error

# Per-bucket copy. Deliberately generic: no model names, no provider
# names, no status codes, no billing/console URLs, no org identifiers.
_BUCKET_MESSAGES = {
    ErrorBucket.CONTEXT_LENGTH_EXCEEDED:
        "That request was too large to process. Try shortening it or "
        "splitting it into smaller pieces.",
    ErrorBucket.RATE_LIMIT_WINDOW:
        "This step hit a temporary usage limit. Please try again in a "
        "moment.",
    ErrorBucket.MALFORMED_REQUEST:
        "Something went wrong preparing that request. Our team has been "
        "notified.",
    ErrorBucket.PERMANENT_AUTH:
        "This step is temporarily unavailable. Our team has been "
        "notified.",
    ErrorBucket.TRANSIENT_NETWORK:
        "A temporary connection issue interrupted this step. Please try "
        "again.",
}

# Fallback for exceptions that don't look like a provider error at all
# (e.g. ChainExhaustedError itself, or an unrelated application bug) --
# classify_error() would happily force these into TRANSIENT_NETWORK via
# its own catch-all, which is the right *retry* signal internally but a
# misleading thing to tell the user, so this path is kept distinct from
# the bucket table above rather than routed through it.
_GENERIC_MESSAGE = "Something went wrong completing this step. Please try again."

# Exception types that are already known to be internal/orchestration
# failures rather than a raw provider error -- these skip
# classify_error() entirely and always get the generic message, since
# classify_error() has no real signal to work with for them (no
# status_code, no provider body) and would otherwise fall through to its
# TRANSIENT_NETWORK default, which is accurate for retry logic but not
# for what the user should be told.
_NON_PROVIDER_EXCEPTION_NAMES = frozenset({
    "ChainExhaustedError",
    "MissingDependencyError",
})


def user_facing_message(exc: BaseException) -> str:
    """Return a short, safe, generic string describing `exc`, suitable
    for a chat bubble or an API response body. Never includes the
    provider's own error text, a status code, a URL, or an org/account
    identifier -- only the bucket-level category is reflected.

    Full-detail logging (traceback.print_exc(), Sentry, etc.) is
    unaffected by this function and must keep happening exactly as
    before at each call site; this function only decides what the
    *user* sees.
    """
    if exc.__class__.__name__ in _NON_PROVIDER_EXCEPTION_NAMES:
        return _GENERIC_MESSAGE

    try:
        bucket = classify_error(exc)
    except Exception:
        # classify_error() is expected to be pure/side-effect-free, but
        # a sanitizer must never itself become the thing that crashes an
        # error-handling path -- fail safe to the generic message.
        return _GENERIC_MESSAGE

    return _BUCKET_MESSAGES.get(bucket, _GENERIC_MESSAGE)
