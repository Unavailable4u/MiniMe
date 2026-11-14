"""
m2c/src/m2c/trust/fingerprint.py -- Patch C26: a fingerprint of everything in a project folder that can change what the
agent is allowed or told to do.

Why
---
A project folder is attacker-controlled content (a cloned repository), and a few of its files are *configuration with power*:
settings that can allow commands without asking, hooks that run commands, skills that add instructions the model follows.
A person's decision "I trust this folder" must therefore be tied to the exact bytes of those files, so that a ``git pull``
which edits a hook is a NEW question, not an old answer.  This module computes the thing the decision is tied to.

What is covered (and nothing else)
----------------------------------
    .m2c/settings.toml       the project settings
    .m2c/rules/**            project rules
    .m2c/hooks/**            hooks (they will live here, or in settings.toml)
    .m2c/skills/**           project skills
    .agents/skills/**        project skills (cross-tool location)
    .claude/skills/**        project skills (Claude-style location)

Files anywhere else (source code, README, ``.m2c/checkpoints``, ``.claude/commands`` ...) change nothing here.  Two limits
worth knowing: the digest does not include file modes (the bytes decide what is configured), and trusting a hook that runs
``./scripts/x.sh`` trusts the configuration, not whatever that script later becomes -- the same as any command a person
puts in a config file.  Instruction files (``M2C.md``, ``AGENTS.md``) are not in the list: C12 reads them as plain text.

The digest
----------
SHA-256 over a version tag, then for every file in sorted order of its relative POSIX path (UTF-8, surrogateescape for
names that are not valid UTF-8): a 4-byte big-endian length of the path, the path, and the 32-byte SHA-256 of the file's
bytes.  The length prefix makes the framing unambiguous.  The folder's own location is NOT part of the digest, so the same
files in another place have the same digest (the store keys decisions by folder, see store.py).  A test carries an
independent reference implementation so a later release cannot silently change what an old stored decision means.

What makes a folder untrustworthy (``problem``)
-----------------------------------------------
* ``symlink``    a symbolic link anywhere in the covered places -- including ``.m2c``, ``.agents`` or ``.claude`` themselves,
                 the settings file, or any file or directory below the covered directories.  A link can point out of the
                 project, so it is never followed, never hashed, and the folder is never trusted automatically.
* ``too_large``  more than MAX_FILES files, MAX_TOTAL_BYTES bytes, MAX_ENTRIES directory entries or MAX_DEPTH levels.  A
                 person cannot review that many files, and hashing must stay cheap and bounded.
* ``unreadable`` anything odd: a FIFO, socket or device, a directory where a file belongs, a file that cannot be opened, a
                 root that is missing.  (A FIFO is opened without blocking and refused by its type, so it cannot hang us.)

With a problem the result has no digest, and the files found so far are listed (relative paths only).

How it walks
------------
With directory file descriptors: every component is opened with ``O_NOFOLLOW`` relative to its parent, and every entry is
classified with ``lstat`` semantics, so a link cannot be followed even if one appears while we walk.  Nothing here raises for
the content of the folder: any unexpected error is reported as ``unreadable``.

``root`` is expected to be the canonical (real) path of the project; the final component of ``root`` itself is not
followed either.  Linux is the only supported platform (build plan O3).
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass

TRUST_FILES: tuple[str, ...] = (".m2c/settings.toml",)
TRUST_TREES: tuple[str, ...] = (".m2c/rules", ".m2c/hooks", ".m2c/skills", ".agents/skills", ".claude/skills")
TRUST_ROOTS: tuple[str, ...] = TRUST_FILES + TRUST_TREES

MAX_FILES = 500
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_DEPTH = 12

PROBLEMS = ("symlink", "too_large", "unreadable")

_VERSION_TAG = b"m2c-trust-fingerprint-v1\n"
_CHUNK = 64 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class Fingerprint:
    """``digest`` is None exactly when ``problem`` is set; ``files`` are sorted relative POSIX paths."""

    digest: str | None
    files: tuple[str, ...]
    problem: str | None
    total_bytes: int = 0


class _Problem(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Walk:
    """One walk over one project folder (state kept here so a problem can still report the files found so far)."""

    def __init__(self, max_files: int, max_total_bytes: int, max_entries: int, max_depth: int) -> None:
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.hashes: dict[str, bytes] = {}
        self.total = 0
        self.entries = 0

    # ------------------------------------------------------------------------------------------------ opening
    @staticmethod
    def _open_dir(parent_fd: int, name: str) -> int | None:
        """The directory ``name`` below ``parent_fd``, never through a link; None if it is absent or not a directory."""
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise _Problem("symlink")
        if not stat.S_ISDIR(info.st_mode):
            return None
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _Problem("symlink") from None
            raise _Problem("unreadable") from None

    def _chain(self, root_fd: int, parts: list[str]) -> int | None:
        """Open ``parts`` one directory at a time from ``root_fd``.  The caller closes the result (a new descriptor)."""
        fd = os.dup(root_fd)
        for name in parts:
            child = self._open_dir(fd, name)
            os.close(fd)
            if child is None:
                return None
            fd = child
        return fd

    # ------------------------------------------------------------------------------------------------ hashing
    def _add_file(self, dir_fd: int, name: str, rel: str) -> None:
        if len(self.hashes) >= self.max_files:
            raise _Problem("too_large")
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _Problem("symlink") from None
            raise _Problem("unreadable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _Problem("unreadable")
            if self.total + info.st_size > self.max_total_bytes:
                raise _Problem("too_large")
            digest = hashlib.sha256()
            read = 0
            while True:
                try:
                    chunk = os.read(fd, _CHUNK)
                except OSError:
                    raise _Problem("unreadable") from None
                if not chunk:
                    break
                read += len(chunk)
                if self.total + read > self.max_total_bytes:  # the file grew while we read it
                    raise _Problem("too_large")
                digest.update(chunk)
        finally:
            os.close(fd)
        self.total += read
        self.hashes[rel] = digest.digest()

    # ------------------------------------------------------------------------------------------------ walking
    def _tick(self) -> None:
        self.entries += 1
        if self.entries > self.max_entries:
            raise _Problem("too_large")

    def _walk_dir(self, dir_fd: int, prefix: str, depth: int) -> None:
        if depth > self.max_depth:
            raise _Problem("too_large")
        try:
            with os.scandir(dir_fd) as scan:
                names = sorted(entry.name for entry in scan)
        except OSError:
            raise _Problem("unreadable") from None
        for name in names:
            self._tick()
            rel = prefix + "/" + name
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                raise _Problem("unreadable") from None
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                raise _Problem("symlink")
            if stat.S_ISDIR(mode):
                child = self._open_dir(dir_fd, name)
                if child is None:
                    raise _Problem("unreadable")
                try:
                    self._walk_dir(child, rel, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                self._add_file(dir_fd, name, rel)
            else:
                raise _Problem("unreadable")

    def visit_file(self, root_fd: int, rel: str) -> None:
        *parents, leaf = rel.split("/")
        parent_fd = self._chain(root_fd, parents)
        if parent_fd is None:
            return
        try:
            try:
                info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError:
                raise _Problem("unreadable") from None
            if stat.S_ISLNK(info.st_mode):
                raise _Problem("symlink")
            if not stat.S_ISREG(info.st_mode):
                raise _Problem("unreadable")
            self._add_file(parent_fd, leaf, rel)
        finally:
            os.close(parent_fd)

    def visit_tree(self, root_fd: int, rel: str) -> None:
        tree_fd = self._chain(root_fd, rel.split("/"))
        if tree_fd is None:
            return
        try:
            self._walk_dir(tree_fd, rel, 1)
        finally:
            os.close(tree_fd)

    # ------------------------------------------------------------------------------------------------ result
    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self.hashes, key=_sort_key))

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(_VERSION_TAG)
        for rel in sorted(self.hashes, key=_sort_key):
            raw = _sort_key(rel)
            h.update(len(raw).to_bytes(4, "big"))
            h.update(raw)
            h.update(self.hashes[rel])
        return h.hexdigest()


def _sort_key(rel: str) -> bytes:
    return rel.encode("utf-8", "surrogateescape")


def compute_fingerprint(
    root,
    *,
    max_files: int | None = None,
    max_total_bytes: int | None = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> Fingerprint:
    """The fingerprint of the covered files under ``root`` (see the module docstring).  Never raises.

    The caps default to the module constants, read at call time (so a test can lower them for one run)."""
    walk = _Walk(
        MAX_FILES if max_files is None else max_files,
        MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes,
        MAX_ENTRIES if max_entries is None else max_entries,
        MAX_DEPTH if max_depth is None else max_depth,
    )
    root_fd = None
    try:
        try:
            root_fd = os.open(os.fspath(root), _DIR_FLAGS)
        except (OSError, TypeError, ValueError):
            raise _Problem("unreadable") from None
        for rel in TRUST_FILES:
            walk.visit_file(root_fd, rel)
        for rel in TRUST_TREES:
            walk.visit_tree(root_fd, rel)
        return Fingerprint(walk.digest(), walk.files(), None, walk.total)
    except _Problem as problem:
        return Fingerprint(None, walk.files(), problem.code, walk.total)
    except RecursionError:
        return Fingerprint(None, walk.files(), "too_large", walk.total)
    except Exception:  # noqa: BLE001 -- "never raise on odd files": anything unexpected means "cannot be trusted"
        return Fingerprint(None, walk.files(), "unreadable", walk.total)
    finally:
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass


def fingerprint_matches(root, digest, **limits) -> bool:
    """True only if the folder has no problem and its digest equals ``digest`` (a 64-character hex string)."""
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    result = compute_fingerprint(root, **limits)
    return result.problem is None and result.digest == digest
