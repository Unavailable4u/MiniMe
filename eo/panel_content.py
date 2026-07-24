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

Schema (see migrations/xxxx_add_workspace_panel_content.sql):
    workspace_panel_content(
        workspace_id  text references workspaces(id) on delete cascade,
        panel_key     text,
        content       text,
        updated_at    timestamptz,
        updated_by    text,
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
    }


def _empty_content(ws_id: str, panel_key: str) -> dict:
    return {"workspace_id": ws_id, "panel_key": panel_key, "content": "", "updated_at": None, "updated_by": None}


def get_content(ws_id: str, panel_key: str) -> dict:
    """Returns an empty-content shape (not a 404) when nothing has been
    saved yet — a panel nobody's touched should render blank, not error."""
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor() as cur:
        cur.execute(
            "select workspace_id, panel_key, content, updated_at, updated_by "
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
            "select workspace_id, panel_key, content, updated_at, updated_by "
            "from workspace_panel_content where workspace_id = %s",
            (ws_id,),
        )
        rows = cur.fetchall()
    return {r["panel_key"]: _row_to_content(r) for r in rows}


def set_content(ws_id: str, panel_key: str, content: str, user_id: str) -> dict:
    if panel_key not in VALID_PANEL_KEYS:
        raise ValueError(f"unknown panel_key {panel_key!r}")
    with db.cursor() as cur:
        cur.execute(
            """
            insert into workspace_panel_content (workspace_id, panel_key, content, updated_at, updated_by)
            values (%s, %s, %s, %s, %s)
            on conflict (workspace_id, panel_key)
            do update set content = excluded.content, updated_at = excluded.updated_at, updated_by = excluded.updated_by
            returning workspace_id, panel_key, content, updated_at, updated_by
            """,
            (ws_id, panel_key, content, _now(), user_id),
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


# NEW — bug audit §2 ("delete cascade"): every panel in this module is a
# whole-notebook artifact (one row per (workspace_id, panel_key)), built
# by summarizing every source node in the workspace at generation time —
# see agents/mind_mapper.py, agents/study_generator.py, etc. None of them
# record *which* source nodes went into a given generation, so once a
# source is deleted there's no precise way to know which saved panels it
# tainted.
#
# The audit guide flags two options: a precise `source_node_ids` column
# (needs each generator to start recording its inputs) or the "immediate,
# cheap fix" of clearing every saved panel for the workspace so nothing
# stale is left on screen. Going with the cheap fix here — it needs no
# schema change (this table already exists; a new column would need a
# manual migration against part8_schema.sql, which isn't tracked in this
# repo), and every panel view already renders its own "nothing generated
# yet" empty state when no row is saved (see NotebooksTab.jsx's
# MindMapView etc.), so clearing rows here is indistinguishable from
# "never generated" rather than an error state. The precise
# source_node_ids version is a reasonable follow-up if wiping the whole
# notebook's panels on every single source delete turns out to be too
# aggressive in practice.
def clear_workspace(ws_id: str, user_id: str) -> int:
    """Deletes every saved panel for a workspace. Called by the
    delete-source cascade (api/server.py's delete_workspace_node) so a
    Mind Map / Study Guide / etc. built from a now-deleted source doesn't
    keep showing as if it still reflects the notebook's current sources.
    Returns the number of rows removed (0 is normal — most notebooks
    won't have every panel generated)."""
    with db.cursor() as cur:
        cur.execute("delete from workspace_panel_content where workspace_id = %s", (ws_id,))
        count = cur.rowcount
    if count:
        write_audit(user_id, "panel_content.clear_workspace", "workspace", ws_id, {"rows_deleted": count})
    return count
