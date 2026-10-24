"""
m2c/src/m2c/store/sessions.py -- Patch C02: creating and reading sessions.

A session is one conversation with the agent about one project directory
(product guide §11.2: "Sessions are tied to a directory").  It is a small
row; everything that happens in it goes into the append-only ``events`` log
(events.py).  The guide's remark that sessions are kept "locally and in the
backend log" is superseded by build plan §O1: the device is the only home of
session content, so nothing here is ever sent anywhere.

Identifiers
-----------
``new_session_id()`` returns a random UUID4 in canonical form.  Any id a
caller supplies must match the protocol's run-id pattern
(``^[A-Za-z0-9_-]{8,64}$``, protocol/v1/common.json), so that a session id can
double as the run id the server sees (the id is opaque to the server: it
validates and never parses it).  ``--resume <id>`` reopens by this id.

Fields
------
``project_path`` must be absolute and is normalised lexically with
``os.path.normpath`` (the same treatment paths.config_dir gives M2C_HOME), so
``list_sessions(project_path=...)`` finds what ``create_session`` stored.
Symbolic links are *not* resolved: the caller decides whether the session
belongs to the link or its target.  ``mode`` is stored exactly as given and
is not checked against a list, because the mode set is an additive protocol
enum (see migrations.py).  ``model_pref`` is optional; ``None`` means "no
preference".

There is deliberately no delete function.  A session with events cannot be
deleted (a trigger in the schema stops it); user-visible deletion and
retention are later work packages and will be explicit migrations.

Place this file at: m2c/src/m2c/store/sessions.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import StoreError, normalize_ts, transaction

# protocol/v1/common.json "run_id".
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class UnknownSessionError(StoreError, LookupError):
    """No session with that id exists."""


class SessionExistsError(StoreError):
    """A session with that id already exists."""


@dataclass(frozen=True)
class Session:
    id: str
    project_path: str
    created_at: str
    mode: str
    model_pref: str | None


_COLUMNS = "id, project_path, created_at, mode, model_pref"


def new_session_id() -> str:
    return str(uuid.uuid4())


def _normalise_project_path(value: Any) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        raise ValueError("project_path must be a path") from None
    if not isinstance(raw, str) or raw == "":
        raise ValueError("project_path must be a non-empty path")
    if not os.path.isabs(raw):
        raise ValueError(f"project_path must be absolute (got {raw!r})")
    return os.path.normpath(raw)


def _row(row: tuple) -> Session:
    return Session(*row)


def create_session(
    conn: sqlite3.Connection,
    *,
    project_path: str | os.PathLike[str],
    mode: str,
    model_pref: str | None = None,
    session_id: str | None = None,
    created_at: datetime | str | None = None,
) -> Session:
    """Insert a session and return it.

    Raises ``ValueError`` for a malformed argument and ``SessionExistsError``
    if ``session_id`` is already taken (the existing row is left alone).
    """
    path = _normalise_project_path(project_path)
    if not isinstance(mode, str) or mode == "":
        raise ValueError("mode must be a non-empty string")
    if model_pref is not None and (not isinstance(model_pref, str) or model_pref == ""):
        raise ValueError("model_pref must be None or a non-empty string")
    if session_id is None:
        session_id = new_session_id()
    elif not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id must match [A-Za-z0-9_-]{8,64}")
    stamp = normalize_ts(created_at)

    with transaction(conn):
        exists = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if exists is not None:
            raise SessionExistsError(f"a session with id {session_id} already exists")
        conn.execute(
            f"INSERT INTO sessions ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (session_id, path, stamp, mode, model_pref),
        )
    return Session(session_id, path, stamp, mode, model_pref)


def get_session(conn: sqlite3.Connection, session_id: str) -> Session | None:
    """The session with this id, or ``None``."""
    row = conn.execute(f"SELECT {_COLUMNS} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else _row(row)


def require_session(conn: sqlite3.Connection, session_id: str) -> Session:
    """Like ``get_session`` but raises ``UnknownSessionError`` when missing."""
    found = get_session(conn, session_id)
    if found is None:
        raise UnknownSessionError(f"no session with id {session_id}")
    return found


def list_sessions(
    conn: sqlite3.Connection,
    *,
    project_path: str | os.PathLike[str] | None = None,
    limit: int | None = None,
) -> list[Session]:
    """Sessions newest first (ties broken by id, descending), optionally for one project."""
    sql = f"SELECT {_COLUMNS} FROM sessions"
    params: list[Any] = []
    if project_path is not None:
        sql += " WHERE project_path = ?"
        params.append(_normalise_project_path(project_path))
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        sql += " LIMIT ?"
        params.append(limit)
    return [_row(row) for row in conn.execute(sql, params)]
