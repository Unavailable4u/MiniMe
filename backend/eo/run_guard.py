"""
eo/run_guard.py -- per-run safety rails for the agent pipeline.

Why this exists (audit, 2026-09-25): a task that hit repeated errors kept
running in the background long after the HTTP request had already been
answered (or had timed out), burning rate limit on roles that could not
succeed, and the macro-loop then re-ran the whole graph. Three independent
rails fix that, all keyed by session_id and all cheap to check:

1. CANCEL FLAG  -- request_cancel(session_id, reason). Checked at every LLM
   chain step (utils/llm_client.py) and at every role boundary
   (eo/executor.py). When the HTTP layer gives up on a request (deadline,
   client stop button) it sets this flag so the orphaned worker thread
   actually stops instead of running for another half hour.

2. FAILURE BREAKER -- record_role_outcome(). N consecutive failed roles (or M
   failed roles in total) trips the breaker. The executor then pauses the run
   for manual approval instead of grinding through the rest of the graph.

3. TOKEN BUDGET -- like an agent hitting its tool-call limit and asking the
   user whether to keep going. Every LLM call's real token usage is added to
   the session's counter (utils/llm_client.py::_log_usage). When the counter
   crosses the soft budget the executor pauses at the next role boundary
   (reason "token_budget_exceeded"); POST /api/resume {"action": "approve"}
   grants another chunk and continues. A hard ceiling (soft x HARD_FACTOR)
   stops even a single runaway role mid-flight.

Storage: memory.bus (Upstash Redis), keys under "run_guard:" which
memory/bus.py exempts from app_slug namespacing -- the HTTP request that sets
a cancel flag and the worker thread that reads it run under different
app_slug contexts and would otherwise look at different keys. Hot-path
checks are served from a small in-process cache first so a chain step never
pays a Redis round trip just to look at a flag.
"""
import os
import threading
import time

DEFAULT_TOKEN_BUDGET = int(os.getenv("TASK_TOKEN_BUDGET", "120000"))       # 0 disables the token stop
TOKEN_HARD_LIMIT_FACTOR = float(os.getenv("TASK_TOKEN_HARD_FACTOR", "2.0"))
MAX_CONSECUTIVE_ROLE_FAILURES = int(os.getenv("TASK_MAX_CONSECUTIVE_FAILURES", "3"))
MAX_TOTAL_ROLE_FAILURES = int(os.getenv("TASK_MAX_TOTAL_FAILURES", "6"))

_TTL = 60 * 60 * 6          # every key self-expires; a dead run never leaves state behind forever
_LOCAL_TTL = 1.5            # seconds a remote flag read is trusted in-process

_lock = threading.Lock()
_local_cancel: dict = {}    # session_id -> reason (authoritative in-process, set synchronously)
_remote_seen: dict = {}     # session_id -> (checked_at, reason_or_None)
_local_tokens: dict = {}    # session_id -> int (mirror; Redis is the source of truth across processes)


def _k(name: str, session_id: str) -> str:
    return f"run_guard:{name}:{session_id}"


def _redis():
    from memory.bus import redis  # deferred: keeps this module importable in unit tests without Upstash env
    return redis


def _safe(fn, default=None):
    """Bookkeeping must never take down the real call (same contract as
    llm_client._set_cooldown)."""
    try:
        return fn()
    except Exception as exc:
        print(f"  [run_guard] bookkeeping call failed (non-fatal): {exc}")
        return default


# ---------------------------------------------------------------- lifecycle

def begin_run(session_id: str) -> None:
    """Called once at the start of a FRESH task (not on resume): clears every
    rail so a previous run's cancel flag / failures / token count can't leak
    into this one."""
    if not session_id:
        return
    with _lock:
        _local_cancel.pop(session_id, None)
        _remote_seen.pop(session_id, None)
        _local_tokens.pop(session_id, None)

    def _clear():
        r = _redis()
        for name in ("cancel", "tokens", "budget", "fail_streak", "fail_total", "tripped"):
            r.delete(_k(name, session_id))
    _safe(_clear)


# ------------------------------------------------------------------- cancel

def request_cancel(session_id: str, reason: str = "cancelled") -> None:
    if not session_id:
        return
    with _lock:
        _local_cancel[session_id] = reason
    _safe(lambda: _redis().set(_k("cancel", session_id), reason, ex=_TTL))
    print(f"  [run_guard] cancel requested for session {session_id}: {reason}")


def cancel_reason(session_id: str):
    """Reason string if this session was cancelled, else None."""
    if not session_id:
        return None
    with _lock:
        if session_id in _local_cancel:
            return _local_cancel[session_id]
        seen = _remote_seen.get(session_id)
    now = time.monotonic()
    if seen and now - seen[0] < _LOCAL_TTL:
        return seen[1]
    raw = _safe(lambda: _redis().get(_k("cancel", session_id)))
    reason = raw.decode() if isinstance(raw, bytes) else raw
    with _lock:
        _remote_seen[session_id] = (now, reason)
    return reason or None


# ------------------------------------------------------------ token budget

def token_budget(session_id: str) -> int:
    """This session's current SOFT budget (base + any manual grants). 0 = disabled."""
    if DEFAULT_TOKEN_BUDGET <= 0:
        return 0
    raw = _safe(lambda: _redis().get(_k("budget", session_id)))
    try:
        return int(raw) if raw is not None else DEFAULT_TOKEN_BUDGET
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_BUDGET


def add_tokens(session_id: str, n) -> int:
    """Adds real usage (from the provider's own usage object) to the session
    counter and returns the new total."""
    if not session_id or not n:
        return tokens_used(session_id) if session_id else 0
    n = int(n)
    with _lock:
        _local_tokens[session_id] = _local_tokens.get(session_id, 0) + n
        local_total = _local_tokens[session_id]

    def _incr():
        r = _redis()
        total = r.incrby(_k("tokens", session_id), n)
        r.expire(_k("tokens", session_id), _TTL)
        return int(total)
    remote_total = _safe(_incr)
    return max(local_total, remote_total or 0)


def tokens_used(session_id: str) -> int:
    if not session_id:
        return 0
    raw = _safe(lambda: _redis().get(_k("tokens", session_id)))
    try:
        remote = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        remote = 0
    with _lock:
        return max(remote, _local_tokens.get(session_id, 0))


def over_token_budget(session_id: str) -> bool:
    budget = token_budget(session_id)
    return bool(budget) and tokens_used(session_id) >= budget


def over_hard_limit(session_id: str) -> bool:
    budget = token_budget(session_id)
    return bool(budget) and tokens_used(session_id) >= int(budget * TOKEN_HARD_LIMIT_FACTOR)


# --------------------------------------------------------- failure breaker

def record_role_outcome(session_id: str, ok: bool, role: str = None) -> "str | None":
    """Feeds the breaker. Returns a human-readable reason if the breaker is
    (now) tripped, else None. A success resets the consecutive streak."""
    if not session_id:
        return None
    if ok:
        _safe(lambda: _redis().set(_k("fail_streak", session_id), 0, ex=_TTL))
        return breaker_reason(session_id)

    def _bump():
        r = _redis()
        streak = int(r.incr(_k("fail_streak", session_id)))
        total = int(r.incr(_k("fail_total", session_id)))
        r.expire(_k("fail_streak", session_id), _TTL)
        r.expire(_k("fail_total", session_id), _TTL)
        return streak, total
    res = _safe(_bump)
    if not res:
        return None
    streak, total = res
    reason = None
    if streak >= MAX_CONSECUTIVE_ROLE_FAILURES:
        reason = f"{streak} roles in a row failed (last: {role or 'unknown'})"
    elif total >= MAX_TOTAL_ROLE_FAILURES:
        reason = f"{total} roles have failed in this run (last: {role or 'unknown'})"
    if reason:
        _safe(lambda: _redis().set(_k("tripped", session_id), reason, ex=_TTL))
        print(f"  [run_guard] failure breaker TRIPPED for session {session_id}: {reason}")
    return reason


def breaker_reason(session_id: str):
    if not session_id:
        return None
    raw = _safe(lambda: _redis().get(_k("tripped", session_id)))
    return (raw.decode() if isinstance(raw, bytes) else raw) or None


# ------------------------------------------------------------ manual grant

def grant_more(session_id: str) -> dict:
    """The user approved a paused run: give it another token chunk, and clear
    the breaker so the resumed run gets a fresh chance."""
    if not session_id:
        return {}
    new_budget = 0
    if DEFAULT_TOKEN_BUDGET > 0:
        # Base the new ceiling on what was actually used, so a resume after a
        # hard stop still gets a full chunk of headroom rather than ~0.
        new_budget = max(token_budget(session_id), tokens_used(session_id)) + DEFAULT_TOKEN_BUDGET
        _safe(lambda: _redis().set(_k("budget", session_id), new_budget, ex=_TTL))

    def _reset():
        r = _redis()
        r.delete(_k("fail_streak", session_id))
        r.delete(_k("fail_total", session_id))
        r.delete(_k("tripped", session_id))
    _safe(_reset)
    with _lock:
        _local_cancel.pop(session_id, None)
        _remote_seen.pop(session_id, None)
    _safe(lambda: _redis().delete(_k("cancel", session_id)))
    return {"token_budget": new_budget, "tokens_used": tokens_used(session_id)}


# ------------------------------------------------------------ enforcement

def raise_if_stopped(session_id: str) -> None:
    """Hard-stop check used inside the LLM call path: raises RunStopped for an
    explicit cancel or a blown HARD token ceiling. (The SOFT budget and the
    failure breaker are handled at role boundaries by the executor, where a
    resumable snapshot can be written.)"""
    if not session_id:
        return
    reason = cancel_reason(session_id)
    if reason:
        from utils.llm_client import RunStopped
        raise RunStopped(f"run stopped: {reason}", reason=reason)
    if over_hard_limit(session_id):
        from utils.llm_client import RunStopped
        used = tokens_used(session_id)
        raise RunStopped(
            f"token limit reached ({used:,} tokens used); send the request again to continue",
            reason="token_hard_limit",
        )
