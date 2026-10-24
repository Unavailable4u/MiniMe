"""
m2c/src/m2c/store/migrations.py -- Patch C02: the schema and its forward-only
migrations.

Versioning
----------
The schema version lives in a table, ``schema_version``, with one row per
migration ever applied::

    schema_version(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)

The current version is the highest row; **version 0** is a database that has
no such table (a new, empty file).  A table rather than ``PRAGMA
user_version`` so that the history (which migration, when) is data a later
``m2c doctor`` (C21: "stale or incompatible state migrations") can list.
The table is created by ``migrate()`` itself, in the same transaction as the
first migration, so a migration body never has to know about it.

Rules
-----
* **Forward only.**  There are no down-migrations.  ``migrate(target=n)``
  with ``n`` below the current version raises ``StoreError``.
* **One transaction per migration**, taken with ``BEGIN IMMEDIATE``: the
  schema change and its ``schema_version`` row commit together or not at
  all.  SQLite DDL is transactional, so a migration that fails half way
  leaves the database exactly as it was, at the previous version.  Earlier
  migrations in the same call stay applied.
* **Re-checked under the lock.**  If two processes open a new database at
  once, the loser waits for the write lock, sees the winner's row and skips
  that migration.  Each migration is applied exactly once.
* **An up-to-date database is not written to.**  ``migrate()`` reads the
  version first, without the write lock, and returns ``[]`` if nothing is
  pending; opening ``state.db`` while another process is writing never
  blocks on a migration check.
* **A newer database is refused, not touched.**  If the file's version is
  higher than this client knows, ``SchemaTooNewError`` is raised (a
  ``StoreError``): running old code on a newer schema could corrupt it.
* **Migration lists are validated**: versions must be 1, 2, 3 ... with no
  gap, duplicate or reordering (``validate_migrations``); a bad list is a
  programming error (``ValueError``) and is rejected before anything runs.
* A migration that raises is reported as ``StoreError("migration N (name)
  failed: ...")`` -- the version, the name and the SQLite error text, which
  never contains row data.

Writing a new migration (for the next work package that needs one)
------------------------------------------------------------------
Append a ``Migration(version=N+1, name=..., apply=fn)`` to ``MIGRATIONS``;
never edit a released one.  ``fn(conn)`` runs inside the migration's
transaction and must use ``conn.execute`` one statement at a time
(``executescript`` would COMMIT the open transaction).  ``ALTER TABLE ... ADD
COLUMN`` and ``CREATE ...`` are fine as they are.  Rebuilding a table
(rename-copy-drop) needs more care: ``PRAGMA foreign_keys`` cannot change
inside a transaction, so use ``PRAGMA defer_foreign_keys = ON`` and finish
with ``PRAGMA foreign_key_check``.  A migration that rebuilds ``events``
must drop and re-create its three append-only triggers on purpose; that is
the one place they may legitimately go away, and the migration's name should
say so.  The tests in tests/test_store_migrations.py walk every version of
``MIGRATIONS`` from 0 and between every pair, so a new entry is covered
automatically, but add a data-preserving case for it.

The v1 schema, table by table
-----------------------------
``sessions``
    One row per conversation with a project.
    ``id`` text primary key (matches the protocol's run-id pattern, so it can
    be reused as a run id); ``project_path`` absolute path of the project
    (sessions are tied to a directory, product guide §11.2); ``created_at``;
    ``mode`` text -- *not* a CHECK list, because modes are an additive
    protocol enum (``plan``, ``ask``, ``accept-edits`` today; ``auto`` and
    others later) and a CHECK would turn each addition into a table rebuild;
    ``model_pref`` text, NULL meaning "no preference".  A session that has
    events cannot be deleted (trigger ``sessions_keep_logged``); ``id`` cannot
    change (``sessions_id_immutable``).  User-visible deletion is a later work
    package and will be a deliberate migration.

``events``  (append-only)
    The device log, the source of truth (§O1).  ``(session_id, seq)`` is the
    primary key; ``seq`` starts at 1 and rises by exactly 1 per session
    (the helper in events.py guarantees it; the database enforces uniqueness
    only).  ``ts`` UTC text; ``type`` the event type; ``json`` the payload as
    canonical JSON text (sorted keys, no spaces, ASCII-only).  Three triggers
    make it append-only: ``events_no_update`` and ``events_no_delete`` abort
    those statements, and ``events_no_overwrite`` aborts an INSERT whose key
    already exists.  The third one matters: ``INSERT OR REPLACE`` deletes the
    old row *without* firing DELETE triggers (unless ``recursive_triggers``
    is on), so without it a REPLACE would rewrite history silently.  The
    triggers live in the file, so they stop any connection, not only this
    package's.  They guard against bugs and accidents; they cannot stop
    someone who can write the database file (``DROP TRIGGER`` works).
    Detecting *that* needs the hash chain from build plan §11.6, deliberately
    not part of this patch and addable later as a forward migration.

``approvals``  (an indexed record of decisions; the event is the truth)
    ``(session_id, seq)`` is the primary key *and* a foreign key to the
    ``events`` row that recorded the decision, so an approval cannot exist
    without an event in the append-only log ("a later step cannot pretend
    that an approval happened if it did not", feature guide §6.2).  Write
    the event and the approvals row inside one ``db.transaction()``.
    ``call_id`` the tool call (protocol ``call_id``); ``tool`` its name;
    ``input_summary`` the exact input the user was shown and approved --
    the guide says saved approvals are the exact command, not a pattern --
    stored as the caller supplies it (redaction of secrets is the caller's
    job, C15); ``decision`` ``allow`` | ``deny`` and ``scope`` ``once`` |
    ``session`` (the protocol's ``approval`` enums, enforced by CHECK because
    this table is a security record); ``decided_by`` ``user`` | ``policy`` |
    ``remote`` (a human at the terminal, an allow/deny rule, or a remote
    approval from the web app, X04); ``ts`` UTC text.  Rows are inserted,
    never edited by this package; unlike ``events`` the table has no triggers,
    because C06 may need to record a revocation and that is a later decision.

``checkpoints``
    One row per file per checkpoint step: ``(session_id, step, path)`` is the
    primary key.  ``path`` is relative to the session's project; ``blob_hash``
    names the saved copy of the file's content (the blob store is C08's) and
    is **NULL when the file did not exist at that step**, so rewinding knows
    to remove it (the guide's "tombstone"); ``ts`` UTC text.  Not append-only:
    C08 owns pruning and rewind semantics.

Unknown-but-useful things deliberately *not* here: a hash chain on events,
user-visible deletion and retention, session forking (``parent`` column), and
helper functions for approvals and checkpoints.  Each is a later work package
and, where it changes the schema, a forward migration.

Place this file at: m2c/src/m2c/store/migrations.py
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..logging_setup import log_event
from .db import StoreError, transaction, utc_now_iso

logger = logging.getLogger("m2c.store")


class SchemaTooNewError(StoreError):
    """The database was written by a newer m2c than this one."""

    def __init__(self, found: int, supported: int):
        self.found = found
        self.supported = supported
        super().__init__(
            f"state.db has schema version {found}, but this m2c only understands up to "
            f"{supported}; upgrade m2c (the database was not changed)"
        )


@dataclass(frozen=True)
class Migration:
    """One step of the schema: ``apply(conn)`` runs inside its own transaction."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


# --------------------------------------------------------------------------
# v1: the initial schema
# --------------------------------------------------------------------------
_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE sessions (
        id           TEXT NOT NULL PRIMARY KEY,
        project_path TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        mode         TEXT NOT NULL,
        model_pref   TEXT
    )
    """,
    "CREATE INDEX sessions_by_project ON sessions (project_path, created_at)",
    """
    CREATE TABLE events (
        session_id TEXT    NOT NULL REFERENCES sessions (id),
        seq        INTEGER NOT NULL CHECK (seq >= 1),
        ts         TEXT    NOT NULL,
        type       TEXT    NOT NULL CHECK (length(type) > 0),
        json       TEXT    NOT NULL,
        PRIMARY KEY (session_id, seq)
    ) WITHOUT ROWID
    """,
    """
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events is append-only: UPDATE is not allowed');
    END
    """,
    """
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events is append-only: DELETE is not allowed');
    END
    """,
    # Closes the INSERT OR REPLACE back door (see the module docstring).
    """
    CREATE TRIGGER events_no_overwrite BEFORE INSERT ON events
    WHEN EXISTS (SELECT 1 FROM events WHERE session_id = NEW.session_id AND seq = NEW.seq)
    BEGIN
        SELECT RAISE(ABORT, 'events is append-only: (session_id, seq) already exists');
    END
    """,
    # A session that has a log is never deleted or renamed behind the log's
    # back, even by a connection that has foreign_keys switched off.
    """
    CREATE TRIGGER sessions_keep_logged BEFORE DELETE ON sessions
    WHEN EXISTS (SELECT 1 FROM events WHERE session_id = OLD.id)
    BEGIN
        SELECT RAISE(ABORT, 'a session that has events cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER sessions_id_immutable BEFORE UPDATE OF id ON sessions
    BEGIN
        SELECT RAISE(ABORT, 'a session id cannot be changed');
    END
    """,
    """
    CREATE TABLE approvals (
        session_id    TEXT    NOT NULL,
        seq           INTEGER NOT NULL,
        call_id       TEXT    NOT NULL,
        tool          TEXT    NOT NULL,
        input_summary TEXT    NOT NULL,
        decision      TEXT    NOT NULL CHECK (decision IN ('allow', 'deny')),
        scope         TEXT    NOT NULL CHECK (scope IN ('once', 'session')),
        decided_by    TEXT    NOT NULL CHECK (decided_by IN ('user', 'policy', 'remote')),
        ts            TEXT    NOT NULL,
        PRIMARY KEY (session_id, seq),
        FOREIGN KEY (session_id, seq) REFERENCES events (session_id, seq)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX approvals_by_call ON approvals (session_id, call_id)",
    """
    CREATE TABLE checkpoints (
        session_id TEXT    NOT NULL REFERENCES sessions (id),
        step       INTEGER NOT NULL CHECK (step >= 0),
        path       TEXT    NOT NULL,
        blob_hash  TEXT,
        ts         TEXT    NOT NULL,
        PRIMARY KEY (session_id, step, path)
    ) WITHOUT ROWID
    """,
)


def _v1_initial_schema(conn: sqlite3.Connection) -> None:
    for statement in _V1_STATEMENTS:
        conn.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema", _v1_initial_schema),
)
LATEST_VERSION: int = MIGRATIONS[-1].version

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL PRIMARY KEY CHECK (version >= 1),
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
)
"""


# --------------------------------------------------------------------------
# Reading the state
# --------------------------------------------------------------------------
def validate_migrations(migrations: Sequence[Migration]) -> None:
    """Raise ``ValueError`` unless the versions are exactly 1, 2, 3 ..."""
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(
                f"migration versions must run 1, 2, 3 ... without gaps or repeats; "
                f"found {migration.version} where {expected} was expected"
            )


def current_version(conn: sqlite3.Connection) -> int:
    """The schema version of the database: 0 when it has none."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if has_table is None:
        return 0
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    if not versions:
        return 0
    if versions != list(range(1, len(versions) + 1)):
        raise StoreError("the schema_version table is inconsistent (missing or repeated versions)")
    return versions[-1]


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------
def _apply_one(conn: sqlite3.Connection, migration: Migration) -> bool:
    """Apply one migration in its own transaction.  False if someone else already did."""
    with transaction(conn):
        if current_version(conn) >= migration.version:
            return False  # another process migrated while we waited for the lock
        conn.execute(_SCHEMA_VERSION_DDL)
        try:
            migration.apply(conn)
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(
                f"migration {migration.version} ({migration.name}) failed: {type(exc).__name__}: {exc}"
            ) from exc
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, utc_now_iso()),
        )
    log_event(logger, logging.INFO, "store.migrated", version=migration.version)
    return True


def migrate(
    conn: sqlite3.Connection,
    *,
    target: int | None = None,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> list[int]:
    """Bring the database up to ``target`` (default: the latest version).

    Returns the versions this call applied, in order; ``[]`` when the
    database was already there (in which case nothing is written).  See the
    module docstring for the rules.  ``migrations`` exists so tests can run a
    longer list; production code uses the default.
    """
    validate_migrations(migrations)
    latest = migrations[-1].version if migrations else 0
    goal = latest if target is None else target
    if isinstance(goal, bool) or not isinstance(goal, int) or not 0 <= goal <= latest:
        raise ValueError(f"target must be an integer from 0 to {latest}")

    current = current_version(conn)
    if current > latest:
        raise SchemaTooNewError(current, latest)
    if goal < current:
        raise StoreError(
            f"migrations are forward-only: the database is at version {current}, target was {goal}"
        )

    applied: list[int] = []
    for migration in migrations:
        if migration.version <= current:
            continue
        if migration.version > goal:
            break
        if _apply_one(conn, migration):
            applied.append(migration.version)
    return applied
