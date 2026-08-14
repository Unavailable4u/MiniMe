"""
eo/tracing.py

D1 — Langfuse client init.

The langfuse v4 SDK (pinned in requirements.txt) is safe to call even when
no credentials are configured: get_client() internally falls back to a
NoOpTracer whenever LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are unset, so
every call site that uses get_tracer() below can do so unconditionally —
same "blank env var = disabled, nothing else has to branch on it"
convention as SENTRY_DSN in api/server.py. There is no custom null-object
wrapper here because the SDK already provides one.

TRACING_ENABLED is exposed only for call sites that would otherwise do
non-trivial work to build a payload (e.g. serializing a large prompt/
response pair) before handing it to what might be a no-op — those can
check this flag first and skip the work entirely. Nothing needs it for
correctness, only to avoid wasted work when tracing is off.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if python-dotenv isn't installed; LANGFUSE_* can come from real env vars instead

from langfuse import get_client

# load_dotenv() above must run before this is computed -- api/server.py also
# calls load_dotenv(), but only once the app boots. Anything that imports
# this module first (a bare `python -c "..."` repro, a standalone script, a
# test) would otherwise read os.getenv() against the real OS environment,
# find nothing, and silently compute TRACING_ENABLED = False even with a
# real .env sitting right there. Same gap eo/db.py already closes for
# DATABASE_URL, closed here the same way.
TRACING_ENABLED = bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(
    os.getenv("LANGFUSE_SECRET_KEY")
)

# D1 audit fix -- span input/output text was previously attached to
# Langfuse spans with no cap at all. Full prompts/completions/task_text
# on every generation + session + role + member span, all queued for
# the same batch export, routinely produced payloads that didn't fit
# in the SDK's default 5s HTTP export timeout (see LANGFUSE_TIMEOUT
# below). This is a hard ceiling on any single text value handed to
# start_as_current_observation(input=...) / span.update(output=...)
# -- shared by utils/llm_client.py's generation spans and this
# module's callers in eo/executor.py's session/role/member spans, so
# both are capped the same way instead of drifting independently.
# Kept well under Pusher's ~10KB per-event limit (see
# eo/executor.py's _summarize(), limit=9000) since a single span
# attribute has no business being anywhere near that anyway -- this
# is about total *batch* size across dozens of concurrent spans, not
# just one.
TRACE_TEXT_CHAR_LIMIT = 4000


def truncate_for_trace(text, limit: int = TRACE_TEXT_CHAR_LIMIT) -> str:
    """Best-effort cap on text attached to a Langfuse span attribute.
    Never raises -- a tracing-side truncation failure must not look
    like a real error to the caller, same defensive posture as every
    other function in this module and in utils/llm_client.py's
    _traced_generation()/_end_traced_generation(). Non-str input is
    coerced via str() so callers don't need their own isinstance
    check before calling this."""
    try:
        if text is None:
            return text
        s = text if isinstance(text, str) else str(text)
        if len(s) <= limit:
            return s
        omitted = len(s) - limit
        return f"{s[:limit]}... [truncated, {omitted} more chars]"
    except Exception:
        return text


def get_tracer():
    """
    Returns the shared Langfuse client singleton (langfuse.get_client()
    caches this internally, so repeated calls are cheap).

    Safe to call whether or not LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are
    set. With no keys configured, every method on the returned object
    (start_as_current_observation, update, flush, etc.) is a no-op instead
    of raising — callers in utils/llm_client.py and eo/executor.py don't
    need their own try/except purely to guard against "tracing is off";
    that guard is still there for actual failures (network errors talking
    to Langfuse, etc.), same defensive posture as eo/db.py's _log_usage().
    """
    return get_client()
