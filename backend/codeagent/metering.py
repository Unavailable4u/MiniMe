"""
codeagent/metering.py -- Patch S10: metering inside the loop (and the state the "grant more" path shares).

WHAT THIS IS. The loop (S07, loop.py) calls a Metering object before and after every model call and sends the
device whatever a BudgetPause says; it emits nothing about credits itself. This module is the real Metering:

    make_metering_factory(...)   -> factory(run) -> RunMetering     what AgentLoop(metering_factory=...) takes
    RunMetering.before_call()    the pool check and the per-run ceiling; may raise BudgetPause
    RunMetering.after_call(u)    prices the call, writes ONE ledger row, emits `usage` and `budget_warning`
    RunCeilingPolicy             how big the per-run ceiling and one grant are
    RunBudgetStore               the Redis keys (ceiling, warned flags) that survive a restart
    ledger_run_credits()         the read-only "what has this run cost so far" query

NOT WIRED. Nothing here installs itself and build_default_loop() is unchanged: the later wiring patch passes
`make_metering_factory()` (every default is the real collaborator) to the loop, and replaces hello.py's runner. The
grant route that raises a run's ceiling is api/routes/code_runs.py; it uses RunBudgetStore and RunCeilingPolicy from
here so the two cannot disagree about a key or a number.

    device                      loop.py (S07)                    this module                        others
    ------                      -------------                    -----------                        ------
                                before_call()  ------------->    usage_ledger.check(user)           S01
                                                                 spent >= ceiling ?
                                   BudgetPause  <-------------   notice, BudgetPause
    error quota_exhausted  <--  (the loop sends the error, then paused_budget, fixture 04's order)
                                model call
                                after_call(usage) ---------->    credits_for(...)                   S02
                                                                 usage_ledger.commit(surface='m2c') S01
                                                                 runs.add_progress                  S06
    usage, budget_warning  <--  (through `emit`, which numbers and stores them)

THE RULES (build plan O2, 11.5; feature guide 10; protocol README 4)
  * One pool, shared with the web app. before_call asks usage_ledger.check(user_id); exhausted -> BudgetPause(
    "quota_exhausted", error_code="quota_exhausted", reset_at=...). The loop then sends the `quota_exhausted` error
    (details.reset_at when known) and only then moves the run to paused_budget with the reason quota_exhausted.
  * A per-run ceiling, separate from the pool. Reaching it stops the run the same way (the protocol says
    quota_exhausted covers "the pool or the run ceiling"), with two differences the device can use: the error has no
    reset_at (a run ceiling does not reset), and a `notice` run_creditceiling_reached comes first. A grant raises the
    run's ceiling and nothing else: the pool is only ever read here.
  * The model is never changed here. A refused call is a pause, not a cheaper model (the loop's own chain walk
    announces any class change; this module has no say in which model answers).
  * Metering FAILS OPEN. A check that cannot read the ledger (degraded, or raising) allows the call; a ledger write
    that raises is logged by exception type and the run's total still moves; an emit that raises costs the event,
    nothing else; a Redis that cannot be read keeps the last known ceiling. Only a BudgetPause stops a run, and only
    from numbers this module could read.
  * One ledger row per completed model call, numbers and opaque ids only (user, surface 'm2c', run id, provider,
    model, token counts, credits). A Usage with reported=False writes no row, charges nothing and sends no `usage`
    event (nothing was measured); the run's step counter still moves.

WHAT THE LOOP ASKS FOR AND WHAT LIVES WHERE
  The loop asks the factory for a Metering at the start of EVERY turn and every resume, so the run's running state
  cannot live on the Metering object. It lives in a small per-run record the factory keeps (a bounded LRU, so a long-
  lived process does not grow): credits spent, the warnings already sent, the last pool figures. A record that is
  evicted, or a process that is new, rebuilds itself:
    spent       the ledger's sum of credits for this run id (read-only; a failed read starts at zero with a warning
                and is retried at the next call, never given up on)
    ceiling     the base ceiling from RunCeilingPolicy, or the raised one a grant stored in Redis, whichever is larger
    warned      which scopes were warned about, also from Redis, so a restart does not repeat a warning
  The raised ceiling is read from Redis only when the run is about to stop (spent >= ceiling): a grant made by another
  worker is seen without paying a Redis round trip on every call.

REDIS KEYS (beside run_guard's, which memory/bus.py already exempts from app_slug namespacing; same 6 h TTL)
    run_guard:m2c_ceiling:<run_id>            the raised ceiling, credits, a decimal string
    run_guard:m2c_warned:<scope>:<run_id>     "1" once a warning of that scope (run | account) was sent
  run_manager's cleanup (run_guard.begin_run) does not know these names, so they expire by TTL; a run id is unique
  across accounts and never reused, so a leftover key can only describe its own run.

THE NUMBERS (PRODUCT DECISIONS: change the constants, nothing else)
    RUN_CEILING_FRACTION 0.25   a run may spend a quarter of the account's pool, floor 1 credit. The 25% is this
                                patch's choice and needs a product decision. M2C_RUN_CREDIT_CEILING=<credits>
                                replaces it with an absolute number.
    GRANT_CHUNK_FRACTION 0.25   one grant adds a quarter of the pool (floor 1 credit): the same decision.
    WARNING_PERCENT 80          build plan 11.5.

LOGGING (build plan 11.4, O1). Run id, user id, device id, counts, credit amounts, state and scope words, error codes
and exception TYPES. Never a prompt, a reply, a tool argument or an exception's text (tests/static/
test_metering_static.py reads this file). Every text sent to the device is a constant below.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from codeagent import emitter as emitter_module
from codeagent import runs as runs_module
from codeagent.loop import BudgetPause, Metering
from eo import credits as credits_module
from eo import db, run_guard, usage_ledger

logger = logging.getLogger(__name__)

SURFACE = "m2c"                               # the ledger's surface for this product (usage_ledger.SURFACES)

RUN_CEILING_FRACTION = 0.25                   # PRODUCT DECISION (module docstring)
RUN_CEILING_FLOOR = 1.0                       # credits
GRANT_CHUNK_FRACTION = 0.25                   # PRODUCT DECISION
GRANT_CHUNK_FLOOR = 1.0                       # credits
WARNING_PERCENT = 80
ENV_RUN_CEILING = "M2C_RUN_CREDIT_CEILING"    # an absolute number of credits
WARNING_SCOPES = ("run", "account")
CEILING_TTL_SECONDS = int(getattr(run_guard, "_TTL", 6 * 60 * 60))    # the same style as run_guard's keys
DEFAULT_MAX_STATES = 4096

REASON_QUOTA_EXHAUSTED = "quota_exhausted"    # state_changed reason and error code (protocol README 4)
NOTICE_RUN_CEILING = "run_credit_ceiling_reached"
MSG_RUN_CEILING = "This run reached its credit limit. You can grant it more credits to continue."

_CREDIT_DECIMALS = 6                          # usage_ledger.credits is numeric(18, 6)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")       # protocol/v1/run_event.json usage.model
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?"
                           r"(Z|[+-][0-9]{2}:[0-9]{2})$")             # protocol/v1/common.json timestamp
_EPSILON = 1e-9


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _positive(value) -> "float | None":
    """A finite number above zero, else None (booleans, NaN, text and None are all None)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _non_negative(value) -> "float | None":
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _credits(value) -> float:
    number = _non_negative(value)
    return 0.0 if number is None else round(number, _CREDIT_DECIMALS)


def _tokens(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _percent(spent: float, of: float) -> int:
    """Whole percent, 0..100 (the schema's range). `of` is above zero."""
    return max(0, min(100, int(math.floor(spent * 100.0 / of + _EPSILON))))


def _timestamp(value) -> "str | None":
    """A protocol timestamp, or None: an error's details.reset_at that fails the schema would be refused."""
    return value if isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value) else None


def _label(value, fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and value else fallback


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _blocking(fn, *args, **kwargs):
    """Run an injected callable: a coroutine function is awaited, a plain one runs off the event loop (the ledger, the
    run store and Redis are synchronous)."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


# ---------------------------------------------------------------------
# The numbers: how big a run's ceiling and a grant are
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class RunCeilingPolicy:
    """The per-run credit ceiling and the size of one grant.

    base(pool)   fixed (injected), else the M2C_RUN_CREDIT_CEILING environment variable (an absolute number of
                 credits), else RUN_CEILING_FRACTION of the account's pool with a floor of RUN_CEILING_FLOOR; None
                 when none of those is known (no pool could be read and nothing is configured)
    chunk(pool)  chunk_fixed (injected), else GRANT_CHUNK_FRACTION of the pool with a floor
    A value that is not a finite number above zero is ignored, never trusted."""
    fixed: "float | None" = None
    fraction: float = RUN_CEILING_FRACTION
    floor: float = RUN_CEILING_FLOOR
    chunk_fixed: "float | None" = None
    chunk_fraction: float = GRANT_CHUNK_FRACTION
    chunk_floor: float = GRANT_CHUNK_FLOOR
    environ: "Mapping[str, str] | None" = None

    def base(self, pool) -> "float | None":
        fixed = _positive(self.fixed)
        if fixed is not None:
            return fixed
        environ = self.environ if self.environ is not None else os.environ
        configured = _positive(environ.get(ENV_RUN_CEILING))
        if configured is not None:
            return configured
        known = _non_negative(pool)
        if known is None:
            return None
        return round(max(self.floor, known * self.fraction), _CREDIT_DECIMALS)

    def chunk(self, pool) -> float:
        fixed = _positive(self.chunk_fixed)
        if fixed is not None:
            return fixed
        known = _non_negative(pool)
        if known is None:
            return self.chunk_floor
        return round(max(self.chunk_floor, known * self.chunk_fraction), _CREDIT_DECIMALS)


# ---------------------------------------------------------------------
# Redis: the raised ceiling and the warnings already sent
# ---------------------------------------------------------------------

def _ceiling_key(run_id: str) -> str:
    return f"run_guard:m2c_ceiling:{run_id}"


def _warned_key(scope: str, run_id: str) -> str:
    return f"run_guard:m2c_warned:{scope}:{run_id}"


class RunBudgetStore:
    """The two small Redis facts about a run that must outlive a process. Every method may raise (Redis is
    down): callers decide what that means (metering keeps its last value; the grant route fails closed).

    `redis` is a callable returning the client; the default is run_guard's, looked up at every call, so a test (or the
    process) that swaps run_guard's client swaps this one too."""

    def __init__(self, redis: "Callable[[], Any] | None" = None, ttl: int = CEILING_TTL_SECONDS):
        self._redis = redis
        self._ttl = int(ttl)

    def _client(self):
        return self._redis() if self._redis is not None else run_guard._redis()

    @staticmethod
    def _text(raw) -> "str | None":
        if raw is None:
            return None
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    def ceiling(self, run_id: str) -> "float | None":
        """The raised ceiling a grant stored, or None (never granted, expired, or not a usable number)."""
        return _positive(self._text(self._client().get(_ceiling_key(run_id))))

    def set_ceiling(self, run_id: str, value: float) -> None:
        number = _positive(value)
        if number is None:
            raise ValueError("a ceiling must be a finite number above zero")
        self._client().set(_ceiling_key(run_id), repr(round(number, _CREDIT_DECIMALS)), ex=self._ttl)

    def restore_ceiling(self, run_id: str, previous: "float | None") -> None:
        """Put back what set_ceiling replaced (a grant that was undone). None removes the key."""
        if previous is None:
            self._client().delete(_ceiling_key(run_id))
        else:
            self.set_ceiling(run_id, previous)

    def warned(self, run_id: str) -> set:
        client = self._client()
        return {scope for scope in WARNING_SCOPES if self._text(client.get(_warned_key(scope, run_id))) == "1"}

    def mark_warned(self, run_id: str, scope: str) -> None:
        self._client().set(_warned_key(scope, run_id), "1", ex=self._ttl)


# ---------------------------------------------------------------------
# The ledger, read-only: what has this run cost so far
# ---------------------------------------------------------------------

_RUN_CREDITS_SQL = ("select coalesce(sum(credits), 0) as credits from usage_ledger "
                    "where user_id = %s and run_id = %s and surface = 'm2c'")


def ledger_run_credits(user_id: str, run_id: str) -> float:
    """Credits the ledger holds for this run (the account's own rows, surface 'm2c'). Read-only. Raises on a database
    failure: the caller decides (metering fails open to zero). usage_ledger_user_created_idx narrows the scan to the
    account's rows; a run's share is read once per process, not per call."""
    with db.cursor(user_id=user_id) as cur:
        cur.execute(_RUN_CREDITS_SQL, (user_id, run_id))
        row = cur.fetchone()
    return _credits(row["credits"] if row else 0)


# ---------------------------------------------------------------------
# Per-run state and the factory
# ---------------------------------------------------------------------

class _RunState:
    """What one run's metering knows, across turns. Plain data: no lock, no event loop."""

    __slots__ = ("spent", "hydrated", "raised", "warned", "warned_loaded", "pool", "used", "resets_at")

    def __init__(self):
        self.spent = 0.0                 # credits this run has used (ledger sum at first sight + this process's calls)
        self.hydrated = False            # the ledger's figure has been read
        self.raised: "float | None" = None       # the highest raised ceiling seen in Redis
        self.warned: set = set()
        self.warned_loaded = False
        self.pool: "float | None" = None         # the last pool figures a successful check gave
        self.used: "float | None" = None
        self.resets_at: "str | None" = None


@dataclass
class _Deps:
    emit: Callable
    check: Callable
    commit: Callable
    credits: Callable
    progress: Callable
    ledger_sum: Callable
    store: RunBudgetStore
    policy: RunCeilingPolicy


async def _default_emit(run_id: str, t: str, payload: dict):
    """The process emitter, looked up at every call (like build_default_loop does), so a swapped one is used."""
    return await emitter_module.get_emitter().emit(run_id, t, payload)


def make_metering_factory(*, emit: "Callable | None" = None, check: "Callable | None" = None,
                          commit: "Callable | None" = None, credits: "Callable | None" = None,
                          progress: "Callable | None" = None, ledger_sum: "Callable | None" = None,
                          store: "RunBudgetStore | None" = None, policy: "RunCeilingPolicy | None" = None,
                          clock: "Callable[[], datetime] | None" = None,
                          max_states: int = DEFAULT_MAX_STATES) -> "Callable[[Any], RunMetering]":
    """The `metering_factory` AgentLoop takes: factory(run) -> a Metering for that run. Every collaborator is
    injectable, so tests need no database and no Redis; the defaults are the real ones.

    emit        async (run_id, t, payload): RunEmitter.emit (default: the process emitter)
    check       (user_id) -> usage_ledger.CheckResult (default: usage_ledger.check at `clock`'s time)
    commit      usage_ledger.commit's keywords -> dict
    credits     S02's credits_for(provider, model, input, cached_input, output) -> float
    progress    runs.add_progress(user_id, run_id, steps=, credits=): the run row's informational counters
    ledger_sum  (user_id, run_id) -> credits the ledger holds for the run
    store       RunBudgetStore (Redis); policy: RunCeilingPolicy
    clock       () -> an aware datetime; only the default check uses it
    max_states  how many runs' state one process keeps (a larger number than the open runs it serves)"""
    if not isinstance(max_states, int) or isinstance(max_states, bool) or max_states < 1:
        raise ValueError("max_states must be a positive integer")
    now = clock or _utcnow
    deps = _Deps(
        emit=emit or _default_emit,
        check=check or (lambda user_id: usage_ledger.check(user_id, now=now())),
        commit=commit or usage_ledger.commit,
        credits=credits or credits_module.credits_for,
        progress=progress or runs_module.add_progress,
        ledger_sum=ledger_sum or ledger_run_credits,
        store=store or RunBudgetStore(),
        policy=policy or RunCeilingPolicy())
    states: "OrderedDict[str, _RunState]" = OrderedDict()

    def factory(run) -> "RunMetering":
        state = states.get(run.id)
        if state is None:
            state = states[run.id] = _RunState()
            while len(states) > max_states:
                states.popitem(last=False)          # the oldest: it rebuilds itself from the ledger and Redis
        else:
            states.move_to_end(run.id)
        return RunMetering(deps, run, state)

    return factory


# ---------------------------------------------------------------------
# The Metering
# ---------------------------------------------------------------------

class RunMetering(Metering):
    """The loop's Metering for one run (see the module docstring). Built by the factory for a turn; the state it
    reads and writes outlives it."""

    def __init__(self, deps: _Deps, run, state: _RunState):
        self._d = deps
        self._s = state
        self.run_id = run.id
        self.user_id = run.user_id
        self.device_id = getattr(run, "device_id", None)

    # -- what a test or a caller may look at ---------------------------------------------------------
    @property
    def spent(self) -> float:
        return round(self._s.spent, _CREDIT_DECIMALS)

    @property
    def ceiling(self) -> "float | None":
        """The ceiling in force as far as this process knows (no Redis read)."""
        return self._effective()

    def _effective(self) -> "float | None":
        known = [v for v in (self._d.policy.base(self._s.pool), self._s.raised) if v is not None]
        return max(known) if known else None

    # =========================================================================================
    # before_call
    # =========================================================================================

    async def before_call(self) -> None:
        state = self._s
        await self._load_warned()
        await self._hydrate()
        check = await self._pool()
        if check is not None:
            state.pool, state.used = check.pool, check.used
            state.resets_at = _timestamp(check.resets_at)
            await self._warn_account()
            if not check.allowed:
                logger.info("[metering] pool exhausted run=%s user=%s device=%s", self.run_id, self.user_id, self.device_id)
                raise BudgetPause(REASON_QUOTA_EXHAUSTED, error_code=REASON_QUOTA_EXHAUSTED, reset_at=state.resets_at)
        ceiling = await self._ceiling_now()
        await self._warn_run(ceiling)
        if ceiling is not None and state.spent >= ceiling - _EPSILON:
            await self._emit("run_event", {"type": "notice", "code": NOTICE_RUN_CEILING, "severity": "warning",
                                           "message": MSG_RUN_CEILING})
            logger.info("[metering] run ceiling reached run=%s user=%s device=%s spent=%s ceiling=%s",
                        self.run_id, self.user_id, self.device_id, self.spent, ceiling)
            raise BudgetPause(REASON_QUOTA_EXHAUSTED, error_code=REASON_QUOTA_EXHAUSTED)

    async def _pool(self):
        """The ledger's check, or None when the pool cannot be judged (degraded, or the check itself failed): the call
        is then allowed (metering fails open)."""
        try:
            result = await _blocking(self._d.check, self.user_id)
        except Exception as exc:
            logger.warning("[metering] pool check failed, allowing the call run=%s user=%s error=%s",
                           self.run_id, self.user_id, type(exc).__name__)
            return None
        if result is None or getattr(result, "degraded", False):
            logger.warning("[metering] pool could not be read, allowing the call run=%s user=%s", self.run_id, self.user_id)
            return None
        return result

    async def _hydrate(self) -> None:
        """Start the run's total from the ledger (a resumed run, a new process, an evicted record). A failed read
        leaves the total as it is, with a warning, and is tried again at the next call."""
        state = self._s
        if state.hydrated:
            return
        try:
            total = _credits(await _blocking(self._d.ledger_sum, self.user_id, self.run_id))
        except Exception as exc:
            logger.warning("[metering] could not read the run's credits from the ledger, counting from here run=%s "
                           "user=%s error=%s", self.run_id, self.user_id, type(exc).__name__)
            return
        state.spent = max(state.spent, total)
        state.hydrated = True

    async def _load_warned(self) -> None:
        state = self._s
        if state.warned_loaded:
            return
        state.warned_loaded = True
        try:
            state.warned |= set(await _blocking(self._d.store.warned, self.run_id))
        except Exception as exc:
            logger.warning("[metering] could not read the warnings already sent run=%s error=%s",
                           self.run_id, type(exc).__name__)

    async def _ceiling_now(self) -> "float | None":
        """The ceiling in force. Redis is asked only when the run is about to stop (or no ceiling is known): a grant
        another worker made is then seen; a Redis that fails keeps what this process already knew."""
        state = self._s
        ceiling = self._effective()
        if ceiling is None or state.spent >= ceiling - _EPSILON:
            try:
                raised = await _blocking(self._d.store.ceiling, self.run_id)
            except Exception as exc:
                logger.warning("[metering] could not read the raised ceiling run=%s error=%s",
                               self.run_id, type(exc).__name__)
                raised = None
            if raised is not None:
                state.raised = raised if state.raised is None else max(state.raised, raised)
                ceiling = self._effective()
        return ceiling

    # =========================================================================================
    # after_call
    # =========================================================================================

    async def after_call(self, usage) -> None:
        state = self._s
        if usage is None or not getattr(usage, "reported", False):
            await self._count_step(0.0)               # a call happened; nothing was measured, so nothing is charged
            return
        provider, model = _label(usage.provider), _label(usage.model)
        tokens_in, tokens_cached, tokens_out = _tokens(usage.input), _tokens(usage.cached_input), _tokens(usage.output)
        try:
            charge = _credits(self._d.credits(provider, model, tokens_in, tokens_cached, tokens_out))
        except Exception as exc:
            logger.warning("[metering] could not price a call, charging 0 run=%s user=%s error=%s",
                           self.run_id, self.user_id, type(exc).__name__)
            charge = 0.0
        state.spent = round(state.spent + charge, _CREDIT_DECIMALS)      # the run's total moves whatever the ledger does
        if state.used is not None:
            state.used = round(state.used + charge, _CREDIT_DECIMALS)    # an estimate until the next check
        try:
            await _blocking(self._d.commit, user_id=self.user_id, surface=SURFACE, run_id=self.run_id,
                            provider=provider, model=model, input_tokens=tokens_in,
                            cached_input_tokens=tokens_cached, output_tokens=tokens_out, credits=charge)
        except Exception as exc:
            logger.error("[metering] ledger write failed run=%s user=%s device=%s credits=%s error=%s",
                         self.run_id, self.user_id, self.device_id, charge, type(exc).__name__)
        await self._count_step(charge)
        await self._emit("run_event", {
            "type": "usage", "model": model if _MODEL_RE.fullmatch(model) else "unknown",
            "input_tokens": tokens_in, "cached_input_tokens": tokens_cached, "output_tokens": tokens_out,
            "credits": charge, "run_credits_total": self.spent})
        logger.info("[metering] call metered run=%s user=%s device=%s input=%d cached=%d output=%d credits=%s total=%s",
                    self.run_id, self.user_id, self.device_id, tokens_in, tokens_cached, tokens_out, charge, self.spent)
        await self._warn_run(self._effective())
        await self._warn_account()

    async def _count_step(self, charge: float) -> None:
        """The run row's informational counters (steps and credits). usage_ledger is the metering truth."""
        try:
            await _blocking(self._d.progress, self.user_id, self.run_id, steps=1, credits=charge)
        except Exception as exc:
            logger.warning("[metering] run counters not updated run=%s user=%s error=%s",
                           self.run_id, self.user_id, type(exc).__name__)

    # =========================================================================================
    # Warnings and events
    # =========================================================================================

    async def _warn_run(self, ceiling: "float | None") -> None:
        if ceiling is not None and ceiling > 0:
            await self._maybe_warn("run", _percent(self._s.spent, ceiling), None)

    async def _warn_account(self) -> None:
        state = self._s
        if state.pool is not None and state.pool > 0 and state.used is not None:
            await self._maybe_warn("account", _percent(state.used, state.pool), state.resets_at)

    async def _maybe_warn(self, scope: str, percent: int, reset_at: "str | None") -> None:
        """budget_warning at WARNING_PERCENT, once per scope per run: the flag is set before the send, so a failed
        send is never retried into a repeat (and a restart finds the flag in Redis)."""
        state = self._s
        if percent < WARNING_PERCENT or scope in state.warned:
            return
        state.warned.add(scope)
        try:
            await _blocking(self._d.store.mark_warned, self.run_id, scope)
        except Exception as exc:
            logger.warning("[metering] could not record a warning run=%s scope=%s error=%s",
                           self.run_id, scope, type(exc).__name__)
        payload = {"scope": scope, "spent_pct": percent}
        if reset_at:
            payload["reset_at"] = reset_at
        await self._emit("budget_warning", payload)
        logger.info("[metering] budget warning run=%s user=%s scope=%s pct=%d", self.run_id, self.user_id, scope, percent)

    async def _emit(self, t: str, payload: dict) -> bool:
        """Send one stream message. A failure costs that message only (the stream's own faults surface at the loop's
        next emit, which handles them)."""
        try:
            result = self._d.emit(self.run_id, t, payload)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            logger.warning("[metering] could not send a %s run=%s user=%s error=%s",
                           t, self.run_id, self.user_id, type(exc).__name__)
            return False
