"""
eo/db.py — shared Postgres connection pool for Part 8.2's migration.

Every store module that used to read/write JSON files under data/chats/
(chat_store.py, chat_workspace.py, memory_batch.py) now goes through this
module instead of touching files directly. This is the ONLY place that
knows about DATABASE_URL or the connection pool — nothing else should
import psycopg directly.

Connects via DATABASE_URL. As of migration 0003 (A1 -- real per-user
RLS), DATABASE_URL must point at the low-privilege `minime_app` role
created by that migration, NOT the `postgres` role. This matters: a
connection as `postgres` (or as any table owner) ignores RLS policies
by default even when they exist and even with FORCE ROW LEVEL
SECURITY set -- Postgres never enforces RLS against a superuser, and
Supabase's `postgres` role on a direct DATABASE_URL connection is
exactly that. Writing policies without also making this switch is a
no-op: they'd sit there unused while every query kept running with
full unrestricted access, same as before 0003. See migration 0003's
own header comment for the role-creation and DATABASE_URL-rotation
steps.

Every rewritten store module still does its own owner_id filtering in
the query itself, unchanged from before -- RLS here is defense in
depth (a bug or a forgotten WHERE clause fails closed at the database
instead of leaking rows), not a replacement for that Python-side
scoping. `cursor()` below takes an optional `user_id` (and a `trusted`
escape hatch for the handful of documented no-acting-user internal
reads) that call sites pass through so the RLS policies in migration
0003 have something to check against.
"""
import os
import time
from contextlib import contextmanager

import sentry_sdk
from psycopg.rows import dict_row
from psycopg.types.json import Json as _PsycopgJson
from psycopg_pool import ConnectionPool, PoolTimeout

from memory.bus import incr as _bus_incr
from memory.bus import read as _bus_read

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if python-dotenv isn't installed; DATABASE_URL can come from real env vars instead

DATABASE_URL = os.getenv("DATABASE_URL")
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
# perf audit §4.6 / priority #9: was defaulting to 10. Note this is now a
# deliberately *paired* number with api/server.py's APP_THREAD_POOL_SIZE
# (see that file's own comment) rather than a guess against the sync
# threadpool's old, unconfigured 40-token AnyIO default — most sync
# routes here do touch the DB, so there's little point letting the
# threadpool admit more concurrent requests than this pool can serve.
# If you raise one, raise the other, and check it against your actual
# Postgres/pooler connection limit (Supabase's pooler has its own
# separate cap — see env(example).txt).
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

# perf audit §4.6 / priority #9 (part 4): switched the pool implementation
# itself from psycopg2's ThreadedConnectionPool to psycopg3's psycopg_pool.
# ConnectionPool. The earlier retry-with-backoff wrapper (part 2, now
# removed below) existed only because psycopg2's pool is fail-fast --
# getconn() raises PoolError immediately at maxconn rather than waiting.
# psycopg_pool.ConnectionPool.getconn(timeout=...) does what the original
# audit comment assumed was already happening: it blocks the caller in a
# real waiting queue (not a poll loop) for up to `timeout` seconds,
# growing the pool if there's room under max_size, before raising
# PoolTimeout. That's a straight upgrade for the common case (a
# connection frees up in well under a second) -- "brief wait, then
# proceeds" instead of "hard fail, maybe retry, maybe fail again."
#
# The tradeoff, worth having on your radar: this now blocks a
# thread-pool thread for up to _GETCONN_TIMEOUT_SECONDS instead of
# failing near-instantly. Combined with B1/B2 from the wider audit
# (sync routes and long agent tasks sharing the same thread pool), a
# long enough wait here still eats into that same limited budget --
# it's just a slower failure mode than a bare PoolError now, not a
# different one. Keep this timeout modest; it is not a substitute for
# sizing DB_POOL_MAX / APP_THREAD_POOL_SIZE correctly in the first
# place (see get_pool_stats() below for the numbers to check before
# raising it).
_GETCONN_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_GETCONN_TIMEOUT_SECONDS", "2.0"))
# Below this measured wait, a getconn() call counts as "immediate" in
# get_pool_stats() rather than "waited" -- a few milliseconds of lock
# contention isn't the same signal as actually queueing behind a busy
# pool. Purely a stats-bucketing threshold; does not affect behavior.
_IMMEDIATE_THRESHOLD_SECONDS = 0.01

# perf audit §4.6 / priority #9 (part 3): the retry/503 handling above
# means pool exhaustion no longer surfaces as a loud 500, which is the
# point -- but it also means it's now invisible unless something counts
# it. Same pattern as chat_page_cache.py's global hit/miss counters
# (perf audit item #3): a coarse, global, Redis-backed tally via
# memory.bus.incr(), read back through get_pool_stats(). This is the
# number that answers "is DB_POOL_MAX=20 / APP_THREAD_POOL_SIZE=20
# actually enough" with real traffic instead of a guess -- see
# api/routes/system.py's /api/system/db-pool-stats.
#
# perf audit §4.6 / priority #9 (part 4): key names bumped to ":v2:" --
# part 3's counters measured outcomes of the old fail-fast-plus-manual-
# retry loop (a "retry" meant "succeeded on a second poll within
# ~0.35s"). Now that getconn() blocks natively, "waited" means something
# different (queued for up to _GETCONN_TIMEOUT_SECONDS in the pool's own
# waiting list) -- keeping the old key names would have silently mixed
# two different measurements under one counter.
_STATS_IMMEDIATE_KEY = "db_pool:stats:v2:acquired_immediately"
_STATS_WAITED_KEY = "db_pool:stats:v2:acquired_after_wait"
_STATS_EXHAUSTED_KEY = "db_pool:stats:v2:exhausted"
_STATS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days, matches chat_page_cache.py's STATS_TTL_SECONDS

_pool: ConnectionPool | None = None


class DatabaseUnavailable(Exception):
    """Raised when the DB pool stayed exhausted through the full wait.

    Carries `retry_after` (seconds) so the caller can tell the client how
    long to back off, matching the shape of a real Retry-After header.
    """

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(
            f"database connection pool exhausted; retry after ~{retry_after:.2f}s"
        )


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it to your .env file — see "
                "part8_schema.sql's setup instructions."
            )
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=_POOL_MIN,
            max_size=_POOL_MAX,
            # dict_row here replaces psycopg2.extras.RealDictCursor below --
            # applied once, pool-wide, at connection-open time, rather than
            # per-cursor at every db.cursor() call site.
            kwargs={"row_factory": dict_row},
            timeout=_GETCONN_TIMEOUT_SECONDS,
            # Explicit rather than the (deprecated-with-a-warning) implicit
            # default -- see psycopg_pool's own DeprecationWarning on this.
            # Non-blocking: this kicks off background connection workers
            # for min_size connections but doesn't wait for them here, so
            # constructing the pool (on first real db.cursor() call, same
            # lazy timing as before) doesn't itself stall the caller.
            open=True,
        )
    return _pool


def _getconn_with_timeout(pool: ConnectionPool):
    """pool.getconn(), tallied for get_pool_stats() and translated into
    DatabaseUnavailable (instead of a raw PoolTimeout) on failure so
    api/server.py's exception handler has a single, stable type to
    catch -- see that handler's own comment for why 503 + Retry-After
    beats letting this hit FastAPI as a bare 500.
    """
    t0 = time.monotonic()
    try:
        conn = pool.getconn()  # blocks up to the pool's own `timeout=` (see _get_pool)
    except PoolTimeout as exc:
        _bus_incr(_STATS_EXHAUSTED_KEY, ex=_STATS_TTL_SECONDS)
        sentry_sdk.capture_message(
            f"eo.db: connection pool exhausted after waiting "
            f"{_GETCONN_TIMEOUT_SECONDS:.1f}s (DB_POOL_MAX={_POOL_MAX})",
            level="warning",
        )
        raise DatabaseUnavailable(retry_after=_GETCONN_TIMEOUT_SECONDS) from exc

    waited = time.monotonic() - t0
    stats_key = _STATS_IMMEDIATE_KEY if waited < _IMMEDIATE_THRESHOLD_SECONDS else _STATS_WAITED_KEY
    _bus_incr(stats_key, ex=_STATS_TTL_SECONDS)
    return conn


def get_pool_stats() -> dict:
    """Returns our own Redis-backed outcome counters (see the _STATS_*
    keys above), plus psycopg_pool's own in-process pool.get_stats()
    (pool_size/pool_available/requests_wait_ms/requests_queued/
    requests_errors) merged in under a "psycopg_pool_native" key -- that
    data is free (the pool already tracks it for its own purposes) and
    gives finer-grained wait-time detail (real milliseconds, not just a
    bucket) than our own counters do, at the cost of resetting on every
    process restart, unlike the Redis-backed counters below which
    persist across restarts/deploys.

    exhaustion_rate is None (not 0.0) when there's no data yet, same
    "cold vs. unmeasured" distinction as chat_page_cache.get_cache_stats().
    This is the number to check before raising DB_POOL_MAX /
    APP_THREAD_POOL_SIZE / DB_POOL_GETCONN_TIMEOUT_SECONDS any further --
    a nonzero "exhausted" count means real requests are getting a 503
    today; a high "acquired_after_wait" relative to "acquired_immediately"
    means the pool is running close to its ceiling even when it isn't
    fully failing yet.
    """
    immediate = _bus_read(_STATS_IMMEDIATE_KEY, default=0) or 0
    waited = _bus_read(_STATS_WAITED_KEY, default=0) or 0
    exhausted = _bus_read(_STATS_EXHAUSTED_KEY, default=0) or 0
    total = immediate + waited + exhausted
    stats = {
        "acquired_immediately": immediate,
        "acquired_after_wait": waited,
        "exhausted": exhausted,
        "exhaustion_rate": (exhausted / total) if total else None,
        "pool_min": _POOL_MIN,
        "pool_max": _POOL_MAX,
        "getconn_timeout_seconds": _GETCONN_TIMEOUT_SECONDS,
    }
    if _pool is not None:
        stats["psycopg_pool_native"] = _pool.get_stats()
    return stats


@contextmanager
def cursor(user_id: str | None = None, trusted: bool = False):
    """Yields a dict-returning cursor inside a transaction. Commits on
    clean exit, rolls back on any exception, always returns the
    connection to the pool. This is the one function every rewritten
    store module calls — mirrors the old code's `with _lock:` shape:
    one context manager wraps each read-modify-write operation.

    user_id: the acting user for this transaction. Set as the Postgres
        session variable `app.current_user_id` via set_config() (NOT a
        plain SET, since SET doesn't take bind parameters — set_config
        does, so this stays injection-safe same as any other query
        param). Migration 0003's RLS policies read this back via
        current_setting('app.current_user_id', true) to decide which
        rows a query can see/touch. Pass whichever identity the calling
        function already received as its own owner_id/user_id/actor_id
        param — don't invent a new one.

    trusted: set True ONLY for the small, explicitly-documented set of
        call sites that intentionally have no acting user (background
        triggers, cross-user admin-style reads like audit_log's
        list_for_target) — see each call site's own docstring for why
        it qualifies. Sets `app.trusted_internal`, which migration
        0003's policies also check. Do not reach for this as a
        shortcut to avoid figuring out the right user_id; every
        genuinely user-scoped call site should pass user_id instead.

    Passing neither means the transaction runs with app.current_user_id
    and app.trusted_internal both unset, so migration 0003's policies
    fail CLOSED on every owner-scoped table (no rows visible) — that's
    intentional; forgetting to pass identity should break loudly
    (empty results / a caller-visible bug) rather than silently
    granting broader access.

    Usage:
        with db.cursor(user_id=owner_id) as cur:
            cur.execute("SELECT * FROM chats WHERE id = %s", (chat_id,))
            row = cur.fetchone()
    """
    pool = _get_pool()
    conn = _getconn_with_timeout(pool)
    try:
        # No cursor_factory needed here anymore -- dict_row is set
        # pool-wide via _get_pool()'s kwargs={"row_factory": dict_row},
        # so every connection this pool hands out already returns dict
        # rows by default (the psycopg3 equivalent of psycopg2.extras.
        # RealDictCursor, applied once at connect time instead of once
        # per cursor).
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute("SELECT set_config('app.current_user_id', %s, true)", (user_id,))
            if trusted:
                cur.execute("SELECT set_config('app.trusted_internal', 'true', true)")
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def Json(value):
    """Thin re-export of psycopg's Json adapter — wrap any dict/list value
    (e.g. a chat's `messages` list) with this before passing it as a
    query parameter for a jsonb column. Plain Python lists of strings
    (e.g. `tags`, `linked_chat_ids`) do NOT need this — psycopg adapts
    those to Postgres text[] arrays automatically. (Migrated from
    psycopg2.extras.Json to psycopg.types.json.Json as part of perf
    audit §4.6 / #9 (part 4) — same call signature, no call-site changes
    needed anywhere that already does `db.Json(...)`.)"""
    return _PsycopgJson(value)