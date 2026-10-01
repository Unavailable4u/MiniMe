"""
eo/code_proposals.py — W5.1 (Build Workbench plan, step 197): the
pending-review store behind the Build workbench's "select code / Add
to chat / Edit mode" flow (W4.1/W4.2) turning into a Copilot-style
Keep/Undo diff (W5.3/W5.4). See migrations/0010_code_proposals.sql's
own header for the full column-by-column schema rationale.

Lifecycle, in one line: create_proposal() computes an edit and stores
it at status='pending' WITHOUT touching workspace_code_files at all;
resolve_proposal() is the only function in this module (or, as far as
this module is concerned, in this codebase) that ever turns a
proposal's stored `proposed` text into a real
workspace_code_files.write_file()/delete_file() call. Nothing else
here writes a single byte of real file content.

W5.2 seam: agents/code_editor.py + eo/code_edit_apply.py — the real
"LLM proposes a SEARCH/REPLACE edit, apply_edits() turns it into
original/proposed text" pipeline the plan's D2/D3 describe — is now
create_proposal()'s DEFAULT `generate_edit` (see
_default_generate_edit() below). Everything about how a generated edit
turns into a stored proposal row, gets listed, gets fetched, and gets
resolved is unchanged from W5.1. _stub_generate_edit() stays in this
module as the canned, no-LLM generator tests can pass explicitly; it
is no longer the default.

W5.5: a `kind: "folder"` ref no longer 400s. create_proposal() runs
every incoming ref through _expand_refs() (below) BEFORE
_paths_from_refs() ever sees it — a folder ref becomes a size-budgeted
set of `kind: "file"` refs (skip binaries/lockfiles/node_modules/
minified; see _expand_folder()), and what's actually stored in this
proposal's own `refs` column gets each folder ref annotated with
`includedCount`/`totalMatched`/`truncated` so a client can render
"included 14 of 40 files" from the stored proposal alone, without
recomputing it. _paths_from_refs() itself is otherwise untouched: it
still refuses a raw `kind: "folder"` ref outright, which after this
change only happens if some future caller reaches it directly, not via
create_proposal() — see its own docstring.

W8.5: agents/deploy_config_writer.py's plan now enters this same
store through create_deploy_config_proposal() (below) — the deploy
config file becomes an ordinary pending proposal, reviewed with the
identical Keep/Undo flow, and resolve_proposal() stays the only thing
that ever writes it. No new table, status, route or event: the
proposal is tagged `model_meta.generator == "deploy_config_writer"`
and that tag is all that tells it apart from a chat edit.

Same FakeCursor-isolatable, `eo.db`-only-touches-Postgres shape every
other Postgres-backed store module in this package already takes (see
eo/workspace_code_files.py, eo/chat_store.py) — nothing here imports
psycopg directly.
"""
import functools
import hashlib
import os
import re
import uuid
from datetime import UTC, datetime

from eo import db, workspace_code_files
from eo.audit_log import write_audit
from eo.code_element_context import normalize_element_refs
from eo.workspace_code_files import VersionConflictError
from relay.emitter import EventType, emit_workspace_event

# Kept in lockstep BY HAND with migrations/0010_code_proposals.sql's
# CHECK constraint — same small/stable/"not worth a DB-side derivation"
# call workspace_code_files._VALID_VERSION_SOURCES already makes for
# itself, for the identical reason (nothing outside this module ever
# needs to enumerate these).
_VALID_STATUSES = {"pending", "accepted", "rejected", "partial", "stale", "failed"}

# The three ops a proposal's `files` entries — and, once W5.2 lands,
# generate_edit()'s own {"path","op","content"} entries — may use.
# Matches the plan's D3 edit-op vocabulary (`replace`/`create`/
# `delete`) minus D3's own `search`/`replace` SEARCH/REPLACE fields,
# which belong to eo/code_edit_apply.py's apply_edits() (W5.2) — by
# the time a `files` entry lands in THIS module, an edit has already
# been reduced to "here is the whole proposed text for this path",
# whether that reduction happened via a real apply_edits() call or
# (today) _stub_generate_edit()'s much simpler direct construction.
_VALID_OPS = {"replace", "create", "delete"}

# resolve_proposal()'s per-file decision vocabulary. "keep" writes the
# file for real (via workspace_code_files); "undo" — and, per this
# module's own "unlisted path defaults to undo" rule below, silence —
# discards it. No "partial-within-a-file" value here on purpose: a
# reviewer who kept SOME hunks and undid others inside W5.3's merge
# view expresses that by sending "keep" + a `final_content` that
# already reflects the per-hunk choices, not by inventing a third
# decision value at this layer.
_VALID_DECISIONS = {"keep", "undo"}

# The ref `kind`s _paths_from_refs() itself knows how to turn into "one
# file to read and propose an edit for". "folder" is deliberately NOT
# here — a folder ref never reaches _paths_from_refs() any more (W5.5's
# _expand_refs(), below, rewrites every folder ref into zero or more
# `kind: "file"` refs before create_proposal() calls _paths_from_refs()
# at all). It stays absent from this set (rather than added) so a
# folder ref reaching _paths_from_refs() directly — skipping
# _expand_refs(), a caller bug now instead of the expected path — still
# fails loud instead of being silently treated as one file's path.
_SUPPORTED_REF_KINDS = {"range", "file", "element", "error"}


# ---------------------------------------------------------------------------
# W5.5 — folder ref expansion
# ---------------------------------------------------------------------------
# "folder chips expand server-side to a size-budgeted file set (skip
# binaries, lockfiles, node_modules, minified; show 'included 14 of 40
# files' on the chip)" (Build Workbench plan §5, step 219). Everything in
# this section runs BEFORE _paths_from_refs() — see _expand_refs()'s own
# docstring for exactly where it sits in create_proposal()'s flow.

# Common binary/asset extensions actually plausible in a
# workspace_code_files tree (images, fonts, archives, compiled/media
# output). Not an attempt at a universal binary-sniffing list — same
# "just what this codebase's own file types need" scope
# workspace_code_files._EXTENSION_LANGUAGE_MAP already keeps for itself —
# these are files agents/code_editor.py's own prompt could never usefully
# show a model as text anyway; skipping them here is what keeps a
# folder's file-count/size budget from being spent on something that was
# never editable prose in the first place.
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".tar", ".gz", ".tgz", ".rar", ".7z",
    ".pdf", ".mp3", ".mp4", ".mov", ".wav", ".avi",
    ".wasm", ".pyc", ".so", ".dylib", ".dll", ".class", ".jar",
    ".db", ".sqlite", ".sqlite3",
}

# Exact lockfile basenames — real project files, just never ones anyone
# asks the model to hand-edit; including one in a folder's file set would
# only crowd out files the instruction actually cares about.
_LOCKFILE_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "composer.lock", "Cargo.lock",
    "Gemfile.lock",
}

# A path with any of these as a DIRECTORY segment (never the final
# component — see _skip_reason()) is skipped no matter how deep under
# the folder ref it sits, same "everything under this folder" prefix
# reasoning workspace_code_files.delete_file()'s own docstring already
# gives for a folder delete, just checked per-segment here instead of as
# a single prefix, since node_modules/.git/etc. can appear at any depth
# under the folder someone actually chipped.
_SKIPPED_DIR_SEGMENTS = {
    "node_modules", ".git", "__pycache__", "dist", "build", ".next", ".venv", "venv",
}

_MINIFIED_SUFFIX = re.compile(r"\.min\.[^./]+$", re.IGNORECASE)

# How many files, and how many combined bytes of their `size`
# (workspace_code_files.list_files()'s own metadata — no content read
# needed to decide this), ONE folder ref may pull in. Deliberately
# smaller than agents/code_editor.py's own MAX_CONTEXT_CHARS (60_000):
# that budget is spent RENDERING the files this module already decided
# to include (±N lines of context, numbered, per file); this budget
# decides inclusion up front, in the cheaper currency of "how many
# separate files is one instruction plausibly about" — a folder ref that
# fills this budget still leaves agents/code_editor.py's own per-file/
# total-char limits to further trim what the model actually sees, same
# as an explicit file ref would.
_FOLDER_MAX_FILES = 40
_FOLDER_MAX_TOTAL_CHARS = 200_000


def _skip_reason(path: str) -> str | None:
    """None = include this candidate path; otherwise a short, stable
    reason code for why it was left out of a folder's expansion. A cheap
    closed-set string (same convention as this module's other small
    vocabularies: _VALID_OPS, _VALID_DECISIONS) rather than free text —
    nothing outside this function reads the value today, but a future
    "these were binaries, not truncation" UI hint gets to use it without
    a data shape change.

    Checked in this order: directory segment (node_modules/.git/etc. —
    cheapest check, and the most common reason in a real project tree),
    then exact lockfile basename, then extension, then a `.min.<ext>`
    filename suffix.
    """
    segments = path.split("/")
    if any(seg in _SKIPPED_DIR_SEGMENTS for seg in segments[:-1]):
        return "excluded_dir"
    name = segments[-1]
    if name in _LOCKFILE_BASENAMES:
        return "lockfile"
    _, ext = os.path.splitext(name)
    if ext.lower() in _BINARY_EXTENSIONS:
        return "binary"
    if _MINIFIED_SUFFIX.search(name):
        return "minified"
    return None


def _expand_folder(ws_id: str, folder_path: str) -> dict:
    """Turns one `kind: "folder"` ref's `path` into the size-budgeted set
    of real file paths create_proposal() actually reads/edits for it.

    Matching rule is the same `file_path LIKE 'prefix/%'` idea
    workspace_code_files already uses for a folder delete/move (see that
    module's own comment on _escape_like_prefix) — evaluated in Python
    against workspace_code_files.list_files()'s already-fetched flat
    map instead of a second SQL query, since list_files() (metadata
    only, no content) is already an O(files-in-workspace) call this
    function needs regardless of how many folder refs a proposal has.

    Path-sorted before any skip/budget filtering, so "included 14 of 40"
    means the same 14 files on a re-expand (W5.4's Regenerate replays
    the SAME stored folder ref through create_proposal() again) — not
    "whichever 14 a dict happened to iterate first".

    Returns `{"included": [str, ...], "total_matched": int, "skipped":
    int, "truncated": bool}`. `total_matched` counts every file under
    the prefix BEFORE any filtering (the chip's "of 40" side); `skipped`
    is `total_matched - len(included)` (covers BOTH a _skip_reason()
    match and a budget cutoff — the two are distinguished only by
    `truncated`, not by a separate count, since the chip's own "included
    N of M" wording doesn't need the split; a per-category breakdown
    would be easy to add here later if a UI ever wants one). `truncated`
    is True only when the file/size BUDGET — not a skip rule — is why an
    otherwise-includable file was left out.
    """
    prefix = folder_path.rstrip("/") + "/"
    project = workspace_code_files.list_files(ws_id)
    candidates = sorted(
        (meta for path, meta in project.items() if path.startswith(prefix)),
        key=lambda m: m["file_path"],
    )
    total_matched = len(candidates)

    included: list[str] = []
    used_chars = 0
    truncated = False
    for meta in candidates:
        path = meta["file_path"]
        if _skip_reason(path) is not None:
            continue
        size = meta.get("size", 0)
        if len(included) >= _FOLDER_MAX_FILES or used_chars + size > _FOLDER_MAX_TOTAL_CHARS:
            truncated = True
            break
        included.append(path)
        used_chars += size

    return {
        "included": included,
        "total_matched": total_matched,
        "skipped": total_matched - len(included),
        "truncated": truncated,
    }


def _expand_refs(refs: list[dict], ws_id: str) -> tuple[list[dict], list[dict]]:
    """create_proposal()'s folder-expansion pass — runs BEFORE
    _paths_from_refs(). Returns `(stored_refs, effective_refs)`:

      - `stored_refs` is `refs`, unchanged except that every `kind:
        "folder"` entry gains `includedCount` / `totalMatched` /
        `truncated` keys (straight from _expand_folder() above). This is
        what create_proposal() writes to the proposal's own `refs`
        column, so a later read — the original create response, W5.4's
        "reopen the tab" GET, its Regenerate replaying `proposal.refs` —
        all show the SAME "included 14 of 40 files" numbers, computed
        once here rather than recomputed (and potentially drifting)
        differently by more than one reader.

      - `effective_refs` is what actually reaches resolve_scope() /
        generate_edit(): every non-folder ref unchanged, plus one
        `kind: "file"` ref per included path (no line range — the whole
        file is in scope, same as an explicit file chip). This is what
        gives an expanded file real EditScope coverage instead of being
        flagged scope_violation for edits the person's OWN folder
        instruction asked for (see agents/code_editor.py's
        resolve_scope()).

    A folder ref whose expansion includes zero files (empty folder,
    everything skipped, no such prefix) is dropped from `effective_refs`
    entirely but LEFT in `stored_refs` with includedCount=0 / a real
    totalMatched — the chip can say "included 0 of 0" (or "0 of 3, all
    skipped") rather than the ref silently vanishing. create_proposal()
    still fails loud if that leaves NO effective refs at all — see its
    own "no files to edit" check, the folder-ref analogue of
    _paths_from_refs()'s "at least one ref is required".
    """
    stored: list[dict] = []
    effective: list[dict] = []
    for ref in refs:
        if ref.get("kind") != "folder":
            stored.append(ref)
            effective.append(ref)
            continue
        path = ref.get("path")
        if not path:
            raise ValueError("ref of kind 'folder' is missing 'path'")
        info = _expand_folder(ws_id, path)
        stored.append({
            **ref,
            "includedCount": len(info["included"]),
            "totalMatched": info["total_matched"],
            "truncated": info["truncated"],
        })
        for file_path in info["included"]:
            effective.append({"kind": "file", "path": file_path, "provider": ref.get("provider")})
    return stored, effective


class ProposalStaleError(Exception):
    """Raised by resolve_proposal() when one or more files the
    reviewer asked to KEEP have moved (a different `version` in
    workspace_code_files today than the `base_version` this proposal
    captured at propose time) — same role
    workspace_code_files.VersionConflictError plays for a plain Save,
    just evaluated across a whole proposal's file set instead of one
    PUT. api/routes/code_edit.py's resolve route catches this
    specifically and turns it into a 409.

    Carries `stale_files`: [{path, expected_version, current}] — one
    entry per file that failed the check, `current` being the full
    live-file shape (workspace_code_files._row_to_file()'s own shape)
    so the client can offer the same Reload/Keep-mine/Compare choices
    W1.1's plain-Save 409 already gives, per file, without a second
    round trip.
    """

    def __init__(self, stale_files: list[dict]):
        self.stale_files = stale_files
        super().__init__(
            f"{len(stale_files)} file(s) changed since this proposal was created: "
            f"{[f['path'] for f in stale_files]}"
        )


def _now():
    return datetime.now(UTC)


def _iso(value):
    return value.isoformat() if value is not None else None


def _hash_content(content: str) -> str:
    """A short, stable fingerprint of a file's content at the moment a
    proposal snapshot it — stored as `base_hash` on each `files` entry
    for W5.4's staleness banner ("File changed since this edit was
    proposed") to compare against, display-only, alongside
    base_version (which is what resolve_proposal() below actually
    enforces). Deliberately NOT the same algorithm as
    frontend/app/lib/workbench/codeContext.js's hashString() (djb2) —
    that function fingerprints a REF's snippet, client-side, for a
    completely different comparison (has the chip's own tracked range
    drifted); this one fingerprints a whole file's content, server-
    side, from data the client never has read access to compute
    against ahead of time. The two never need to agree with each
    other. sha256 truncated to 16 hex chars is plenty of collision
    resistance for a "did this obviously change" display hint, not a
    security boundary.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _row_to_proposal(row: dict) -> dict:
    """Full shape returned by every function below — files/refs/
    model_meta are already Python list/dict objects by the time
    psycopg hands back a jsonb column (see eo/db.py's pool-wide
    dict_row + psycopg's own jsonb adapter), so no json.loads() is
    needed here, same as eo/chat_store.py's own `payload` column
    reads."""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "session_id": row.get("session_id"),
        "created_by": row.get("created_by"),
        "instruction": row["instruction"],
        "status": row["status"],
        "files": row.get("files") or [],
        "summary": row.get("summary"),
        "refs": row.get("refs") or [],
        "model_meta": row.get("model_meta") or {},
        "created_at": _iso(row["created_at"]),
        "resolved_at": _iso(row.get("resolved_at")),
    }


_PROPOSAL_COLUMNS = (
    "id, workspace_id, session_id, created_by, instruction, status, "
    "files, summary, refs, model_meta, created_at, resolved_at"
)


def _paths_from_refs(refs: list[dict]) -> list[str]:
    """De-dupes refs (W4.1's model: `{id, kind, path, fromLine,
    toLine, snippet, hash, provider, ...}`, stored/passed through
    opaquely — see migrations/0010_code_proposals.sql's own comment on
    the `refs` column) down to the distinct file paths
    create_proposal() needs to read via workspace_code_files.get_file()
    before it can hand anything to generate_edit(). Order-preserving
    (first occurrence wins) purely so a stub/real generator's output
    ordering is deterministic given the same input, not because
    anything downstream depends on ref order specifically.

    create_proposal() always calls this with `effective_refs` —
    _expand_refs()'s output, not the raw refs a caller sent — so by the
    time anything reaches here, every `kind: "folder"` ref has already
    been rewritten into zero or more `kind: "file"` refs (W5.5). A
    `kind: "folder"` ref reaching this function directly is therefore a
    caller bug (a test, or some future direct call, skipping
    _expand_refs()), refused outright rather than silently treated as
    if its `path` were one file's path — pretending a folder path IS a
    file path here would silently propose an edit "to" a folder that
    workspace_code_files.get_file() would just read back as an always-
    empty file, then silently write nothing useful on resolve. Failing
    loud here is the same "fail loud on a bad path" posture
    workspace_code_files._validate_file_path() already takes for a
    malformed path.
    """
    if not refs:
        raise ValueError("at least one ref is required")
    paths: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        kind = ref.get("kind")
        path = ref.get("path")
        if kind == "folder":
            raise ValueError(
                f"folder refs are not supported by this endpoint yet (path={path!r}) "
                "-- server-side folder expansion is step W5.5, not W5.1"
            )
        if kind not in _SUPPORTED_REF_KINDS:
            raise ValueError(f"unsupported ref kind {kind!r}")
        if not path:
            raise ValueError(f"ref of kind {kind!r} is missing 'path'")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


# Small, deliberately non-exhaustive comment-marker table — just enough
# for _stub_generate_edit() below to drop an honest, visibly-a-comment
# line into whatever language the referenced file already is. Extend
# only if a real file type shows up in testing that this maps wrong;
# this is throwaway once W5.2's real agent replaces the stub entirely,
# so it's not worth chasing full language coverage here.
_LINE_COMMENT_BY_EXT = {
    ".py": "#", ".sh": "#", ".yml": "#", ".yaml": "#",
    ".js": "//", ".jsx": "//", ".ts": "//", ".tsx": "//",
    ".css": "//", ".java": "//", ".go": "//",
}


def _stub_generate_edit(ws_id: str, instruction: str, refs: list[dict],
                         current_files: dict) -> dict:
    """W5.1's honest stand-in for agents/code_editor.py + eo/
    code_edit_apply.py's apply_edits() (W5.2). No longer the default
    since W5.2 — create_proposal() uses agents/code_editor.py's
    generate_edit unless a caller passes `generate_edit` explicitly
    (this stub, or a test double). Every generator must return the
    same shape:

        {"summary": str, "files": [{"path", "op", "content"}, ...],
         "model_meta": dict (optional)}

    `current_files`: {path: workspace_code_files.get_file()'s own
    shape}, one entry per distinct path _paths_from_refs() pulled out
    of `refs`, already read by create_proposal() BEFORE this is called
    — this function never touches the database itself, real or stub,
    so it's trivially swappable/testable without any DB isolation
    machinery of its own.

    What it actually does, per referenced path: appends one clearly-
    labeled comment line recording `instruction`, using
    _LINE_COMMENT_BY_EXT to pick a marker for the file's own extension
    (falling back to `//`). A path workspace_code_files has nothing
    saved at yet (version 0 — see get_file()'s own `_empty_file()`
    shape) gets op="create" with just that comment line as its whole
    content; an existing file gets op="replace" with the comment
    appended to its current content. This is REAL content that lands
    in a REAL review flow (W5.3's merge view, W5.4's proposal card) and
    a REAL file write on resolve — same "the confirmation gate is
    real, the destination integration isn't yet" honesty
    agents/deploy_agent.py's trigger_live_deploy() already documents
    for itself — it is simply not a real code edit, because the thing
    that would produce one doesn't exist in this codebase yet.
    """
    files = []
    for path, current in current_files.items():
        marker = "//"
        for ext, m in _LINE_COMMENT_BY_EXT.items():
            if path.endswith(ext):
                marker = m
                break
        comment_line = f"{marker} TODO(code_editor, W5.2): {instruction}"
        if current["version"] == 0:
            files.append({"path": path, "op": "create", "content": comment_line + "\n"})
        else:
            original = current["content"] or ""
            sep = "\n" if original and not original.endswith("\n") else ""
            files.append({"path": path, "op": "replace", "content": original + sep + comment_line + "\n"})
    return {
        "summary": f"[stub] {instruction}",
        "files": files,
        "model_meta": {"generator": "_stub_generate_edit", "note": "W5.2 not yet wired"},
    }


def _build_files_payload(edit_files: list[dict], current_files: dict) -> list[dict]:
    """Turns generate_edit()'s `{path, op, content}` entries into the
    shape actually stored in workspace_code_proposals.files: `{path,
    op, base_version, base_hash, original, proposed}`.
    base_version/base_hash/original all come from `current_files` —
    the SNAPSHOT create_proposal() read from workspace_code_files
    BEFORE calling generate_edit(), not a fresh read taken here — so
    what's stored describes "what this file looked like when the
    proposal was made", exactly what resolve_proposal() later needs to
    detect drift against, deliberately decoupled from how long
    generate_edit() itself took to run.
    """
    out = []
    seen: set[str] = set()
    for edit in edit_files:
        path = edit.get("path")
        if not path:
            raise ValueError("generated edit is missing 'path' on one of its files")
        if path in seen:
            raise ValueError(f"generated edit lists {path!r} more than once")
        seen.add(path)
        op = edit.get("op", "replace")
        if op not in _VALID_OPS:
            raise ValueError(f"unknown op {op!r} for {path!r}")
        current = current_files.get(path)
        base_version = current["version"] if current is not None else 0
        original = current["content"] if current is not None else ""
        out.append({
            "path": path,
            "op": op,
            "base_version": base_version,
            "base_hash": _hash_content(original),
            "original": original,
            "proposed": edit.get("content", ""),
        })
    return out


def _default_generate_edit(session_id: str | None):
    """W5.2: the real generator — agents/code_editor.py's generate_edit(),
    with `session_id` bound so the model call is attributed to the chat
    session in Token Usage without widening the four-argument
    `generate_edit(ws_id, instruction, refs, current_files)` contract
    every replacement (tests, stubs) implements. Imported lazily:
    agents/code_editor.py pulls in eo.registry, and this module is
    imported by api/routes/code_edit.py at app start-up, so a top-level
    import would put the whole agent/registry graph on that path.
    """
    from agents.code_editor import generate_edit as real_generate_edit
    return functools.partial(real_generate_edit, session_id=session_id)


def create_proposal(ws_id: str, instruction: str, refs: list[dict],
                     session_id: str | None, user_id: str,
                     generate_edit=None) -> dict:
    """POST .../code/proposals — see this module's own docstring for
    the full lifecycle. Reads every referenced file's CURRENT content
    via workspace_code_files.get_file() up front, calls `generate_edit`
    (default: agents/code_editor.py's generate_edit — see
    _default_generate_edit()) SYNCHRONOUSLY, and stores whatever comes back as a
    new row. Nothing here writes to workspace_code_files, no matter
    what generate_edit returns — see resolve_proposal() for the only
    path that does.

    W5.5: `refs` is run through _expand_refs() first — every `kind:
    "folder"` entry becomes zero or more `kind: "file"` refs before
    _paths_from_refs()/get_file() ever see it, and what's stored in
    this proposal's own `refs` column is _expand_refs()'s
    `stored_refs` (the original refs, folder entries annotated with
    includedCount/totalMatched/truncated), not the caller's raw list —
    see _expand_refs()'s own docstring for why.

    A `generate_edit` failure does NOT raise out of this function: it
    is caught, and a status='failed' row is stored and returned
    instead (files=[], model_meta={"error": str(exc)}) — same
    "the pending tray / chat card gets something to show, not a bare
    500" reasoning W5.4's own "live status badge synced by the Pusher
    event" design implies a failed generation needs a REAL row to
    attach that badge to. A bad INPUT (empty instruction, no refs, an
    unsupported ref kind, a folder ref) is a different class of
    problem — that's still a plain ValueError raised straight through,
    same as every other route in this codebase turns a bad-request
    shape into a 400, because there's no proposal worth creating at
    all in that case.
    """
    if not instruction or not instruction.strip():
        raise ValueError("instruction cannot be empty")
    # W7.1a: an element chip's `element` object is page data from the
    # preview iframe — sanitized here (shape, types, lengths) so what is
    # stored on the row, and what reaches the agent, is never the raw
    # request blob. A no-op for refs without an `element` key.
    refs = normalize_element_refs(refs or [])
    stored_refs, effective_refs = _expand_refs(refs, ws_id)
    paths = _paths_from_refs(effective_refs)

    current_files = {path: workspace_code_files.get_file(ws_id, path) for path in paths}

    if generate_edit is None:
        generate_edit = _default_generate_edit(session_id)
    try:
        result = generate_edit(ws_id, instruction, refs, current_files)
        # W7.1a: files the generator read on its own (a stylesheet it was
        # shown for an element chip) come back as `extra_files` — the
        # snapshot it edited against. Without it such a file would be
        # stored as base_version 0 / original "", which makes Keep abort
        # as "stale" and the review diff show the whole file as added.
        # What create_proposal read up front always wins.
        snapshot = {**(result.get("extra_files") or {}), **current_files}
        files = _build_files_payload(result.get("files", []), snapshot)
        summary = result.get("summary") or instruction
        model_meta = result.get("model_meta") or {}
        status = "pending"
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        files = []
        summary = f"Edit generation failed: {exc}"
        model_meta = {"error": str(exc)}
        status = "failed"

    proposal_id = f"prop_{uuid.uuid4().hex[:12]}"
    now = _now()
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            f"""
            insert into workspace_code_proposals
                (id, workspace_id, session_id, created_by, instruction, status,
                 files, summary, refs, model_meta, created_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning {_PROPOSAL_COLUMNS}
            """,
            (proposal_id, ws_id, session_id, user_id, instruction, status,
             db.Json(files), summary, db.Json(stored_refs), db.Json(model_meta), now),
        )
        row = cur.fetchone()

    write_audit(user_id, "code_proposal.create", "workspace", ws_id,
                {"proposal_id": proposal_id, "status": status, "file_count": len(files)})

    # Gotcha (plan §1.5 / §7.2): payload is ids/status only, never file
    # content — same 10,240-byte Pusher cap CODE_FILE_UPDATED's own
    # payload respects. A CODE_PROPOSAL_READY fires even for a
    # status='failed' row, same reasoning as this function's own
    # docstring on why a failed generation still gets a real row: the
    # pending tray / chat card's badge needs to move off "generating"
    # either way.
    emit_workspace_event(
        EventType.CODE_PROPOSAL_READY,
        workspace_id=ws_id,
        agent="code_editor",
        payload={"workspace_id": ws_id, "proposal_id": proposal_id, "status": status},
    )
    return _row_to_proposal(row)


# ---------------------------------------------------------------------------
# W8.5 — deploy config through the same review
# ---------------------------------------------------------------------------
# agents/deploy_config_writer.py already DECIDES the host and the config
# file's full content (and emits DEPLOY_CONFIG_PROPOSED); what it never
# had was a human-in-the-loop write — agents/deploy_agent.py's
# write_deploy_config() just writes the file. This section turns that
# plan into a pending proposal instead, so the file only lands (in
# workspace_code_files, history snapshot and all) when someone presses
# Keep. Deliberately NOT a second LLM call: the plan IS the edit, so the
# generator below is a pure function of it, and create_proposal() does
# everything else (snapshot, store, audit, CODE_PROPOSAL_READY) exactly
# as it does for a chat edit.

# What marks a proposal as a deploy-config one: the frontend uses it to
# send "Regenerate" back to the deploy writer (not the code_editor
# agent, which would just have an LLM rewrite the file), and
# _reject_superseded_deploy_proposals() below uses it to find the
# earlier proposal a re-propose replaces.
DEPLOY_CONFIG_GENERATOR = "deploy_config_writer"

# `reason` is model-written free text; a stored copy only needs to be
# long enough to show in a card, not unbounded jsonb.
_DEPLOY_REASON_MAX_CHARS = 500


def _deploy_plan_file(plan: dict) -> tuple[str, str]:
    """(config_filename, config_content) out of a deploy_config_writer
    plan, or ValueError. The path's own SHAPE (no `..`, not absolute,
    allowed characters) is checked later by workspace_code_files.get_file()
    inside create_proposal(), same as every other proposal path — only
    presence and type are checked here."""
    if not isinstance(plan, dict):
        raise ValueError("deploy plan must be a dict")
    path = plan.get("config_filename")
    content = plan.get("config_content")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("deploy plan has no config_filename")
    if not isinstance(content, str):
        raise ValueError("deploy plan has no config_content")
    return path, content


def _deploy_generate_edit(plan: dict):
    """Returns a `generate_edit` (the four-argument contract every
    generator here implements) that hands back `plan`'s file as-is.
    `create` when nothing is saved at that path yet, `replace` when a
    file already is — the same op split _stub_generate_edit() makes, so
    the review UI draws it as a new file or as a diff accordingly."""
    path, content = _deploy_plan_file(plan)
    platform = str(plan.get("platform") or "deploy")[:60]
    reason = plan.get("reason")
    reason = reason[:_DEPLOY_REASON_MAX_CHARS] if isinstance(reason, str) else None

    def generate(ws_id, instruction, refs, current_files):
        op = "create" if current_files[path]["version"] == 0 else "replace"
        verb = "Add" if op == "create" else "Update"
        return {
            "summary": f"{verb} {platform} deploy config ({path})",
            "files": [{"path": path, "op": op, "content": content}],
            "model_meta": {
                "generator": DEPLOY_CONFIG_GENERATOR,
                "platform": platform,
                "config_filename": path,
                "reason": reason,
            },
        }

    return generate


def _reject_superseded_deploy_proposals(ws_id: str, keep_id: str, user_id: str) -> None:
    """A re-propose replaces the earlier deploy proposal — the same
    "each call overwrites the prior proposal" semantics
    api/routes/deploy.py's propose route already documents for the
    memory-bus plan. Every OTHER pending deploy-config proposal in this
    workspace is rejected through the normal resolve_proposal() path (no
    decisions -> every file undone -> status 'rejected', audited, and
    CODE_PROPOSAL_RESOLVED emitted, so open trays drop it live).

    Matched on the generator tag alone, not on path: the writer picks
    exactly ONE platform per plan, so a re-propose that switches host
    (render.yaml -> fly.toml) must retire the render.yaml proposal too.
    A pending proposal from a chat edit that happens to touch the same
    file is left alone — that one belongs to whoever asked for it, and
    resolve_proposal()'s base_version check already makes whichever Keep
    comes second a clean 'stale'."""
    for old in list_proposals(ws_id, status="pending"):
        if old["id"] == keep_id:
            continue
        if (old.get("model_meta") or {}).get("generator") != DEPLOY_CONFIG_GENERATOR:
            continue
        try:
            resolve_proposal(ws_id, old["id"], [], user_id)
        except (FileNotFoundError, ValueError):
            # Resolved or deleted by someone else between the list and
            # here — nothing left to supersede.
            continue


def create_deploy_config_proposal(ws_id: str, plan: dict, session_id: str | None,
                                   user_id: str) -> dict:
    """Stores `plan` (agents/deploy_config_writer.py's output) as a
    pending proposal and returns it — see this section's header. Writes
    nothing to workspace_code_files: Keep (resolve_proposal()) is still
    the only path that does.

    Raises ValueError for a plan with no usable file (missing
    filename/content, or a path workspace_code_files rejects) — a plan
    problem, not a store problem, so the caller decides what to do with
    it (the deploy route reports it alongside the plan instead of
    failing the whole propose call).

    Earlier PENDING deploy-config proposals in the workspace are
    rejected once the new one exists (see
    _reject_superseded_deploy_proposals()) — after, not before, so a
    failure creating the new one never costs the person the old one."""
    _deploy_plan_file(plan)  # fail fast, before any DB work
    path = plan["config_filename"]
    platform = str(plan.get("platform") or "deploy")[:60]
    proposal = create_proposal(
        ws_id,
        f"Propose {platform} deploy config ({path})",
        [{"kind": "file", "path": path, "provider": "cloud"}],
        session_id,
        user_id,
        generate_edit=_deploy_generate_edit(plan),
    )
    if proposal["status"] == "pending":
        _reject_superseded_deploy_proposals(ws_id, proposal["id"], user_id)
    return proposal


def list_proposals(ws_id: str, status: str | None = None) -> list[dict]:
    """GET .../code/proposals[?status=pending] — most recent first.
    W5.4's pending tray ("the tray is the source of truth... populated
    from GET .../code/proposals?status=pending on mount") is the
    intended caller for the filtered form; an unfiltered call (e.g. a
    future History-style "all proposals" view) is supported by the
    same function rather than a second one.

    trusted=True — same reasoning workspace_code_files.list_files()/
    get_file_history() give for themselves: this reads by workspace_id
    only, no per-call acting user, and the route layer
    (api/routes/code_edit.py) already gates on the caller being a
    member/owner of ws_id via _require_workspace() before this is ever
    reached."""
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}")
    with db.cursor(trusted=True) as cur:
        if status is not None:
            cur.execute(
                f"select {_PROPOSAL_COLUMNS} from workspace_code_proposals "
                "where workspace_id = %s and status = %s order by created_at desc",
                (ws_id, status),
            )
        else:
            cur.execute(
                f"select {_PROPOSAL_COLUMNS} from workspace_code_proposals "
                "where workspace_id = %s order by created_at desc",
                (ws_id,),
            )
        rows = cur.fetchall()
    return [_row_to_proposal(r) for r in rows]


def get_proposal(ws_id: str, proposal_id: str) -> dict:
    """GET .../code/proposals/{id} — W5.4's "close the browser mid-
    review, reopen -> tray still shows it and Review restores the
    diff" needs this to return the FULL stored files[] (original +
    proposed per file), not list_proposals()'s own metadata-adjacent
    row shape — unlike workspace_code_files' list/get split, there's
    no separate "heavy" column to omit here (a proposal's files jsonb
    is already the point of fetching it), so this and list_proposals()
    share one row shape.

    Raises FileNotFoundError (same convention
    eo/chat_workspace.get_workspace() and eo/correction_candidates.py
    already use for "no row here") when nothing matches BOTH
    workspace_id and id — filtering on workspace_id here too, not just
    id, is defense in depth per eo/db.py's own "every store module
    still does its own scoping in the query itself" note, even though
    `id` alone is already globally unique."""
    with db.cursor(trusted=True) as cur:
        cur.execute(
            f"select {_PROPOSAL_COLUMNS} from workspace_code_proposals "
            "where workspace_id = %s and id = %s",
            (ws_id, proposal_id),
        )
        row = cur.fetchone()
    if row is None:
        raise FileNotFoundError(f"no proposal {proposal_id!r} in workspace {ws_id!r}")
    return _row_to_proposal(row)


def _mark_status(ws_id: str, proposal_id: str, status: str, user_id: str,
                  resolved: bool = False) -> dict:
    """Shared by resolve_proposal()'s two exit points (stale-abort and
    a real accepted/rejected/partial resolution) — the only two places
    this module ever changes a proposal's status after creation.
    `resolved=True` also stamps resolved_at; the stale-abort path
    passes resolved=False on purpose — a 'stale' proposal isn't
    resolved, it's still sitting there pending the SAME review, just
    now flagged so the client can offer "Regenerate / Review anyway"
    (W5.4) instead of blindly retrying the same stale write."""
    now = _now()
    with db.cursor(user_id=user_id) as cur:
        if resolved:
            cur.execute(
                f"""
                update workspace_code_proposals set status = %s, resolved_at = %s
                where workspace_id = %s and id = %s
                returning {_PROPOSAL_COLUMNS}
                """,
                (status, now, ws_id, proposal_id),
            )
        else:
            cur.execute(
                f"""
                update workspace_code_proposals set status = %s
                where workspace_id = %s and id = %s
                returning {_PROPOSAL_COLUMNS}
                """,
                (status, ws_id, proposal_id),
            )
        row = cur.fetchone()
    if row is None:
        raise FileNotFoundError(f"no proposal {proposal_id!r} in workspace {ws_id!r}")
    return _row_to_proposal(row)


def resolve_proposal(ws_id: str, proposal_id: str, decisions: list[dict],
                      user_id: str) -> dict:
    """POST .../code/proposals/{id}/resolve — the ONLY function in
    this codebase that ever turns a proposal's stored `proposed` text
    into a real workspace_code_files write. See this module's own
    docstring for the lifecycle this sits at the end of.

    `decisions`: [{"path", "decision": "keep"|"undo", "final_content"?
    }], one entry per file the reviewer has an opinion on. A path
    listed in the proposal's own `files` but ABSENT from `decisions`
    defaults to "undo" — a reviewer who only sends decisions for files
    they actually looked at doesn't accidentally write the rest; there
    is no implicit "keep everything not mentioned" behavior anywhere
    in this function. `final_content`, when given, is the file's
    ACTUAL final text after W5.3's per-hunk Keep/Undo inside the merge
    view — some hunks kept, others undone, still one "keep" decision
    for the whole file at this layer. Omitted, a "keep" writes the
    proposal's own stored `proposed` text verbatim (the whole-file
    case).

    Two-phase, not one, and NOT wrapped in a single Postgres
    transaction spanning every file (workspace_code_files.write_file()/
    delete_file() each open and commit their OWN transaction, same as
    every other caller of those two functions):

      Phase 1 — read the LIVE workspace_code_files row for every file
      being kept and compare its `version` against the `base_version`
      this proposal captured at propose time. Any mismatch means the
      file moved since this proposal was created (a hand edit, a
      pipeline regen, or another proposal's own resolve landing first)
      — the WHOLE resolve is aborted before a single byte is written,
      the proposal is marked 'stale', and ProposalStaleError is raised
      (-> 409) naming every file that conflicted. This is the case the
      plan's own "re-checks base_version -> 409 stale" line describes,
      and it's what makes "the file changes only after resolve" (this
      step's own Done-when) actually true for the overwhelmingly
      common case of no concurrent edit landing mid-review.

      Phase 2 — only reached if Phase 1 found nothing stale: write
      every kept file for real, one workspace_code_files.write_file()/
      delete_file() call per file. Each write_file() call ALSO passes
      base_version (the same value Phase 1 just confirmed matches) so
      it gets its own independent, atomic, row-locked conflict check
      (see that function's own docstring) — Phase 1's check is an
      up-front, best-effort pass across the WHOLE set; write_file()'s
      own per-call check is what's actually race-free for any ONE
      file. A VersionConflictError surfacing here anyway (a write
      landing in the narrow window between Phase 1's read and this
      specific file's write — genuinely rare, and only possible when
      two people are resolving overlapping proposals at the same
      instant) is caught, added to the same stale-files shape
      ProposalStaleError carries, and does NOT roll back any kept
      files already written earlier in this same loop — same "largely
      succeeds or largely doesn't, not glued into one all-or-nothing
      transaction" trade-off eo/workspace_code_files.write_files()
      already documents for its own batch path, just surfacing here
      instead of there. delete_file() has no base_version parameter of
      its own at all (a pre-existing gap — W1.2 never added conflict
      detection to delete), so for a kept op="delete" file, Phase 1's
      read is the ONLY staleness protection this function can offer;
      fixing that is out of this step's scope.

    Overall `status` after a successful (non-stale) resolve: 'accepted'
    if every proposal file was kept, 'rejected' if every one was
    undone, 'partial' otherwise. write_audit()'s `action` follows the
    same three-way split (code_proposal.accept / .reject / .partial).

    Raises FileNotFoundError if the proposal doesn't exist, ValueError
    if it's already resolved (status != 'pending') or a decision uses
    an unrecognized `decision` value, ProposalStaleError per Phase 1/2
    above."""
    proposal = get_proposal(ws_id, proposal_id)
    if proposal["status"] != "pending":
        raise ValueError(
            f"proposal {proposal_id!r} is already resolved (status={proposal['status']!r})"
        )

    for d in decisions:
        if d.get("decision") not in _VALID_DECISIONS:
            raise ValueError(
                f"decision must be one of {sorted(_VALID_DECISIONS)}, got {d.get('decision')!r}"
            )
    decisions_by_path = {d["path"]: d for d in decisions}

    # Phase 1 — see docstring.
    stale = []
    for f in proposal["files"]:
        decision = decisions_by_path.get(f["path"], {}).get("decision", "undo")
        if decision != "keep":
            continue
        live = workspace_code_files.get_file(ws_id, f["path"])
        if live["version"] != f["base_version"]:
            stale.append({"path": f["path"], "expected_version": f["base_version"], "current": live})
    if stale:
        _mark_status(ws_id, proposal_id, "stale", user_id, resolved=False)
        raise ProposalStaleError(stale)

    # Phase 2 — see docstring, including the mid-loop-conflict trade-off.
    kept_paths, undone_paths = [], []
    for f in proposal["files"]:
        path = f["path"]
        entry = decisions_by_path.get(path, {})
        decision = entry.get("decision", "undo")
        if decision != "keep":
            undone_paths.append(path)
            continue
        final_content = entry.get("final_content")
        content = final_content if final_content is not None else f["proposed"]
        try:
            if f["op"] == "delete":
                workspace_code_files.delete_file(ws_id, path, user_id)
            else:
                workspace_code_files.write_file(
                    ws_id, path, content, user_id,
                    base_version=f["base_version"], source="proposal",
                )
            kept_paths.append(path)
        except VersionConflictError as exc:
            stale.append({"path": path, "expected_version": f["base_version"], "current": exc.current})
    if stale:
        _mark_status(ws_id, proposal_id, "stale", user_id, resolved=False)
        raise ProposalStaleError(stale)

    if kept_paths and undone_paths:
        final_status = "partial"
    elif kept_paths:
        final_status = "accepted"
    else:
        final_status = "rejected"

    row = _mark_status(ws_id, proposal_id, final_status, user_id, resolved=True)

    action = {"accepted": "code_proposal.accept", "rejected": "code_proposal.reject",
              "partial": "code_proposal.partial"}[final_status]
    write_audit(user_id, action, "workspace", ws_id,
                {"proposal_id": proposal_id, "kept": kept_paths, "undone": undone_paths})

    # Plan §3's own edit-flow line: "... Done -> resolve -> write_file()
    # + history snapshot + code_file_updated -> every open editor/
    # preview refreshes." write_file()/delete_file() above don't emit
    # this on their own (only api/task_runner.py's pipeline batch path
    # does, unchanged by this patch — see relay/emitter.py's
    # CODE_PROPOSAL_RESOLVED comment for the full "why here, not
    # there" note) — resolve_proposal() is what closes that gap for
    # the proposal-write path specifically. Skipped entirely when
    # nothing was actually written (kept_paths is empty, i.e. a plain
    # 'rejected' resolution) — same "no-op instead of an empty event"
    # discipline emit_workspace_event()'s own workspace_id=None no-op
    # path already takes, just decided by this caller instead.
    if kept_paths:
        emit_workspace_event(
            EventType.CODE_FILE_UPDATED,
            workspace_id=ws_id,
            agent="code_editor",
            payload={
                "file_path": kept_paths[-1],
                "file_paths": kept_paths,
                "workspace_id": ws_id,
            },
        )

    emit_workspace_event(
        EventType.CODE_PROPOSAL_RESOLVED,
        workspace_id=ws_id,
        agent="code_editor",
        payload={"workspace_id": ws_id, "proposal_id": proposal_id, "status": final_status},
    )
    return row
