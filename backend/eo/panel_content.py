"""
eo/panel_content.py — generic per-workspace persistence for the "paste
the chat's output into a box" panels: Mind Map, Study (flashcards/quiz/
study guide), PRD, Architecture, Schema, API Contract, Devil's Advocate,
Feasibility, Wireframes, Contradictions, Extraction Table (manual-paste
fallback).

None of these ever had a backend store — content lived only in each
component's local React state, so a reload (or even just switching
sub-tabs, which unmounts the component) silently discarded whatever had
been pasted in. That was a deliberate, flagged simplification, not a
bug — see the "paste-and-Load" comments in NotebooksTab.jsx.

This module gives every one of those panels the exact same shape of
durability workspace_facts.py already gives brand_voice/target_user/
tech_stack: one row per (workspace_id, panel_key), last-write-wins, no
version history. If the person later wants undo/history across edits,
that's a bigger feature (how many past versions, how conflicts resolve)
and deliberately out of scope for this pass — flagged here rather than
silently decided.

Schema (see migrations/0001_add_panel_content_source_node_ids.sql, the
first tracked migration in this repo — earlier columns on this table
predate migration tracking and were applied by hand; content_source
added by migrations/0005_add_panel_content_source.sql):
    workspace_panel_content(
        workspace_id     text references workspaces(id) on delete cascade,
        panel_key        text,
        content          text,
        updated_at       timestamptz,
        updated_by       text,
        source_node_ids  text[],  -- NEW, migration 0001 — see that file's
                                   -- comment for the full NULL-vs-array
                                   -- semantics; short version: NULL on a
                                   -- GENERATED_PANEL_KEYS row means "used
                                   -- the whole notebook," a non-null array
                                   -- means "scoped to exactly these nodes,"
                                   -- and manual-paste panels never set it.
        content_source   text,     -- NEW, migration 0005 — 'manual' (the
                                   -- paste-and-Load path, the default) or
                                   -- 'chat' (write_panel_from_role()'s
                                   -- direct-write path). See that
                                   -- migration's own comment for why this
                                   -- is the piece updated_by alone can't
                                   -- answer — both paths stamp the same
                                   -- owner_id there.
        primary key (workspace_id, panel_key)
    )
"""
import re
from datetime import UTC, datetime

from eo import db
from eo.audit_log import write_audit
from utils.mermaid_lint import looks_valid_mermaid

# Explicit allowlist rather than accepting any string for panel_key — a
# frontend typo (e.g. "mind_map" vs "mindmap") should fail loudly at the
# API layer as a 400, not silently write a row under a key nothing will
# ever read back.
VALID_PANEL_KEYS = {
    "mindmap",
    "study_flashcards",
    "study_quiz",
    "study_guide",
    "prd",
    "architecture",
    "schema",
    "api_contract",
    "devils_advocate",
    "feasibility",
    "wireframes",
    "contradictions",
    "extraction_manual",
    "audit",
    "suggested_workflows",  # NEW — bug audit §7: agents/workflow_suggester.py's
                             # {"workflows": [...]} result, JSON-encoded into
                             # this column same as every other panel here —
                             # see api/server.py's _generate_workflows.
    "suggested_route",  # NEW — chat audit fix: agents/mind_mapper.py's
                         # generate_suggested_route() wired up — the
                         # prerequisite-ordered "study path" flowchart,
                         # distinct from "mindmap"'s topic-overview one.
                         # See api/server.py's _generate_suggested_route.
    "topic_workflows",  # NEW — step 7 persistence fix: a single JSON
                         # dict of {topic_key: workflow} for every
                         # per-topic Mind Map click this workspace has
                         # ever made, keyed by agents/workflow_suggester.py's
                         # build_topic_workflow() topic_id when it found a
                         # real topic match, or a slugified topic_label
                         # when it fell back to the generic sequence (no
                         # stable id to key by in that case). One row per
                         # workspace, same "single blob, keyed inside"
                         # shape as suggested_workflows above, rather than
                         # a dynamic workflow:<topic_id> key per topic --
                         # keeps this allowlist and invalidate_for_nodes()'s
                         # fixed-list query untouched. See api/server.py's
                         # topic_workflow_endpoint for the get-merge-set.
    "podcast",  # NEW — Phase 5 step 5.4: api/server.py's notebooks_podcast()
                # used to hand back script_text + an on-disk mp3 with nothing
                # saved, so a reload lost both. content is a JSON string —
                # {"script_text", "audio_path"} — same encode-on-write/
                # decode-on-read shape as suggested_workflows above.
                # audio_path is NOTES_EXPORTS_DIR-relative (currently just
                # "podcast_<ws_id>.mp3"), not an absolute filesystem path,
                # so this row stays portable if NOTES_EXPORTS_DIR ever moves —
                # no GET route serves the file back yet (that's step 5.6+
                # alongside the manifest/dispatch registration).
    "video_overview",  # NEW — Phase 5 step 5.5: api/server.py's
                # notebooks_video_overview(), same shape as "podcast" above
                # one step up the chain. content is a JSON string —
                # {"slide_text", "script_text", "video_path"} — video_path
                # is NOTES_EXPORTS_DIR-relative (currently
                # "video_overview_<ws_id>.mp4"), same portability reasoning
                # as podcast's audio_path.
    "presentation_rehearsal",  # NEW — Phase 5 step 5.10: api/server.py's
                # _generate_presentation_rehearsal(), own key rather than
                # overloading "podcast" — a rehearsal script/audio is
                # content a user would want kept separate from (not
                # overwriting) a saved podcast. content is a JSON string —
                # {"script_text", "audio_path", "mode", "difficulty"} —
                # audio_path is NOTES_EXPORTS_DIR-relative (currently
                # "presentation_rehearsal_<ws_id>.mp3"), same portability
                # reasoning as podcast's own audio_path.
}

# NEW — bug audit §2 real fix (migration 0001). The subset of
# VALID_PANEL_KEYS that are ever written *from a notebook's sources* via
# the Generate picker, as opposed to pasted in by hand from an external
# chat. Only these panels ever get a source_node_ids value, and only
# these are eligible for the delete-cascade's selective invalidation in
# invalidate_for_nodes() below — the manual-paste panels (prd,
# architecture, schema, api_contract, devils_advocate, feasibility,
# wireframes, contradictions, extraction_manual, audit) were never tied
# to any specific source in the first place, so a source being deleted
# has no bearing on them and they're deliberately left alone. (The old
# clear_workspace() cheap fix used to wipe these too, as a side effect
# of "clear everything" — that was never actually correct, just cheap.)
GENERATED_PANEL_KEYS = {
    "mindmap",
    "study_flashcards",
    "study_quiz",
    "study_guide",
    "suggested_workflows",
    "suggested_route",
    "topic_workflows",  # NEW — step 7. Always written with
                         # source_node_ids=None (see topic_workflow_endpoint),
                         # so any source delete invalidates the whole blob
                         # rather than just the topics that source actually
                         # fed. Deliberately coarse: these are cheap,
                         # click-triggered regenerations, and the row
                         # already can't record per-topic scope without a
                         # second column — same tradeoff the NULL-scope
                         # case for mindmap/suggested_workflows already
                         # accepts for a whole-notebook Regenerate.
    "podcast",  # NEW — Phase 5 step 5.4. notebooks_podcast() reads
                # scope["source_node_ids"] the same "blank scope = whole
                # notebook" way every other Generate target does, so it's
                # source-scoped exactly like suggested_workflows and
                # belongs in this set too — deleting a source that fed a
                # saved podcast script should invalidate it, same as any
                # other generated panel.
    "video_overview",  # NEW — Phase 5 step 5.5. Same reasoning as
                # "podcast" immediately above — notebooks_video_overview()
                # is source-scoped the same way.
    "presentation_rehearsal",  # NEW — Phase 5 step 5.10. Same reasoning
                # as "podcast"/"video_overview" immediately above —
                # _generate_presentation_rehearsal() reads
                # scope["source_node_ids"] the same "blank scope = whole
                # notebook" way, so it's source-scoped exactly like its
                # two siblings and belongs in this set too.
}


def _now():
    return datetime.now(UTC)


def _iso(value):
    return value.isoformat() if value is not None else None


def _row_to_content(row: dict) -> dict:
    return {
        "workspace_id": row["workspace_id"],
        "panel_key": row["panel_key"],
        "content": row["content"],
        "updated_at": _iso(row["updated_at"]),
        "updated_by": row.get("updated_by"),
        "source_node_ids": row.get("source_node_ids"),
        "content_source": row.get("content_source") or "manual",
    }


def _empty_content(ws_id: str, panel_key: str) -> dict:
    return {
        "workspace_id": ws_id,
        "panel_key": panel_key,
        "content": "",
        "updated_at": None,
        "updated_by": None,
        "source_node_ids": None,
        "content_source": None,
    }


def get_content(ws_id: str, panel_key: str) -> dict:
    """Returns an empty-content shape (not a 404) when nothing has been
    saved yet — a panel nobody's touched should render blank, not error."""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select workspace_id, panel_key, content, updated_at, updated_by, source_node_ids, content_source "
            "from workspace_panel_content where workspace_id = %s and panel_key = %s",
            (ws_id, panel_key),
        )
        row = cur.fetchone()
    return _row_to_content(row) if row else _empty_content(ws_id, panel_key)


def list_content(ws_id: str) -> dict:
    """All saved panels for a workspace in one round trip, keyed by
    panel_key. Panels with no saved row simply don't appear in the
    dict — callers fall back to empty-string same as get_content."""
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select workspace_id, panel_key, content, updated_at, updated_by, source_node_ids, content_source "
            "from workspace_panel_content where workspace_id = %s",
            (ws_id,),
        )
        rows = cur.fetchall()
    return {r["panel_key"]: _row_to_content(r) for r in rows}


def set_content(ws_id: str, panel_key: str, content: str, user_id: str, source_node_ids=None,
                 content_source: str = "manual") -> dict:
    """source_node_ids: pass the scope's source_node_ids list when this
    write comes from a Generate/Regenerate run (see api/server.py's
    _generate_mindmap, _make_study_generate, _generate_workflows), so
    the delete cascade below can tell what fed this panel. Leave it
    unset (None) for manual paste-and-Load writes (put_workspace_panel_content)
    — None is also what a whole-notebook Regenerate run (no scope) should
    pass, since "no source_node_ids" and "used everything" happen to
    need the same invalidate-on-any-delete behavior; see
    GENERATED_PANEL_KEYS / invalidate_for_nodes() for how the two cases
    are actually told apart (by panel_key, not by this value).

    content_source: NEW, migration 0005. 'manual' (the default) for the
    paste-and-Load path — put_workspace_panel_content calls this without
    passing the param, so a manual save always defaults correctly with
    no call-site change needed there. 'chat' is passed explicitly by
    write_panel_from_role() below, the only other caller of this
    function, for its automatic direct-write path. updated_by alone
    can't distinguish the two — both paths stamp the same owner_id —
    this column is what actually answers "was this typed by a person or
    written by chat.\""""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    if content_source not in ("manual", "chat"):
        raise ValueError(f"unknown content_source {content_source!r}")
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            """
            insert into workspace_panel_content (workspace_id, panel_key, content, updated_at, updated_by, source_node_ids, content_source)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (workspace_id, panel_key)
            do update set content = excluded.content, updated_at = excluded.updated_at,
                          updated_by = excluded.updated_by, source_node_ids = excluded.source_node_ids,
                          content_source = excluded.content_source
            returning workspace_id, panel_key, content, updated_at, updated_by, source_node_ids, content_source
            """,
            (ws_id, panel_key, content, _now(), user_id, source_node_ids, content_source),
        )
        row = cur.fetchone()
    write_audit(user_id, "panel_content.save", "workspace", ws_id, {"panel_key": panel_key})
    return _row_to_content(row)


def delete_content(ws_id: str, panel_key: str, user_id: str) -> None:
    """Not currently wired to any UI affordance — included so a future
    "clear this panel" button doesn't need a new module function."""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "delete from workspace_panel_content where workspace_id = %s and panel_key = %s",
            (ws_id, panel_key),
        )
    write_audit(user_id, "panel_content.delete", "workspace", ws_id, {"panel_key": panel_key})


# NEW — Master Guide V2 step 15 (T2), patch 1: Plan tab's chat-to-panel
# direct-write feature. Six of Plan's panels (PRD/Architecture/Schema/
# API Contract/Devil's Advocate/Feasibility) are produced by a role
# running through eo/executor.py's execute_graph(), but until now
# nothing connected that role's output back to THIS module's store —
# the panel only ever got filled by a person manually copy-pasting the
# chat's own answer into the panel's paste box (see the six
# MarkdownPastePanel/DiagramPastePanel FIX comments in PlanTab.jsx for
# the paste-box side of that gap). Blueprint's three panels
# (parts/wiring/mech, plus instructions) are NOT part of this map —
# hardware_speccer.py already has its own direct-write path (see that
# module's own docstring) through eo/workspace_facts.py's custom dict,
# a different mechanism for a different (structured, multi-sub-view)
# shape than this module's single opaque `content` string.
#
# Deliberately just a role -> panel_key lookup plus one small extraction
# function here — self-contained and independently testable, same
# incremental order eo/skill_library.py's own patch 1 already followed
# (data-layer piece correct and tested on its own before any call site
# is wired to depend on it; that wiring is patch 2, a later piece).
PLAN_ROLE_PANEL_MAP = {
    "prd_writer": "prd",
    "architecture_diagrammer": "architecture",
    "schema_diagrammer": "schema",
    "api_contract_writer": "api_contract",
    "devils_advocate": "devils_advocate",
    "feasibility_estimator": "feasibility",
}


# Bug fix (2026-08-12, wiring-diagram audit, Bugs 9/10/11): prd_writer's
# brief (eo/registry.py's "prd_writer_hardware_scope") now tells the model
# not to freehand a wiring ```mermaid block at all -- Blueprint's Wiring
# tab (hardware_speccer.py's wiring.nodes/edges, rendered via
# WiringGraph.jsx and, since the wiring-diagram patch, a real deterministic
# Mermaid render too) is the one source of truth for that diagram now.
# That's prevention, not a guarantee: this role is still a plain
# generic_worker prose hire with no structured output and no validation
# step before its text lands here (unlike agents/mind_mapper.py's
# "mapper" role, which has looks_valid_mermaid() + one retry before ever
# reaching a frontend component). A model can still ignore a prompt
# instruction. Rather than bolt on mind_mapper's retry machinery for a
# diagram this PRD role has no business producing in the first place, this
# is a strip-not-repair backstop scoped ONLY to prd_writer's write path:
# any ```mermaid fenced block that slips through anyway is validated with
# the same cheap heuristic mind_mapper already trusts, and dropped (in
# favor of a one-line pointer to the real diagram) if it fails. A retry
# would just re-run the whole PRD generation for one bad diagram in an
# otherwise-fine document -- not worth it when Blueprint already has the
# real thing. Every other PLAN_ROLE_PANEL_MAP role keeps writing straight
# through unchanged; architecture_diagrammer/schema_diagrammer already
# have their own dedicated deterministic-render + sanitizer path (the
# rendering audit's Bug 5 fix) and were never freehand text to begin with.
_MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n?(.*?)```", re.DOTALL)

_PRD_WIRING_FALLBACK_NOTE = (
    "*(Wiring diagram omitted here -- it didn't come out as valid Mermaid, "
    "and this section shouldn't hold a second copy of it anyway. See the "
    "Wiring view under this project's Blueprint tab for the real, "
    "pin-labeled diagram.)*"
)


def _gate_prd_mermaid(text: str) -> str:
    """Drops any ```mermaid fenced block in prd_writer's output that
    doesn't pass looks_valid_mermaid()'s heuristic check, replacing it
    with a short pointer to Blueprint's Wiring tab instead. A block that
    DOES pass is left exactly as the model wrote it -- this is a safety
    net for the common breakage (Bug 10's unescaped-label case and
    similar), not a ban on every fenced Mermaid block prd_writer could
    ever legitimately write (a non-wiring diagram elsewhere in a PRD
    isn't this bug's concern). No-op (returns text unchanged) when there
    is no fenced Mermaid block at all, which is the expected case now
    that the prompt asks the model not to include one.
    """
    if "```mermaid" not in text:
        return text

    def _replace(match: re.Match) -> str:
        candidate = match.group(1).strip()
        if candidate and looks_valid_mermaid(candidate):
            return match.group(0)
        return _PRD_WIRING_FALLBACK_NOTE

    return _MERMAID_FENCE_RE.sub(_replace, text)


def _text_from_role_result(result: dict) -> str:
    """architecture_diagrammer/schema_diagrammer return {"text", "mermaid",
    "plan"} where "text" and "mermaid" are the same string (see those two
    modules' own run_*() — both set `plan["mermaid"] = ...` then return
    `{"text": plan["mermaid"], "mermaid": plan["mermaid"], ...}`); the
    other four roles are plain agents/generic_worker.py hires returning
    {"role", "text", "next_destination"} — free-form prose, no "mermaid"
    key at all. Checking "mermaid" first therefore never picks the wrong
    field for either shape; it's just an explicit statement of which key
    each of the two shapes actually wants written into the panel, rather
    than relying on both shapes happening to agree on "text" forever.
    """
    if not isinstance(result, dict):
        return ""
    return (result.get("mermaid") or result.get("text") or "").strip()


def write_panel_from_role(ws_id: str, role: str, result: dict, user_id: str) -> dict | None:
    """The actual direct-write: given a role that just finished (as
    eo/executor.py's execute_graph() hands back in its own `results`
    dict, keyed by role) and the workspace its owning chat belongs to,
    writes that role's output straight into the matching panel via
    set_content() — no paste box round-trip.

    Returns the saved content dict (set_content()'s own return shape) on
    an actual write, or None for anything that isn't a genuine write:
    `role` not in PLAN_ROLE_PANEL_MAP (most roles — this is the expected,
    common case, not an error), or a mapped role whose result carried no
    non-empty text (e.g. a role that itself failed over to a fallback
    apology string an earlier step already surfaced as an error — writing
    that into the panel would silently blank out whatever a person may
    have manually saved there before). Never raises: set_content()'s own
    ValueError on an unknown panel_key can't actually fire here since
    every PLAN_ROLE_PANEL_MAP value is one of VALID_PANEL_KEYS by
    construction, but this stays a plain return rather than an assert so
    a future edit to either constant fails as a silent no-op, not a
    crash mid-turn, on the same "a bug here shouldn't take down the
    actual task" posture patch 3's own wiring (a later piece) needs to
    lean on to justify calling this best-effort.

    Deliberately does NOT set source_node_ids — these six panels are
    manual-paste-family panels (VALID_PANEL_KEYS but not
    GENERATED_PANEL_KEYS, see that set's own comment above), never
    source-scoped, chat-triggered or not; this call is just a different
    way of arriving at the same paste-box row, not a new "generated from
    these notebook sources" concept.

    DOES pass content_source="chat" (migration 0005) — this write and a
    person's own manual paste-and-Load save both stamp the same
    updated_by (owner_id either way), so content_source is the only
    field that actually distinguishes "chat wrote this" from "a person
    typed this," e.g. for a future "auto-filled by chat, save to keep
    your edits" UI hint before a manual edit silently overwrites it.
    """
    panel_key = PLAN_ROLE_PANEL_MAP.get(role)
    if panel_key is None:
        return None
    text = _text_from_role_result(result)
    if not text:
        return None
    # Bug fix (2026-08-12, Bugs 9/10/11): prd_writer specifically -- see
    # _gate_prd_mermaid()'s own comment for why this is scoped to just
    # this one role rather than every PLAN_ROLE_PANEL_MAP entry.
    if role == "prd_writer":
        text = _gate_prd_mermaid(text)
    return set_content(ws_id, panel_key, text, user_id, content_source="chat")


# CHANGED — bug audit §2 real fix (migration 0001). This used to be the
# "clear every panel in the workspace" cheap fix, called unconditionally
# from the delete-source cascade. Now that generated panels record their
# own source_node_ids (see set_content()), the cascade can invalidate
# only the panels a given deleted source actually fed, and can leave the
# manual-paste panels (never source-scoped to begin with) alone
# entirely. clear_workspace() below is kept as-is for anything that
# still wants a genuine "wipe every panel" action (e.g. a future
# "reset this notebook" button) — it's just no longer what the delete
# cascade calls.
def invalidate_for_nodes(ws_id: str, node_ids: list[str], user_id: str) -> list[str]:
    """Deletes the saved row for every GENERATED_PANEL_KEYS panel whose
    recorded source_node_ids overlaps `node_ids` (the batch of node ids
    just deleted), or whose source_node_ids is NULL — a generated panel
    with no recorded scope means it was built from "the whole notebook"
    at generation time (a plain Regenerate with no picker scope), which
    by definition included whatever's being deleted now. Manual-paste
    panels (prd, architecture, etc.) are never touched — they're outside
    GENERATED_PANEL_KEYS and were never tied to any source. Returns the
    list of panel_keys actually cleared, so callers/tests can assert on
    exactly what got invalidated instead of just a row count.

    Deliberately a hard delete (same as the old clear_workspace()), not
    a "mark stale" flag — every panel view already renders its own
    "nothing generated yet" empty state for a missing row, so a cleared
    panel and a never-generated one are indistinguishable, which is
    the correct user-facing result here (no separate "stale" UI needed).
    """
    if not node_ids:
        return []
    node_id_set = set(node_ids)
    cleared = []
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "select panel_key, source_node_ids from workspace_panel_content "
            "where workspace_id = %s and panel_key = any(%s)",
            (ws_id, list(GENERATED_PANEL_KEYS)),
        )
        rows = cur.fetchall()
        for row in rows:
            scope = row.get("source_node_ids")
            stale = scope is None or bool(node_id_set.intersection(scope))
            if not stale:
                continue
            cur.execute(
                "delete from workspace_panel_content where workspace_id = %s and panel_key = %s",
                (ws_id, row["panel_key"]),
            )
            cleared.append(row["panel_key"])
    if cleared:
        write_audit(user_id, "panel_content.invalidate_for_nodes", "workspace", ws_id,
                    {"panel_keys": cleared, "node_ids": node_ids})
    return cleared


def clear_workspace(ws_id: str, user_id: str) -> int:
    """Deletes every saved panel for a workspace, generated or manual —
    a genuine "wipe everything" action. No longer called by the
    delete-source cascade (see invalidate_for_nodes() above); kept for
    any future "reset this notebook" affordance that actually wants the
    blunt version. Returns the number of rows removed."""
    with db.cursor(user_id=user_id) as cur:
        cur.execute("delete from workspace_panel_content where workspace_id = %s", (ws_id,))
        count = cur.rowcount
    if count:
        write_audit(user_id, "panel_content.clear_workspace", "workspace", ws_id, {"rows_deleted": count})
    return count
