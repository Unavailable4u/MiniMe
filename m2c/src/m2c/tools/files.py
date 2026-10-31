"""
m2c/src/m2c/tools/files.py -- Patch C05: read_file, list_dir, edit_file,
write_file and delete_path.

Ported from daemon/tools.py (MiniMe, 6977ddf): the list_dir / read_file /
write_file / delete behaviour of the daemon, rebuilt on the C05 path guard
(paths.py) and extended as the work package asks.  The daemon module is not
imported and not touched.  The daemon's ``execute_command`` is not ported here:
the shell tool is C07.

Every function takes the session's ``ToolContext`` (root, denylist, seen-files
state, caps) as its first argument and returns ``(output_text, truncated)``;
failures raise ``ToolError`` with a stable ``code``.  ``run_tool`` (in
``__init__``) validates the arguments and turns both into the protocol's
``tool_result`` shape.  Nothing in this module logs: tool name, status, code and
sizes are logged by ``run_tool`` and nothing else (build plan 11.4).

What each tool guarantees
-------------------------
read_file    Numbered lines, optional ``start_line`` / ``end_line``.  Output is
             capped (``caps.read_output_chars``) with a marker that says how to
             continue.  Binary files (a NUL byte) and non-UTF-8 files are
             refused.  Opens without following a final symlink and without
             blocking on a FIFO.  Records the file as *seen*: path, mtime, size
             and a SHA-256 of the whole file (even for a ranged read, so a later
             change is detected whatever part was read).  A file bigger than
             ``caps.max_file_bytes`` is streamed (so a line range of a huge log
             works) but gets no hash, which means it can never be edited or
             overwritten through these tools.

list_dir     Directories first, then files, case-insensitive.  Capped.  Symlinks
             are marked and never followed; the target of a symlink that leaves
             the project is not shown.  Protected entries are omitted.  ``.git``
             is listed but not entered.  (Ignore files are not honoured yet; see
             the patch notes.)

edit_file    Exact string match.  Fails if the string is missing or, without
             ``replace_all``, not unique (and says how many matches).  Returns
             a unified diff.  Requires a prior read and an unchanged file.

write_file   Atomic: a temp file in the *same directory* (so the rename cannot
             cross a filesystem), written in full, ``fsync``ed, validated by
             reading it back, checked against a concurrent change, then
             ``os.replace``d; the directory is ``fsync``ed afterwards.  The mode
             of an existing file is preserved (the temp file is private until
             then).  An EXISTING file requires a prior read in this session and
             must not have changed since (content hash, so a bare ``touch`` is
             not a change).  A new file needs no read.  Missing parent
             directories are created inside the project.  A final symlink is
             refused.

delete_path  A file, a symlink (the link, never the target) or an empty
             directory.  A non-empty directory only with ``recursive=True``,
             which is an explicit argument of this Python function: protocol v1's
             ``delete_path`` schema has no such argument, so the model cannot
             ask for it (a protocol edit is the owner's call).  Never the root,
             never anything named ``.git``, never a tree containing a protected
             path or a ``.git``.

Interrupted writes
------------------
Between "temp file written" and "renamed" the original is never touched, so a
crash (power loss, SIGKILL, an exception) leaves the original whole.  The only
residue is the temp file, named ``.m2c-write.<pid>.<16 hex>.tmp`` so it can be
recognised.  It is removed on every error path (including KeyboardInterrupt),
and a crash that cannot clean up is swept on the next write into that directory:
a temp file whose pid is dead, or older than ``caps.stale_temp_age_s``, is
deleted (``sweep_stale_temps`` is also public).  Hard links to the target are
broken by the rename, as with any atomic write; extended attributes and ACLs are
not copied.
"""
from __future__ import annotations

import difflib
import errno
import hashlib
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .paths import (
    SymlinkError,
    ToolError,
    _open_child_dir,
    open_dir_chain,
    open_regular,
    resolve_for_delete,
    resolve_for_read,
    resolve_for_write,
)

if TYPE_CHECKING:  # pragma: no cover
    from . import ToolContext

TEMP_PREFIX = ".m2c-write."
TEMP_SUFFIX = ".tmp"
_TEMP_RE = re.compile(r"^\.m2c-write\.(\d+)\.([0-9a-f]{16})\.tmp$")
_SWEEP_SCAN_LIMIT = 10_000
_CHUNK = 64 * 1024
_MAX_WALK_DEPTH = 100


# ---------------------------------------------------------------------------
# Seen-files state
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeenRecord:
    """What read_file saw: enough to tell later whether the file changed."""

    mtime_ns: int
    size: int
    sha256: str | None  # None: too big to hash, so never editable through these tools


class SeenFiles:
    """Per-session record of files the model has read (or written).

    Keyed by real path.  Bounded (oldest evicted); an evicted file simply has to
    be read again, which is the safe direction.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._max = max_entries
        self._items: OrderedDict[str, SeenRecord] = OrderedDict()
        self._lock = threading.RLock()

    def record(self, path: str | os.PathLike[str], mtime_ns: int, size: int, sha256: str | None) -> None:
        key = os.fspath(path)
        with self._lock:
            self._items[key] = SeenRecord(mtime_ns, size, sha256)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def get(self, path: str | os.PathLike[str]) -> SeenRecord | None:
        with self._lock:
            return self._items.get(os.fspath(path))

    def forget(self, path: str | os.PathLike[str]) -> None:
        with self._lock:
            self._items.pop(os.fspath(path), None)

    def forget_under(self, path: str | os.PathLike[str]) -> None:
        prefix = os.fspath(path)
        with self._lock:
            for key in [k for k in self._items if k == prefix or k.startswith(prefix + os.sep)]:
                del self._items[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, path: object) -> bool:
        return isinstance(path, (str, os.PathLike)) and self.get(path) is not None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_text(name: str) -> str:
    """A file name made safe to show: control characters and undecodable bytes
    (a name containing a newline could otherwise forge extra output lines)."""
    return "".join(c if c.isprintable() else "?" for c in name)


def _os_error(exc: OSError) -> ToolError:
    """Map an OSError to a ToolError whose text names no path."""
    if isinstance(exc, FileNotFoundError):
        return ToolError("no such file or directory", code="not_found")
    if isinstance(exc, NotADirectoryError):
        return ToolError("a path component is not a directory", code="not_a_directory")
    if isinstance(exc, IsADirectoryError):
        return ToolError("path is a directory", code="is_a_directory")
    if isinstance(exc, PermissionError):
        return ToolError("permission denied", code="io_error")
    detail = os.strerror(exc.errno) if exc.errno else "unknown error"
    return ToolError(f"I/O error: {detail}", code="io_error")


def _guarded(fn):
    """Turn a stray OSError into a ToolError (ToolErrors pass through)."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except OSError as exc:
            raise _os_error(exc) from None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _read_all(fd: int, limit: int) -> bytes:
    """Read until EOF or until more than ``limit`` bytes have arrived."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    return b"".join(chunks)


def _decode(data: bytes) -> str:
    if b"\0" in data:
        raise ToolError("binary file (it contains NUL bytes); only text files can be read or edited", code="binary_file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("file is not valid UTF-8 text", code="not_utf8") from None


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _format_line(n: int, text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) > max_chars:
        return f"{n:>6}\t{text[:max_chars]}... [line truncated: {len(text)} chars]", True
    return f"{n:>6}\t{text}", False


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
@_guarded
def read_file(ctx: ToolContext, path: str, start_line: int | None = None, end_line: int | None = None) -> tuple[str, bool]:
    if start_line is not None and end_line is not None and end_line < start_line:
        raise ToolError("end_line must not be before start_line", code="invalid_args")
    res = resolve_for_read(ctx.root, path, ctx.denylist)
    if res.is_root:
        raise ToolError("path is a directory; use list_dir to see its entries", code="is_a_directory")
    caps = ctx.caps
    pfd = open_dir_chain(ctx.root, res.rel[:-1])
    try:
        fd, st = open_regular(pfd, res.rel[-1])
    finally:
        os.close(pfd)
    try:
        start = start_line or 1
        if st.st_size <= caps.max_file_bytes:
            data = _read_all(fd, caps.max_file_bytes)
            if len(data) > caps.max_file_bytes:
                raise ToolError("file grew while it was being read; try again", code="file_too_large")
            text = _decode(data)
            after = os.fstat(fd)
            ctx.seen.record(res.path, after.st_mtime_ns, len(data), _sha(data))
            return _render_lines(_split_lines(text), start, end_line, caps)
        return _stream_lines(ctx, fd, st, res.path, start, end_line)
    finally:
        os.close(fd)


def _render_lines(lines: list[str], start: int, end_line: int | None, caps) -> tuple[str, bool]:
    total = len(lines)
    if total == 0:
        return "[file is empty]", False
    if start > total:
        return f"[file has {total} lines; start_line {start} is past the end]", False
    last_wanted = min(end_line, total) if end_line is not None else total
    out: list[str] = []
    used = 0
    truncated = False
    shown_to = start - 1
    for n in range(start, last_wanted + 1):
        formatted, cut = _format_line(n, lines[n - 1], caps.max_line_chars)
        truncated = truncated or cut
        budget = caps.read_output_chars - used
        if len(formatted) + 1 > budget:
            if not out:  # always show something: the first line, cut to the budget
                formatted = formatted[: max(budget, 1)]
                out.append(formatted)
                shown_to = n
                truncated = True
            else:
                truncated = True
            break
        out.append(formatted)
        used += len(formatted) + 1
        shown_to = n
    text = "\n".join(out)
    if shown_to < last_wanted:
        text += (
            f"\n[truncated: showing lines {start}-{shown_to} of {total}; "
            f"call read_file with start_line={shown_to + 1} to continue]"
        )
        truncated = True
    return text, truncated


def _stream_lines(ctx, fd: int, st: os.stat_result, real_path, start: int, end_line: int | None) -> tuple[str, bool]:
    """Line range of a file too big to load: scan forward, keep only what is shown."""
    caps = ctx.caps
    keep = caps.max_line_chars * 4  # bytes kept of any one line (a UTF-8 character is at most 4)
    out: list[str] = []
    used = 0
    truncated = False
    line_no = 0
    shown_to = start - 1
    cur = bytearray()
    cur_len = 0
    stopped_early = False
    reached_eof = False

    def finish_line() -> bool:
        """Handle one complete line; return False when output is full or done."""
        nonlocal line_no, used, truncated, shown_to, cur, cur_len
        line_no += 1
        raw, length, over = bytes(cur), cur_len, cur_len > keep
        cur, cur_len = bytearray(), 0
        if line_no < start:
            return True
        if end_line is not None and line_no > end_line:
            return False
        try:
            text = raw.decode("utf-8", errors="ignore" if over else "strict")
        except UnicodeDecodeError:
            raise ToolError("file is not valid UTF-8 text", code="not_utf8") from None
        if over:
            formatted = f"{line_no:>6}\t{text[: caps.max_line_chars]}... [line truncated: {length} bytes]"
            truncated = True
        else:
            formatted, cut = _format_line(line_no, text, caps.max_line_chars)
            truncated = truncated or cut
        budget = caps.read_output_chars - used
        if len(formatted) + 1 > budget:
            if not out:
                out.append(formatted[: max(budget, 1)])
                shown_to = line_no
            return False
        out.append(formatted)
        used += len(formatted) + 1
        shown_to = line_no
        return True

    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            reached_eof = True
            break
        if b"\0" in chunk:
            raise ToolError("binary file (it contains NUL bytes); only text files can be read or edited", code="binary_file")
        pieces = chunk.split(b"\n")
        for i, piece in enumerate(pieces):
            if len(cur) < keep:
                cur += piece[: keep - len(cur)]
            cur_len += len(piece)
            if i < len(pieces) - 1:
                if not finish_line():
                    stopped_early = True
                    break
        if stopped_early:
            break
    if reached_eof and cur_len:
        finish_line()
        if not (end_line is not None and shown_to >= end_line):
            reached_eof = True
    ctx.seen.record(real_path, st.st_mtime_ns, st.st_size, None)
    if not out:
        return f"[no lines in the requested range (file is larger than {caps.max_file_bytes} bytes)]", False
    text = "\n".join(out)
    done = end_line is not None and shown_to >= end_line
    if stopped_early and not done:
        text += f"\n[truncated: showing lines {start}-{shown_to}; call read_file with start_line={shown_to + 1} to continue]"
        truncated = True
    return text, truncated


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------
class _ListFull(Exception):
    pass


@_guarded
def list_dir(ctx: ToolContext, path: str = ".", max_depth: int = 1) -> tuple[str, bool]:
    res = resolve_for_read(ctx.root, path, ctx.denylist)
    fd = open_dir_chain(ctx.root, res.rel)
    lines: list[str] = []
    truncated = False
    try:
        try:
            _list_level(ctx, fd, res.rel, 1, max_depth, lines)
        except _ListFull:
            truncated = True
    finally:
        os.close(fd)
    if not lines:
        return "[directory is empty]", False
    if truncated:
        lines.append(f"[listing truncated at {ctx.caps.list_max_entries} entries; list a subdirectory]")
    return "\n".join(lines), truncated


def _symlink_label(ctx, parts: tuple[str, ...]) -> str | None:
    """Describe a symlink entry without showing an outside target; None = hide it."""
    rs = str(ctx.root)
    real = os.path.realpath(os.path.join(rs, *parts))
    if real != rs and not real.startswith(rs + os.sep):
        return "symlink, target outside the project"
    rel = tuple(real[len(rs) + 1:].split(os.sep)) if real != rs else ()
    if ctx.denylist.blocks(rel):
        return None
    if not os.path.exists(real):
        return "broken symlink"
    return f"symlink -> {_safe_text('/'.join(rel) or '.')}"


def _list_level(ctx, fd: int, rel: tuple[str, ...], depth: int, max_depth: int, lines: list[str]) -> None:
    caps = ctx.caps
    with os.scandir(fd) as it:
        entries = list(it)
    entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold(), e.name))
    indent = "  " * (depth - 1)
    for entry in entries:
        parts = rel + (entry.name,)
        if ctx.denylist.blocks(parts):
            continue
        name = _safe_text(entry.name)
        if entry.is_symlink():
            label = _symlink_label(ctx, parts)
            if label is None:
                continue
            if len(lines) >= caps.list_max_entries:
                raise _ListFull
            lines.append(f"{indent}{name} ({label})")
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        if len(lines) >= caps.list_max_entries:
            raise _ListFull
        lines.append(f"{indent}{name}/" if is_dir else f"{indent}{name}")
        if is_dir and depth < max_depth and entry.name != ".git" and depth < _MAX_WALK_DEPTH:
            try:
                child = _open_child_dir(fd, entry.name, False)
            except OSError:
                lines.append(f"{indent}  [unreadable]")
                continue
            try:
                _list_level(ctx, child, parts, depth + 1, max_depth, lines)
            finally:
                os.close(child)


# ---------------------------------------------------------------------------
# Loading an existing file for edit / overwrite, and the seen-state check
# ---------------------------------------------------------------------------
def _load_for_change(ctx, pfd: int, name: str, real_path) -> tuple[bytes, os.stat_result, str]:
    """Open the existing file (no-follow), read it whole, and verify the model
    has read it and it has not changed since.  Returns (data, stat, sha)."""
    fd, st = open_regular(pfd, name)
    try:
        if st.st_size > ctx.caps.max_file_bytes:
            raise ToolError(
                f"file is larger than {ctx.caps.max_file_bytes} bytes; it cannot be edited or overwritten with these tools",
                code="file_too_large",
            )
        data = _read_all(fd, ctx.caps.max_file_bytes)
        if len(data) > ctx.caps.max_file_bytes:
            raise ToolError("file is too large to edit", code="file_too_large")
        st = os.fstat(fd)
    finally:
        os.close(fd)
    digest = _sha(data)
    rec = ctx.seen.get(real_path)
    if rec is None:
        raise ToolError(
            "this file has not been read in this session; read it with read_file before changing it",
            code="not_read",
        )
    if rec.sha256 != digest:
        raise ToolError(
            "the file changed since it was read; read it again with read_file before changing it",
            code="changed_since_read",
        )
    return data, st, digest


# ---------------------------------------------------------------------------
# Atomic replace
# ---------------------------------------------------------------------------
def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise ToolError("I/O error: short write", code="io_error")
        offset += written


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except OSError:
        return True  # exists but is not ours (EPERM), or unknown: assume alive
    return True


def _sweep(ctx, dir_fd: int) -> int:
    """Delete stale M2C temp files in one directory.  Best effort, never raises."""
    removed = 0
    now = time.time()
    try:
        with os.scandir(dir_fd) as it:
            for n, entry in enumerate(it):
                if n >= _SWEEP_SCAN_LIMIT:
                    break
                m = _TEMP_RE.match(entry.name)
                if not m:
                    continue
                try:
                    st = os.lstat(entry.name, dir_fd=dir_fd)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    pid = int(m.group(1))
                    old = now - st.st_mtime > ctx.caps.stale_temp_age_s
                    if old or (pid != os.getpid() and not _pid_alive(pid)):
                        os.unlink(entry.name, dir_fd=dir_fd)
                        removed += 1
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return removed


@_guarded
def sweep_stale_temps(ctx: ToolContext, parts: tuple[str, ...] = ()) -> int:
    """Remove stale ``.m2c-write.*.tmp`` files from one project directory.

    A temp file is stale when the process that made it is gone or it is older
    than ``caps.stale_temp_age_s``.  Returns how many were removed.
    """
    fd = open_dir_chain(ctx.root, parts)
    try:
        return _sweep(ctx, fd)
    finally:
        os.close(fd)


def _verify_unchanged(ctx, pfd: int, name: str, before_st: os.stat_result, before_sha: str) -> None:
    """Last look before the rename: the target is still the file we checked."""
    try:
        fd, st = open_regular(pfd, name)  # a swapped-in symlink raises SymlinkError here
    except FileNotFoundError:
        raise ToolError("the file was removed while it was being written; read it again", code="changed_since_read") from None
    try:
        same = (st.st_ino, st.st_dev, st.st_size) == (before_st.st_ino, before_st.st_dev, before_st.st_size)
        if same:
            same = _sha(_read_all(fd, ctx.caps.max_file_bytes)) == before_sha
    finally:
        os.close(fd)
    if not same:
        raise ToolError("the file changed while it was being written; read it again", code="changed_since_read")


def _atomic_write(ctx, pfd: int, name: str, data: bytes, existing: tuple[os.stat_result, str] | None) -> os.stat_result:
    """Temp file in the same directory -> fsync -> validate -> recheck -> rename."""
    _sweep(ctx, pfd)
    temp = f"{TEMP_PREFIX}{os.getpid()}.{secrets.token_hex(8)}{TEMP_SUFFIX}"
    # A new file takes the mode the user's umask gives it; an existing file's
    # mode is applied (fchmod) before any data is written, and the temp file is
    # created 0600 so it is never more readable than the file it replaces.
    create_mode = 0o600 if existing else 0o666
    tfd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, create_mode, dir_fd=pfd)
    leftover = True
    try:
        try:
            if existing:
                old_st = existing[0]
                os.fchmod(tfd, stat.S_IMODE(old_st.st_mode))
                try:
                    os.fchown(tfd, old_st.st_uid, old_st.st_gid)
                except OSError:
                    pass  # not permitted to give the file back to its owner: keep ours
            _write_all(tfd, data)
            os.fsync(tfd)
            if os.fstat(tfd).st_size != len(data):
                raise ToolError("I/O error: the temp file has the wrong size", code="io_error")
            os.lseek(tfd, 0, os.SEEK_SET)
            if _sha(_read_all(tfd, len(data))) != _sha(data):
                raise ToolError("I/O error: the temp file does not match what was written", code="io_error")
            final_st = os.fstat(tfd)
        finally:
            os.close(tfd)
        if existing:
            _verify_unchanged(ctx, pfd, name, existing[0], existing[1])
        else:
            try:
                os.lstat(name, dir_fd=pfd)
            except FileNotFoundError:
                pass
            else:
                raise ToolError("the file was created by someone else while it was being written; read it first", code="changed_since_read")
        os.replace(temp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        leftover = False
        try:
            os.fsync(pfd)  # make the rename durable; the data is already in place either way
        except OSError:
            pass
        return final_st
    finally:
        if leftover:
            try:
                os.unlink(temp, dir_fd=pfd)
            except OSError:
                pass


def _record_after_write(ctx, real_path, data: bytes) -> None:
    try:
        st = os.stat(real_path, follow_symlinks=False)
        ctx.seen.record(real_path, st.st_mtime_ns, len(data), _sha(data))
    except OSError:
        ctx.seen.forget(real_path)


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------
@_guarded
def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> tuple[str, bool]:
    if old_string == "":
        raise ToolError("old_string must not be empty (use write_file to create a file)", code="invalid_args")
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical; nothing to change", code="no_change")
    res = resolve_for_write(ctx.root, path, ctx.denylist)
    pfd = open_dir_chain(ctx.root, res.rel[:-1])
    try:
        name = res.rel[-1]
        data, st, digest = _load_for_change(ctx, pfd, name, res.path)
        text = _decode(data)
        count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string was not found in the file", code="string_not_found")
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string matches {count} places; add surrounding context to make it unique, or set replace_all",
                code="string_not_unique",
            )
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        try:
            new_data = new_text.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolError("the new text is not valid UTF-8", code="invalid_args") from None
        if len(new_data) > ctx.caps.max_file_bytes:
            raise ToolError("the edited file would be too large", code="content_too_large")
        _atomic_write(ctx, pfd, name, new_data, (st, digest))
        _record_after_write(ctx, res.path, new_data)
    finally:
        os.close(pfd)
    rel = res.rel_posix
    replaced = count if replace_all else 1
    head = f"Edited {rel}: replaced {replaced} occurrence{'s' if replaced != 1 else ''}."
    diff = "\n".join(
        difflib.unified_diff(text.split("\n"), new_text.split("\n"), f"a/{rel}", f"b/{rel}", n=3, lineterm="")
    )
    cap = ctx.caps.diff_max_chars
    if len(diff) > cap:
        diff = diff[:cap] + "\n[diff truncated; the edit itself was applied in full]"
        return f"{head}\n{diff}", True
    return f"{head}\n{diff}", False


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
@_guarded
def write_file(ctx: ToolContext, path: str, content: str) -> tuple[str, bool]:
    if not isinstance(content, str):
        raise ToolError("content must be a string", code="invalid_args")
    if "\0" in content:
        raise ToolError("content contains a NUL byte; only text can be written", code="invalid_args")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("content is not valid UTF-8 text", code="invalid_args") from None
    if len(data) > ctx.caps.max_write_bytes:
        raise ToolError(
            f"content is {len(data)} bytes; the limit is {ctx.caps.max_write_bytes}", code="content_too_large"
        )
    res = resolve_for_write(ctx.root, path, ctx.denylist)
    pfd = open_dir_chain(ctx.root, res.rel[:-1], create=True)
    try:
        name = res.rel[-1]
        try:
            lst = os.lstat(name, dir_fd=pfd)
        except FileNotFoundError:
            lst = None
        existing = None
        if lst is not None:
            if stat.S_ISLNK(lst.st_mode):
                raise SymlinkError("refusing to write through a symlink")
            if stat.S_ISDIR(lst.st_mode):
                raise ToolError("path is a directory", code="is_a_directory")
            if not stat.S_ISREG(lst.st_mode):
                raise ToolError("path is not a regular file", code="not_a_file")
            _data, st, digest = _load_for_change(ctx, pfd, name, res.path)
            existing = (st, digest)
        _atomic_write(ctx, pfd, name, data, existing)
        _record_after_write(ctx, res.path, data)
    finally:
        os.close(pfd)
    verb = "Overwrote" if existing else "Created"
    return f"{verb} {res.rel_posix} ({len(data)} bytes)", False


# ---------------------------------------------------------------------------
# delete_path
# ---------------------------------------------------------------------------
def _scan_tree(ctx, parent_fd: int, name: str, rel: tuple[str, ...], counter: list[int], depth: int = 0) -> None:
    """Pre-flight for a recursive delete: nothing protected, no .git, not too big."""
    if depth > _MAX_WALK_DEPTH:
        raise ToolError("directory tree is too deep to delete", code="refused")
    fd = _open_child_dir(parent_fd, name, False)
    try:
        with os.scandir(fd) as it:
            entries = list(it)
        for entry in entries:
            parts = rel + (entry.name,)
            if ctx.denylist.blocks(parts):
                raise ToolError("protected path", code="protected_path")
            if entry.name.casefold() == ".git":
                raise ToolError("refusing to delete a tree that contains .git", code="refused")
            counter[0] += 1
            if counter[0] > ctx.caps.max_delete_entries:
                raise ToolError(
                    f"directory tree has more than {ctx.caps.max_delete_entries} entries; delete it in parts", code="refused"
                )
            if entry.is_dir(follow_symlinks=False):
                _scan_tree(ctx, fd, entry.name, parts, counter, depth + 1)
    finally:
        os.close(fd)


def _remove_tree(parent_fd: int, name: str) -> None:
    """rmtree by descriptor: symlinks are unlinked, never entered."""
    fd = _open_child_dir(parent_fd, name, False)
    try:
        with os.scandir(fd) as it:
            entries = list(it)
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _remove_tree(fd, entry.name)
            else:
                os.unlink(entry.name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


@_guarded
def delete_path(ctx: ToolContext, path: str, recursive: bool = False) -> tuple[str, bool]:
    res = resolve_for_delete(ctx.root, path, ctx.denylist)
    if res.is_root:
        raise ToolError("refusing to delete the project root", code="refused")
    if any(p.casefold() == ".git" for p in res.rel):
        raise ToolError("refusing to delete .git", code="refused")
    pfd = open_dir_chain(ctx.root, res.rel[:-1])
    try:
        name = res.rel[-1]
        st = os.lstat(name, dir_fd=pfd)
        if stat.S_ISLNK(st.st_mode):
            os.unlink(name, dir_fd=pfd)
            kind = "symlink"
        elif stat.S_ISDIR(st.st_mode):
            if recursive:
                counter = [0]
                _scan_tree(ctx, pfd, name, res.rel, counter)
                _remove_tree(pfd, name)
                kind = f"directory ({counter[0]} entries)"
            else:
                try:
                    os.rmdir(name, dir_fd=pfd)
                except OSError as exc:
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        raise ToolError(
                            "directory is not empty; delete its contents first (recursive deletion is not available to the model)",
                            code="directory_not_empty",
                        ) from None
                    raise
                kind = "empty directory"
        else:
            os.unlink(name, dir_fd=pfd)
            kind = "file"
    finally:
        os.close(pfd)
    ctx.seen.forget_under(res.path)
    return f"Deleted {kind} {res.rel_posix}", False
