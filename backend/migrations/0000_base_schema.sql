-- 0000_base_schema.sql
--
-- WHAT THIS FILE IS. The schema that existed BEFORE migration 0001: the
-- statements that were run by hand in the Supabase SQL editor (Dashboard ->
-- SQL Editor) during the original build-out, "Part 8.2" through "Part 8.6" plus
-- the stage model and the panel-content table, collected here in the order
-- they were run. Migrations 0001 onward were then applied on top of this, also
-- by hand in the SQL editor.
--
-- WHY IT IS IN THE REPO. Migrations 0001..latest only make sense on top of
-- these tables (0001 alters workspace_panel_content, 0002 references chats,
-- 0003 writes policies on workspaces/workspace_members/..., 0010 references
-- workspaces). Before this file existed, nothing in the repo said where those
-- tables came from, and `scripts/public.sql` is a Supabase "context only"
-- dump (its own header says it is not meant to be run). Having the base here
-- means a scratch database for the real-database tests (B01) can be built by
-- applying every file in backend/migrations/ in filename order, with no
-- guessing.
--
-- DO YOU NEED TO RUN THIS ON THE LIVE SUPABASE PROJECT? No. It is already
-- applied there. It is also safe to re-run: every statement is idempotent
-- (`if not exists`, `drop constraint if exists` before `add constraint`), so
-- pasting the whole folder into the SQL editor is harmless.
--
-- WHAT IT DOES NOT CONTAIN.
--   * Tables created by migrations 0002 and later (chat_messages,
--     workspace_code_files, llm_call_log, workspace_code_file_versions,
--     workspace_code_proposals, workspace_code_findings, user_plans,
--     usage_ledger). Those live in their own numbered files.
--   * Any GRANT or RLS policy. Grants to the app role (`minime_app`) and the
--     row-level-security policies start at 0003. On the hand-built base the
--     `alter table ... enable row level security` lines below are the only
--     RLS; policies did not exist until 0003.
--   * Supabase's own `auth` schema. `auth.users` is owned by Supabase; on a
--     plain Postgres scratch database it has to be stubbed first (see the
--     "Real-database tests" section of the root README).
--
-- ONE RECONSTRUCTED STATEMENT -- READ THIS. The live database has a column,
-- workspaces.active_stages (jsonb, default '["note"]'), that appears in
-- scripts/public.sql but in NO recorded SQL: it was added in the Supabase
-- editor without the statement being saved. The `add column` near the end of
-- this file is rebuilt from the column definition in that dump so a scratch
-- database matches the live one. If you remember a different original
-- statement, replace it.
--
-- KNOWN DRIFT. scripts/public.sql was captured a few migrations behind (it has
-- no user_plans / usage_ledger, i.e. 0012). Re-capture it after applying new
-- migrations if you rely on it as a reference.


-- ============================================================
-- Part 8.2 -- core schema: workspaces, workspace_members, chats, batches
-- ============================================================
create table if not exists workspaces (
    id          text primary key,
    name        text not null,
    owner_id    uuid not null references auth.users(id) on delete cascade,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists idx_workspaces_owner on workspaces(owner_id);

-- Collaborators on a workspace (roles arrive in 8.3 / 8.4 below).
create table if not exists workspace_members (
    workspace_id  text not null references workspaces(id) on delete cascade,
    user_id       uuid not null references auth.users(id) on delete cascade,
    added_at      timestamptz not null default now(),
    primary key (workspace_id, user_id)
);

create table if not exists chats (
    id                text primary key,
    title             text not null default 'New Chat',
    owner_id          uuid not null references auth.users(id) on delete cascade,
    workspace_id      text references workspaces(id) on delete set null,
    is_private        boolean not null default false,
    tags              text[] not null default '{}',
    template_id       text,
    linked_chat_ids   text[] not null default '{}',
    messages          jsonb not null default '[]'::jsonb,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index if not exists idx_chats_owner on chats(owner_id);
create index if not exists idx_chats_workspace on chats(workspace_id);
create index if not exists idx_chats_updated_at on chats(updated_at desc);
create index if not exists idx_chats_template on chats(template_id) where template_id is not null;
create index if not exists idx_chats_tags on chats using gin(tags);

-- Mutual-membership groups (memory_batch.py).
create table if not exists batches (
    id          text primary key,
    name        text not null,
    owner_id    uuid not null references auth.users(id) on delete cascade,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists batch_members (
    batch_id  text not null references batches(id) on delete cascade,
    chat_id   text not null references chats(id) on delete cascade,
    primary key (batch_id, chat_id)
);

create index if not exists idx_batches_owner on batches(owner_id);
create index if not exists idx_batch_members_chat on batch_members(chat_id);

-- RLS enabled with no policies: only the service_role key can read/write
-- until 0003 adds real per-user policies.
alter table workspaces enable row level security;
alter table workspace_members enable row level security;
alter table chats enable row level security;
alter table batches enable row level security;
alter table batch_members enable row level security;


-- ============================================================
-- Part 8.3 -- collaborator roles
-- ============================================================
alter table workspace_members
    add column if not exists role text not null default 'viewer'
        check (role in ('viewer', 'editor'));


-- ============================================================
-- Part 8.4 -- role hierarchy (moderator/partner), owner voting, attribution
-- ============================================================
-- 1. owner_id becomes nullable: a workspace can be "joint" (no single owner).
alter table workspaces alter column owner_id drop not null;

-- 2. Widen the role vocabulary.
alter table workspace_members drop constraint if exists workspace_members_role_check;
alter table workspace_members
    add constraint workspace_members_role_check
    check (role in ('viewer', 'editor', 'moderator', 'partner'));

-- 3. A moderator can toggle attribution visibility only if this is true.
alter table workspace_members
    add column if not exists can_toggle_attribution boolean not null default false;

-- 4. Does the viewer/editor tier see who-wrote-what?
alter table workspaces
    add column if not exists show_attribution boolean not null default true;

-- 5. Open ballots for electing a new owner while a workspace is joint.
--    vote_target = NULL means "stay joint".
create table if not exists workspace_owner_votes (
    workspace_id  text not null references workspaces(id) on delete cascade,
    voter_id      uuid not null,
    vote_target   uuid,
    cast_at       timestamptz not null default now(),
    primary key (workspace_id, voter_id)
);

alter table workspace_owner_votes enable row level security;


-- ============================================================
-- Part 8.6 -- audit log (append-only by convention; see 0003 for the grants
-- that later made it append-only for the app role)
-- ============================================================
create table if not exists audit_log (
    id           bigserial primary key,
    user_id      uuid not null,          -- the ACTING user, not a target
    action       text not null,
    target_type  text not null,
    target_id    text not null,          -- text, not an fk: the log must outlive its targets
    detail       jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

create index if not exists audit_log_user_id_idx on audit_log (user_id, created_at desc);
create index if not exists audit_log_target_idx on audit_log (target_type, target_id, created_at desc);


-- ============================================================
-- Part 8.5 -- third-party integrations: per-user OAuth credential storage
-- (access_token / refresh_token are stored Fernet-encrypted by
-- eo/integrations.py, never plaintext)
-- ============================================================
create table if not exists user_integrations (
    id              bigserial primary key,
    user_id         uuid not null,
    provider        text not null,
    account_label   text,
    access_token    text not null,
    refresh_token   text,
    scope           text,
    expires_at      timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (user_id, provider)
);

create index if not exists user_integrations_user_id_idx on user_integrations (user_id);

alter table user_integrations enable row level security;


-- ============================================================
-- Stage model on workspaces
-- ============================================================
alter table workspaces add column if not exists stage text not null default 'note';
alter table workspaces add column if not exists stage_history jsonb not null default '[]'::jsonb;

-- (The original had no `drop ... if exists`, so it failed on a second run.)
alter table workspaces drop constraint if exists workspaces_stage_check;
alter table workspaces
    add constraint workspaces_stage_check
    check (stage in ('note','research','plan','build','test','growth'));

-- RECONSTRUCTED from scripts/public.sql -- see the header of this file.
alter table workspaces add column if not exists active_stages jsonb not null default '["note"]'::jsonb;


-- ============================================================
-- workspace_panel_content -- backs eo/panel_content.py: generic persistence
-- for the "paste the chat's output into a box" panels, one row per
-- (workspace_id, panel_key), last-write-wins. Migration 0001 alters this.
-- ============================================================
create table if not exists workspace_panel_content (
    workspace_id  text not null references workspaces(id) on delete cascade,
    panel_key     text not null,
    content       text not null default '',
    updated_at    timestamptz not null default now(),
    updated_by    text,
    primary key (workspace_id, panel_key)
);

create index if not exists idx_workspace_panel_content_ws
    on workspace_panel_content (workspace_id);
