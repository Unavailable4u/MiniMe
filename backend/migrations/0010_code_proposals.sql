-- 0010_code_proposals.sql
--
-- W5.1 (Build Workbench plan, step 197): the pending-review store
-- behind "select code / Add to chat / Edit mode" (W4.1/W4.2) turning
-- into a Copilot-style Keep/Undo diff (W5.3/W5.4). A proposal is
-- created by POST .../code/proposals (eo/code_proposals.py's
-- create_proposal()), sits at status='pending' with its edited
-- content computed but NOT yet written to workspace_code_files, and
-- only becomes a real file write when POST .../code/proposals/{id}/
-- resolve (resolve_proposal()) is called -- see that function's own
-- docstring for the two-phase "check every kept file's base_version
-- before writing any of them" contract. This table is the ONLY place
-- a proposed edit lives between those two moments; workspace_code_files
-- itself is untouched until resolve.
--
-- Same "one row per key, per workspace" shape as workspace_code_files
-- (migration 0006) and workspace_code_file_versions (migration 0009),
-- except keyed by a generated `id` rather than (workspace_id, some
-- natural key) -- a workspace can have several proposals pending at
-- once (one per file being edited, per W5.4's "one active proposal per
-- file" rule enforced at the eo/route layer, not by this schema), so
-- there's no natural composite key the way a single file's own path
-- is one for workspace_code_files.
--
-- Columns, matching the plan's own §5 W5.1 schema line exactly:
--   id            -- generated, see id shape below.
--   workspace_id  -- same FK-with-cascade as every other workspace-
--                    scoped table in this schema.
--   session_id    -- the chat session that produced this proposal, so
--                    a proposal card (W5.4) can be traced back to the
--                    chat turn that made it. Nullable -- same "a
--                    handful of callers don't have one" reasoning
--                    migration 0008's llm_call_log.session_id already
--                    documents; a curl-level create-then-resolve test
--                    (this migration's own "Done when") has no real
--                    chat session behind it.
--   created_by    -- the acting user_id at propose time (always
--                    present -- api/routes/code_edit.py's require_auth
--                    dependency guarantees this, unlike session_id).
--   instruction   -- the person's own edit request, verbatim, exactly
--                    as typed into Edit mode (W4.2) -- what
--                    generate_edit() (eo/code_proposals.py) was asked
--                    to do, kept alongside the result so a later
--                    reviewer (or W5.4's proposal card) can see the
--                    request next to what it produced.
--   status        -- see the CHECK constraint below for the closed set
--                    and _VALID_STATUSES in eo/code_proposals.py for
--                    the single source of truth it's kept in lockstep
--                    with by hand -- same small/stable/"not worth a
--                    DB-side derivation" reasoning
--                    workspace_code_file_versions.source (migration
--                    0009) already documents for itself.
--   files         -- jsonb array, one entry per file this proposal
--                    touches: {path, op, base_version, base_hash,
--                    original, proposed}. base_version/base_hash/
--                    original are a SNAPSHOT of workspace_code_files
--                    taken at propose time (see
--                    eo/code_proposals.py:_build_files_payload()'s own
--                    docstring) -- resolve_proposal() re-reads the
--                    LIVE row from workspace_code_files at resolve
--                    time and compares against base_version, it never
--                    trusts this column's own copy as still-current.
--   summary       -- one-line, model-authored (or, until W5.2 lands,
--                    stub-authored) description of what the edit did,
--                    for W5.4's proposal card and the pending tray.
--   refs          -- jsonb array, the caller's OWN ref shape
--                    (frontend/app/lib/workbench/codeContext.js's
--                    `{id, kind, path, fromLine, toLine, snippet,
--                    hash, provider, ...}`) verbatim, unvalidated
--                    beyond what eo/code_proposals.py itself checks
--                    (kind/path). Stored opaquely on purpose -- W5.1
--                    only needs `kind`/`path` out of a ref to decide
--                    which files to read; nothing here needs to keep
--                    this column's shape in lockstep with the
--                    frontend's own ref model as it grows in later
--                    steps (W6.x's `element`/`error` kinds add more
--                    fields to the SAME ref shape without this table
--                    ever needing a migration for it).
--   model_meta    -- jsonb, free-form (token counts, model id, retry
--                    count once W5.2's real agent lands; {"error":...}
--                    for a status='failed' row today). No CHECK/shape
--                    constraint on purpose -- same "whatever the
--                    generator wants to record" latitude
--                    llm_call_log's own free-standing columns don't
--                    need here since this is a single opaque jsonb
--                    blob, not queried columns.
--   created_at    -- set once, by create_proposal(), never updated.
--   resolved_at   -- null until resolve_proposal() transitions this
--                    row out of 'pending'; set exactly once, at the
--                    same moment `status` stops being 'pending'.
--
-- id shape: `prop_<12 hex chars>` (eo/code_proposals.py-generated,
-- like correction_candidates.py's own `corr_<10 hex chars>` candidate
-- ids), NOT a DB-generated uuid -- create_proposal() needs the id
-- available before its own INSERT (to pass to generate_edit() as part
-- of a future audit/tracing hook, and to build the CODE_PROPOSAL_READY
-- event payload from the same value the INSERT used) rather than
-- reading a `returning id` back first. Stored as `text`, not `uuid`,
-- for exactly that reason -- there is no gen_random_uuid() default to
-- rely on here the way chat_messages/llm_call_log's own uuid `id`
-- columns do.
--
-- RLS: straight copy of workspace_code_files_scope (migration 0006)
-- with the table name swapped -- list_proposals()/get_proposal() run
-- with trusted=True (no per-call user_id, same reasoning
-- get_file_history() gives for itself: the route layer already gates
-- membership of ws_id via _require_workspace() before either function
-- is ever reached), so the trusted branch is what lets normal reads
-- through; the membership/ownership branches cover create_proposal()/
-- resolve_proposal(), which do have a real user_id (created_by /
-- the resolving reviewer, respectively).
--
-- Gotcha (0006 -> 0007's own bug, not repeating it here): RLS policy
-- and GRANT both live in THIS file.
--
-- HOW TO APPLY: run as the `postgres` role (same as every migration
-- before this one) -- `psql "$DATABASE_URL" -f
-- migrations/0010_code_proposals.sql`. Uses IF NOT EXISTS / CREATE
-- TABLE IF NOT EXISTS throughout, so it's safe to run more than once.
-- No app restart needed -- same per-query privilege/RLS check note
-- migration 0007/0009 already make.

create table if not exists workspace_code_proposals (
    id            text primary key,
    workspace_id  text not null references workspaces(id) on delete cascade,
    session_id    text,
    created_by    text not null,
    instruction   text not null,
    status        text not null default 'pending'
                  check (status in ('pending', 'accepted', 'rejected', 'partial', 'stale', 'failed')),
    files         jsonb not null default '[]'::jsonb,
    summary       text,
    refs          jsonb not null default '[]'::jsonb,
    model_meta    jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    resolved_at   timestamptz
);

comment on table workspace_code_proposals is
    'W5.1: pending/resolved LLM-proposed code edits for the Build '
    'workbench''s review flow -- see eo/code_proposals.py for read/'
    'write access. A row here never implies workspace_code_files was '
    'written; only resolve_proposal() (status leaving ''pending'') '
    'ever writes real file content, via workspace_code_files.'
    'write_file(source=''proposal'').';

alter table workspace_code_proposals enable row level security;
alter table workspace_code_proposals force row level security;

drop policy if exists workspace_code_proposals_scope on workspace_code_proposals;
create policy workspace_code_proposals_scope on workspace_code_proposals
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

-- Serves both list_proposals() shapes (GET .../code/proposals and GET
-- .../code/proposals?status=pending) -- a leading workspace_id column
-- covers the unfiltered "every proposal in this workspace" query too,
-- just without status narrowing the scan; created_at desc trailing
-- matches list_proposals()'s own "most recent first" ORDER BY so
-- neither query needs a separate sort step.
create index if not exists workspace_code_proposals_workspace_status_idx
    on workspace_code_proposals (workspace_id, status, created_at desc);

-- Same "exactly the DML it needs" posture as every GRANT in this
-- schema -- no delete. A proposal is a permanent, status-transitioned
-- record (pending -> accepted/rejected/partial/stale/failed); nothing
-- in eo/code_proposals.py ever removes one, same append-only-in-
-- practice posture audit_log/llm_call_log take for themselves,
-- expressed here as simply never granting delete in the first place
-- rather than granting-then-revoking it.
grant select, insert, update on workspace_code_proposals to minime_app;
