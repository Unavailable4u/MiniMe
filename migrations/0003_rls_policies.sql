-- 0003_rls_policies.sql
--
-- A1 — real per-user RLS. eo/db.py's original docstring (see git blame
-- on that file) documented this table's-eye view of where things stood
-- after 8.2: RLS was enabled on every table but had no policies, and
-- the only thing stopping one user's queries from reading another
-- user's rows was every store module's own "WHERE owner_id = %s"
-- discipline in Python. This migration is what closes that gap at the
-- database layer.
--
-- THE PART THAT ACTUALLY MATTERS — READ THIS BEFORE RUNNING:
-- Writing RLS policies does nothing on its own. DATABASE_URL currently
-- connects as the `postgres` role (confirmed in eo/db.py's own
-- docstring and env(example).txt — the Supabase Session Pooler string
-- defaults to it). Postgres never enforces RLS against a superuser or
-- against a table's owning role, REGARDLESS of FORCE ROW LEVEL
-- SECURITY. `postgres` on a Supabase project is both. So if you only
-- run this file and leave DATABASE_URL pointed at `postgres`, every
-- policy below is inert — queries keep running exactly as
-- unrestricted as before, silently. The role-creation block below and
-- the DATABASE_URL rotation in "HOW TO APPLY" are not optional
-- extras, they're the actual fix; the policies are just rules that
-- fix applies to.
--
-- WHY THE POLICIES AREN'T ALL THE SAME SHAPE:
-- The obvious version of this migration is "every table gets
-- `USING (owner_id = current_setting('app.current_user_id'))`" and
-- stop there. That's wrong for this schema specifically, for two
-- reasons found by reading the actual call sites, not by guessing:
--
--   1. Workspaces aren't always single-owner. chat_workspace.py has a
--      real joint-ownership model: owner_id can be NULL while the
--      workspace is "joint," in which case access runs through
--      workspace_members (role: owner/partner/moderator/viewer) and
--      workspace_owner_votes instead. A policy that only checks
--      workspaces.owner_id would lock every partner out of a joint
--      workspace's own rows.
--
--   2. Chat sharing depends on this too. chat_store.py's
--      resolve_chat_access() deliberately reads a chat row that does
--      NOT belong to the requesting user — that's the whole point of
--      the function, checking whether a non-owner has access via
--      workspace membership before deciding to grant it. A flat
--      `owner_id = current_setting(...)` policy on `chats` would make
--      that query return zero rows for a legitimate workspace
--      partner, and chat sharing between workspace members would
--      silently stop working the moment this migration ran.
--
-- So every table below that's reachable through a shared workspace
-- gets an `OR ... IN (SELECT workspace_id FROM workspace_members
-- WHERE user_id = current_setting(...))` branch alongside the direct
-- owner check. Tables that are genuinely always single-owner (batches,
-- user_integrations) don't get that branch — there's no sharing
-- mechanism for them in the current code, and adding one speculatively
-- would just be a wider, unearned hole.
--
-- WHY SOME POLICIES ALSO CHECK app.trusted_internal:
-- A handful of functions are, by their own docstrings, intentionally
-- actor-free: chat_workspace.list_notify_targets ("Deliberately
-- auth-free... there's no actor_id to check access against"),
-- chat_workspace.active_stages_precheck / auto_partial_promote
-- (background trigger, no acting user), audit_log.list_for_target
-- ("No access check here... callers decide who's allowed to ask"),
-- panel_content.get_content/list_content (no user_id param — the
-- caller already checked workspace access before calling). These
-- can't satisfy a current_user_id check because there genuinely isn't
-- one for that call. eo/db.py's cursor(trusted=True) sets
-- app.trusted_internal for exactly these call sites (see that file's
-- updated docstring) and each policy below OR's it in. This is a
-- narrow, explicit, auditable exception — grep eo/ for
-- `trusted=True` any time you want the full list of what it covers.
-- It is NOT a general bypass: cursor() called with neither user_id
-- nor trusted leaves both settings unset, which means NO row is
-- visible on any owner-scoped table — the failure mode for "forgot to
-- pass identity" is an empty result, not full access.
--
-- HOW TO APPLY (Supabase SQL Editor — same workflow as 0001/0002):
--   1. Generate a real password and save it in a password manager
--      first, before touching this file:
--        openssl rand -base64 24
--      (or any password generator — 20+ random chars, no need to
--      remember it, you're about to paste it straight into your .env)
--   2. Below, find the line:
--        create role minime_app login password 'CHANGE_ME_STRONG_PASSWORD';
--      and replace CHANGE_ME_STRONG_PASSWORD with the password from
--      step 1, in your LOCAL copy of this file only.
--   3. Paste the whole file into Supabase's SQL Editor and run it —
--      same as every migration before this one. It runs as
--      `postgres` (whatever role the SQL Editor uses), which is
--      required here: creating a role and granting privileges are
--      admin actions minime_app itself won't be able to do once it
--      exists.
--   4. IMPORTANT — after running it, undo step 2 in your local file
--      (put CHANGE_ME_STRONG_PASSWORD back, or just re-copy this file
--      fresh from git) before you commit/push anything. The real
--      password belongs in your .env and your password manager, never
--      in a file that ends up in git history.
--   5. In Supabase's Connection String page, build a new Session
--      Pooler URL using user `minime_app` and the password from step
--      1 instead of `postgres`. Put THAT string in DATABASE_URL in
--      your real .env (and in whatever your deploy target's env vars
--      are) — not in env(example).txt, which stays a template.
--   6. Restart the app. From this point on, every request runs as
--      minime_app, which cannot bypass RLS — the policies below are
--      now actually load-bearing.
--   7. Keep using the old admin connection (Supabase SQL Editor, or a
--      `postgres`-role DATABASE_URL) for running FUTURE migrations —
--      minime_app deliberately doesn't get DDL rights (see GRANT list
--      below), so it can't run migrations, only the app's normal
--      reads/writes.
--
-- Safe to run more than once: role creation and grants are guarded,
-- and `create policy` is wrapped in DROP POLICY IF EXISTS so re-runs
-- replace rather than error. Re-running it will NOT reset
-- minime_app's password back to CHANGE_ME_STRONG_PASSWORD (the `if
-- not exists` guard skips role creation entirely once it exists) —
-- if you ever need to rotate the password, do that separately with
-- `alter role minime_app password '...'`, not by re-running this file.

-- ---------------------------------------------------------------------
-- 1. The low-privilege role the app actually connects as (see above —
--    this is the part that makes everything below not a no-op).
--    Edit the password on the line below before running (step 2
--    above), then edit it back before committing (step 4).
-- ---------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'minime_app') then
        create role minime_app login password 'iLz91G5ysUFMDrBPRv8hZAu3';
    end if;
end
$$;

-- Explicitly NOT granting BYPASSRLS, NOT making it a superuser, and
-- NOT making it own any table (ownership alone would also bypass RLS
-- — see header). Table ownership stays with `postgres`; minime_app
-- gets exactly the DML it needs and nothing else.
grant usage on schema public to minime_app;
grant select, insert, update, delete on
    chats, chat_messages, workspaces, workspace_members,
    workspace_owner_votes, workspace_panel_content, batches,
    batch_members, user_integrations, audit_log
    to minime_app;
-- audit_log is meant to be append-only from the app's perspective —
-- entries get written and read, never edited or removed by app code
-- (eo/audit_log.py has no update/delete function at all). Revoke those
-- two explicitly so a future bug can't silently start rewriting
-- history; if a real "redact an audit entry" admin feature ever gets
-- built, grant it back deliberately then.
revoke update, delete on audit_log from minime_app;
-- Every table uses a driver-generated id or a text primary key
-- assigned in Python (new_chat_id() etc.) except chat_messages, which
-- defaults its id via gen_random_uuid() — needs sequence/function
-- usage, not a real sequence grant, so nothing extra required there.

-- ---------------------------------------------------------------------
-- 2. Enable + FORCE RLS on every table. FORCE matters here specifically
--    because minime_app does NOT own these tables (postgres does) —
--    without FORCE, a non-owner role is already subject to RLS by
--    default, so FORCE is technically redundant for minime_app today.
--    It's included anyway as insurance against a future "just for
--    this one migration" ALTER TABLE ... OWNER TO minime_app that
--    would otherwise silently turn RLS back off for that table.
-- ---------------------------------------------------------------------
alter table chats                    enable row level security;
alter table chats                    force row level security;
alter table chat_messages            enable row level security;
alter table chat_messages            force row level security;
alter table workspaces               enable row level security;
alter table workspaces               force row level security;
alter table workspace_members        enable row level security;
alter table workspace_members        force row level security;
alter table workspace_owner_votes    enable row level security;
alter table workspace_owner_votes    force row level security;
alter table workspace_panel_content  enable row level security;
alter table workspace_panel_content  force row level security;
alter table batches                  enable row level security;
alter table batches                  force row level security;
alter table batch_members            enable row level security;
alter table batch_members            force row level security;
alter table user_integrations        enable row level security;
alter table user_integrations        force row level security;
alter table audit_log                enable row level security;
alter table audit_log                force row level security;

-- ---------------------------------------------------------------------
-- 3. Policies. One helper pattern repeated per table:
--      current_setting('app.current_user_id', true) -- the `true`
--      makes it return NULL instead of raising when unset, matching
--      cursor()'s "neither user_id nor trusted passed" case.
-- ---------------------------------------------------------------------

-- workspaces: owner directly, OR any current member (covers joint
-- workspaces and every role — viewer/moderator/partner/owner-row-less
-- owner all still need to at least SELECT their own workspace).
drop policy if exists workspaces_scope on workspaces;
create policy workspaces_scope on workspaces
    for all
    using (
        owner_id::text = current_setting('app.current_user_id', true)
        or id in (
            select workspace_id from workspace_members
            where user_id::text = current_setting('app.current_user_id', true)
        )
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- workspace_members: visible to any member of that workspace (a
-- partner needs to see the roster, not just their own row), plus the
-- trusted-internal escape hatch for list_notify_targets.
drop policy if exists workspace_members_scope on workspace_members;
create policy workspace_members_scope on workspace_members
    for all
    using (
        workspace_id in (
            select workspace_id from workspace_members wm2
            where wm2.user_id::text = current_setting('app.current_user_id', true)
        )
        or user_id::text = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- workspace_owner_votes: same membership rule — any partner can see
-- the current ballot, not just their own vote (get_vote_status shows
-- everyone's votes to any partner).
drop policy if exists workspace_owner_votes_scope on workspace_owner_votes;
create policy workspace_owner_votes_scope on workspace_owner_votes
    for all
    using (
        workspace_id in (
            select workspace_id from workspace_members
            where user_id::text = current_setting('app.current_user_id', true)
        )
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- chats: owner directly, OR a member of the chat's workspace as long
-- as the chat isn't private — this is what keeps
-- resolve_chat_access() working (see header). is_private always wins
-- even for a fellow workspace member, matching that function's own
-- documented rule.
drop policy if exists chats_scope on chats;
create policy chats_scope on chats
    for all
    using (
        owner_id::text = current_setting('app.current_user_id', true)
        or (
            not coalesce(is_private, false)
            and workspace_id is not null
            and workspace_id in (
                select workspace_id from workspace_members
                where user_id::text = current_setting('app.current_user_id', true)
            )
        )
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- chat_messages: owner_id is denormalized onto this table already
-- (migration 0002's own design note) specifically so it wouldn't need
-- a join back to chats for scoping — same idea applies here. Shared
-- (non-private, workspace) chats still need the same OR-membership
-- branch as `chats` itself, via a join, since a shared chat's messages
-- have the ORIGINAL owner's owner_id, not the viewing partner's.
drop policy if exists chat_messages_scope on chat_messages;
create policy chat_messages_scope on chat_messages
    for all
    using (
        owner_id::text = current_setting('app.current_user_id', true)
        or chat_id in (
            select c.id from chats c
            join workspace_members wm on wm.workspace_id = c.workspace_id
            where not coalesce(c.is_private, false)
              and wm.user_id::text = current_setting('app.current_user_id', true)
        )
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- workspace_panel_content: no owner_id column at all (see
-- panel_content.py's create-table comment) — always scoped through
-- workspace membership. get_content/list_content run with
-- trusted=True (no user_id param on those two functions — see
-- header), so the trusted branch is what actually lets normal panel
-- reads through; the membership branch covers set_content/
-- delete_content/invalidate_for_nodes/clear_workspace, which do have
-- a real user_id.
drop policy if exists workspace_panel_content_scope on workspace_panel_content;
create policy workspace_panel_content_scope on workspace_panel_content
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

-- batches / batch_members: single-owner only in the current code (no
-- batch-sharing mechanism exists anywhere in memory_batch.py), so no
-- membership branch — adding one would be speculative, not something
-- an actual call site needs today.
drop policy if exists batches_scope on batches;
create policy batches_scope on batches
    for all
    using (owner_id::text = current_setting('app.current_user_id', true));

drop policy if exists batch_members_scope on batch_members;
create policy batch_members_scope on batch_members
    for all
    using (
        batch_id in (
            select id from batches
            where owner_id::text = current_setting('app.current_user_id', true)
        )
    );

-- user_integrations: single-owner, no sharing concept for a user's own
-- OAuth credentials — flat check, no trusted branch needed either
-- (every function in integrations.py takes a real user_id).
drop policy if exists user_integrations_scope on user_integrations;
create policy user_integrations_scope on user_integrations
    for all
    using (user_id::text = current_setting('app.current_user_id', true));

-- audit_log: two legitimate read shapes — "show me everything that
-- happened to me" (list_for_user, real user_id) and "show me
-- everything that happened to this target" (list_for_target, no
-- actor — runs trusted=True). No membership branch: an audit entry
-- about a workspace-wide action is still fetched via
-- list_for_target's trusted path, not by a partner querying their own
-- user_id, so it doesn't need one.
drop policy if exists audit_log_scope on audit_log;
create policy audit_log_scope on audit_log
    for all
    using (
        user_id::text = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

comment on role minime_app is
    'Application runtime role for MiniMe (migration 0003 / A1). Owns '
    'nothing, has no BYPASSRLS, no DDL rights — DML only, subject to '
    'RLS on every table. DATABASE_URL in the app''s real env must '
    'point here, not at `postgres`. Migrations still run as `postgres`.';