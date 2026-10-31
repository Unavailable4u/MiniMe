"""
m2c/src/m2c/tools/paths.py -- Patch C05: the path guard and the two-form
denylist for the local tool executors.

Ported from daemon/path_guard.py (MiniMe, 6977ddf) and extended.  The daemon
module is untouched and nothing here imports it; ``assert_safe_root``,
``is_within_root`` and ``assert_within_root`` keep the daemon's names, signatures
and messages (the 14 daemon tests run unchanged against them, see
tests/tools/test_tools_path_guard_port.py).  What C05 adds is everything the
daemon's guard did not need while it only listed and read files:

* a **resolver per intent** (``resolve_for_read`` / ``_write`` / ``_delete``)
  that returns a project-relative path made of *real* components, so the rest
  of the executor never handles a string it has to re-check;
* the **denylist**, in two forms (below);
* **no-follow opening** (``open_dir_chain``, ``open_regular``) that closes the
  gap between "the path was checked" and "the path was opened".

Containment (what ``resolve_*`` enforces)
-----------------------------------------
* Everything is pinned under the session root.  Relative paths are joined to
  it; absolute paths are accepted only if they land inside it, exactly like a
  relative one would.  ``~`` is *not* expanded in a tool argument (a file may be
  called ``~``); only the configured root goes through ``expanduser``.
* Symlinks are resolved (``os.path.realpath``) and the **real** target must be
  inside the root -- for reads, writes and deletes alike.  A symlink inside the
  project that points outside is refused, not followed.
* Containment uses ``os.path.commonpath``, never a string prefix, so
  ``/work/proj-x`` is not "inside" ``/work/proj``.
* NUL bytes, empty strings, non-strings and absurdly long paths are rejected
  before the filesystem is touched.
* A **write never follows a symlink for the final component**: if the target
  name is a symlink (to anywhere, even to another file in the project, even a
  dangling one) the write is refused rather than redirected.  A delete of a
  symlink removes the link itself and never its target, and refuses a link whose
  real target is outside the root.
* The over-broad roots the daemon refused (``/``, the home directory) plus the
  usual system directories (``/home``, ``/etc``, ``/usr``, ``/var``, ``/tmp``
  itself ...) are refused as a *root*.  Unlike the daemon, the set is computed at
  call time, not frozen at import, so a changed ``HOME`` is honoured.

Check, then open: this is defense in depth, not the boundary
------------------------------------------------------------
A path can be checked and then *replaced* before it is opened (a directory
swapped for a symlink by another process).  ``open_dir_chain`` therefore walks
from an open directory descriptor for the root, one component at a time, with
``O_NOFOLLOW | O_DIRECTORY`` on every step, and ``open_regular`` opens the final
component with ``O_NOFOLLOW`` (and ``O_NONBLOCK``, so a FIFO cannot hang the
tool).  A component that became a symlink after the check fails with
``SymlinkError`` instead of being followed.  The resolver's output is made of
real components precisely so this walk never has to follow anything legitimate.

That is still not a security boundary: a process that can write inside the
project can race in ways a library cannot fully close (hard links, bind mounts,
a directory renamed between two steps).  **The sandbox (work package C11) is the
real boundary**; this module makes the honest, non-racing case safe and turns
the common attack into a refusal.

The denylist, in two forms (product guide 8.5)
----------------------------------------------
1. ``HARD_NAME_PATTERNS`` / ``HARD_SEQUENCE_PATTERNS``: a list compiled into the
   code.  The model, project files and settings cannot change it (there is no
   API to extend it; ``Denylist`` only ever *adds* user patterns).  Private
   keys and key files, ``.env`` files, credential stores, shell history, and
   M2C's own state directory.  Names are matched case-insensitively against
   **every component** of the path, so ``.ssh/`` protects everything below it
   and ``dotfiles/.aws/credentials`` is as protected as ``~/.aws/credentials``.
   The list errs towards refusing: ``*.pem`` also hides public certificates,
   ``.env.example`` is not exempt (an owner decision, see the patch notes), and
   a virtualenv directory named ``.env`` is hidden whole.
2. A **user list** passed in by the caller (the settings loader is C06's job;
   this module reads no settings).  Patterns are written like ignore-file lines:
   a name or glob with no ``/`` matches any path component; with ``/`` it
   matches that run of components anywhere; a leading ``/`` anchors it to the
   project root; ``**`` matches any number of components; a trailing ``/`` is
   allowed.  A matched directory protects everything below it.  A pattern that
   cannot be understood raises ``ValueError`` -- the session must not start
   with a protection the user thinks they have but does not.

The check runs on the *lexical* components first (before any ``stat``, so a
protected name gives the same answer whether or not the file exists: no
existence oracle) and again on the *resolved* components (so a symlink named
``notes.txt`` that points at ``.env`` is refused).  The error is always the
fixed text ``protected path``; it never echoes the path or any content.

Known limits, stated plainly: a hard link to a protected file under another name
is not detected; case folding is Unicode ``casefold``, which is stricter than
Linux needs but right for ``/mnt`` filesystems; the list is not exhaustive (the
user list exists for the rest).
"""
from __future__ import annotations

import errno
import fnmatch
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_PATH_CHARS = 4096
_MAX_USER_PATTERN_CHARS = 256
_MAX_USER_PATTERN_SEGMENTS = 16

# Local error vocabulary.  protocol/v1 defines no per-tool error codes (its ten
# codes are server-level), so these are carried in ToolResult.code and in the
# text of a failed result, never as an extra field on the wire (tool_result is
# a closed object).  Stable strings; tests pin them.
ERROR_CODES = (
    "invalid_args", "unknown_tool", "invalid_path", "outside_root", "protected_path",
    "symlink_refused", "not_found", "is_a_directory", "not_a_directory", "not_a_file",
    "binary_file", "not_utf8", "file_too_large", "content_too_large", "not_read",
    "changed_since_read", "string_not_found", "string_not_unique", "no_change",
    "directory_not_empty", "refused", "io_error", "timeout", "rg_not_found",
    "search_failed", "internal_error", "unsafe_root",
)


class ToolError(Exception):
    """A failure a tool reports to the model: a stable ``code`` plus fixed text.

    Messages are written to be shown to the model, so they never contain file
    contents, never repeat a model-supplied value, and (for ``protected_path``)
    never name the path.  ``run_tool`` turns this into a failed ``ToolResult``;
    anything that is *not* a ToolError becomes ``internal_error`` with no text.
    """

    code = "internal_error"

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class PathGuardError(ToolError):
    """A path failed containment or root-sanity checks (daemon-compatible name)."""

    code = "outside_root"


class InvalidPathError(PathGuardError):
    code = "invalid_path"


class SymlinkError(PathGuardError):
    code = "symlink_refused"


class ProtectedPathError(PathGuardError):
    """The path is on the hard or the user denylist.  The text is fixed."""

    code = "protected_path"

    def __init__(self, message: str = "protected path") -> None:
        super().__init__("protected path")


# ---------------------------------------------------------------------------
# The hard-coded denylist
# ---------------------------------------------------------------------------
# Matched (fnmatch, case-insensitively) against EVERY component of a path.  A
# match on a directory protects everything below it.
HARD_NAME_PATTERNS: tuple[str, ...] = (
    # private keys and key material
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk", "*.kdbx",
    # dotenv files (".env.example" is deliberately included; see the module docstring)
    ".env", ".env.*", "*.env", ".envrc",
    # credential files
    ".netrc", "_netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc", ".s3cfg", ".vault-token",
    # credential directories (the whole directory, not just the one well-known file)
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
    # shell and tool history
    ".*_history", ".zhistory", "fish_history",
)

# Matched against a run of consecutive components, anywhere in the path.
HARD_SEQUENCE_PATTERNS: tuple[str, ...] = (
    ".config/gcloud",
    ".config/gh",
    ".config/op",
    ".terraform.d/credentials.tfrc.json",
)

_HARD_NAMES = tuple(p.casefold() for p in HARD_NAME_PATTERNS)
_HARD_SEQS = tuple(tuple(s.casefold().split("/")) for s in HARD_SEQUENCE_PATTERNS)

# Roots refused as a project root (resolved at call time).
_BROAD_ROOTS = (
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/media", "/mnt",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/Users",
)


def _hard_match(folded: Sequence[str]) -> bool:
    for part in folded:
        for pat in _HARD_NAMES:
            if fnmatch.fnmatchcase(part, pat):
                return True
    for seq in _HARD_SEQS:
        n = len(seq)
        for i in range(len(folded) - n + 1):
            if all(fnmatch.fnmatchcase(folded[i + k], seq[k]) for k in range(n)):
                return True
    return False


@dataclass(frozen=True)
class _UserPattern:
    segments: tuple[str, ...]
    anchored: bool


def _compile_user_pattern(pattern: object) -> _UserPattern:
    if not isinstance(pattern, str):
        raise ValueError("denylist entries must be strings")
    raw = pattern.strip()
    if not raw:
        raise ValueError("a denylist entry must not be empty")
    if "\0" in raw:
        raise ValueError("a denylist entry must not contain a NUL byte")
    if len(raw) > _MAX_USER_PATTERN_CHARS:
        raise ValueError("a denylist entry is too long")
    if raw.startswith("!"):
        raise ValueError("negated denylist entries are not supported (nothing can be un-protected)")
    anchored = raw.startswith("/")
    segments = [s for s in raw.split("/") if s != ""]
    if not segments:
        raise ValueError("a denylist entry that matches the whole project is not allowed")
    if any(s in (".", "..") for s in segments):
        raise ValueError("'.' and '..' are not allowed in a denylist entry")
    if len(segments) > _MAX_USER_PATTERN_SEGMENTS:
        raise ValueError("a denylist entry has too many path segments")
    if all(s == "**" for s in segments):
        raise ValueError("a denylist entry that matches the whole project is not allowed")
    return _UserPattern(tuple(s.casefold() for s in segments), anchored)


def _match_prefix(segs: Sequence[str], parts: Sequence[str]) -> bool:
    """True if ``segs`` match a *prefix* of ``parts`` (so everything below a
    matched directory is matched too); ``**`` matches zero or more components."""
    if not segs:
        return True
    head = segs[0]
    if head == "**":
        return any(_match_prefix(segs[1:], parts[i:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatch.fnmatchcase(parts[0], head) and _match_prefix(segs[1:], parts[1:])


def _user_match(pat: _UserPattern, folded: Sequence[str]) -> bool:
    if pat.anchored:
        return _match_prefix(pat.segments, folded)
    return any(_match_prefix(pat.segments, folded[i:]) for i in range(len(folded)))


class Denylist:
    """The two-form denylist for one session.

    ``user_patterns`` is the settings-supplied list (validated and copied: a
    caller mutating its list afterwards changes nothing).  ``protected_dirs``
    are absolute directories that are protected as a whole (M2C's own state
    directory); they are compared by real path.  The hard list is always on and
    cannot be switched off from here.
    """

    def __init__(
        self,
        user_patterns: Iterable[str] = (),
        *,
        root: str | os.PathLike[str],
        protected_dirs: Iterable[str | os.PathLike[str]] = (),
    ) -> None:
        if isinstance(user_patterns, (str, bytes)):
            raise ValueError("the user denylist must be a list of patterns, not a single string")
        listed = tuple(user_patterns)
        self._user = tuple(_compile_user_pattern(p) for p in listed)
        self.user_patterns: tuple[str, ...] = tuple(listed)
        self.root = Path(root)
        self.protected_dirs: tuple[Path, ...] = tuple(Path(os.path.realpath(p)) for p in protected_dirs)

    def is_protected(self, parts: Sequence[str]) -> bool:
        """Name rules (hard, then user) against root-relative components."""
        folded = tuple(p.casefold() for p in parts)
        if _hard_match(folded):
            return True
        return any(_user_match(u, folded) for u in self._user)

    def is_protected_abs(self, path: str | os.PathLike[str]) -> bool:
        """True if ``path`` is, or is inside, one of the protected directories."""
        p = os.fspath(path)
        for d in self.protected_dirs:
            ds = str(d)
            if p == ds or p.startswith(ds + os.sep):
                return True
        return False

    def blocks(self, parts: Sequence[str]) -> bool:
        """Both forms, for results built from root-relative components (listings)."""
        return self.is_protected(parts) or self.is_protected_abs(os.path.join(str(self.root), *parts))

    def check(self, parts: Sequence[str]) -> None:
        if self.blocks(parts):
            raise ProtectedPathError()


# ---------------------------------------------------------------------------
# Roots (ported from the daemon's assert_safe_root / is_within_root / assert_within_root)
# ---------------------------------------------------------------------------
def _is_inside(real: str, root: str) -> bool:
    """commonpath containment on two already-resolved absolute path strings."""
    if real == root:
        return True
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:  # mixed absolute/relative, or different drives: never safe
        return False


def _broad_roots() -> set[Path]:
    broad: set[Path] = set()
    for p in _BROAD_ROOTS:
        try:
            if Path(p).exists():
                broad.add(Path(p).resolve())
        except OSError:
            continue
    try:
        broad.add(Path.home().resolve())
    except (RuntimeError, OSError):  # no HOME and no passwd entry: nothing to compare with
        pass
    return broad


def assert_safe_root(
    root: str | os.PathLike[str],
    *,
    protected_dirs: Iterable[str | os.PathLike[str]] = (),
) -> Path:
    """Validate the configured session root (called once, when the session starts).

    Returns the resolved absolute Path; raises PathGuardError otherwise.  Stricter
    than ``is_within_root``: the root must exist, be a directory, not be one of
    the over-broad roots, not sit inside a credential directory, and not be (or
    be inside) M2C's own state directory (``protected_dirs``).
    """
    try:
        resolved = Path(root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise PathGuardError("configured root cannot be resolved", code="unsafe_root") from None

    if not resolved.exists():
        raise PathGuardError(f"configured root does not exist: {resolved}", code="unsafe_root")
    if not resolved.is_dir():
        raise PathGuardError(f"configured root is not a directory: {resolved}", code="unsafe_root")
    if resolved in _broad_roots():
        raise PathGuardError(
            f"configured root is too broad to be safe: {resolved} "
            "(refusing '/', the home directory, or other system roots — "
            "point M2C at a specific project folder instead)",
            code="unsafe_root",
        )
    if _hard_match(tuple(p.casefold() for p in resolved.parts)):
        raise PathGuardError(
            f"configured root is inside a protected location: {resolved}", code="protected_path"
        )
    rs = str(resolved)
    for d in protected_dirs:
        ds = os.path.realpath(d)
        if _is_inside(rs, ds):
            raise PathGuardError(
                f"configured root is M2C's own state directory (or inside it): {resolved}",
                code="unsafe_root",
            )
    return resolved


def is_within_root(candidate: str | os.PathLike[str], root: Path) -> bool:
    """True iff ``candidate`` resolves to a path at or inside ``root``.

    ``root`` must already be the resolved Path returned by assert_safe_root().
    Anything that cannot be resolved (a symlink loop, a NUL byte) is *not* within
    the root: this fails closed.
    """
    try:
        resolved = os.path.realpath(os.path.expanduser(os.fspath(candidate)))
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    return _is_inside(resolved, str(root))


def assert_within_root(candidate: str | os.PathLike[str], root: Path) -> Path:
    """Same check as is_within_root(), but raises PathGuardError with a message."""
    try:
        resolved = Path(os.path.realpath(os.path.expanduser(os.fspath(candidate))))
    except (OSError, RuntimeError, ValueError, TypeError):
        raise PathGuardError("path cannot be resolved") from None
    if not _is_inside(str(resolved), str(root)):
        raise PathGuardError(f"path escapes configured root: {resolved} (root is {root})")
    return resolved


# ---------------------------------------------------------------------------
# Resolving a model-supplied path
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Resolved:
    """A checked path: the root plus the *real* components below it."""

    root: Path
    rel: tuple[str, ...]

    @property
    def path(self) -> Path:
        return self.root.joinpath(*self.rel)

    @property
    def is_root(self) -> bool:
        return not self.rel

    @property
    def rel_posix(self) -> str:
        return "/".join(self.rel) or "."


def _check_raw(raw: object) -> str:
    if not isinstance(raw, str):
        raise InvalidPathError("path must be a string")
    if raw == "":
        raise InvalidPathError("path is empty")
    if "\0" in raw:
        raise InvalidPathError("path contains a NUL byte")
    if len(raw) > MAX_PATH_CHARS:
        raise InvalidPathError("path is too long")
    return raw


def _lexical_parts(root: Path, raw: str) -> tuple[str, ...]:
    """Every component of the path as written, root prefix stripped, for the
    denylist check that runs before the filesystem is touched."""
    text = raw
    if os.path.isabs(raw):
        rs = str(root)
        if raw == rs or raw.startswith(rs + os.sep):
            text = raw[len(rs):]
    return tuple(p for p in text.split(os.sep) if p not in ("", "."))


def _realpath(p: str) -> str:
    try:
        return os.path.realpath(p)
    except (OSError, RuntimeError, ValueError):
        raise InvalidPathError("path cannot be resolved") from None


def _prepare(root: Path, raw: object, denylist: Denylist) -> tuple[str, str]:
    text = _check_raw(raw)
    if denylist.is_protected(_lexical_parts(root, text)):
        raise ProtectedPathError()
    joined = text if os.path.isabs(text) else os.path.join(str(root), text)
    return text, joined


def _finish(root: Path, real: str, denylist: Denylist) -> Resolved:
    rs = str(root)
    if not _is_inside(real, rs):
        raise PathGuardError("path is outside the project root")
    rel: tuple[str, ...] = () if real == rs else tuple(real[len(rs) + 1:].split(os.sep))
    if denylist.is_protected(rel) or denylist.is_protected_abs(real):
        raise ProtectedPathError()
    return Resolved(root, rel)


def resolve_for_read(root: Path, raw: object, denylist: Denylist) -> Resolved:
    """Resolve a path the tool will read, list or search (symlinks followed, and
    the real target must be inside the root).  The path need not exist."""
    _text, joined = _prepare(root, raw, denylist)
    return _finish(root, _realpath(joined), denylist)


def _split_final(joined: str) -> tuple[str, str]:
    if joined.endswith(os.sep):
        raise InvalidPathError("path must name a file, not end in a separator")
    parent, name = os.path.split(joined)
    if name in ("", ".", ".."):
        raise InvalidPathError("path must name a file")
    return parent, name


def resolve_for_write(root: Path, raw: object, denylist: Denylist) -> Resolved:
    """Resolve a path the tool will create or replace.

    The *parent* is resolved (symlinks followed, must stay inside the root); the
    final name is never resolved: if it is a symlink the write is refused.
    """
    _text, joined = _prepare(root, raw, denylist)
    parent, name = _split_final(joined)
    real_parent = _realpath(parent)
    rs = str(root)
    if not _is_inside(real_parent, rs):
        raise PathGuardError("path is outside the project root")
    final = os.path.join(real_parent, name)
    try:
        if stat.S_ISLNK(os.lstat(final).st_mode):
            raise SymlinkError("refusing to write through a symlink")
    except FileNotFoundError:
        pass
    except NotADirectoryError:
        pass  # a parent component is a file: the open reports it precisely
    except OSError:
        raise InvalidPathError("path cannot be inspected") from None
    return _finish(root, final, denylist)


def resolve_for_delete(root: Path, raw: object, denylist: Denylist) -> Resolved:
    """Resolve a path the tool will delete.

    The root itself is refused.  A symlink names *itself* (the link is removed,
    never its target); if its real target is outside the root, or protected, the
    delete is refused.
    """
    _text, joined = _prepare(root, raw, denylist)
    trimmed = joined.rstrip(os.sep) or os.sep
    rs = str(root)
    if _realpath(trimmed) == rs:
        raise ToolError("refusing to delete the project root", code="refused")
    parent, name = _split_final(trimmed)
    real_parent = _realpath(parent)
    if not _is_inside(real_parent, rs):
        raise PathGuardError("path is outside the project root")
    final = os.path.join(real_parent, name)
    try:
        is_link = stat.S_ISLNK(os.lstat(final).st_mode)
    except FileNotFoundError:
        is_link = False
    except OSError:
        raise InvalidPathError("path cannot be inspected") from None
    if is_link:
        _finish(root, _realpath(final), denylist)  # target must be inside and unprotected
    return _finish(root, final, denylist)


# ---------------------------------------------------------------------------
# No-follow opening
# ---------------------------------------------------------------------------
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_child_dir(parent_fd: int, name: str, create: bool) -> int:
    """Open ``name`` (a directory) under ``parent_fd`` without following a symlink."""
    for attempt in (0, 1):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create or attempt:
                raise
            try:
                os.mkdir(name, 0o777, dir_fd=parent_fd)  # the umask applies
            except FileExistsError:
                pass
        except OSError as exc:
            if exc.errno in (errno.ENOTDIR, errno.ELOOP):
                # O_NOFOLLOW|O_DIRECTORY on a symlink reports ENOTDIR on Linux;
                # lstat tells "became a symlink" apart from "is a plain file".
                try:
                    is_link = stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode)
                except OSError:
                    raise exc from None
                if is_link:
                    raise SymlinkError("a path component is a symlink; refusing to follow it") from None
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)) from None
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def open_dir_chain(root: Path, parts: Sequence[str], *, create: bool = False) -> int:
    """Return an open directory fd for ``root/parts...``, walking one component
    at a time with O_NOFOLLOW so nothing is followed.  The caller closes it.

    ``create=True`` makes missing directories (inside the root, by construction).
    Raises FileNotFoundError / NotADirectoryError / SymlinkError.
    """
    fd = os.open(str(root), _DIR_FLAGS)
    try:
        for part in parts:
            if part in ("", ".", "..") or os.sep in part or "\0" in part:
                raise InvalidPathError("invalid path component")
            child = _open_child_dir(fd, part, create)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_regular(dir_fd: int, name: str, flags: int = os.O_RDONLY) -> tuple[int, os.stat_result]:
    """Open ``name`` under ``dir_fd`` without following a final symlink, and
    insist it is a regular file.  Returns ``(fd, fstat)``; the caller closes fd.

    O_NONBLOCK keeps a FIFO from blocking the open; the fstat then refuses it.
    """
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SymlinkError("the file became a symlink; refusing to follow it") from None
        raise
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise ToolError("path is a directory; use list_dir to see its entries", code="is_a_directory")
        if not stat.S_ISREG(st.st_mode):
            raise ToolError("path is not a regular file", code="not_a_file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st
