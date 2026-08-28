"""
eo/introspection.py — Patch B4 (CLI-as-Internal-Interface plan, §3.3
"the fallback path").

Three plain, read-only functions over the backend's own codebase:
list_directory(path), read_file(path), search_text(pattern, root).
Plain Python only — os.walk/os.listdir/os.scandir for traversal,
open() for reads, re.search for matching. No subprocess, no shell, no
way for a pattern or path argument to cause anything to execute.

This is explicitly the FALLBACK path (§3.3): an agent facing an
unfamiliar request should check eo/capabilities.py's list_capabilities()
/ capabilities_for_role() (Patch B1/B3) first — the targeted,
already-curated answer — and only reach for one of these three
functions when that comes back empty or insufficient. Patch B5b wires
that fallback ordering into the actual agent call path; this patch
only builds the three functions themselves and registers them on
eo/capabilities.py's shared surface, per that module's own docstring
("so they're discoverable through the same shared surface as
everything else").

Every function calls eo/redaction_guard.py's is_readable() (Patch B2)
before returning any content, directory entry, or search hit — that
module's docstring explains why this is a hard-coded check with zero
dependency on the (model-writable) capability-entries data, and this
module is that check's primary caller. A denied path never raises; it
comes back as {"readable": False, ...} with no further detail, so a
rejection can't be used to infer *why* something is hidden (a
distinguishable "exists but denied" vs. "doesn't exist" error message
would itself leak information a redaction is meant to withhold).

Return shape, consistently across all three functions:
    {"readable": bool, ...the function's own keys..., "error": str|None}

`readable=False` means redaction_guard denied the top-level path/root
argument itself — nothing below it was touched at all.
`error` is set for an ordinary operational problem (not found, not a
directory/file, unreadable encoding, bad regex) on a path that DID
clear the readability gate — these are safe to describe plainly, since
they carry no information about what a redaction is protecting.
"""
from __future__ import annotations

import os
import re

from eo.redaction_guard import is_readable

# Read-size and match-count caps -- generous enough for real
# introspection use (a role brief, a module file, a handful of search
# hits), small enough that a single tool call can't balloon a context
# window or a Pusher event payload the way local_workspace_tools.py's
# own _EVENT_FIELD_PREVIEW_CHARS notes an unbounded file/command
# payload would. Truncates rather than errors, same "still return
# something useful" posture as that module's own truncate-for-display
# helper.
MAX_READ_BYTES = 200_000
MAX_SEARCH_MATCHES = 200
_SEARCH_LINE_PREVIEW_CHARS = 200


def list_directory(path: str) -> dict:
    """Non-recursive listing of one directory. Each entry additionally
    carries its OWN readable flag (via is_readable() on that entry's
    full path) — a directory being listable doesn't automatically mean
    every file inside it is readable (a secret-named file can sit
    right next to ordinary ones in an otherwise-allowed directory); the
    caller finds that out per-entry here rather than only on a later
    failed read_file() call.

    Entry *names* are shown even for entries that aren't themselves
    readable — knowing a file named `.env` exists in a directory is not
    the same exposure as reading its contents, and hiding the entry
    entirely would make a directory listing an inconsistent, silently
    incomplete view for no real security benefit.
    """
    if not is_readable(path):
        return {"readable": False, "path": path, "entries": None, "error": None}

    resolved = os.path.abspath(os.path.realpath(path))
    if not os.path.isdir(resolved):
        return {"readable": True, "path": path, "entries": None,
                "error": "not_a_directory"}

    try:
        names = sorted(os.listdir(resolved))
    except OSError:
        return {"readable": True, "path": path, "entries": None,
                "error": "list_failed"}

    entries = []
    for name in names:
        full = os.path.join(resolved, name)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(full) else "file",
            "readable": is_readable(full),
        })
    return {"readable": True, "path": path, "entries": entries, "error": None}


def read_file(path: str) -> dict:
    """Reads one file's full text content, up to MAX_READ_BYTES.
    Binary/non-UTF-8 content comes back as error="not_text" rather
    than raw bytes or a decode exception — this function's whole
    contract is "text an agent can read," not a general file-transfer
    tool."""
    if not is_readable(path):
        return {"readable": False, "path": path, "content": None,
                "truncated": False, "error": None}

    resolved = os.path.abspath(os.path.realpath(path))
    if not os.path.isfile(resolved):
        return {"readable": True, "path": path, "content": None,
                "truncated": False, "error": "not_a_file"}

    try:
        with open(resolved, "rb") as f:
            raw = f.read(MAX_READ_BYTES + 1)
    except OSError:
        return {"readable": True, "path": path, "content": None,
                "truncated": False, "error": "read_failed"}

    truncated = len(raw) > MAX_READ_BYTES
    raw = raw[:MAX_READ_BYTES]
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"readable": True, "path": path, "content": None,
                "truncated": False, "error": "not_text"}

    return {"readable": True, "path": path, "content": content,
            "truncated": truncated, "error": None}


def search_text(pattern: str, root: str) -> dict:
    """Regex search across every file under `root`, walked with
    os.walk(). `root` itself gates the whole call via is_readable() —
    a denied root returns immediately with nothing walked. Below that,
    each individual FILE encountered during the walk is independently
    checked with is_readable() too (root being allowed doesn't make
    every file under it readable, same reasoning as list_directory()'s
    per-entry check) — a denied file is silently skipped, not reported
    as an error, since a growing "skipped: n files" count would itself
    leak how much redacted content exists under a given root.

    Caps out at MAX_SEARCH_MATCHES hits (truncated=True when it does)
    and previews each matched line to _SEARCH_LINE_PREVIEW_CHARS,
    rather than returning unbounded full-line content for a broad
    pattern over a large tree."""
    if not is_readable(root):
        return {"readable": False, "root": root, "matches": None,
                "truncated": False, "error": None}

    try:
        compiled = re.compile(pattern)
    except re.error:
        return {"readable": True, "root": root, "matches": None,
                "truncated": False, "error": "invalid_pattern"}

    resolved_root = os.path.abspath(os.path.realpath(root))
    matches: list[dict] = []
    truncated = False

    for dirpath, _dirnames, filenames in os.walk(resolved_root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if not is_readable(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, start=1):
                        if compiled.search(line):
                            preview = line.strip()[:_SEARCH_LINE_PREVIEW_CHARS]
                            matches.append({"path": full, "line": lineno, "text": preview})
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        if truncated:
            break

    return {"readable": True, "root": root, "matches": matches,
            "truncated": truncated, "error": None}
