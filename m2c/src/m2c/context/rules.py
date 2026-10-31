"""
m2c/src/m2c/context/rules.py -- Patch C12: path-scoped instruction rules.

Where rules live
----------------
    <M2C home>/rules/*.md        global rules      (id ``global/<name>``)
    <repo>/.m2c/rules/*.md       project rules     (id ``project/<name>``)

Only ``*.md`` files directly inside those directories count (no recursion, no
dotfiles).  Global rules come first, then project rules, each group sorted by
file name, so the order is deterministic and more specific rules come later.
Rules are INSTRUCTION text for the model.  They are not permission rules; those
live in the policy engine (C06) and never leave the device.

File format
-----------
A rule may start with a front-matter block listing the paths it applies to:

    ---
    paths:
      - "src/**/*.py"
      - tests/**
    ---
    Prefer dataclasses over dicts for structured data.

A rule without ``paths`` (or without a front-matter block) applies everywhere.
Accepted forms of ``paths``: a block list (indented or not), a flow list
``[a, "b", 'c']`` (commas inside quotes or ``{braces}`` do not split), or one
scalar pattern.  Other keys (``description:`` ...) are accepted and ignored.
There is no YAML dependency: this is a small parser for exactly these shapes.

Malformed front matter is never a crash: that one rule is skipped and a
``bad_front_matter`` note says why (the note never repeats file content).  These
all count as malformed: a block that is never closed, ``paths`` with no
pattern, an empty or over-long (256 characters) pattern, more than 32 patterns,
control characters, a repeated key, an indented line that is not a list item,
an unterminated quote or bracket, and brace groups that expand to more than 32
alternatives.  A rule whose text is empty is skipped with ``empty_rule``.

Globs
-----
Patterns are matched against paths relative to the repository root, written with
``/``.  ``*`` and ``?`` and ``[set]`` stay inside one path segment, ``**`` is any
number of whole segments, ``{a,b}`` is alternatives.  A pattern that contains a
``/`` (other than a trailing one) is anchored at the root; a pattern without
one (``*.py``, ``docs/``) matches at any depth, like a .gitignore line.  A
trailing ``/`` means "everything inside".  Matching is case-sensitive.  A path
with ``..`` segments, or an absolute one, never matches.

The matcher works on path SEGMENTS and does not translate patterns into regular
expressions, so a hostile rule file cannot cause catastrophic backtracking
(``**/**/**/a`` against a deep path is linear), and it is the only code that
interprets a pattern.

Selecting and sending
---------------------
The protocol (run.start.json) carries ``rules`` as ``[{name, text}]``: the rules
the client matched to the files in play.  ``load_rules`` returns every rule with
its ``paths``; ``select_rules(rules, files_in_play, root)`` keeps the
unconditional ones plus those with a glob matching a file in play;
``rules_for_run_start`` produces the wire shape (``paths`` is NOT sent: the
schema is closed).  With no files in play only the unconditional rules apply.

Size limits (UTF-8 bytes)
-------------------------
A rule file over ``max_rule_bytes`` (16 KiB) is cut on a line boundary with a
marker (``truncated`` note).  If the rules exceed ``max_total_bytes`` (60 000)
or ``max_rules`` (64: the protocol's ``maxItems``), rules are dropped from the
FRONT of the order first (global before project; inside a group the earliest
name first), each with a ``total_cap`` note.

Place this file at: m2c/src/m2c/context/rules.py
"""
from __future__ import annotations

import fnmatch
import functools
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from os import PathLike

from .instructions import (
    NOTE_TOTAL_CAP,
    NOTE_TRUNCATED,
    REASONS,
    ContextNote,
    ContextSkip,
    classify_escape,
    is_inside,
    nbytes,
    read_text_capped,
    real_path,
    resolve_home,
    safe_label,
)

MAX_RULE_BYTES = 16 * 1024
MAX_RULES_TOTAL_BYTES = 60_000
MAX_RULES = 64                   # protocol/v1/run.start.json: rules.maxItems
MAX_GLOBS = 32
MAX_GLOB_LENGTH = 256
MAX_BRACE_EXPANSIONS = 32
MAX_PATH_SEGMENTS = 256
MAX_ID_LENGTH = 128              # protocol/v1/run.start.json: rules[].name.maxLength

NOTE_BAD_FRONT_MATTER = "bad_front_matter"
NOTE_EMPTY_RULE = "empty_rule"


class FrontMatterError(ValueError):
    """The front matter is malformed.  The message is fixed text, never file content."""


@dataclass(frozen=True)
class Rule:
    id: str                    # "global/<name>" or "project/<name>"
    paths: tuple[str, ...]     # globs; () means the rule applies everywhere
    text: str
    source: str                # label of the file, relative (never an absolute path)


@dataclass(frozen=True)
class RulesResult:
    rules: tuple[Rule, ...]
    notes: tuple[ContextNote, ...]


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(?:\s+(.*)|\s*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"'):
        if len(value) < 2 or value[-1] != value[0]:
            raise FrontMatterError("a quote is not closed")
        return value[1:-1]
    return value


def _split_flow(inner: str) -> list[str]:
    """Split ``a, "b,c", {d,e}`` on commas outside quotes and braces."""
    items: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    for ch in inner:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if quote:
        raise FrontMatterError("a quote is not closed")
    items.append("".join(current))
    return [item.strip() for item in items] if inner.strip() else []


def _parse_flow_list(value: str) -> list[str]:
    if not value.endswith("]"):
        raise FrontMatterError("a bracketed list is not closed")
    return [_unquote(item) for item in _split_flow(value[1:-1])]


def _parse_block(lines: list[str]) -> dict:
    meta: dict = {}
    current: str | None = None
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(ord(ch) < 32 and ch != "\t" for ch in line):
            raise FrontMatterError("a control character is not allowed")
        if stripped == "-" or stripped.startswith("- "):
            if current is None:
                raise FrontMatterError("a list item has no key")
            meta[current].append(_unquote(stripped[1:]))
            continue
        if line[0] in " \t":
            raise FrontMatterError("unexpected indentation")
        match = _KEY_RE.match(line)
        if not match:
            raise FrontMatterError("a line is not 'key: value'")
        key, value = match.group(1), (match.group(2) or "").strip()
        if key in meta:
            raise FrontMatterError("a key is repeated")
        if value == "":
            meta[key] = []
            current = key
        elif value.startswith("["):
            meta[key] = _parse_flow_list(value)
            current = None
        else:
            meta[key] = _unquote(value)
            current = None
    return meta


def _find_group(text: str) -> tuple[int, int, list[str]] | None:
    """The first ``{a,b}`` group (with at least one comma) in ``text``."""
    start = 0
    while True:
        a = text.find("{", start)
        if a < 0:
            return None
        depth = 0
        b = -1
        for i in range(a, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    b = i
                    break
        if b < 0:
            return None
        alternatives: list[str] = []
        depth = 0
        current = a + 1
        for i in range(a + 1, b):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            elif text[i] == "," and depth == 0:
                alternatives.append(text[current:i])
                current = i + 1
        alternatives.append(text[current:b])
        if len(alternatives) >= 2:
            return a, b, alternatives
        start = a + 1


def _expand_braces(pattern: str) -> list[str]:
    """Every alternative of a pattern with ``{a,b}`` groups; ValueError if more
    than MAX_BRACE_EXPANSIONS."""
    done: list[str] = []
    work = [pattern]
    while work:
        item = work.pop()
        group = _find_group(item)
        if group is None:
            done.append(item)
        else:
            a, b, alternatives = group
            work.extend(item[:a] + alt + item[b + 1:] for alt in alternatives)
        if len(done) + len(work) > MAX_BRACE_EXPANSIONS:
            raise ValueError("too many brace alternatives")
    return done


def _check_globs(value) -> list[str]:
    patterns = [value] if isinstance(value, str) else value
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise FrontMatterError("paths must be a list of patterns")
    if not patterns:
        raise FrontMatterError("paths lists no pattern")
    if len(patterns) > MAX_GLOBS:
        raise FrontMatterError("paths lists too many patterns")
    checked = []
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            raise FrontMatterError("a pattern is empty")
        if len(pattern) > MAX_GLOB_LENGTH:
            raise FrontMatterError("a pattern is too long")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in pattern):
            raise FrontMatterError("a control character is not allowed")
        try:
            _expand_braces(pattern)
        except ValueError:
            raise FrontMatterError("a pattern has too many brace alternatives") from None
        checked.append(pattern)
    return checked


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split ``text`` into ``(metadata, body)``.

    Text that does not start with a ``---`` line has no front matter: the
    metadata is ``{}`` and the body is the whole text.  ``metadata['paths']``,
    when present, is a validated list of patterns.  Raises FrontMatterError
    (fixed message, no file content) for anything malformed.
    """
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "...")), None)
    if end is None:
        raise FrontMatterError("the front matter block is not closed")
    meta = _parse_block(lines[1:end])
    if "paths" in meta:
        meta["paths"] = _check_globs(meta["paths"])
    return meta, "\n".join(lines[end + 1:])


# ---------------------------------------------------------------------------
# Glob matching (segment based; no regular expressions)
# ---------------------------------------------------------------------------
def _segments(pattern: str) -> tuple[str, ...]:
    p = pattern.strip()
    p = p.removeprefix("./")
    anchored = p.startswith("/")
    if anchored:
        p = p[1:]
    directory = p.endswith("/")
    core = p.rstrip("/")
    anchored = anchored or "/" in core
    segments = [s for s in core.split("/") if s not in ("", ".")]
    if not segments:
        return ()
    if directory:
        segments.append("**")
    if not anchored and segments[0] != "**":
        segments.insert(0, "**")
    return tuple(segments)


@functools.lru_cache(maxsize=2048)
def _compile(pattern: str) -> tuple[tuple[str, ...], ...]:
    try:
        expanded = _expand_braces(pattern)
    except ValueError:
        return ()
    return tuple(seg for seg in (_segments(p) for p in expanded) if seg)


def _match_segments(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    count = len(parts)
    reachable = {0}
    for index, segment in enumerate(pattern):
        if segment == "**":
            lowest = min(reachable)
            lowest = lowest + 1 if index == len(pattern) - 1 else lowest   # a trailing ** needs one segment
            reachable = set(range(lowest, count + 1))
        else:
            reachable = {j + 1 for j in reachable if j < count and fnmatch.fnmatchcase(parts[j], segment)}
        if not reachable:
            return False
    return count in reachable


def glob_match(pattern: str, rel_path: str) -> bool:
    """Does ``pattern`` match the root-relative, ``/``-separated ``rel_path``?"""
    if not rel_path or rel_path.startswith("/"):
        return False
    parts = tuple(p for p in rel_path.split("/") if p != ".")
    if not parts or len(parts) > MAX_PATH_SEGMENTS or any(p in ("", "..") for p in parts):
        return False
    return any(_match_segments(segments, parts) for segments in _compile(pattern))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _rule_id(scope: str, stem: str) -> str:
    cleaned = "".join(ch if ch.isprintable() and ch != "/" else "_" for ch in stem) or "rule"
    return f"{scope}/{cleaned}"[:MAX_ID_LENGTH]


def _load_directory(directory: str, base_real: str, scope: str, label_prefix: str, max_rule_bytes: int,
                    notes: list[ContextNote]) -> list[Rule]:
    if not os.path.lexists(directory):
        return []
    label_dir = label_prefix.rstrip("/")
    real_dir = os.path.realpath(directory)
    escape = classify_escape(directory, real_dir, base_real)
    if escape:
        notes.append(ContextNote(escape, label_dir, f"Skipped {safe_label(label_dir)}: {REASONS[escape]}."))
        return []
    try:
        names = sorted(os.listdir(real_dir))
    except OSError:
        return []
    found: list[Rule] = []
    for name in names:
        if name.startswith(".") or not name.lower().endswith(".md"):
            continue
        candidate = os.path.join(real_dir, name)
        if os.path.isdir(candidate):
            continue
        label = label_prefix + name
        real = os.path.realpath(candidate)
        escape = classify_escape(candidate, real, base_real)
        if escape:
            notes.append(ContextNote(escape, label, f"Skipped {safe_label(label)}: {REASONS[escape]}."))
            continue
        try:
            text, truncated = read_text_capped(real, max_rule_bytes)
        except ContextSkip as exc:
            notes.append(ContextNote(exc.code, label, f"Skipped {safe_label(label)}: {REASONS[exc.code]}."))
            continue
        try:
            meta, body = parse_front_matter(text)
        except FrontMatterError as exc:
            notes.append(ContextNote(NOTE_BAD_FRONT_MATTER, label,
                                     f"Skipped {safe_label(label)}: invalid front matter ({exc})."))
            continue
        body = body.strip()
        if not body:
            notes.append(ContextNote(NOTE_EMPTY_RULE, label, f"Skipped {safe_label(label)}: the rule has no text."))
            continue
        if truncated:
            body += (f"\n<!-- m2c: truncated: {safe_label(label)} is larger than {max_rule_bytes} bytes; "
                     f"the rest was left out -->")
            notes.append(ContextNote(NOTE_TRUNCATED, label,
                                     f"{safe_label(label)} is larger than {max_rule_bytes} bytes; the rest was left out."))
        stem = name[:-3]
        found.append(Rule(id=_rule_id(scope, stem), paths=tuple(meta.get("paths", ())), text=body, source=label))
    return found


def load_rules(
    root: str | PathLike[str],
    home: str | PathLike[str] | None = None,
    *,
    max_rule_bytes: int = MAX_RULE_BYTES,
    max_total_bytes: int = MAX_RULES_TOTAL_BYTES,
    max_rules: int = MAX_RULES,
) -> RulesResult:
    """Load every rule (global first, then project) with its ``paths``.

    Missing directories are not errors.  ``home`` None means ``paths.config_dir()``
    (M2C_HOME honoured; ConfigDirError for a bad value).
    """
    real_root = real_path(root)
    global_dir = real_path(resolve_home(home))
    notes: list[ContextNote] = []
    rules = _load_directory(os.path.join(global_dir, "rules"), global_dir, "global", "global/rules/",
                            max_rule_bytes, notes)
    rules += _load_directory(os.path.join(real_root, ".m2c", "rules"), real_root, "project", ".m2c/rules/",
                             max_rule_bytes, notes)
    while rules and (len(rules) > max_rules or sum(nbytes(r.text) for r in rules) > max_total_bytes):
        gone = rules.pop(0)
        notes.append(ContextNote(NOTE_TOTAL_CAP, gone.source,
                                 f"{safe_label(gone.source)} was left out: the rules exceed the size limit."))
    return RulesResult(rules=tuple(rules), notes=tuple(notes))


# ---------------------------------------------------------------------------
# Selecting and sending
# ---------------------------------------------------------------------------
def _relative_paths(files: Iterable[str | PathLike[str]], root: str | PathLike[str] | None) -> list[str]:
    roots = []
    if root is not None:
        roots = [os.path.normpath(os.path.abspath(os.fspath(root))), real_path(root)]
    out = []
    for item in files:
        text = os.path.normpath(os.fspath(item))
        if os.path.isabs(text):
            base = next((r for r in roots if is_inside(text, r)), None)
            if base is None:
                continue
            text = os.path.relpath(text, base)
        if text == "." or text == ".." or text.startswith(".." + os.sep):
            continue
        out.append(text.replace(os.sep, "/"))
    return out


def select_rules(rules: Iterable[Rule], files_in_play: Iterable[str | PathLike[str]],
                 root: str | PathLike[str] | None = None) -> list[Rule]:
    """The rules that apply: those without ``paths``, plus those with a glob
    matching at least one of ``files_in_play`` (paths relative to ``root``, or
    absolute and inside it).  Order is preserved."""
    files = _relative_paths(files_in_play, root)
    return [r for r in rules if not r.paths or any(glob_match(g, f) for g in r.paths for f in files)]


def rules_for_run_start(rules: Iterable[Rule]) -> list[dict[str, str]]:
    """The wire shape of run.start ``rules``: exactly ``{name, text}`` (the schema
    is closed, so ``paths`` and ``source`` stay on the device)."""
    return [{"name": r.id[:MAX_ID_LENGTH], "text": r.text} for r in rules]
