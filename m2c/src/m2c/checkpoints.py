"""
m2c/src/m2c/checkpoints.py -- Patch C08: checkpoints and rewind.

Before an edit, ``snapshot()`` saves what the affected files looked like.
``rewind()`` puts the project back to the state just before a given step ran.
A bug here destroys the user's files, so this module is written around its
failure paths; the happy path is the short part.

What is stored, and where
-------------------------
* **Blobs**: ``~/.m2c/checkpoints/<session>/<sha256>`` (through ``paths``, so
  ``$M2C_HOME`` is honoured).  One file per distinct content, named by the
  SHA-256 of its bytes, so identical content is stored once per session
  (deduplication).  Directories are 0700, blobs 0600, whatever the umask.
  Every blob is written to a temp file in the same directory, flushed,
  ``fsync``ed and renamed into place, so a blob is either whole or absent.
* **The index**: one row per file per step in the C02 store's ``checkpoints``
  table, ``(session_id, step, path, blob_hash, ts, mode)``.  ``path`` is
  relative to the project root and uses ``/``.  ``blob_hash`` is NULL when the
  file **did not exist** before the step (so a rewind deletes it); ``mode`` is
  the permission bits, NULL exactly then.  ``mode`` is the one column this
  patch adds (migration v2 in store/migrations.py).  There is no index outside
  the store.

``snapshot(session_id, step, paths, root)``
-------------------------------------------
Call it *before* the step changes the files.  For each path:

* a regular file: its bytes go into a blob, its mode into the row;
* a path that does not exist yet: recorded as "did not exist";
* a symlink (final component, or a directory on the way): **skipped and
  reported**, never read, never followed -- and because the snapshot skipped
  it, a rewind will not touch it either;
* a directory, FIFO, socket or device: skipped (``not_regular_file``);
* a file above the size cap (``max_file_bytes``, default
  ``MAX_SNAPSHOT_BYTES``, injectable): skipped with ``too_large``;
* a file that changed while it was being read: skipped (``changed_while_reading``)
  rather than recorded torn;
* an unreadable file: skipped (``unreadable``).

Skips are returned in ``SnapshotResult.skipped``; ``result.complete`` is False
if there is any.  A caller that must not edit without a checkpoint refuses the
edit when it is False; one that can live without it warns the user.  Skips
are results, not exceptions, because "this file cannot be checkpointed" is
information, not a bug.

**Refusals are exceptions** (``PathRefusedError``) and refuse the *whole* call
before anything is written: a path outside the root, ``..`` anywhere in a path
(it is ambiguous next to a symlink), a malformed path, and a path that escapes
the project through a symlinked directory.  An unknown session, a root that is
not the session's project directory, and an unusable root raise too.  A path
that reaches this function from a model has already been through the C05 path
guard; this is the second lock, not the first.

All or nothing per call
~~~~~~~~~~~~~~~~~~~~~~~
Order: validate everything, write every blob, *then* insert the step's rows in
**one** database transaction.  An interruption at any point leaves either all
of the call's rows or none of them.  Blobs written before an interruption are
orphans: no row names them, so nothing ever reads them, and ``gc_orphans()``
removes them.  A second call for the same ``(step, path)`` keeps the first row
(the true pre-step state; a later one would be a half-edited file), reported
as ``already_recorded``.

``rewind(session_id, step, root, truncate_conversation=False)``
---------------------------------------------------------------
"The project as it was just before ``step`` ran": for each path that has a row
at ``step`` or later, take the row with the **lowest** step (the earliest
pre-state still on record) and make the file match it -- write the blob back
with the saved mode, or delete the file if it did not exist.  In order:

1. **Plan** (read-only).  Every row is validated: a blob hash that is not 64
   lowercase hex digits, a path that is absolute or contains ``..``, a missing
   mode, is ``CorruptCheckpointError``.  The hash becomes a file name, so a
   tampered index must not be able to name ``../../x``.
2. **Preflight** (read-only).  Every blob is opened without following
   symlinks and its SHA-256 is recomputed.  One missing, truncated or
   corrupt blob raises ``CorruptCheckpointError`` **before any file is
   touched**, so a damaged checkpoint can never produce a half-restored
   project.
3. **Apply**, file by file.  Each directory on the way is opened with
   ``O_NOFOLLOW`` (C05's ``open_dir_chain``); a restore is a temp file in the
   *same directory* (named like C05's, so C05's stale-temp sweep also cleans
   what a killed rewind leaves), the blob copied and re-verified while copying,
   the saved mode applied, ``fsync``, then ``rename``.  Never in place.  Then:

   * a target that is now a symlink, or has a symlinked directory above it,
     or is now a directory, is **skipped and reported**, never written
     through, replaced or removed;
   * a file already in the right state (same bytes, same mode, or already
     absent) is ``unchanged`` and not rewritten;
   * an I/O error on one file is recorded and the others still proceed, then
     ``RewindIncompleteError`` is raised carrying the partial result.
4. **Finish**, in one transaction: the rows for ``step`` and later are deleted
   and, if asked, the ``rewind`` event is appended.  The rows are deleted so
   that the abandoned timeline cannot poison the next one (a later snapshot at
   the same step number would otherwise lose to the stale row).  They are
   deleted only after a pass with no I/O failure, so an interrupted rewind
   keeps everything it needs to be run again.  If the rows changed while the
   rewind was running (a snapshot from a loop that is still going), nothing is
   pruned and ``CheckpointConflictError`` asks for a re-run.

Idempotent: a second run, or a run after an interruption, finds files already
in the right state and ends in the same place.  Blobs are not deleted by
rewind; ``gc_orphans()`` does that.

``truncate_conversation=True`` appends ``("rewind", {"to_step": step})`` to
the session's event log through ``events.append_event``, in the same
transaction as the prune.  The log is append-only, so the marker *is* the
truncation: the loop and the UI read it.  This module never edits or deletes
events.

``gc_orphans(session_id=None)``
-------------------------------
Removes blobs that no row of that session refers to, and stale temp files.
It holds the database write lock while it decides, and it leaves alone any
blob younger than ``min_age_s`` (default one hour): a snapshot writes its
blobs *before* its rows, so a very young unreferenced blob may be one in
flight.  A snapshot that reuses an existing blob refreshes its age for the
same reason.  Only regular files with a blob name are removed; a symlink or
anything else in the directory is left alone.

Honest limits (also what ``REWIND_NOTICE`` tells the user)
----------------------------------------------------------
* Only the files that were passed to ``snapshot()`` are covered.  A shell
  command (C07) that edits files not named to ``snapshot()`` is not
  checkpointed; whoever calls this decides what a step touches.
* Files inside this project only.  Network requests, installed packages,
  database changes, git pushes and anything outside the root are not undone.
* Directories created by a step are left in place (they may hold other
  things); a directory a step removed is re-created with default permissions
  when a file inside it is restored.
* Owner, group, timestamps, extended attributes and ACLs are not saved or
  restored.  A restored file is owned by the current user.
* A hard link is snapshotted by content and restored as a new file (the
  rename breaks the link), like C05's atomic writes do.
* A restore is atomic per file, not across files.  The whole-rewind guarantee
  is "re-runnable until done", not "all at once".
* Not a security boundary against a process that can write inside the project
  while a rewind runs (it can swap a path between a check and its use); the
  sandbox (C11) is that boundary.  This module's checks are no-follow opens
  and re-checks, which stop the honest race and the common attack.

Fail closed / fail open
-----------------------
Everything here is a safety check and fails closed: an unreadable blob, a bad
row, an unexpected ``OSError`` while snapshotting, all stop the operation.
Logging fails open (``log_event`` swallows its own errors) and logs only
counts and the session id: never a file name, never a byte of content.

Fault-injection seams (used by the tests, not by callers)
---------------------------------------------------------
``_insert_rows`` (the index transaction of a snapshot), ``_apply_item`` (one
file of a rewind), ``_verify_blobs`` (the preflight) and ``_stat_signature``
(the size/mtime check) are module-level functions looked up at call time so a
test can make a real failure land at exactly that point.

Place this file at: m2c/src/m2c/checkpoints.py
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from . import paths as _config
from .logging_setup import log_event
from .store import db, events
from .store.db import StoreError, transaction, utc_now_iso
from .store.sessions import SESSION_ID_RE, require_session
from .tools.files import TEMP_PREFIX, TEMP_SUFFIX
from .tools.paths import PathGuardError, SymlinkError, ToolError, assert_safe_root, open_dir_chain

logger = logging.getLogger("m2c.checkpoints")

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
CHECKPOINTS_DIRNAME = "checkpoints"
DIR_MODE = 0o700
BLOB_MODE = 0o600

# Larger than anything the edit tools can edit (Caps.max_file_bytes, 1 MB) or
# write (Caps.max_write_bytes, 2 MB), so every file those tools can change can
# be checkpointed; a test pins that.  A bigger file (delete_path has no size
# limit) is skipped with ``too_large`` and the caller decides.
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024

# An unreferenced blob younger than this is left alone by gc_orphans (a
# snapshot writes blobs before its rows).
GC_MIN_AGE_S = 3600.0

EVENT_REWIND = "rewind"
MAX_PATH_CHARS = 4096

# Shown by the terminal UI verbatim, next to every rewind.
REWIND_NOTICE = (
    "Rewind restores files in this project only. It does not undo network requests, "
    "installed packages, database changes, git pushes or anything else outside these files."
)

# Why a path was skipped (or, FAIL_IO, why a restore failed).  Stable strings.
SKIP_SYMLINK = "symlink"
SKIP_SYMLINKED_DIR = "symlinked_directory"
SKIP_TOO_LARGE = "too_large"
SKIP_NOT_REGULAR = "not_regular_file"
SKIP_CHANGED = "changed_while_reading"
SKIP_PARENT_NOT_DIR = "parent_not_a_directory"
SKIP_IS_DIRECTORY = "is_a_directory"
SKIP_UNREADABLE = "unreadable"
FAIL_IO = "io_error"

REASON_TEXT: Mapping[str, str] = {
    SKIP_SYMLINK: "is a symbolic link; links are never read, followed or replaced",
    SKIP_SYMLINKED_DIR: "has a symbolic link as one of its directories; it is never followed",
    SKIP_TOO_LARGE: "is larger than the checkpoint size limit",
    SKIP_NOT_REGULAR: "is not a regular file (a directory, pipe, socket or device)",
    SKIP_CHANGED: "changed while it was being read, so no consistent copy could be taken",
    SKIP_PARENT_NOT_DIR: "has a file where a directory should be",
    SKIP_IS_DIRECTORY: "is now a directory; it was left alone",
    SKIP_UNREADABLE: "cannot be read (permission denied)",
    FAIL_IO: "could not be written (an I/O error); run the rewind again",
}

_CHUNK = 64 * 1024
_BLOB_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOB_TEMP_PREFIX = ".m2c-ckpt."
_BLOB_TEMP_RE = re.compile(r"^\.m2c-ckpt\.(\d+)\.[0-9a-f]{16}\.tmp$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class CheckpointError(StoreError):
    """A checkpoint operation could not be done safely.

    Messages are written for the person at the terminal.  They may name a
    project-relative path; they never contain file contents.
    """


class PathRefusedError(CheckpointError):
    """A path was refused: outside the project, malformed, or leaving it through a symlink."""


class RootError(CheckpointError):
    """The project root is unusable, or is not the directory this session belongs to."""


class CorruptCheckpointError(CheckpointError):
    """A saved copy or an index row is missing, damaged or tampered with.  Nothing was changed."""


class CheckpointConflictError(CheckpointError):
    """The checkpoints changed while a rewind was running; nothing was pruned.  Run it again."""


class RewindIncompleteError(CheckpointError):
    """Some files could not be restored (I/O errors).  ``result`` says what was and was not done.

    The checkpoints are kept, so running the same rewind again finishes the job.
    """

    def __init__(self, message: str, result: "RewindResult") -> None:
        super().__init__(message)
        self.result = result


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
def _safe_text(name: str) -> str:
    """A path made safe to show: control characters (a newline could forge a line) become '?'."""
    return "".join(c if c.isprintable() else "?" for c in str(name))


class Skipped(NamedTuple):
    """A path that was not checkpointed or not restored, and why (``reason`` is a stable code)."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{_safe_text(self.path)} {REASON_TEXT.get(self.reason, self.reason)}"


@dataclass(frozen=True)
class SnapshotResult:
    """What ``snapshot()`` did.  Path tuples are sorted.

    ``recorded``          files whose bytes were saved
    ``absent``            paths recorded as "did not exist"
    ``already_recorded``  paths that already had a row for this step (kept)
    ``skipped``           ``Skipped`` entries: not checkpointed, with the reason
    ``blobs_written``     new blob files    ``blobs_reused``  existing blobs found by content
    """

    session_id: str
    step: int
    recorded: tuple[str, ...]
    absent: tuple[str, ...]
    already_recorded: tuple[str, ...]
    skipped: tuple[Skipped, ...]
    blobs_written: int
    blobs_reused: int

    @property
    def complete(self) -> bool:
        """True if every requested path is covered (nothing was skipped)."""
        return not self.skipped


@dataclass(frozen=True)
class RewindResult:
    """What ``rewind()`` did.  Path tuples are sorted.

    ``restored``   files written back (bytes and mode)   ``deleted``  files removed
    ``unchanged``  already in the right state            ``skipped``  left alone, with the reason
    ``failed``     I/O errors (only on ``RewindIncompleteError.result``)
    """

    to_step: int
    restored: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: tuple[str, ...]
    skipped: tuple[Skipped, ...]
    failed: tuple[Skipped, ...]
    conversation_truncated: bool

    @property
    def complete(self) -> bool:
        return not self.skipped and not self.failed

    @property
    def notice(self) -> str:
        """The sentence the UI shows with every rewind."""
        return REWIND_NOTICE


@dataclass(frozen=True)
class GcResult:
    blobs_removed: int
    temps_removed: int
    bytes_freed: int


# --------------------------------------------------------------------------
# Argument checks
# --------------------------------------------------------------------------
def _check_session_id(value: object) -> str:
    # The id becomes a directory name: this check is what keeps "../x" out of it.
    if not isinstance(value, str) or not SESSION_ID_RE.fullmatch(value):
        raise ValueError("session_id must match [A-Za-z0-9_-]{8,64}")
    return value


def _check_step(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("step must be a non-negative integer")
    return value


def _check_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@contextlib.contextmanager
def _connection(conn: sqlite3.Connection | None, environ: Mapping[str, str] | None) -> Iterator[sqlite3.Connection]:
    """The caller's connection, or a private one (opened and closed here)."""
    if conn is not None:
        yield conn
        return
    own = db.open_db(environ)
    try:
        yield own
    finally:
        own.close()


def _inside(real: str, root: str) -> bool:
    return real == root or real.startswith(root + os.sep)


def _validate_root(root: object, session, environ: Mapping[str, str] | None) -> str:
    """The resolved project root, or ``RootError``.

    Besides C05's ``assert_safe_root`` (exists, is a directory, not ``/`` or
    the home directory, not inside M2C's own state directory), the root must
    be the directory this session belongs to (guide 11.2: sessions are tied to
    a directory).  A rewind run from another directory would otherwise write
    old copies over unrelated files that happen to share a relative path.
    """
    if isinstance(root, bool) or not isinstance(root, (str, os.PathLike)):
        raise RootError("the project root must be a path")
    try:
        resolved = assert_safe_root(root, protected_dirs=[_config.config_dir(environ)])
    except PathGuardError as exc:
        raise RootError(f"cannot use this directory as the project root: {exc.message}") from None
    real = str(resolved)
    if os.path.realpath(session.project_path) != real:
        raise RootError(
            "this is not the project directory the session belongs to; "
            "checkpoints are only taken and restored in the session's own project"
        )
    return real


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def _relative_parts(root_parts: tuple[str, ...], raw: object) -> tuple[str, ...]:
    """Components of ``raw`` below the root, purely lexically, or ``PathRefusedError``.

    Components are compared one by one (never as a string prefix, so
    ``/work/proj-x`` is not inside ``/work/proj``).  ``..`` is refused even
    when it would stay inside: a lexical clean-up and the OS disagree about it
    next to a symlink, and callers have real relative paths to give.
    """
    if isinstance(raw, os.PathLike):
        raw = os.fspath(raw)
    if not isinstance(raw, str):
        raise PathRefusedError("a path must be text")
    if raw == "" or "\0" in raw:
        raise PathRefusedError("a path must not be empty or contain a NUL byte")
    if len(raw) > MAX_PATH_CHARS:
        raise PathRefusedError("a path is too long")
    if raw.endswith("/"):
        raise PathRefusedError("a path must name a file, not end in a separator")
    parts = tuple(p for p in raw.split("/") if p not in ("", "."))
    if ".." in parts:
        raise PathRefusedError(f"{_safe_text(raw)}: '..' is not allowed in a checkpoint path")
    if raw.startswith("/"):
        n = len(root_parts)
        if parts[:n] != root_parts:
            raise PathRefusedError(f"{_safe_text(raw)}: outside the project")
        parts = parts[n:]
    if not parts:
        raise PathRefusedError("the project root itself cannot be checkpointed")
    return parts


def _normalise_paths(paths: object, root: str) -> list[tuple[str, ...]]:
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError("paths must be a list of paths, not a single path")
    try:
        items = list(paths)  # type: ignore[call-overload]
    except TypeError:
        raise ValueError("paths must be a list of paths") from None
    root_parts = tuple(p for p in root.split("/") if p)
    unique: dict[str, tuple[str, ...]] = {}
    for raw in items:
        parts = _relative_parts(root_parts, raw)
        unique.setdefault("/".join(parts), parts)
    normalised = [unique[key] for key in sorted(unique)]
    # A directory on the way that is a symlink leading outside the project: refuse the
    # whole call now, before a single blob is written.  (One that stays inside is only
    # skipped, later, when the no-follow walk meets it.)
    for parts in normalised:
        try:
            parent = os.path.realpath(os.path.join(root, *parts[:-1]))
        except (OSError, RuntimeError, ValueError):
            raise PathRefusedError(f"{_safe_text('/'.join(parts))}: cannot be resolved") from None
        if not _inside(parent, root):
            raise PathRefusedError(f"{_safe_text('/'.join(parts))}: leaves the project through a symbolic link")
    return normalised


def _parts_from_row(path: object) -> tuple[str, ...]:
    """Components of a path read back from the index, or ``CorruptCheckpointError``."""
    if not isinstance(path, str) or path == "" or "\0" in path or len(path) > MAX_PATH_CHARS:
        raise CorruptCheckpointError("the checkpoint index has an entry with an invalid path; nothing was changed")
    parts = tuple(path.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        raise CorruptCheckpointError("the checkpoint index has an entry with an invalid path; nothing was changed")
    return parts


# --------------------------------------------------------------------------
# Low-level file helpers
# --------------------------------------------------------------------------
def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        offset += written


def _fsync_dir(fd: int) -> None:
    """Make a rename or unlink durable.  The data is already in place either way, so this fails open."""
    with contextlib.suppress(OSError):
        os.fsync(fd)


def _stat_signature(fd: int) -> tuple[int, int, int, int]:
    """What must be the same before and after a read for the bytes to be one consistent copy."""
    st = os.fstat(fd)
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def _open_private_dir(parent_fd: int, name: str) -> int:
    """Open (creating if needed) ``name`` under ``parent_fd`` as a real directory, 0700, ours."""
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CheckpointError(
                f"{name} in the checkpoint store is not a real directory (a link or a file is in its place); "
                "refusing to use it"
            ) from None
        raise
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise CheckpointError(f"{name} in the checkpoint store is owned by another user; refusing to use it")
        if stat.S_IMODE(st.st_mode) != DIR_MODE:
            os.fchmod(fd, DIR_MODE)
            if stat.S_IMODE(os.fstat(fd).st_mode) != DIR_MODE:
                raise CheckpointError(
                    f"cannot set mode 0700 on {name} in the checkpoint store: the filesystem ignores "
                    "permission changes (typical under /mnt/c); choose a directory on the Linux filesystem"
                )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_existing_dir(parent_fd: int, name: str) -> int | None:
    """Open an existing directory without following a link; None if it is absent."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CheckpointError(
                f"{name} in the checkpoint store is not a real directory; refusing to use it"
            ) from None
        raise


def _blob_problem(dir_fd: int | None, digest: str) -> str | None:
    """None if the blob exists, is a regular file and hashes to its name; else what is wrong."""
    if dir_fd is None:
        return "missing"
    try:
        fd = os.open(digest, _FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"  # includes a symlink in its place (ELOOP)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not a regular file"
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)
    return None if hasher.hexdigest() == digest else "corrupt"


class _TooLarge(Exception):
    """Internal: the file being copied went past the size cap."""


@dataclass(frozen=True)
class _Staged:
    tmp: str
    digest: str
    size: int


class _BlobStore:
    """The blobs of one session: ``<config dir>/checkpoints/<session>/``.

    Holds one directory descriptor, opened lazily and closed by the ``with``
    block.  Writing creates the directories (private, never through a link);
    reading never creates anything.
    """

    def __init__(self, environ: Mapping[str, str] | None, session_id: str) -> None:
        self._environ = environ
        self._session_id = _check_session_id(session_id)
        self._fd: int | None = None

    def __enter__(self) -> "_BlobStore":
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def dir_fd(self, create: bool) -> int | None:
        if self._fd is not None:
            return self._fd
        if create:
            cfg = _config.prepare_config_dir(self._environ)
            cfd = os.open(str(cfg), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                top = _open_private_dir(cfd, CHECKPOINTS_DIRNAME)
                try:
                    self._fd = _open_private_dir(top, self._session_id)
                finally:
                    os.close(top)
            finally:
                os.close(cfd)
            return self._fd
        try:
            cfd = os.open(str(_config.config_dir(self._environ)), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        try:
            top = _open_existing_dir(cfd, CHECKPOINTS_DIRNAME)
            if top is None:
                return None
            try:
                self._fd = _open_existing_dir(top, self._session_id)
            finally:
                os.close(top)
        finally:
            os.close(cfd)
        return self._fd

    def stage(self, src_fd: int, cap: int) -> _Staged:
        """Copy ``src_fd`` into a private temp file, hashing as it goes.  Raises ``_TooLarge``."""
        fd = self.dir_fd(True)
        assert fd is not None
        tmp = f"{_BLOB_TEMP_PREFIX}{os.getpid()}.{secrets.token_hex(8)}{TEMP_SUFFIX}"
        tfd = os.open(tmp, _NEW_FLAGS, BLOB_MODE, dir_fd=fd)
        done = False
        try:
            hasher = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(src_fd, _CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise _TooLarge
                hasher.update(chunk)
                _write_all(tfd, chunk)
            os.fchmod(tfd, BLOB_MODE)  # exactly 0600 whatever the umask
            os.fsync(tfd)
            done = True
            return _Staged(tmp, hasher.hexdigest(), total)
        finally:
            os.close(tfd)
            if not done:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=fd)

    def discard(self, staged: _Staged) -> None:
        fd = self.dir_fd(True)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.unlink(staged.tmp, dir_fd=fd)

    def commit(self, staged: _Staged) -> bool:
        """Put the staged blob in place; True if an identical, *verified* blob was already there.

        Deduplication verifies the existing blob instead of trusting its name:
        a blob that rotted on disk is replaced by the good bytes now in hand,
        so a snapshot that succeeds never leaves a rewind that cannot.
        """
        fd = self.dir_fd(True)
        assert fd is not None
        try:
            if _blob_problem(fd, staged.digest) is None:
                with contextlib.suppress(OSError):  # refresh the age so gc leaves it alone mid-snapshot
                    os.utime(staged.digest, None, dir_fd=fd, follow_symlinks=False)
                self.discard(staged)
                return True
            os.replace(staged.tmp, staged.digest, src_dir_fd=fd, dst_dir_fd=fd)
            _fsync_dir(fd)
            return False
        except BaseException:
            self.discard(staged)
            raise


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
class _Captured(NamedTuple):
    path: str
    blob_hash: str | None   # None: did not exist
    mode: int | None
    reused: bool


def _capture_open_file(rel: str, fd: int, store: _BlobStore, cap: int) -> _Captured | Skipped:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):  # swapped for a pipe or similar after the lstat
        return Skipped(rel, SKIP_NOT_REGULAR)
    before = _stat_signature(fd)
    if before[0] > cap:
        return Skipped(rel, SKIP_TOO_LARGE)
    try:
        staged = store.stage(fd, cap)
    except _TooLarge:
        return Skipped(rel, SKIP_TOO_LARGE)
    try:
        if _stat_signature(fd) != before:
            store.discard(staged)
            return Skipped(rel, SKIP_CHANGED)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        reused = store.commit(staged)
    except BaseException:
        store.discard(staged)
        raise
    return _Captured(rel, staged.digest, mode, reused)


def _capture(root: str, parts: tuple[str, ...], store: _BlobStore, cap: int) -> _Captured | Skipped:
    """The pre-step state of one path: a blob, "absent", or a reason it cannot be checkpointed."""
    rel = "/".join(parts)
    absent = _Captured(rel, None, None, False)
    try:
        pfd = open_dir_chain(Path(root), parts[:-1])
    except FileNotFoundError:
        return absent  # a directory on the way does not exist, so neither does the file
    except NotADirectoryError:
        return Skipped(rel, SKIP_PARENT_NOT_DIR)
    except SymlinkError:
        real = os.path.realpath(os.path.join(root, *parts[:-1]))
        if not _inside(real, root):  # re-check: it may have become one since the up-front pass
            raise PathRefusedError(f"{_safe_text(rel)}: leaves the project through a symbolic link") from None
        return Skipped(rel, SKIP_SYMLINKED_DIR)
    except PermissionError:
        return Skipped(rel, SKIP_UNREADABLE)
    except ToolError:
        raise PathRefusedError(f"{_safe_text(rel)}: not a usable path") from None
    try:
        name = parts[-1]
        try:
            lst = os.lstat(name, dir_fd=pfd)
        except FileNotFoundError:
            return absent
        if stat.S_ISLNK(lst.st_mode):
            return Skipped(rel, SKIP_SYMLINK)
        if not stat.S_ISREG(lst.st_mode):
            return Skipped(rel, SKIP_NOT_REGULAR)
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=pfd)  # O_NOFOLLOW, and O_NONBLOCK so a FIFO cannot hang us
        except FileNotFoundError:
            return absent
        except PermissionError:
            return Skipped(rel, SKIP_UNREADABLE)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return Skipped(rel, SKIP_SYMLINK)
            raise
        try:
            return _capture_open_file(rel, fd, store, cap)
        finally:
            os.close(fd)
    finally:
        os.close(pfd)


def _insert_rows(
    conn: sqlite3.Connection, session_id: str, step: int, rows: list[tuple[str, str | None, int | None]]
) -> list[str]:
    """Record a step's rows in ONE transaction; return the paths that already had a row (kept).

    This is the only place a snapshot touches the index, and the line between
    "nothing recorded" and "everything recorded".  ``ON CONFLICT DO NOTHING``
    (not ``OR IGNORE``, which would also swallow a CHECK failure) keeps the
    first row of a ``(session, step, path)``.
    """
    ignored: list[str] = []
    if not rows:
        return ignored
    stamp = utc_now_iso()
    try:
        with transaction(conn):
            for path, blob_hash, mode in rows:
                cur = conn.execute(
                    "INSERT INTO checkpoints (session_id, step, path, blob_hash, ts, mode) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (session_id, step, path) DO NOTHING",
                    (session_id, step, path, blob_hash, stamp, mode),
                )
                if cur.rowcount == 0:
                    ignored.append(path)
    except sqlite3.Error as exc:
        raise CheckpointError(f"could not record the checkpoint index: {exc}") from exc
    return ignored


def snapshot(
    session_id: str,
    step: int,
    paths: Iterable[str | os.PathLike[str]],
    root: str | os.PathLike[str],
    *,
    conn: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    max_file_bytes: int = MAX_SNAPSHOT_BYTES,
) -> SnapshotResult:
    """Record the pre-edit state of ``paths`` (relative to, or inside, ``root``) as ``step``.

    Call this before the step changes the files.  Returns a ``SnapshotResult``;
    raises ``PathRefusedError`` (nothing recorded) for a path outside the
    project, ``RootError`` for a root that is not the session's project, and
    ``CheckpointError`` for anything unexpected.  See the module docstring for
    the rules, in particular "all or nothing".

    ``conn`` is the caller's store connection (it joins the caller's
    transaction, if one is open); without it a private connection is opened
    and closed.  ``max_file_bytes`` is the size cap.
    """
    _check_session_id(session_id)
    _check_step(step)
    _check_positive_int("max_file_bytes", max_file_bytes)
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError("paths must be a list of paths, not a single path")

    with _connection(conn, environ) as c:
        session = require_session(c, session_id)
        real_root = _validate_root(root, session, environ)
        wanted = _normalise_paths(paths, real_root)
        have = {
            row[0]
            for row in c.execute("SELECT path FROM checkpoints WHERE session_id = ? AND step = ?", (session_id, step))
        }

        already: list[str] = []
        skipped: list[Skipped] = []
        captured: list[_Captured] = []
        with _BlobStore(environ, session_id) as store:
            for parts in wanted:
                rel = "/".join(parts)
                if rel in have:
                    already.append(rel)
                    continue
                try:
                    outcome = _capture(real_root, parts, store, max_file_bytes)
                except OSError as exc:
                    raise CheckpointError(
                        f"cannot take a checkpoint of {_safe_text(rel)} "
                        f"({exc.strerror or type(exc).__name__}); nothing was recorded"
                    ) from exc
                if isinstance(outcome, Skipped):
                    skipped.append(outcome)
                else:
                    captured.append(outcome)

        # Every blob is on disk.  Now, and only now, the index -- in one transaction.
        ignored = _insert_rows(c, session_id, step, [(x.path, x.blob_hash, x.mode) for x in captured])

    kept = set(ignored)
    result = SnapshotResult(
        session_id=session_id,
        step=step,
        recorded=tuple(x.path for x in captured if x.blob_hash is not None and x.path not in kept),
        absent=tuple(x.path for x in captured if x.blob_hash is None and x.path not in kept),
        already_recorded=tuple(sorted(already + ignored)),
        skipped=tuple(sorted(skipped)),
        blobs_written=sum(1 for x in captured if x.blob_hash is not None and not x.reused),
        blobs_reused=sum(1 for x in captured if x.blob_hash is not None and x.reused),
    )
    log_event(logger, logging.INFO, "checkpoints.snapshot", run_id=session_id, count=len(result.recorded) + len(result.absent))
    if result.skipped:
        log_event(logger, logging.WARNING, "checkpoints.snapshot_skipped", run_id=session_id, count=len(result.skipped))
    return result


# --------------------------------------------------------------------------
# rewind
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _PlanItem:
    step: int
    path: str
    parts: tuple[str, ...]
    blob_hash: str | None   # None: the file did not exist, so rewind deletes it
    mode: int | None


_ROWS_SQL = (
    "SELECT step, path, blob_hash, mode FROM checkpoints "
    "WHERE session_id = ? AND step >= ? ORDER BY path, step"
)


def _validated(step: int, path: object, blob_hash: object, mode: object) -> _PlanItem:
    parts = _parts_from_row(path)
    if blob_hash is None:
        return _PlanItem(step, "/".join(parts), parts, None, None)
    if not isinstance(blob_hash, str) or not _BLOB_RE.fullmatch(blob_hash):
        raise CorruptCheckpointError("the checkpoint index has an entry with an invalid blob name; nothing was changed")
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise CorruptCheckpointError(
            f"the checkpoint index has no usable file mode for {_safe_text(str(path))}; nothing was changed"
        )
    return _PlanItem(step, "/".join(parts), parts, blob_hash, mode)


def _load_plan(conn: sqlite3.Connection, session_id: str, step: int) -> tuple[list[_PlanItem], list[tuple]]:
    """The file-by-file plan (earliest row per path at ``step`` or later) and the rows it came from."""
    raw = [tuple(r) for r in conn.execute(_ROWS_SQL, (session_id, step))]
    plan: dict[str, _PlanItem] = {}
    for row_step, path, blob_hash, mode in raw:
        if path in plan:  # ordered by step within a path: the first is the earliest pre-state
            continue
        plan[path] = _validated(row_step, path, blob_hash, mode)
    return sorted(plan.values(), key=lambda item: item.path), raw


def _verify_blobs(store: _BlobStore, plan: list[_PlanItem]) -> None:
    """Preflight: every blob the plan needs exists and hashes to its name.  Read-only."""
    dir_fd = store.dir_fd(False)
    checked: dict[str, str | None] = {}
    for item in plan:
        if item.blob_hash is None:
            continue
        if item.blob_hash not in checked:
            checked[item.blob_hash] = _blob_problem(dir_fd, item.blob_hash)
        problem = checked[item.blob_hash]
        if problem is not None:
            raise CorruptCheckpointError(
                f"cannot rewind: the saved copy of {_safe_text(item.path)} is {problem}; no file was changed"
            )


def _matches_pre_state(pfd: int, name: str, item: _PlanItem) -> bool:
    """True if the file already has the saved bytes and mode (so it need not be rewritten)."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=pfd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != item.mode:
            return False
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
        return hasher.hexdigest() == item.blob_hash
    except OSError:
        return False
    finally:
        os.close(fd)


def _write_restored(pfd: int, name: str, store: _BlobStore, item: _PlanItem) -> None:
    """Temp file in the target's directory -> copy and re-verify -> mode -> fsync -> rename."""
    assert item.blob_hash is not None and item.mode is not None
    blob_dir = store.dir_fd(False)
    if blob_dir is None:
        raise CorruptCheckpointError(f"cannot rewind: the saved copy of {_safe_text(item.path)} is missing")
    try:
        src = os.open(item.blob_hash, _FILE_FLAGS, dir_fd=blob_dir)
    except OSError:
        raise CorruptCheckpointError(
            f"cannot rewind: the saved copy of {_safe_text(item.path)} is missing or unreadable"
        ) from None
    temp = f"{TEMP_PREFIX}{os.getpid()}.{secrets.token_hex(8)}{TEMP_SUFFIX}"
    tfd = os.open(temp, _NEW_FLAGS, 0o600, dir_fd=pfd)
    leftover = True
    try:
        try:
            hasher = hashlib.sha256()
            while True:
                chunk = os.read(src, _CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                _write_all(tfd, chunk)
            if hasher.hexdigest() != item.blob_hash:  # damaged since the preflight: never write bad bytes
                raise CorruptCheckpointError(
                    f"cannot rewind: the saved copy of {_safe_text(item.path)} is corrupt; this file was not changed"
                )
            os.fchmod(tfd, item.mode)
            os.fsync(tfd)
        finally:
            os.close(tfd)
            os.close(src)
        os.replace(temp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        leftover = False
        _fsync_dir(pfd)
    finally:
        if leftover:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=pfd)


def _apply_item(root: str, store: _BlobStore, item: _PlanItem) -> tuple[str, str | None]:
    """Make one file match its saved pre-state.  Returns ``(kind, reason)``.

    ``kind`` is ``restored``, ``deleted``, ``unchanged`` or ``skipped`` (then
    ``reason`` says why).  An ``OSError`` is not handled here: ``rewind`` records
    it as a failure of this file and carries on with the others.
    """
    parts = item.parts
    restoring = item.blob_hash is not None
    try:
        pfd = open_dir_chain(Path(root), parts[:-1], create=restoring)  # O_NOFOLLOW at every step
    except FileNotFoundError:
        return ("unchanged", None)  # only when deleting: no such directory, so no such file
    except SymlinkError:
        return ("skipped", SKIP_SYMLINKED_DIR)
    except NotADirectoryError:
        return ("skipped", SKIP_PARENT_NOT_DIR)
    try:
        name = parts[-1]
        try:
            lst: os.stat_result | None = os.lstat(name, dir_fd=pfd)
        except FileNotFoundError:
            lst = None
        if lst is not None:
            if stat.S_ISLNK(lst.st_mode):
                return ("skipped", SKIP_SYMLINK)
            if stat.S_ISDIR(lst.st_mode):
                return ("skipped", SKIP_IS_DIRECTORY)
            if not stat.S_ISREG(lst.st_mode):
                return ("skipped", SKIP_NOT_REGULAR)
        if not restoring:
            if lst is None:
                return ("unchanged", None)
            os.unlink(name, dir_fd=pfd)
            _fsync_dir(pfd)
            return ("deleted", None)
        if lst is not None and _matches_pre_state(pfd, name, item):
            return ("unchanged", None)
        _write_restored(pfd, name, store, item)
        return ("restored", None)
    finally:
        os.close(pfd)


def _finish(
    conn: sqlite3.Connection, session_id: str, step: int, expected_rows: list[tuple], truncate: bool
) -> None:
    """Prune the consumed rows and (optionally) append the marker: one transaction, or neither."""
    try:
        with transaction(conn):
            if [tuple(r) for r in conn.execute(_ROWS_SQL, (session_id, step))] != expected_rows:
                raise CheckpointConflictError(
                    "the checkpoints changed while the rewind was running; nothing was pruned. "
                    "Run the rewind again"
                )
            conn.execute("DELETE FROM checkpoints WHERE session_id = ? AND step >= ?", (session_id, step))
            if truncate:
                events.append_event(conn, session_id, EVENT_REWIND, {"to_step": step})
    except sqlite3.Error as exc:
        raise CheckpointError(f"could not finish the rewind in the store: {exc}") from exc


def rewind(
    session_id: str,
    step: int,
    root: str | os.PathLike[str],
    truncate_conversation: bool = False,
    *,
    conn: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
) -> RewindResult:
    """Restore the project to the state just before ``step`` ran.

    See the module docstring for the order of work and the guarantees.  Raises
    ``CorruptCheckpointError`` (before any file is touched) for a damaged
    checkpoint, ``RewindIncompleteError`` (checkpoints kept; run it again) if
    some file could not be written, ``CheckpointConflictError`` if the
    checkpoints changed underneath it, and ``RootError`` if ``root`` is not the
    session's project.  Skips (a symlink where a file was, a directory in the
    way) are not errors: they come back in ``result.skipped``.

    With ``truncate_conversation=True`` a ``rewind`` event ``{"to_step": step}``
    is appended to the session's event log, in the same transaction as the
    prune.  Show ``REWIND_NOTICE`` to the user alongside the result.
    """
    _check_session_id(session_id)
    _check_step(step)
    if not isinstance(truncate_conversation, bool):
        raise ValueError("truncate_conversation must be True or False")

    with _connection(conn, environ) as c:
        session = require_session(c, session_id)
        real_root = _validate_root(root, session, environ)
        plan, expected_rows = _load_plan(c, session_id, step)

        restored: list[str] = []
        deleted: list[str] = []
        unchanged: list[str] = []
        skipped: list[Skipped] = []
        failed: list[Skipped] = []
        with _BlobStore(environ, session_id) as store:
            _verify_blobs(store, plan)  # read-only; raises before a single file is touched
            for item in plan:
                try:
                    kind, reason = _apply_item(real_root, store, item)
                except OSError:
                    failed.append(Skipped(item.path, FAIL_IO))
                    continue
                if kind == "restored":
                    restored.append(item.path)
                elif kind == "deleted":
                    deleted.append(item.path)
                elif kind == "unchanged":
                    unchanged.append(item.path)
                else:
                    skipped.append(Skipped(item.path, reason or SKIP_NOT_REGULAR))

        result = RewindResult(
            to_step=step,
            restored=tuple(restored),
            deleted=tuple(deleted),
            unchanged=tuple(unchanged),
            skipped=tuple(skipped),
            failed=tuple(failed),
            conversation_truncated=False,
        )
        if failed:
            log_event(logger, logging.ERROR, "checkpoints.rewind_incomplete", run_id=session_id,
                      code=FAIL_IO, count=len(failed))
            raise RewindIncompleteError(
                f"{len(failed)} file(s) could not be restored; the checkpoints were kept, "
                "so running the same rewind again will finish the job",
                result,
            )
        _finish(c, session_id, step, expected_rows, truncate_conversation)

    result = dataclasses.replace(result, conversation_truncated=truncate_conversation)
    log_event(logger, logging.INFO, "checkpoints.rewind", run_id=session_id,
              count=len(result.restored) + len(result.deleted))
    if result.skipped:
        log_event(logger, logging.WARNING, "checkpoints.rewind_skipped", run_id=session_id, count=len(result.skipped))
    return result


# --------------------------------------------------------------------------
# gc_orphans
# --------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except OSError:
        return True  # exists but is not ours, or unknown: assume alive
    return True


def _gc_session(conn: sqlite3.Connection, top_fd: int, session_id: str, min_age_s: float) -> tuple[int, int, int]:
    try:
        sfd = os.open(session_id, _DIR_FLAGS, dir_fd=top_fd)
    except OSError:
        return (0, 0, 0)  # absent, or not a real directory: not ours to clean
    blobs = temps = freed = 0
    try:
        referenced = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT blob_hash FROM checkpoints WHERE session_id = ? AND blob_hash IS NOT NULL",
                (session_id,),
            )
        }
        now = time.time()
        with os.scandir(sfd) as it:
            names = [entry.name for entry in it]
        for name in names:
            try:
                st = os.lstat(name, dir_fd=sfd)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):  # a symlink or anything else: never followed, never removed
                continue
            age = max(0.0, now - st.st_mtime)
            if _BLOB_RE.fullmatch(name):
                if name in referenced or age < min_age_s:
                    continue
                size = st.st_size
            else:
                match = _BLOB_TEMP_RE.fullmatch(name)
                if match is None:
                    continue
                if _pid_alive(int(match.group(1))) and age < max(min_age_s, GC_MIN_AGE_S):
                    continue  # a snapshot in another process may be writing it right now
                size = 0
            try:
                os.unlink(name, dir_fd=sfd)
            except OSError:
                continue
            if _BLOB_RE.fullmatch(name):
                blobs += 1
                freed += size
            else:
                temps += 1
    finally:
        os.close(sfd)
    return (blobs, temps, freed)


def gc_orphans(
    session_id: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    min_age_s: float = GC_MIN_AGE_S,
) -> GcResult:
    """Remove blobs that no checkpoint row refers to, and stale temp files.

    ``session_id=None`` covers every session directory.  Blobs younger than
    ``min_age_s`` are kept (see the module docstring); tests pass ``0``.  The
    database write lock is held while the decision is made, so a row cannot
    appear between "no row refers to it" and the delete.  Nothing is removed
    from a directory that is not a real directory, and only regular files are
    ever removed.
    """
    if session_id is not None:
        _check_session_id(session_id)
    if isinstance(min_age_s, bool) or not isinstance(min_age_s, (int, float)) or min_age_s < 0:
        raise ValueError("min_age_s must be a non-negative number")
    try:
        top_fd = os.open(str(_config.config_dir(environ) / CHECKPOINTS_DIRNAME), _DIR_FLAGS)
    except FileNotFoundError:
        return GcResult(0, 0, 0)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CheckpointError("the checkpoint store is not a real directory; refusing to use it") from None
        raise
    try:
        names = [session_id] if session_id is not None else sorted(
            n for n in os.listdir(top_fd) if SESSION_ID_RE.fullmatch(n)
        )
        blobs = temps = freed = 0
        with _connection(conn, environ) as c, transaction(c):
            for name in names:
                b, t, f = _gc_session(c, top_fd, name, float(min_age_s))
                blobs, temps, freed = blobs + b, temps + t, freed + f
    finally:
        os.close(top_fd)
    log_event(logger, logging.INFO, "checkpoints.gc", count=blobs + temps)
    return GcResult(blobs_removed=blobs, temps_removed=temps, bytes_freed=freed)


__all__ = [
    "CheckpointConflictError",
    "CheckpointError",
    "CorruptCheckpointError",
    "FAIL_IO",
    "GC_MIN_AGE_S",
    "GcResult",
    "MAX_SNAPSHOT_BYTES",
    "PathRefusedError",
    "REWIND_NOTICE",
    "RewindIncompleteError",
    "RewindResult",
    "RootError",
    "SKIP_CHANGED",
    "SKIP_IS_DIRECTORY",
    "SKIP_NOT_REGULAR",
    "SKIP_PARENT_NOT_DIR",
    "SKIP_SYMLINK",
    "SKIP_SYMLINKED_DIR",
    "SKIP_TOO_LARGE",
    "SKIP_UNREADABLE",
    "SnapshotResult",
    "Skipped",
    "gc_orphans",
    "rewind",
    "snapshot",
]
