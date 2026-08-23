"""
tests/manual/test_openrouter.py — OR-4 (reliability_overhaul_plan.md).

Replaces tests/manual/test_cerebras.py: Cerebras is fully retired from
this codebase (see .env.example, utils/llm_client.py's provider getter
dispatch, and every agent CHAIN under agents/), not kept in parallel, so
this is a straight swap of the file, not an addition alongside a
still-live Cerebras test.

Hits the real OpenRouter API with an OPENROUTER_API_KEY_1..9-shaped env
var; not run in CI (see tests/manual/conftest.py — this directory's
fake_bus override is a no-op specifically so tests here keep talking to
real infra).

OpenRouter is OpenAI-SDK-compatible (same base_url trick
utils/llm_client.py's _get_openrouter() uses for
_get_mistral()/_get_gemini()/_get_huggingface()) — no dedicated SDK,
just the openai package pointed at OPENROUTER_BASE_URL.

Run from backend/, with your venv active and .env already loaded:

    python -m pytest tests/manual/test_openrouter.py -v
"""
import os

import pytest
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

pytestmark = pytest.mark.manual

# Matches utils/llm_client.py's OPENROUTER_BASE_URL exactly.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Any one of the OPENROUTER_API_KEY_1..9 pool works for a manual smoke
# test — defaults to _1 since that's always provisioned first, but can be
# overridden (e.g. `OPENROUTER_TEST_KEY_ENV=OPENROUTER_API_KEY_9 pytest ...`)
# to spot-check a specific key in the pool instead.
_KEY_ENV = os.getenv("OPENROUTER_TEST_KEY_ENV", "OPENROUTER_API_KEY_1")


@pytest.mark.skipif(not os.getenv(_KEY_ENV), reason=f"{_KEY_ENV} not set")
def test_openrouter_chat_completion():
    api_key = os.getenv(_KEY_ENV)
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    response = client.chat.completions.create(
        model="openrouter/free",
        messages=[{"role": "user", "content": "Say hello in exactly 5 words."}],
    )
    content = response.choices[0].message.content
    assert content, "expected non-empty response content"
    print("Response:", content)


@pytest.mark.skipif(not os.getenv(_KEY_ENV), reason=f"{_KEY_ENV} not set")
def test_openrouter_never_sends_ratelimit_headers():
    """OR-1's live header check, kept as a regression guard: confirms the
    finding utils/llm_client.py's OPENROUTER_BASE_URL comment and
    rate_ledger.py's _gating_mode_for() docstring both depend on --
    OpenRouter doesn't send x-ratelimit-* headers on chat completions.
    If this ever starts failing, that finding is stale and rate_ledger's
    request-count-fallback gating for this provider needs re-checking
    against real token-based headers instead."""
    api_key = os.getenv(_KEY_ENV)
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    raw_response = client.chat.completions.with_raw_response.create(
        model="openrouter/free",
        messages=[{"role": "user", "content": "Say hello in exactly 5 words."}],
    )
    headers = getattr(raw_response, "headers", None) or {}
    ratelimit_headers = [h for h in headers.keys() if "ratelimit" in h.lower()]
    assert not ratelimit_headers, (
        f"OpenRouter sent unexpected rate-limit headers: {ratelimit_headers} -- "
        f"OR-1's finding (never sends x-ratelimit-*) may be stale; re-check "
        f"rate_ledger.py's request-count-fallback gating for this provider."
    )
