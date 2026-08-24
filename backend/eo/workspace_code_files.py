"""
eo/workspace_code_files.py — Master Guide V2 step 16 (T3), patch 8: the
Code sub-tab's backend persistence layer.

Same shape as eo/panel_content.py's per-workspace store — one row per
key, last-write-wins, no version history — except the key here is a
file path within the workspace's generated codebase instead of a fixed
panel_key from an allowlist. This is the gap flagged in the Master
Guide: agents/code_writers.py already generates real code today, but
its output only ever lands in the memory-bus submitted_code key (see
eo/code_loader.py's own docstring — {module_key: {"language", "code"}},
session-scoped, no folder structure, gone once the session's memory-bus
data expires). This module gives that output a real per-workspace,
per-file home that survives a reload, the same way panel_content.py
gave the paste-panels one.

Two deliberate differences from panel_content.py's shape, both because
a codebase is a different kind of data than one pasted blob per panel:

  1. No VALID_PANEL_KEYS-style allowlist — file paths are open-ended by
     nature (whatever structure_architect.py/code_writers.py decide a
     given app needs), so this module validates SHAPE (no traversal, no
     absolute paths, bounded length) via _validate_file_path() rather
     than membership in a fixed set.
  2. list_files() returns metadata only, NOT content — panel_content's
     list_content() can afford to return every panel's full content in
     one call because there are at most ~15 panels; a workspace's
     codebase can have many files of real size, and patch 10's file-tree
     view only needs paths/language/size to render the tree. get_file()
     is the per-file call that returns content, fetched on click-to-open
     — see that function's own docstring.

Schema (see migrations/0006_add_workspace_code_files.sql):
    workspace_code_files(
        workspace_id  text references workspaces(id) on delete cascade,
        file_path     text,
        content       text,
        language      text,
        updated_at    timestamptz,
        updated_by    text,
        primary key (workspace_id, file_path)
    )
"""
import io
import os
import re
import zipfile
from datetime import UTC, datetime

from eo import db
from eo.audit_log import write_audit

# Defense-in-depth against a malformed or malicious file_path ending up
# as a row key or (eventually, patch 11) a path segment in a server-side
# zip write -- same "fail loudly at the API layer, not silently" posture
# panel_content.py's VALID_PANEL_KEYS check takes, just shape-based
# instead of allowlist-based since file paths can't be enumerated ahead
# of time.
_MAX_PATH_LENGTH = 512
_VALID_PATH_CHARS = re.compile(r"^[A-Za-z0-9_./-]+$")


def _validate_file_path(file_path: str) -> None:
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")
    if len(file_path) > _MAX_PATH_LENGTH:
        raise ValueError(f"file_path exceeds {_MAX_PATH_LENGTH} characters")
    if file_path.startswith("/") or file_path.startswith("\\"):
        raise ValueError("file_path must be relative, not absolute")
    # normpath collapses "a/../../b" etc.; if that ever climbs above the
    # workspace root (starts with ".." after normalizing) or the raw
    # string contains a literal ".." segment, reject outright rather than
    # trying to sanitize -- same "fail loud on a typo/attack" posture as
    # panel_content.py's ValueError on an unknown panel_key.
    if ".." in file_path.split("/") or ".." in file_path.split("\\"):
        raise ValueError("file_path cannot contain '..' segments")
    normalized = os.path.normpath(file_path)
    if normalized.startswith("..") or os.path.isabs(normalized):
        raise ValueError("file_path resolves outside the workspace root")
    if not _VALID_PATH_CHARS.match(file_path):
        raise ValueError(
            "file_path may only contain letters, numbers, '.', '_', '-', and '/'"
        )


# Extension -> language, for the file-tree view's syntax highlighting and
# for stamping a default when the caller doesn't pass one explicitly
# (e.g. a write coming from patch 9's task_runner.py hook, which has
# code_writers.py's own declared language already and doesn't need this
# guess). Deliberately small -- just what agents/code_writers.py and
# common project-scaffolding files actually produce -- not an attempt at
# a universal extension map.
_EXTENSION_LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sql": "sql",
    ".sh": "bash",
    ".txt": "text",
    ".env": "dotenv",
}


def _infer_language(file_path: str) -> str:
    _, ext = os.path.splitext(file_path)
    return _EXTENSION_LANGUAGE_MAP.get(ext.lower(), "text")


def _now():
    return datetime.now(UTC)


def _iso(value):
    return value.isoformat() if value is not None else None


def _row_to_meta(row: dict) -> dict:
    """Metadata shape — no `content` key. See list_files()'s docstring
    for why the list endpoint deliberately omits it."""
    return {
        "workspace_id": row["workspace_id"],
        "file_path": row["file_path"],
        "language": row.get("language"),
        "size": len(row["content"]) if row.get("content") is not None else 0,
        "updated_at": _iso(row["updated_at"]),
        "updated_by": row.get("updated_by"),
    }


def _row_to_file(row: dict) -> dict:
    """Full shape, content included — what get_file()/write_file()
    return."""
    return {
        "workspace_id": row["workspace_id"],
        "file_path": row["file_path"],
        "content": row["content"],
        "language": row.get("language"),
        "updated_at": _iso(row["updated_at"]),
        "updated_by": row.get("updated_by"),
    }


def _empty_file(ws_id: str, file_path: str) -> dict:
    return {
        "workspace_id": ws_id,
        "file_path": file_path,
        "content": "",
        "language": None,
        "updated_at": None,
        "updated_by": None,
    }


def list_files(ws_id: str) -> dict:
    """Every saved file's metadata for a workspace, keyed by file_path —
    content deliberately omitted, see this module's own docstring.
    Patch 10's file-tree view builds its tree client-side from these
    flat file_path keys (the same "flat map of paths, no separate
    directory rows" approach eo/code_loader.py's file_map already uses),
    then calls get_file() per click-to-open."""
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select workspace_id, file_path, content, language, updated_at, updated_by "
            "from workspace_code_files where workspace_id = %s",
            (ws_id,),
        )
        rows = cur.fetchall()
    return {r["file_path"]: _row_to_meta(r) for r in rows}


def get_file(ws_id: str, file_path: str) -> dict:
    """Returns an empty-content shape (not a 404) when nothing's been
    saved at this path yet — same "unsaved panel renders blank, not an
    error" convention as panel_content.get_content()."""
    _validate_file_path(file_path)
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select workspace_id, file_path, content, language, updated_at, updated_by "
            "from workspace_code_files where workspace_id = %s and file_path = %s",
            (ws_id, file_path),
        )
        row = cur.fetchone()
    return _row_to_file(row) if row else _empty_file(ws_id, file_path)


def write_file(ws_id: str, file_path: str, content: str, user_id: str, language: str | None = None) -> dict:
    """Upsert, last-write-wins — same posture panel_content.set_content()
    documents (no version history; a bigger feature, deliberately out of
    scope for this pass, same as it was for panel_content). `language`
    defaults to an extension-based guess (_infer_language) when the
    caller doesn't already know it — patch 9's task_runner.py hook will
    have code_writers.py's own declared language and can pass it
    explicitly instead of relying on the guess."""
    _validate_file_path(file_path)
    resolved_language = language or _infer_language(file_path)
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            """
            insert into workspace_code_files (workspace_id, file_path, content, language, updated_at, updated_by)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (workspace_id, file_path)
            do update set content = excluded.content, language = excluded.language,
                          updated_at = excluded.updated_at, updated_by = excluded.updated_by
            returning workspace_id, file_path, content, language, updated_at, updated_by
            """,
            (ws_id, file_path, content, resolved_language, _now(), user_id),
        )
        row = cur.fetchone()
    write_audit(user_id, "code_file.write", "workspace", ws_id, {"file_path": file_path})
    return _row_to_file(row)


def build_zip_archive(ws_id: str) -> bytes | None:
    """Patch 11: zips the current file set for a workspace, in memory —
    returns None when there are zero saved files (route layer turns that
    into a 404, same "nothing to export" convention
    workspace_data.export_workspace_files() uses for zero chats).

    Deliberately doesn't reuse list_files()+get_file()-per-file: this is
    one query for content across every row instead of N+1 round trips,
    since (unlike patch 10's tree, which only needs metadata up front)
    a zip needs every file's content anyway. Built with io.BytesIO
    rather than writing to a temp path on disk the way
    workspace_data.py's NOTES_EXPORTS_DIR export does — that route zips
    files that already exist on disk from export_artifact(); this data
    only ever lives in the workspace_code_files table, so there's no
    on-disk source to zip from and no reason to create one just to
    stream it back out.

    arcname is the file's own file_path, unmodified — _validate_file_path()
    already guarantees it's relative with no '..' segments, so the
    directory structure inside the zip matches the file tree exactly."""
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select file_path, content from workspace_code_files where workspace_id = %s",
            (ws_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            zf.writestr(row["file_path"], row["content"] or "")
    return buf.getvalue()


def delete_file(ws_id: str, file_path: str, user_id: str) -> None:
    """Not currently wired to any UI affordance — included for parity
    with panel_content.delete_content() so a future "delete this file"
    action in patch 10's file-tree view doesn't need a new module
    function."""
    _validate_file_path(file_path)
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "delete from workspace_code_files where workspace_id = %s and file_path = %s",
            (ws_id, file_path),
        )
    write_audit(user_id, "code_file.delete", "workspace", ws_id, {"file_path": file_path})
