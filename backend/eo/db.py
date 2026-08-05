"""
eo/db.py — shared Postgres connection pool for Part 8.2's migration.

Every store module that used to read/write JSON files under data/chats/
(chat_store.py, chat_workspace.py, memory_batch.py) now goes through this
module instead of touching files directly. This is the ONLY place that
knows about DATABASE_URL or the connection pool — nothing else should
import psycopg2 directly.

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
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if python-dotenv isn't installed; DATABASE_URL can come from real env vars instead

DATABASE_URL = os.getenv("DATABASE_URL")
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
# perf audit §4.6 / priority #9: was defaulting to 10 while FastAPI's own
# sync-route threadpool (api/server.py's 121-of-124 sync `def` routes,
# each dispatched via Starlette's run_in_threadpool) can run well more
# than 10 of them concurrently — meaning under real concurrent load,
# requests would queue for a DB connection even with idle threadpool
# capacity to actually run them. Raised the default closer to that
# threadpool's own concurrency headroom; still fully overridable via
# DB_POOL_MAX (see env(example).txt) so a given deployment can tune it
# against its actual Postgres connection limit rather than this default.
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it to your .env file — see "
                "part8_schema.sql's setup instructions."
            )
        _pool = ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, dsn=DATABASE_URL)
    return _pool


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
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
    """Thin re-export of psycopg2.extras.Json — wrap any dict/list value
    (e.g. a chat's `messages` list) with this before passing it as a
    query parameter for a jsonb column. Plain Python lists of strings
    (e.g. `tags`, `linked_chat_ids`) do NOT need this — psycopg2 adapts
    those to Postgres text[] arrays automatically."""
    return psycopg2.extras.Json(value)