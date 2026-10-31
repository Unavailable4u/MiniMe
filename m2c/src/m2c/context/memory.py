"""
m2c/src/m2c/context/memory.py -- Patch C12: the project's auto-memory index and
memory writes that the user approves first.

Where memory lives
------------------
Memory is plain files on this device, one directory per project:

    <M2C home>/memory/<project-key>/MEMORY.md        the index (sent as ``memory_index``)
    <M2C home>/memory/<project-key>/<topic>.md       topic files (not sent by this module)

``<M2C home>`` is ``~/.m2c`` unless ``M2C_HOME`` says otherwise (paths.py).  The
project key is ``<slug>-<12 hex digits>``: the slug is the repository folder's
name (lower-case ASCII, unsafe characters turned into ``-``, at most 40
characters) and the digits are the start of the SHA-256 of the repository
root's REAL path (symbolic links resolved, so two ways of reaching the same
folder share one memory).  The key is therefore stable between runs, readable
in a directory listing, and different for two projects that happen to share a
folder name.  It is keyed by location: moving or renaming the repository starts
a new, empty memory (the old directory stays and can be moved by hand).  A git
remote was rejected as the key because many repositories have none and it would
put a URL in a directory name.

The index sent to the server
----------------------------
``load_memory_index`` returns the first 200 lines or the first 25 KB (25 * 1024
bytes) of ``MEMORY.md``, whichever is smaller.  Cuts happen on a line boundary
and never inside a UTF-8 character; ``truncated`` and a ``truncated`` note say
when something was left out.  A missing file is an empty index.  A symbolic link
that leads out of the memory directory, a non-regular file, or a file with NUL
bytes is not read (a note says why).  Nothing is ever written by loading.

Memory writes: prepared, shown, approved, applied
--------------------------------------------------
``prepare_memory_write(root, text, ...)`` validates a proposed write and returns
a frozen ``PreparedMemoryWrite``.  It NEVER writes, creates a directory, or
changes anything: the caller shows ``prepared.preview`` to the user, and only
``apply_memory_write(prepared, approved=True)`` writes (``approved`` must be the
boolean ``True``, not merely truthy).  There is no "force" option.  Every write
is therefore shown first (guide section 9.3: "every auto-memory write is shown
in the UI").

Secrets are never stored.  A text that looks like any of these is REFUSED with
``MemoryWriteRefused``, whose message says what kind of thing was found and on
which line (never the text itself):

    a private key block; GitHub, GitLab, Slack and Stripe tokens (ghp_, github_pat_,
    glpat-, xox, sk_live_); API keys (sk-, AIza...); AWS access key IDs (AKIA...);
    a JSON Web Token; ``Bearer <token>``; an ``Authorization:`` value; a password
    assignment (``password = ...``, ``DB_PASSWORD=...``); a secret/token/API-key
    assignment with a token-shaped value; a ``.env`` style ``NAME=value`` line; a
    URL with ``user:password@``.

Detection works on the text after Unicode NFKC normalisation with invisible
format characters (zero-width spaces ...) removed, so those cannot be used to
slip a token past the patterns.  It is deliberately simple and errs towards
refusing: ``password: required`` is refused, which costs the user one rewording.
C15 (secrets, redaction and environment) will generalise secret detection for
the whole client; ``find_secrets`` is the one function to replace or extend then.
Detection runs again inside ``apply_memory_write``, because a
``PreparedMemoryWrite`` is a plain dataclass that a caller could build by hand;
for the same reason apply re-validates the project key and the target name and
derives the file path itself instead of trusting a path from the object.

Other rules for a write: ``target`` is ``MEMORY.md`` or a topic file name of the
form ``name.md`` (letters, digits, ``.``, ``_``, ``-``; at most 64 characters; no
directories); ``mode`` is ``append`` (the default: a new line is added) or
``replace``; the text is at most 8 KiB, not empty, without NUL.  ``prepare``
records the SHA-256 of the file as it is now (symbolic links are refused), and
apply fails with ``MemoryWriteConflict`` if the file changed in between, so an
approved write never lands on content the user did not see.  Files are written
atomically (temporary file, fsync, rename) with mode 0600 in directories of
mode 0700.

Never logged: this module logs nothing; text and paths do not appear in notes.

Place this file at: m2c/src/m2c/context/memory.py
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from .instructions import (
    NOTE_TRUNCATED,
    REASONS,
    ContextNote,
    ContextSkip,
    classify_escape,
    clip_utf8,
    is_inside,
    nbytes,
    read_text_capped,
    real_path,
    resolve_home,
)

MEMORY_DIRNAME = "memory"
INDEX_NAME = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024
MAX_WRITE_BYTES = 8 * 1024
_MAX_EXISTING_BYTES = 4 * 1024 * 1024

_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}\.md")
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}-[0-9a-f]{12}")
_MODES = ("append", "replace")


class MemoryWriteError(Exception):
    """Base class of the memory-write failures."""


class MemoryWriteRefused(MemoryWriteError, ValueError):
    """The text looks like it contains a secret.  ``findings`` has kind and line only."""

    def __init__(self, findings: tuple[SecretFinding, ...]) -> None:
        self.findings = findings
        super().__init__(_refusal_message(findings))


class MemoryWriteNotApproved(MemoryWriteError):
    """``apply_memory_write`` was called without ``approved=True``."""


class MemoryWriteConflict(MemoryWriteError):
    """The target file changed after the write was prepared; nothing was written."""


# ---------------------------------------------------------------------------
# Locating the project's memory
# ---------------------------------------------------------------------------
def project_key(root: str | PathLike[str]) -> str:
    """Stable per-project directory name (see the module docstring)."""
    real = real_path(root)
    slug = re.sub(r"[^a-z0-9._-]+", "-", os.path.basename(real).lower()).strip("-._")[:40].strip("-._") or "project"
    digest = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return f"{slug}-{digest}"


def memory_dir(root: str | PathLike[str], home: str | PathLike[str] | None = None) -> Path:
    """``<M2C home>/memory/<project-key>``.  Pure: creates nothing."""
    return Path(resolve_home(home)) / MEMORY_DIRNAME / project_key(root)


# ---------------------------------------------------------------------------
# Loading the index
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MemoryIndex:
    text: str                        # the ``memory_index`` field of run.start
    truncated: bool
    notes: tuple[ContextNote, ...]


def load_memory_index(root: str | PathLike[str], home: str | PathLike[str] | None = None) -> MemoryIndex:
    """The first 200 lines or 25 KB of the project's MEMORY.md ("" if none)."""
    label = f"{MEMORY_DIRNAME}/{INDEX_NAME}"
    directory = memory_dir(root, home)
    if not os.path.lexists(directory / INDEX_NAME):
        return MemoryIndex("", False, ())
    real_dir = os.path.realpath(directory)
    lexical = os.path.join(real_dir, INDEX_NAME)
    real = os.path.realpath(lexical)
    escape = classify_escape(lexical, real, real_dir)
    if escape:
        return MemoryIndex("", False, (ContextNote(escape, label, f"Skipped {label}: {REASONS[escape]}."),))
    try:
        text, cut_by_bytes = read_text_capped(real, MAX_INDEX_BYTES)
    except ContextSkip as exc:
        return MemoryIndex("", False, (ContextNote(exc.code, label, f"Skipped {label}: {REASONS[exc.code]}."),))
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    too_many_lines = len(lines) > MAX_INDEX_LINES
    joined = "\n".join(lines[:MAX_INDEX_LINES])
    clipped = clip_utf8(joined, MAX_INDEX_BYTES)
    truncated = cut_by_bytes or too_many_lines or clipped != joined
    notes = ()
    if truncated:
        notes = (ContextNote(NOTE_TRUNCATED, label,
                             f"{label} was cut to its first {MAX_INDEX_LINES} lines or 25 KB."),)
    return MemoryIndex(clipped, truncated, notes)


# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SecretFinding:
    """Where something secret-looking was found.  Never contains the text."""

    kind: str
    line: int


_I = re.IGNORECASE
_NAME_TAIL = r"[A-Za-z0-9_.-]*"
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("authorization_header", re.compile(
        r"\bAuthorization\s*[:=]\s*[\"']?(?:(?:Bearer|Basic|Token|Digest|Negotiate)\s+\S{8,}"
        r"|[A-Za-z0-9._~+/=-]{16,})", _I)),
    ("url_credentials", re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")),
)
_PASSWORD_RE = re.compile(
    r"(?:passw(?:or)?d|pwd|passphrase)" + _NAME_TAIL + r"\s*[:=]\s*(?P<value>\"[^\"]+\"|'[^']+'|[^\s,;]+)", _I)
_SECRET_NAME_RE = re.compile(
    r"(?:secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth[_-]?key|credential)s?" + _NAME_TAIL
    + r"\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9+/=_.-]{16,})", _I)
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]+=\S")
_PLACEHOLDER_RE = re.compile(r"^(?:<.*>|\$\{.*\}|\$\(.*\)|\$[A-Za-z_][A-Za-z0-9_]*|\{\{.*\}\}|%\(.*\)s?|[*x.•]{3,})$", _I)


def _normalise_for_scan(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def find_secrets(text: str) -> list[SecretFinding]:
    """Everything in ``text`` that looks like a secret, as (kind, line) pairs.

    Never returns or logs the matched text.  Simple and deliberately cautious;
    C15 (secrets, redaction and environment) will generalise client-wide secret
    detection and should replace or extend this function.
    """
    found: set[tuple[int, str]] = set()
    for number, line in enumerate(_normalise_for_scan(text).split("\n"), 1):
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                found.add((number, kind))
        for match in _PASSWORD_RE.finditer(line):
            value = match.group("value").strip("\"'")
            if value and not _PLACEHOLDER_RE.match(value):
                found.add((number, "password_assignment"))
        for match in _SECRET_NAME_RE.finditer(line):
            value = match.group("value")
            if any(c.isdigit() for c in value) and any(c.isalpha() for c in value):
                found.add((number, "secret_assignment"))
        if _ENV_LINE_RE.match(line):
            found.add((number, "env_line"))
    return [SecretFinding(kind=kind, line=number) for number, kind in sorted(found)]


_KIND_TEXT = {
    "private_key": "a private key block",
    "github_token": "a GitHub access token",
    "gitlab_token": "a GitLab access token",
    "api_key": "an API key",
    "stripe_key": "a Stripe key",
    "aws_access_key": "an AWS access key ID",
    "slack_token": "a Slack token",
    "google_api_key": "a Google API key",
    "jwt": "a JSON Web Token",
    "bearer_token": "a Bearer token",
    "authorization_header": "an Authorization header value",
    "password_assignment": "a password assignment",
    "secret_assignment": "a secret, token or API key assignment",
    "env_line": "a .env style NAME=value line",
    "url_credentials": "a URL with a user name and password",
}


def _refusal_message(findings: tuple[SecretFinding, ...]) -> str:
    shown = "; ".join(f"{_KIND_TEXT.get(f.kind, f.kind)} (line {f.line})" for f in findings[:5])
    more = f" and {len(findings) - 5} more" if len(findings) > 5 else ""
    return (f"Memory write refused: the text looks like it contains {shown}{more}. Memory must never hold secrets, "
            f"so nothing was saved. Describe where the secret lives (for example 'the API key is in the .env file') "
            f"instead of writing its value.")


# ---------------------------------------------------------------------------
# Preparing and applying a write
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PreparedMemoryWrite:
    """A validated memory write waiting for the user's approval.  Holds no open
    files and has done nothing yet.  ``home`` is the M2C home directory;
    ``base_sha256`` is the SHA-256 of the target file when the write was
    prepared (None: it did not exist)."""

    project_key: str
    target: str
    mode: str
    text: str
    home: str
    base_sha256: str | None

    @property
    def preview(self) -> str:
        """What to show the user before asking.  Contains no absolute path."""
        if self.mode == "replace":
            head = f"Memory write: REPLACE the whole of {self.target} for this project with:"
        else:
            head = f"Memory write: ADD to {self.target} for this project:"
        return f"{head}\n---\n{self.text}\n---"


def _check_target(target: str) -> None:
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        raise ValueError("target must be MEMORY.md or a file name like topic.md (letters, digits, . _ -)")


def _check_mode(mode: str) -> None:
    if mode not in _MODES:
        raise ValueError("mode must be 'append' or 'replace'")


def _clean_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("the memory text must be a string")
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n").rstrip()
    if not cleaned.strip():
        raise ValueError("the memory text is empty")
    if "\x00" in cleaned:
        raise ValueError("the memory text must not contain NUL characters")
    if nbytes(cleaned) > MAX_WRITE_BYTES:
        raise ValueError(f"one memory write can be at most {MAX_WRITE_BYTES} bytes")
    return cleaned


def _read_current(path: str) -> bytes | None:
    """The target's bytes, None if absent; ValueError for links and non-files."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("the memory file cannot be inspected") from None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("the memory file is a symbolic link; refusing to use it")
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_EXISTING_BYTES:
        raise ValueError("the memory file is not a regular file of reasonable size")
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        raise ValueError("the memory file cannot be read") from None


def _target_path(home: str, key: str, target: str) -> tuple[str, str]:
    """(directory, file) for a validated key and target; the directory must not
    leave the home directory."""
    if not _KEY_RE.fullmatch(key if isinstance(key, str) else ""):
        raise ValueError("invalid project key")
    _check_target(target)
    if not os.path.isabs(home):
        raise ValueError("the home directory must be an absolute path")
    directory = os.path.join(home, MEMORY_DIRNAME, key)
    if not is_inside(os.path.realpath(directory), os.path.realpath(home)):
        raise ValueError("the memory directory leads outside the home directory")
    return directory, os.path.join(directory, target)


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def prepare_memory_write(
    root: str | PathLike[str],
    text: str,
    *,
    home: str | PathLike[str] | None = None,
    target: str = INDEX_NAME,
    mode: str = "append",
) -> PreparedMemoryWrite:
    """Validate a proposed memory write and return it for the user to approve.

    Never writes.  Raises ValueError for a bad target/mode/text,
    ``MemoryWriteRefused`` (a ValueError) when the text looks like it holds a
    secret.  See the module docstring.
    """
    _check_target(target)
    _check_mode(mode)
    cleaned = _clean_text(text)
    findings = tuple(find_secrets(cleaned))
    if findings:
        raise MemoryWriteRefused(findings)
    home_dir = os.path.abspath(resolve_home(home))
    key = project_key(root)
    _directory, path = _target_path(home_dir, key, target)
    return PreparedMemoryWrite(project_key=key, target=target, mode=mode, text=cleaned, home=home_dir,
                               base_sha256=_digest(_read_current(path)))


def _make_private_dir(path: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if not os.path.isdir(path):
            raise ValueError("a memory path component exists but is not a directory") from None
        return
    os.chmod(path, 0o700)


def apply_memory_write(prepared: PreparedMemoryWrite, *, approved: bool) -> str:
    """Write an approved memory write; returns the label of the file.

    ``approved`` must be exactly ``True`` (MemoryWriteNotApproved otherwise).
    Re-validates everything (the object may have been built by hand), refuses
    secrets again, and raises MemoryWriteConflict if the target changed since
    ``prepare_memory_write``.
    """
    if approved is not True:
        raise MemoryWriteNotApproved("the memory write was not approved; nothing was written")
    _check_target(prepared.target)
    _check_mode(prepared.mode)
    cleaned = _clean_text(prepared.text)
    findings = tuple(find_secrets(cleaned))
    if findings:
        raise MemoryWriteRefused(findings)
    home = prepared.home
    directory, path = _target_path(home, prepared.project_key, prepared.target)

    os.makedirs(home, mode=0o700, exist_ok=True)
    _make_private_dir(os.path.join(home, MEMORY_DIRNAME))
    _make_private_dir(directory)
    directory, path = _target_path(home, prepared.project_key, prepared.target)   # re-check now that it exists
    current = _read_current(path)
    if _digest(current) != prepared.base_sha256:
        raise MemoryWriteConflict("the memory file changed after the write was prepared; nothing was written")

    body = cleaned.encode("utf-8") + b"\n"
    if prepared.mode == "append" and current:
        body = current + (b"" if current.endswith(b"\n") else b"\n") + body
    real_directory = os.path.realpath(directory)
    fd, temporary = tempfile.mkstemp(dir=real_directory, prefix=".m2c-memory-")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, os.path.join(real_directory, prepared.target))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return f"{MEMORY_DIRNAME}/{prepared.target}"
