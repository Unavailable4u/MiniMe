-- 0005_add_panel_content_source.sql
--
-- Patch 4: workspace_panel_content rows don't currently record HOW a
-- panel got its content -- a manual paste-and-Load (api/routes/
-- workspace_data.py's put_workspace_panel_content) and an automatic
-- chat-to-panel write (eo/panel_content.py's write_panel_from_role(),
-- patch 2/3) both just stamp `updated_by` with the same owner_id and
-- overwrite `content` the same way, so there's no way to tell them
-- apart after the fact -- e.g. to warn "this panel was auto-filled by
-- chat, edit and save to keep your own version" before a person's next
-- manual edit silently clobbers it, or vice versa.
--
-- This adds a `content_source` column with exactly two allowed values:
-- 'manual' (the existing paste-and-Load path) and 'chat' (the
-- write_panel_from_role() direct-write path). Existing rows predate
-- this distinction entirely -- every one of them was, by definition,
-- written by the only path that existed at the time (paste-and-Load),
-- so they backfill to 'manual', not NULL; NULL would just move the
-- "which one is this" question one level down instead of answering it.
--
-- HOW TO APPLY: run this file's contents once against the project's
-- Supabase Postgres instance (SQL editor, or `psql "$DATABASE_URL" -f
-- migrations/0005_add_panel_content_source.sql`). Additive and
-- backward-compatible -- IF NOT EXISTS / DEFAULT together mean this is
-- safe to run more than once and existing rows never need a separate
-- backfill step.

alter table workspace_panel_content
    add column if not exists content_source text not null default 'manual';

alter table workspace_panel_content
    drop constraint if exists workspace_panel_content_content_source_check;

alter table workspace_panel_content
    add constraint workspace_panel_content_content_source_check
    check (content_source in ('manual', 'chat'));

comment on column workspace_panel_content.content_source is
    'How this panel''s current content was last written: ''manual'' for '
    'a person''s own paste-and-Load save (api/routes/workspace_data.py''s '
    'put_workspace_panel_content, eo/panel_content.py''s set_content() '
    'called with its default content_source), or ''chat'' for an '
    'automatic direct-write from a finished chat turn (eo/panel_content.py''s '
    'write_panel_from_role(), patch 2/3). Both paths already stamp the '
    'same updated_by/updated_at -- this column is the piece that was '
    'missing to tell the two apart. Existing rows backfilled to '
    '''manual'' since paste-and-Load was the only write path that '
    'existed before this column was added.';
