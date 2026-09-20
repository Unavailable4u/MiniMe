-- 0009_code_file_versions.sql
--
-- W1.1 (MiniMe Build Workbench plan, step 16 follow-up): version
-- history + optimistic conflict detection for workspace_code_files
-- (migration 0006). Today every write -- a person's own Save via
-- api/routes/code.py's PUT, and the tier-3 pipeline's batch regen via
-- api/task_runner.py's _write_code_files() -- is a blind last-write-
-- wins upsert with zero history: chat regenerating a file while it's
-- open in the editor silently clobbers a hand edit, and the reverse
-- (Save landing while a regen is mid-flight) silently clobbers the
-- newer AI output, with no way to recover either side. See
-- eo/workspace_code_files.py's write_file()/write_files() docstrings
-- (as of migration 0006/patch 8) for the "no version history,
-- deliberately out of scope" note this migration removes.
--
-- Two changes:
--
--   1. workspace_code_files gets a `version` integer column, default 1
--      (so every already-saved file becomes "version 1, no history"
--      the moment this runs -- there's nothing to backfill into
--      workspace_code_file_versions for content that predates this
--      migration). write_file() now accepts an optional base_version;
--      when it's given and doesn't match the row's current version, it
--      raises VersionConflictError and api/routes/code.py's PUT route
--      turns that into a 409 carrying the current file, instead of
--      silently overwriting. write_files() (the pipeline's batch path)
--      is unchanged in that respect -- it still always wins, same as
--      before this migration, since a tier-3 regen has no "base
--      version" a person edited against -- but it now bumps `version`
--      and snapshots the content it's about to overwrite first, so
--      what used to be lost forever is at least recoverable from
--      history.
--
--   2. New table workspace_code_file_versions: one row per PAST
--      version of a file. The live/current version is never duplicated
--      in here -- it only ever lives in workspace_code_files itself --
--      a row lands here at the moment it's about to be superseded by a
--      new write. `source` records what produced the version being
--      replaced: `user` (a direct Save), `pipeline` (a tier-3 regen),
--      `proposal` (reserved for W5.x's code_editor agent, once a
--      reviewed edit's `resolve` writes through this same path), or
--      `restore` (a History-panel restore -- see
--      eo/workspace_code_files.py's restore_version() docstring for
--      why that creates a NEW version instead of rewriting the past,
--      same "history is append-only" posture audit_log and
--      llm_call_log (migration 0008) already take elsewhere in this
--      schema). Pruned to the most recent 30 rows per (workspace_id,
--      file_path) after every write that adds one -- see
--      _MAX_HISTORY_VERSIONS in eo/workspace_code_files.py -- so this
--      table stays bounded per file regardless of how long a workspace
--      has been iterated on.
--
-- Same "one row per key, RLS policy copied from the sibling table,
-- RLS + GRANT in the SAME migration" shape workspace_code_files itself
-- used in migration 0006 -- see migration 0007's own header comment
-- for exactly the bug that splitting a new table's RLS policy from its
-- GRANT across two separate migrations caused last time (0006 -> 0007:
-- every query rejected at the privilege check, before RLS was ever
-- evaluated). Not repeating that here.
--
-- HOW TO APPLY: run as the `postgres` role (same as every migration
-- before this one, and required to GRANT on a table `minime_app`
-- doesn't own) -- `psql "$DATABASE_URL" -f
-- migrations/0009_code_file_versions.sql`. Uses ADD COLUMN IF NOT
-- EXISTS / CREATE TABLE IF NOT EXISTS throughout, so it's safe to run
-- more than once. No app restart needed -- same "Postgres checks
-- privileges/RLS per-query, not per-connection" note migration 0007
-- already makes.

alter table workspace_code_files
    add column if not exists version integer not null default 1;

comment on column workspace_code_files.version is
    'Current live version number for this file. Bumped by every '
    'successful write (write_file()/write_files()/restore_version() in '
    'eo/workspace_code_files.py), never reset, never reused. The '
    'content that was live AT a past version lives in '
    'workspace_code_file_versions, not here -- this row is always the '
    'latest, by definition.';

create table if not exists workspace_code_file_versions (
    workspace_id  text not null references workspaces(id) on delete cascade,
    file_path     text not null,
    version       integer not null,
    content       text not null default '',
    updated_at    timestamptz,
    updated_by    text,
    source        text not null default 'user'
                  check (source in ('user', 'pipeline', 'proposal', 'restore')),
    primary key (workspace_id, file_path, version)
);

comment on table workspace_code_file_versions is
    'One row per PAST version of a workspace_code_files row -- the '
    'live/current version is never duplicated in here, only in '
    'workspace_code_files itself. Written by '
    'eo/workspace_code_files.py''s write_file()/write_files()/'
    'restore_version() right before they overwrite a row that already '
    'existed (a brand-new file has no prior version to snapshot, so it '
    'never gets a row here). Read by get_file_history() (Patch W2.6''s '
    'History panel) and restore_version(). Pruned to the most recent '
    '30 rows per (workspace_id, file_path) after every insert -- see '
    '_MAX_HISTORY_VERSIONS in that module -- so this table stays '
    'bounded per file no matter how long a workspace has been edited.';

-- Primary key (workspace_id, file_path, version) already gives an
-- efficient index for both this table's own queries -- get_file_history()'s
-- "order by version desc" for one (workspace_id, file_path), and the
-- prune step's "keep the top 30 versions for this file" -- a B-tree
-- index scans just as cheaply in either direction, so no separate
-- index is needed on top of the PK.

alter table workspace_code_file_versions enable row level security;
alter table workspace_code_file_versions force row level security;

-- Straight copy of workspace_code_files_scope (migration 0006) with
-- the table name swapped, same reasoning as that policy's own header
-- comment: get_file_history() runs with trusted=True (no per-call
-- user_id) so the trusted branch is what lets normal reads through;
-- the membership/ownership branches cover write_file()/write_files()/
-- restore_version(), which do have a real user_id.
drop policy if exists workspace_code_file_versions_scope on workspace_code_file_versions;
create policy workspace_code_file_versions_scope on workspace_code_file_versions
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

-- select/insert for normal snapshot writes and history reads; delete
-- for the prune step (this table is NOT append-only the way audit_log/
-- llm_call_log are -- pruning the oldest rows past 30 is an expected,
-- routine part of every write, not an exceptional admin action). No
-- update, ever -- once a version row is written its content is a fact
-- about the past and nothing in this codebase should ever rewrite it;
-- same "insurance against a future bug silently rewriting history"
-- reasoning migration 0008 documents for llm_call_log, minus the
-- delete restriction that table doesn't need and this one does.
grant select, insert, delete on workspace_code_file_versions to minime_app;
revoke update on workspace_code_file_versions from minime_app;
