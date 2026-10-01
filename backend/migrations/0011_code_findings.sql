-- 0011_code_findings.sql
--
-- W8.3a (Build Workbench plan, backend half of the Problems panel): a
-- per-file store for the findings the tier-3 pipeline already produces
-- -- sandbox_tester's failing test runs (KEYS["test_results"]) and
-- security_scanner/static_scan's Gitleaks + Semgrep results
-- (KEYS["security_scan_results"]) -- normalised into one shape the
-- editor can draw as diagnostics: {path, line, severity, message,
-- source}. eo/code_findings.py builds the rows from those two bus keys;
-- api/task_runner.py's _write_code_files() stores them right after the
-- files themselves are saved, BEFORE the CODE_FILE_UPDATED event, so the
-- frontend's existing refetch-on-that-event picks them up with no new
-- event type (plan section 7, gotcha 2). GET .../code/findings reads
-- them back.
--
-- Shape of a row, and why:
--   workspace_id, file_path -- the file the finding is about. Together
--       with `seq` they form the primary key. There is deliberately NO
--       generated id: an identity/serial column needs sequence
--       privileges handed to minime_app as well, one more thing to
--       forget in a GRANT (see the 0006 -> 0007 bug below), and nothing
--       ever addresses a single finding -- a file's findings are always
--       replaced as a whole set (delete every row for the file, insert
--       the new ones), so a per-file ordinal is all the key needs.
--   seq -- 0-based position within that file's finding list, in the
--       order eo/code_findings.py produced them.
--   line -- 1-based line the finding points at, or NULL when the source
--       data carried no usable line (a test assertion that failed
--       inside the appended generated tests, or a scan description with
--       no "line N" in it). NULL means "file-level problem", not "line
--       0".
--   severity -- 'error' | 'warning' | 'info', i.e. @codemirror/lint's
--       own severity names, so the frontend never has to translate
--       the scanners' critical/moderate/minor itself.
--   message, source -- display text, and which producer it came from
--       ('sandbox_test', 'gitleaks', 'semgrep', or the generic
--       'security_scan' when the LLM summarising pass dropped the
--       tool name).
--   file_version -- workspace_code_files.version of the file AT THE TIME
--       the finding was produced. A person's own Save bumps that version
--       and shifts line numbers, so a finding whose file_version no
--       longer matches the live file's version is known-stale; the
--       frontend can drop or grey those instead of drawing them on the
--       wrong line. Nullable only because a write that returns no
--       version has nothing to record.
--   created_at -- when the finding was stored.
--
-- Lifecycle: the foreign key to workspace_code_files(workspace_id,
-- file_path) is ON DELETE CASCADE, so deleting a file (or a folder
-- subtree), or moving it -- move_path() in eo/workspace_code_files.py
-- deletes the old row and inserts a new one -- drops its findings with
-- it instead of leaving orphans that the Problems panel would keep
-- showing for a path that no longer exists. ON UPDATE CASCADE is
-- there so a future in-place rename can't be blocked by this FK.
-- (Foreign-key checks and cascades bypass row-level security by
-- design, so FORCE ROW LEVEL SECURITY below doesn't get in their way.)
--
-- RLS: straight copy of workspace_code_files_scope (migration 0006) with
-- the table name swapped, same as migrations 0009 and 0010 did for
-- their own tables. replace_findings() runs with a real user_id (the
-- membership/ownership branches); list_findings() runs trusted=True
-- because the route layer has already gated membership of ws_id via
-- _require_workspace().
--
-- Gotcha (0006 -> 0007's own bug, not repeating it): the RLS policy AND
-- the GRANT both live in THIS file. Without the GRANT every query is
-- rejected at the privilege check before RLS is ever evaluated.
--
-- HOW TO APPLY: run as the `postgres` role (same as every migration
-- before this one) -- `psql "$DATABASE_URL" -f
-- migrations/0011_code_findings.sql`. Uses IF NOT EXISTS / DROP POLICY
-- IF EXISTS throughout, so it's safe to run more than once. No app
-- restart needed -- Postgres checks privileges/RLS per-query, not
-- per-connection (migration 0007's own note).

create table if not exists workspace_code_findings (
    workspace_id  text not null,
    file_path     text not null,
    seq           integer not null check (seq >= 0),
    line          integer check (line is null or line >= 1),
    severity      text not null check (severity in ('error', 'warning', 'info')),
    message       text not null,
    source        text not null,
    file_version  integer,
    created_at    timestamptz not null default now(),
    primary key (workspace_id, file_path, seq),
    foreign key (workspace_id, file_path)
        references workspace_code_files (workspace_id, file_path)
        on delete cascade
        on update cascade
);

comment on table workspace_code_findings is
    'W8.3a: scan/test findings per code file, normalised to '
    '{line, severity, message, source} for the Build workbench''s '
    'Problems panel. Written by eo/code_findings.py''s '
    'replace_findings() (called from api/task_runner.py''s '
    '_write_code_files()), read by GET .../code/findings. A file''s '
    'findings are always replaced as a whole set, never edited '
    'row by row.';

comment on column workspace_code_findings.file_version is
    'workspace_code_files.version of the file when this finding was '
    'produced. A finding whose file_version differs from the live '
    'file''s version predates a later write and its line number may '
    'no longer be right.';

alter table workspace_code_findings enable row level security;
alter table workspace_code_findings force row level security;

drop policy if exists workspace_code_findings_scope on workspace_code_findings;
create policy workspace_code_findings_scope on workspace_code_findings
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

-- The primary key's leading (workspace_id, file_path) columns already
-- serve both queries this table gets: list_findings()'s "everything in
-- one workspace" scan and replace_findings()'s "delete these paths in
-- one workspace". No separate index needed.

-- select for the GET route, insert + delete for replace_findings()'s
-- delete-then-insert. No update, ever: a finding is never edited, only
-- replaced along with the rest of its file's set -- same "grant exactly
-- the DML it needs" posture as every other GRANT in this schema.
grant select, insert, delete on workspace_code_findings to minime_app;
