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
predate migration tracking and were applied by hand):
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
        primary key (workspace_id, panel_key)
    )
"""
from datetime import datetime, timezone
from eo import db
from eo.audit_log import write_audit

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
    return datetime.now(timezone.utc)


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
    }


def _empty_content(ws_id: str, panel_key: str) -> dict:
    return {
        "workspace_id": ws_id,
        "panel_key": panel_key,
        "content": "",
        "updated_at": None,
        "updated_by": None,
        "source_node_ids": None,
    }


def get_content(ws_id: str, panel_key: str) -> dict:
    """Returns an empty-content shape (not a 404) when nothing has been
    saved yet — a panel nobody's touched should render blank, not error."""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor() as cur:
        cur.execute(
            "select workspace_id, panel_key, content, updated_at, updated_by, source_node_ids "
            "from workspace_panel_content where workspace_id = %s and panel_key = %s",
            (ws_id, panel_key),
        )
        row = cur.fetchone()
    return _row_to_content(row) if row else _empty_content(ws_id, panel_key)


def list_content(ws_id: str) -> dict:
    """All saved panels for a workspace in one round trip, keyed by
    panel_key. Panels with no saved row simply don't appear in the
    dict — callers fall back to empty-string same as get_content."""
    with db.cursor() as cur:
        cur.execute(
            "select workspace_id, panel_key, content, updated_at, updated_by, source_node_ids "
            "from workspace_panel_content where workspace_id = %s",
            (ws_id,),
        )
        rows = cur.fetchall()
    return {r["panel_key"]: _row_to_content(r) for r in rows}


def set_content(ws_id: str, panel_key: str, content: str, user_id: str, source_node_ids=None) -> dict:
    """source_node_ids: pass the scope's source_node_ids list when this
    write comes from a Generate/Regenerate run (see api/server.py's
    _generate_mindmap, _make_study_generate, _generate_workflows), so
    the delete cascade below can tell what fed this panel. Leave it
    unset (None) for manual paste-and-Load writes (put_workspace_panel_content)
    — None is also what a whole-notebook Regenerate run (no scope) should
    pass, since "no source_node_ids" and "used everything" happen to
    need the same invalidate-on-any-delete behavior; see
    GENERATED_PANEL_KEYS / invalidate_for_nodes() for how the two cases
    are actually told apart (by panel_key, not by this value)."""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor() as cur:
        cur.execute(
            """
            insert into workspace_panel_content (workspace_id, panel_key, content, updated_at, updated_by, source_node_ids)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (workspace_id, panel_key)
            do update set content = excluded.content, updated_at = excluded.updated_at,
                          updated_by = excluded.updated_by, source_node_ids = excluded.source_node_ids
            returning workspace_id, panel_key, content, updated_at, updated_by, source_node_ids
            """,
            (ws_id, panel_key, content, _now(), user_id, source_node_ids),
        )
        row = cur.fetchone()
    write_audit(user_id, "panel_content.save", "workspace", ws_id, {"panel_key": panel_key})
    return _row_to_content(row)


def delete_content(ws_id: str, panel_key: str, user_id: str) -> None:
    """Not currently wired to any UI affordance — included so a future
    "clear this panel" button doesn't need a new module function."""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor() as cur:
        cur.execute(
            "delete from workspace_panel_content where workspace_id = %s and panel_key = %s",
            (ws_id, panel_key),
        )
    write_audit(user_id, "panel_content.delete", "workspace", ws_id, {"panel_key": panel_key})


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
    with db.cursor() as cur:
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
    with db.cursor() as cur:
        cur.execute("delete from workspace_panel_content where workspace_id = %s", (ws_id,))
        count = cur.rowcount
    if count:
        write_audit(user_id, "panel_content.clear_workspace", "workspace", ws_id, {"rows_deleted": count})
    return count
