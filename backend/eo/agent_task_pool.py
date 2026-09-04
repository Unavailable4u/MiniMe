"""
eo/agent_task_pool.py — dedicated thread pool for long-running agent
task execution, separate from the AnyIO thread limiter that backs
Starlette's run_in_threadpool (api/server.py's APP_THREAD_POOL_SIZE).

perf audit §4.6 / priority #9, item 6 (B1 from the wider audit): before
this module existed, every `def` route in api/routes/tasks.py --
post_task, post_task_preview, post_task_confirm, post_resume,
post_task_from_template -- ran on the exact same 20-token AnyIO thread
limiter as every cheap, fast sync route elsewhere in the app (auth
checks, quota reads, role/skill lookups, etc.). A handful of
multi-minute agent runs could fully occupy that limiter, and every
unrelated fast request would queue behind them -- the two workloads
have completely different concurrency needs (agent runs: few,
long-lived, CPU/IO-light-but-slow; fast routes: many, short-lived) but
shared one undifferentiated budget. Sizing that shared budget correctly
for both was never really possible: big enough for fast-route burst
traffic meant leaving room for a fast route to get starved by long
agent runs, and small enough to bound agent-run concurrency meant
capping fast-route throughput too.

This module gives agent task execution its own pool, sized
independently via AGENT_TASK_POOL_SIZE, so api/server.py's
APP_THREAD_POOL_SIZE goes back to serving only what it was actually
sized for (see that file's own comment) -- fast, DB-pool-bound sync
routes -- while agent runs get a separate, deliberately smaller budget
that reflects how many of them should realistically run at once, not
how many threads happen to be free on the shared limiter.

Routes call run_in_agent_pool(fn, *args, **kwargs) from an `async def`
handler (see api/routes/tasks.py) instead of being a plain `def` that
Starlette would otherwise silently hand to the shared limiter.

Same "counters + Sentry breadcrumb on saturation" shape as eo/db.py's
_getconn_with_timeout()/get_pool_stats() -- deliberately mirrored so
this pool and the DB pool are read the same way (see
api/routes/system.py's /api/system/agent-pool-stats, alongside
/api/system/db-pool-stats) when deciding whether either ceiling needs
raising.
"""
import asyncio
import contextvars
import os
import time
from concurrent.futures import ThreadPoolExecutor

import sentry_sdk

from memory.bus import incr as _bus_incr
from memory.bus import read as _bus_read

# How many agent tasks (run_task/preview_task/confirm_task/resume_graph/
# run_task_from_template) may execute concurrently, independent of
# api/server.py's APP_THREAD_POOL_SIZE. Deliberately much smaller than
# that default (20) -- these are long-lived runs, not brief request
# handlers, and each one already fans out its own LLM calls (rate-limited
# separately via utils/llm_client.py's rate_ledger); this bounds how many
# can be *mid-run* at once, not how much LLM throughput each gets.
# Tune based on /api/system/agent-pool-stats' queued/saturated numbers
# under real traffic, same as DB_POOL_MAX / APP_THREAD_POOL_SIZE.
AGENT_TASK_POOL_SIZE = int(os.getenv("AGENT_TASK_POOL_SIZE", "8"))

_executor = ThreadPoolExecutor(
    max_workers=AGENT_TASK_POOL_SIZE,
    thread_name_prefix="agent-task",
)

# Same key-naming/versioning convention as eo/db.py's _STATS_*_KEY
# constants -- a global, Redis-backed, cross-restart tally rather than
# in-process counters that reset on every deploy.
_STATS_STARTED_KEY = "agent_pool:stats:v1:started"
_STATS_QUEUED_KEY = "agent_pool:stats:v1:queued_on_submit"
_STATS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days, matches eo/db.py

# Below this measured queue-wait, a submission counts as "started
# immediately" rather than "queued" -- mirrors eo/db.py's
# _IMMEDIATE_THRESHOLD_SECONDS bucketing rationale exactly (a few
# milliseconds of scheduling jitter isn't the same signal as genuinely
# waiting behind a full pool).
_IMMEDIATE_THRESHOLD_SECONDS = 0.01

# Only fire one Sentry breadcrumb per this many seconds while the pool
# stays saturated, so a long queue-up doesn't spam Sentry once per
# request -- same "signal, not noise" reasoning as everywhere else this
# codebase rate-limits its own alerting (see utils/llm_client.py).
_SATURATION_ALERT_COOLDOWN_SECONDS = 60
_last_saturation_alert_at = 0.0


async def run_in_agent_pool(fn, /, *args, **kwargs):
    """Runs fn(*args, **kwargs) on the dedicated agent-task executor and
    awaits the result, instead of Starlette's default run_in_threadpool
    (which would otherwise share api/server.py's APP_THREAD_POOL_SIZE
    limiter with every fast sync route in the app -- see this module's
    own docstring for why that's the problem this exists to fix).

    Call sites: api/routes/tasks.py's post_task / post_task_preview /
    post_task_confirm / post_resume / post_task_from_template, each now
    `async def` so this can be awaited directly instead of the route
    itself running synchronously on the shared limiter.

    Perf audit follow-up (registry.py N+1, part 2): a bare
    `loop.run_in_executor(executor, func)` does NOT propagate the
    calling coroutine's contextvars into the executor thread -- unlike
    Starlette's own run_in_threadpool (AnyIO) and unlike
    asyncio.to_thread() (3.9+), neither of which this codebase's sync
    routes used to go through by accident, this call was a bare
    `executor.submit(func)` with no context wrapper. Confirmed
    empirically: a contextvar set before the call reads back as its
    *default* inside the worker thread, not the value that was set.

    That silently broke eo/registry.py's `_role_prompts_cache_ctx`
    (the per-run role-prompts cache _load_prompts() checks before
    hitting Redis -- see that module's own comment) for every real
    task, since staff_task()/generic_worker.py's role-prompt reads all
    happen *inside* run_task() et al, which is to say inside whatever
    this function hands to the executor. Every hired role went back to
    a fresh Redis read, the exact N+1 the original registry.py fix
    eliminated -- just relocated here. It also affects
    memory/bus.py's `_app_slug_ctx` the same way (same mechanism, same
    blind spot; more of a correctness concern there than a perf one).

    Fix: capture the caller's context with contextvars.copy_context()
    and run fn *inside* that captured context via ctx.run(), so every
    contextvar the caller had set (registry.py's cache, bus.py's
    app-slug scope, anything else using this pattern) is visible to
    code running on the dedicated executor thread, exactly as it would
    have been under Starlette's run_in_threadpool.
    """
    global _last_saturation_alert_at

    t0 = time.monotonic()
    active = len(_executor._threads) if _executor._threads else 0
    queue_depth = _executor._work_queue.qsize()
    likely_queued = active >= AGENT_TASK_POOL_SIZE and queue_depth > 0

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    future = loop.run_in_executor(_executor, lambda: ctx.run(fn, *args, **kwargs))

    if likely_queued:
        now = time.monotonic()
        if now - _last_saturation_alert_at > _SATURATION_ALERT_COOLDOWN_SECONDS:
            _last_saturation_alert_at = now
            sentry_sdk.capture_message(
                f"eo.agent_task_pool: pool saturated at submit time "
                f"(AGENT_TASK_POOL_SIZE={AGENT_TASK_POOL_SIZE}, "
                f"queue_depth={queue_depth}) -- new task queued behind "
                f"running ones",
                level="warning",
            )

    try:
        result = await future
    finally:
        waited = time.monotonic() - t0
        stats_key = _STATS_QUEUED_KEY if waited >= _IMMEDIATE_THRESHOLD_SECONDS else _STATS_STARTED_KEY
        _bus_incr(stats_key, ex=_STATS_TTL_SECONDS)

    return result


def get_agent_pool_stats() -> dict:
    """Mirrors eo/db.py's get_pool_stats() shape -- see
    api/routes/system.py's /api/system/agent-pool-stats. Read this
    before raising or lowering AGENT_TASK_POOL_SIZE: a queued count
    that's high relative to started_immediately means agent runs are
    routinely waiting on each other, which is either "working as
    designed, this is the deliberate concurrency cap" or "raise the
    cap" depending on how long the queue-waits actually run relative to
    an individual task's own duration -- this module doesn't have
    enough information to tell those apart on its own, hence surfacing
    the raw counts rather than guessing at a threshold.
    """
    started = _bus_read(_STATS_STARTED_KEY, default=0) or 0
    queued = _bus_read(_STATS_QUEUED_KEY, default=0) or 0
    total = started + queued
    return {
        "started_immediately": started,
        "queued_on_submit": queued,
        "queued_rate": (queued / total) if total else None,
        "agent_task_pool_size": AGENT_TASK_POOL_SIZE,
        "in_process_active_threads": len(_executor._threads) if _executor._threads else 0,
        "in_process_queue_depth": _executor._work_queue.qsize(),
    }
