-- 0001_add_panel_content_source_node_ids.sql
--
-- Bug audit #2's real fix: workspace_panel_content rows don't currently
-- record which source nodes fed into a given generation, so the delete
-- cascade in api/server.py (delete_workspace_node) has no way to tell
-- which saved panels a given source actually touched -- its only option
-- was clearing every panel for the whole workspace on every single
-- source delete (eo/panel_content.py's clear_workspace()), including
-- panels that had nothing to do with the deleted source (and, as a side
-- effect, also nuking the manual-paste panels -- PRD/Architecture/
-- Schema/etc. -- that were never generated FROM sources in the first
-- place and shouldn't be touched by this cascade at all).
--
-- This is the first tracked migration in this repo. There is no
-- migration history before this one -- every prior schema change
-- (workspaces, chats, workspace_facts, workspace_panel_content itself,
-- graph_edges, node_summaries, etc.) was applied by hand against
-- Supabase and only ever documented after the fact in code comments
-- (see eo/db.py's module docstring, and panel_content.py's old
-- reference to a "part8_schema.sql" that was never actually committed).
-- Starting real migration tracking here so that stops happening --
-- every future schema change should get its own numbered .sql file in
-- this folder, applied in order, instead of living only in a comment.
--
-- HOW TO APPLY: run this file's contents once against the project's
-- Supabase Postgres instance (SQL editor, or `psql "$DATABASE_URL" -f
-- migrations/0001_add_panel_content_source_node_ids.sql`). It's an
-- additive, backward-compatible change -- IF NOT EXISTS guards make it
-- safe to run more than once, and every existing row simply gets NULL
-- in the new column (see semantics below), so nothing needs backfilling
-- for this to be safe to deploy.

alter table workspace_panel_content
    add column if not exists source_node_ids text[];

comment on column workspace_panel_content.source_node_ids is
    'Node ids (bare, not the "node:{ws_id}:{id}" prefixed vector id) that '
    'fed this panel''s last generation. NULL has a specific meaning: '
    'either "generated from the whole notebook at the time" (Regenerate '
    'run with no source_node_ids scope) or "not a source-generated panel '
    'at all" (the manual-paste panels: prd, architecture, schema, '
    'api_contract, devils_advocate, feasibility, wireframes, '
    'contradictions, extraction_manual, audit -- these are never written '
    'with a source_node_ids value and the delete cascade skips them '
    'entirely by panel_key, not by inspecting this column). A non-null '
    'array means "scoped to exactly these source nodes" -- see '
    'eo/panel_content.py''s GENERATED_PANEL_KEYS and invalidate_for_nodes().';
