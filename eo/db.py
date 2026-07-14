"""
eo/db.py — shared Postgres connection pool for Part 8.2's migration.

Every store module that used to read/write JSON files under data/chats/
(chat_store.py, chat_workspace.py, memory_batch.py) now goes through this
module instead of touching files directly. This is the ONLY place that
knows about DATABASE_URL or the connection pool — nothing else should
import psycopg2 directly.

Connects using the service_role-equivalent path: a direct Postgres
connection via DATABASE_URL (the Session Pooler string from Supabase),
NOT Supabase's REST API. Row-level security is enabled on every table
(see part8_schema.sql) but has no policies yet — that means only a
connection with sufficient Postgres privileges (which this is, since
DATABASE_URL connects as the `postgres` role) can read/write until 8.3
adds real per-user RLS policies. Until then, every function in the
rewritten store modules is responsible for its own owner_id filtering
in the query itself — the database isn't enforcing it yet, the Python
code is. That's intentional and matches 8.2's own scope: get identity
and storage right first, push the check into RLS in 8.3.
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
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

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
def cursor():
    """Yields a dict-returning cursor inside a transaction. Commits on
    clean exit, rolls back on any exception, always returns the
    connection to the pool. This is the one function every rewritten
    store module calls — mirrors the old code's `with _lock:` shape:
    one context manager wraps each read-modify-write operation.

    Usage:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM chats WHERE id = %s", (chat_id,))
            row = cur.fetchone()
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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