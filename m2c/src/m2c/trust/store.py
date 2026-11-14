"""
m2c/src/m2c/trust/store.py -- Patch C26: the folder-trust store (``<M2C home>/trust.json``) and the trust decision.

What is stored
--------------
One small JSON file mapping the CANONICAL absolute project root (its real path, so a symlinked path and the real path are the
same folder) to what the person decided and for which files:

    {"version": 1,
     "folders": {"/home/me/code/app": {"decision": "trusted" | "untrusted",
                                       "fingerprint": "<64 hex, see fingerprint.py>",
                                       "time": <seconds since the epoch>}}}

A decision is only ever valid for the fingerprint it was made for.  Any change to a trust-relevant file (a ``git pull`` that
edits a hook, say) changes the fingerprint, so the old answer no longer applies and the person is asked again.  An explicit
"untrusted" is remembered the same way, so the person is not asked again for the same files.

How the file is protected (it is a security file: a forged "trusted" entry would switch on a folder's hooks)
--------------------------------------------------------------------------------------------------------
* It lives in the M2C home (mode 0700, tightened by ``paths.ensure_config_dir``), created 0600, written atomically: a temp file
  in the same directory (mode 0600 from the start), flushed and fsynced, then renamed over the old file.  A crash leaves the old
  file or the new one, never half of one.
* Reading REFUSES a file that is not a regular file, is owned by another user, or is accessible to anyone else (any of the
  mode bits 0o177) -- like C03's credential file -- and says how to fix it.  Reading never changes the mode.  A file that
  is not valid JSON of the expected shape is "damaged", and one from a newer m2c is refused rather than misread.  Error texts
  are fixed texts plus the path: they never echo anything read from the file.
* An ENTRY that is not exactly well-formed (a decision other than the two words, a fingerprint that is not 64 lowercase hex
  characters, a time that is not a non-negative integer, a key that is not an absolute normalized path) is ignored and counted
  in ``ignored_entries``; it never counts as trust.
* The model cannot reach it: the file tools refuse anything under the M2C home (C05's protected directories) and the sandbox
  never mounts it (C11); tests/trust/test_trust_protected.py proves both for this file.  Nothing in this package can send it
  anywhere (a test checks the imports).
* The file is bounded: at most MAX_ENTRIES decisions (the oldest are dropped first) and MAX_FILE_BYTES bytes.

Two m2c processes writing at the same moment can lose one remembered decision (last writer wins).  That fails safe: the
person is asked again.

The decision: ``project_trust_state``
-------------------------------------
``project_trust_state(root, store)`` returns a TrustState: ``trusted``, ``needs_prompt``, ``files`` (relative paths), ``reason``
(one fixed word from REASONS), the ``fingerprint`` the answer would be remembered for, and a ``message`` (only for an
unusable store).  Anything uncertain is NOT trusted, and it never raises.

    nothing_to_trust  none of the covered files exist                          not trusted, no prompt
    trusted           a stored "trusted" for exactly these files               TRUSTED
    declined          a stored "untrusted" for exactly these files             not trusted, no prompt
    never_asked       no (valid) stored decision for this folder               not trusted, PROMPT
    changed           a stored decision for different files                    not trusted, PROMPT
    symlink           a link in a covered place                                not trusted, no prompt
    too_large         over the file/byte/entry/depth caps                      not trusted, no prompt
    unreadable        an odd file, an unreadable file, a bad root              not trusted, no prompt
    store_unusable    the trust file cannot be used (loose mode, damaged ...)  not trusted, no prompt

"No prompt" for the last four is deliberate: the folder cannot be trusted automatically, so there is nothing to ask and nothing
to remember; the status line says why.  A stored "trusted" is never believed over a problem found now.

Headless mode never prompts: it will read ``project_trust_state(root, TrustStore.default()).trusted`` (a one-line follow-up in
headless.py), so only a decision the person made earlier in the terminal can turn project settings on there.

The state is evaluated ONCE, when the session starts.  A change to the files during the session is applied at the next start;
a session never upgrades itself from untrusted to trusted without the prompt (tui/session.py).
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .. import paths
from ..logging_setup import log_event
from .fingerprint import compute_fingerprint

logger = logging.getLogger(__name__)

FILE_NAME = "trust.json"
VERSION = 1
MAX_FILE_BYTES = 1_000_000
MAX_ENTRIES = 1000
DECISIONS = ("trusted", "untrusted")
REASONS = (
    "nothing_to_trust", "trusted", "changed", "never_asked", "declined", "symlink", "too_large", "unreadable",
    "store_unusable",
)

_HEX = frozenset("0123456789abcdef")
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


class TrustStoreError(Exception):
    """The trust file cannot be used.  ``str()`` is a fixed text plus the path: fit to print, never file content."""


@dataclass(frozen=True)
class TrustEntry:
    decision: str
    fingerprint: str
    time: int


@dataclass(frozen=True)
class TrustState:
    root: str
    trusted: bool
    needs_prompt: bool
    files: tuple[str, ...]
    reason: str
    fingerprint: Optional[str] = None
    message: str = ""


def canonical_root(path) -> Optional[str]:
    """The real absolute path of a folder as a str, or None if ``path`` is not a usable path."""
    try:
        raw = os.fspath(path)
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if not isinstance(raw, str) or not raw or "\0" in raw:
            return None
        return os.path.realpath(raw)
    except (OSError, ValueError, TypeError):
        return None


def _valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _entry(key: object, value: object) -> Optional[TrustEntry]:
    if not isinstance(key, str) or not key.startswith("/") or "\0" in key or len(key) > 4096:
        return None
    if os.path.normpath(key) != key or not isinstance(value, dict):
        return None
    decision, fingerprint, when = value.get("decision"), value.get("fingerprint"), value.get("time")
    if not isinstance(decision, str) or decision not in DECISIONS or not _valid_fingerprint(fingerprint):
        return None
    if type(when) is not int or when < 0:
        return None
    return TrustEntry(decision, fingerprint, when)


class TrustStore:
    """The trust file.  Every call reads the file afresh (it is tiny) so a stale copy is never believed."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time,
                 prepare_dir: Optional[Callable[[], object]] = None) -> None:
        self.path = Path(path)
        self._clock = clock
        self._prepare_dir = prepare_dir
        self.ignored_entries = 0

    @classmethod
    def default(cls, environ: Optional[Mapping[str, str]] = None) -> "TrustStore":
        """``<M2C home>/trust.json`` (M2C_HOME honoured).  Touches nothing; the directory is made on the first write."""
        return cls(paths.config_dir(environ) / FILE_NAME, prepare_dir=lambda: paths.ensure_config_dir(environ))

    # ------------------------------------------------------------------------------------------------ reading
    def _fail(self, what: str) -> TrustStoreError:
        return TrustStoreError(f"The trust file {self.path} {what}")

    def _read(self) -> Optional[str]:
        """The file's text, None if there is no file; raises TrustStoreError for anything not to be believed."""
        path = self.path
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError:
            raise self._fail("cannot be inspected; refusing to use it.") from None
        if not stat.S_ISREG(before.st_mode):
            raise self._fail(f"is not a regular file (a link or a directory); refusing to use it. Remove it with: rm -r {shlex.quote(str(path))}")
        if before.st_uid != os.geteuid():
            raise self._fail(f"belongs to another user; refusing to use it. Remove it with: rm {shlex.quote(str(path))}")
        mode = stat.S_IMODE(before.st_mode)
        if mode & 0o177:
            raise self._fail(
                f"is accessible to other accounts (mode {mode:04o}); refusing to use it. "
                f"Fix it with: chmod 600 {shlex.quote(str(path))}"
            )
        try:
            fd = os.open(path, _READ_FLAGS)
        except OSError:
            raise self._fail("cannot be read; refusing to use it.") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_ino, info.st_dev) != (before.st_ino, before.st_dev):
                raise self._fail("changed while it was being read; refusing to use it.")
            if info.st_size > MAX_FILE_BYTES:
                raise self._fail(f"is too large; refusing to use it. Remove it with: rm {shlex.quote(str(path))}")
            data = b""
            while len(data) <= MAX_FILE_BYTES:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            raise self._fail("cannot be read; refusing to use it.") from None
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES:
            raise self._fail(f"is too large; refusing to use it. Remove it with: rm {shlex.quote(str(path))}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise self._damaged() from None

    def _damaged(self) -> TrustStoreError:
        return self._fail(f"is damaged; refusing to use it. Delete it with: rm {shlex.quote(str(self.path))}")

    def load(self) -> dict[str, TrustEntry]:
        """Every well-formed decision.  Malformed entries are skipped (and counted in ``ignored_entries``)."""
        self.ignored_entries = 0
        text = self._read()
        if text is None:
            return {}
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise self._damaged() from None
        if not isinstance(data, dict):
            raise self._damaged()
        version = data.get("version")
        if type(version) is not int:
            raise self._damaged()
        if version > VERSION:
            raise self._fail("was written by a newer m2c; refusing to use it. Update m2c, or move the file aside.")
        if version != VERSION or not isinstance(data.get("folders"), dict):
            raise self._damaged()
        entries: dict[str, TrustEntry] = {}
        for key, value in data["folders"].items():
            entry = _entry(key, value)
            if entry is None:
                self.ignored_entries += 1
            else:
                entries[key] = entry
        return entries

    def get(self, root) -> Optional[TrustEntry]:
        key = canonical_root(root)
        entries = self.load()
        return None if key is None else entries.get(key)

    def items(self) -> list[tuple[str, TrustEntry]]:
        return sorted(self.load().items())

    # ------------------------------------------------------------------------------------------------ writing
    def set(self, root, decision: str, fingerprint: str) -> None:
        """Remember ``decision`` for the folder and the fingerprint it was made for.  Raises ValueError for a bad
        argument (a programming error) and TrustStoreError when the file cannot be read or written."""
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ValueError("decision must be 'trusted' or 'untrusted'")
        if not _valid_fingerprint(fingerprint):
            raise ValueError("fingerprint must be 64 lowercase hex characters")
        key = canonical_root(root)
        if key is None:
            raise ValueError("root is not a usable folder path")
        entries = self.load()
        entries.pop(key, None)
        entries[key] = TrustEntry(decision, fingerprint, int(self._clock()))
        if len(entries) > MAX_ENTRIES:
            ordered = sorted(entries.items(), key=lambda item: item[1].time)  # stable: oldest first
            entries = dict(ordered[len(entries) - MAX_ENTRIES:])
        self._write(entries)

    def forget(self, root) -> bool:
        """Drop the decision for a folder (it need not exist any more).  True if there was one."""
        key = canonical_root(root)
        entries = self.load()
        if key is None or key not in entries:
            return False
        del entries[key]
        self._write(entries)
        return True

    def _write(self, entries: Mapping[str, TrustEntry]) -> None:
        failure = self._fail("could not be saved. Check free space and permissions in that folder.")
        tmp = None
        try:
            if self._prepare_dir is not None:
                self._prepare_dir()
            else:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            body = json.dumps(
                {"version": VERSION, "folders": {k: {"decision": e.decision, "fingerprint": e.fingerprint, "time": e.time}
                                                 for k, e in sorted(entries.items())}},
                indent=2, sort_keys=True,
            ) + "\n"
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".trust-", suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(body.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
            self._sync_dir()
        except (OSError, paths.ConfigDirError):
            raise failure from None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _sync_dir(self) -> None:
        """Best effort: make the rename itself durable."""
        try:
            fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def project_trust_state(root, store: TrustStore, **limits) -> TrustState:
    """The trust decision for a project folder (see the module docstring).  Reads, never writes; never raises.

    ``limits`` (max_files, max_total_bytes, max_entries, max_depth) are passed to the fingerprint; tests use them.
    """
    canonical = canonical_root(root) if isinstance(root, (str, os.PathLike)) else None
    if canonical is None:
        return _state("", "unreadable", (), None)
    try:
        found = compute_fingerprint(canonical, **limits)
        if found.problem is not None:
            return _state(canonical, found.problem, found.files, None)
        if not found.files:
            return _state(canonical, "nothing_to_trust", (), found.digest)
        try:
            entry = store.get(canonical)
        except TrustStoreError as exc:
            return _state(canonical, "store_unusable", found.files, found.digest, str(exc))
        if entry is None:
            return _state(canonical, "never_asked", found.files, found.digest)
        if entry.fingerprint != found.digest:
            return _state(canonical, "changed", found.files, found.digest)
        return _state(canonical, "trusted" if entry.decision == "trusted" else "declined", found.files, found.digest)
    except Exception:  # noqa: BLE001 -- security checks fail closed: anything unexpected is "not trusted"
        return _state(canonical, "unreadable", (), None)


def _state(root: str, reason: str, files, fingerprint, message: str = "") -> TrustState:
    state = TrustState(
        root=root,
        trusted=reason == "trusted",
        needs_prompt=reason in ("never_asked", "changed"),
        files=tuple(files),
        reason=reason,
        fingerprint=fingerprint,
        message=message,
    )
    log_event(logger, logging.DEBUG, "trust.state", code=reason, count=len(state.files))
    return state
