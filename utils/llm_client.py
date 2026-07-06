"""
utils/llm_client.py — replaces utils/gemini_client.py entirely.

Gemini and OpenRouter are not used anywhere in this system (per the v5
Master Blueprint correction). This module provides one generic
generate_text() function that any agent can call with its own ordered
fallback chain, drawn from: Groq, Cerebras, GitHub Models, Cloudflare
Workers AI.

Each agent defines its own chain as a list of steps. Three providers
(groq, cerebras, github) are OpenAI-SDK-shaped and use "key_env":

    CHAIN = [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
        {"provider": "cerebras", "model": "llama-3.3-70b", "key_env": "CEREBRAS_API_KEY_9"},
        {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
    ]

Cloudflare Workers AI is a plain REST call needing two credentials, so
its step shape is different -- "account_id_env" and "token_env" instead
of a single "key_env":

    CHAIN = [
        {"provider": "cloudflare", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
         "account_id_env": "CLOUDFLARE_ACCOUNT_ID_4", "token_env": "CLOUDFLARE_API_KEY_4"},
    ]

generate_text() walks the chain in order, moving to the next step only on
a rate-limit / server / transient error. A prompt or parsing error does NOT
fall through to the next provider — that would just mean the next provider
fails the same way, and it masks real bugs.

CLOUDFLARE CAVEAT (read before relying on this for usage tracking): as of
this writing, Cloudflare Workers AI's REST response does not reliably
include a token-usage field the way the three OpenAI-compatible providers
do. _call_cloudflare_step() below checks for a "usage" object in the
response and uses it if present, but on many models/accounts it will be
absent -- in that case _log_usage() (Part 6.7) silently logs nothing for
that call, same as it already does for any call with no usage object.
This means a Cloudflare-only chain may show zero token count in the
dashboard even though real calls succeeded. Verify against your actual
account/model before assuming Cloudflare rows in the dashboard are
complete -- request counts may be the more reliable Cloudflare signal
for now, not token counts.
"""

import os
from datetime import date

import requests
from groq import Groq, RateLimitError as GroqRateLimitError, APIStatusError as GroqAPIStatusError
from cerebras.cloud.sdk import Cerebras
from openai import OpenAI, RateLimitError as OpenAIRateLimitError, APIStatusError as OpenAIAPIStatusError

from memory.bus import read as bus_read, write as bus_write
from relay.emitter import emit_event

# Part 6.7 — static known daily free-tier limits, since most providers
# don't expose remaining-quota via API. Verify these against your actual
# account tier; they're a display estimate, not an enforced ceiling.
#
# "cloudflare" deliberately has no entry: Workers AI's free tier is
# measured in "neurons," not tokens, so a token-based daily_limit would
# be a made-up number, not a real estimate. page.js already handles a
# missing daily_limit gracefully (shows raw token count instead of a
# percentage bar), so leaving this out is the honest choice over
# guessing a wrong number.
QUOTA_CONFIG = {
    "groq": 14400,
    "cerebras": 14400,
    "github": 150,  # GitHub Models free tier is much lower than the LLM providers -- verify current published RPD
}
# "mistral" deliberately has no entry, same reasoning as "cloudflare" above:
# no verified published daily request limit at hand for La Plateforme's
# free/trial tier. page.js already handles a missing daily_limit
# gracefully. Fill in once you've confirmed the real number against your
# account rather than guessing.

# GitHub Models' OpenAI-compatible inference endpoint.
# Verify this is still current if calls start failing with 404 --
# GitHub has changed this endpoint before.
GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"

# Mistral La Plateforme is also OpenAI-SDK-compatible (same trick as
# GitHub Models above) -- added so documentation_agent.py / final_qa.py
# can route through generate_text() instead of hand-rolling their own
# OpenAI client, which is the only way their calls get usage-logged.
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"

_TRANSIENT_SDK_ERRORS = (
    GroqRateLimitError, GroqAPIStatusError,
    OpenAIRateLimitError, OpenAIAPIStatusError,
)


class _CloudflareTransientError(Exception):
    """Raised for Cloudflare responses that look retryable (429, 5xx,
    timeout, connection failure) -- kept as its own exception type so it
    can sit in the same _TRANSIENT_ERRORS tuple as the SDK exceptions
    without generate_text() needing to know Cloudflare uses requests
    instead of an SDK under the hood."""
    pass


_TRANSIENT_ERRORS = _TRANSIENT_SDK_ERRORS + (_CloudflareTransientError,)

_client_cache = {}


def _get_groq(key_env: str, timeout: float = None) -> Groq:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("groq", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"api_key": key}
        if timeout is not None:
            kwargs["timeout"] = timeout
        _client_cache[cache_key] = Groq(**kwargs)
    return _client_cache[cache_key]


def _get_cerebras(key_env: str, timeout: float = None) -> Cerebras:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("cerebras", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"api_key": key}
        if timeout is not None:
            kwargs["timeout"] = timeout
        _client_cache[cache_key] = Cerebras(**kwargs)
    return _client_cache[cache_key]


def _get_github(key_env: str, timeout: float = None) -> OpenAI:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("github", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"base_url": GITHUB_MODELS_BASE_URL, "api_key": key}
        if timeout is not None:
            kwargs["timeout"] = timeout
        _client_cache[cache_key] = OpenAI(**kwargs)
    return _client_cache[cache_key]


def _get_mistral(key_env: str, timeout: float = None) -> OpenAI:
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("mistral", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"base_url": MISTRAL_BASE_URL, "api_key": key}
        if timeout is not None:
            kwargs["timeout"] = timeout
        _client_cache[cache_key] = OpenAI(**kwargs)
    return _client_cache[cache_key]


def _get_cloudflare_creds(account_id_env: str, token_env: str):
    """Not a real client object (Cloudflare has no SDK client here, just
    a REST call) -- returns (account_id, token) or None if either is
    missing, so the calling code can skip this step the same way a
    missing key_env skips a step for the other three providers."""
    account_id = os.getenv(account_id_env)
    token = os.getenv(token_env)
    if not account_id or not token:
        return None
    return account_id, token


def _call_step(client, model: str, system_prompt: str, user_content: str):
    """OpenAI-SDK-shaped call, used for groq/cerebras/github. Returns
    (text, usage) — usage is the provider SDK's usage object (has
    .total_tokens on all three, since they're all OpenAI-compatible
    chat.completions responses) or None if the response didn't include
    one for some reason."""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    return text, usage


def _call_cloudflare_step(creds, model: str, system_prompt: str, user_content: str,
                           json_mode: bool = False):
    """Plain REST call — Cloudflare Workers AI has no OpenAI-compatible
    SDK, so this is its own path rather than going through _call_step().
    Returns (text, usage_dict_or_None). See the module docstring's
    CLOUDFLARE CAVEAT: usage is frequently absent from this response.

    json_mode: when True, sends response_format: {"type": "json_object"}
    -- only reliable on models Cloudflare has confirmed for JSON Mode
    (see dependency_mapper.py's docstring for why it opts into this).
    Default False keeps every existing caller (reviewer.py, fixer_pool.py,
    security_scanner.py's Cloudflare fallback steps) byte-for-byte
    unchanged -- they never set this key in their chain, so this param
    stays at its default for them."""
    account_id, token = creds
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429 or (status is not None and 500 <= status < 600):
            raise _CloudflareTransientError(str(exc)) from exc
        raise  # auth errors, 4xx other than 429 -- a real bug, don't mask it
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        raise _CloudflareTransientError(str(exc)) from exc

    data = response.json()
    if not data.get("success", True) and data.get("errors"):
        # Cloudflare can return HTTP 200 with an error payload inside --
        # treat as transient (matches reviewer.py/fixer_pool.py/
        # security_scanner.py's existing hand-rolled behavior for this).
        raise _CloudflareTransientError(f"Cloudflare error: {data['errors']}")

    text = (data.get("result", {}) or {}).get("response", "") or ""
    usage = (data.get("result", {}) or {}).get("usage")  # often absent -- see module docstring
    return text.strip(), usage


def log_usage(provider: str, key_id: str, tokens, session_id: str = None, tier=None,
              agent_name: str = "Agent") -> None:
    """Public usage logger -- increments today's usage:{provider}:{key_id}:{date}
    entry in Upstash and fires a usage_update event. Never raises.

    Unlike the old behavior (see _log_usage below), this ALWAYS logs the
    request when called, even if `tokens` is None -- only the token count
    is skipped in that case. This is what the module's own CLOUDFLARE
    CAVEAT comment already promised ("request counts may be the more
    reliable Cloudflare signal for now") but the previous implementation
    didn't actually deliver: it silently logged nothing at all, not even
    a request count, whenever a usage object was missing.

    Call this directly for any provider call that doesn't go through
    generate_text() -- e.g. duplication_checker.py's / memory_search.py's
    HuggingFace embedding calls, which have no chat-completion "usage"
    object to extract a token count from at all."""
    try:
        today = date.today().isoformat()
        db_key = f"usage:{provider}:{key_id}:{today}"
        current = bus_read(db_key, default={"requests": 0, "tokens": 0})
        current["requests"] = current.get("requests", 0) + 1
        if tokens is not None:
            current["tokens"] = current.get("tokens", 0) + tokens
        bus_write(db_key, current)

        emit_event(
            "usage_update",
            session_id=session_id,
            agent=agent_name,
            tier=tier,
            payload={
                "provider": provider,
                "key_id": key_id,
                "tokens_used_today": current["tokens"],
                "daily_limit": QUOTA_CONFIG.get(provider),
            },
        )
    except Exception as exc:
        print(f"  [{agent_name}] usage logging failed (non-fatal): {exc}")


def _log_usage(provider: str, key_id: str, usage, session_id: str, tier, agent_name: str) -> None:
    """Internal adapter used by generate_text()'s chat-completion call
    sites: extracts a token count out of whatever usage shape the
    provider returned (SDK object with .total_tokens, or a plain dict
    with "total_tokens"), then delegates to the public log_usage() above.

    usage may be an SDK object (groq/cerebras/github/mistral -- has
    .total_tokens as an attribute) or a plain dict (cloudflare, when
    present at all -- has "total_tokens" as a key), or None entirely.
    Any of these still result in the request being logged now -- only the
    token count is best-effort."""
    tokens = None
    if usage is not None:
        tokens = getattr(usage, "total_tokens", None)
        if tokens is None and isinstance(usage, dict):
            tokens = usage.get("total_tokens")
    log_usage(provider, key_id, tokens, session_id=session_id, tier=tier, agent_name=agent_name)


def generate_text(system_prompt: str, user_content: str, chain: list, agent_name: str = "Agent",
                   session_id: str = None, tier: int = None) -> str:
    """
    Walks `chain` in order. Each step is a dict. For groq/cerebras/github:
        {"provider": "groq"|"cerebras"|"github", "model": "...", "key_env": "..."}
    For cloudflare:
        {"provider": "cloudflare", "model": "...", "account_id_env": "...", "token_env": "..."}

    Moves to the next step only on a transient provider error (rate limit,
    5xx, timeout). Raises immediately on anything else (bad prompt, auth
    error unrelated to rate limiting, etc.) so real bugs don't get masked
    as "well, try the next provider."

    session_id/tier (Stage 6, Part 6.7): if given, logs this call's token
    usage to Upstash and fires a usage_update event so a connected
    frontend can render the quota dashboard live. Leaving session_id
    unset keeps this function's return value and behavior identical to
    before Stage 6 step 6 -- emit_event's own no-op-on-None handles the
    rest, same pattern as executor.py's session_id plumbing.

    Raises RuntimeError if every step in the chain is exhausted or unusable
    (e.g. missing API key/credentials).
    """
    last_exc = None

    for i, step in enumerate(chain):
        provider = step["provider"]
        model = step["model"]

        if provider == "cloudflare":
            account_id_env = step["account_id_env"]
            token_env = step["token_env"]
            creds = _get_cloudflare_creds(account_id_env, token_env)
            if creds is None:
                print(f"  [{agent_name}] cloudflare:{model} skipped — "
                      f"{account_id_env}/{token_env} not set.")
                continue
            key_id = account_id_env  # what identifies this "account" in the usage dashboard
            label = f"cloudflare:{model}"
            json_mode = step.get("json_mode", False)
            try:
                text, usage = _call_cloudflare_step(creds, model, system_prompt, user_content,
                                                      json_mode=json_mode)
                _log_usage(provider, key_id, usage, session_id, tier, agent_name)
                return text
            except _TRANSIENT_ERRORS as exc:
                last_exc = exc
                is_last = i == len(chain) - 1
                if is_last:
                    break
                print(f"  [{agent_name}] {label} failed ({exc.__class__.__name__}), "
                      f"falling back to next in chain...")
            continue

        key_env = step["key_env"]
        timeout = step.get("timeout")
        getter = {
            "groq": _get_groq, "cerebras": _get_cerebras, "github": _get_github,
            "mistral": _get_mistral,
        }.get(provider)
        if getter is None:
            raise ValueError(f"[{agent_name}] Unknown provider '{provider}' in chain.")

        client = getter(key_env, timeout)
        if client is None:
            print(f"  [{agent_name}] {provider}:{model} skipped — {key_env} not set.")
            continue

        label = f"{provider}:{model}"
        try:
            text, usage = _call_step(client, model, system_prompt, user_content)
            _log_usage(provider, key_env, usage, session_id, tier, agent_name)
            return text
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

# HuggingFace Inference — sentence embeddings for Upstash Vector (DB4).
# Used by agents/memory_search.py (cyclemem embeddings) and
# eo/semantic_cache.py (Part 4 step 4, task-similarity cache). Both share
# this one function so there's exactly one embedding code path, per the
# migration guide's own instruction not to duplicate it.
#
# Model choice is NOT arbitrary: your actual Upstash Vector index
# (checked via idx.info()) reports dimension=384, similarity_function=
# COSINE. sentence-transformers/all-MiniLM-L6-v2 is the standard model
# for that exact pairing -- if you ever recreate the Vector index with a
# different dimension, this model string must change to match, or every
# upsert/query call will fail with a dimension-mismatch error.
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HF_FEATURE_EXTRACTION_URL = "https://router.huggingface.co/hf-inference/models"

def embed_text(text: str) -> list:
    """Embeds `text` via HuggingFace Inference API, returns a 384-dim
    vector (list[float]) ready for Upstash Vector's upsert()/query().

    Raises RuntimeError if HUGGINGFACE_API_KEY is missing, or the HF
    request fails outright (caller decides how to degrade -- e.g.
    memory_search.py already wraps its embed_text() calls in try/except
    and treats a failure as "no prior context," not a hard error)."""
    api_key = os.getenv("HUGGINGFACE_API_KEY")
    if not api_key:
        raise RuntimeError("HUGGINGFACE_API_KEY not set — required for embed_text().")

    url = f"{HF_FEATURE_EXTRACTION_URL}/{EMBEDDING_MODEL}/pipeline/feature-extraction"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"inputs": text, "options": {"wait_for_model": True}},
        timeout=30,
    )
    response.raise_for_status()
    embedding = response.json()

    # Feature-extraction can return either an already-pooled [dim] vector
    # or an unpooled [seq_len][dim] matrix depending on the model/endpoint
    # version -- mean-pool across tokens if it's the unpooled shape, so
    # callers always get back a flat list[float] regardless of which
    # shape HF happens to serve.
    if embedding and isinstance(embedding[0], list):
        seq_len = len(embedding)
        dim = len(embedding[0])
        embedding = [sum(tok[i] for tok in embedding) / seq_len for i in range(dim)]

    return embedding