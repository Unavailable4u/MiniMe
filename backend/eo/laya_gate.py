"""
eo/laya_gate.py — shared loader for Laya (github.com/NandhaKishorM/laya),
the open-weight (Apache 2.0), non-autoregressive "System 1" decision
model: typed questions (choice/score/noul) over a state, answered in one
local forward pass with a probability per answer.

Laya migration audit (2026-09-27): several choke points in this codebase
were spending a full generation-model round trip (Groq, a whole model
call to emit one word or a short closed-set label) on exactly the
"typed decision over a state" shape Laya targets -- see each call site's
own comment for its specific before/after. This module owns what every
one of those call sites needs and shouldn't each reimplement:

  - ONE process-wide Agent. Loading a checkpoint is the expensive part
    (Laya's README, "Production Preload & Memory"), so call sites go
    through get_agent()/predict(), never laya.load() directly, and
    api/server.py's _lifespan calls preload() once at startup so the
    first real request doesn't pay the load.
  - a fail-open predict() wrapper: Laya unavailable or erroring returns
    None and each caller falls back to its own pre-Laya safe default.
    A FAILED LOAD IS REMEMBERED for LAYA_LOAD_RETRY_SECONDS (default
    300) -- without that, an unreachable Hugging Face would make every
    single call re-attempt the import/download.
  - input-budget helpers, because the English checkpoint's context is
    SMALL. It defaults to 512 tokens with 192 of those reserved for the
    answer options (head_max_len), leaving roughly 300 tokens -- about
    1,000-1,200 English characters, fewer for code/URLs/non-English --
    for the state itself; anything past that is silently truncated, not
    rejected. So call sites must NOT pass a long text as one state:
    chunk() splits it, clip() budgets each field of a multi-field state.
    (laya-multilingual reads longer with max_len=8192, but its own
    published table shows accuracy dropping past ~4k tokens, and it is
    weaker on English -- so it is not a drop-in fix for a safety gate.)
  - SERIALIZED inference. check_content_safety() is called from a
    ThreadPoolExecutor at intake (api/task_runner.py) while other
    requests run in parallel, so one shared Agent would be hit by
    several threads at once. I have not verified that laya's
    Agent.predict is thread-safe (Hugging Face fast tokenizers, for one,
    can raise "Already borrowed" under concurrent use), so predict()
    holds a lock around the forward pass. Each pass is short and torch
    already parallelizes inside it, so this costs little; remove the
    lock only after testing concurrent predict() on the real model.
  - an OPT-IN decision log (LAYA_DECISION_LOG=/path/file.jsonl): one
    JSON line per prediction (site, clipped state, answers). NOTE these
    are Laya's OWN outputs, not ground truth -- they are raw material to
    review/correct before fine-tuning (Convai publishes a Kaggle
    fine-tuning notebook), not labels as-is. The state can contain user
    content, so this is off unless you set the env var; fields are
    clipped to _LOG_FIELD_CHARS and the file stops growing at
    LAYA_DECISION_LOG_MAX_MB (default 50).

Probabilities: Laya trains against proper scoring rules, but its model
card reportedly says the released checkpoints are OVER-CONFIDENT -- the
thresholds at each call site are starting guesses to tune on your own
labeled samples, not calibrated cut-offs.

Call sites (Tier 1): eo/output_guard.py (check_content_safety),
eo/injection_guard.py (score_snippet), eo/semantic_cache.py
(_verify_still_accurate), eo/device_archetype.py
(resolve_ambiguous_archetype).

Laya migration Tier 4 (2026-09-29) adds two small shared helpers below,
parse_mode()/bump_counter()/read_counter(), for gates that sit in front
of an existing LLM call rather than replacing it outright: an
on/observe/off mode (Tier 2's eo/sga.py pre-gate and Tier 3's
eo/laya_routing.py both hand-roll this same three-way parse; Tier 4's
agents/overlapping_checker.py and agents/contradiction_prefilter.py use
the shared version here instead of a third copy) and a tiny best-effort
counter pair for tracking how often a gate's verdict would have agreed
with the real call it might someday skip. Tier 2/3's existing local
copies are left as they are -- this isn't a refactor of already-shipped
code, just where new call sites should reach first.
"""
import json
import logging
import os
import threading
import time

from memory.bus import incr as _bus_incr
from memory.bus import read as _bus_read

logger = logging.getLogger(__name__)

LAYA_MODEL = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")

# How long a failed load is remembered before the next call may retry.
_LOAD_RETRY_SECONDS = float(os.environ.get("LAYA_LOAD_RETRY_SECONDS", "300"))

_DECISION_LOG_PATH = os.environ.get("LAYA_DECISION_LOG", "")
_DECISION_LOG_MAX_BYTES = int(float(os.environ.get("LAYA_DECISION_LOG_MAX_MB", "50")) * 1024 * 1024)
_LOG_FIELD_CHARS = 2000

_agent = None
_agent_lock = threading.Lock()
_last_load_failure = 0.0   # time.monotonic() of the last failed load; 0.0 = none
_log_lock = threading.Lock()
_predict_lock = threading.Lock()   # see module docstring: serialized inference


def get_agent():
    """Returns the process-wide Laya Agent, loading it on first call
    (double-checked locking, so two concurrent early callers don't each
    load their own copy). Returns None if `laya` isn't installed or the
    checkpoint can't be loaded; a failure is remembered for
    _LOAD_RETRY_SECONDS so a broken environment doesn't retry (and
    potentially re-download) on every call."""
    global _agent, _last_load_failure
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        if _last_load_failure and (time.monotonic() - _last_load_failure) < _LOAD_RETRY_SECONDS:
            return None
        try:
            import laya
            _agent = laya.load(LAYA_MODEL)
            _last_load_failure = 0.0
        except Exception as exc:
            _last_load_failure = time.monotonic()
            logger.warning(
                "[laya_gate] could not load Laya checkpoint %r (%s: %s) -- "
                "call sites fail open to their pre-Laya default; next load "
                "attempt in %.0fs.", LAYA_MODEL, exc.__class__.__name__,
                exc, _LOAD_RETRY_SECONDS,
            )
            return None
    return _agent


def preload() -> bool:
    """Loads the checkpoint now (blocking) -- call once at startup, off
    the event loop. Never raises; returns whether Laya is ready."""
    return get_agent() is not None


def chunk(text: str, chars: int, max_chunks: int) -> list:
    """Splits `text` into at most `max_chunks` pieces of `chars`
    characters each (always at least one, even for short text), sized to
    fit the English checkpoint's small state budget -- see module
    docstring. Text beyond chars*max_chunks is not examined."""
    text = text or ""
    pieces = [text[i:i + chars] for i in range(0, len(text), chars)]
    return pieces[:max_chunks] or [text[:chars]]


def clip(text: str, max_chars: int, keep: str = "head") -> str:
    """Fits one field of a multi-field state into `max_chars`.
    keep="head" keeps the start, "tail" keeps the end (use for running
    context where the recent part matters most), "both" keeps the start
    and end with an ellipsis between."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    if keep == "tail":
        return text[-max_chars:]
    if keep == "both":
        head = max_chars // 2
        return text[:head] + " ... " + text[-(max_chars - head - 5):]
    return text[:max_chars]


def _log_decision(site: str, state, result) -> None:
    """Appends one JSON line to LAYA_DECISION_LOG if set. Never raises."""
    if not _DECISION_LOG_PATH:
        return
    try:
        clipped = {
            k: (v[:_LOG_FIELD_CHARS] if isinstance(v, str) else v)
            for k, v in (state.items() if isinstance(state, dict) else [("state", str(state))])
        }
        line = json.dumps({
            "ts": time.time(), "site": site, "model": LAYA_MODEL,
            "state": clipped, "answers": (result or {}).get("answers"),
        }, ensure_ascii=False)
        with _log_lock:
            if (os.path.exists(_DECISION_LOG_PATH)
                    and os.path.getsize(_DECISION_LOG_PATH) >= _DECISION_LOG_MAX_BYTES):
                return
            with open(_DECISION_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        logger.warning("[laya_gate] decision log write failed (%s: %s)",
                        exc.__class__.__name__, exc)


def predict(state, questions, site: str = "unknown"):
    """Runs one Laya forward pass over `state` for the typed `questions`
    dict. Returns the raw `{"answers": {...}, ...}` dict, or None on any
    failure (agent not loaded, malformed input, inference error) --
    never raises; each call site checks for None and uses its own
    pre-Laya fail-open/fail-closed default. `site` only labels the
    optional decision log."""
    agent = get_agent()
    if agent is None:
        return None
    try:
        with _predict_lock:
            result = agent.predict(state, questions)
    except Exception as exc:
        logger.warning(
            "[laya_gate] predict() failed (%s: %s) -- caller falls back "
            "to its own pre-Laya default.", exc.__class__.__name__, exc,
        )
        return None
    _log_decision(site, state, result)
    return result


# ---------------------------------------------------------------------
# Shared gate helpers (Tier 4, 2026-09-29) -- see module docstring above.
# ---------------------------------------------------------------------

_COUNTER_TTL_SECONDS = 60 * 60 * 24 * 30   # 30 days, same as eo/sga.py's own counters


def parse_mode(raw, default: str, label: str = "gate") -> str:
    """Shared on/observe/off parser. An unrecognized value is treated as
    "off" -- a typo must never silently change routing/filtering
    behavior -- with a warning naming which gate it was for."""
    mode = (raw if raw not in (None, "") else default).strip().lower()
    if mode in ("on", "observe", "off"):
        return mode
    logger.warning("[laya_gate] unknown mode %r for %s -- treating as 'off'", raw, label)
    return "off"


def bump_counter(key: str) -> None:
    """Best-effort counter increment for gate observe/on-mode stats.
    Never raises."""
    try:
        _bus_incr(key, ex=_COUNTER_TTL_SECONDS)
    except Exception as exc:
        logger.warning("[laya_gate] counter %s failed (non-fatal): %s", key, exc)


def read_counter(key: str) -> int:
    """Best-effort counter read; returns 0 on any failure (including a
    key that was never bumped)."""
    try:
        return int(_bus_read(key, default=0) or 0)
    except Exception:
        return 0
