"""
eo/credits.py -- S02: cost-weighted credits for one model call.

    credits_for(provider, model, input, cached_input, output) -> float

turns one call's token counts into credits (Build Plan section O2, Decision
D5). The point of "cost-weighted" is the agent loop: a coding agent re-sends
a large, mostly cached prompt at every step and gets a short reply, so
counting raw tokens would drain the shared pool far faster than the call
really costs, and ignoring the cache discount would overcharge it. Each
class of token is priced separately, in USD per million tokens:

    input        prompt tokens that were NOT served from the provider's cache
    cached_input prompt tokens served from the cache (the "cached read" price)
    output       generated tokens

TOKEN CONVENTION (the one callers must follow)
----------------------------------------------
`input` is the TOTAL prompt tokens of the call, INCLUDING the cached ones;
`cached_input` is the part of that total that came from the cache. So

    cached   = min(cached_input, input)
    uncached = input - cached            (= max(input - cached_input, 0))

and uncached + cached always equals `input`. This is the OpenAI-compatible
shape (usage.prompt_tokens includes usage.prompt_tokens_details.cached_tokens)
and is what usage_ledger.cached_tokens_from_usage() extracts. A provider
that reports cache reads SEPARATELY from input tokens (Anthropic's native
usage does) must be adapted by the caller: pass input = its input tokens +
cache-read tokens + cache-write tokens, cached_input = its cache-read
tokens. Passing the uncached count as `input` would make cached_input
exceed it; such a call is clamped (cached cannot exceed the prompt), which
under-counts rather than raises.

Every token argument is cleaned first: None, negative, NaN, infinite,
non-numeric and boolean values count as 0; floats are truncated to whole
tokens; numeric strings are accepted.

WHERE THE PRICES COME FROM
--------------------------
  * utils/llm_client.py's _TOKEN_PRICES_PER_MILLION is the one table of
    input and output prices. It is imported read-only (lazily, inside
    _llm_price_table(), so this module never creates an import cycle with
    llm_client and importing it stays cheap) and is NEVER edited here.
  * config/credits.json supplies the cached-read price of every model, and
    the full three-price entry of any model missing from that table. For a
    model in both, the table wins for input/output and credits.json wins for
    cached_input.
  * The size of one credit is NOT in credits.json. It stays single-sourced
    in config/plans.json as credits_per_usd and is read through
    eo/plans.py (D5): credits = USD cost x plans.credits_per_usd().

UNKNOWN MODELS
--------------
A (provider, model) with no usable price is charged
config/credits.json's default_prices_usd_per_million, which must be at least
as expensive as every priced model (a unit test enforces it), and a warning
is logged ONCE per (provider, model) per process, so a model that nobody
priced shows up in the logs without flooding them. A table model with no
cached price in credits.json is billed with cached tokens at its input price
(no invented discount) and also logs once.

FAIL OPEN
---------
This is metering, so nothing here raises, whatever it is given: odd token
values, a non-string provider or model, a missing/corrupt credits.json (a
built-in copy of the default prices is used), an unimportable llm_client
(credits.json entries only), or a broken plans.json. The one case that
cannot be priced at all -- no usable credits_per_usd -- charges 0 credits and
logs once; the call itself is never blocked. Results are rounded to 6
decimals to match usage_ledger.credits numeric(18, 6).

This module touches no database and holds no per-call state beyond the
log-once set and the cached config. It is NOT wired into the ledger yet:
usage_ledger.credits_for_call() still prices calls its own S01 way, and
wiring this in is a later patch (S10).
"""
import functools
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from eo import plans

_log = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "credits.json"

CREDIT_DECIMALS = 6   # matches usage_ledger.credits numeric(18, 6) / _CREDIT_DECIMALS
_USD_DECIMALS = 8     # matches llm_client._compute_cost_usd
_PRICE_KEYS = ("input", "cached_input", "output")

# Used only if config/credits.json cannot be read at all, so a broken config
# file still prices every call at a conservative rate instead of at 0. A
# unit test pins it equal to the default shipped in credits.json.
_LAST_RESORT_DEFAULT = {"input": 1.0, "cached_input": 1.0, "output": 3.0}

_warned: set = set()
_warned_lock = threading.Lock()


@dataclass(frozen=True)
class ModelPrices:
    """USD per million tokens for one model. `known` is False when these are
    the unknown-model default rather than a real price."""
    input: float
    cached_input: float
    output: float
    known: bool = True


# ---------------------------------------------------------------------
# Logging (once per key) and small helpers
# ---------------------------------------------------------------------

def _label(value) -> str:
    try:
        return str(value)[:80]
    except Exception:
        return "?"


def _warn_once(key: tuple, message: str, *args) -> None:
    """Log `message` at WARNING the first time `key` is seen in this process."""
    try:
        with _warned_lock:
            if key in _warned:
                return
            _warned.add(key)
        _log.warning(message, *args)
    except Exception:
        pass  # logging must never be the thing that breaks metering


def _tokens(value) -> int:
    """A token count as a non-negative int. See the module docstring."""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _price(value) -> "float | None":
    """A usable price (finite, >= 0, not a bool) or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _config() -> dict:
    """{"default": {input, cached_input, output}, "models": {provider: {model:
    {price key: value}}}}. Never raises: any problem reading the file falls
    back to the built-in default and no per-model entries; a bad individual
    entry or value is dropped (and logged) on its own."""
    path = Path(os.getenv("CREDITS_CONFIG_PATH") or _DEFAULT_CONFIG_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        default = {key: _price(raw["default_prices_usd_per_million"][key]) for key in _PRICE_KEYS}
        if any(value is None for value in default.values()):
            raise ValueError("default_prices_usd_per_million needs input, cached_input and "
                             "output, each a non-negative number")
        models: dict = {}
        raw_models = raw.get("models", {})
        if not isinstance(raw_models, dict):
            raise ValueError("'models' must be an object")
        for provider, entries in raw_models.items():
            if not isinstance(entries, dict):
                _log.warning("credits.json: ignoring provider %s (not an object)", _label(provider))
                continue
            for model, entry in entries.items():
                if not isinstance(entry, dict):
                    _log.warning("credits.json: ignoring %s/%s (not an object)",
                                 _label(provider), _label(model))
                    continue
                cleaned = {}
                for key, value in entry.items():
                    if key not in _PRICE_KEYS:
                        continue
                    price = _price(value)
                    if price is None:
                        _log.warning("credits.json: ignoring %s/%s %s=%r (not a non-negative number)",
                                     _label(provider), _label(model), key, value)
                        continue
                    cleaned[key] = price
                models.setdefault(provider, {})[model] = cleaned
        return {"default": default, "models": models}
    except Exception as exc:
        _log.error("credits.json could not be used (%s: %s); pricing with the built-in default "
                   "and no per-model entries", type(exc).__name__, _label(exc))
        return {"default": dict(_LAST_RESORT_DEFAULT), "models": {}}


def reload_config() -> None:
    """Drop the cached config and forget which models were already warned
    about (tests, or after editing credits.json in a long-running process)."""
    _config.cache_clear()
    with _warned_lock:
        _warned.clear()


def _llm_price_table() -> dict:
    """utils/llm_client.py's input/output price table, read-only. Imported
    lazily so eo.credits never forms an import cycle with llm_client."""
    from utils import llm_client
    table = llm_client._TOKEN_PRICES_PER_MILLION
    return table if isinstance(table, dict) else {}


# ---------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------

def prices_for(provider, model) -> ModelPrices:
    """The three per-million prices for (provider, model), or the unknown-
    model default (known=False, logged once). Never raises."""
    cfg = _config()
    try:
        table = _llm_price_table()
    except Exception as exc:
        _warn_once(("llm_table",), "credits: llm_client's price table is unavailable (%s: %s); "
                   "pricing from credits.json only", type(exc).__name__, _label(exc))
        table = {}

    try:
        if isinstance(provider, str) and isinstance(model, str) and provider and model:
            table_provider = table.get(provider)
            table_entry = table_provider.get(model) if isinstance(table_provider, dict) else None
            table_in = _price(table_entry.get("input")) if isinstance(table_entry, dict) else None
            table_out = _price(table_entry.get("output")) if isinstance(table_entry, dict) else None
            json_entry = cfg["models"].get(provider, {}).get(model, {})

            if table_in is not None and table_out is not None:
                inp, out = table_in, table_out          # the table owns input/output
            else:
                inp, out = _price(json_entry.get("input")), _price(json_entry.get("output"))

            if inp is not None and out is not None:
                cached = _price(json_entry.get("cached_input"))
                if cached is None:
                    _warn_once(("no-cached", provider, model),
                               "credits: no cached-read price for %s/%s in credits.json; "
                               "billing cached tokens at the input price", _label(provider), _label(model))
                    cached = inp
                return ModelPrices(inp, cached, out, known=True)
    except Exception as exc:
        _warn_once(("lookup", _label(provider), _label(model)),
                   "credits: price lookup failed for %s/%s (%s: %s); using the default price",
                   _label(provider), _label(model), type(exc).__name__, _label(exc))

    _warn_once(("unknown", _label(provider), _label(model)),
               "credits: no price for model %s/%s; charging the conservative default "
               "(add it to config/credits.json)", _label(provider), _label(model))
    d = cfg["default"]
    return ModelPrices(d["input"], d["cached_input"], d["output"], known=False)


def _usd(provider, model, input, cached_input, output) -> float:
    prices = prices_for(provider, model)
    total = _tokens(input)
    cached = min(_tokens(cached_input), total)
    uncached = total - cached
    return (uncached * prices.input + cached * prices.cached_input
            + _tokens(output) * prices.output) / 1_000_000


def cost_usd_for(provider, model, input=None, cached_input=None, output=None) -> float:
    """The USD cost of one call, with cached reads at the cached price.
    Same token convention and fail-open behavior as credits_for() (a failure
    returns 0.0). Rounded to 8 decimals like llm_client._compute_cost_usd.
    This is the figure the ledger's cost_usd column should hold alongside
    credits_for()'s credits, since llm_client's own cost has no cached price."""
    try:
        return round(_usd(provider, model, input, cached_input, output), _USD_DECIMALS)
    except Exception as exc:
        _warn_once(("usd-failed",), "credits: cost_usd_for failed (%s: %s); returning 0.0",
                   type(exc).__name__, _label(exc))
        return 0.0


def credits_for(provider, model, input=None, cached_input=None, output=None) -> float:
    """Credits to charge for one call: USD cost (see the module docstring for
    the token convention) x plans.credits_per_usd(), rounded to 6 decimals.
    Never raises and never returns a negative or non-finite number; if the
    credit size itself is unusable it returns 0.0 (logged once) rather than
    guessing a rate."""
    try:
        rate = plans.credits_per_usd()
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"credits_per_usd must be positive and finite, got {rate!r}")
        credits = _usd(provider, model, input, cached_input, output) * rate
        if not math.isfinite(credits) or credits < 0:
            return 0.0
        return round(credits, CREDIT_DECIMALS)
    except Exception as exc:
        _warn_once(("credits-failed", type(exc).__name__),
                   "credits: could not price a call (%s: %s); charging 0 credits",
                   type(exc).__name__, _label(exc))
        return 0.0
