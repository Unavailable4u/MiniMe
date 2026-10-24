"""
m2c/src/m2c/store/events.py -- Patch C02: the append-only event log.

``events(session_id, seq, ts, type, json)`` is the device log, the source of
truth for what happened in a session (build plan §O1).  The server's
in-flight buffer may expire; this record does not.  This module is the way
to add to it, read it back and export it.

Appending
---------
``append_event`` and ``append_events`` assign the next ``seq`` for the
session *inside one transaction*: ``db.transaction`` takes the write lock
first (``BEGIN IMMEDIATE``), then the helper reads ``MAX(seq)`` and inserts
``MAX + 1``.  Two writers, in two threads or two processes, therefore cannot
read the same maximum; one waits for the other (up to the busy timeout) and
the seqs come out unique and contiguous, 1, 2, 3 ...  If the caller is
already inside ``db.transaction`` the append joins it as a savepoint, so an
event and, say, an ``approvals`` row commit together; if that outer
transaction rolls back, the seq is not consumed (no gaps).

``seq`` here is the *local* order of this session's log.  It starts at 1 like
the protocol's server stream (protocol/v1/README.md §4) but is not required
to equal it; C04 decides whether to record the server's ``seq`` inside the
payload.

The payload may be any JSON value.  It is serialised first, before the
transaction starts, so an unserialisable payload (``TypeError``) or a NaN
(``ValueError``) never leaves a half-written batch or holds the lock.  It is
stored as *canonical* JSON text: sorted keys, no spaces, ``ensure_ascii``.
Canonical so the stored bytes are stable (a later hash chain can cover them
without re-serialising); ASCII-only so that no payload can ever fail to
encode, lone surrogates from broken model output included, and so that
U+2028 / U+0085 and friends cannot split a line in the JSONL export.

Read-back
---------
``read_events`` returns events in ``seq`` order, optionally only those after
a given ``seq`` (what ``run.resume`` needs: "everything since my last seen
seq") and with a limit.  ``last_seq`` gives that last seen seq.  Reading an
unknown or empty session returns ``[]`` / ``0``; only ``append_*`` and
``export_*`` raise ``UnknownSessionError`` for an id that does not exist,
because there a typo is a bug worth surfacing.

Append-only
-----------
The schema (migrations.py) has triggers that abort UPDATE, DELETE and an
INSERT onto an existing key, so there is deliberately no update or delete
function here and none can be added by accident.  See migrations.py for
exactly what those triggers do and do not guarantee.

JSONL export
------------
``export_jsonl(conn, dest, session_id=None)`` writes one JSON object per
line::

    {"session_id":"...","seq":1,"ts":"...","type":"...","json":{...}}

The key order is fixed and the ``json`` value is the stored canonical text
verbatim, so the same database always exports to the same bytes.  One
session: ordered by ``seq``.  All sessions: ordered by the session's
``created_at``, then session id, then ``seq``.  A row whose stored ``json`` is
not valid JSON (only possible if something bypassed this module) aborts the
export with ``StoreError`` naming the session and seq rather than writing an
invalid line.

The export holds everything the session did, tool output and file contents
included, so a path destination is written like the database itself: a
temporary file in the destination's directory (mode 0600 from the start),
fsynced, then ``os.replace``d into place.  The result is mode 0600 whatever
the umask or the previous file's mode; a failed export never leaves a partial
file or temp file behind and never destroys the previous export; and
because the file is *replaced*, not opened, a symbolic link or hard link at
the destination is never written through.  A text stream (anything with
``write``) is also accepted, for stdout and tests; the caller owns its
permissions.

Place this file at: m2c/src/m2c/store/events.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .db import StoreError, normalize_ts, transaction
from .sessions import UnknownSessionError

MAX_TYPE_LENGTH = 128


@dataclass(frozen=True)
class Event:
    session_id: str
    seq: int
    ts: str
    type: str
    data: Any  # the ``json`` column, parsed


def dumps_canonical(value: Any) -> str:
    """Canonical JSON text: sorted keys, no spaces, ASCII only, no NaN/Infinity."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_type(event_type: Any) -> str:
    if not isinstance(event_type, str) or not 0 < len(event_type) <= MAX_TYPE_LENGTH:
        raise ValueError(f"event type must be a string of 1 to {MAX_TYPE_LENGTH} characters")
    return event_type


def _event(row: tuple) -> Event:
    session_id, seq, ts, event_type, text = row
    return Event(session_id, seq, ts, event_type, json.loads(text))


# --------------------------------------------------------------------------
# Appending
# --------------------------------------------------------------------------
def append_events(
    conn: sqlite3.Connection,
    session_id: str,
    items: Iterable[tuple[str, Any]],
    *,
    ts: datetime | str | None = None,
) -> list[Event]:
    """Append ``(type, data)`` items to the session's log in one transaction.

    Returns the stored events, with their seqs.  All items share one
    timestamp (``ts`` or now).  Nothing is written if any item is invalid or
    the session does not exist.
    """
    prepared = [(_check_type(event_type), dumps_canonical(data), data) for event_type, data in items]
    if not prepared:
        return []
    stamp = normalize_ts(ts)

    with transaction(conn):
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise UnknownSessionError(f"no session with id {session_id}")
        (last,) = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
        stored: list[Event] = []
        for offset, (event_type, text, data) in enumerate(prepared, start=1):
            seq = last + offset
            conn.execute(
                "INSERT INTO events (session_id, seq, ts, type, json) VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, stamp, event_type, text),
            )
            stored.append(Event(session_id, seq, stamp, event_type, json.loads(text)))
    return stored


def append_event(
    conn: sqlite3.Connection,
    session_id: str,
    event_type: str,
    data: Any,
    *,
    ts: datetime | str | None = None,
) -> Event:
    """Append one event and return it (with its assigned ``seq``)."""
    return append_events(conn, session_id, [(event_type, data)], ts=ts)[0]


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def last_seq(conn: sqlite3.Connection, session_id: str) -> int:
    """The highest seq in the session's log; 0 if it has none (or does not exist)."""
    (value,) = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,)
    ).fetchone()
    return value


def iter_events(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
) -> Iterator[Event]:
    """Events of the session with ``seq > after_seq``, in seq order."""
    sql = (
        "SELECT session_id, seq, ts, type, json FROM events "
        "WHERE session_id = ? AND seq > ? ORDER BY seq"
    )
    params: list[Any] = [session_id, after_seq]
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        sql += " LIMIT ?"
        params.append(limit)
    for row in conn.execute(sql, params):
        yield _event(row)


def read_events(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_seq: int = 0,
    limit: int | None = None,
) -> list[Event]:
    """``iter_events`` collected into a list."""
    return list(iter_events(conn, session_id, after_seq=after_seq, limit=limit))


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def _export_line(session_id: str, seq: int, ts: str, event_type: str, text: str) -> str:
    try:
        json.loads(text)
    except ValueError:
        raise StoreError(
            f"the stored payload of session {session_id} seq {seq} is not valid JSON; export stopped"
        ) from None
    return (
        f'{{"session_id":{json.dumps(session_id)},"seq":{seq},"ts":{json.dumps(ts)},'
        f'"type":{json.dumps(event_type)},"json":{text}}}'
    )


def iter_export_lines(conn: sqlite3.Connection, session_id: str | None = None) -> Iterator[str]:
    """JSONL lines (without the trailing newline), in the documented order.

    Raises ``UnknownSessionError`` at once (not on first iteration) if
    ``session_id`` is given and does not exist.
    """
    if session_id is not None:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise UnknownSessionError(f"no session with id {session_id}")
        cursor = conn.execute(
            "SELECT session_id, seq, ts, type, json FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
    else:
        cursor = conn.execute(
            "SELECT e.session_id, e.seq, e.ts, e.type, e.json FROM events AS e "
            "JOIN sessions AS s ON s.id = e.session_id "
            "ORDER BY s.created_at, s.id, e.seq"
        )

    def lines() -> Iterator[str]:
        for row in cursor:
            yield _export_line(*row)

    return lines()


def _write_lines(lines: Iterable[str], out: TextIO) -> int:
    count = 0
    for line in lines:
        out.write(line)
        out.write("\n")
        count += 1
    return count


def export_jsonl(
    conn: sqlite3.Connection,
    dest: str | os.PathLike[str] | TextIO,
    *,
    session_id: str | None = None,
) -> int:
    """Write the log as JSONL to ``dest`` (a path or a text stream); return the event count."""
    lines = iter_export_lines(conn, session_id)
    if hasattr(dest, "write"):
        return _write_lines(lines, dest)  # type: ignore[arg-type]

    target = Path(os.fspath(dest))
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            count = _write_lines(lines, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return count
