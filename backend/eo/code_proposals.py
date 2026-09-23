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

W5.2 seam (read before touching the `generate_edit` parameter below):
agents/code_editor.py + eo/code_edit_apply.py — the real "LLM proposes
a SEARCH/REPLACE edit, apply_edits() turns it into original/proposed
text" pipeline the plan's D2/D3 describe — are a LATER step, not this
one. create_proposal() below is deliberately written so THAT step only
ever has to change ONE line (this module's `generate_edit` default
argument) once it exists; everything about how a generated edit turns
into a stored proposal row, gets listed, gets fetched, and gets
resolved is already fully real in this patch. See
_stub_generate_edit()'s own docstring for exactly what today's default
does instead, and why that's an honest (not a fake/mocked) stand-in.

Same FakeCursor-isolatable, `eo.db`-only-touches-Postgres shape every
other Postgres-backed store module in this package already takes (see
eo/workspace_code_files.py, eo/chat_store.py) — nothing here imports
psycopg directly.
"""
import hashlib
import uuid
from datetime import UTC, datetime

from eo import db, workspace_code_files
from eo.audit_log import write_audit
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

# The ref `kind`s this step knows how to turn into "one file to read
# and propose an edit for". "folder" is deliberately NOT here — see
# _paths_from_refs()'s own docstring for why a folder ref reaching this
# module today is a caller bug, not a case to quietly degrade.
_SUPPORTED_REF_KINDS = {"range", "file", "element", "error"}


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

    A `kind: "folder"` ref is refused outright (ValueError, which the
    route turns into a 400) rather than silently treated as if its
    `path` were one file's path: W5.5 ("Folder / multi-file scope") is
    the step that expands a folder chip server-side into a size-
    budgeted set of real file paths ("included 14 of 40 files" on the
    chip, binaries/lockfiles/node_modules skipped); that expansion
    logic doesn't exist yet, and pretending a folder path IS a file
    path here would silently propose an edit "to" a folder that
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
    code_edit_apply.py's apply_edits() (W5.2, not built yet) — see this
    module's own docstring for the "only one line changes when W5.2
    lands" contract. This is create_proposal()'s DEFAULT `generate_edit`
    callable; any real or test replacement must return the same shape:

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


def create_proposal(ws_id: str, instruction: str, refs: list[dict],
                     session_id: str | None, user_id: str,
                     generate_edit=None) -> dict:
    """POST .../code/proposals — see this module's own docstring for
    the full lifecycle. Reads every referenced file's CURRENT content
    via workspace_code_files.get_file() up front, calls `generate_edit`
    (default: _stub_generate_edit — see its own docstring for the W5.2
    swap-in seam) SYNCHRONOUSLY, and stores whatever comes back as a
    new row. Nothing here writes to workspace_code_files, no matter
    what generate_edit returns — see resolve_proposal() for the only
    path that does.

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
    refs = refs or []
    paths = _paths_from_refs(refs)

    current_files = {path: workspace_code_files.get_file(ws_id, path) for path in paths}

    generate_edit = generate_edit or _stub_generate_edit
    try:
        result = generate_edit(ws_id, instruction, refs, current_files)
        files = _build_files_payload(result.get("files", []), current_files)
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
             db.Json(files), summary, db.Json(refs), db.Json(model_meta), now),
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
