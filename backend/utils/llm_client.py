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
(groq, openrouter, mistral, gemini, huggingface) are OpenAI-SDK-shaped
and use "key_env":

    CHAIN = [
        {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
        {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY"},
        {"provider": "openrouter", "model": "openrouter/free", "key_env": "OPENROUTER_API_KEY_9"},
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
    tool calls. openai/gpt-oss-120b and qwen/qwen3.6-27b (the models
    most chains use now that llama-3.3-70b-versatile is decommissioned)
    support it.
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

import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import requests
from groq import Groq, RateLimitError as GroqRateLimitError, APIStatusError as GroqAPIStatusError
from cerebras.cloud.sdk import Cerebras
from cerebras.cloud.sdk import (
    RateLimitError as CerebrasRateLimitError,
    APIStatusError as CerebrasAPIStatusError,
)
from openai import OpenAI, RateLimitError as OpenAIRateLimitError, APIStatusError as OpenAIAPIStatusError

from memory.bus import read as bus_read, write as bus_write
from relay.emitter import emit_event
import logging

from eo.tracing import get_tracer, truncate_for_trace

# Phase 3 (reliability_overhaul_plan.md §PHASE 3) -- rate_ledger.py's
# QUOTA_CONFIG lookup (_tpm_limit_for) imports *this* module lazily,
# inside a function, specifically to avoid a circular import once this
# module imports rate_ledger at load time (see rate_ledger.py's own
# comment on _tpm_limit_for). Safe to import at module level here as
# long as that stays one-directional.
from utils import rate_ledger

# Phase 3d (§PHASE 3, "If NOT OK" / exception-path dispatch) -- the error
# taxonomy Phase 1 built. Replaces the old single _TRANSIENT_ERRORS +
# _is_request_too_large_error() dispatch in generate_text()'s except
# blocks below with the five-bucket recovery table classify_error()'s
# own docstring documents; see llm_errors.py's module docstring for the
# full table this dispatch must respect.
from utils.llm_errors import ErrorBucket, classify_error

# Part 26 §4 re-export: embed_text() used to be defined directly in this
# module, but eo/routing_memory.py wanted it without the heavy groq/
# cerebras/openai SDK imports above, so it now lives in
# utils/embedding.py (zero heavy imports) and this is just a re-export
# so existing callers can keep doing `from utils.llm_client import
# embed_text` / `embed_text_with_fallback` (agents/memory_search.py,
# agents/duplication_checker.py, agents/source_quality_flagger.py,
# eo/knowledge_graph.py, eo/semantic_cache.py). Not used directly in
# this file, hence the noqa.
from utils.embedding import embed_text, embed_text_with_fallback  # noqa: F401

# D1 audit fix -- see eo/executor.py's matching _trace_logger; same
# TRACE_EXPORT_FAILED marker convention so tracing-side failures from
# either module are greppable together.
_trace_logger = logging.getLogger("eo.tracing")

# Quota-reality fix, §1 — replaces the old flat per-provider dict. Three
# separate bugs that one flat number hid:
#   1a. get_quota_snapshot() was comparing TOKEN usage against a number
#       documented (and used below) as a REQUEST-per-day ceiling.
#   1b. One number per provider can't represent reality -- every model
#       has its own RPM/RPD/TPM/TPD, and this repo mixes several models
#       per provider.
#   1c. The old numbers (14400 for groq/cerebras) were never right for
#       the models actually in use -- llama-3.3-70b-versatile (now
#       decommissioned by Groq; replaced by openai/gpt-oss-120b and
#       qwen/qwen3.6-27b below) had a real RPD of 1,000, not 14,400
#       (that figure belongs to llama-3.1-8b-instant, which nothing
#       here calls).
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
    "openrouter": {
        # OR-1b (reliability_overhaul_plan.md). No "tpm"/"tpd" here,
        # deliberately -- OR-1's live header check (test_openrouter.py)
        # confirmed OpenRouter never sends x-ratelimit-* headers on any
        # model, and its own docs describe the free tier as a REQUEST
        # count ceiling (not token-based) in the first place, so a "tpm"
        # entry would be fabricating a number nothing actually enforces.
        # rate_ledger._gating_mode_for() reads the absence of "tpm" here
        # as the signal to gate this provider on request count instead
        # (OR-1d) -- don't add a "tpm" figure later without re-checking
        # that function's docstring, it would silently switch this
        # provider back to token-based gating.
        #
        # rpm=20, rpd=50 confirmed against OpenRouter's published free-tier
        # limits as of 2026-08-23 (per-key, applies at $0 account balance):
        # 20 requests/minute, 50 requests/day. Buying >=$10 of credit once
        # raises the DAILY figure to 1000 (the per-minute 20 rpm cap is
        # fixed regardless of credit) -- if/when this account's balance
        # changes, update "rpd" here to match, or the ledger will start
        # blocking calls the account could actually still make.
        #
        # "openrouter/free" is OpenRouter's own auto-router across
        # whatever free models are currently live (see llm_client.py's
        # OPENROUTER_BASE_URL comment for why CHAINs should route through
        # this rather than a pinned "<vendor>/<model>:free" slug -- two
        # different pinned slugs 404'd within one debugging session).
        # Limits are enforced per underlying model in principle, but this
        # codebase has no way to know in advance which model the router
        # will pick for a given call, so key_id-level rpm/rpd here is the
        # best available approximation, not a per-model guarantee.
        "openrouter/free": {"rpm": 20, "rpd": 50},
    },
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

# OpenRouter -- also OpenAI-SDK-compatible (same base_url trick as
# Mistral/Gemini/HF above), added per reliability_overhaul_plan.md OR-1a.
#
# OR-1's manual header check (test_openrouter.py) confirmed OpenRouter never
# sends x-ratelimit-* headers on chat completions, on any model tried --
# unlike groq/cerebras/mistral/gemini, so record_headroom() will just get an
# empty `headers` back for this provider every time. That's expected, not a
# bug; the request-count-based gating this implies is a separate rate_ledger
# extension (OR-1c note in the plan) and is NOT part of this patch.
#
# Also per OR-1's live testing: OpenRouter's free-model roster rotates fast
# enough that hardcoding any specific "<vendor>/<model>:free" slug is not
# safe -- two different slugs 404'd within the same debugging session.
# CHAINs in this codebase should route through "openrouter/free" (OpenRouter's
# own auto-router across whatever free models are currently live) rather than
# a pinned slug. See _call_step()'s _is_openrouter_reasoning_guard branch
# below for the failure mode that showed up under that router (a reasoning
# model silently burning the whole max_tokens budget on hidden reasoning,
# returning empty text with finish_reason == "length").
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_TRANSIENT_SDK_ERRORS = (
    GroqRateLimitError, GroqAPIStatusError,
    OpenAIRateLimitError, OpenAIAPIStatusError,
    CerebrasRateLimitError, CerebrasAPIStatusError,
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

# Fix 3 (reliability audit): a 401/403 means the key/project is
# genuinely revoked, suspended, or denied -- not overloaded. It will
# not resolve itself in the ~60s _DEFAULT_COOLDOWN_SECONDS gives every
# other transient error. Before this fix, a broken key like this got
# the exact same short cooldown as a normal 429, came back into
# _best_match()'s candidate pool a minute later, and failed the same
# way again -- silently burning a chain slot on every call that
# happened to land on it. Giving these a much longer cooldown instead
# means a genuinely dead key stops being retried until someone's
# actually fixed it (or this cooldown expires and it gets one more
# chance), rather than every ~60 seconds.
_PERMANENT_ERROR_STATUS_CODES = {401, 403}
_PERMANENT_ERROR_COOLDOWN_SECONDS = 6 * 60 * 60.0  # 6 hours


def _status_code_from_exc(exc):
    """Best-effort extraction of the HTTP status code an SDK exception
    carries. Groq/Cerebras/OpenAI's APIStatusError subclasses all expose
    this directly as exc.status_code (confirmed against all three SDKs'
    own _exceptions.py source); fall back to exc.response.status_code
    for anything that only carries it on the underlying response object.
    Returns None if neither is present -- e.g. _CloudflareTransientError,
    which is only ever raised for an already-confirmed-transient case
    (429/5xx/timeout; see _call_cloudflare_step()), so it has no status
    code of its own to check here and correctly falls through to the
    existing Retry-After / default-cooldown handling below."""
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return status_code
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


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
      0. Fix 3 -- if the error's status code is 401/403, skip every
         signal below entirely and return _PERMANENT_ERROR_COOLDOWN_SECONDS.
         A Retry-After header or "try again in Xs" text answers "how
         long until quota resets," which isn't the question a bad/
         revoked key is asking.
      1. A real `Retry-After` response header -- standard for 429s, and
         what the Groq/OpenAI SDKs expose via exc.response.headers when
         the provider sends one.
      2. Groq's own "try again in 8m5.568s" phrasing inside the
         exception's message text (see module note above).
      3. _DEFAULT_COOLDOWN_SECONDS, if neither signal is present.
    """
    if _status_code_from_exc(exc) in _PERMANENT_ERROR_STATUS_CODES:
        return _PERMANENT_ERROR_COOLDOWN_SECONDS

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


_REQUEST_TOO_LARGE_LIMIT_PATTERN = re.compile(
    r"Limit (?P<limit>\d+), Requested (?P<requested>\d+)", re.IGNORECASE,
)

# Bug fix (2026-08-16): Fix D/D2's original _shrink_prompt_for_retry() cut
# user_content blindly from the end (`user_content[:new_len]`), with no
# idea what was actually IN the tail it was slicing off. For callers like
# agents/hardware_speccer.py's Call 2, the tail is exactly the content the
# call most needs (the finalized parts JSON, then Patch 0.4's hw_ref
# precedent context) -- so a shrink triggered by that same content being
# too large would quietly discard some of it, producing a well-formed but
# starved response (sparse/empty wiring.edges, mech.placements) instead of
# a visible failure.
#
# A caller that has some genuinely optional trailing context (never a
# structural, "the model needs this" part of the prompt) can mark the
# boundary with this sentinel. On a 413, _shrink_prompt_for_retry() drops
# everything after the LAST occurrence of the marker first -- a full,
# cheap, structure-respecting cut -- before ever falling back to
# proportional/blind slicing of what's left. Not caller-specific by
# design: any agent building a "core content + optional context" prompt
# can adopt this the same way hardware_speccer.py does below.
DROPPABLE_CONTEXT_MARKER = "\n\n<<<DROPPABLE_CONTEXT>>>\n\n"


def _shrink_prompt_for_retry(user_content: str, exc) -> str:
    """Fix D (reliability guide, §3 "Fix D"): before this fix, a 413
    "request too large" was caught by the same _TRANSIENT_ERRORS branch
    as a normal 429 -- it set a cooldown (Fix B) and moved to the next
    chain step, but handed that next step the IDENTICAL, still-oversized
    prompt. If the next step's model has a similar or smaller per-request
    TPM ceiling (common when a chain's steps are same-tier free models,
    as happened here), it fails the exact same way, and the whole chain
    burns out on a request that was never going to fit anywhere --
    ending in "All providers in fallback chain exhausted" even though
    every provider was individually reachable and the real problem was
    request size, not availability.

    Parses the provider's own "Limit X, Requested Y" figures out of the
    error message (Groq's actual 413 body shape -- see the traceback
    that motivated this fix) and truncates user_content to fit under
    that limit, with a safety margin for the system prompt and
    message-framing overhead the SDK adds on top of raw content length.
    Falls back to a flat 40% cut if the message doesn't carry those
    figures (some providers phrase this differently) -- still guarantees
    forward progress instead of repeating the identical failure on every
    remaining chain step.

    Bug fix (2026-08-16): before any of the above, check for
    DROPPABLE_CONTEXT_MARKER. If present, drop everything from the LAST
    marker onward and return just the core content -- a caller-declared
    "safe to lose" region is always preferable to guessing which raw
    characters are safe to cut. This alone is usually enough (optional
    context is what pushed the request over the limit in the first
    place); if a second shrink is still needed on the same step, the
    marker will already be gone from the returned core content, so the
    next call falls through to the ratio/flat-cut logic below, now
    operating on the smaller, structurally-intact core only."""
    if DROPPABLE_CONTEXT_MARKER in user_content:
        core = user_content.rsplit(DROPPABLE_CONTEXT_MARKER, 1)[0]
        if len(core) < len(user_content):
            return core
    match = _REQUEST_TOO_LARGE_LIMIT_PATTERN.search(str(exc))
    if match:
        limit = int(match.group("limit"))
        requested = int(match.group("requested"))
        if requested > 0 and limit > 0:
            # 15% safety margin below the stated limit: system prompt +
            # chat-message JSON framing + (if this is a continuation
            # hop) _continuation_prompt()'s own wrapper text all add
            # tokens the provider counts that aren't in len(user_content).
            keep_ratio = max(0.1, min(1.0, (limit / requested) * 0.85))
            new_len = max(1, int(len(user_content) * keep_ratio))
            if new_len < len(user_content):
                return user_content[:new_len]
    # No parseable limit/requested figures -- take a conservative
    # across-the-board cut so the next step still gets a materially
    # smaller request instead of the identical one that just failed.
    return user_content[: max(1, int(len(user_content) * 0.6))]


def _set_cooldown(provider: str, key_id: str, exc) -> None:
    """Writes cooldown_until:{provider}:{key_id} = a UTC unix timestamp
    to the bus after a PERMANENT_AUTH failure, so eo/panel.py's
    _best_match() can skip this specific account until that timestamp
    passes instead of only checking daily token usage (Fix B).
    Deliberately mirrors log_usage()'s own "never raises" contract below
    -- a cooldown-write failure should never take down the actual
    generate_text() call that triggered it, it just means this account
    isn't skipped early next time, same as if quota tracking itself had
    failed to log.

    Phase 3e: this now fires from exactly one call site per branch --
    the ErrorBucket.PERMANENT_AUTH arm below -- rather than
    unconditionally at the top of every `except _TRANSIENT_ERRORS` block.
    _retry_after_seconds(exc) resolves to the long (6h)
    _PERMANENT_ERROR_COOLDOWN_SECONDS duration for the 401/403 status
    codes that land in this bucket, so the actual duration written is
    unchanged -- only when it's written has narrowed. RATE_LIMIT_WINDOW
    failures with no headroom anywhere in the remaining chain get their
    own short, ledger-derived cooldown via _set_ledger_cooldown() below
    instead of this one; CONTEXT_LENGTH_EXCEEDED, MALFORMED_REQUEST, and
    TRANSIENT_NETWORK no longer write a cooldown at all -- none of those
    three indicate the account/key itself is bad or rate-limited, so
    cooling it down was never the correct signal for them.
    """
    try:
        cooldown_until = datetime.now(timezone.utc).timestamp() + _retry_after_seconds(exc)
        bus_write(f"cooldown_until:{provider}:{key_id}", cooldown_until)
    except Exception as write_exc:
        print(f"  [llm_client] cooldown write failed (non-fatal): {write_exc}")


def _set_ledger_cooldown(provider: str, key_id: str, wait_seconds: float) -> None:
    """Phase 3e: the RATE_LIMIT_WINDOW counterpart to _set_cooldown()
    above. Used only from the "neither does anything else remaining in
    the chain" arm of the RATE_LIMIT_WINDOW branch -- i.e. exactly the
    case where _decide_ledger_action() already decided this call site is
    about to sleep wait_seconds and retry THIS step in place. Writing
    that same short, ledger-derived duration as a cooldown (rather than
    _set_cooldown()'s exception-derived _retry_after_seconds(exc), which
    for a rate-limit response is often either absent or a whole-window
    figure far longer than the ledger's own reset estimate) means any
    other caller/worker consulting cooldown_until:{provider}:{key_id} in
    the meantime -- e.g. eo/panel.py's _best_match() -- sees a duration
    consistent with what the ledger itself believes, instead of a
    mismatched or overly long one. Same "never raises" contract as
    _set_cooldown(): a write failure here must never take down the
    generate_text() call that's already decided to wait and retry.
    """
    try:
        cooldown_until = datetime.now(timezone.utc).timestamp() + wait_seconds
        bus_write(f"cooldown_until:{provider}:{key_id}", cooldown_until)
    except Exception as write_exc:
        print(f"  [llm_client] ledger cooldown write failed (non-fatal): {write_exc}")


def _is_cooling_down(provider: str, key_id: str) -> bool:
    """Reads cooldown_until:{provider}:{key_id} straight off the bus --
    the same key _set_cooldown() above writes on every transient/
    permanent-error failure, and the same key eo/panel.py's
    _is_cooling_down() already reads for the tag-driven worker pool.
    That pool checks it before picking an account; the hardcoded CHAIN
    walked by generate_text()/stream_completion() below never did, so a
    step whose key had already earned a 6-hour cooldown (e.g. a 403
    PERMISSION_DENIED -- see _PERMANENT_ERROR_STATUS_CODES above) was
    retried on every single call anyway, burning a real request (and
    the latency of waiting for it to fail) before falling through to
    the next step, every time, until the cooldown happened to be
    checked by some other code path.

    Fails toward "treat it as available" on a bad/missing read (no
    recorded cooldown, or a bus error) -- same posture
    eo/panel.py's own _is_cooling_down() takes, and the same "never let
    quota/cooldown bookkeeping take down the real call" contract
    _set_cooldown() itself follows above. A key genuinely still broken
    will just fail again and re-extend its own cooldown, so failing
    open here costs at most one wasted attempt, never a false skip of
    a key that's actually fine.
    """
    try:
        cooldown_until = bus_read(f"cooldown_until:{provider}:{key_id}", default=None)
    except Exception:
        return False
    if not cooldown_until:
        return False
    return cooldown_until > datetime.now(timezone.utc).timestamp()


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


def _get_openrouter(key_env: str, timeout: float = None) -> OpenAI:
    """Same OpenAI-SDK-via-base_url trick as _get_mistral()/_get_gemini()/
    _get_huggingface() above -- OpenRouter's API is OpenAI-compatible, so no
    new SDK dependency and no new branch needed in the OpenAI-shaped half of
    _call_step(). Reasoning-suppression for this provider specifically is
    handled in _call_step() via extra_body, not here."""
    key = os.getenv(key_env)
    if not key:
        return None
    cache_key = ("openrouter", key_env, timeout)
    if cache_key not in _client_cache:
        kwargs = {"base_url": OPENROUTER_BASE_URL, "api_key": key}
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


def _traced_generation(label: str, model: str, system_prompt: str, prompt_for_step: str,
                        agent_name: str, session_id: str, tier, path, domain):
    """D1 patch 2 -- returns a context manager yielding a Langfuse
    generation observation (or None if tracing is unavailable), never
    raising itself. Isolated as its own helper so both call sites below
    (cloudflare's REST path and the OpenAI-SDK-shaped path) share one
    place where a *tracing* failure is swallowed -- separately from the
    provider call itself, which must run exactly once either way. If
    this raises while building/entering the span, it's caught here and
    None is returned; the caller (see _end_traced_generation()'s `if
    traced is None: return` guard) then just runs the real LLM call
    untraced for this one step."""
    try:
        # D1 audit fix -- prompt_for_step/system_prompt were previously
        # attached to the span at full length (can be an entire generated
        # file for code-writer/scanner/report-writer roles). Truncated
        # here via truncate_for_trace() so a single span's contribution to
        # the export batch stays bounded; see eo/tracing.py's
        # TRACE_TEXT_CHAR_LIMIT docstring for why this is the fix that
        # matters most (batch *size*, not just the HTTP timeout).
        cm = get_tracer().start_as_current_observation(
            name=label, as_type="generation", model=model,
            input=truncate_for_trace(prompt_for_step),
            metadata={
                "system_prompt": truncate_for_trace(system_prompt), "agent_name": agent_name,
                "session_id": session_id, "tier": tier, "path": path,
                "domain": domain,
            },
        )
        gen = cm.__enter__()
        return (cm, gen)
    except Exception as trace_exc:
        _trace_logger.warning(
            "TRACE_EXPORT_FAILED: [%s] tracing failed to start for %s "
            "(non-fatal): %s", agent_name, label, trace_exc)
        return None


def _end_traced_generation(traced, agent_name: str, label: str, text, usage,
                            finish_reason, exc_info=(None, None, None)):
    """D1 patch 2 -- counterpart to _traced_generation(): records the
    result on the span (if tracing started) and closes it. `traced` is
    whatever _traced_generation() returned -- None, or an (cm, gen)
    pair. Any failure here (e.g. talking to Langfuse) is caught and
    logged, never propagated -- by this point the real provider call
    has already completed (or already failed on its own), so a
    tracing-side error here must not look like a failed/second LLM
    call to the rest of generate_text()."""
    if traced is None:
        return
    cm, gen = traced
    try:
        if exc_info[0] is None:
            # D1 audit fix -- same unbounded-payload issue as the input
            # side in _traced_generation() above: `text` is the full
            # completion, capped here for the same reason.
            gen.update(
                output=truncate_for_trace(text),
                usage_details=_usage_details_from_usage(usage),
                metadata={"finish_reason": finish_reason},
            )
        cm.__exit__(*exc_info)
    except Exception as trace_exc:
        _trace_logger.warning(
            "TRACE_EXPORT_FAILED: [%s] tracing failed to close for %s "
            "(non-fatal): %s", agent_name, label, trace_exc)


def _usage_details_from_usage(usage) -> dict | None:
    """D1 patch 2 -- same tolerant unwrap _log_usage() already does (SDK
    object with attributes, or cloudflare's plain dict, or None), reused
    here so Langfuse gets real prompt/completion/total numbers off the
    *same* usage object instead of a second, separately-computed guess.
    Returns None (not {}) when nothing usable is present, since passing
    an empty dict to usage_details would tell Langfuse "zero tokens"
    rather than "unknown" -- the cloudflare-often-absent case this
    module's own docstring already calls out."""
    if usage is None:
        return None

    def _get(key_attr, key_dict):
        val = getattr(usage, key_attr, None)
        if val is None and isinstance(usage, dict):
            val = usage.get(key_dict)
        return val

    prompt = _get("prompt_tokens", "prompt_tokens")
    completion = _get("completion_tokens", "completion_tokens")
    total = _get("total_tokens", "total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    details = {}
    if prompt is not None:
        details["input"] = prompt
    if completion is not None:
        details["output"] = completion
    if total is not None:
        details["total"] = total
    return details


def _call_step(client, model: str, system_prompt: str, user_content: str,
                max_tokens: int = None, provider: str = None):
    """OpenAI-SDK-shaped call, used for groq/cerebras/mistral/gemini/
    openrouter. Returns (text, usage, finish_reason) — usage is the
    provider SDK's usage object (has .total_tokens on all three, since
    they're all OpenAI-compatible chat.completions responses) or None if
    the response didn't include one for some reason.

    OR-1c (reliability_overhaul_plan.md, empty-output guard): OpenRouter's
    free-tier auto-router ("openrouter/free") can land a call on a
    reasoning model. When that happens with a small max_tokens budget, the
    model spends the entire budget on hidden reasoning tokens and returns
    finish_reason == "length" with an EMPTY message.content -- confirmed
    live against openai/gpt-oss-120b:free (whole budget went to
    reasoning_tokens in the usage object, zero completion text). This is
    silent: no exception, no error field, just an empty string that looks
    like a normal (if unhelpful) response to any caller that doesn't
    specifically check for it.

    Fix here is two-part, both gated on provider == "openrouter" so
    groq/cerebras/mistral/gemini call behavior is unchanged:
      1. Send `extra_body={"reasoning": {"exclude": true}}` so OpenRouter
         suppresses reasoning-token spend on models that support the
         param, leaving the full max_tokens budget for visible output.
      2. If the response still comes back with empty text AND
         finish_reason == "length" (some models on the free rotation may
         not honor `exclude`), raise _EmptyReasoningBudgetError so the
         caller (generate_text()) can retry with a larger budget instead
         of silently propagating an empty string downstream. Callers that
         call _call_step() directly (bypassing generate_text()) will see
         this exception rather than a swallowed empty string -- that's
         intentional; there's no safe default text to return here.

    Fix C (reliability guide, §3 "Fix C", truncation handoff):
    finish_reason is the third element of the tuple now — it's
    "length" when the provider stopped because it hit max_tokens
    (real, partial output exists and is worth keeping) versus "stop"
    for a normal completion. All three OpenAI-compatible providers
    here expose this on response.choices[0].finish_reason. Callers
    that don't care can just ignore the third value.

    Root Cause A fix: max_tokens is now an explicit argument instead
    of being omitted (which silently fell back to whatever short
    default the provider/SDK applies -- see the comment above
    _max_tokens_for()). generate_text() always resolves and passes a
    real value via _max_tokens_for(); callers hitting this function
    directly (there are none left as of this fix) still fall back to
    "don't send the key at all" when max_tokens is left None/falsy, so
    this stays backward compatible.

    Phase 3c: now returns a 4th element, `headers` -- the raw HTTP
    response headers (a case-insensitive mapping), so generate_text()
    can feed rate_ledger.record_headroom() the provider-reported
    x-ratelimit-* values (rule 1, the preferred signal -- see
    rate_ledger's module docstring) after every call. Getting at the
    headers means going through .with_raw_response.create(...) instead
    of .create(...) directly and parsing the body ourselves --
    confirmed present on all three SDKs used here (groq, cerebras,
    openai, the last of which mistral/gemini/huggingface also ride via
    _get_mistral()/_get_gemini()/_get_huggingface()'s base_url trick).
    `headers` is best-effort like everything else in this tuple: a
    missing/empty mapping is a normal "this provider didn't send
    rate-limit headers on this call" outcome, not an error.

    OR-1: confirmed OpenRouter never sends x-ratelimit-* headers on any
    model tried, so `headers` will be an empty/near-empty mapping for
    every openrouter call, same as a provider that just didn't include
    them -- record_headroom() already treats that as a normal no-op, no
    special-casing needed here for that part."""
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if provider == "openrouter":
        # Suppress reasoning-token spend -- see docstring above. Ignored
        # (harmlessly, per OpenRouter's docs) by models that don't expose
        # a reasoning budget at all.
        kwargs["extra_body"] = {"reasoning": {"exclude": True}}
    raw_response = client.chat.completions.with_raw_response.create(**kwargs)
    headers = getattr(raw_response, "headers", None) or {}
    response = raw_response.parse()
    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    usage = getattr(response, "usage", None)
    finish_reason = getattr(choice, "finish_reason", None)
    if provider == "openrouter" and not text and finish_reason == "length":
        raise _EmptyReasoningBudgetError(
            model=model, usage=usage, finish_reason=finish_reason,
        )
    return text, usage, finish_reason, headers


class _EmptyReasoningBudgetError(RuntimeError):
    """OR-1c: raised by _call_step() when an openrouter call comes back with
    empty message.content and finish_reason == "length" -- i.e. the whole
    max_tokens budget went to hidden reasoning tokens and nothing was left
    for visible output, even after requesting reasoning={"exclude": True}.
    generate_text() catches this specifically (see its dispatch wrapper) and
    retries the same step once with a larger max_tokens budget rather than
    letting an empty string propagate downstream as if it were a real,
    if unhelpful, response."""

    def __init__(self, model: str, usage, finish_reason: str):
        self.model = model
        self.usage = usage
        self.finish_reason = finish_reason
        reasoning_tokens = getattr(
            getattr(usage, "completion_tokens_details", None),
            "reasoning_tokens", None,
        )
        super().__init__(
            f"openrouter model '{model}' returned empty text with "
            f"finish_reason=length (reasoning_tokens={reasoning_tokens!r}, "
            f"full usage={usage!r}) -- entire max_tokens budget was likely "
            f"consumed by hidden reasoning; caller should retry with a "
            f"larger max_tokens"
        )


# --------------------------------------------------------------------------
# Phase 2 step 2.5 — real (non-test-harness) tool-calling classification.
#
# scripts/test_tool_calling.py (steps 2.3/2.4) proved out a system prompt +
# tools array against Groq's openai/gpt-oss-120b specifically (migrated
# off llama-3.3-70b-versatile, which Groq has decommissioned), and talks
# to Groq directly via the `openai` SDK, deliberately bypassing this
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

# llama-3.3-70b-versatile decommissioned by Groq; this single-model, no-
# fallback path (see block comment above) doesn't have room for a second
# model, so openai/gpt-oss-120b was picked as the closer capability match
# of the two suggested replacements -- qwen/qwen3.6-27b is used elsewhere.
CLASSIFY_INTENT_MODEL = "openai/gpt-oss-120b"
CLASSIFY_INTENT_KEY_ENV = "GROQ_API_KEY"  # shared default key, same as most chains above


# CHANGED — Phase 2 step 2.4 revisit, surfaced by Phase 5 step 5.8's
# fuller coverage run (scripts/test_capability_coverage.py). The
# original prompt's closing line -- "if a request could reasonably map
# to more than one tool, don't call any of them" -- turned out to be
# read by the model far more broadly than intended: it started treating
# *the mere existence of other, differently-shaped tools* as grounds to
# hedge, not just genuinely open-ended requests. Confirmed misfires
# (5.8's run, all 3/3 repeats, well-tuned model just refusing to
# commit): "Quiz me on what I just read." / "Can you test me on this
# material?" (both -> no call, expected generate_study_quiz), "Give me
# a summary I can study from." / "I need a written summary to study
# from." (both -> no call, expected generate_study_guide), "Map out the
# connections between these topics." (-> no call, expected
# generate_mindmap, model asked "mind map or clusters?"), "What should
# I be taking notes on here?" (-> no call, expected
# generate_suggested_notes), and (new in 5.8) "Give me a video
# walkthrough of this material." (-> no call, expected
# generate_video_overview). In every one of these the user named a
# specific kind of material clearly enough for a human to act on
# without asking -- the old prompt's "more than one tool could apply"
# framing was true only in the trivial sense that *some* tool always
# exists that isn't the right one, not that this request was actually
# unclear between two candidates.
#
# Fix: replace the blanket "hedge if >1 tool could apply" rule with (a)
# a few concrete examples of confident classification despite wording
# that doesn't echo the tool's own name, and (b) an explicit statement
# that other tools merely existing isn't itself a reason to hedge --
# only genuine open-endedness about *which* material the user wants
# (e.g. "what should I do next", "help me study this") should trigger a
# clarifying question instead of a call. The individual tool
# descriptions (api/server.py's CAPABILITIES_MANIFEST) were tightened
# alongside this same fix to spell out the confusable-neighbor
# distinctions this prompt's examples reference (study_guide vs
# mindmap vs facts, mindmap vs clusters, video_overview's "walkthrough"
# synonym) -- prompt and descriptions were tuned together against the
# same 5.8 test cases, not independently.
CLASSIFY_INTENT_SYSTEM_PROMPT = (
    "You are the assistant for a study workspace app. You have tools "
    "that generate study materials from the sources currently in the "
    "user's workspace.\n\n"
    "Call a tool whenever the user's request clearly names or "
    "describes one of these materials -- even if their wording doesn't "
    "echo the tool's own name. For example: 'quiz me', 'test my "
    "understanding', and 'test me on this material' should all call "
    "generate_study_quiz; 'a summary I can study from' and 'a written "
    "summary' should both call generate_study_guide; 'map out how "
    "these connect' and 'show me how these relate' should call "
    "generate_mindmap; 'what should I take notes on' should call "
    "generate_suggested_notes; 'a video walkthrough' or 'an explainer "
    "video' should call generate_video_overview. The fact that *other*, "
    "differently-shaped tools also exist is not itself a reason to "
    "hedge or ask a clarifying question -- only do that when the "
    "request itself is genuinely open-ended about which kind of "
    "material the user wants (e.g. 'what should I do next', 'help me "
    "study this'), not merely because more than one tool happens to be "
    "available.\n\n"
    "Watch out for near-misses that sound like a tool but aren't: 'a "
    "step-by-step study workflow' or 'a good study plan' for a topic "
    "asks for an ordered sequence of steps, NOT a written summary -- "
    "don't call generate_study_guide for these, and don't call any "
    "other tool either, since none of them cover ordered study plans "
    "yet. Being willing to commit to a clear match (above) doesn't mean "
    "reaching for the closest-sounding tool when the request is asking "
    "for something structurally different from anything on offer.\n\n"
    "If the request doesn't match any tool -- including requests for "
    "things that sound similar but aren't offered, small talk, or "
    "anything unrelated to the workspace -- do NOT call a tool. Just "
    "reply normally in plain text: say what you can help with instead, "
    "or ask a clarifying question.\n\n"
    "Call at most one tool per turn."
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
                           json_mode: bool = False, max_tokens: int = None):
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
    stays at its default for them.

    Root Cause A fix: max_tokens is threaded through to Workers AI's
    own "max_tokens" request field the same way _call_step() now
    threads it to the OpenAI-compatible providers -- previously this
    request never set it either, riding on Cloudflare's own default.

    Phase 3c: now returns a 4th element, `headers` -- the raw REST
    response's headers (a `requests` CaseInsensitiveDict), mirroring
    _call_step()'s new 4th return value so generate_text() can feed
    both branches into rate_ledger.record_headroom() the same way. Per
    the module docstring's CLOUDFLARE CAVEAT above (usage is often
    absent from this response entirely), don't assume Cloudflare's
    headers carry standard x-ratelimit-* fields either -- Workers AI
    isn't confirmed to send them. This is threaded through anyway (a)
    on the chance a given account/model does surface them, matching the
    same honest-but-hopeful posture the usage field already gets, and
    (b) so the cloudflare branch in generate_text() doesn't need a
    special-cased call shape vs. the SDK branch. record_headroom()
    itself already treats an all-None reading as a no-op (see its own
    docstring), so a Cloudflare call with no rate-limit headers costs
    nothing beyond one harmless bus write of empty values."""
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
    if max_tokens:
        payload["max_tokens"] = max_tokens
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
    # Some structured/tool-shaped completions (seen on
    # @cf/meta/llama-3.1-8b-instruct, wired into eo/panel.py's
    # MEMBER_B_CHAIN) come back with result["response"] as a nested
    # dict/list instead of a plain string. A non-empty dict/list is
    # truthy, so the `or ""` above doesn't rescue it, and the old
    # `text.strip()` below threw AttributeError on the un-stringified
    # object -- which isn't in _TRANSIENT_ERRORS, so it wasn't caught
    # chain-locally and killed the whole member vote instead of just
    # this step. Normalize to a string here so callers always get text.
    if not isinstance(text, str):
        text = json.dumps(text) if text else ""
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
    return text.strip(), usage, finish_reason, response.headers


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
    both call sites, just forwarded through here and into log_usage().

    Phase 3c: the actual extraction now lives in the shared
    _extract_total_tokens() helper (above _LEDGER_WAIT_CAP_SECONDS) --
    record_usage() needs the identical tolerant unwrap, so it's factored
    out rather than copied a second time."""
    tokens = _extract_total_tokens(usage)
    log_usage(provider, key_id, tokens, session_id=session_id, tier=tier, path=path,
              agent_name=agent_name, domain=domain, model=model)


# CO5 follow-up -- spot-check probe for the groq/cerebras
# stream_options={"include_usage": True} question flagged in
# stream_completion()'s docstring point 4. This does NOT fix anything by
# itself (there's nothing to fix without seeing a real response); it just
# makes the spot-check loud and easy to run once live keys are flipped on,
# instead of the shape mismatch silently resulting in tokens=None the way
# _log_usage() above degrades by design for every other caller.
_USAGE_SHAPE_PROVIDERS_TO_WATCH = ("groq", "cerebras")


def _probe_usage_shape(provider: str, model: str, usage, agent_name: str) -> None:
    """Called once per streamed step, right after a step finishes, with
    whatever `usage` stream_completion() collected off the trailing
    usage-only SSE chunk (see point 4). Only asserts/warns for the two
    providers that haven't been spot-checked yet -- silent no-op for
    mistral/gemini/huggingface, which go through the same `openai` SDK
    client that's already confirmed to accept the kwarg.

    Three loud outcomes, each printed with a distinct tag so they're easy
    to grep for once real keys are running:

      [USAGE-PROBE][MISSING]  -- usage is None. Either the TypeError
      fallback silently dropped stream_options (SDK rejected the kwarg),
      or the SDK accepted it but never sent a usage-only trailing chunk.
      Both mean tokens=None is being logged for every call right now.

      [USAGE-PROBE][UNKNOWN-SHAPE] -- usage came back as something that's
      neither an object with .total_tokens nor a dict with "total_tokens".
      _log_usage() will silently coerce this to tokens=None too; this
      makes that visible instead so the extraction logic above can be
      extended for the actual shape.

      [USAGE-PROBE][OK] -- total_tokens was found and extracted cleanly.
      Printed once so a successful spot-check is unambiguous in the logs,
      not just an absence of warnings.

    Never raises -- a probe that could itself break the stream would
    defeat the point (see _log_usage's "never raises" contract, which
    this mirrors)."""
    if provider not in _USAGE_SHAPE_PROVIDERS_TO_WATCH:
        return
    try:
        if usage is None:
            print(f"  [{agent_name}] [USAGE-PROBE][MISSING] {provider}:{model} -- "
                  f"no usage object came back on this streamed call. Either "
                  f"stream_options={{'include_usage': True}} was rejected (TypeError "
                  f"fallback path, see stream_completion() point 4) or the SDK accepted "
                  f"it but sent no trailing usage chunk. tokens is being logged as None "
                  f"for this call right now.")
            return
        tokens = getattr(usage, "total_tokens", None)
        shape = "attr" if tokens is not None else None
        if tokens is None and isinstance(usage, dict):
            tokens = usage.get("total_tokens")
            shape = "dict" if tokens is not None else None
        if tokens is None:
            print(f"  [{agent_name}] [USAGE-PROBE][UNKNOWN-SHAPE] {provider}:{model} -- "
                  f"usage object came back as {type(usage).__name__!r} ({usage!r}) but "
                  f"no .total_tokens attribute or 'total_tokens' dict key was found on "
                  f"it. _log_usage() is silently logging tokens=None for this shape.")
        else:
            print(f"  [{agent_name}] [USAGE-PROBE][OK] {provider}:{model} -- "
                  f"total_tokens={tokens} extracted via {shape} shape.")
    except Exception as exc:
        # The probe itself must never be the thing that breaks a live
        # stream during the spot-check -- report and move on.
        print(f"  [{agent_name}] [USAGE-PROBE][ERROR] {provider}:{model} -- "
              f"probe raised {exc!r} while inspecting usage; ignoring.")


_MAX_REQUEST_TOO_LARGE_RETRIES = 2  # Fix D2: cap how many times a single
# chain step will be retried in place after a 413 "request too large"
# (see classify_error()'s CONTEXT_LENGTH_EXCEEDED bucket / 3d, and
# _shrink_prompt_for_retry() below) before generate_text() gives up on
# that step and moves on (or, if it's the last step, raises).
# _shrink_prompt_for_retry() computes its cut from
# the provider's own stated Limit/Requested figures, so this should
# converge within a single retry in the common case -- 2 is a safety
# margin for a provider whose accounting isn't a 1:1 match for
# len(user_content) (e.g. a very different tokenizer), not an
# expectation that it normally takes both attempts.

_MAX_CONTINUATIONS = 2  # Fix C: cap how many times one call will chase a
# "length" cutoff before just returning what it has. Bounded independently
# of chain length: a chain can be up to MAX_CHAIN_STEPS (3) long for
# *account* fallback reasons alone, so without this cap a pathologically
# short max_tokens setting could burn the whole chain on continuations
# and leave nothing for a genuine 429/5xx to fall back to.

# Root Cause A fix (2026-08-16 reliability audit): every call through
# _call_step()/_call_cloudflare_step() previously left max_tokens unset
# entirely, so it silently rode on whatever short default the provider/
# SDK applies. That's the actual reason truncation (finish_reason ==
# "length") was showing up on almost every agent in the chain rather
# than one flaky call -- Fix C's continuation handoff was recovering
# from a self-inflicted ceiling, not a real provider limit. Reasoning-
# flavored models (e.g. groq's qwen/qwen3.6-27b, used by both
# eo/inspector.py and hardware_speccer.py's fallback chain) make this
# worse: they spend part of that budget on invisible <think> tokens
# before ever emitting the real answer, so a flat default isn't enough
# for them specifically.
#
# DEFAULT_MAX_TOKENS / REASONING_MODEL_MAX_TOKENS are floors applied
# automatically per step via _max_tokens_for() below -- a chain step
# that wants a different budget can still set its own "max_tokens" key
# (see generate_text()'s chain-shape docstring), which always wins.
DEFAULT_MAX_TOKENS = 8192
REASONING_MODEL_MAX_TOKENS = 16384
_REASONING_MODEL_HINTS = ("qwen3.6", "qwen/qwen3.6", "-thinking", "r1", "deepseek-r1")


def _max_tokens_for(model: str, step: dict) -> int:
    """Resolves the max_tokens budget for one chain step. An explicit
    step["max_tokens"] (set by the caller's chain definition) always
    wins; otherwise a model name matching _REASONING_MODEL_HINTS gets
    REASONING_MODEL_MAX_TOKENS, everything else gets
    DEFAULT_MAX_TOKENS. See the Root Cause A comment above
    _MAX_CONTINUATIONS for why this exists."""
    explicit = step.get("max_tokens") if step else None
    if explicit:
        return explicit
    model_lower = (model or "").lower()
    if any(hint in model_lower for hint in _REASONING_MODEL_HINTS):
        return REASONING_MODEL_MAX_TOKENS
    return DEFAULT_MAX_TOKENS


def _estimate_tokens_for_call(system_prompt: str, user_content: str) -> int:
    """Phase 3 (§PHASE 2 'Pre-flight estimation') -- rough chars/4
    heuristic. Doesn't need to be exact, only good enough to gate
    rate_ledger.can_proceed() before a call goes out; record_usage()
    (fed from the real usage object after the call returns) is what
    keeps the ledger's sliding window accurate over time regardless of
    how rough this pre-flight guess is."""
    return (len(system_prompt or "") + len(user_content or "")) // 4


def _extract_total_tokens(usage) -> "int | None":
    """Phase 3c -- pulled out of _log_usage() (below) so the same
    tolerant unwrap (SDK object with .total_tokens, or a plain dict
    with "total_tokens", or neither) is shared between _log_usage()'s
    dashboard logging and record_usage()'s ledger bookkeeping instead
    of two copies of the same three lines drifting apart. usage may
    legitimately be None (no usage object came back at all -- the
    Cloudflare gap the module docstring describes, or any other
    provider hiccup); that's not an error, just "nothing to record"."""
    if usage is None:
        return None
    tokens = getattr(usage, "total_tokens", None)
    if tokens is None and isinstance(usage, dict):
        tokens = usage.get("total_tokens")
    return tokens


# Phase 3c -- provider-reported rate-limit header names this module has
# actually seen (Groq, Cerebras, and the OpenAI-compatible providers
# riding _get_mistral()/_get_gemini()/_get_huggingface()'s base_url
# trick all use the same OpenAI-style x-ratelimit-* header names).
# Cloudflare's REST response isn't confirmed to send any of these --
# see _call_cloudflare_step()'s Phase 3c docstring note -- so a
# Cloudflare header mapping just won't match and this falls through to
# all-None, which record_headroom() already treats as a no-op.
_HEADROOM_REMAINING_TOKENS_HEADERS = ("x-ratelimit-remaining-tokens",)
_HEADROOM_REMAINING_REQUESTS_HEADERS = ("x-ratelimit-remaining-requests",)
# Reset can come back as a plain seconds figure ("60") or a duration
# string ("1m0s", "30s") depending on provider -- Groq (and the OpenAI-
# compatible providers) use the duration-string form.
_HEADROOM_RESET_TOKENS_HEADERS = ("x-ratelimit-reset-tokens",)
_HEADROOM_RESET_REQUESTS_HEADERS = ("x-ratelimit-reset-requests",)
_RESET_DURATION_RE = re.compile(
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s)?$")


def _parse_reset_seconds(raw: "str | None") -> "float | None":
    """Best-effort parse of a reset header value into seconds. Accepts
    a bare number ("60", "60.0") or Groq/OpenAI-style duration strings
    ("1m0s", "30s", "2m"). Returns None (never raises) on anything that
    doesn't match either shape -- can_proceed()'s caller-side fallback
    (a flat 5.0s guess, see its own docstring) covers that case."""
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    match = _RESET_DURATION_RE.match(raw.strip())
    if not match or not (match.group("minutes") or match.group("seconds")):
        return None
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return minutes * 60 + seconds


def _extract_headroom_from_headers(headers) -> "tuple[int|None, int|None, float|None]":
    """Phase 3c -- parses whatever provider-reported rate-limit headers
    are present into rate_ledger.record_headroom()'s
    (remaining_tokens, remaining_requests, reset_seconds) shape.

    Never raises: any missing/malformed header just falls through to
    None for that field, same fail-open posture as everything else this
    phase touches (see rate_ledger's own module docstring). `headers`
    may be a requests CaseInsensitiveDict (cloudflare branch) or an
    httpx-style Headers mapping (SDK branch, off raw_response.headers)
    -- both support case-insensitive .get(), which is all this needs.
    Picks the SMALLER of the tokens/requests reset windows when both are
    present, since that's the soonest point *either* ceiling frees up,
    and can_proceed() only needs one wait figure back out."""
    try:
        if not headers:
            return None, None, None

        def _int_header(names):
            for name in names:
                value = headers.get(name)
                if value is not None:
                    try:
                        return int(float(value))
                    except (TypeError, ValueError):
                        return None
            return None

        remaining_tokens = _int_header(_HEADROOM_REMAINING_TOKENS_HEADERS)
        remaining_requests = _int_header(_HEADROOM_REMAINING_REQUESTS_HEADERS)

        reset_candidates = []
        for names in (_HEADROOM_RESET_TOKENS_HEADERS, _HEADROOM_RESET_REQUESTS_HEADERS):
            for name in names:
                parsed = _parse_reset_seconds(headers.get(name))
                if parsed is not None:
                    reset_candidates.append(parsed)
        reset_seconds = min(reset_candidates) if reset_candidates else None

        return remaining_tokens, remaining_requests, reset_seconds
    except Exception as parse_exc:
        print(f"  [rate_ledger] header headroom parse failed (non-fatal): {parse_exc}")
        return None, None, None


# Phase 3b -- hard ceiling on how long a single gated call will sleep
# before retrying, regardless of what rate_ledger.can_proceed() suggests.
# Bounds worst-case added latency from a single step's wait/reroute
# decision; a suggested_wait_seconds bigger than this (e.g. a whole
# window's worth) gets capped rather than blocking the caller for real.
_LEDGER_WAIT_CAP_SECONDS = 20.0


def _remaining_chain_headroom(chain: list, from_index: int, estimated_tokens: int):
    """Phase 3b -- cheap, read-only look-ahead used once the CURRENT
    step's can_proceed() has already reported no headroom. For every
    step after from_index, applies the same skip conditions the main
    dispatch loop below applies right before it would otherwise send
    (missing creds, active cooldown) so this never counts a step that
    couldn't actually be dispatched to anyway, then asks the ledger --
    no network call, just a bus read -- whether THAT step currently has
    headroom for the same estimated_tokens figure.

    Returns a list of (index, provider, key_id, model, ok, wait_seconds)
    tuples, one per still-eligible step, so a caller can both look for a
    reroute target (any ok=True) and, failing that, work out which of
    the remaining steps resets soonest."""
    results = []
    for j in range(from_index + 1, len(chain)):
        step = chain[j]
        provider = step["provider"]
        model = step["model"]
        if provider == "cloudflare":
            creds = _get_cloudflare_creds(step["account_id_env"], step["token_env"])
            if creds is None:
                continue
            key_id = step["account_id_env"]
        else:
            if not os.getenv(step["key_env"]):
                continue
            key_id = step["key_env"]
        if _is_cooling_down(provider, key_id):
            continue
        ok, wait = rate_ledger.can_proceed(provider, key_id, model, estimated_tokens)
        results.append((j, provider, key_id, model, ok, wait))
    return results


def _decide_ledger_action(chain: list, index: int, current_wait: float,
                           estimated_tokens: int) -> "tuple[str, float]":
    """Phase 3 (§PHASE 3, the 'If NOT OK' branch) -- shared by both the
    cloudflare and SDK-shaped dispatch loops below so the reroute-vs-wait
    decision itself isn't duplicated in two places.

    Returns ("reroute", 0.0) when some later, still-eligible step in the
    chain already has headroom right now -- the caller should skip
    sending on the current step entirely and fall through to that next
    step instead, same as this codebase's existing "falling back to next
    in chain" pattern on a hard failure.

    Returns ("wait", wait_seconds) when nothing downstream has headroom
    either. wait_seconds is the smaller of _LEDGER_WAIT_CAP_SECONDS and
    the soonest reset among the current step and every remaining step
    checked -- i.e. "the step with the soonest reset" from the plan,
    whichever step that turns out to be. The caller retries the CURRENT
    step after sleeping rather than jumping straight to that other step
    (the chain is walked in order elsewhere in this function); waiting
    only as long as the best-case step needs, rather than the current
    step's own possibly-longer suggested wait, means the next gate check
    re-evaluates sooner instead of over-sleeping."""
    ahead = _remaining_chain_headroom(chain, index, estimated_tokens)
    if any(ok for (_j, _p, _k, _m, ok, _w) in ahead):
        return "reroute", 0.0
    candidate_waits = [current_wait] + [w for (_j, _p, _k, _m, _ok, w) in ahead]
    return "wait", min(min(candidate_waits), _LEDGER_WAIT_CAP_SECONDS)


def _ledger_gate(chain: list, index: int, provider: str, key, model: str,
                  system_prompt: str, prompt_for_step: str, label: str,
                  agent_name: str) -> str:
    """3f-1 -- shared pre-flight ledger gate, extracted verbatim from the
    duplicated cloudflare/SDK-shaped blocks in generate_text(). Reads
    ledger state only (estimate -> can_proceed() -> _decide_ledger_action()),
    sleeping in the "wait" case as a side effect, but never touches any
    of generate_text()'s own loop-scoped state -- so unlike 3f-3/3f-4
    this can return a plain sentinel with no state to hand back.

    `key` is whichever identifier this call site uses to key the ledger
    (key_id for cloudflare, key_env for the SDK-shaped branch) -- this
    function is agnostic to which one it's given.

    Returns one of:
      "proceed"      -- headroom confirmed, caller should make the call.
      "reroute"      -- caller should `break` out of this step's retry
                         loop and fall through to the next chain step.
      "waited-retry" -- caller already slept the decided duration and
                         should `continue` to retry THIS step now.
    """
    _estimated_tokens = _estimate_tokens_for_call(system_prompt, prompt_for_step)
    _ledger_ok, _ledger_wait = rate_ledger.can_proceed(
        provider, key, model, _estimated_tokens)
    if _ledger_ok:
        return "proceed"
    _action, _wait_seconds = _decide_ledger_action(
        chain, index, _ledger_wait, _estimated_tokens)
    if _action == "reroute":
        print(f"  [{agent_name}] {label} has no headroom for "
              f"~{_estimated_tokens} estimated tokens -- a later chain "
              f"step already has headroom, rerouting immediately "
              f"instead of waiting.")
        return "reroute"
    print(f"  [{agent_name}] {label} has no headroom for "
          f"~{_estimated_tokens} estimated tokens, and neither does "
          f"anything else remaining in the chain -- sleeping "
          f"{_wait_seconds:.1f}s (suggested {_ledger_wait:.1f}s, capped "
          f"at {_LEDGER_WAIT_CAP_SECONDS:.0f}s) before retrying {label} "
          f"in place.")
    time.sleep(_wait_seconds)
    return "waited-retry"


def _record_ledger_bookkeeping(provider: str, key, model: str, usage, headroom_headers) -> None:
    """3f-2 -- shared post-call ledger bookkeeping, extracted verbatim
    from the duplicated cloudflare/SDK-shaped blocks in generate_text().
    Called right after _log_usage() in both branches.

    Both signals get fed regardless of which one can_proceed() ends up
    trusting on the next call: record_headroom() with whatever this
    response's headers carried (often nothing at all for Cloudflare --
    see _call_cloudflare_step()'s Phase 3c docstring note, and
    record_headroom() itself no-ops cleanly on an all-None reading), and
    record_usage() with the real token count this call actually cost,
    feeding the sliding-window fallback that's the ONLY signal available
    whenever provider-reported headroom is absent or stale.

    Purely side-effecting (rate_ledger.record_headroom()/record_usage()
    calls on already-computed `usage`/`headroom_headers`) -- no control
    flow, no loop-scoped state to hand back, unlike 3f-3/3f-4.

    `key` is whichever identifier this call site uses to key the ledger
    (key_id for cloudflare, key_env for the SDK-shaped branch) -- this
    function is agnostic to which one it's given.
    """
    _remaining_tokens, _remaining_requests, _reset_seconds = \
        _extract_headroom_from_headers(headroom_headers)
    rate_ledger.record_headroom(provider, key, model,
                                 _remaining_tokens, _remaining_requests, _reset_seconds)
    _actual_tokens = _extract_total_tokens(usage)
    if _actual_tokens is not None:
        rate_ledger.record_usage(provider, key, model, _actual_tokens)


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


def _handle_finish_reason(accumulated_text: str, full_text: str, finish_reason,
                           allow_continuation: bool, continuations_used: int,
                           is_last: bool, label: str, agent_name: str) -> "tuple[str, str, int]":
    """3f-3 -- shared finish_reason/continuation handling, extracted
    from the duplicated cloudflare/SDK-shaped blocks in generate_text():
    the three `finish_reason == "length"` branches plus the final
    `return full_text` for a complete (non-truncated) response.

    Unlike 3f-1/3f-2, this mutates loop-scoped state (`accumulated_text`,
    `continuations_used`) that lives in generate_text()'s own for-loop --
    a nested helper can't reach into the caller's loop variables
    directly, so instead of running side effects it takes the current
    values in and hands back the (possibly updated) values for the
    caller to reassign, plus a sentinel telling the caller what to do
    next.

    Returns (action, accumulated_text, continuations_used):
      action == "return"    -- finish_reason wasn't "length": this step
                                produced a complete answer. Caller
                                should `return full_text` using its own
                                already-computed full_text -- the
                                accumulated_text/continuations_used
                                values returned alongside are unchanged
                                and unused in this case.
      action == "next-step" -- caller should reassign its
                                accumulated_text/continuations_used
                                loop variables to the returned values
                                and `break` out of this step's retry
                                loop to fall through to the next chain
                                step (or end-of-chain handling, if
                                is_last).
    """
    if (finish_reason == "length" and allow_continuation
            and continuations_used < _MAX_CONTINUATIONS and not is_last):
        # Fix C: real partial output, hand it to the next step.
        print(f"  [{agent_name}] {label} truncated (finish_reason=length), "
              f"continuing on next chain step...")
        return "next-step", full_text, continuations_used + 1
    if finish_reason == "length" and not allow_continuation and not is_last:
        # Caller opted out of continuation (e.g. single-shot JSON
        # classifier) -- discard the partial text and retry the
        # *original* prompt fresh on the next step instead of splicing
        # a continuation onto it.
        print(f"  [{agent_name}] {label} truncated (finish_reason=length), "
              f"continuation disabled for this call -- discarding partial "
              f"output and retrying original prompt on next chain step...")
        return "next-step", accumulated_text, continuations_used
    if finish_reason == "length" and is_last:
        # Root Cause C fix (2026-08-16 reliability audit): previously
        # this case fell straight through to the unconditional `return
        # full_text` below with NO log line and no flag on the return
        # value -- a truncation that happened to land on the LAST chain
        # step (nothing left to hand off to, regardless of
        # allow_continuation) was silently shipped as if it were a
        # normal, complete answer. Route it through the same
        # accumulated_text path every other "ran out while truncated"
        # case already uses, so it gets the same visible "chain
        # exhausted while still truncated" log line at the bottom of
        # this function instead of vanishing.
        print(f"  [{agent_name}] {label} truncated (finish_reason=length) "
              f"on the LAST chain step -- no further step to hand off to.")
        return "next-step", full_text, continuations_used
    return "return", accumulated_text, continuations_used


def _handle_transient_error(exc, provider: str, key, model: str, chain: list, index: int,
                             is_last: bool, label: str, agent_name: str, system_prompt: str,
                             prompt_for_step: str, user_content: str, accumulated_text: str,
                             same_step_shrinks: int) -> "tuple[str, str, str, int]":
    """3f-4 -- shared classify_error() exception dispatch, extracted
    from the duplicated `except _TRANSIENT_ERRORS as exc:` bodies in
    generate_text() (cloudflare/SDK-shaped branches), including the 3e
    _set_cooldown()/_set_ledger_cooldown() call sites for whichever
    bucket actually cools a key down.

    This is the riskiest of the 3f extractions: the original code uses
    `raise`, `continue`, and `break` to control the OUTER while/for loop
    directly, which a nested helper can't do on the caller's behalf. So
    instead of running that control flow itself, this returns a
    sentinel that the caller's except-block translates back into the
    real raise/continue/break, plus whatever loop-scoped state
    (`user_content`, `accumulated_text`, `same_step_shrinks`) the
    bucket's handling needs to update.

    `key` is whichever identifier this call site uses to key the
    ledger/cooldown (key_id for cloudflare, key_env for the SDK-shaped
    branch) -- this function is agnostic to which one it's given.

    Returns (action, user_content, accumulated_text, same_step_shrinks):
      action == "raise"          -- caller should bare `raise` (re-raise
                                     the exception currently being
                                     handled) -- MALFORMED_REQUEST only;
                                     a bare raise still works here since
                                     the caller's except-block frame is
                                     what's actually executing it, and
                                     that frame's "currently handled
                                     exception" context is unaffected by
                                     this helper call returning normally.
      action == "retry-in-place" -- caller should `continue` to retry
                                     THIS step now (a shrink was just
                                     applied, or a rate-limit wait
                                     already ran inside this call).
      action == "next-step"      -- caller should `break` to fall
                                     through to the next chain step (or
                                     off the end, if is_last).
    """
    _bucket = classify_error(exc)  # Phase 3d
    if _bucket == ErrorBucket.CONTEXT_LENGTH_EXCEEDED:
        # Unchanged Fix D/D2 shrink-and-retry-in-place logic -- a genuine
        # per-request size ceiling is the one case where shrinking the
        # prompt is the correct recovery.
        if same_step_shrinks < _MAX_REQUEST_TOO_LARGE_RETRIES:
            shrunk = _shrink_prompt_for_retry(user_content, exc)
            same_step_shrinks += 1
            print(f"  [{agent_name}] {label} rejected the request as too "
                  f"large ({exc.__class__.__name__}, CONTEXT_LENGTH_EXCEEDED) "
                  f"-- shrinking prompt from {len(user_content)} to "
                  f"{len(shrunk)} chars and retrying {label} in place "
                  f"(attempt {same_step_shrinks}/{_MAX_REQUEST_TOO_LARGE_RETRIES})...")
            # stale partial output no longer matches the shrunk prompt
            return "retry-in-place", shrunk, "", same_step_shrinks
        if not is_last:
            print(f"  [{agent_name}] {label} still over context length "
                  f"after {_MAX_REQUEST_TOO_LARGE_RETRIES} in-place shrinks, "
                  f"falling back to next in chain...")
        return "next-step", user_content, accumulated_text, same_step_shrinks
    if _bucket == ErrorBucket.RATE_LIMIT_WINDOW:
        # Phase 3d: an org-scoped, time-windowed quota problem is never a
        # size problem -- never shrink the prompt here (see llm_errors.py's
        # recovery table). Route back through 3b's reroute-vs-bounded-wait
        # decision instead of the old blind "fall through to next step"
        # dispatch. _retry_after_seconds(exc) (the provider's own
        # Retry-After / "try again in Xs" signal for THIS failure) stands
        # in for the pre-flight can_proceed() wait estimate 3b normally
        # feeds _decide_ledger_action() with.
        _estimated_tokens = _estimate_tokens_for_call(system_prompt, prompt_for_step)
        _action, _wait_seconds = _decide_ledger_action(
            chain, index, _retry_after_seconds(exc), _estimated_tokens)
        if _action == "reroute":
            print(f"  [{agent_name}] {label} hit a rate-limit window "
                  f"({exc.__class__.__name__}) -- a later chain step "
                  f"already has headroom, rerouting immediately instead "
                  f"of waiting.")
            return "next-step", user_content, accumulated_text, same_step_shrinks
        # 3e: no headroom anywhere in the remaining chain -- this is the
        # one RATE_LIMIT_WINDOW case that DOES cool this key down, using
        # the ledger's own short wait figure rather than _set_cooldown()'s
        # exception-derived duration.
        _set_ledger_cooldown(provider, key, _wait_seconds)
        print(f"  [{agent_name}] {label} hit a rate-limit window "
              f"({exc.__class__.__name__}), and neither does anything "
              f"else remaining in the chain -- sleeping "
              f"{_wait_seconds:.1f}s before retrying {label} in place.")
        time.sleep(_wait_seconds)
        return "retry-in-place", user_content, accumulated_text, same_step_shrinks
    if _bucket == ErrorBucket.MALFORMED_REQUEST:
        # Our own payload is wrong. Never retry unchanged -- retrying
        # (same step, next step, or after a wait) would just fail
        # identically. Log loudly and surface it to the caller as the
        # real bug it is.
        print(f"  [{agent_name}] {label} rejected the request as "
              f"malformed ({exc.__class__.__name__}) -- this is a bug "
              f"in the request itself, not a transient failure. "
              f"Not retrying; raising.")
        return "raise", user_content, accumulated_text, same_step_shrinks
    if _bucket == ErrorBucket.PERMANENT_AUTH:
        # Bad/revoked key -- no amount of retrying helps. 3e: this is now
        # the ONLY place in this branch that calls _set_cooldown() -- it
        # applies the long (6h) cooldown for a 401/403 (see
        # _retry_after_seconds()'s _PERMANENT_ERROR_STATUS_CODES check)
        # right where the decision to pull this key from rotation is
        # made, instead of unconditionally at the top of the except
        # block regardless of bucket.
        _set_cooldown(provider, key, exc)
        if not is_last:
            print(f"  [{agent_name}] {label} failed with a permanent "
                  f"auth error ({exc.__class__.__name__}) -- pulling it "
                  f"from rotation for this chain and falling back to "
                  f"next in chain...")
        return "next-step", user_content, accumulated_text, same_step_shrinks
    # ErrorBucket.TRANSIENT_NETWORK -- timeout/5xx/connection reset. 3e:
    # no longer cooled down here -- a single network blip doesn't mean
    # this account/key is bad, so standard backoff is just "move to the
    # next chain step now, this step gets tried again on its own merits
    # next time it comes up" rather than an explicit cooldown.
    if not is_last:
        print(f"  [{agent_name}] {label} failed ({exc.__class__.__name__}, "
              f"TRANSIENT_NETWORK), falling back to next in chain...")
    return "next-step", user_content, accumulated_text, same_step_shrinks


def _run_chain_step(chain: list, index: int, is_last: bool, provider: str, model: str, key,
                     label: str, system_prompt: str, user_content: str, accumulated_text: str,
                     continuations_used: int, allow_continuation: bool, agent_name: str,
                     session_id, tier, path, domain, call_fn) -> "tuple[str, str, str, int, object]":
    """3f-5 -- the single shared per-step retry loop, replacing the two
    duplicated `while True:` blocks in generate_text() (the cloudflare
    and SDK-shaped branches). Wires together 3f-1..3f-4's helpers in the
    exact order both branches already used: pre-flight ledger gate
    (_ledger_gate(), 3f-1) -> traced call (_traced_generation()/
    _end_traced_generation(), unchanged since D1 patch 2) -> the one
    remaining per-branch difference (`call_fn`, see below) -> post-call
    bookkeeping (_record_ledger_bookkeeping(), 3f-2) -> finish_reason/
    continuation handling (_handle_finish_reason(), 3f-3) -> transient
    error dispatch (_handle_transient_error(), 3f-4).

    `call_fn` is a one-argument closure the caller builds right before
    calling this function, taking `prompt_for_step` and returning the
    same 4-tuple _call_step()/_call_cloudflare_step() already return --
    (text, usage, finish_reason, headroom_headers), the 4-tuple contract
    from 3c's message. Everything else that differs between the two
    call sites (creds vs. client, json_mode, the model's max_tokens) is
    already baked into the closure by the caller, so this function
    itself doesn't need to know which branch it's running for.

    `key` is whichever identifier this call site uses to key the
    ledger/cooldown (key_id for cloudflare, key_env for the SDK-shaped
    branch) -- unchanged from how 3f-1/3f-2/3f-4 already take it.

    `same_step_shrinks` (Fix D2) is NOT a parameter here: unlike
    accumulated_text/continuations_used, it never needs to survive past
    a single chain step, so it's initialized fresh to 0 on every call to
    this function -- exactly matching the old `same_step_shrinks = 0`
    that used to sit just above each branch's `while True:` in
    generate_text(), which reset on every new `for i, step in
    enumerate(chain)` iteration.

    Returns (action, full_text, accumulated_text, continuations_used, last_exc):
      action == "return"    -- this step produced a complete answer.
                                Caller should `return full_text`
                                immediately, unchanged.
      action == "next-step" -- caller should reassign its own
                                accumulated_text/continuations_used loop
                                variables to the returned values, update
                                its own last_exc if the returned last_exc
                                isn't None, and let its `for` loop fall
                                through to the next chain step (this
                                function has already done the internal
                                `break` -- there's nothing left for the
                                caller to break out of). full_text is
                                unused/stale in this case.
    A bare `raise` inside the _TRANSIENT_ERRORS handler (MALFORMED_REQUEST
    only, per _handle_transient_error()) propagates straight out of this
    function -- the caller doesn't need a try/except of its own for that,
    same as before 3f-5.
    """
    last_exc = None
    same_step_shrinks = 0  # Fix D2: scoped to retries-in-place on THIS step only
    while True:  # Fix D2: retry loop scoped to THIS step only
        prompt_for_step = (
            _continuation_prompt(user_content, accumulated_text)
            if accumulated_text else user_content
        )
        _gate = _ledger_gate(chain, index, provider, key, model,
                              system_prompt, prompt_for_step, label, agent_name)
        if _gate == "reroute":
            break  # skip this step, fall through to the next chain step
        if _gate == "waited-retry":
            continue  # retry the SAME step now that we've waited
        try:
            # D1 patch 2 -- tracing wraps the real network call; it runs
            # exactly once regardless of whether tracing itself succeeds.
            _traced = _traced_generation(label, model, system_prompt, prompt_for_step,
                                          agent_name, session_id, tier, path, domain)
            try:
                text, usage, finish_reason, headroom_headers = call_fn(prompt_for_step)
            except BaseException:
                _end_traced_generation(_traced, agent_name, label, None, None, None,
                                        exc_info=sys.exc_info())
                raise
            _end_traced_generation(_traced, agent_name, label, text, usage, finish_reason)
            _log_usage(provider, key, usage, session_id, tier, path, agent_name, domain=domain, model=model)
            _record_ledger_bookkeeping(provider, key, model, usage, headroom_headers)
            full_text = accumulated_text + text
            _action, accumulated_text, continuations_used = _handle_finish_reason(
                accumulated_text, full_text, finish_reason, allow_continuation,
                continuations_used, is_last, label, agent_name)
            if _action == "next-step":
                break  # move on to next chain step (or fall through to
                # end-of-chain truncated-output handling, if is_last)
            return "return", full_text, accumulated_text, continuations_used, last_exc
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            _action, user_content, accumulated_text, same_step_shrinks = \
                _handle_transient_error(
                    exc, provider, key, model, chain, index, is_last, label,
                    agent_name, system_prompt, prompt_for_step, user_content,
                    accumulated_text, same_step_shrinks)
            if _action == "raise":
                raise
            if _action == "retry-in-place":
                continue  # retry the SAME step now
            break  # move on to next chain step (or fall off the end, if is_last)
    return "next-step", "", accumulated_text, continuations_used, last_exc


def generate_text(system_prompt: str, user_content: str, chain: list, agent_name: str = "Agent",
                   session_id: str = None, tier: int = None, path: str = None,
                   domain: str = None, allow_continuation: bool = True) -> str:
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

    `allow_continuation` (default True, preserves prior behavior): set
    this False for single-shot structured-output calls (e.g. a "respond
    with ONLY JSON" classifier) where Fix C's continuation prompt is the
    wrong tool. Fix C's "continue exactly where the partial answer left
    off" instruction is written for prose/code -- a reasoning model
    (e.g. groq's qwen/qwen3.6-27b) that gets truncated mid-<think> and is
    then told to "continue seamlessly" will just finish its train of
    thought and stop, never emitting the JSON it was originally asked
    for. _strip_fences() then strips the now-closed, empty <think> block,
    leaving "" for json.loads() to choke on with "Expecting value: line 1
    column 1 (char 0)". With allow_continuation=False, a "length"
    truncation is treated the same as a transient error: the partial
    text is discarded and the *original* prompt (not a continuation
    prompt) is retried fresh, with a full token budget, on the next chain
    step -- rather than being spliced together.
    """
    last_exc = None
    accumulated_text = ""   # Fix C: partial output carried across a
    # "length" truncation handoff. Empty string means "nothing generated
    # yet" -- distinct from a step that legitimately returns "".
    continuations_used = 0
    _original_user_content = user_content  # Bug fix (2026-08-16), see below

    for i, step in enumerate(chain):
        provider = step["provider"]
        model = step["model"]
        is_last = i == len(chain) - 1
        # Bug fix (2026-08-16): Fix D2's same_step_shrinks loop below
        # reassigns the shared `user_content` variable in place. That's
        # correct WITHIN one step's retry loop, but this is a plain
        # function-local variable shared across the whole `for` loop --
        # nothing previously reset it between chain steps, so a 413 on
        # step 1 silently handed every later fallback provider the
        # ALREADY-shrunk prompt too, compounding data loss across
        # providers instead of giving each one a fresh full attempt.
        # Resetting here scopes a shrink strictly to retries-in-place on
        # THIS step, matching what same_step_shrinks' own name implies.
        user_content = _original_user_content
        # Fix D2 (reliability guide, §3 "Fix D2" -- retry-in-place for a
        # too-large request): the first cut of Fix D only ever handed a
        # shrunk prompt to the *next* chain step. That's fine when there
        # IS a next step, but the traceback that motivated this fix hit
        # the 413 on the chain's LAST step -- back then `if is_last:
        # break` fired before Fix D's shrink logic ever ran, so the
        # request was never actually retried smaller; generate_text()
        # just gave up immediately with the same 413 as last_exc, even
        # though this exact provider/model would likely have accepted
        # the very same request at a smaller size. same_step_shrinks
        # lets a too-large request be retried against the SAME step,
        # shrunk each time from that step's own stated Limit/Requested
        # figures, before falling through to "next step" (or giving up,
        # on the last step) logic. 3f-5: same_step_shrinks itself is no
        # longer initialized here -- it moved inside _run_chain_step(),
        # which is called fresh once per chain step below and so resets
        # it to 0 on every call, same as this loop used to do by hand.

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
            if _is_cooling_down(provider, key_id):
                print(f"  [{agent_name}] {label} skipped — still cooling down "
                      f"(see cooldown_until:{provider}:{key_id}).")
                continue
            json_mode = step.get("json_mode", False)
            _max_tok = _max_tokens_for(model, step)  # Root Cause A fix

            def _call_fn(prompt_for_step, _creds=creds, _model=model, _jm=json_mode, _mt=_max_tok):
                return _call_cloudflare_step(_creds, _model, system_prompt, prompt_for_step,
                                              json_mode=_jm, max_tokens=_mt)

            # 3f-5 -- the whole retry loop (pre-flight gate -> traced call
            # -> dispatch -> bookkeeping -> finish_reason handling ->
            # exception dispatch) now lives in the single shared
            # _run_chain_step(), also called from the SDK-shaped branch
            # below. This branch differs only in `key_id` vs. `key_env`
            # and in `_call_fn` (which wraps _call_cloudflare_step() vs.
            # _call_step()) -- see that function's docstring.
            _action, full_text, accumulated_text, continuations_used, _step_exc = \
                _run_chain_step(chain, i, is_last, provider, model, key_id, label,
                                 system_prompt, user_content, accumulated_text,
                                 continuations_used, allow_continuation, agent_name,
                                 session_id, tier, path, domain, _call_fn)
            if _step_exc is not None:
                last_exc = _step_exc
            if _action == "return":
                return full_text
            continue

        key_env = step["key_env"]
        timeout = step.get("timeout")
        getter = {
            "groq": _get_groq, "cerebras": _get_cerebras,
            "mistral": _get_mistral, "gemini": _get_gemini,
            "huggingface": _get_huggingface,
            "openrouter": _get_openrouter,  # OR-1a
        }.get(provider)
        if getter is None:
            raise ValueError(f"[{agent_name}] Unknown provider '{provider}' in chain.")

        label = f"{provider}:{model}"
        if _is_cooling_down(provider, key_env):
            print(f"  [{agent_name}] {label} skipped — still cooling down "
                  f"(see cooldown_until:{provider}:{key_env}).")
            continue

        client = getter(key_env, timeout)
        if client is None:
            print(f"  [{agent_name}] {provider}:{model} skipped — {key_env} not set.")
            continue

        _max_tok = _max_tokens_for(model, step)  # Root Cause A fix

        def _call_fn(prompt_for_step, _client=client, _model=model, _mt=_max_tok, _provider=provider):
            try:
                return _call_step(_client, _model, system_prompt, prompt_for_step,
                                   max_tokens=_mt, provider=_provider)
            except _EmptyReasoningBudgetError as _empty_exc:
                # OR-1c: whole budget went to hidden reasoning, nothing left
                # for visible output, even with reasoning={"exclude": True}
                # sent. Retry this SAME step once with a much larger budget
                # rather than letting an empty string reach the caller as if
                # it were a real (if unhelpful) answer. 4x is a starting
                # point, not a tuned constant -- revisit if this fires often
                # enough in practice to be worth reading real reasoning_tokens
                # counts off of _empty_exc.usage and sizing off that instead.
                print(f"  [{agent_name}] {label} returned empty text "
                      f"(reasoning burned the budget) — retrying once with "
                      f"max_tokens={_mt * 4} instead of {_mt}.")
                return _call_step(_client, _model, system_prompt, prompt_for_step,
                                   max_tokens=_mt * 4, provider=_provider)

        # 3f-5 -- same shared _run_chain_step() as the cloudflare branch
        # above; this branch differs only in `key_env` vs. `key_id` and
        # in `_call_fn` (which wraps _call_step() vs.
        # _call_cloudflare_step()) -- see that function's docstring.
        _action, full_text, accumulated_text, continuations_used, _step_exc = \
            _run_chain_step(chain, i, is_last, provider, model, key_env, label,
                             system_prompt, user_content, accumulated_text,
                             continuations_used, allow_continuation, agent_name,
                             session_id, tier, path, domain, _call_fn)
        if _step_exc is not None:
            last_exc = _step_exc
        if _action == "return":
            return full_text

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

async def stream_completion(system_prompt: str, user_content: str, chain: list,
                             agent_name: str = "Agent", session_id: str = None,
                             tier=None, path=None, domain=None):
    """
    CO5 patch 2 -- streaming twin of generate_text() above. Built for
    agents/output_organizer.py's organize_final_answer_stream(), which
    imports this by name; see that function's docstring for the caller
    side of this contract.

    SCOPE -- read before wiring another chain into this:

    1. OpenAI-SDK-shaped providers only (groq/cerebras/mistral/gemini/
       huggingface). Cloudflare Workers AI's step shape is a plain,
       non-streaming REST call (_call_cloudflare_step) -- a cloudflare
       step in `chain` is skipped here with a log line, same pattern
       _call_step()'s callers use for a missing key_env, not attempted
       as a hard error. output_organizer.CHAIN never includes
       cloudflare, so this doesn't block CO5 itself; a future caller
       that DOES need Cloudflare in its chain will silently lose that
       fallback step here until someone adds SSE support to the REST
       path.

    2. Fix A (fallback-on-transient-error) only applies BEFORE the
       first chunk of a given step has been yielded to the caller.
       Once one chunk has gone out, the caller (the SSE endpoint,
       ultimately the browser) has already started rendering partial
       text -- silently restarting the same answer on a different
       provider at that point would mean either duplicating or
       discarding visible text, so this stops and raises instead.
       generate_text()'s "retry the same logical answer on a later
       step" semantics only make sense before anything has been shown.

    3. Fix C (truncation-continuation-handoff) is NOT implemented here.
       generate_text() handles finish_reason == "length" by re-prompting
       the next chain step with a continuation prompt and stitching the
       text together server-side before the caller ever sees it -- doing
       that mid-stream means the caller would see a pause/gap while a
       second provider is prompted, which is a real UX decision, not a
       one-line port of Fix C's non-streaming version. Left as an
       explicit gap for a later patch; finish_reason is still checked
       and logged if a step ends "length" so the gap is visible in logs
       rather than silently dropped.

    4. stream_options={"include_usage": True} is an OpenAI-API
       convention for getting a final usage-only chunk out of a
       streamed response. Passed here so _log_usage() still gets real
       numbers instead of silently logging nothing for every streamed
       call -- confirmed accepted by the `openai` SDK (mistral/gemini/
       huggingface steps go through that client). NOT independently
       confirmed against the `groq` / `cerebras` SDKs' own stream
       kwargs as of this patch -- both are OpenAI-compatible but that
       specific kwarg needs checking against their current SDK
       versions before relying on usage numbers for those two
       providers; if the kwarg is rejected, the call fails BEFORE
       yielding a chunk, so it falls through to the next chain step
       via the normal Fix-A path rather than breaking the stream.
       _probe_usage_shape() (just above _MAX_CONTINUATIONS below) logs
       a loud [USAGE-PROBE][...] line for every groq/cerebras streamed
       step so this can be spot-checked from real logs once live keys
       are running, instead of a shape mismatch silently degrading to
       tokens=None inside _log_usage() with nothing to grep for.

    5b. D1 patch 2 part B -- tracing. One Langfuse generation span per
       chain step attempt (not per chunk): opened via _traced_generation()
       right after this step's client is resolved, kept open across the
       whole token-by-token stream, and closed exactly once via
       _end_traced_generation() -- either in the "done" branch with the
       full accumulated text + real usage_details (usage only exists once
       the trailing usage-only chunk lands, see point 4), or in the
       "error" branch with exc_info set, whether that error is about to
       raise mid-stream or just fall through to the next chain step. A
       tracing failure inside either helper is caught and logged there,
       same non-fatal posture as generate_text()'s spans -- it never skips
       or double-fires the real provider call, and never blocks a chunk
       from reaching the caller.

    5. Every provider client here is the same *synchronous* SDK client
       generate_text() uses -- there is no separate async SDK wired up
       anywhere in this module. To make this a real async generator
       (so FastAPI's StreamingResponse doesn't block the event loop for
       other in-flight requests during a long synthesis stream), the
       blocking iteration over the SDK's stream object runs in a worker
       thread via asyncio.to_thread, and chunks are handed back across
       an asyncio.Queue.

    6. OR-1c-stream (reliability_overhaul_plan.md): same empty-output
       risk _call_step() documents for generate_text() applies here too
       -- an "openrouter" step can land on a reasoning model that burns
       the entire max_tokens budget on hidden reasoning tokens and never
       emits a single content delta, ending finish_reason == "length"
       having yielded nothing. `extra_body={"reasoning": {"exclude":
       True}}` is sent for openrouter steps (mirrors _call_step()) to
       suppress that spend in the first place. If it still happens
       anyway (`started` is False when the "done" event arrives, same
       provider, same finish_reason), this falls through to the NEXT
       chain step rather than the retry-with-larger-budget
       generate_text() does -- deliberately simpler: retrying the SAME
       step here would mean either restructuring this generator into a
       nested retry loop or duplicating the whole _run_stream_sync/
       consumer-loop pair, and per point 2 above this codebase already
       only allows step-level fallback (not in-place retry) before the
       first chunk goes out, so falling through is consistent with the
       function's existing Fix-A scope rather than a new retry pattern
       bolted on for one provider.

    Yields: str delta-text chunks only (not raw SSE payloads/JSON --
    api/routes/tasks.py's endpoint layer, CO5 step 3, is what wraps each
    chunk in its own `data: {...}` envelope).

    Raises RuntimeError if every attempted step fails before yielding
    anything, mirroring generate_text()'s all-exhausted failure mode.
    Raises mid-stream (see point 2) if a step fails after it already
    yielded real text -- callers should treat that as "the answer the
    user is already seeing stopped early," not as a clean failure they
    can silently retry.
    """
    loop = asyncio.get_event_loop()
    last_exc = None

    for i, step in enumerate(chain):
        provider = step["provider"]
        model = step["model"]

        if provider == "cloudflare":
            print(f"  [{agent_name}] cloudflare:{model} skipped in stream_completion — "
                  f"Cloudflare's REST path doesn't support streaming yet (see docstring).")
            continue

        key_env = step["key_env"]
        timeout = step.get("timeout")
        getter = {
            "groq": _get_groq, "cerebras": _get_cerebras,
            "mistral": _get_mistral, "gemini": _get_gemini,
            "huggingface": _get_huggingface,
            "openrouter": _get_openrouter,  # OR-1c-stream
        }.get(provider)
        if getter is None:
            raise ValueError(f"[{agent_name}] Unknown provider '{provider}' in chain.")

        label = f"{provider}:{model}"
        if _is_cooling_down(provider, key_env):
            print(f"  [{agent_name}] {label} skipped — still cooling down "
                  f"(see cooldown_until:{provider}:{key_env}).")
            continue

        client = getter(key_env, timeout)
        if client is None:
            print(f"  [{agent_name}] {provider}:{model} skipped — {key_env} not set.")
            continue

        # D1 patch 2 part B -- same traced-call pattern as generate_text()'s
        # two branches (see _traced_generation()/_end_traced_generation()
        # docstrings), adapted for streaming: the span opens here, before
        # the first chunk, and stays open for the life of this step's
        # attempt -- it's closed exactly once below, either in the "done"
        # branch (success) or the "error" branch (failure), never both and
        # never left dangling on a `continue`/`break` to the next chain step.
        _traced = _traced_generation(label, model, system_prompt, user_content,
                                      agent_name, session_id, tier, path, domain)
        _stream_text_parts = []  # accumulates chunks so the span gets the full output, not just the last delta

        chunk_queue: asyncio.Queue = asyncio.Queue()

        def _run_stream_sync(client=client, model=model, provider=provider):
            """Runs in a worker thread (blocking SDK iteration). Pushes
            ("chunk", text) / ("done", (usage, finish_reason)) /
            ("error", exc) onto chunk_queue via call_soon_threadsafe so
            the async side above can await it safely."""
            # Root Cause A fix: same _max_tokens_for() budget resolution
            # generate_text() now uses -- this streaming path had the
            # identical "max_tokens never set" gap.
            _stream_max_tokens = _max_tokens_for(model, step)
            # OR-1c-stream: see docstring point 6 -- suppress reasoning-
            # token spend for openrouter steps the same way _call_step()
            # does for the non-streaming path. `create_kwargs` built once
            # so both the with-stream_options and without-stream_options
            # attempts below stay in sync rather than drifting if only one
            # gets this added later.
            create_kwargs = dict(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=_stream_max_tokens,
                stream=True,
            )
            if provider == "openrouter":
                create_kwargs["extra_body"] = {"reasoning": {"exclude": True}}
            try:
                try:
                    response_stream = client.chat.completions.create(
                        **create_kwargs,
                        stream_options={"include_usage": True},
                    )
                except TypeError:
                    # See point 4 -- some SDK versions may reject
                    # stream_options. Retry once without it rather than
                    # losing the whole step over a usage-tracking extra;
                    # usage will just be None for this call in that case.
                    response_stream = client.chat.completions.create(**create_kwargs)
                usage = None
                finish_reason = None
                for event in response_stream:
                    if not event.choices:
                        # Usage-only trailing chunk (stream_options path).
                        usage = getattr(event, "usage", None) or usage
                        continue
                    choice = event.choices[0]
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr
                    text = getattr(choice.delta, "content", None)
                    if text:
                        loop.call_soon_threadsafe(chunk_queue.put_nowait, ("chunk", text))
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("done", (usage, finish_reason)))
            except _TRANSIENT_ERRORS as exc:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("error", exc))
            except Exception as exc:
                # Non-transient (prompt/parsing/auth-shape) error -- still
                # surfaced through the queue rather than left to hang it,
                # but NOT retried on the next chain step (matches
                # generate_text()'s "don't mask real bugs" rule).
                loop.call_soon_threadsafe(chunk_queue.put_nowait, ("error", exc))

        stream_task = asyncio.ensure_future(asyncio.to_thread(_run_stream_sync))
        started = False

        while True:
            kind, payload = await chunk_queue.get()
            if kind == "chunk":
                started = True
                _stream_text_parts.append(payload)
                yield payload
            elif kind == "done":
                usage, finish_reason = payload
                # D1 patch 2 part B -- usage only exists once the trailing
                # usage-only SSE chunk lands (see docstring point 4), which
                # is exactly this "done" branch, so the span can only be
                # updated with real usage_details here -- never right after
                # the call returns, since at that point usage isn't known yet.
                _end_traced_generation(_traced, agent_name, label,
                                        "".join(_stream_text_parts), usage, finish_reason)
                _probe_usage_shape(provider, model, usage, agent_name)
                _log_usage(provider, key_env, usage, session_id, tier, path, agent_name,
                           domain=domain, model=model)
                if finish_reason == "length":
                    print(f"  [{agent_name}] {label} truncated (finish_reason=length) "
                          f"mid-stream -- Fix C continuation is NOT implemented for "
                          f"streaming (see docstring point 3); stream ends here.")
                # OR-1c-stream (docstring point 6): nothing was ever
                # streamed AND the step stopped because it hit max_tokens
                # -- on openrouter specifically this means the whole
                # budget went to hidden reasoning tokens, same failure
                # _call_step() guards against for generate_text(). Since
                # no chunk has reached the caller yet, it's still safe to
                # fall through to the next chain step (Fix-A's own "before
                # first chunk" scope, see docstring point 2) instead of
                # ending the stream having yielded nothing at all.
                if provider == "openrouter" and not started and finish_reason == "length":
                    print(f"  [{agent_name}] {label} yielded no output before "
                          f"exhausting its budget (reasoning-token burn) -- "
                          f"falling back to next in chain instead of ending "
                          f"the stream empty.")
                    await stream_task
                    break
                await stream_task
                return
            else:  # "error"
                last_exc = payload
                # D1 patch 2 part B -- close the span on every failure exit
                # from this step's attempt, whether it's about to raise
                # (mid-stream failure) or just fall through to the next
                # chain step (pre-first-chunk transient failure). Mirrors
                # generate_text()'s `except BaseException: _end_traced_generation(...,
                # exc_info=sys.exc_info()); raise` pattern -- text/usage/
                # finish_reason are ignored by _end_traced_generation
                # whenever exc_info is set, so None/None/None here is fine.
                _end_traced_generation(
                    _traced, agent_name, label, None, None, None,
                    exc_info=(type(last_exc), last_exc, last_exc.__traceback__),
                )
                await stream_task
                if started:
                    raise RuntimeError(
                        f"[{agent_name}] {label} failed mid-stream after partial output "
                        f"was already sent to the caller: {last_exc}"
                    ) from last_exc
                if isinstance(payload, _TRANSIENT_ERRORS):
                    _set_cooldown(provider, key_env, payload)  # Fix B
                    print(f"  [{agent_name}] {label} failed before first chunk "
                          f"({payload.__class__.__name__}), falling back to next in chain...")
                    break
                # Non-transient and nothing streamed yet for this step --
                # still a real bug, don't mask it by falling through.
                raise RuntimeError(f"[{agent_name}] {label} failed: {last_exc}") from last_exc

    raise RuntimeError(
        f"[{agent_name}] All providers in fallback chain exhausted or unavailable "
        f"before any streamed output. Last error: {last_exc}"
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
# heavy imports); the re-export itself now lives at the top of this
# file, alongside the other imports.