"""
m2c/src/m2c/context/instructions.py -- Patch C12: hierarchical instruction files
(M2C.md, AGENTS.md, CLAUDE.md) with ``@path`` imports.

What this produces
------------------
``load_instructions(root, cwd, home)`` returns the text for the ``instructions``
field of ``run.start`` (protocol/v1/run.start.json), plus the list of files it
came from and a list of *notes* about anything that was skipped or cut.  Nothing
is sent anywhere by this module; it only reads files (product guide section 9,
build plan C12).

Order, and what "takes precedence" means
----------------------------------------
Content is concatenated from the LEAST specific source to the MOST specific:

    1. the global file          <M2C home>/M2C.md   (M2C_HOME is honoured, see paths.py)
    2. the repository root      M2C.md, then AGENTS.md, then CLAUDE.md
    3. every directory below the root down to the session's working directory,
       each with the same three names in the same order

A later block overrides an earlier one by being later (models weight what they
read last).  Only ``M2C.md`` is read from the global directory.  Directories that
are not on the way from the root to the cwd are never read.

``@path`` imports
-----------------
An import is a token ``@some/path.md`` that

* is at the start of a line or follows whitespace or an opening bracket/quote
  (so ``someone@example.com`` and URLs such as ``.../@user/x`` are not imports);
* is not inside a fenced code block (``` or ~~~) or an inline code span;
* looks like a path (it contains a ``/`` or ends in an extension), so a plain
  mention such as ``@alice`` is left alone and produces no note.

Trailing sentence punctuation (``.:!?``) is not part of the path.  A successful
import is replaced, where it stood, by the imported text between
``<!-- m2c: begin import X -->`` and ``<!-- m2c: end import X -->`` lines.  A
refused import is left in place with a short ``<!-- m2c: import skipped: why -->``
comment after it, and a ``ContextNote`` is recorded.

An import is resolved relative to the REAL directory of the file that contains
it, and is refused (never a crash) when

* its target, after resolving every symbolic link, is outside the repository
  root (for files under the root) or outside the global directory (for the
  global file): codes ``outside_root`` (the path itself leaves) and
  ``symlink_escape`` (the path looks inside but a link takes it out);
* the target, or the file a link points to, is not ``.md`` or ``.txt``
  (``not_text``), or contains NUL bytes;
* it would be more than ``max_import_depth`` (default 5) imports deep
  (``depth_limit``), or closes a cycle (``cycle``);
* the same file is already part of the output (``duplicate``): each file is
  included once, which also stops a "billion laughs" import tree from growing;
* the import budget (``max_total_bytes`` worth of imported text) is used up
  (``import_budget``): this bounds the work a hostile repository can cause;
* it does not exist (``missing``) or cannot be read (``unreadable``).

The same containment rule applies to the instruction files themselves: a
committed symlink ``CLAUDE.md -> ~/.ssh/some-file`` is skipped, not followed,
because everything here is sent to a server.

Size limits (all in UTF-8 bytes)
--------------------------------
* ``max_file_bytes`` (32 KiB): a longer file is cut on a line boundary and an
  ``<!-- m2c: truncated: ... -->`` marker is appended (``truncated``).
* ``max_total_bytes`` (100 000): if the whole text is longer, WHOLE files are
  dropped from the least specific end first (global before repository before
  subdirectory), because the most specific instructions are the ones that take
  precedence; a ``total_cap`` note names each one and a
  ``<!-- m2c: left out (size limit): ... -->`` line tells the model.  If the
  single most specific file alone is still too long it is truncated.

Bytes, not characters, because the whole ``run.start`` message has a 1 MiB
ceiling on the wire (server ws.py, client link/client.py): with these caps and
the ones in rules.py and memory.py the three fields together stay below
~190 KB of text, far under the ceiling even after JSON escaping, and each field
is far below the protocol's 200 000-character ``bulk_text`` maximum.

Privacy
-------
Labels and notes use paths RELATIVE to the repository root (or ``global/...``);
an absolute path never appears in the output or in a note.  File contents are
never logged (build plan 11.4): this module logs nothing at all.

Shared helpers
--------------
``ContextNote``, ``ContextSkip``, ``is_inside``, ``classify_escape``,
``read_text_capped`` and the byte helpers are used by rules.py and memory.py so
that there is exactly one implementation of "read a file safely".  They open
files with ``O_NOFOLLOW | O_NONBLOCK`` and check ``S_ISREG`` on the open
descriptor, so a FIFO named ``M2C.md`` cannot hang the client and a device file
cannot be read.  A directory component swapped for a symlink between the check
and the open (a race by someone who can already write inside the repository) is
out of scope.

Place this file at: m2c/src/m2c/context/instructions.py
"""
from __future__ import annotations

import codecs
import errno
import os
import re
import stat
from dataclasses import dataclass
from os import PathLike

from .. import paths as _paths

# ---------------------------------------------------------------------------
# Names, limits and note codes
# ---------------------------------------------------------------------------
INSTRUCTION_FILE_NAMES = ("M2C.md", "AGENTS.md", "CLAUDE.md")
GLOBAL_FILE_NAMES = ("M2C.md",)
IMPORT_EXTENSIONS = (".md", ".txt")
MAX_IMPORT_DEPTH = 5
MAX_FILE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 100_000

NOTE_MISSING = "missing"
NOTE_NOT_TEXT = "not_text"
NOTE_OUTSIDE = "outside_root"
NOTE_SYMLINK = "symlink_escape"
NOTE_CYCLE = "cycle"
NOTE_DEPTH = "depth_limit"
NOTE_DUPLICATE = "duplicate"
NOTE_UNREADABLE = "unreadable"
NOTE_BUDGET = "import_budget"
NOTE_TRUNCATED = "truncated"
NOTE_TOTAL_CAP = "total_cap"

REASONS = {
    NOTE_MISSING: "file not found",
    NOTE_NOT_TEXT: "not a text file (only .md and .txt can be imported)",
    NOTE_OUTSIDE: "it is outside the allowed directory",
    NOTE_SYMLINK: "a symbolic link leads outside the allowed directory",
    NOTE_CYCLE: "import cycle",
    NOTE_DEPTH: "import depth limit reached",
    NOTE_DUPLICATE: "already included",
    NOTE_UNREADABLE: "could not be read",
    NOTE_BUDGET: "import size budget used up",
}


@dataclass(frozen=True)
class ContextNote:
    """Something was skipped or cut.  ``source`` is a label (never an absolute
    path); ``message`` is one sentence and never contains file content."""

    code: str
    source: str
    message: str


@dataclass(frozen=True)
class InstructionsResult:
    text: str                       # the ``instructions`` field of run.start
    sources: tuple[str, ...]        # labels of the instruction files in the text, least specific first
    imports: tuple[str, ...]        # labels of the imported files in the text, in order
    notes: tuple[ContextNote, ...]


class ContextSkip(Exception):
    """A file cannot be used; ``code`` is one of the NOTE_* strings."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Shared helpers (also used by rules.py and memory.py)
# ---------------------------------------------------------------------------
def nbytes(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def clip_utf8(text: str, max_bytes: int) -> str:
    """``text`` cut to at most ``max_bytes`` UTF-8 bytes, never mid-character."""
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", "surrogatepass")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def cut_at_line(text: str) -> str:
    """Drop a trailing partial line (everything after the last newline)."""
    cut = text.rfind("\n")
    return text[:cut] if cut > 0 else text


def safe_label(value: str, limit: int = 120) -> str:
    """Printable, single-line, comment-safe form of a name for markers and notes."""
    cleaned = "".join(ch if ch.isprintable() and ch not in "<>" else "_" for ch in value)
    return cleaned[:limit]


def is_inside(path: str, base: str) -> bool:
    """``path`` equals ``base`` or lies below it (both already normalised)."""
    if path == base:
        return True
    prefix = base if base.endswith(os.sep) else base + os.sep
    return path.startswith(prefix)


def classify_escape(lexical: str, real: str, base_real: str) -> str | None:
    """None when ``real`` is inside ``base_real``; otherwise the reason."""
    if is_inside(real, base_real):
        return None
    return NOTE_SYMLINK if is_inside(lexical, base_real) else NOTE_OUTSIDE


def real_path(value: str | PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(value)))


def resolve_home(home: str | PathLike[str] | None) -> str:
    """The M2C home directory: the argument, or ``paths.config_dir()`` (which
    honours M2C_HOME and raises ConfigDirError for a relative one)."""
    return os.fspath(_paths.config_dir() if home is None else home)


def rel_label(real: str, base_real: str, prefix: str = "") -> str:
    rel = os.path.relpath(real, base_real).replace(os.sep, "/")
    return prefix + ("" if rel == "." else rel)


def read_text_capped(real: str, max_bytes: int) -> tuple[str, bool]:
    """Read ``real`` (an already resolved path) as text, at most ``max_bytes``.

    Returns ``(text, truncated)``.  When the file is longer the text is cut on a
    line boundary and ``truncated`` is True.  Raises ContextSkip with
    ``missing``, ``unreadable`` (not a regular file, no permission, ...) or
    ``not_text`` (NUL bytes).  The text has no BOM and uses ``\\n`` newlines;
    invalid UTF-8 becomes U+FFFD.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        raise ContextSkip(NOTE_MISSING if exc.errno in (errno.ENOENT, errno.ENOTDIR) else NOTE_UNREADABLE) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ContextSkip(NOTE_UNREADABLE)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
    except OSError:
        raise ContextSkip(NOTE_UNREADABLE) from None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    if b"\x00" in raw:
        raise ContextSkip(NOTE_NOT_TEXT)
    text = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(raw, final=not truncated)
    text = text.removeprefix("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if truncated:
        text = cut_at_line(text)
    return text, truncated


# ---------------------------------------------------------------------------
# Import syntax
# ---------------------------------------------------------------------------
_IMPORT_RE = re.compile(r"(?<![^\s(\[{\"'])@(?P<path>[^\s()<>\[\]{}\"'`,;]+)")
_CODE_SPAN_RE = re.compile(r"(`+).+?\1")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_PATHLIKE_RE = re.compile(r"/|\.[A-Za-z0-9]{1,8}$")
_TRAILING_PUNCTUATION = ".:!?"


@dataclass
class _Chunk:
    label: str
    text: str
    imports: tuple[str, ...]


class _Loader:
    """State for one ``load_instructions`` call."""

    def __init__(self, max_depth: int, max_file_bytes: int, max_total_bytes: int) -> None:
        self.max_depth = max_depth
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.seen: set[str] = set()
        self.notes: list[ContextNote] = []
        self.imports: list[str] = []
        self.imported_bytes = 0

    def note(self, code: str, source: str, message: str) -> None:
        self.notes.append(ContextNote(code, source, message))

    # -- discovered files ---------------------------------------------------
    def discover(self, directory: str, names: tuple[str, ...], base_real: str, prefix: str) -> list[_Chunk]:
        chunks = []
        for name in names:
            candidate = os.path.join(directory, name)
            if not os.path.lexists(candidate):
                continue
            label = rel_label(candidate, base_real, prefix)
            chunk = self._discovered(candidate, label, base_real, prefix)
            if chunk is not None:
                chunks.append(chunk)
        return chunks

    def _discovered(self, candidate: str, label: str, base_real: str, prefix: str) -> _Chunk | None:
        real = os.path.realpath(candidate)
        escape = classify_escape(candidate, real, base_real)
        if escape:
            self.note(escape, label, f"Skipped {safe_label(label)}: {REASONS[escape]}.")
            return None
        if real in self.seen:
            self.note(NOTE_DUPLICATE, label, f"Skipped {safe_label(label)}: same file as one already included.")
            return None
        try:
            text, truncated = read_text_capped(real, self.max_file_bytes)
        except ContextSkip as exc:
            self.note(exc.code, label, f"Skipped {safe_label(label)}: {REASONS[exc.code]}.")
            return None
        self.seen.add(real)
        first_import = len(self.imports)
        body = self._expand(text, real, label, base_real, prefix, 0, (real,))
        if truncated:
            body += "\n" + self._truncation_marker(label)
        if not body.strip():
            return None
        header = f"<!-- m2c: instructions from {safe_label(label)} -->"
        return _Chunk(label, header + "\n" + body.strip("\n"), tuple(self.imports[first_import:]))

    def _truncation_marker(self, label: str) -> str:
        self.note(NOTE_TRUNCATED, label,
                  f"{safe_label(label)} is larger than {self.max_file_bytes} bytes; the rest was left out.")
        return (f"<!-- m2c: truncated: {safe_label(label)} is larger than {self.max_file_bytes} bytes; "
                f"the rest was left out -->")

    # -- imports ------------------------------------------------------------
    def _expand(self, text: str, real: str, label: str, base_real: str, prefix: str, depth: int,
                stack: tuple[str, ...]) -> str:
        out: list[str] = []
        fence: str | None = None
        for line in text.split("\n"):
            stripped = line.lstrip()
            if fence is not None:
                if stripped.startswith(fence * 3):
                    fence = None
                out.append(line)
                continue
            opening = _FENCE_RE.match(stripped)
            if opening:
                fence = opening.group(1)[0]
                out.append(line)
                continue
            if "@" not in line:
                out.append(line)
                continue
            out.extend(self._expand_line(line, real, label, base_real, prefix, depth, stack))
        return "\n".join(out)

    def _expand_line(self, line: str, real: str, label: str, base_real: str, prefix: str, depth: int,
                     stack: tuple[str, ...]) -> list[str]:
        spans = [m.span() for m in _CODE_SPAN_RE.finditer(line)]
        lines: list[str] = []
        pending = ""
        cursor = 0
        spliced = False
        for match in _IMPORT_RE.finditer(line):
            if any(start <= match.start() < end for start, end in spans):
                continue
            token = match.group("path").rstrip(_TRAILING_PUNCTUATION)
            if not token or not _PATHLIKE_RE.search(token):
                continue
            pending += line[cursor:match.start()]
            end = match.start() + 1 + len(token)
            block, code = self._import(token, real, label, base_real, prefix, depth + 1, stack)
            if block is not None:
                if pending.strip():
                    lines.append(pending.rstrip())
                lines.extend(block)
                pending = ""
                spliced = True
            else:
                reason = REASONS[code]
                self.note(code, label, f"Import of {safe_label(token)} in {safe_label(label)} was skipped: {reason}.")
                pending += line[match.start():end] + f" <!-- m2c: import skipped: {reason} -->"
            cursor = end
        tail = pending + line[cursor:]
        if spliced:
            if tail.strip():
                lines.append(tail.lstrip())
        else:
            lines.append(tail)
        return lines

    def _import(self, token: str, importer_real: str, importer_label: str, base_real: str, prefix: str, depth: int,
                stack: tuple[str, ...]) -> tuple[list[str] | None, str | None]:
        lexical = os.path.normpath(os.path.join(os.path.dirname(importer_real), token))
        target = os.path.realpath(lexical)
        escape = classify_escape(lexical, target, base_real)
        if escape:
            return None, escape
        if not all(os.path.splitext(p)[1].lower() in IMPORT_EXTENSIONS for p in (lexical, target)):
            return None, NOTE_NOT_TEXT
        if depth > self.max_depth:
            return None, NOTE_DEPTH
        if target in stack:
            return None, NOTE_CYCLE
        if target in self.seen:
            return None, NOTE_DUPLICATE
        if self.imported_bytes >= self.max_total_bytes:
            return None, NOTE_BUDGET
        try:
            text, truncated = read_text_capped(target, self.max_file_bytes)
        except ContextSkip as exc:
            return None, exc.code
        self.seen.add(target)
        self.imported_bytes += nbytes(text)
        label = rel_label(target, base_real, prefix)
        self.imports.append(label)
        body = self._expand(text, target, label, base_real, prefix, depth, stack + (target,))
        if truncated:
            body += "\n" + self._truncation_marker(label)
        shown = safe_label(label)
        block = [f"<!-- m2c: begin import {shown} -->"]
        if body.strip():
            block.extend(body.strip("\n").split("\n"))
        block.append(f"<!-- m2c: end import {shown} -->")
        return block, None


# ---------------------------------------------------------------------------
# Fitting the total
# ---------------------------------------------------------------------------
def _omitted_marker(labels: list[str]) -> str:
    return "<!-- m2c: left out (size limit): " + ", ".join(safe_label(x) for x in labels) + " -->"


def _fit(chunks: list[_Chunk], cap: int, loader: _Loader) -> tuple[str, list[_Chunk]]:
    """Join the chunks within ``cap`` bytes, dropping the least specific first."""
    kept = list(chunks)
    dropped: list[str] = []

    def total() -> int:
        marker = _omitted_marker(dropped) if dropped else ""
        return (sum(nbytes(c.text) for c in kept) + 2 * max(len(kept) - 1, 0)
                + (nbytes(marker) + 2 if marker else 0))

    while len(kept) > 1 and total() > cap:
        gone = kept.pop(0)
        dropped.append(gone.label)
        loader.note(NOTE_TOTAL_CAP, gone.label,
                    f"{safe_label(gone.label)} was left out: the instructions exceed the size limit.")
    marker = _omitted_marker(dropped) if dropped else ""
    if kept and total() > cap:
        only = kept[0]
        trunc = (f"<!-- m2c: truncated: {safe_label(only.label)} did not fit the size limit -->")
        allowed = cap - (nbytes(marker) + 2 if marker else 0) - nbytes(trunc) - 1
        text = cut_at_line(clip_utf8(only.text, allowed)) if allowed > 0 else ""
        loader.note(NOTE_TRUNCATED, only.label,
                    f"{safe_label(only.label)} was cut: the instructions exceed the size limit.")
        kept[0] = _Chunk(only.label, (text + "\n" + trunc) if text else trunc, only.imports)
    parts = ([marker] if marker else []) + [c.text for c in kept]
    return clip_utf8("\n\n".join(parts), cap), kept


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def load_instructions(
    root: str | PathLike[str],
    cwd: str | PathLike[str],
    home: str | PathLike[str] | None = None,
    *,
    max_import_depth: int = MAX_IMPORT_DEPTH,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> InstructionsResult:
    """Load the instruction files for a session (see the module docstring).

    ``root`` is the repository root, ``cwd`` the session's working directory
    (inside ``root``; ValueError otherwise) and ``home`` the M2C home directory
    (None: ``paths.config_dir()``, which honours M2C_HOME and raises
    ``ConfigDirError`` for a bad value).  Missing files and directories are not
    errors.  Never raises for anything inside a repository file.
    """
    real_root, real_cwd = real_path(root), real_path(cwd)
    if not is_inside(real_cwd, real_root):
        raise ValueError("the working directory must be inside the repository root")
    global_dir = real_path(resolve_home(home))
    loader = _Loader(max_import_depth, max_file_bytes, max_total_bytes)

    chunks = loader.discover(global_dir, GLOBAL_FILE_NAMES, global_dir, "global/")
    directory = real_root
    chunks += loader.discover(directory, INSTRUCTION_FILE_NAMES, real_root, "")
    rel = os.path.relpath(real_cwd, real_root)
    if rel != ".":
        for part in rel.split(os.sep):
            directory = os.path.join(directory, part)
            chunks += loader.discover(directory, INSTRUCTION_FILE_NAMES, real_root, "")

    text, kept = _fit(chunks, max_total_bytes, loader)
    return InstructionsResult(
        text=text,
        sources=tuple(c.label for c in kept),
        imports=tuple(label for c in kept for label in c.imports),
        notes=tuple(loader.notes),
    )
