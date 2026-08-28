"""
eo/data_store.py — Patch C1 (MiniMe-Patch-Series-C-Plan.md, Track 2):
section-addressable storage for per-task artifact data.

Same shape discipline as eo/secondary_data.py (one JSON document per
key, a lock around read-modify-write) but generalized to arbitrary
free-form text sections instead of that module's topic-tree schema.
This is a second, parallel primitive, not a replacement for
secondary_data.py's JSON-Patch mechanism — that stays exactly as-is
for topic-tree data.

Storage: one memory.bus document per session_id, key
f"artifact:{session_id}" — deliberately the SAME namespacing behavior
(app_slug-scoped, not exempted in memory/bus.py's _namespaced()) as
the stage_output:{session_id}:{role} keys this module's write_section()
is meant to replace (see C3), rather than a new scoping rule of its
own. Document shape:

    {section_id: {"text": str, "written_by": role, "version": int}}

Locked around every read-modify-write with a module-level
threading.Lock, same granularity secondary_data.py's apply_patch()
uses — this prevents two in-process writers from corrupting the same
document, but does NOT prevent two sequential locked writes (e.g. two
separate task processes, or two separate calls that each acquire and
release the lock in turn) from silently clobbering each other. That's
what patch_section()'s optional `expected_version` is for — see its
own docstring.

Line-number addressing is deliberately not offered anywhere in this
module: line numbers drift the moment any other patch lands before
this one is applied, which defeats the point of letting stacked/
parallel agent patches target the same store. Snippet identity
(patch_section's old_snippet/new_snippet pair) is the only addressing
scheme used here that survives concurrent edits.

Every function here is deterministic, in-process Python — no LLM call,
no subprocess, no HTTP. This is "Mode C" in the plan's cost-regime
split; Mode B (an LLM deciding which section is relevant) is built on
top of this module by a later piece of Track 2, not inside it.
"""
from __future__ import annotations

import re
import threading

from eo.introspection import MAX_READ_BYTES, MAX_SEARCH_MATCHES
from memory.bus import read as _bus_read
from memory.bus import write as _bus_write

_lock = threading.Lock()

# Match-preview cap for search_sections() — same reasoning as
# eo/introspection.py's own _SEARCH_LINE_PREVIEW_CHARS (not imported
# directly since that name is module-private there): a single broad
# pattern shouldn't be able to balloon a search response with full,
# unbounded line content.
_SEARCH_LINE_PREVIEW_CHARS = 200


class VersionConflict(Exception):
    """Raised by patch_section() when the caller passed
    expected_version and the section's current stored version doesn't
    match it — see patch_section()'s own docstring."""

    def __init__(self, session_id: str, section_id: str,
                 expected_version: int, actual_version: int):
        self.session_id = session_id
        self.section_id = section_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"version conflict on section {section_id!r} "
            f"(session {session_id!r}): expected version "
            f"{expected_version}, found {actual_version}"
        )


def _key(session_id: str) -> str:
    return f"artifact:{session_id}"


def _read_doc(session_id: str) -> dict:
    return _bus_read(_key(session_id), default={}) or {}


def _write_doc(session_id: str, doc: dict) -> None:
    _bus_write(_key(session_id), doc)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _require_session_id(session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id is required")


def _require_section_id(section_id: str) -> None:
    if not section_id:
        raise ValueError("section_id is required")


def list_sections(session_id: str) -> list[dict]:
    """Table of contents only: [{"section_id", "written_by", "byte_len",
    "version"}, ...], in no particular guaranteed order beyond the
    document's own dict insertion order. No text — this is what a Mode
    B relevance role reads first, and what any other reviewer sees
    before deciding whether it needs a section's full text at all.
    Returns [] for a session with no artifact document yet, same "don't
    persist on read, don't error on absence" posture
    secondary_data.get_secondary_data() takes for an unseen workspace.
    """
    _require_session_id(session_id)
    doc = _read_doc(session_id)
    return [
        {
            "section_id": section_id,
            "written_by": entry.get("written_by"),
            "byte_len": _byte_len(entry.get("text", "")),
            "version": entry.get("version", 0),
        }
        for section_id, entry in doc.items()
    ]


def read_section(session_id: str, section_id: str) -> str:
    """One section's text, capped at MAX_READ_BYTES (reused from
    eo/introspection.py — one shared discipline for "how much raw text
    a single call can return," not a second number for the same job).
    Fails loud on a section that doesn't exist — same posture as
    patch_section()'s snippet matching below, and the opposite of
    read_file()'s own "denial comes back as data, not an exception"
    contract, since there's no redaction concern here to distinguish
    "exists but hidden" from "doesn't exist" for. Truncation is silent
    (matches introspection.read_file()'s own truncate-rather-than-error
    posture for the same cap) — a caller that needs to know whether it
    was truncated can compare the returned length against
    list_sections()'s byte_len for this section_id.
    """
    _require_session_id(session_id)
    _require_section_id(section_id)
    doc = _read_doc(session_id)
    entry = doc.get(section_id)
    if entry is None:
        raise KeyError(
            f"section {section_id!r} not found for session {session_id!r}"
        )
    text = entry.get("text", "")
    raw = text.encode("utf-8")
    if len(raw) <= MAX_READ_BYTES:
        return text
    # Truncate on a byte boundary, then decode leniently — the same
    # trade-off eo/introspection.py's read_file() makes (whole file
    # capped in bytes, not characters), but applied to a str already
    # in memory rather than bytes freshly read off disk, so a trailing
    # partial multi-byte character is dropped rather than raised on.
    return raw[:MAX_READ_BYTES].decode("utf-8", errors="ignore")


def search_sections(session_id: str, pattern: str) -> list[dict]:
    """Regex search across every section's text for this session.
    Returns [{"section_id", "match"}, ...] — one entry per matching
    line, each match previewed to _SEARCH_LINE_PREVIEW_CHARS, capped at
    MAX_SEARCH_MATCHES total hits (reused from eo/introspection.py,
    same reasoning as read_section()'s MAX_READ_BYTES reuse above).
    Iterates sections in the document's own order and stops the moment
    the cap is hit, so which sections get represented in a truncated
    result is deterministic given the document's current shape, not an
    arbitrary cutoff. Raises ValueError for an invalid regex — fails
    loud rather than silently returning no matches for a caller typo.
    """
    _require_session_id(session_id)
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc

    doc = _read_doc(session_id)
    matches: list[dict] = []
    for section_id, entry in doc.items():
        text = entry.get("text", "")
        for line in text.splitlines():
            if compiled.search(line):
                preview = line.strip()[:_SEARCH_LINE_PREVIEW_CHARS]
                matches.append({"section_id": section_id, "match": preview})
                if len(matches) >= MAX_SEARCH_MATCHES:
                    return matches
    return matches


def write_section(session_id: str, section_id: str, text: str,
                   author_role: str) -> dict:
    """Full write of one section — replaces its text entirely and bumps
    its version (1 for a brand-new section, otherwise the previous
    version + 1). This is what an agent finishing a role calls instead
    of the current full-blob stage_output:{session_id}:{role} write
    (see C3 for the call-site change) — write_section() is scoped to
    one named section of a session's artifact document rather than one
    role's entire output.

    Returns the resulting section's list_sections()-shaped entry
    (section_id/written_by/byte_len/version) so a caller can act on the
    new state without a separate read.
    """
    _require_session_id(session_id)
    _require_section_id(section_id)
    if text is None:
        raise ValueError("text is required")
    if not author_role:
        raise ValueError("author_role is required")

    with _lock:
        doc = _read_doc(session_id)
        existing = doc.get(section_id)
        version = (existing.get("version", 0) + 1) if existing else 1
        doc[section_id] = {"text": text, "written_by": author_role, "version": version}
        _write_doc(session_id, doc)

    return {
        "section_id": section_id,
        "written_by": author_role,
        "byte_len": _byte_len(text),
        "version": version,
    }


def patch_section(session_id: str, section_id: str, old_snippet: str,
                   new_snippet: str, expected_version: int | None = None
                   ) -> dict:
    """Snippet-match patch onto one EXISTING section, same discipline as
    the str_replace tool: `old_snippet` must appear in the section's
    current text exactly once. Raises ValueError if the section doesn't
    exist, if old_snippet isn't found in its text, or if old_snippet
    isn't unique within it — never guesses which occurrence was meant.
    Bumps the section's version by 1 on success and preserves its
    existing `written_by` (patch_section doesn't change authorship,
    only content — unlike write_section, which always takes a fresh
    author_role).

    expected_version: if given, raises VersionConflict when it doesn't
    match the section's CURRENT stored version — the optimistic-
    concurrency check secondary_data.py's apply_patch() does NOT have
    (its lock prevents corruption, not clobbering: two sequential
    locked writes can still silently overwrite each other). Required
    if you actually intend parallel/stacked patches against the same
    section; pass None (the default) for the common case where
    sections are scoped narrowly enough that two callers can't
    plausibly race on the same one.

    Returns the resulting section's list_sections()-shaped entry, same
    as write_section().
    """
    _require_session_id(session_id)
    _require_section_id(section_id)
    if not old_snippet:
        raise ValueError("old_snippet is required")
    if new_snippet is None:
        raise ValueError("new_snippet is required")

    with _lock:
        doc = _read_doc(session_id)
        entry = doc.get(section_id)
        if entry is None:
            raise ValueError(
                f"section {section_id!r} not found for session {session_id!r}"
            )

        current_version = entry.get("version", 0)
        if expected_version is not None and expected_version != current_version:
            raise VersionConflict(session_id, section_id, expected_version, current_version)

        text = entry.get("text", "")
        occurrences = text.count(old_snippet)
        if occurrences == 0:
            raise ValueError(
                f"old_snippet not found in section {section_id!r}"
            )
        if occurrences > 1:
            raise ValueError(
                f"old_snippet is not unique in section {section_id!r} "
                f"({occurrences} occurrences) — patch_section requires "
                f"exactly one match"
            )

        new_text = text.replace(old_snippet, new_snippet, 1)
        new_version = current_version + 1
        doc[section_id] = {
            "text": new_text,
            "written_by": entry.get("written_by"),
            "version": new_version,
        }
        _write_doc(session_id, doc)

    return {
        "section_id": section_id,
        "written_by": doc[section_id]["written_by"],
        "byte_len": _byte_len(new_text),
        "version": new_version,
    }
