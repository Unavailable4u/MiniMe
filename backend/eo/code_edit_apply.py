"""
eo/code_edit_apply.py — W5.2 (Build Workbench plan, step 204): the
deterministic half of the code-edit pipeline. agents/code_editor.py
asks a model for edits in the plan's D3 shape

    {"summary": str, "edits": [{"path", "op": "replace"|"create"|"delete",
                                "search", "replace", "content"}, ...]}

and THIS module — pure functions, no LLM, no database, no I/O of any
kind — turns those edits into per-file `original` / `proposed` text.
The model never produces a whole file to be trusted verbatim; it points
at a `search` block and this module decides, deterministically,
whether and where that block lands.

Matching, in the order the plan asks for (D3 / W5.2 "Build"):

  1. Exact match (after normalising line endings on BOTH sides — see
     "CRLF" below).
  2. Whitespace-normalised, line-based match: every line compared with
     runs of whitespace collapsed and both ends stripped. Blank lines
     at the very start/end of `search` are ignored. When the model
     dropped (or added) a uniform indent, `replace` is re-indented by
     the same delta so the block still lines up with the file.
  3. Otherwise a precise failure (EditFailure.message) naming the
     closest line it could find — written to be pasted straight into
     the retry prompt agents/code_editor.py sends.

A `search` that matches 2+ places is a failure ("ambiguous") UNLESS the
caller passed an EditScope (the ranges the user actually selected) and
exactly one of the candidates falls inside them — that is the "a range
ref disambiguates" rule. Ranges are tracked through the edits already
applied to the same file, so a second edit still sees the right lines.

CRLF: a file stored with CRLF (or lone CR) line endings is matched
through a normalised LF view with an offset map back into the original
text, so `search`/`replace` from the model (always LF) work on it, only
the edited lines change, and text the model wrote is converted to the
file's own dominant line ending on the way in. Untouched lines keep
whatever ending they had — a mixed-ending file is never silently
normalised.

Scope: with an EditScope, every edit is also checked against what the
user referenced. An edit outside the referenced files/ranges is still
APPLIED — it isn't this module's place to refuse it — but the file's
result carries `scope_violation=True` plus human-readable reasons, so
the review UI (W5.3/W5.4) can warn before the user presses Keep.

Nothing here imports eo.db / eo.workspace_code_files on purpose: path
validation is passed in as a `validate_path` callable so this module
stays importable (and testable) with zero infrastructure.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

VALID_OPS = ("replace", "create", "delete")

# Upper bound on how many candidate locations one `search` reports.
# Only ambiguity detection (>= 2) and range narrowing need the list; a
# pathological one-character `search` in a huge file must not build a
# million-entry list first.
MAX_MATCHES = 200

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_WS_RUN = re.compile(r"\s+")
# Our own prompt gutter ("> 12| code" / "  12| code"). Only used to
# phrase a helpful failure message — never to rewrite a search block.
_GUTTER = re.compile(r"^\s*[>*]?\s*\d+\s*[|│]")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Match:
    """One place a `search` block matched. `start`/`end` are offsets into
    the text exactly as stored (line endings and all). Lines are 1-based
    and inclusive; a match that ends with a line break belongs to the
    line that break terminates."""
    start: int
    end: int
    kind: str  # "exact" | "whitespace"
    start_line: int
    end_line: int


@dataclass
class EditFailure:
    index: int  # 0-based position in the `edits` list handed to apply_edits
    path: str | None
    code: str   # invalid_edit | invalid_path | unknown_file | already_exists |
                # not_found | ambiguous | too_large | skipped
    message: str


@dataclass
class FileResult:
    path: str
    op: str  # "replace" | "create" | "delete" — the NET op for this path
    original: str  # "" for a create
    proposed: str  # "" for a delete
    added: int = 0
    removed: int = 0
    match_kinds: list[str] = field(default_factory=list)
    scope_violation: bool = False
    scope_reasons: list[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    files: list[FileResult] = field(default_factory=list)
    failures: list[EditFailure] = field(default_factory=list)
    # Paths whose edits netted out to no change at all (replace X with X).
    unchanged: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_edit_files(self) -> list[dict]:
        """The `{path, op, content}` shape eo/code_proposals.py's
        `generate_edit` contract expects for its `files` list."""
        return [{"path": f.path, "op": f.op, "content": f.proposed} for f in self.files]


@dataclass
class EditScope:
    """What the user referenced. `files` maps path -> None (the whole
    file is in scope) or a list of (from_line, to_line) 1-based inclusive
    ranges (only those lines are)."""
    files: dict[str, list[tuple[int, int]] | None] = field(default_factory=dict)


class _EditProblem(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Line-ending helpers
# ---------------------------------------------------------------------------
def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _count_breaks(text: str) -> int:
    return len(_LINE_BREAK.findall(text))


def _normalize_eol(text: str) -> tuple[str, list[int] | None]:
    """LF-only view of `text` plus a map from every offset in that view
    (including len(view)) back to the matching offset in `text`. `None`
    for the map when nothing needed normalising — the overwhelmingly
    common case, which then costs nothing."""
    if "\r" not in text:
        return text, None
    out: list[str] = []
    omap: list[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        omap.append(i)
        if ch == "\r":
            out.append("\n")
            i += 2 if (i + 1 < n and text[i + 1] == "\n") else 1
        else:
            out.append(ch)
            i += 1
    omap.append(n)
    return "".join(out), omap


def _orig(omap: list[int] | None, idx: int) -> int:
    return idx if omap is None else omap[idx]


def _eol_style(text: str) -> str:
    """The line ending text the model wrote should be converted to:
    whichever ending dominates the file it is being merged into."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf > 0 and crlf > lf and crlf >= cr:
        return "\r\n"
    if cr > 0 and cr > lf and cr > crlf:
        return "\r"
    return "\n"


def _to_style(text_lf: str, style: str) -> str:
    return text_lf if style == "\n" else text_lf.replace("\n", style)


def _ws_norm(line: str) -> str:
    return _WS_RUN.sub(" ", line).strip()


def _terminator_len(text: str, idx: int) -> int:
    """Length of the line terminator starting at text[idx] (0 if none)."""
    if text.startswith("\r\n", idx):
        return 2
    if idx < len(text) and text[idx] in "\r\n":
        return 1
    return 0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _block_lines(search: str) -> tuple[list[str], int, int]:
    """`search` as LF lines with the block-terminating newline dropped and
    blank lines at both edges trimmed. Returns (core_lines, n_lead_blank,
    n_trail_blank) — the counts are needed again to trim `replace`
    symmetrically."""
    lines = _to_lf(search).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lead = 0
    while lead < len(lines) and not lines[lead].strip():
        lead += 1
    lines = lines[lead:]
    trail = 0
    while lines and not lines[-1].strip():
        lines.pop()
        trail += 1
    return lines, lead, trail


def find_matches(text: str, search: str) -> list[Match]:
    """Every place `search` matches in `text`: exact matches if there are
    any (overlapping ones included — two overlapping hits are just as
    ambiguous as two disjoint ones), otherwise whitespace-insensitive
    whole-line matches. [] when neither finds anything or `search` is
    empty. Offsets are into `text` as stored."""
    if not search:
        return []
    norm, omap = _normalize_eol(text)
    needle = _to_lf(search)

    def line_of(idx: int) -> int:
        return norm.count("\n", 0, idx) + 1

    found: list[int] = []
    pos = norm.find(needle)
    while pos != -1 and len(found) < MAX_MATCHES:
        found.append(pos)
        pos = norm.find(needle, pos + 1)
    if found:
        return [
            Match(_orig(omap, s), _orig(omap, s + len(needle)), "exact",
                  line_of(s), line_of(s + len(needle) - 1))
            for s in found
        ]

    core, _lead, _trail = _block_lines(search)
    if not core:
        return []
    key = [_ws_norm(line) for line in core]
    lines = norm.split("\n")
    m = len(key)
    if m > len(lines):
        return []
    fkeys = [_ws_norm(line) for line in lines]
    starts: list[int] = []
    off = 0
    for line in lines:
        starts.append(off)
        off += len(line) + 1
    out: list[Match] = []
    first = key[0]
    for i in range(len(lines) - m + 1):
        if fkeys[i] == first and fkeys[i:i + m] == key:
            j = i + m - 1
            out.append(Match(
                _orig(omap, starts[i]), _orig(omap, starts[j] + len(lines[j])),
                "whitespace", i + 1, j + 1,
            ))
            if len(out) >= MAX_MATCHES:
                break
    return out


def _not_found_message(text: str, search: str) -> str:
    """Precise, model-readable reason a `search` matched nothing: the
    closest line in the file to the search's first non-blank line (by
    ratio), and a note when the search looks like it still carries the
    prompt's line-number gutter."""
    parts = ["the `search` text was not found in the file (neither exactly nor "
             "when ignoring whitespace differences)."]
    core, _l, _t = _block_lines(search)
    if core:
        if all(_GUTTER.match(line) for line in core):
            parts.append("Every line of `search` starts with a line-number gutter "
                         "(like '  12| '). The gutter is NOT part of the file — "
                         "copy only the code after it.")
        first = _ws_norm(core[0])
        if first:
            file_lines = _to_lf(text).split("\n")
            normed = [_ws_norm(line) for line in file_lines]
            close = difflib.get_close_matches(first, [n for n in normed if n], n=1, cutoff=0.6)
            if close:
                idx = normed.index(close[0])
                shown = file_lines[idx].strip()
                if len(shown) > 160:
                    shown = shown[:157] + "..."
                parts.append(f"The closest line to your first `search` line is line "
                             f"{idx + 1}: {shown!r}.")
            else:
                parts.append("Nothing in the file resembles the first line of `search`.")
    parts.append("Copy the text verbatim from the file shown to you.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Scope tracking
# ---------------------------------------------------------------------------
class _ScopeTracker:
    """Mutable copy of an EditScope whose ranges are shifted as edits to
    the same file are applied, so line numbers stay meaningful across a
    multi-edit response."""

    def __init__(self, scope: EditScope | None):
        self.enabled = scope is not None
        self.ranges: dict[str, list[list[int]] | None] = {}
        if scope is not None:
            for path, rs in scope.files.items():
                self.ranges[path] = None if rs is None else [[int(a), int(b)] for a, b in rs]

    def narrow(self, path: str, matches: list[Match]) -> list[Match]:
        """Ambiguity tie-breaker: prefer matches fully inside a referenced
        range, then ones merely overlapping one; if neither helps, hand
        the list back unchanged (still ambiguous)."""
        rs = self.ranges.get(path) if self.enabled else None
        if not rs:
            return matches
        inside = [m for m in matches
                  if any(a <= m.start_line and m.end_line <= b for a, b in rs)]
        if inside:
            return inside
        touching = [m for m in matches
                    if any(a <= m.end_line and m.start_line <= b for a, b in rs)]
        return touching or matches

    def check(self, path: str, start_line: int, end_line: int) -> str | None:
        """None when in scope, else a human-readable reason."""
        if not self.enabled:
            return None
        if path not in self.ranges:
            return f"{path} was not one of the files you referenced"
        rs = self.ranges[path]
        if rs is None:
            return None
        if any(a <= end_line and start_line <= b for a, b in rs):
            return None
        shown = ", ".join(f"{a}-{b}" for a, b in rs)
        where = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
        return f"{path}: {where} is outside the range(s) you referenced ({shown})"

    def shift(self, path: str, start_line: int, end_line: int, delta: int) -> None:
        rs = self.ranges.get(path) if self.enabled else None
        if not rs or delta == 0:
            return
        for r in rs:
            a, b = r
            if b < start_line:
                continue
            if a > end_line:
                r[0], r[1] = a + delta, b + delta
                continue
            r[0] = min(a, start_line)
            r[1] = max(r[0], (b + delta) if b >= end_line else (end_line + delta))


# ---------------------------------------------------------------------------
# Applying one replace
# ---------------------------------------------------------------------------
def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _reindent(rep_lines: list[str], file_lines: list[str], search_lines: list[str]) -> list[str]:
    """When the model quoted a block with a different (but uniformly
    different) indent than the file has, shift `replace` by that same
    delta. Bails out (returns rep_lines untouched) unless EVERY non-blank
    line pair agrees on one delta — a mixed tabs/spaces situation is not
    something to guess at."""
    relation: set[tuple[str, str]] = set()
    for f_line, s_line in zip(file_lines, search_lines):
        if not s_line.strip():
            continue
        fi, si = _indent(f_line), _indent(s_line)
        if fi == si:
            relation.add(("same", ""))
        elif fi.endswith(si):
            relation.add(("add", fi[:len(fi) - len(si)]))
        elif si.endswith(fi):
            relation.add(("strip", si[:len(si) - len(fi)]))
        else:
            return rep_lines
    if len(relation) != 1:
        return rep_lines
    mode, extra = next(iter(relation))
    if mode == "same":
        return rep_lines
    if mode == "add":
        return [(extra + line) if line.strip() else line for line in rep_lines]
    return [line[len(extra):] if line.startswith(extra) else line for line in rep_lines]


def _choose_match(path: str, matches: list[Match], tracker: _ScopeTracker,
                   n: int, text: str, search: str, had_prior: bool) -> Match:
    if not matches:
        raise _EditProblem("not_found", f"edit {n} ({path}): " + _not_found_message(text, search))
    if len(matches) == 1:
        return matches[0]
    narrowed = tracker.narrow(path, matches)
    if len(narrowed) == 1:
        return narrowed[0]
    lines = ", ".join(str(m.start_line) for m in narrowed[:10])
    more = f" (and {len(narrowed) - 10} more)" if len(narrowed) > 10 else ""
    if len(narrowed) < len(matches):
        scope_note = " Even inside the lines you were shown as selected, it still matches"
    else:
        scope_note = " It matches"
    after = (" (line numbers are as of the file after your earlier edits to it were applied)"
             if had_prior else "")
    raise _EditProblem(
        "ambiguous",
        f"edit {n} ({path}): the `search` text is ambiguous.{scope_note} "
        f"{len(narrowed)} places, starting at lines {lines}{more}{after}. "
        "Include more of the surrounding lines so it matches exactly once.",
    )


def _replace_in(text: str, match: Match, search: str, replace: str,
                 style: str) -> tuple[str, int]:
    """Splice `replace` over `match`. Returns (new_text, change in the
    number of line breaks)."""
    rep = _to_lf(replace)
    s_lf = _to_lf(search)
    start, end = match.start, match.end

    if match.kind == "exact":
        # Whole-line repair, both directions: a block that ends in a
        # newline on one side but not the other would otherwise glue two
        # lines together, or leave a stray blank line. Only fires when
        # the match itself is a whole-line block — never mid-line.
        if s_lf.endswith("\n") and rep and not rep.endswith("\n"):
            rep += "\n"
        elif (not s_lf.endswith("\n") and rep.endswith("\n")
              and _terminator_len(text, end) > 0):
            rep = rep[:-1]
        new_piece = _to_style(rep, style)
    else:
        core, lead, trail = _block_lines(search)
        rep_lines = rep.split("\n")
        if rep_lines and rep_lines[-1] == "":
            rep_lines.pop()
        # trim the same number of blank edge lines from `replace` that
        # were ignored on `search`, so a block quoted with a blank line
        # above it doesn't sprout an extra one.
        drop = 0
        while drop < lead and rep_lines and not rep_lines[0].strip():
            rep_lines.pop(0)
            drop += 1
        drop = 0
        while drop < trail and rep_lines and not rep_lines[-1].strip():
            rep_lines.pop()
            drop += 1
        file_lines = _to_lf(text[start:end]).split("\n")
        rep_lines = _reindent(rep_lines, file_lines, core)
        if not rep_lines:
            # deleting whole lines — take their terminator with them
            end += _terminator_len(text, end)
            new_piece = ""
        else:
            new_piece = _to_style("\n".join(rep_lines), style)

    delta = _count_breaks(new_piece) - _count_breaks(text[start:end])
    return text[:start] + new_piece + text[end:], delta


# ---------------------------------------------------------------------------
# Diff stats
# ---------------------------------------------------------------------------
def _diff_lines(text: str) -> list[str]:
    if not text:
        return []
    parts = _LINE_BREAK.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def diff_stats(original: str, proposed: str) -> tuple[int, int]:
    """(added, removed) line counts, stdlib difflib only. Common leading
    and trailing lines are peeled off first, so a small edit in a large
    file never pays for a full SequenceMatcher run."""
    a, b = _diff_lines(original), _diff_lines(proposed)
    lo = 0
    while lo < len(a) and lo < len(b) and a[lo] == b[lo]:
        lo += 1
    hi = 0
    while hi < len(a) - lo and hi < len(b) - lo and a[len(a) - 1 - hi] == b[len(b) - 1 - hi]:
        hi += 1
    a_mid, b_mid = a[lo:len(a) - hi], b[lo:len(b) - hi]
    if not a_mid and not b_mid:
        return 0, 0
    added = removed = 0
    sm = difflib.SequenceMatcher(None, a_mid, b_mid, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def format_failures(failures: Iterable[EditFailure]) -> str:
    return "\n".join(f"- {f.message}" for f in failures)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
@dataclass
class _State:
    path: str
    original: str | None      # None: created inside this batch
    text: str | None          # None: deleted
    style: str = "\n"
    created: bool = False
    deleted: bool = False
    failed: bool = False
    edits_applied: int = 0
    kinds: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _clean_path(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    p = raw.strip()
    while p.startswith("./"):
        p = p[2:]
    return p or None


def apply_edits(
    edits: list[dict],
    files: dict[str, str],
    *,
    scope: EditScope | None = None,
    validate_path: Callable[[str], None] | None = None,
    existing_paths: Iterable[str] | None = None,
    max_file_chars: int | None = None,
) -> ApplyResult:
    """Apply D3 `edits` to `files` and report per-file results.

    files          path -> current text, for every file an edit may
                   replace or delete. A path missing from here is
                   "unknown" to `replace`/`delete`.
    scope          what the user referenced (see EditScope). None turns
                   off both range disambiguation and scope_violation.
    validate_path  called with each edit's path; a ValueError becomes an
                   `invalid_path` failure (agents/code_editor.py passes
                   workspace_code_files._validate_file_path plus its
                   secret-file filter).
    existing_paths every path that already exists in the project, whether
                   or not it is in `files` — so `create` can refuse to
                   clobber a file the model was never shown.
    max_file_chars fail a file whose proposed text is longer than this.

    Edits are applied in order; a later edit to the same path sees the
    earlier ones' result. Every edit is attempted and ALL failures are
    collected (so one retry prompt can carry all of them). Once an edit
    to a path fails, its later edits are reported as `skipped` rather
    than run against a half-applied file, and that path is left out of
    `files` in the result. Paths whose edits net out to no change land in
    `unchanged`.
    """
    result = ApplyResult()
    existing = set(existing_paths or ()) | set(files)
    tracker = _ScopeTracker(scope)
    states: dict[str, _State] = {}
    order: list[str] = []

    def fail(i: int, path: str | None, code: str, message: str):
        result.failures.append(EditFailure(i, path, code, message))
        if path is not None and path in states:
            states[path].failed = True

    def state_for(path: str) -> _State | None:
        st = states.get(path)
        if st is None and path in files:
            st = _State(path=path, original=files[path], text=files[path],
                        style=_eol_style(files[path]))
            states[path] = st
            order.append(path)
        return st

    for i, edit in enumerate(edits):
        n = i + 1
        if not isinstance(edit, dict):
            fail(i, None, "invalid_edit", f"edit {n}: must be an object with a path and an op.")
            continue
        path = _clean_path(edit.get("path"))
        if path is None:
            fail(i, None, "invalid_edit", f"edit {n}: `path` is missing or not a string.")
            continue

        op = edit.get("op")
        if op is None:  # tolerate a missing op when the intent is unambiguous
            op = "replace" if "search" in edit else ("create" if "content" in edit else None)
        if op not in VALID_OPS:
            fail(i, path, "invalid_edit",
                 f"edit {n} ({path}): `op` must be one of {', '.join(VALID_OPS)}, got {op!r}.")
            continue

        if validate_path is not None:
            try:
                validate_path(path)
            except ValueError as exc:
                fail(i, path, "invalid_path", f"edit {n} ({path}): {exc}")
                continue

        prior = states.get(path)
        if prior is not None and prior.failed:
            fail(i, path, "skipped",
                 f"edit {n} ({path}): not attempted because an earlier edit to this "
                 "file failed. Re-send every edit for this file.")
            continue

        if op == "create":
            content = edit.get("content")
            if not isinstance(content, str):
                fail(i, path, "invalid_edit",
                     f"edit {n} ({path}): a create needs a string `content` (use \"\" for an empty file).")
                continue
            st = states.get(path)
            if st is not None and not st.deleted:
                fail(i, path, "already_exists",
                     f"edit {n} ({path}): this file already exists (or was already created in "
                     "this response). Use op \"replace\" with a `search` block to change it.")
                continue
            if st is None and path in existing:
                fail(i, path, "already_exists",
                     f"edit {n} ({path}): this file already exists in the project. Use op "
                     "\"replace\" with a `search` block to change it — only files shown to "
                     "you can be edited.")
                continue
            if st is not None:  # deleted earlier in this batch, now re-created
                st.text, st.deleted = content, False
            else:
                st = _State(path=path, original=None, text=content, created=True)
                states[path] = st
                order.append(path)
            if tracker.enabled and path not in tracker.ranges:
                st.violations.append(f"{path} is a new file you did not reference")
            continue

        # replace / delete both need an existing, not-yet-deleted file
        st = state_for(path)
        if st is None or st.deleted:
            why = "was already deleted earlier in this response" if st is not None else (
                "is not a file that was shown to you (it may not exist, or it was too "
                "large to include)")
            fail(i, path, "unknown_file",
                 f"edit {n} ({path}): this path {why}. You can only replace or delete files "
                 "shown above; use op \"create\" for a new file.")
            continue

        if op == "delete":
            whole = (not tracker.enabled) or (path in tracker.ranges and tracker.ranges[path] is None)
            if not whole:
                st.violations.append(
                    f"{path} is deleted, but you only referenced part of it"
                    if path in tracker.ranges else f"{path} is deleted but was not referenced")
            st.text, st.deleted = None, True
            continue

        # op == "replace"
        search, replace = edit.get("search"), edit.get("replace")
        if not isinstance(search, str) or not search.strip():
            fail(i, path, "invalid_edit",
                 f"edit {n} ({path}): a replace needs a non-empty string `search` block.")
            continue
        if not isinstance(replace, str):
            fail(i, path, "invalid_edit",
                 f"edit {n} ({path}): `replace` must be a string (use \"\" to delete the matched text).")
            continue
        try:
            matches = find_matches(st.text, search)
            match = _choose_match(path, matches, tracker, n, st.text, search,
                                  had_prior=st.edits_applied > 0)
            reason = tracker.check(path, match.start_line, match.end_line)
            new_text, delta = _replace_in(st.text, match, search, replace, st.style)
        except _EditProblem as prob:
            fail(i, path, prob.code, prob.message)
            continue
        if reason is not None:
            st.violations.append(reason)
        tracker.shift(path, match.start_line, match.end_line, delta)
        st.text = new_text
        st.edits_applied += 1
        st.kinds.append(match.kind)

    for path in order:
        st = states[path]
        if st.failed:
            continue
        if st.created and st.deleted:
            continue  # created and removed inside one response: nothing to propose
        if st.deleted:
            op, proposed = "delete", ""
        elif st.created:
            op, proposed = "create", st.text or ""
        else:
            op, proposed = "replace", st.text or ""
        original = st.original or ""
        if op == "replace" and proposed == original:
            result.unchanged.append(path)
            continue
        if max_file_chars is not None and len(proposed) > max_file_chars:
            result.failures.append(EditFailure(
                -1, path, "too_large",
                f"{path}: the edited file would be {len(proposed)} characters, over the "
                f"{max_file_chars} limit. Make a smaller change."))
            continue
        added, removed = diff_stats(original, proposed)
        result.files.append(FileResult(
            path=path, op=op, original=original, proposed=proposed,
            added=added, removed=removed, match_kinds=list(st.kinds),
            scope_violation=bool(st.violations),
            scope_reasons=list(dict.fromkeys(st.violations)),
        ))
    return result
