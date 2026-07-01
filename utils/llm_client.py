"""
utils/llm_client.py — replaces utils/gemini_client.py entirely.

Gemini and OpenRouter are not used anywhere in this system (per the v5
Master Blueprint correction). This module provides one generic
generate_text() function that any agent can call with its own ordered
fallback chain, drawn from: Groq, Cerebras, GitHub Models.

Each agent defines its own chain as a list of steps:

    CHAIN = [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
        {"provider": "cerebras", "model": "llama-3.3-70b", "key_env": "CEREBRAS_API_KEY_9"},
        {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
    ]

generate_text() walks the chain in order, moving to the next step only on
a rate-limit / server / transient error. A prompt or parsing error does NOT
fall through to the next provider — that would just mean the next provider
fails the same way, and it masks real bugs.
"""

import os

from groq import Groq, RateLimitError as GroqRateLimitError, APIStatusError as GroqAPIStatusError
from cerebras.cloud.sdk import Cerebras
from openai import OpenAI, RateLimitError as OpenAIRateLimitError, APIStatusError as OpenAIAPIStatusError

# GitHub Models' OpenAI-compatible inference endpoint.
# Verify this is still current if calls start failing with 404 --
# GitHub has changed this endpoint before.
GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"

_TRANSIENT_ERRORS = (
    GroqRateLimitError, GroqAPIStatusError,
    OpenAIRateLimitError, OpenAIAPIStatusError,
)

_client_cache = {}


def _get_groq(key_env: str) -> Groq:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("groq", key_env)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = Groq(api_key=key)
    return _client_cache[cache_key]


def _get_cerebras(key_env: str) -> Cerebras:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("cerebras", key_env)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = Cerebras(api_key=key)
    return _client_cache[cache_key]


def _get_github(key_env: str) -> OpenAI:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("github", key_env)
    if cache_key not in _client_cache:
        _client_cache[cache_key] = OpenAI(base_url=GITHUB_MODELS_BASE_URL, api_key=key)
    return _client_cache[cache_key]


_PROVIDER_GETTERS = {
    "groq": _get_groq,
    "cerebras": _get_cerebras,
    "github": _get_github,
}


def _call_step(client, model: str, system_prompt: str, user_content: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def generate_text(system_prompt: str, user_content: str, chain: list, agent_name: str = "Agent") -> str:
    """
    Walks `chain` in order. Each step is a dict:
        {"provider": "groq"|"cerebras"|"github", "model": "...", "key_env": "..."}

    Moves to the next step only on a transient provider error (rate limit,
    5xx, timeout). Raises immediately on anything else (bad prompt, auth
    error unrelated to rate limiting, etc.) so real bugs don't get masked
    as "well, try the next provider."

    Raises RuntimeError if every step in the chain is exhausted or unusable
    (e.g. missing API key).
    """
    last_exc = None

    for i, step in enumerate(chain):
        provider = step["provider"]
        model = step["model"]
        key_env = step["key_env"]

        getter = _PROVIDER_GETTERS.get(provider)
        if getter is None:
            raise ValueError(f"[{agent_name}] Unknown provider '{provider}' in chain.")

        client = getter(key_env)
        if client is None:
            print(f"  [{agent_name}] {provider}:{model} skipped — {key_env} not set.")
            continue

        label = f"{provider}:{model}"
        try:
            return _call_step(client, model, system_prompt, user_content)
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            is_last = i == len(chain) - 1
            if is_last:
                break
            print(f"  [{agent_name}] {label} failed ({exc.__class__.__name__}), "
                  f"falling back to next in chain...")

    raise RuntimeError(
        f"[{agent_name}] All providers in fallback chain exhausted or unavailable. "
        f"Last error: {last_exc}"
    )