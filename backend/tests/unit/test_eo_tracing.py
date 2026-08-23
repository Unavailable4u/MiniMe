"""
tests/unit/test_eo_tracing.py — Patch 7e-2.

eo/tracing.py had zero test coverage before this. Two things worth
pinning down: truncate_for_trace()'s "never raises" contract (the
module's own docstring is explicit about this being load-bearing --
callers in utils/llm_client.py and eo/executor.py rely on it not
needing their own try/except), and TRACING_ENABLED's dependency on
os.environ being read AFTER load_dotenv() runs, not against whatever
the process environment happened to look like at interpreter start.

Isolation for TRACING_ENABLED: it's a module-level constant computed
once at import time from os.getenv(), so testing its behavior under
different env-var combinations means reloading the module with a
patched environment via importlib.reload(). Each such test reloads
the module again at the end (env cleared) to avoid leaking a
non-default TRACING_ENABLED value into whatever test runs next in the
same pytest process -- same class of cross-test state leak
tests/conftest.py's own _reset_role_prompts_cache/_reset_app_slug_context
fixtures exist to close for other modules.

get_tracer() is a thin pass-through to langfuse.get_client() (the SDK
provides its own no-op fallback per the module's docstring) -- mocked
here rather than exercised against the real SDK, since verifying "did
we call the one function this delegates to" is the only thing this
module is actually responsible for getting right.
"""
import importlib

import eo.tracing as tracing


# ---------------------------------------------------------------------
# truncate_for_trace
# ---------------------------------------------------------------------

def test_truncate_for_trace_returns_short_text_unchanged():
    assert tracing.truncate_for_trace("hello world") == "hello world"


def test_truncate_for_trace_returns_none_for_none_input():
    assert tracing.truncate_for_trace(None) is None


def test_truncate_for_trace_coerces_non_str_input_via_str():
    assert tracing.truncate_for_trace(12345, limit=10) == "12345"


def test_truncate_for_trace_truncates_text_over_the_limit_with_omitted_count():
    text = "a" * 50
    result = tracing.truncate_for_trace(text, limit=10)
    assert result.startswith("a" * 10)
    assert "truncated" in result
    assert "40 more chars" in result


def test_truncate_for_trace_uses_module_default_limit_when_not_specified():
    text = "a" * (tracing.TRACE_TEXT_CHAR_LIMIT + 100)
    result = tracing.truncate_for_trace(text)
    assert result.startswith("a" * tracing.TRACE_TEXT_CHAR_LIMIT)
    assert "100 more chars" in result


def test_truncate_for_trace_text_exactly_at_limit_is_not_truncated():
    text = "a" * 10
    assert tracing.truncate_for_trace(text, limit=10) == text


def test_truncate_for_trace_never_raises_even_on_a_broken_limit():
    """The module's own docstring: "Never raises -- a tracing-side
    truncation failure must not look like a real error to the
    caller." A non-comparable `limit` (e.g. None, where `len(s) <=
    limit` raises TypeError) must fall through to the except-block
    and return the original text, not propagate."""
    text = "some text"
    assert tracing.truncate_for_trace(text, limit=None) == text


# ---------------------------------------------------------------------
# get_tracer
# ---------------------------------------------------------------------

def test_get_tracer_delegates_to_langfuse_get_client(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(tracing, "get_client", lambda: sentinel)
    assert tracing.get_tracer() is sentinel


# ---------------------------------------------------------------------
# TRACING_ENABLED — computed at import time from LANGFUSE_* env vars
# ---------------------------------------------------------------------

def _reload_tracing_with_env(monkeypatch, public_key, secret_key):
    if public_key is None:
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    else:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", public_key)
    if secret_key is None:
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", secret_key)
    return importlib.reload(tracing)


def test_tracing_enabled_true_when_both_keys_are_set(monkeypatch):
    try:
        reloaded = _reload_tracing_with_env(monkeypatch, "pub_123", "secret_456")
        assert reloaded.TRACING_ENABLED is True
    finally:
        # Leave no non-default state behind for whatever test runs next.
        _reload_tracing_with_env(monkeypatch, None, None)


def test_tracing_enabled_false_when_only_public_key_is_set(monkeypatch):
    try:
        reloaded = _reload_tracing_with_env(monkeypatch, "pub_123", None)
        assert reloaded.TRACING_ENABLED is False
    finally:
        _reload_tracing_with_env(monkeypatch, None, None)


def test_tracing_enabled_false_when_only_secret_key_is_set(monkeypatch):
    try:
        reloaded = _reload_tracing_with_env(monkeypatch, None, "secret_456")
        assert reloaded.TRACING_ENABLED is False
    finally:
        _reload_tracing_with_env(monkeypatch, None, None)


def test_tracing_enabled_false_when_neither_key_is_set(monkeypatch):
    reloaded = _reload_tracing_with_env(monkeypatch, None, None)
    assert reloaded.TRACING_ENABLED is False
