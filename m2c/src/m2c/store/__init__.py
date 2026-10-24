"""
m2c/src/m2c/store/__init__.py -- Patch C02: the local store.

SQLite at ``~/.m2c/state.db`` (``$M2C_HOME/state.db`` when set), standard
library only, no network.  It holds what build plan §O1 calls the device's
source of truth for a session: the session list, the append-only event log,
approvals and checkpoints.

    db.py          open the file safely (modes, WAL, fsync, timeouts), transaction()
    migrations.py  schema version table, forward-only migrations, the v1 schema
    sessions.py    create / get / list sessions
    events.py      append (next seq in one transaction), read back, JSONL export

Typical use::

    from m2c.store import open_db, create_session, append_event, read_events

    conn = open_db()                     # migrated, 0600, WAL
    s = create_session(conn, project_path="/home/me/code/app", mode="ask")
    append_event(conn, s.id, "user_message", {"text": "hello"})
    for event in read_events(conn, s.id):
        ...

Each thread needs its own connection.  See the module docstrings for the
rules (what is guaranteed, and what is not).

Not in this patch, on purpose: a hash chain over ``events`` (an optional
later forward migration, adapted from MiniLocker's ledger/chain.py at commit
54520a2, build plan §11.6; it would detect edits, not prevent a rewrite),
helpers for approvals and checkpoints (C06, C08), and user-visible deletion.

Place this file at: m2c/src/m2c/store/__init__.py
"""
from __future__ import annotations

from .db import DB_FILENAME, StoreError, connect, db_path, open_db, transaction
from .events import (
    Event,
    append_event,
    append_events,
    export_jsonl,
    iter_events,
    last_seq,
    read_events,
)
from .migrations import LATEST_VERSION, SchemaTooNewError, current_version, migrate
from .sessions import (
    Session,
    SessionExistsError,
    UnknownSessionError,
    create_session,
    get_session,
    list_sessions,
    new_session_id,
    require_session,
)

__all__ = [
    "DB_FILENAME",
    "LATEST_VERSION",
    "Event",
    "SchemaTooNewError",
    "Session",
    "SessionExistsError",
    "StoreError",
    "UnknownSessionError",
    "append_event",
    "append_events",
    "connect",
    "create_session",
    "current_version",
    "db_path",
    "export_jsonl",
    "get_session",
    "iter_events",
    "last_seq",
    "list_sessions",
    "migrate",
    "new_session_id",
    "open_db",
    "read_events",
    "require_session",
    "transaction",
]
