"""
m2c/src/m2c/store/db.py -- Patch C02: opening the local state database.

Per build plan §O1 the device is the source of truth: sessions, the event
log, approvals and checkpoints live in one SQLite file, ``state.db``, inside
the config directory (``~/.m2c``, or ``$M2C_HOME``; see paths.py).  This
module is the only place that opens that file.  Everything else in the
package (migrations, sessions, events) receives a connection from here.

Nothing in the store touches the network.  It is local data, written by this
process and read by this process; tests/test_store_db.py fails if any store
module imports a network module.

Where the file lives
--------------------
``open_db()`` resolves the directory through ``paths.prepare_config_dir()``
(create with mode 0700, tighten a looser one, refuse another user's
directory, prove it is writable) and appends ``DB_FILENAME``.  The path is
never spelled ``~/.m2c`` here.  ``connect(path)`` is the lower-level entry
point that takes an explicit path and runs no migrations; tests and
``migrations.py`` use it.

File modes (fail closed)
------------------------
The database holds everything the user typed and everything the model and
the tools returned, so it is created and kept at mode 0600, and so are the
sidecar files SQLite creates next to it (``-wal``, ``-shm``, ``-journal``).
How:

* The file is created by ``os.open(..., O_CREAT, 0o600)`` *before* SQLite
  sees it, and SQLite gives its sidecars the mode of the main file.  So the
  umask can only ever make them stricter, never looser.
* ``O_NOFOLLOW``: a symbolic link in place of ``state.db`` is refused, so a
  link cannot redirect the database somewhere else.
* The opened file must be a regular file owned by the current user; a file
  with a looser mode is tightened with ``fchmod`` and re-checked, and a
  filesystem that ignores chmod (drvfs under ``/mnt/c``) is refused, exactly
  like paths.ensure_config_dir does for the directory.
* After the connection is configured, any sidecar that exists is checked the
  same way (a symlink or foreign-owned sidecar is refused).
* The parent directory stays 0700 because ``open_db`` goes through
  ``prepare_config_dir``; ``connect`` does not touch the directory.

All problems raise ``StoreError`` rather than carrying on.

Settings that make an interrupted write safe
--------------------------------------------
* ``journal_mode=WAL``: a transaction is not visible, and not part of the
  database, until its commit record is in the log; a process killed
  mid-transaction (SIGKILL, power cut) leaves an uncommitted tail that the
  next opener discards.  Readers never block the writer.  If the filesystem
  cannot do WAL (some network mounts), SQLite stays in its rollback-journal
  mode, which is also crash-safe, only slower with concurrent readers; that
  is logged and accepted.
* ``synchronous=FULL``: a commit is fsynced before it returns, so a commit
  the caller saw succeed survives power loss, not only a process crash.  The
  default for WAL (NORMAL) can lose the most recent commits on power loss.
  This log is the source of truth (§O1), and events are small, so durability
  wins over speed; batch with ``events.append_events`` when volume matters.
* ``foreign_keys=ON`` (it is off by default in SQLite, per connection) and
  a check that the setting really took, since a build without foreign-key
  support would silently ignore it.
* ``timeout=BUSY_TIMEOUT_S`` (the ``busy_timeout``): a second opener or a
  second writer waits for the lock instead of failing at once.
* ``isolation_level=None``: Python's implicit transaction handling is off.
  The store issues BEGIN / COMMIT itself through ``transaction()``, so there
  is no hidden state about when a transaction starts.

``transaction()``
-----------------
``with transaction(conn):`` runs ``BEGIN IMMEDIATE`` (the write lock is taken
*before* anything is read, so "read the last seq, then insert the next" can
never interleave with another writer), commits on success and rolls back on
any exception, including KeyboardInterrupt.  Inside an already-open
transaction it becomes a SAVEPOINT, so helpers compose: a caller can wrap
several ``events.append_event`` calls and an approvals insert in one outer
``transaction()`` and they commit or roll back together.  Start the outer
one with ``transaction()`` rather than a bare ``BEGIN`` so the write lock is
held from the start.

Timestamps
----------
Stored as text, ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` in UTC (``utc_now_iso``).
Fixed width, so they sort as text; RFC 3339, so they match protocol/v1's
``timestamp``.  ``normalize_ts`` accepts a timezone-aware datetime or an
RFC 3339 string (server-supplied times are kept verbatim).  Ordering of
events never depends on ``ts``; ``seq`` is the order.

Logging follows logging_setup.py: only content-free events, only allowlisted
keys.  Paths, payloads and SQL errors' text are never logged.

Place this file at: m2c/src/m2c/store/db.py
"""
from __future__ import annotations

import contextlib
import errno
import itertools
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import paths
from ..logging_setup import log_event

logger = logging.getLogger("m2c.store")

DB_FILENAME = "state.db"
DB_FILE_MODE = 0o600
BUSY_TIMEOUT_S = 5.0

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_savepoint_ids = itertools.count(1)

# protocol/v1/common.json "timestamp": RFC 3339 with an explicit offset.
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$"
)


class StoreError(Exception):
    """The local store cannot be opened, trusted or used.

    Messages are written for the person at the terminal.  They may name a
    path; they never contain event payloads or anything typed into a session.
    """


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
def utc_now_iso() -> str:
    """Now, in UTC, as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_ts(value: Any = None) -> str:
    """Return a storable timestamp.

    ``None`` means now.  A timezone-aware ``datetime`` is converted to UTC.
    A string must be RFC 3339 with an explicit offset and is kept as given
    (so a server-supplied time is not rewritten).  Anything else raises
    ``ValueError``; a naive datetime does too, because it would silently
    take the local zone.
    """
    if value is None:
        return utc_now_iso()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, str) and _RFC3339_RE.fullmatch(value):
        return value
    raise ValueError("timestamp must be an aware datetime or an RFC 3339 string with an offset")


# --------------------------------------------------------------------------
# Locating and securing the file
# --------------------------------------------------------------------------
def db_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the database lives, without touching the filesystem."""
    return paths.config_dir(environ) / DB_FILENAME


def _secure_database_file(db_file: Path) -> None:
    """Create ``db_file`` at 0600 if missing; verify or tighten it otherwise."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:  # native Windows: doctor reports the OS first, this is the backstop
        raise StoreError("POSIX file permissions are required to protect the database")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(db_file, flags, DB_FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StoreError(f"{db_file} is a symbolic link; refusing to use it") from None
        raise StoreError(f"cannot open {db_file} ({exc.strerror or type(exc).__name__})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f"{db_file} is not a regular file")
        if info.st_uid != geteuid():
            raise StoreError(f"{db_file} is owned by another user; refusing to use it")
        if stat.S_IMODE(info.st_mode) != DB_FILE_MODE:
            try:
                os.fchmod(fd, DB_FILE_MODE)
            except OSError as exc:
                raise StoreError(
                    f"cannot set mode 0600 on {db_file} ({exc.strerror or type(exc).__name__})"
                ) from exc
            if stat.S_IMODE(os.fstat(fd).st_mode) != DB_FILE_MODE:
                raise StoreError(
                    f"cannot set mode 0600 on {db_file}: the filesystem ignores permission changes "
                    "(this is typical under /mnt/c); choose a directory on the Linux filesystem"
                )
            log_event(logger, logging.INFO, "store.db_mode_tightened")
    finally:
        os.close(fd)


def _secure_sidecars(db_file: Path) -> None:
    """Make sure the -wal / -shm / -journal files that exist are 0600 regular files."""
    for suffix in _SIDECAR_SUFFIXES:
        side = db_file.with_name(db_file.name + suffix)
        try:
            info = os.lstat(side)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StoreError(f"cannot inspect {side} ({exc.strerror or type(exc).__name__})") from exc
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f"{side} is not a regular file; refusing to use the database")
        if stat.S_IMODE(info.st_mode) == DB_FILE_MODE:
            continue
        try:
            os.chmod(side, DB_FILE_MODE)
        except FileNotFoundError:  # another connection closed and SQLite removed it
            continue
        except OSError as exc:
            raise StoreError(
                f"cannot set mode 0600 on {side} ({exc.strerror or type(exc).__name__})"
            ) from exc
        log_event(logger, logging.INFO, "store.db_mode_tightened")


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------
def _configure(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StoreError("this SQLite build does not support foreign keys")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        # Rollback-journal mode is still crash-safe; only concurrency suffers.
        log_event(logger, logging.WARNING, "store.wal_unavailable", status=str(mode))
    conn.execute("PRAGMA synchronous = FULL")


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path``, securely configured.

    Runs no migrations: use ``migrations.migrate`` on the result, or call
    ``open_db`` which does both.  The caller closes the connection.  A
    connection belongs to the thread that made it (sqlite3's default); give
    each thread its own.
    """
    db_file = Path(path)
    _secure_database_file(db_file)
    try:
        conn = sqlite3.connect(os.fspath(db_file), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(f"cannot open the database ({exc})") from exc
    try:
        _configure(conn)
        _secure_sidecars(db_file)
    except sqlite3.Error as exc:
        conn.close()
        raise StoreError(f"cannot use {db_file} as a database ({exc})") from exc
    except BaseException:
        conn.close()
        raise
    return conn


def open_db(environ: Mapping[str, str] | None = None) -> sqlite3.Connection:
    """Open ``state.db`` in the config directory, migrated to the latest schema.

    Raises ``paths.ConfigDirError`` if the directory cannot be trusted,
    ``StoreError`` if the file cannot, and ``migrations.SchemaTooNewError``
    if the database was written by a newer m2c (it is never downgraded).
    """
    # Imported here, not at the top: migrations.py imports this module for
    # StoreError and transaction(), so a top-level import would be circular.
    from . import migrations

    directory = paths.prepare_config_dir(environ)
    conn = connect(directory / DB_FILENAME)
    try:
        migrations.migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------
@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: ``BEGIN IMMEDIATE`` .. ``COMMIT``, or roll back.

    Outside a transaction this takes the write lock up front.  Inside one it
    opens a SAVEPOINT, so a failed inner block undoes only itself.
    """
    if conn.in_transaction:
        name = f"m2c_sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
