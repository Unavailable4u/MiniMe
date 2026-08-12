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

from langfuse import get_client

TRACING_ENABLED = bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(
    os.getenv("LANGFUSE_SECRET_KEY")
)


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
