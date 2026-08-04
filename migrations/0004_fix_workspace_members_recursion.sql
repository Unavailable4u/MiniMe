-- 0004_fix_workspace_members_recursion.sql
--
-- Fixes a real bug in 0003: workspace_members_scope's own USING clause
-- queries workspace_members (aliased wm2) to check membership. But
-- that inner query is ITSELF subject to workspace_members_scope (RLS
-- applies to every query against the table, including ones issued
-- from inside another policy on the same table) -- so evaluating the
-- policy requires evaluating the policy requires evaluating the
-- policy, forever. Postgres detects this and raises:
--   psycopg2.errors.InvalidObjectDefinition: infinite recursion
--   detected in policy for relation "workspace_members"
--
-- This wasn't hypothetical -- it broke list_chats() and
-- list_workspaces() the moment DATABASE_URL switched to minime_app,
-- because chats_scope and workspaces_scope both subquery
-- workspace_members too (to check "is this user a member of this
-- chat's/workspace's workspace"), which hits the exact same
-- recursion. batches_scope never touches workspace_members, which is
-- why /api/batches kept working while /api/chats and /api/workspaces
-- 500'd -- consistent with the traceback that surfaced this.
--
-- THE FIX: move the membership check into a SECURITY DEFINER
-- function. A SECURITY DEFINER function runs with the privileges of
-- whoever OWNS the function (here: `postgres`, since that's who's
-- running this migration), not the caller (minime_app) -- and
-- `postgres` bypasses RLS entirely (same reason DATABASE_URL had to
-- stop using it in 0003). So the function's own internal SELECT
-- against workspace_members skips RLS, breaks the recursive chain,
-- and just returns a plain true/false. Every policy that needs a
-- membership check calls this function instead of subquerying
-- workspace_members inline.
--
-- HOW TO APPLY: same as every migration so far -- paste this whole
-- file into Supabase's SQL Editor (role: postgres, same as always)
-- and run it. No password/role edits needed this time, nothing to
-- change in this file before running it. Safe to run more than once.

-- ---------------------------------------------------------------------
-- 1. Helper functions. STABLE (not VOLATILE) since within one
--    statement the session vars they read don't change -- lets
--    Postgres's planner cache repeated calls instead of re-running
--    the subquery per row.
-- ---------------------------------------------------------------------
create or replace function public._current_uid()
returns text
language sql
stable
as $$
    select current_setting('app.current_user_id', true)
$$;

create or replace function public._trusted_internal()
returns boolean
language sql
stable
as $$
    select current_setting('app.trusted_internal', true) = 'true'
$$;

-- SECURITY DEFINER is the part that matters -- see header. Runs as
-- `postgres` (this function's owner), bypassing RLS on the SELECT
-- inside it regardless of which role calls the function.
create or replace function public._is_workspace_member(p_workspace_id text, p_user_id text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from workspace_members
        where workspace_id = p_workspace_id
          and user_id::text = p_user_id
    )
$$;

-- Lock this down same spirit as the tables themselves: minime_app can
-- CALL it, nothing else needs to.
revoke all on function public._current_uid() from public;
revoke all on function public._trusted_internal() from public;
revoke all on function public._is_workspace_member(text, text) from public;
grant execute on function public._current_uid() to minime_app;
grant execute on function public._trusted_internal() to minime_app;
grant execute on function public._is_workspace_member(text, text) to minime_app;

-- ---------------------------------------------------------------------
-- 2. Re-create every policy that checks workspace membership, using
--    the function instead of an inline subquery on workspace_members.
--    Same access rules as 0003 -- only the recursion-causing shape
--    changes, not who can see what.
-- ---------------------------------------------------------------------

drop policy if exists workspaces_scope on workspaces;
create policy workspaces_scope on workspaces
    for all
    using (
        owner_id::text = _current_uid()
        or _is_workspace_member(id, _current_uid())
        or _trusted_internal()
    );

drop policy if exists workspace_members_scope on workspace_members;
create policy workspace_members_scope on workspace_members
    for all
    using (
        _is_workspace_member(workspace_id, _current_uid())
        or user_id::text = _current_uid()
        or _trusted_internal()
    );

drop policy if exists workspace_owner_votes_scope on workspace_owner_votes;
create policy workspace_owner_votes_scope on workspace_owner_votes
    for all
    using (
        _is_workspace_member(workspace_id, _current_uid())
        or _trusted_internal()
    );

drop policy if exists chats_scope on chats;
create policy chats_scope on chats
    for all
    using (
        owner_id::text = _current_uid()
        or (
            not coalesce(is_private, false)
            and workspace_id is not null
            and _is_workspace_member(workspace_id, _current_uid())
        )
        or _trusted_internal()
    );

drop policy if exists chat_messages_scope on chat_messages;
create policy chat_messages_scope on chat_messages
    for all
    using (
        owner_id::text = _current_uid()
        or chat_id in (
            select c.id from chats c
            where not coalesce(c.is_private, false)
              and c.workspace_id is not null
              and _is_workspace_member(c.workspace_id, _current_uid())
        )
        or _trusted_internal()
    );

drop policy if exists workspace_panel_content_scope on workspace_panel_content;
create policy workspace_panel_content_scope on workspace_panel_content
    for all
    using (
        _is_workspace_member(workspace_id, _current_uid())
        or workspace_id in (
            select id from workspaces
            where owner_id::text = _current_uid()
        )
        or _trusted_internal()
    );

-- batches_scope, batch_members_scope, user_integrations_scope,
-- audit_log_scope are unchanged from 0003 -- none of them touch
-- workspace_members, so none of them were ever affected by this bug.
