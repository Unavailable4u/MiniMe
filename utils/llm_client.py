"""
utils/llm_client.py — replaces utils/gemini_client.py entirely.

OpenRouter is not used anywhere in this system (per the v5 Master
Blueprint correction). GitHub Models was used here too until its full
retirement on 2026-07-30 (quota-reality fix, §4) -- every chain that
stepped through it has since been moved onto a different provider or had
that step removed outright; nothing in this module routes to it anymore.
This module provides one generic generate_text() function that any agent
can call with its own ordered fallback chain, drawn from: Groq, Cerebras,
Mistral, Gemini, HuggingFace, Cloudflare Workers AI.

Each agent defines its own chain as a list of steps. Most providers
(groq, cerebras, mistral, gemini, huggingface) are OpenAI-SDK-shaped and
use "key_env":

    CHAIN = [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
        {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_9"},
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

Notebooks Chat-First refinement, Phase 2 step 2.1 findings -- which
provider(s) support OpenAI-style tool calling ("tools"/"tool_choice" in
the request, "tool_calls" in the response), checked against the actual
models each chain is pinned to today, not just the provider in the
abstract:

  - Groq: yes, full tools/tool_choice/tool_calls, including parallel
    tool calls. llama-3.3-70b-versatile (the model most chains use)
    supports it.
  - Cerebras: yes. gpt-oss-120b (the model most chains use) supports
    tools, but REJECTS a request that sets both "tools" and
    "response_format" in the same call -- a step that wants both will
    have to drop one. (Unrelated to tool calling, but noted while
    checking this model: qwen-3-32b and llama-3.3-70b are being
    deprecated on Cerebras -- not a concern today since no chain here
    uses either, just don't reach for them later without checking.)
  - Mistral: yes. mistral-medium-latest (the model most chains use) is
    on Mistral's own confirmed tool-calling model list.
  - Gemini: yes, natively documented on the OpenAI-compat endpoint
    _get_gemini() already points at. gemini-3.6-flash/
    gemini-3.1-flash-lite (the chain models) are current-gen.
  - HuggingFace router: yes, but support is per-model/per-backend, not
    router-wide -- openai/gpt-oss-120b:fastest (the model
    generic_worker.py actually uses) is HF's own documented example
    model for tool calling, so it's a safe bet; don't assume it holds
    for every other router model without checking.
  - Cloudflare Workers AI: yes -- llama-3.3-70b-instruct-fp8-fast (the
    chain model) is tagged "Function calling" in Cloudflare's own model
    docs, with a "tools" field in the same
    {type: "function", function: {name, description, parameters}}
    shape the other five use.

Two gaps this leaves for Phase 2 step 2.2 (converting the capability
manifest into a tools array) and beyond, not yet addressed by anything
in this module:
  1. _call_step() (groq/cerebras/mistral/gemini/huggingface) doesn't
     pass "tools" into client.chat.completions.create(...) today, and
     only ever reads choice.message.content -- it has no path back for
     choice.message.tool_calls.
  2. _call_cloudflare_step() is a bigger gap: it's a raw REST call, so
     it needs an actual "tools" key built into its `payload` dict (not
     just threaded through, like the SDK-shaped path above), and a
     return path for a tool-call response -- right now it only ever
     reads result.get("response", "") as plain text and has no
     tool_calls handling at all.
"""

import json
import os
import re
from datetime import date, datetime, timezone

import requests
from groq import Groq, RateLimitError as GroqRateLimitError, APIStatusError as GroqAPIStatusError
from cerebras.cloud.sdk import Cerebras
from openai import OpenAI, RateLimitError as OpenAIRateLimitError, APIStatusError as OpenAIAPIStatusError

from memory.bus import read as bus_read, write as bus_write
from relay.emitter import emit_event

# Quota-reality fix, §1 — replaces the old flat per-provider dict. Three
# separate bugs that one flat number hid:
#   1a. get_quota_snapshot() was comparing TOKEN usage against a number
#       documented (and used below) as a REQUEST-per-day ceiling.
#   1b. One number per provider can't represent reality -- every model
#       has its own RPM/RPD/TPM/TPD, and this repo mixes several models
#       per provider.
#   1c. The old numbers (14400 for groq/cerebras) were never right for
#       the models actually in use -- llama-3.3-70b-versatile's real RPD
#       is 1,000, not 14,400 (that figure belongs to
#       llama-3.1-8b-instant, which nothing here calls).
# Per-model, per-provider now. get_quota_snapshot() resolves which model
# a given key_id actually used today (from the usage record itself, or a
# fallback) and looks up QUOTA_CONFIG[provider][model] -- see
# eo/quota_sentinel.py.
QUOTA_CONFIG = {
    "groq": {
        # confirmed against the account's Free Plan Limits page, 2026-07-30
        "llama-3.3-70b-versatile": {"rpm": 30, "rpd": 1000,  "tpm": 12000, "tpd": 100000},
        "llama-3.1-8b-instant":    {"rpm": 30, "rpd": 14400, "tpm": 6000,  "tpd": 500000},
        "qwen/qwen3.6-27b":        {"rpm": 30, "rpd": 1000,  "tpm": 8000,  "tpd": 200000},
        "openai/gpt-oss-120b":     {"rpm": 30, "rpd": 1000,  "tpm": 8000,  "tpd": 200000},
        # "qwen/qwen3-32b" deliberately absent -- not in the current live
        # model list at all (see the reality guide §3). Don't add a number
        # for a model that may already be 404ing.
    },
    "cerebras": {
        # confirmed against cloud.cerebras.ai/.../models, 2026-07-30
        "gpt-oss-120b": {"rpm": 5, "rpd": 2400, "tpm": 30000, "tpd": 1000000},
        "gemma-4-31b":  {"rpm": 5, "rpd": 2400, "tpm": 30000, "tpd": 1000000},
    },
    "gemini": {
        # confirmed against Google AI Studio > Rate Limit page, 2026-07-30.
        # gemini-2.5-pro / gemini-3.1-pro deliberately absent -- 0/0 on the
        # free tier (paid-only). Never wire either into a free-tier key's
        # chain; it will fail every time, not just on exhaustion.
        "gemini-3.6-flash":      {"rpm": 5,  "rpd": 20,    "tpm": 250000},
        "gemini-3.1-flash-lite": {"rpm": 15, "rpd": 500,   "tpm": 250000},
        "gemini-3.5-flash-lite": {"rpm": 15, "rpd": 500,   "tpm": 250000},
        "gemma-4-31b":           {"rpm": 30, "rpd": 14400, "tpm": 16000},
        # NOTE: same model family as Cerebras' gemma-4-31b, different
        # provider/base_url -- verify the exact model id string Gemini's
        # OpenAI-compat layer expects before wiring it into a CHAIN.
    },
    "mistral": {
        # confirmed against admin.mistral.ai > Limits, 2026-07-30. Mistral
        # publishes RPS/TPM, not RPD -- no daily figure to put here
        # honestly, so "rpd" is deliberately absent for both entries
        # below; get_quota_snapshot() treats a missing "rpd" as "no
        # verified daily number" the same way a missing provider does.
        "mistral-large-latest":  {"rps": 0.07, "tpm": 250000},   # ~ mistral-large-2512
        "mistral-medium-latest": {"rps": 0.83, "tpm": 25000},
    },
    "cloudflare": {
        # confirmed via dash.cloudflare.com > AI > Workers AI > Usage,
        # 2026-07-30: "Daily usage (resets at 00:00 UTC) - Neurons used
        # today: 0/10k". Ceiling only -- unit is neurons, not
        # requests/tokens like every other entry above, so
        # get_quota_snapshot() has a dedicated neurons-aware branch for
        # this provider rather than reusing the requests-based math.
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast": {"neurons_rpd": 10000},
        # NOTE: reviewer.py's cloudflare step actually calls
        # "@cf/meta/llama-3.1-8b-instruct" -- a different model with no
        # entry here yet. Flagging, not fixing as part of this patch.
    },
    # "github" -- retiring/retired, see the reality guide §4. Left out of
    # this rewrite deliberately rather than given a fresh per-model
    # number now.
    # "huggingface" (chat, via the router) -- still not a request-count
    # product. It's a monthly CREDIT pool ($0.10/month free tier), so a
    # "rpd" style number here would be a unit fabrication no matter what
    # went in it. See the reality guide §5.
}

# Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
# GITHUB_MODELS_BASE_URL and _get_github() below are removed as part of
# this same pass, now that no CHAIN anywhere in the repo still steps
# through "github".

# Mistral La Plateforme is also OpenAI-SDK-compatible (same trick as
# GitHub Models above) -- added so documentation_agent.py / final_qa.py
# can route through generate_text() instead of hand-rolling their own
# OpenAI client, which is the only way their calls get usage-logged.
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"

# Gemini's OpenAI-compatibility layer -- same trick again. Confirmed
# current against Google's own OpenAI-compatibility docs at wiring time;
# Google has iterated on this path before, so re-verify against
# https://ai.google.dev/gemini-api/docs/openai if calls start 404ing.
# Trailing slash matters -- Google's own examples include it.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Hugging Face's Inference Providers router -- also OpenAI-SDK-compatible,
# but unlike Gemini/Mistral/GitHub its `model` string isn't just a bare
# model id: HF fans one id out across multiple backend providers
# (Cerebras, Together, Fireworks, etc.), so you address a specific one
# with "<repo_id>:<provider>" (e.g. "openai/gpt-oss-120b:cerebras"), or
# let HF pick for you with the "<repo_id>:fastest"/":cheapest"/":auto"
# suffix. A bare repo id with no suffix works but is under-specified --
# always use a suffix in this codebase's CHAINs so the choice is explicit
# and reproducible, not whatever HF's default happened to be that day.
# GET https://router.huggingface.co/v1/models (Bearer $HUGGINGFACE_API_KEY)
# lists what's actually live right now -- re-check there if a model:provider
# pair below starts 404ing, HF's provider roster shifts over time.
HF_ROUTER_BASE_URL = "https://router.huggingface.co/v1"

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

# Fix B (reliability guide, §3 "Fix B"): quota tracking (QUOTA_CONFIG /
# eo/quota_sentinel.py) only ever answered "how many tokens has this
# account used TODAY" -- it had no concept of a provider's own
# retry-after signal (e.g. Groq's 429 body: "Please try again in
# 8m5.568s"), so a short-lived per-minute/per-hour cooldown looked
# identical to "out of tokens until midnight" and benched the account
# for the rest of the day instead of ~8 minutes. The functions below
# extract that signal and write it to the bus as
# cooldown_until:{provider}:{key_id} -- eo/panel.py's _best_match()
# reads it back to skip an account only until its OWN stated window
# clears, not until end of day.
_RETRY_AFTER_TEXT_PATTERN = re.compile(
    r"try again in (?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?P<seconds>[\d.]+)s",
    re.IGNORECASE,
)

# Conservative guess used only when a transient error carries NEITHER a
# Retry-After header NOR Groq-style "try again in ...s" text -- e.g. a
# bare 5xx or a connection timeout with no timing signal at all. Better
# than not recording a cooldown at all (which would let the very next
# call immediately retry the same still-failing account), but this is a
# guess, not a provider-stated number -- keep it short.
_DEFAULT_COOLDOWN_SECONDS = 60.0


def _seconds_from_retry_after_text(message: str):
    """Parses a Groq-style "Please try again in 8m5.568s." (or "5.568s",
    or "1h2m3s") out of an error message's own text. Returns None if the
    pattern isn't present. This is the fallback path -- Groq's API
    doesn't send a Retry-After header, only this phrasing inside the
    message, so string-parsing is the only way to recover the real
    number for this specific provider."""
    if not message:
        return None
    match = _RETRY_AFTER_TEXT_PATTERN.search(message)
    if not match:
        return None
    seconds = float(match.group("seconds"))
    if match.group("minutes"):
        seconds += int(match.group("minutes")) * 60
    if match.group("hours"):
        seconds += int(match.group("hours")) * 3600
    return seconds


def _retry_after_seconds(exc) -> float:
    """Best-effort: how many seconds until it's worth retrying THIS
    account, from whichever signal the failed call actually gave us.
    Tries, in order:
      1. A real `Retry-After` response header -- standard for 429s, and
         what the Groq/OpenAI SDKs expose via exc.response.headers when
         the provider sends one.
      2. Groq's own "try again in 8m5.568s" phrasing inside the
         exception's message text (see module note above).
      3. _DEFAULT_COOLDOWN_SECONDS, if neither signal is present.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            header_value = headers.get("retry-after") or headers.get("Retry-After")
            if header_value:
                try:
                    return float(header_value)
                except (TypeError, ValueError):
                    pass  # some providers send an HTTP-date instead of seconds -- fall through
    parsed = _seconds_from_retry_after_text(str(exc))
    if parsed is not None:
        return parsed
    return _DEFAULT_COOLDOWN_SECONDS


def _set_cooldown(provider: str, key_id: str, exc) -> None:
    """Writes cooldown_until:{provider}:{key_id} = a UTC unix timestamp
    to the bus after a transient failure, so eo/panel.py's _best_match()
    can skip this specific account until that timestamp passes instead
    of only checking daily token usage (Fix B). Deliberately mirrors
    log_usage()'s own "never raises" contract below -- a cooldown-write
    failure should never take down the actual generate_text() call that
    triggered it, it just means this account isn't skipped early next
    time, same as if quota tracking itself had failed to log.
    """
    try:
        cooldown_until = datetime.now(timezone.utc).timestamp() + _retry_after_seconds(exc)
        bus_write(f"cooldown_until:{provider}:{key_id}", cooldown_until)
    except Exception as write_exc:
        print(f"  [llm_client] cooldown write failed (non-fatal): {write_exc}")


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


def _get_gemini(key_env: str, timeout: float = None) -> OpenAI:
    """Same OpenAI-SDK-via-base_url trick as _get_mistral() above --
    Gemini's OpenAI-compatibility layer takes a normal OpenAI
    client pointed at GEMINI_BASE_URL, so no new SDK dependency and no new
    branch in _call_step() (it's already provider-agnostic OpenAI-shaped)."""
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("gemini", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"base_url": GEMINI_BASE_URL, "api_key": key}
        if timeout is not None:
            kwargs["timeout"] = timeout
        _client_cache[cache_key] = OpenAI(**kwargs)
    return _client_cache[cache_key]


def _get_huggingface(key_env: str, timeout: float = None) -> OpenAI:
    """Same trick again, pointed at HF's Inference Providers router. Note
    this is a genuinely different HF product surface than
    utils/embedding.py's embed_text() (which hits HF's older
    hf-inference/feature-extraction endpoint directly, not this router) --
    same HUGGINGFACE_API_KEY* token works for both, since it's just a
    Bearer credential on the account, but don't conflate the two call
    paths when debugging a failure in one vs. the other."""
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("huggingface", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"base_url": HF_ROUTER_BASE_URL, "api_key": key}
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
    """OpenAI-SDK-shaped call, used for groq/cerebras/mistral/gemini. Returns
    (text, usage, finish_reason) — usage is the provider SDK's usage
    object (has .total_tokens on all three, since they're all
    OpenAI-compatible chat.completions responses) or None if the
    response didn't include one for some reason.

    Fix C (reliability guide, §3 "Fix C", truncation handoff):
    finish_reason is the third element of the tuple now — it's
    "length" when the provider stopped because it hit max_tokens
    (real, partial output exists and is worth keeping) versus "stop"
    for a normal completion. All three OpenAI-compatible providers
    here expose this on response.choices[0].finish_reason. Callers
    that don't care can just ignore the third value."""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    usage = getattr(response, "usage", None)
    finish_reason = getattr(choice, "finish_reason", None)
    return text, usage, finish_reason


# --------------------------------------------------------------------------
# Phase 2 step 2.5 — real (non-test-harness) tool-calling classification.
#
# scripts/test_tool_calling.py (steps 2.3/2.4) proved out a system prompt +
# tools array against Groq's llama-3.3-70b-versatile specifically, and
# talks to Groq directly via the `openai` SDK, deliberately bypassing this
# module's whole fallback chain (see that script's own header comment).
#
# generate_text()/_call_step()/_call_cloudflare_step() above still don't
# accept a `tools` kwarg at all -- that's the two gaps flagged in this
# module's own docstring (step 2.1 findings), and wiring `tools` through
# the *entire* multi-provider chain (including Cloudflare's raw REST
# shape) is real, separate work, not required by step 2.5's actual scope:
# "add the classification call before sendTask() -- but only log the
# result for now, don't branch." So this function deliberately mirrors
# the test harness's choice rather than solving those two gaps: single
# provider (Groq, the one step 2.1/2.4 actually validated), no fallback
# chain, no retries beyond what's needed to not crash on a flaky call.
#
# This function is written to NEVER raise. A classification-only,
# log-only feature must not be able to break (or even slow down) the
# real send path if GROQ_API_KEY is unset, Groq is down, or the model
# emits something unparseable -- any of those just means "no
# classification this turn," not a failed message send. Callers get back
# a dict with an "error" key instead of an exception.
# --------------------------------------------------------------------------

CLASSIFY_INTENT_MODEL = "llama-3.3-70b-versatile"
CLASSIFY_INTENT_KEY_ENV = "GROQ_API_KEY"  # shared default key, same as most chains above

CLASSIFY_INTENT_SYSTEM_PROMPT = (
    "You are the assistant for a study workspace app. You have tools "
    "that generate study materials from the sources currently in the "
    "user's workspace.\n\n"
    "Only call a tool when the user is clearly asking for one of these "
    "specific study materials to be generated. If the request doesn't "
    "match any tool -- including requests for things that sound similar "
    "but aren't offered, small talk, or anything unrelated to the "
    "workspace -- do NOT call a tool. Just reply normally in plain "
    "text: say what you can help with instead, or ask a clarifying "
    "question.\n\n"
    "Call at most one tool per turn. If a request could reasonably map "
    "to more than one tool, don't call any of them -- ask the user "
    "which one they want instead."
)


def classify_tool_intent(message: str, tools: list) -> dict:
    """
    Sends `message` (a single chat turn) plus `tools` (built by
    utils.capability_tools.manifest_to_tools() from the Phase 1
    capability manifest) to Groq with tool_choice="auto", and returns a
    normalized classification result:

        {"tool_calls": [{"name": str, "arguments": dict}, ...],
         "ambiguous": bool,   # >1 simultaneous tool call -- ignore names, treat as no match
         "content": str | None,   # the model's plain-text reply, if it didn't call a tool
         "error": str | None}     # set (and the other fields empty/None) on any failure

    Nothing here calls generateNotebooks(...) or any other side-effecting
    endpoint -- step 2.5 is log-only by design. Branching on this result
    (the "high-confidence tool call" dispatch) is step 2.6.

    If `tools` is empty (e.g. every capability happens to be disabled),
    returns immediately without making a call -- there's nothing for the
    model to classify against.
    """
    if not tools:
        return {"tool_calls": [], "ambiguous": False, "content": None, "error": "no tools available"}

    client = _get_groq(CLASSIFY_INTENT_KEY_ENV)
    if client is None:
        return {"tool_calls": [], "ambiguous": False, "content": None,
                 "error": f"{CLASSIFY_INTENT_KEY_ENV} not set"}

    try:
        response = client.chat.completions.create(
            model=CLASSIFY_INTENT_MODEL,
            messages=[
                {"role": "system", "content": CLASSIFY_INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            tools=tools,
            tool_choice="auto",
        )
    except Exception as exc:  # fail open, see header comment -- any failure just means "no classification this turn"
        return {"tool_calls": [], "ambiguous": False, "content": None, "error": str(exc)}

    choice = response.choices[0].message
    raw_tool_calls = choice.tool_calls or []

    if not raw_tool_calls:
        return {"tool_calls": [], "ambiguous": False, "content": (choice.content or None), "error": None}

    if len(raw_tool_calls) > 1:
        # Multiple simultaneous calls = low-confidence classification.
        # Step 2.6's dispatch should treat this the same as "no tool
        # call" and fall through to sendTask(), not execute all of them.
        return {"tool_calls": [], "ambiguous": True, "content": None, "error": None}

    call = raw_tool_calls[0]
    raw_args = call.function.arguments
    try:
        # Empty-properties schemas ("whole" scope) can come back as the
        # literal string "null" rather than "{}" (the known null-args
        # quirk from step 2.4's findings) -- normalize so callers get a
        # plain dict either way.
        args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
        if args is None:
            args = {}
    except json.JSONDecodeError:
        args = {"_unparsed": raw_args}

    return {
        "tool_calls": [{"name": call.function.name, "arguments": args}],
        "ambiguous": False,
        "content": None,
        "error": None,
    }


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

    result = data.get("result", {}) or {}
    text = (result.get("response", "") or "")
    usage = result.get("usage")  # often absent -- see module docstring
    # Fix C: Cloudflare Workers AI's REST response doesn't expose a
    # finish_reason field the way the OpenAI-compatible providers do, so
    # this is almost always None. Kept as a real lookup (not a hardcoded
    # None) in case a given model/account does surface it, but don't
    # rely on Cloudflare ever reporting "length" -- truncation handoff
    # (below) simply won't trigger for a Cloudflare step in practice,
    # same honest-scoping caveat as the module docstring's usage-object
    # caveat above.
    finish_reason = result.get("finish_reason")
    return text.strip(), usage, finish_reason


def log_usage(provider: str, key_id: str, tokens, session_id: str = None, tier=None,
              path: str = None, agent_name: str = "Agent", domain: str = None,
              model: str = None) -> None:
    """Public usage logger -- increments today's usage:{provider}:{key_id}:{date}
    entry in Upstash and fires a usage_update event. Never raises.

    Quota-reality fix, §1: `model` (optional) is stored on the daily
    record as `record["model"]` -- the actual model string this call
    used, so get_quota_snapshot() can look up QUOTA_CONFIG[provider][model]
    instead of guessing which model an account is on. Overwrites the
    previous value on each call (a key_id can call more than one model in
    a day -- e.g. performance_reviewer.py's two-model chain on one
    Gemini key -- so this reflects the most recent call, not a running
    history; see eo/quota_sentinel.py's _model_for() for the fallback
    used before any call has landed today).

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
    object to extract a token count from at all.

    Migration Part 27 §1: `path` (str) added alongside the original
    `tier` (int) param, rather than replacing it. These are genuinely two
    different callers, not one migrated name: eo/executor.py's
    boundary-A agents (code_writers, reviewer, fixer_pool,
    security_scanner, the *_lean trio) were migrated to the string
    `path` label ("instant"/"direct"/"fixed"/"adaptive") and are the ones
    that were crashing here with `path=` unexpected-keyword errors.
    dependency_mapper/documentation_agent/duplication_checker/
    memory_search's own `tier` (int) parameter was deliberately NOT part
    of that migration (see eo/executor.py's UNSCOPED_TIER_AGENTS comment)
    -- they still call this with `tier=`, and that keeps working
    unchanged. Both are accepted; the usage_update payload below includes
    whichever one the caller actually passed.

    Migration Part 2 §2.6 -- the one real cost-tracking gap the upgrade
    plan flagged: "per project or per section" breakdown. Two additions,
    both purely additive, neither changes the existing
    usage:{provider}:{key_id}:{date} key or its write path above:

    - `domain` (optional, e.g. "coding"/"simulate" -- eo/executor.py's
      dispatch already has this as decision.get("domain")) is written to
      a second key, usage_by_domain:{domain}:{date}, when given.
    - workspace_id is NOT a parameter here -- it's derived automatically
      from session_id via eo.chat_workspace.workspace_for_chat(), so no
      existing call site (there are ~20 of them across agents/*.py) needs
      to be touched to pass it explicitly. Written to
      usage_by_workspace:{workspace_id}:{date} when session_id resolves
      to a workspace.

    Both secondary writes are best-effort and silently skipped (not
    logged as an error) when domain/workspace_id isn't available for
    this call -- a call with neither still logs its request/token count
    to the account-level key exactly as before, it's simply not
    attributable to a project/section breakdown. See
    eo.quota_sentinel.get_usage_history_scoped() for the read side."""
    try:
        today = date.today().isoformat()
        db_key = f"usage:{provider}:{key_id}:{today}"
        current = bus_read(db_key, default={"requests": 0, "tokens": 0})
        current["requests"] = current.get("requests", 0) + 1
        if tokens is not None:
            current["tokens"] = current.get("tokens", 0) + tokens
        if model is not None:
            current["model"] = model
        bus_write(db_key, current)

        workspace_id = None
        if session_id:
            try:
                from eo.chat_workspace import workspace_for_chat
                ws = workspace_for_chat(session_id)
                workspace_id = ws["id"] if ws else None
            except Exception:
                workspace_id = None  # non-fatal: chat_workspace unavailable or session unresolved

        if domain:
            dom_key = f"usage_by_domain:{domain}:{today}"
            dom_current = bus_read(dom_key, default={"requests": 0, "tokens": 0})
            dom_current["requests"] = dom_current.get("requests", 0) + 1
            if tokens is not None:
                dom_current["tokens"] = dom_current.get("tokens", 0) + tokens
            bus_write(dom_key, dom_current)

        if workspace_id:
            ws_key = f"usage_by_workspace:{workspace_id}:{today}"
            ws_current = bus_read(ws_key, default={"requests": 0, "tokens": 0})
            ws_current["requests"] = ws_current.get("requests", 0) + 1
            if tokens is not None:
                ws_current["tokens"] = ws_current.get("tokens", 0) + tokens
            bus_write(ws_key, ws_current)

        emit_event(
            "usage_update",
            session_id=session_id,
            agent=agent_name,
            payload={
                "provider": provider,
                "key_id": key_id,
                "tokens_used_today": current["tokens"],
                # FIX (quota-reality, §1): QUOTA_CONFIG is now per-model, not
                # a flat per-provider number -- QUOTA_CONFIG.get(provider)
                # alone would return a dict here, not an int, and silently
                # break every consumer doing arithmetic on it (see
                # TokenUsageTab.jsx's live-session panel). Resolve via the
                # model this call actually used; None when it isn't known
                # or that model has no verified daily figure (e.g. mistral).
                # NOTE: this field is still compared against TOKEN usage in
                # TokenUsageTab.jsx's live-session panel (SessionContext.jsx/
                # the bottom "keys" section) -- the same tokens-vs-requests
                # unit mismatch §1a fixed for get_quota_snapshot() exists
                # here too, just not fixed as part of this patch. Flagging
                # for a follow-up rather than expanding this one.
                "daily_limit": QUOTA_CONFIG.get(provider, {}).get(model, {}).get("rpd") if model else None,
                "requests_used_today": current["requests"],
                "tier": tier,
                "path": path,
                "domain": domain,
                "workspace_id": workspace_id,
            },
        )
    except Exception as exc:
        print(f"  [{agent_name}] usage logging failed (non-fatal): {exc}")


def _log_usage(provider: str, key_id: str, usage, session_id: str, tier, path, agent_name: str,
               domain: str = None, model: str = None) -> None:
    """Internal adapter used by generate_text()'s chat-completion call
    sites: extracts a token count out of whatever usage shape the
    provider returned (SDK object with .total_tokens, or a plain dict
    with "total_tokens"), then delegates to the public log_usage() above.

    usage may be an SDK object (groq/cerebras/mistral -- has
    .total_tokens as an attribute) or a plain dict (cloudflare, when
    present at all -- has "total_tokens" as a key), or None entirely.
    Any of these still result in the request being logged now -- only the
    token count is best-effort.

    model (quota-reality fix, §1): the model string this specific chain
    step called -- generate_text() already has it in scope as `model` at
    both call sites, just forwarded through here and into log_usage()."""
    tokens = None
    if usage is not None:
        tokens = getattr(usage, "total_tokens", None)
        if tokens is None and isinstance(usage, dict):
            tokens = usage.get("total_tokens")
    log_usage(provider, key_id, tokens, session_id=session_id, tier=tier, path=path,
              agent_name=agent_name, domain=domain, model=model)


_MAX_CONTINUATIONS = 2  # Fix C: cap how many times one call will chase a
# "length" cutoff before just returning what it has. Bounded independently
# of chain length: a chain can be up to MAX_CHAIN_STEPS (3) long for
# *account* fallback reasons alone, so without this cap a pathologically
# short max_tokens setting could burn the whole chain on continuations
# and leave nothing for a genuine 429/5xx to fall back to.


def _continuation_prompt(original_user_content: str, partial_text: str) -> str:
    """Fix C (reliability guide, §3 "Fix C", item 1 -- truncation
    handoff): builds the next step's prompt when the previous step's
    response was cut off by hitting max_tokens (finish_reason ==
    "length"), not by an error. The partial text is real, already-paid-
    for output -- this hands it to the next provider in the chain and
    asks it to continue exactly where generation stopped, instead of
    discarding it and starting over from scratch."""
    return (
        f"{original_user_content}\n\n"
        "--- Partial answer already generated (cut off mid-generation, "
        "not by you) ---\n"
        f"{partial_text}\n\n"
        "Continue exactly from where the partial answer above left off. "
        "Do not repeat, rephrase, or restart any part of it, and do not "
        "add any preamble, header, or acknowledgement -- just continue "
        "the text seamlessly as if it were never interrupted."
    )


def generate_text(system_prompt: str, user_content: str, chain: list, agent_name: str = "Agent",
                   session_id: str = None, tier: int = None, path: str = None,
                   domain: str = None) -> str:
    """
    Walks `chain` in order. Each step is a dict. For groq/cerebras/mistral/gemini:
        {"provider": "groq"|"cerebras"|"mistral"|"gemini", "model": "...", "key_env": "..."}
    For cloudflare:
        {"provider": "cloudflare", "model": "...", "account_id_env": "...", "token_env": "..."}

    Moves to the next step only on a transient provider error (rate limit,
    5xx, timeout). Raises immediately on anything else (bad prompt, auth
    error unrelated to rate limiting, etc.) so real bugs don't get masked
    as "well, try the next provider."

    session_id/tier/path (Stage 6, Part 6.7; Part 27 §1): if given, logs
    this call's token usage to Upstash and fires a usage_update event so
    a connected frontend can render the quota dashboard live. Leaving
    session_id unset keeps this function's return value and behavior
    identical to before Stage 6 step 6 -- emit_event's own no-op-on-None
    handles the rest, same pattern as executor.py's session_id plumbing.

    `tier` (int, 0-3) and `path` (str, "instant"/"direct"/"fixed"/
    "adaptive") are two distinct labels, not two names for the same
    thing -- see this function's own module docstring update / Part 27
    §1's audit. Pass whichever one your call site actually has; both are
    forwarded to log_usage() and included in the usage_update payload.
    Passing `path=` here used to raise TypeError (reviewer.py/
    fixer_pool.py were already calling it this way) -- that's the bug
    this parameter addition fixes.

    `domain` (Part 2 §2.6, optional): the classification domain this call
    belongs to (e.g. "coding", "simulate" -- eo/executor.py's dispatch
    already has this as decision.get("domain")). Purely forwarded to
    log_usage() for the per-project/per-section usage breakdown; omitting
    it costs nothing and changes no other behavior. workspace_id is never
    a parameter here -- log_usage() derives it from session_id on its
    own, so existing call sites that already pass session_id don't need
    any change to get workspace-level attribution; only call sites that
    want domain-level attribution too need to add `domain=...`.

    Fix C (reliability guide, §3 "Fix C", item 1 -- truncation handoff):
    if a step's response comes back with finish_reason == "length" (the
    provider stopped because it hit max_tokens, not because it errored),
    that partial text is real output, not a failure. Rather than either
    returning a silently-truncated answer or discarding it and retrying
    from scratch, this walks to the next step in the chain with a
    continuation prompt (see _continuation_prompt() above) built from
    the partial text, and stitches the pieces together. This is capped
    at _MAX_CONTINUATIONS additional hops so a short max_tokens setting
    can't eat the whole fallback chain that Fix A built for account/
    provider failures. If the chain runs out (or the cap is hit) while
    still truncated, the accumulated partial text is returned as-is --
    still strictly better than the pre-Fix-C behavior, which threw it
    away on any subsequent failure.

    Raises RuntimeError if every step in the chain is exhausted or unusable
    (e.g. missing API key/credentials) AND no partial output was ever
    produced. If at least one step produced partial (truncated) output
    before the chain ran out, that partial text is returned instead of
    raising -- see Fix C note above.
    """
    last_exc = None
    accumulated_text = ""   # Fix C: partial output carried across a
    # "length" truncation handoff. Empty string means "nothing generated
    # yet" -- distinct from a step that legitimately returns "".
    continuations_used = 0

    for i, step in enumerate(chain):
        provider = step["provider"]
        model = step["model"]
        prompt_for_step = (
            _continuation_prompt(user_content, accumulated_text)
            if accumulated_text else user_content
        )

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
                text, usage, finish_reason = _call_cloudflare_step(
                    creds, model, system_prompt, prompt_for_step, json_mode=json_mode)
                _log_usage(provider, key_id, usage, session_id, tier, path, agent_name, domain=domain, model=model)
                full_text = accumulated_text + text
                is_last = i == len(chain) - 1
                if (finish_reason == "length" and continuations_used < _MAX_CONTINUATIONS
                        and not is_last):
                    # Fix C: real partial output, hand it to the next step.
                    accumulated_text = full_text
                    continuations_used += 1
                    print(f"  [{agent_name}] {label} truncated (finish_reason=length), "
                          f"continuing on next chain step...")
                    continue
                return full_text
            except _TRANSIENT_ERRORS as exc:
                last_exc = exc
                _set_cooldown(provider, key_id, exc)   # Fix B
                is_last = i == len(chain) - 1
                if is_last:
                    break
                print(f"  [{agent_name}] {label} failed ({exc.__class__.__name__}), "
                      f"falling back to next in chain...")
            continue

        key_env = step["key_env"]
        timeout = step.get("timeout")
        getter = {
            "groq": _get_groq, "cerebras": _get_cerebras,
            "mistral": _get_mistral, "gemini": _get_gemini,
            "huggingface": _get_huggingface,
        }.get(provider)
        if getter is None:
            raise ValueError(f"[{agent_name}] Unknown provider '{provider}' in chain.")

        client = getter(key_env, timeout)
        if client is None:
            print(f"  [{agent_name}] {provider}:{model} skipped — {key_env} not set.")
            continue

        label = f"{provider}:{model}"
        try:
            text, usage, finish_reason = _call_step(client, model, system_prompt, prompt_for_step)
            _log_usage(provider, key_env, usage, session_id, tier, path, agent_name, domain=domain, model=model)
            full_text = accumulated_text + text
            is_last = i == len(chain) - 1
            if (finish_reason == "length" and continuations_used < _MAX_CONTINUATIONS
                    and not is_last):
                # Fix C: real partial output, hand it to the next step
                # instead of returning a silently-truncated answer or
                # discarding it on a later failure.
                accumulated_text = full_text
                continuations_used += 1
                print(f"  [{agent_name}] {label} truncated (finish_reason=length), "
                      f"continuing on next chain step...")
                continue
            return full_text
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            _set_cooldown(provider, key_env, exc)   # Fix B
            is_last = i == len(chain) - 1
            if is_last:
                break
            print(f"  [{agent_name}] {label} failed ({exc.__class__.__name__}), "
                  f"falling back to next in chain...")

    if accumulated_text:
        # Fix C: the chain ran out (or hit _MAX_CONTINUATIONS) while a
        # prior step's output was still truncated -- that text was
        # genuinely generated and paid for, so hand it back instead of
        # raising and losing it. It may still be an incomplete answer;
        # callers that care can inspect for an unnatural cutoff the same
        # way they'd notice any other truncated response.
        print(f"  [{agent_name}] chain exhausted while still truncated -- "
              f"returning {len(accumulated_text)} chars of partial output "
              f"instead of discarding it.")
        return accumulated_text

    raise RuntimeError(
        f"[{agent_name}] All providers in fallback chain exhausted or unavailable. "
        f"Last error: {last_exc}"
    )

# HuggingFace Inference — sentence embeddings for Upstash Vector (DB4).
# Used by agents/memory_search.py (cyclemem embeddings), eo/semantic_cache.py
# (Part 4 step 4, task-similarity cache), and eo/routing_memory.py
# (routing-outcome retrieval). All three share this one function so
# there's exactly one embedding code path, per the migration guide's own
# instruction not to duplicate it.
#
# Part 26 §4 — this used to be defined here directly, but embed_text()
# only needs os/requests, while this module also imports groq/cerebras/
# openai at load time. eo/routing_memory.py wanted embed_text() without
# that SDK weight, so the function now lives in utils/embedding.py (zero
# heavy imports) and this is just a re-export for existing callers that
# already do `from utils.llm_client import embed_text`.
#
# Patch 7 adds embed_text_with_fallback()/HF_EMBEDDING_KEY_ENVS to that
# same re-export -- agents/memory_search.py and
# agents/duplication_checker.py already import their HF helpers from
# here rather than utils.embedding directly, so this is what makes
# `from utils.llm_client import embed_text_with_fallback` work for them
# without changing their existing import style.
from utils.embedding import (
    embed_text, embed_text_with_fallback, HF_EMBEDDING_KEY_ENVS,
    EMBEDDING_MODEL, HF_FEATURE_EXTRACTION_URL,
)