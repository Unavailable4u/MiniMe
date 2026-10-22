-- 0012_user_plans_and_usage_ledger.sql
--
-- S01 (shared usage ledger and plan limits): the billing foundation the
-- Tech-A/B roadmap and the M2C/Menux build plans all assume exists.
-- Two tables:
--
--   user_plans    -- one row per account: which plan it is on, the size of
--                    its monthly credit pool, and the window that pool
--                    covers.
--   usage_ledger  -- one row per real LLM call, attributed to a user and to
--                    the product surface that made it ('web', 'm2c',
--                    'menux', or 'system' for platform-internal calls
--                    with no owning account).
--
-- This is deliberately a SECOND table next to llm_call_log (migration
-- 0008), not an extension of it. llm_call_log is internal, cross-user
-- cost/latency telemetry with no user column and no normal-user read path;
-- usage_ledger is the per-account metering record a user is allowed to see
-- the totals of (GET /api/usage). Different audience, different RLS, and
-- different retention concerns -- see eo/usage_ledger.py for the writer.
--
-- Design notes:
--   - NUMBERS AND IDS ONLY. A ledger row never holds prompt text, output
--     text, file paths, or error messages. session_id / run_id are opaque
--     identifiers, not content. This is the content-free server-side
--     record the local-first overrides (O1/O2) promise, and it is why no
--     free-text column exists on this table.
--   - Used credits are never stored as a running counter. "Used this
--     period" is always SUM(credits) over usage_ledger for the period
--     window, so GET /api/usage cannot drift from the rows it summarises.
--     usage_ledger_user_created_idx exists to make exactly that query
--     cheap.
--   - user_id is `text`, like owner_id elsewhere in this schema, and is
--     NULL if and only if surface = 'system' (enforced by a CHECK). A
--     'system' row belongs to no account: it is the honest label for a
--     call made with no owning user in scope, rather than guessing one.
--     Because policies compare user_id to app.current_user_id, system rows
--     are invisible to every end user and readable only on the trusted
--     internal path.
--   - Token columns and cost_usd are nullable and independent, with the
--     same "unknown stays unknown" meaning as in llm_call_log: NULL
--     cost_usd is a gap in the price table, not a confirmed free call.
--     `credits`, however, is NOT NULL -- the writer always assigns a
--     conservative figure to an unpriced call (config/plans.json,
--     `unpriced_usd_per_million_tokens`) so an unpriced model can never
--     meter as free by accident.
--   - credits / credit_pool are numeric, not integer: one call routinely
--     costs a fraction of a credit.
--   - user_plans.plan_key is NOT constrained by a CHECK. The set of plans
--     lives in config/plans.json, so adding a tier is a config change, not
--     a migration. eo/plans.py treats an unknown plan_key as 'free'.
--   - An account with NO user_plans row is valid: eo/plans.py resolves it
--     to the plans.json default plan and a calendar-month window. A row
--     only needs to exist once billing assigns a non-default plan or a
--     support action adjusts credit_pool.
--   - usage_ledger is append-only from the app's perspective, same posture
--     as audit_log (0003) and llm_call_log (0008): update/delete are
--     revoked from minime_app as insurance against a future bug silently
--     rewriting what a user was charged.
--   - Writes come from the app role on the trusted internal path: the
--     ledger hook runs deep inside the LLM fallback chain, where the
--     acting user is resolved from eo/usage_context.py rather than passed
--     in, so the INSERT policy checks app.trusted_internal. Reads are
--     scoped to the row's own user.
--   - GRANTs live in THIS file, not a follow-up -- see migration 0007's
--     header for exactly the bug (every query rejected at the privilege
--     check, before RLS is even evaluated) that splitting them caused.
--
-- HOW TO APPLY: run as the `postgres` role, same as every migration here
--   psql "$DATABASE_URL" -f migrations/0012_user_plans_and_usage_ledger.sql
-- Uses IF NOT EXISTS / DROP POLICY IF EXISTS throughout, so it is safe to
-- run more than once. No app restart needed -- Postgres checks privileges
-- and RLS per query, not per connection (migration 0007's note). Until it
-- is applied, the ledger hook degrades to one printed, non-fatal warning
-- per call and GET /api/usage reports the plan defaults with zero usage.

-- ---------------------------------------------------------------------
-- user_plans
-- ---------------------------------------------------------------------
create table if not exists user_plans (
    user_id       text primary key,
    plan_key      text not null default 'free',
    credit_pool   numeric(18, 4) not null check (credit_pool >= 0),
    period_start  timestamptz not null,
    period_end    timestamptz not null,
    status        text not null default 'active'
                      check (status in ('active', 'past_due', 'canceled')),
    constraint user_plans_period_order check (period_end > period_start)
);

comment on table user_plans is
    'S01: one row per account -- plan_key (see config/plans.json), the size '
    'of its credit pool for the period, and the period window. Absent row = '
    'plans.json default plan, calendar-month window (eo/plans.py). Billing '
    'events and support adjustments write here; the app only reads it today.';

comment on column user_plans.credit_pool is
    'Credits available per period for this account. Per-row rather than '
    'looked up from plan_key so a support top-up or grant can change one '
    'account without changing the plan definition.';

alter table user_plans enable row level security;
alter table user_plans force row level security;

-- Users may read their own plan. Every write is a billing event or a
-- support action, never a user request, so INSERT/UPDATE are trusted-only.
drop policy if exists user_plans_select on user_plans;
create policy user_plans_select on user_plans
    for select
    using (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

drop policy if exists user_plans_insert on user_plans;
create policy user_plans_insert on user_plans
    for insert
    with check (current_setting('app.trusted_internal', true) = 'true');

drop policy if exists user_plans_update on user_plans;
create policy user_plans_update on user_plans
    for update
    using (current_setting('app.trusted_internal', true) = 'true')
    with check (current_setting('app.trusted_internal', true) = 'true');

-- No delete: ending a plan is status = 'canceled', which keeps the history.
grant select, insert, update on user_plans to minime_app;
revoke delete on user_plans from minime_app;

-- ---------------------------------------------------------------------
-- usage_ledger
-- ---------------------------------------------------------------------
create table if not exists usage_ledger (
    id                  uuid primary key default gen_random_uuid(),
    user_id             text,
    surface             text not null
                            check (surface in ('web', 'm2c', 'menux', 'system')),
    session_id          text,
    run_id              text,
    provider            text not null,
    model               text,
    input_tokens        integer check (input_tokens is null or input_tokens >= 0),
    cached_input_tokens integer check (cached_input_tokens is null or cached_input_tokens >= 0),
    output_tokens       integer check (output_tokens is null or output_tokens >= 0),
    credits             numeric(18, 6) not null check (credits >= 0),
    cost_usd            numeric(18, 8) check (cost_usd is null or cost_usd >= 0),
    created_at          timestamptz not null default now(),
    -- 'system' rows have no account; every other surface must name one.
    constraint usage_ledger_system_has_no_user
        check ((surface = 'system') = (user_id is null))
);

comment on table usage_ledger is
    'S01: one row per real LLM call, per account and per surface, numbers '
    'and opaque ids only -- never content. Written by eo/usage_ledger.py '
    '(commit), called from utils/llm_client.py beside _record_call_log(), '
    'fail-open. "Used this period" is SUM(credits) over the period window, '
    'not a stored counter. surface = ''system'' (user_id NULL) is a call '
    'made with no owning account in scope.';

comment on column usage_ledger.cost_usd is
    'Provider cost in USD, NULL when the provider/model has no price-table '
    'entry (a gap, not a free call -- same meaning as llm_call_log.cost_usd).';

comment on column usage_ledger.credits is
    'Credits charged against the pool. Always set: an unpriced call is '
    'charged a conservative fallback (config/plans.json), never zero.';

-- The primary lookup: "what has this account used since period_start".
create index if not exists usage_ledger_user_created_idx
    on usage_ledger (user_id, created_at desc);

-- "What did this session / run cost" without a join.
create index if not exists usage_ledger_session_idx
    on usage_ledger (session_id)
    where session_id is not null;

-- Time-range scans for reporting that is not scoped to one account
-- (including the 'system' rows, which have no user_id to lead with).
create index if not exists usage_ledger_created_at_idx
    on usage_ledger (created_at);

alter table usage_ledger enable row level security;
alter table usage_ledger force row level security;

-- Users may read only their own rows; system rows (user_id NULL) never
-- match a user's id, so they are trusted-only by construction.
drop policy if exists usage_ledger_select on usage_ledger;
create policy usage_ledger_select on usage_ledger
    for select
    using (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

drop policy if exists usage_ledger_insert on usage_ledger;
create policy usage_ledger_insert on usage_ledger
    for insert
    with check (current_setting('app.trusted_internal', true) = 'true');

grant select, insert on usage_ledger to minime_app;
revoke update, delete on usage_ledger from minime_app;
