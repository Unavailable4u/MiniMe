"""
tests/unit/test_llm_errors.py -- Phase 1 acceptance tests for
utils/llm_errors.classify_error().

Per the plan's Phase 1 acceptance criteria: unit tests feeding it the
exact error bodies from the log (the "Limit 8000, Requested 10331" TPM
message, a plain 429, a 401) must return the expected buckets. Also
covers the other status-code fallback rows in the recovery table and
the body-wording-wins-over-status-code precedence rule, since those are
just as load-bearing a contract for every later phase as the three
headline cases.

No bus/LLM fixtures needed -- classify_error() is pure, so these tests
just build minimal fake exception/response objects.
"""
import pytest

from utils.llm_errors import ErrorBucket, classify_error


class _FakeResponse:
    def __init__(self, status_code=None, text=None):
        self.status_code = status_code
        self.text = text


class _FakeAPIStatusError(Exception):
    """Stands in for Groq/Cerebras/OpenAI's APIStatusError shape:
    status_code available directly on the exception, matching what
    _status_code_from_exc() in both this module and llm_client.py
    already rely on."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------
# The exact cases from the incident log this plan is built on.
# ---------------------------------------------------------------------

def test_groq_tpm_413_is_rate_limit_window_not_context_length():
    # The literal case that motivated this plan: a 229-character prompt,
    # shrunk twice, still failing -- because this was never a size
    # problem. Body wording ("tokens per minute (TPM)" + "Limit 8000,
    # Requested 10331") must win over the bare 413 status code, which
    # the OLD _is_request_too_large_error() check would have (wrongly)
    # treated as "request too large" on its own.
    exc = _FakeAPIStatusError(
        "Rate limit reached for model `openai/gpt-oss-120b` in organization "
        "`org_abc123` on tokens per minute (TPM): Limit 8000, Requested 10331, "
        "please reduce your rate.",
        status_code=413,
    )
    assert classify_error(exc) == ErrorBucket.RATE_LIMIT_WINDOW


def test_plain_429_is_rate_limit_window():
    exc = _FakeAPIStatusError("Too Many Requests", status_code=429)
    assert classify_error(exc) == ErrorBucket.RATE_LIMIT_WINDOW


def test_401_is_permanent_auth():
    exc = _FakeAPIStatusError("Invalid API Key", status_code=401)
    assert classify_error(exc) == ErrorBucket.PERMANENT_AUTH


# ---------------------------------------------------------------------
# Remaining recovery-table rows.
# ---------------------------------------------------------------------

def test_403_is_permanent_auth():
    exc = _FakeAPIStatusError("Forbidden: project suspended", status_code=403)
    assert classify_error(exc) == ErrorBucket.PERMANENT_AUTH


def test_400_is_malformed_request():
    exc = _FakeAPIStatusError("Invalid request: unknown field 'foo'", status_code=400)
    assert classify_error(exc) == ErrorBucket.MALFORMED_REQUEST


@pytest.mark.parametrize("status_code", [408, 500, 502, 503, 504])
def test_5xx_and_408_are_transient_network(status_code):
    exc = _FakeAPIStatusError("Server error", status_code=status_code)
    assert classify_error(exc) == ErrorBucket.TRANSIENT_NETWORK


def test_bare_connection_error_with_no_status_code_is_transient_network():
    # e.g. requests.exceptions.ConnectionError / Timeout, or
    # _CloudflareTransientError -- no status_code, no response, no
    # recognizable phrase in the message at all.
    exc = ConnectionError("Connection reset by peer")
    assert classify_error(exc) == ErrorBucket.TRANSIENT_NETWORK


def test_413_with_context_length_wording_and_no_window_wording_is_context_length():
    exc = _FakeAPIStatusError(
        "This model's maximum context length is 8192 tokens.",
        status_code=413,
    )
    assert classify_error(exc) == ErrorBucket.CONTEXT_LENGTH_EXCEEDED


def test_413_with_no_recognizable_wording_defaults_to_rate_limit_window():
    # An unexplained 413 with neither window nor context-length phrasing
    # -- per the plan, default to RATE_LIMIT_WINDOW rather than guessing
    # CONTEXT_LENGTH_EXCEEDED, since an unlabeled per-request cap on a
    # small prompt is far more likely to be an unlabeled rate window.
    exc = _FakeAPIStatusError("Request too large", status_code=413)
    assert classify_error(exc) == ErrorBucket.RATE_LIMIT_WINDOW


# ---------------------------------------------------------------------
# Body-wording-wins-over-status-code precedence, and reading from an
# explicitly-passed `response` object (e.g. Cloudflare's raw REST path,
# which has no status_code on the exception itself).
# ---------------------------------------------------------------------

def test_rate_window_wording_wins_even_with_context_length_wording_present():
    # Some providers mention both in the same 413 body. Window wording
    # must win -- see classify_error()'s docstring for why.
    exc = _FakeAPIStatusError(
        "maximum context length exceeded; also subject to tokens per minute (TPM) limits",
        status_code=413,
    )
    assert classify_error(exc) == ErrorBucket.RATE_LIMIT_WINDOW


def test_reads_wording_from_explicit_response_object():
    # Cloudflare's REST path (_CloudflareTransientError) carries no
    # status code or body of its own on the exception -- the raw
    # response object is the only place the real signal lives.
    exc = Exception("HTTP 429")
    response = _FakeResponse(status_code=429, text="requests per minute exceeded")
    assert classify_error(exc, response=response) == ErrorBucket.RATE_LIMIT_WINDOW


def test_reads_status_code_from_explicit_response_when_exc_has_none():
    exc = Exception("boom")
    response = _FakeResponse(status_code=401, text="")
    assert classify_error(exc, response=response) == ErrorBucket.PERMANENT_AUTH
