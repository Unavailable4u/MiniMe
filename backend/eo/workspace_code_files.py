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

W1.1 update (versioning/history/conflict detection): this module used
to be pure last-write-wins with no history at all (same posture
panel_content.set_content() still documents for itself). That was a
real gap — chat regenerating a file while it's open in the editor
silently clobbered a hand edit, and the reverse (Save landing while a
regen was mid-flight) silently clobbered the newer AI output, with no
way to recover either side. Every row now carries a `version`;
write_file() takes an optional `base_version` and raises
VersionConflictError (api/routes/code.py turns that into a 409 with
the current file) when it's given and stale; every write that
replaces existing content snapshots what it's about to overwrite into
workspace_code_file_versions first, pruned to the most recent
_MAX_HISTORY_VERSIONS rows per file. See write_file(), write_files(),
get_file_history(), and restore_version()'s own docstrings below, and
migrations/0009_code_file_versions.sql's header for the full picture.

W1.2 update (path rules + file/folder operations): _VALID_PATH_CHARS
used to reject every Next.js/SvelteKit route-folder convention --
`[id]/page.js`, `(auth)/login.tsx`, `@modal`, `+page.svelte`, and any
path with a space in it -- which meant write_files() would fail its
WHOLE batch the first time code_writers.py generated a real Next.js
app. Widened below to allow the specific extra punctuation those
conventions use, while every traversal/absolute/length/charset-floor
check stays exactly as strict as before. This update also wires up
real file/folder operations: delete_file() now deletes a single file
OR, by path prefix, an entire folder subtree (folders have no row of
their own -- see create_folder()'s docstring); move_path() renames or
moves a file or folder subtree, carrying its version history forward
under the new path rather than starting over; create_folder() gives
an otherwise-empty directory a `.gitkeep` row so it has something to
render in the file tree. See each function's own docstring below, and
api/routes/code.py for the new DELETE/.../move/.../folders routes that
expose them.

Schema (see migrations/0006_add_workspace_code_files.sql and
migrations/0009_code_file_versions.sql):
    workspace_code_files(
        workspace_id  text references workspaces(id) on delete cascade,
        file_path     text,
        content       text,
        language      text,
        version       int not null default 1,
        updated_at    timestamptz,
        updated_by    text,
        primary key (workspace_id, file_path)
    )
    workspace_code_file_versions(
        workspace_id  text references workspaces(id) on delete cascade,
        file_path     text,
        version       int,
        content       text,
        updated_at    timestamptz,
        updated_by    text,
        source        text,   -- 'user' | 'pipeline' | 'proposal' | 'restore'
        primary key (workspace_id, file_path, version)
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
# W1.2: widened from `[A-Za-z0-9_./-]` to also allow `[ ] ( ) @ + ~ = ,`
# and a literal space -- the exact extra punctuation Next.js/SvelteKit
# route-folder conventions need (`[id]/page.js`, `(auth)/login.tsx`,
# `@modal`, `+page.svelte`) plus filenames with spaces in them. Every
# other check in _validate_file_path() below (leading `/` or `\`, `..`
# segments, normpath escaping the root, max length) is unaffected and
# still runs first -- this only widens which individual CHARACTERS are
# legal, not what a path is allowed to resolve to. `%` and backslash are
# deliberately still excluded: `%` has no legitimate use in a source
# file path and this module's own move_path()/delete_file() build SQL
# LIKE patterns from validated paths below (see _escape_like_prefix()),
# so keeping it out of the allowlist entirely is one less thing that
# escaping needs to defend against; backslash staying out keeps a
# Windows-style path shape rejected by the charset itself, on top of
# the explicit `startswith("\\")` check below.
_VALID_PATH_CHARS = re.compile(r"^[A-Za-z0-9_./\[\]()@+~=, -]+$")

# W1.1: how many past versions workspace_code_file_versions keeps per
# (workspace_id, file_path) — see _prune_history()'s own docstring for
# why this runs after every write instead of as a separate cleanup job.
_MAX_HISTORY_VERSIONS = 30

# W1.1: the closed set of "who/what produced the version being
# replaced" values workspace_code_file_versions.source accepts — kept
# in lockstep with migrations/0009_code_file_versions.sql's CHECK
# constraint by hand (small, stable set; not worth an Enum + DB-side
# derivation the way relay/emitter.py's EventType is, since nothing
# outside this module ever needs to enumerate these).
_VALID_VERSION_SOURCES = {"user", "pipeline", "proposal", "restore"}


class VersionConflictError(Exception):
    """Raised by write_file() when a caller-supplied base_version
    doesn't match the row's current version — same role
    eo/chat_workspace.py's WorkspaceAccessError plays for ownership
    checks: a typed exception the route layer catches specifically,
    rather than a bare ValueError that would be indistinguishable from
    a bad file_path. Carries `current` (the full current-file shape,
    _row_to_file()'s or _empty_file()'s) so api/routes/code.py's PUT
    handler can return it as-is in a 409 body — exactly "the current
    file" the W1.1 spec's Done-when criteria asks for, so the client
    can offer Reload / Keep mine / Compare without a second round trip
    just to find out what it's conflicting with."""

    def __init__(self, current: dict):
        self.current = current
        super().__init__(
            f"version conflict on {current.get('file_path')!r}: base_version "
            f"does not match the current version ({current.get('version')})"
        )


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
            "file_path may only contain letters, numbers, spaces, and "
            "'.', '_', '-', '/', '[', ']', '(', ')', '@', '+', '~', '=', ','"
        )


# W1.2: delete_file() and move_path() below both need "this file, OR
# everything under this folder" -- since folders have no row of their
# own (see create_folder()'s docstring), "everything under this
# folder" is expressed as a SQL `file_path LIKE 'prefix/%'` match
# against the flat file_path column. `_VALID_PATH_CHARS` allows a
# literal underscore in a real file path (e.g. `my_component.jsx`),
# and `_` is a LIKE single-character wildcard -- without escaping it,
# a prefix like `src/my_file` would also match an unrelated
# `src/myXfile`. `%` can't actually appear in a validated file_path
# (excluded from the charset above), and neither can a literal
# backslash, but both are escaped anyway on the same "defend the
# invariant at the point of use, don't just trust it was checked
# upstream" principle _validate_file_path() itself follows.
def _escape_like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    for why the list endpoint deliberately omits it. W1.1: gains
    `version` like every other shape in this module — cheap (already
    on the row) and lets the file-tree view flag "this file has
    history" without a second call."""
    return {
        "workspace_id": row["workspace_id"],
        "file_path": row["file_path"],
        "language": row.get("language"),
        "size": len(row["content"]) if row.get("content") is not None else 0,
        "version": row.get("version", 1),
        "updated_at": _iso(row["updated_at"]),
        "updated_by": row.get("updated_by"),
    }


def _row_to_file(row: dict) -> dict:
    """Full shape, content included — what get_file()/write_file()/
    write_files()/restore_version() return. W1.1: gains `version`."""
    return {
        "workspace_id": row["workspace_id"],
        "file_path": row["file_path"],
        "content": row["content"],
        "language": row.get("language"),
        "version": row.get("version", 1),
        "updated_at": _iso(row["updated_at"]),
        "updated_by": row.get("updated_by"),
    }


def _empty_file(ws_id: str, file_path: str) -> dict:
    """W1.1: version 0 — deliberately not 1, so it reads distinctly
    from "a real saved file at version 1" and so a caller that passes
    base_version=0 to write_file() on a path that's never been saved is
    treated as "I know this doesn't exist yet" (matches current) rather
    than a conflict; base_version=None (the default) still skips the
    check entirely regardless of whether the file exists."""
    return {
        "workspace_id": ws_id,
        "file_path": file_path,
        "content": "",
        "language": None,
        "version": 0,
        "updated_at": None,
        "updated_by": None,
    }


def _row_to_version_entry(row: dict) -> dict:
    """One workspace_code_file_versions row, as returned by
    get_file_history() — no workspace_id/file_path keys (the caller
    already knows which file it asked about; every entry in the list is
    the same file), unlike _row_to_file()'s shape."""
    return {
        "version": row["version"],
        "content": row["content"],
        "source": row.get("source"),
        "updated_at": _iso(row["updated_at"]),
        "updated_by": row.get("updated_by"),
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
            "select workspace_id, file_path, content, language, version, updated_at, updated_by "
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
            "select workspace_id, file_path, content, language, version, updated_at, updated_by "
            "from workspace_code_files where workspace_id = %s and file_path = %s",
            (ws_id, file_path),
        )
        row = cur.fetchone()
    return _row_to_file(row) if row else _empty_file(ws_id, file_path)


def _prune_history(cur, ws_id: str, file_path: str) -> None:
    """Keeps at most _MAX_HISTORY_VERSIONS rows in
    workspace_code_file_versions for one (workspace_id, file_path),
    deleting the oldest beyond that. Runs inside the SAME transaction
    as the write that just added a row, right after it — not a
    separate cleanup job/cron — so the table never actually grows
    past _MAX_HISTORY_VERSIONS + 0 rows for any one file between
    writes; there's no window where an unbounded history sits waiting
    to be swept. Cheap because it's a per-file operation triggered
    exactly once per write to that file, not a table-wide scan."""
    cur.execute(
        """
        delete from workspace_code_file_versions
        where workspace_id = %s and file_path = %s
          and version not in (
              select version from workspace_code_file_versions
              where workspace_id = %s and file_path = %s
              order by version desc
              limit %s
          )
        """,
        (ws_id, file_path, ws_id, file_path, _MAX_HISTORY_VERSIONS),
    )


def _snapshot_and_upsert(cur, ws_id: str, file_path: str, content: str,
                          resolved_language: str | None, user_id: str,
                          source: str, current_row: dict | None) -> dict:
    """The write half every versioned write in this module shares —
    write_file(), write_files() (per-file inside its own batch
    statements, see that function for why it doesn't call this
    directly), and restore_version() all reduce to "snapshot whatever
    is live right now (if anything), then upsert the new content in as
    the next version." Must run inside a transaction that already holds
    a row lock on (ws_id, file_path) via `... for update` (write_file()/
    restore_version() take it themselves before calling this; the
    caller is trusted to have done so) — otherwise two concurrent
    callers could both read the same current_row and race to insert the
    same next version number.

    `current_row` is the row this write is about to replace, already
    fetched by the caller (None for a brand-new file — nothing to
    snapshot, version starts at 1, no prune needed since there's
    nothing yet to prune). `source` must be one of
    _VALID_VERSION_SOURCES; a bad value is a caller bug, not a runtime
    condition, so it fails loud rather than silently defaulting."""
    if source not in _VALID_VERSION_SOURCES:
        raise ValueError(f"source must be one of {sorted(_VALID_VERSION_SOURCES)}, got {source!r}")

    now = _now()
    if current_row is not None:
        cur.execute(
            """
            insert into workspace_code_file_versions
                (workspace_id, file_path, version, content, updated_at, updated_by, source)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (ws_id, file_path, current_row["version"], current_row["content"],
             current_row["updated_at"], current_row["updated_by"], source),
        )
    new_version = (current_row["version"] if current_row is not None else 0) + 1

    cur.execute(
        """
        insert into workspace_code_files (workspace_id, file_path, content, language, version, updated_at, updated_by)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (workspace_id, file_path)
        do update set content = excluded.content, language = excluded.language,
                      version = excluded.version, updated_at = excluded.updated_at,
                      updated_by = excluded.updated_by
        returning workspace_id, file_path, content, language, version, updated_at, updated_by
        """,
        (ws_id, file_path, content, resolved_language, new_version, now, user_id),
    )
    new_row = cur.fetchone()

    if current_row is not None:
        _prune_history(cur, ws_id, file_path)

    return new_row


def write_file(ws_id: str, file_path: str, content: str, user_id: str,
                language: str | None = None, base_version: int | None = None,
                source: str = "user") -> dict:
    """Upsert, with W1.1's optimistic conflict check and version
    history layered in. `language` defaults to an extension-based guess
    (_infer_language) when the caller doesn't already know it, same as
    before this patch.

    base_version: when given (not None), the caller is asserting "I
    last read this file at version N — only write if it's still at
    version N." A brand-new, never-saved file has version 0 (see
    _empty_file()), so base_version=0 correctly matches "I know this
    doesn't exist yet." If the assertion doesn't hold, raises
    VersionConflictError carrying the row's actual current state (the
    route layer turns this into a 409 with the current file — see that
    exception's own docstring) and writes NOTHING. Leave base_version
    unset (the default, None) to skip the check entirely and always
    win, same blind-overwrite behavior this function had before W1.1 —
    that's still the right call for a caller with no "base" to compare
    against (e.g. a first-time create flow that already knows the path
    is new).

    The `select ... for update` below takes a real row lock for the
    rest of this transaction — same reasoning and same pattern
    eo/chat_store.py's append_message() already uses for its own
    read-then-write race — so two rapid concurrent write_file() calls
    against the SAME (ws_id, file_path) are serialized: the second one
    to acquire the lock sees the first one's already-committed version,
    and either proceeds (no base_version given, or it still matches) or
    conflicts correctly instead of both reading stale state and one
    silently clobbering the other.

    source: who/what this write is attributed to in
    workspace_code_file_versions.source for the row this write
    replaces (not for the new row itself — the new row's provenance
    only lives in updated_by until IT is later replaced). Defaults to
    "user" since api/routes/code.py's PUT route (a person's own Save)
    is this function's only direct caller today; restore_version()
    below passes "restore" explicitly. write_files() does not call this
    function (see its own docstring for why) but keeps the same
    contract."""
    _validate_file_path(file_path)
    resolved_language = language or _infer_language(file_path)
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "select workspace_id, file_path, content, language, version, updated_at, updated_by "
            "from workspace_code_files where workspace_id = %s and file_path = %s for update",
            (ws_id, file_path),
        )
        current_row = cur.fetchone()
        current_version = current_row["version"] if current_row is not None else 0

        if base_version is not None and base_version != current_version:
            conflict_shape = _row_to_file(current_row) if current_row is not None else _empty_file(ws_id, file_path)
            raise VersionConflictError(conflict_shape)

        new_row = _snapshot_and_upsert(cur, ws_id, file_path, content, resolved_language,
                                        user_id, source, current_row)
    write_audit(user_id, "code_file.write", "workspace", ws_id,
                {"file_path": file_path, "version": new_row["version"]})
    return _row_to_file(new_row)


def write_files(ws_id: str, files: list[dict], user_id: str, source: str = "pipeline") -> list[dict]:
    """Bulk counterpart to write_file() above — perf audit follow-up
    (registry.py N+1, part 3): api/task_runner.py's _write_code_files()
    used to call write_file() once per generated file after every
    completed tier-3 code task, and each of THOSE calls did its own
    db.cursor() upsert PLUS its own separate write_audit() insert --
    2 pool checkouts per file, so N generated files meant 2N sequential
    Postgres round trips on the hot path of every finished code task.

    This does the same upsert for every file in ONE multi-row
    `INSERT ... VALUES (...), (...), ... ON CONFLICT DO UPDATE`
    (one pool checkout total for the file writes, not N), then a single
    best-effort audit row summarizing the whole batch (one more
    checkout, not N) instead of one audit row per file -- same "a
    written fact, not a blow-by-blow log" tradeoff export_chats()
    already documents elsewhere in this codebase for its own bulk path.

    W1.1 update: still 2 pool checkouts total for N files (not counting
    the history/prune statements below, which run inside that SAME
    checkout's transaction, not a new one) -- versioning didn't reopen
    the N+1 this function exists to close. What changed: the SELECT
    that used to not exist now reads every file_path's CURRENT row (if
    any) up front, `for update`, in the SAME transaction as the writes
    below it -- same row-locking reasoning write_file()'s own docstring
    explains, extended to the whole batch in one statement instead of
    one lock per file. Any file that already existed gets its
    pre-overwrite content snapshotted into workspace_code_file_versions
    (source="pipeline" by default -- this is what makes a tier-3 regen
    recoverable instead of silently destructive, the other half of the
    W1.1 gap described in this module's own header docstring) and its
    version bumped; a brand-new file starts at version 1 with nothing
    to snapshot, same as write_file()'s own current_row=None case.
    Unlike write_file(), this never conflict-checks against a
    base_version -- a tier-3 regen has no "base version" a person
    edited against and, same as before this patch, is meant to always
    win; W1.1 only makes what it overwrites recoverable, not
    conditional. Pruning past _MAX_HISTORY_VERSIONS per file is done as
    ONE statement across every file in the batch that got a snapshot
    (a window-function delete, see below) rather than looping
    _prune_history() once per file -- looping there would put an extra
    round trip back on exactly the N-round-trips-per-file path this
    function exists to avoid.

    `files`: list of {"file_path": str, "content": str,
    "language": str | None} dicts. Every file_path is validated with
    _validate_file_path() up front, same check write_file() runs --
    an invalid path raises immediately, same "fail loud on a bad path"
    posture as the single-file path, since a malformed file_path this
    early is a caller bug, not a runtime condition worth quietly
    dropping the way the fail-open wrapper around each call in
    task_runner.py handles genuine write/network failures.

    Trade-off vs. calling write_file() N times in a loop: N independent
    calls means one file's failure doesn't stop the rest from being
    tried; this single-statement batch validates every path up front
    and either writes every file in `files` or writes none of them and
    raises (a bad path, or a DB/connection-level failure), for the
    caller's own fail-open wrapper to catch at batch granularity
    instead of per-file granularity. In practice file_map's paths come
    from file_manager.py/structure_architect.py, not raw user input, so
    a validation failure here would be a genuine upstream bug rather
    than a routine occurrence -- and a genuine mid-batch DB failure
    (e.g. a connection drop) would very likely have failed most/all of
    the N individual calls too under the same conditions.

    Returns an empty list without touching the database at all when
    `files` is empty -- same "no-op instead of a zero-row round trip"
    discipline eo/registry.py's record_role_hires() companion function
    uses for an empty hire list."""
    if not files:
        return []
    if source not in _VALID_VERSION_SOURCES:
        raise ValueError(f"source must be one of {sorted(_VALID_VERSION_SOURCES)}, got {source!r}")

    now = _now()
    paths = []
    resolved_files = []  # (file_path, content, resolved_language)
    for f in files:
        file_path = f["file_path"]
        _validate_file_path(file_path)
        resolved_language = f.get("language") or _infer_language(file_path)
        paths.append(file_path)
        resolved_files.append((file_path, f["content"], resolved_language))

    with db.cursor(user_id=user_id) as cur:
        # Lock every row this batch is about to touch up front, in one
        # statement -- see this function's own docstring for why `for
        # update` here mirrors write_file()'s single-row lock instead
        # of leaving the batch write to race a concurrent single Save.
        cur.execute(
            "select file_path, content, version, updated_at, updated_by "
            "from workspace_code_files where workspace_id = %s and file_path = any(%s) for update",
            (ws_id, paths),
        )
        existing_by_path = {r["file_path"]: r for r in cur.fetchall()}

        history_params = []
        upsert_params = []
        for file_path, content, resolved_language in resolved_files:
            prior = existing_by_path.get(file_path)
            if prior is not None:
                history_params.append((
                    ws_id, file_path, prior["version"], prior["content"],
                    prior["updated_at"], prior["updated_by"], source,
                ))
            new_version = (prior["version"] if prior is not None else 0) + 1
            upsert_params.append((ws_id, file_path, content, resolved_language, new_version, now, user_id))

        if history_params:
            history_placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s)"] * len(history_params))
            flat_history_params = [p for row in history_params for p in row]
            cur.execute(
                f"""
                insert into workspace_code_file_versions
                    (workspace_id, file_path, version, content, updated_at, updated_by, source)
                values {history_placeholders}
                """,
                flat_history_params,
            )

        upsert_placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s)"] * len(upsert_params))
        flat_upsert_params = [p for row in upsert_params for p in row]
        cur.execute(
            f"""
            insert into workspace_code_files (workspace_id, file_path, content, language, version, updated_at, updated_by)
            values {upsert_placeholders}
            on conflict (workspace_id, file_path)
            do update set content = excluded.content, language = excluded.language,
                          version = excluded.version, updated_at = excluded.updated_at,
                          updated_by = excluded.updated_by
            returning workspace_id, file_path, content, language, version, updated_at, updated_by
            """,
            flat_upsert_params,
        )
        rows = cur.fetchall()

        if history_params:
            # One prune statement for the WHOLE batch, not one per
            # file -- see this function's own docstring. A window
            # function ranks each touched file's own versions newest
            # first; anything past _MAX_HISTORY_VERSIONS for its own
            # (workspace_id, file_path) partition gets deleted, same
            # end state _prune_history()'s per-file NOT IN/LIMIT query
            # leaves write_file()'s single-file callers in.
            touched_paths = [row[1] for row in history_params]
            cur.execute(
                """
                delete from workspace_code_file_versions
                where (workspace_id, file_path, version) in (
                    select workspace_id, file_path, version from (
                        select workspace_id, file_path, version,
                               row_number() over (
                                   partition by workspace_id, file_path
                                   order by version desc
                               ) as rn
                        from workspace_code_file_versions
                        where workspace_id = %s and file_path = any(%s)
                    ) ranked
                    where rn > %s
                )
                """,
                (ws_id, touched_paths, _MAX_HISTORY_VERSIONS),
            )

    # Best-effort, same "never let audit logging break the real
    # operation it's attached to" contract write_audit() itself
    # documents -- a failure here must not undo or fail the file writes
    # above, which have already committed by this point.
    write_audit(
        user_id, "code_files.batch_write", "workspace", ws_id,
        {"file_paths": [r["file_path"] for r in rows], "count": len(rows)},
    )
    return [_row_to_file(r) for r in rows]


def get_file_history(ws_id: str, file_path: str) -> list[dict]:
    """Past versions of one file, most recent first — exactly what's
    stored in workspace_code_file_versions, capped at
    _MAX_HISTORY_VERSIONS rows by the prune step every versioned write
    runs. Deliberately does NOT include the current/live version (that
    row lives in workspace_code_files, not here — get_file() is the
    call for it); Patch W2.6's History panel is the intended caller,
    and is expected to combine "get_file() for the current side" with
    "get_file_history() for the past-versions list" the same way
    list_files() + get_file() already split metadata from content.

    Same `trusted=True` reasoning as list_files()/get_file() above —
    this reads by (workspace_id, file_path) only, no per-call acting
    user, so the RLS policy's trusted_internal branch is what lets a
    normal read through; the route layer (api/routes/code.py) is what
    actually gates on the caller being a member/owner of ws_id before
    this function is ever reached."""
    _validate_file_path(file_path)
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select version, content, updated_at, updated_by, source "
            "from workspace_code_file_versions "
            "where workspace_id = %s and file_path = %s "
            "order by version desc",
            (ws_id, file_path),
        )
        rows = cur.fetchall()
    return [_row_to_version_entry(r) for r in rows]


def restore_version(ws_id: str, file_path: str, version: int, user_id: str) -> dict:
    """Brings an old version's content back as a NEW version, rather
    than deleting the versions in between or rewriting the past — same
    "history is append-only" posture every write in this module already
    takes with workspace_code_file_versions (see write_file()'s own
    docstring, and migrations/0009_code_file_versions.sql's header for
    why). Concretely: restoring version 3 when the file is currently at
    version 7 does NOT delete versions 4-7 or turn the live row back
    into "version 3" — it snapshots version 7 (like any other write
    would) and writes version 3's content in as the new version 8, so
    "what actually happened" stays a straight-line, undo-able story:
    nothing is ever lost, including the restore itself.

    Looks for `version` in two places, in order: the CURRENT live row
    (restoring "the version you're already on" is a legitimate,
    harmless no-op-content write, not a special case worth rejecting),
    then workspace_code_file_versions. Raises ValueError — the route
    layer turns this into a 400, same as a bad file_path — when
    `version` isn't the current version and isn't in history either
    (already pruned past _MAX_HISTORY_VERSIONS, or never existed).

    A file's `language` isn't tracked per-version (see
    migrations/0009_code_file_versions.sql's schema — deliberately
    lean, no language column in the history table, since it's
    overwhelmingly a pure function of file_path anyway via
    _infer_language()), so a restore keeps the CURRENT row's language
    when there is one, and falls back to re-inferring from file_path
    when the file doesn't currently exist (e.g. restoring something
    delete_file() removed — that path still works: this module's
    delete_file() only removes the workspace_code_files row, never the
    history, on purpose, so a deleted file's past stays restorable)."""
    _validate_file_path(file_path)
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "select workspace_id, file_path, content, language, version, updated_at, updated_by "
            "from workspace_code_files where workspace_id = %s and file_path = %s for update",
            (ws_id, file_path),
        )
        current_row = cur.fetchone()

        if current_row is not None and current_row["version"] == version:
            target_content = current_row["content"]
            target_language = current_row["language"]
        else:
            cur.execute(
                "select content from workspace_code_file_versions "
                "where workspace_id = %s and file_path = %s and version = %s",
                (ws_id, file_path, version),
            )
            snapshot = cur.fetchone()
            if snapshot is None:
                raise ValueError(
                    f"version {version} not found for {file_path!r} — it may have "
                    f"already been pruned, or never existed"
                )
            target_content = snapshot["content"]
            target_language = current_row["language"] if current_row is not None else _infer_language(file_path)

        new_row = _snapshot_and_upsert(cur, ws_id, file_path, target_content, target_language,
                                        user_id, "restore", current_row)
    write_audit(user_id, "code_file.restore", "workspace", ws_id,
                {"file_path": file_path, "restored_version": version, "new_version": new_row["version"]})
    return _row_to_file(new_row)


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


def delete_file(ws_id: str, path: str, user_id: str) -> list[str]:
    """W1.2: deletes a single file OR, when `path` is a folder rather
    than an exact file_path, every file under it — folders have no row
    of their own in this schema (see create_folder()'s docstring), so
    "delete this folder" can only ever mean "delete every file whose
    path starts with this prefix." Matches `path` itself (an exact
    file) OR anything starting with `path + "/"` (its subtree),
    mirroring move_path()'s own discovery query below — the two
    functions define "a file or the folder at this path" identically
    on purpose, so a person can't move a subtree that DELETE would
    have treated as empty, or vice versa.

    Idempotent, same as this function's pre-W1.2 single-row DELETE
    always was: a path with nothing under it deletes zero rows and
    still returns (an empty list), rather than raising — same
    "DELETE on something that's already gone is still success" REST
    convention, now just also true for a folder that was already
    empty or never existed. api/routes/code.py's DELETE route relies
    on this to stay a plain 200/204, not a 404, on a repeat call.

    Does NOT snapshot the deleted content into workspace_code_file_versions
    before removing it — same "delete only touches the live row, never
    history" posture this function had before W1.2. That's still the
    right call here: every version PRIOR to the one live at delete time
    is already in history from its own earlier write (nothing new to
    capture there), and restore_version() already handles "the file
    doesn't currently exist" by falling back to
    workspace_code_file_versions (see that function's own docstring) —
    so everything except the exact instant-of-deletion content stays
    recoverable without this function needing to do anything extra.

    Returns the list of file_paths actually deleted (empty for a
    no-op), which api/routes/code.py's DELETE route echoes back so the
    caller can confirm a folder delete's blast radius instead of
    guessing from a bare 200."""
    _validate_file_path(path)
    escaped_prefix = _escape_like_prefix(path)
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "select file_path from workspace_code_files "
            "where workspace_id = %s and (file_path = %s or file_path like %s escape '\\') "
            "for update",
            (ws_id, path, escaped_prefix + "/%"),
        )
        deleted_paths = [r["file_path"] for r in cur.fetchall()]
        if deleted_paths:
            cur.execute(
                "delete from workspace_code_files where workspace_id = %s and file_path = any(%s)",
                (ws_id, deleted_paths),
            )
    write_audit(
        user_id, "code_file.delete", "workspace", ws_id,
        {"path": path, "deleted_paths": deleted_paths, "count": len(deleted_paths)},
    )
    return deleted_paths


def create_folder(ws_id: str, folder_path: str, user_id: str) -> dict:
    """W1.2: folders are implicit in this schema — there's no
    directory row, only file rows, so a folder with nothing in it yet
    has no way to "exist" at all (patch 10's file-tree view builds its
    tree from list_files()'s flat file_path keys; a folder that owns
    zero of those keys simply never appears). This writes a
    `.gitkeep` placeholder file at `{folder_path}/.gitkeep` — the same
    convention git itself uses for "track this otherwise-empty
    directory" — so the tree has one real row to hang the folder off
    of. The folder stops being empty (and this placeholder becomes
    irrelevant clutter, same as in a git repo) the moment a real file
    is saved under it; nothing here ever needs to clean it up.

    Goes through write_file() rather than a hand-rolled insert, so a
    created folder's `.gitkeep` gets the exact same path validation,
    versioning and audit trail (`code_file.write`) as any other write.
    That also makes this idempotent for free: creating a folder that
    already has its `.gitkeep` (e.g. a double-click, or a folder that
    already contains real files someone put a `.gitkeep` in on
    purpose) is just an ordinary no-op rewrite of the same empty
    content, not a case this function needs to special-case."""
    if not folder_path or not folder_path.strip():
        raise ValueError("folder_path cannot be empty")
    placeholder_path = folder_path.rstrip("/") + "/.gitkeep"
    return write_file(ws_id, placeholder_path, "", user_id, language="text", source="user")


def move_path(ws_id: str, from_path: str, to_path: str, user_id: str) -> list[dict]:
    """W1.2: renames or moves a single file OR, by prefix (see
    delete_file()'s docstring for the identical "exact path or its
    subtree" matching rule), an entire folder — in ONE transaction, so
    a folder move either lands completely or not at all, never half
    the files at the new path and half stuck at the old one.

    Unlike a plain overwrite, a move carries the file's identity
    forward rather than treating the new path as a brand-new file:
    every existing row in workspace_code_file_versions for the old
    path is re-keyed to the new path FIRST, then the content that was
    live at the old path is snapshotted into history at the new path
    (source="user" — there's no dedicated "move" source value; this
    doesn't touch migrations/0009_code_file_versions.sql's CHECK
    constraint, and a move is a person-initiated write-like action the
    same way a Save is), and finally the live row is re-inserted under
    the new path one version higher. End state: `get_file_history()`
    on the NEW path returns the file's complete history, old versions
    included, with no gap at the point of the move — same "nothing is
    ever lost, including this operation itself" posture
    restore_version() already documents for itself.

    Refuses (raises ValueError, which the route layer turns into a
    400) rather than silently overwriting when:
      - `from_path == to_path` — not a move.
      - `to_path` is `from_path` itself or lies inside `from_path`'s
        own subtree (`to_path == from_path` or
        `to_path.startswith(from_path + "/")`) — moving a folder into
        one of its own descendants, which this function can't express
        as a single consistent rewrite.
      - nothing exists at `from_path` (no exact file, no subtree).
      - the prefix rewrite would produce two different source files
        landing on the same destination path (shouldn't happen with a
        straightforward prefix swap, but checked rather than assumed).
      - any computed destination path already has a LIVE row that
        ISN'T one of the paths being moved in this same batch — i.e.
        the actual "refuses overwriting an existing target" the W1.2
        spec asks for. A destination that collides with another row
        in the SAME batch (only possible if that row is also moving
        away from under `from_path`) is not a real collision and is
        allowed.

    The initial discovery SELECT takes `for update` on every row this
    move will touch, in one statement — same row-locking reasoning
    write_file()'s and write_files()'s own docstrings give for their
    own `for update` selects — so a concurrent write to a file mid-move
    blocks until the move's transaction commits or rolls back, instead
    of racing it.

    Returns the moved files' new (post-move) shapes, one per file,
    same `_row_to_file()` shape write_file() itself returns."""
    _validate_file_path(from_path)
    _validate_file_path(to_path)
    if from_path == to_path:
        raise ValueError("from and to must be different paths")
    if to_path == from_path or to_path.startswith(from_path + "/"):
        raise ValueError(f"cannot move {from_path!r} into its own subtree ({to_path!r})")

    escaped_prefix = _escape_like_prefix(from_path)
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "select workspace_id, file_path, content, language, version, updated_at, updated_by "
            "from workspace_code_files "
            "where workspace_id = %s and (file_path = %s or file_path like %s escape '\\') "
            "for update",
            (ws_id, from_path, escaped_prefix + "/%"),
        )
        rows = cur.fetchall()
        if not rows:
            raise ValueError(f"nothing found at {from_path!r}")

        moves = []  # [(row, new_path), ...]
        new_paths = []
        for row in rows:
            old_path = row["file_path"]
            new_path = to_path if old_path == from_path else to_path + old_path[len(from_path):]
            _validate_file_path(new_path)
            moves.append((row, new_path))
            new_paths.append(new_path)

        if len(set(new_paths)) != len(new_paths):
            raise ValueError("move would land two different files on the same destination path")

        cur.execute(
            "select file_path from workspace_code_files where workspace_id = %s and file_path = any(%s)",
            (ws_id, new_paths),
        )
        # A destination path that only "collides" because it's ALSO one
        # of the rows this same move is carrying away from under
        # from_path isn't really occupied -- by the time this move
        # finishes, that row won't be there anymore. Only a hit outside
        # that set is a genuine pre-existing file this move would
        # otherwise clobber.
        moved_from_paths = {row["file_path"] for row, _ in moves}
        real_collisions = {r["file_path"] for r in cur.fetchall()} - moved_from_paths
        if real_collisions:
            raise ValueError(f"refusing to overwrite existing path(s): {sorted(real_collisions)}")

        now = _now()
        moved_shapes = []
        for row, new_path in moves:
            old_path = row["file_path"]
            cur.execute(
                "update workspace_code_file_versions set file_path = %s "
                "where workspace_id = %s and file_path = %s",
                (new_path, ws_id, old_path),
            )
            cur.execute(
                """
                insert into workspace_code_file_versions
                    (workspace_id, file_path, version, content, updated_at, updated_by, source)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (ws_id, new_path, row["version"], row["content"],
                 row["updated_at"], row["updated_by"], "user"),
            )
            cur.execute(
                "delete from workspace_code_files where workspace_id = %s and file_path = %s",
                (ws_id, old_path),
            )
            new_version = row["version"] + 1
            cur.execute(
                """
                insert into workspace_code_files
                    (workspace_id, file_path, content, language, version, updated_at, updated_by)
                values (%s, %s, %s, %s, %s, %s, %s)
                returning workspace_id, file_path, content, language, version, updated_at, updated_by
                """,
                (ws_id, new_path, row["content"], row["language"], new_version, now, user_id),
            )
            new_row = cur.fetchone()
            _prune_history(cur, ws_id, new_path)
            moved_shapes.append(_row_to_file(new_row))

    write_audit(
        user_id, "code_files.move", "workspace", ws_id,
        {"from": from_path, "to": to_path, "count": len(moved_shapes),
         "paths": [m["file_path"] for m in moved_shapes]},
    )
    return moved_shapes
