"""
m2c/src/m2c/tools/search.py -- Patch C05: glob and grep.

Ported from daemon/tools.py (MiniMe, 6977ddf) in spirit only: the daemon had no
search tools, so these are new.  Nothing is imported from ``daemon/``.

glob
----
Pure Python, no subprocess.  The pattern is matched segment by segment against
the paths under the search directory (``*``, ``?``, ``[...]`` per segment, and a
whole segment ``**`` for "any number of directories, including none").  A
pattern without a ``/`` therefore matches the top level only; the "no files
matched" message says so, because that is the mistake models make.  The walk

* never follows a symlinked directory and never enters ``.git``;
* omits every protected entry (hard and user lists) and does not descend into a
  protected directory;
* lists a symlink only if its real target is a regular file inside the project
  that is not protected;
* is capped by entries scanned (``caps.glob_max_walk_entries``) and by time
  (``caps.glob_timeout_s``), and the result is capped by ``max_results``;
* returns a sorted list (plain code-point order of the project-relative path),
  so the same tree gives the same answer every time.

Ignore files are not read (see the patch notes).

grep
----
A wrapper around ripgrep (``rg``), which is an external tool: nothing is
installed and nothing is imported.  The model controls three things: a regular
expression, a path, and a file-name glob.  None of them may become a ripgrep
*option*, so:

* The argument vector is built in code by ``build_rg_argv`` from a fixed list of
  flags plus validated values.  The pattern is always the one token after
  ``-e`` (so a pattern such as ``--pre=cmd`` or ``-f /etc/passwd`` is searched
  for as text), the path is always the one token after ``--`` (and always starts
  ``./`` or is ``.``), and the glob is glued into one ``--glob=VALUE`` token.
* ``--pre``, ``--pre-glob``, ``-f``, ``--hostname-bin``, ``--search-zip``,
  ``--follow`` and ``--no-ignore`` are never emitted; ``--no-config`` stops a
  config file (and ``RIPGREP_CONFIG_PATH``) from adding any.
* There is no shell: the program is started from an argument list.  The
  environment is reduced to a cleaned ``PATH``; the working directory is the
  project root; the process runs in its own session so the whole group can be
  killed on timeout or when output limits are hit.
* The ripgrep binary is found on a ``PATH`` stripped of relative entries and of
  anything inside the project, and a binary whose real path is inside the
  project is refused (a hostile repository must not choose what runs).
* Protected files are excluded twice.  ripgrep is given ``--glob=!`` exclusions
  for the hard list *after* the model's glob (later globs win), and every result
  line is checked again here against both denylists; the second check is the
  authoritative one (it is case-insensitive, knows the user list, and the M2C
  state directory).  A file name containing a newline can make ripgrep's
  line-oriented output ambiguous, so any such record is dropped rather than
  guessed at.
* Output is capped three ways: by number of matches (``max_matches``, counted
  across files), by bytes kept (``caps.grep_output_bytes``) and by raw bytes
  read from ripgrep (``caps.grep_max_raw_bytes``); a marker line says which.
  There is a wall-clock timeout (``caps.grep_timeout_s``).

Known limits: ripgrep resolves the search path itself, so a swap of a directory
for a symlink between our check and its start is not prevented here; that is
what the sandbox (C11) is for.  Hard links are not detected.
"""
from __future__ import annotations

import fnmatch
import os
import re
import select
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .files import _guarded, _safe_text
from .paths import (
    HARD_NAME_PATTERNS,
    HARD_SEQUENCE_PATTERNS,
    ToolError,
    _is_inside,
    _open_child_dir,
    open_dir_chain,
    resolve_for_read,
)

if TYPE_CHECKING:  # pragma: no cover
    from . import ToolContext

MAX_GLOB_PATTERN_CHARS = 512
MAX_GLOB_SEGMENTS = 32
MAX_GREP_PATTERN_CHARS = 2000
MAX_GREP_GLOB_CHARS = 512
_MAX_WALK_DEPTH = 100
_READ_CHUNK = 65536
_STDERR_KEEP = 4096


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------
def _bad(msg: str) -> ToolError:
    return ToolError(msg, code="invalid_args")


def _search_dir_fd(ctx: ToolContext, path: str | None) -> tuple[int, tuple[str, ...]]:
    """Resolve and open the directory a search starts from (no symlink followed)."""
    res = resolve_for_read(ctx.root, "." if path is None else path, ctx.denylist)
    return open_dir_chain(ctx.root, res.rel), res.rel


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------
def _compile_glob(pattern: object) -> tuple[str, ...]:
    if not isinstance(pattern, str) or pattern == "":
        raise _bad("pattern must be a non-empty string")
    if "\0" in pattern:
        raise _bad("pattern contains a NUL byte")
    if len(pattern) > MAX_GLOB_PATTERN_CHARS:
        raise _bad(f"pattern is longer than {MAX_GLOB_PATTERN_CHARS} characters")
    if pattern.startswith("/"):
        raise _bad("pattern must be relative to the search directory, not absolute")
    segs = [s for s in pattern.split("/") if s not in ("", ".")]
    if not segs:
        raise _bad("pattern matches nothing")
    if any(s == ".." for s in segs):
        raise _bad("pattern must not contain '..'")
    if len(segs) > MAX_GLOB_SEGMENTS:
        raise _bad("pattern has too many path segments")
    out: list[str] = []
    for s in segs:
        if s == "**" and out and out[-1] == "**":
            continue  # "**/**" is "**"
        out.append(s)
    return tuple(out)


def _seg_match(pat: tuple[str, ...], segs: tuple[str, ...]) -> bool:
    """Match path segments against pattern segments; ``**`` = zero or more
    whole segments.  A set of reachable positions keeps it linear in the size
    of the pattern times the path (no exponential backtracking)."""
    positions = {0}
    n = len(segs)
    for p in pat:
        if p == "**":
            nxt = set(range(min(positions), n + 1))
        else:
            nxt = {i + 1 for i in positions if i < n and fnmatch.fnmatchcase(segs[i], p)}
        if not nxt:
            return False
        positions = nxt
    return n in positions


class _Walk:
    def __init__(self, ctx: ToolContext, pat: tuple[str, ...]) -> None:
        self.ctx = ctx
        self.pat = pat
        self.matches: list[str] = []
        self.scanned = 0
        self.stop: str | None = None  # "walk" | "time"
        self.deadline = time.monotonic() + ctx.caps.glob_timeout_s


def _symlink_to_safe_file(ctx: ToolContext, parts: tuple[str, ...]) -> bool:
    rs = str(ctx.root)
    real = os.path.realpath(os.path.join(rs, *parts))
    if real == rs or not _is_inside(real, rs):
        return False
    rel = tuple(real[len(rs) + 1:].split(os.sep))
    if ctx.denylist.blocks(rel) or any(p.casefold() == ".git" for p in rel):
        return False
    return os.path.isfile(real)


def _walk_dir(w: _Walk, fd: int, rel: tuple[str, ...], segs: tuple[str, ...], depth: int) -> None:
    ctx = w.ctx
    with os.scandir(fd) as it:
        entries = list(it)
    for entry in entries:
        if w.stop:
            return
        w.scanned += 1
        if w.scanned > ctx.caps.glob_max_walk_entries:
            w.stop = "walk"
            return
        if time.monotonic() > w.deadline:
            w.stop = "time"
            return
        name = entry.name
        if name.casefold() == ".git":
            continue
        parts = rel + (name,)
        if ctx.denylist.blocks(parts):
            continue
        entry_segs = segs + (name,)
        if entry.is_symlink():
            if _seg_match(w.pat, entry_segs) and _symlink_to_safe_file(ctx, parts):
                w.matches.append("/".join(parts))
        elif entry.is_dir(follow_symlinks=False):
            if depth >= _MAX_WALK_DEPTH:
                continue
            try:
                child = _open_child_dir(fd, name, False)
            except OSError:
                continue
            try:
                _walk_dir(w, child, parts, entry_segs, depth + 1)
            finally:
                os.close(child)
        elif entry.is_file(follow_symlinks=False):
            if _seg_match(w.pat, entry_segs):
                w.matches.append("/".join(parts))


@_guarded
def glob(ctx: ToolContext, pattern: str, path: str | None = None, max_results: int = 200) -> tuple[str, bool]:
    pat = _compile_glob(pattern)
    limit = max(1, min(max_results, ctx.caps.glob_max_results))
    fd, base = _search_dir_fd(ctx, path)
    w = _Walk(ctx, pat)
    try:
        _walk_dir(w, fd, base, (), 0)
    finally:
        os.close(fd)
    found = sorted(w.matches)
    shown = found[:limit]
    lines = [_safe_text(m) for m in shown]
    truncated = len(found) > limit
    if truncated:
        lines.append(
            f"[truncated: showing the first {limit} of {len(found)} matches; narrow the pattern or path, or raise max_results]"
        )
    if w.stop == "walk":
        lines.append(
            f"[stopped after scanning {ctx.caps.glob_max_walk_entries} entries; the results are incomplete; narrow the path or pattern]"
        )
    elif w.stop == "time":
        lines.append(
            f"[stopped at the time limit of {ctx.caps.glob_timeout_s:g} s; the results are incomplete; narrow the path or pattern]"
        )
    if w.stop:
        truncated = True
    if not lines:
        hint = ""
        if len(pat) == 1 and "**" not in pat:
            hint = (
                "; hint: a pattern without '/' matches the top level of the search directory only, "
                f"use '**/{pat[0]}' to search subdirectories"
            )
        return f"[no files matched{hint}]", False
    return "\n".join(lines), truncated


# ---------------------------------------------------------------------------
# grep: the argument vector
# ---------------------------------------------------------------------------
def _exclusion_globs() -> list[str]:
    """``--glob=!`` tokens for the hard list.  Case-sensitive, so only a first
    line of defence; the post-filter is authoritative."""
    out = [f"--glob=!{p}" for p in HARD_NAME_PATTERNS]
    out += [f"--glob=!**/{p}" for p in HARD_SEQUENCE_PATTERNS]
    return out


def _check_int(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def build_rg_argv(
    rg: str,
    *,
    pattern: str,
    search_arg: str,
    ignore_case: bool,
    context_lines: int,
    glob: str | None,
    max_matches: int,
) -> list[str]:
    """The complete ripgrep argument vector.  Raises ValueError for anything the
    tool's argument validation should already have refused.

    Layout: fixed flags, the model's glob (if any), the fixed exclusions, then
    ``-e PATTERN -- PATH``.  Nothing the model supplies is ever a separate
    token that ripgrep could read as an option.
    """
    if not isinstance(pattern, str) or pattern == "" or "\0" in pattern:
        raise ValueError("pattern must be a non-empty string without NUL")
    if not isinstance(search_arg, str) or search_arg == "" or "\0" in search_arg:
        raise ValueError("search path must be a non-empty string without NUL")
    _check_int(context_lines, "context_lines", 0)
    _check_int(max_matches, "max_matches", 1)
    if glob is not None and (not isinstance(glob, str) or glob == "" or "\0" in glob):
        raise ValueError("glob must be a non-empty string without NUL")
    argv = [
        rg,
        "--no-config",
        "--null",
        "--line-number",
        "--with-filename",
        "--no-heading",
        "--color=never",
        "--hidden",
        "--no-follow",
        "--sort=path",
        "--max-columns=500",
        "--max-columns-preview",
        "--max-filesize=20M",
        "--ignore-case" if ignore_case else "--case-sensitive",
        f"--max-count={max_matches}",
    ]
    if context_lines:
        argv.append(f"--context={context_lines}")
    if glob is not None:
        argv.append(f"--glob={glob}")  # before the exclusions: later globs win
    argv.append("--glob=!.git")
    argv.extend(_exclusion_globs())
    argv += ["-e", pattern, "--", search_arg]
    return argv


# ---------------------------------------------------------------------------
# grep: finding ripgrep
# ---------------------------------------------------------------------------
def _clean_path_entries(root: Path) -> list[str]:
    """PATH entries that are absolute and outside the project."""
    rs = str(root)
    out: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        try:
            real = os.path.realpath(entry)
        except (OSError, ValueError):
            continue
        if _is_inside(real, rs):
            continue
        out.append(entry)
    return out


def _vet_binary(ctx: ToolContext, candidate: str) -> str:
    rs = str(ctx.root)
    try:
        real = os.path.realpath(candidate)
    except (OSError, ValueError):
        raise ToolError("ripgrep (rg) could not be located", code="rg_not_found") from None
    if _is_inside(real, rs):
        raise ToolError(
            "ripgrep (rg) is located inside the project and will not be run; install it system-wide",
            code="rg_not_found",
        )
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise ToolError("ripgrep (rg) is not an executable file", code="rg_not_found")
    return candidate


def _find_rg(ctx: ToolContext) -> tuple[str, list[str]]:
    """The ripgrep path to run, and the PATH entries to give it."""
    entries = _clean_path_entries(ctx.root)
    if ctx.rg_path is not None:
        return _vet_binary(ctx, os.fspath(ctx.rg_path)), entries
    for d in entries:
        cand = os.path.join(d, "rg")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return _vet_binary(ctx, cand), entries
    raise ToolError(
        "ripgrep (rg) is not installed or not on PATH, so grep is unavailable; glob and list_dir still work",
        code="rg_not_found",
    )


# ---------------------------------------------------------------------------
# grep: reading and filtering the output
# ---------------------------------------------------------------------------
_RECORD = re.compile(rb"^(\d+)([:-])(.*)$", re.S)


class _Collector:
    """Turns ripgrep's ``path NUL line SEP text`` records into output lines,
    dropping protected files and enforcing the match and byte caps."""

    def __init__(self, ctx: ToolContext, max_matches: int) -> None:
        self.ctx = ctx
        self.max_matches = max_matches
        self.out_cap = ctx.caps.grep_output_bytes
        self.lines: list[str] = []
        self.size = 0
        self.matches = 0
        self.records = 0
        self.pending_sep = False
        self.poisoned = False
        self.stop: str | None = None  # "matches" | "bytes"

    def _emit(self, text: str) -> bool:
        cost = len(text.encode("utf-8", "replace")) + 1
        if self.size + cost > self.out_cap:
            self.stop = "bytes"
            return False
        self.lines.append(text)
        self.size += cost
        return True

    def feed(self, raw: bytes) -> bool:
        """Handle one output line.  False means: stop reading, the caps are hit."""
        if raw == b"--":
            self.pending_sep = True
            return True
        nul = raw.find(b"\0")
        m = _RECORD.match(raw[nul + 1:]) if nul >= 0 else None
        if m is None:
            # Not a record.  A file name containing a newline splits into such
            # fragments; the first starts with "./".  The record that follows
            # carries the tail of that name, which is not a trustworthy path.
            if raw.startswith(b"./") or raw == b".":
                self.poisoned = True
            return True
        if self.poisoned:
            self.poisoned = False
            return True
        self.records += 1
        path = os.fsdecode(raw[:nul])
        if not path.startswith("./"):
            return True
        parts = tuple(p for p in path.split("/") if p not in ("", "."))
        if not parts or ".." in parts or self.ctx.denylist.blocks(parts):
            return True
        is_match = m.group(2) == b":"
        if is_match and self.matches >= self.max_matches:
            self.stop = "matches"
            return False
        text = m.group(3).decode("utf-8", "replace")
        limit = self.ctx.caps.max_line_chars
        if len(text) > limit:
            text = text[:limit] + "... [line truncated]"
        sep_char = ":" if is_match else "-"
        line = f"{_safe_text('/'.join(parts))}{sep_char}{int(m.group(1))}{sep_char}{text}"
        if self.pending_sep and self.lines:
            if not self._emit("--"):
                return False
        self.pending_sep = False
        if not self._emit(line):
            return False
        if is_match:
            self.matches += 1
        return True


def _drain(stream, sink: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            if len(sink) < _STDERR_KEEP:
                sink.extend(chunk[: _STDERR_KEEP - len(sink)])
    except (OSError, ValueError):
        return


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _stderr_summary(ctx: ToolContext, raw: bytes) -> str:
    """The first few stderr lines, minus any that name a protected path."""
    kept: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        tokens = tuple(t for t in re.split(r"[\s/:'\"()]+", line) if t)
        if any(ctx.denylist.blocks((t,)) for t in tokens) or ctx.denylist.blocks(tokens):
            kept.append("[line omitted]")
        else:
            kept.append(_safe_text(line)[:300])
        if len(kept) >= 5:
            break
    return "\n".join(kept)


def _run_rg(ctx: ToolContext, argv: list[str], path_entries: list[str], col: _Collector) -> tuple[str, int | None, bytes]:
    """Start ripgrep and feed its output to the collector.

    Returns ``(how, returncode, stderr)`` where ``how`` is one of "eof",
    "cap" (a collector cap), "raw" (raw-bytes cap) or "timeout".
    """
    caps = ctx.caps
    env = {"PATH": os.pathsep.join(path_entries)}
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(ctx.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise ToolError("ripgrep (rg) could not be started", code="rg_not_found") from None
    err = bytearray()
    drainer = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
    drainer.start()
    how = "eof"
    try:
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + caps.grep_timeout_s
        buf = bytearray()
        raw_total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                how = "timeout"
                break
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
            if not ready:
                continue
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            raw_total += len(chunk)
            buf.extend(chunk)
            if b"\n" in chunk:
                *lines, rest = bytes(buf).split(b"\n")
                buf = bytearray(rest)
                stopped = False
                for ln in lines:
                    if not col.feed(ln):
                        stopped = True
                        break
                if stopped:
                    how = "cap"
                    break
            if raw_total > caps.grep_max_raw_bytes:
                how = "raw"
                break
        if how == "eof" and buf:
            col.feed(bytes(buf))
            if col.stop:
                how = "cap"
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        drainer.join(timeout=1)
    return how, proc.returncode, bytes(err)


@_guarded
def grep(
    ctx: ToolContext,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    context_lines: int = 0,
    case_insensitive: bool = False,
    max_matches: int = 200,
) -> tuple[str, bool]:
    if not isinstance(pattern, str) or pattern == "":
        raise _bad("pattern must be a non-empty string")
    if "\0" in pattern:
        raise _bad("pattern contains a NUL byte")
    if len(pattern) > MAX_GREP_PATTERN_CHARS:
        raise _bad(f"pattern is longer than {MAX_GREP_PATTERN_CHARS} characters")
    if glob is not None:
        if not isinstance(glob, str) or glob == "":
            raise _bad("glob must be a non-empty string")
        if "\0" in glob:
            raise _bad("glob contains a NUL byte")
        if len(glob) > MAX_GREP_GLOB_CHARS:
            raise _bad(f"glob is longer than {MAX_GREP_GLOB_CHARS} characters")

    res = resolve_for_read(ctx.root, "." if path is None else path, ctx.denylist)
    if res.rel:  # the target must exist, and be a file or a directory
        pfd = open_dir_chain(ctx.root, res.rel[:-1])
        try:
            st = os.lstat(res.rel[-1], dir_fd=pfd)
        finally:
            os.close(pfd)
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise ToolError("path is not a file or a directory", code="not_a_file")
    search_arg = "./" + "/".join(res.rel) if res.rel else "."

    rg, path_entries = _find_rg(ctx)
    argv = build_rg_argv(
        rg,
        pattern=pattern,
        search_arg=search_arg,
        ignore_case=bool(case_insensitive),
        context_lines=context_lines,
        glob=glob,
        max_matches=max_matches,
    )
    col = _Collector(ctx, max_matches)
    how, rc, err = _run_rg(ctx, argv, path_entries, col)

    if how == "timeout" and not col.lines:
        raise ToolError(
            f"the search did not finish within {ctx.caps.grep_timeout_s:g} s; narrow it with path or glob",
            code="timeout",
        )
    failed = how == "eof" and rc not in (0, 1)
    if failed and col.records == 0:
        detail = _stderr_summary(ctx, err)
        msg = "search failed" + (f": {detail}" if detail else f" (ripgrep exit code {rc})")
        raise ToolError(msg, code="search_failed")

    lines = list(col.lines)
    truncated = False
    if col.stop == "matches":
        lines.append(
            f"[truncated: showing the first {max_matches} matches; narrow the search with path or glob, or raise max_matches]"
        )
        truncated = True
    elif col.stop == "bytes":
        lines.append(
            f"[truncated: output reached {ctx.caps.grep_output_bytes} bytes after {col.matches} matches; narrow the search with path or glob]"
        )
        truncated = True
    elif how == "raw":
        lines.append("[truncated: the search produced more output than can be read; narrow it with path or glob]")
        truncated = True
    elif how == "timeout":
        lines.append(
            f"[truncated: stopped at the time limit of {ctx.caps.grep_timeout_s:g} s; these are partial results]"
        )
        truncated = True
    if failed:
        lines.append("[note: some files or directories could not be searched]")
    if not lines:
        return "[no matches]", False
    if not col.lines and not truncated:
        lines.insert(0, "[no matches]")
    return "\n".join(lines), truncated
