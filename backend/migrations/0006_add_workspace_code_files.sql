-- 0006_add_workspace_code_files.sql
--
-- Master Guide V2 step 16 (T3), patch 8: the Code sub-tab's backend
-- persistence layer (eo/workspace_code_files.py). Same "one row per key,
-- per workspace, last-write-wins" shape workspace_panel_content already
-- has, keyed by file_path instead of panel_key -- see that module's own
-- header comment for why a codebase needs a different key shape than a
-- fixed panel allowlist.
--
-- No owner_id column, same as workspace_panel_content -- always scoped
-- through workspace membership/ownership, never directly by acting
-- user. RLS policy below is a straight copy of
-- workspace_panel_content_scope (migration 0003) with the table name
-- swapped, for the same reason: list_files()/get_file() run with
-- trusted=True (no per-call user_id), so the trusted branch is what
-- lets normal reads through; the membership/ownership branches cover
-- write_file()/delete_file(), which do have a real user_id.
--
-- HOW TO APPLY: run this file's contents once against the project's
-- Supabase Postgres instance (SQL editor, or `psql "$DATABASE_URL" -f
-- migrations/0006_add_workspace_code_files.sql`). Uses IF NOT EXISTS /
-- CREATE TABLE IF NOT EXISTS throughout, so it's safe to run more than
-- once.

create table if not exists workspace_code_files (
    workspace_id  text not null references workspaces(id) on delete cascade,
    file_path     text not null,
    content       text not null default '',
    language      text,
    updated_at    timestamptz,
    updated_by    text,
    primary key (workspace_id, file_path)
);

comment on table workspace_code_files is
    'Per-workspace persistence for the Code sub-tab (Build tab). One row '
    'per (workspace_id, file_path) -- see eo/workspace_code_files.py for '
    'read/write access, and eo/panel_content.py for the sibling pattern '
    'this table''s shape was copied from.';

alter table workspace_code_files enable row level security;
alter table workspace_code_files force row level security;

drop policy if exists workspace_code_files_scope on workspace_code_files;
create policy workspace_code_files_scope on workspace_code_files
    for all
    using (
        workspace_id in (
            select workspace_id from workspace_members
            where user_id::text = current_setting('app.current_user_id', true)
        )
        or workspace_id in (
            select id from workspaces
            where owner_id::text = current_setting('app.current_user_id', true)
        )
        or current_setting('app.trusted_internal', true) = 'true'
    );

create index if not exists workspace_code_files_workspace_id_idx
    on workspace_code_files (workspace_id);
