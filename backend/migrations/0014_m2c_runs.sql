-- 0014_m2c_runs.sql
--
-- S06 (run manager, ephemeral buffer, run metadata): the server-side record of
-- an M2C run. One table:
--
--   runs -- one row per run a device starts: who owns it, which device runs
--           it, its profile and mode, where it is in the run state machine
--           (codeagent/runs.py), when it started / last changed / ended, the
--           protocol error code it last stopped with, and two simple
--           counters (model steps, credits).
--
-- Design notes:
--   - NO CONTENT, EVER. This table is the content-free half of build plan
--     section O1: a run's conversation and its replay events live only in the
--     encrypted, expiring Redis buffer (codeagent/buffer.py), never here.
--     There is therefore no title, prompt, text, path, file name, command,
--     message or label column of any kind -- the only strings are the opaque
--     run id, the owner / device ids, three short vocabulary words (profile,
--     mode, state) and an error CODE. tests/static/test_runs_migration.py
--     reads this file and fails if a column is added whose name could carry
--     content, so the rule is checked, not just promised.
--   - id IS THE RUN ID, minted by the CLIENT in run.start (protocol/README.md
--     section 1: "opaque, 8-64 of A-Za-z0-9_-, never parse it"), and it is the
--     same string usage_ledger.run_id carries (migration 0012), so the ledger
--     can be summed per run without a join column. It is the primary key on
--     its own: a second run.start with an id that already exists, for any
--     account, is a collision and is refused (the protocol answers
--     invalid_message). Every statement in codeagent/runs.py also filters on
--     user_id, so one account can never read or change another's run.
--   - error_code holds a protocol/v1 error CODE (unauthorized, quota_exhausted,
--     provider_unavailable, ...), never a message. The column is constrained
--     to the SHAPE of a code (lower-case words and underscores, at most 64
--     characters) rather than to the current list: the list lives in
--     protocol/v1/common.json and grows additively, and a migration applied by
--     hand should not have to be re-issued every time it does. runs.py checks
--     the value against its own copy of the list, and a unit test keeps that
--     copy equal to the protocol's.
--   - profile and mode are shape-checked, not enumerated, for the same reason
--     (build plan O8: Menux adds profiles to the same engine, and protocol
--     modes grow additively). state IS enumerated: the run state machine is a
--     closed set and a new state deserves a migration.
--   - THE STATE MACHINE LIVES IN runs.py (an explicit transition table and a
--     conditional UPDATE on the expected current state, so two racing callers
--     cannot both win). The database enforces only what must hold whatever the
--     caller does: (1) ended_at is set if and only if the state is terminal
--     (a CHECK), (2) a terminal state is final -- completed, cancelled, failed
--     and expired can never change again (a BEFORE UPDATE trigger), (3) the
--     identity columns never change, and (4) the counters only go up. The
--     legal edges between non-terminal states are not repeated here, so there
--     is one table to change, not two that can drift.
--   - Counters may still be raised after a run ends. steps and credits are
--     informational (usage_ledger is the metering truth); a model call that
--     was in flight when a run was cancelled still gets its credits recorded
--     on the row instead of being silently dropped.
--   - NO FOREIGN KEYS, on purpose, same as 0013: user_id is `text` like
--     owner_id and usage_ledger.user_id, device_id is a uuid with no reference
--     to devices, so a run row never blocks (or is blocked by) anything about
--     the account or the device, and revoking or deleting a device cannot
--     reach it.
--   - Row-level security is enabled AND forced, same posture as 0003, 0012 and
--     0013. A user sees and changes only their own runs. The device socket has
--     no signed-in end user, but it does know the account its device belongs to
--     and passes it as the acting user, so the policies bind the row to that
--     account. The housekeeping sweep (expiring abandoned runs) has no acting
--     user and reads and writes through the trusted internal path.
--   - NO DELETE GRANT. The code never deletes a run row; it only moves it to a
--     terminal state. Metadata rows are content-free and small, and keeping
--     them is what lets "which runs did this account start" be answered after
--     the buffer is long gone. A retention sweep, if one is wanted later, is a
--     separate migration that grants delete on purpose.
--   - TABLE-LEVEL GRANTS ONLY, plus a trigger, for the reason 0013 gives:
--     tests/static/test_sql_grants.py (B01) replays these migrations and models
--     table-level GRANT / REVOKE statements only. So the app role gets UPDATE on
--     the whole table, and what must never change is guarded by the trigger.
--     The trigger function is an ordinary CREATE FUNCTION (not a DO block),
--     never includes a row value in a message, and applies to every role
--     including the trusted internal path; an administrator who really needs to
--     bypass it can `alter table ... disable trigger`.
--   - GRANTs live in THIS file, not a follow-up -- see migration 0007's header
--     for exactly the bug (every query rejected at the privilege check, before
--     RLS is even evaluated) that splitting them caused.
--
-- HOW TO APPLY: run as the `postgres` role, same as every migration here
--   psql "$DATABASE_URL" -f migrations/0014_m2c_runs.sql
-- or paste the whole file into the Supabase SQL editor (it runs as
-- `postgres`). It is plain SQL -- no psql meta-commands, no transaction
-- control -- and uses IF NOT EXISTS / CREATE OR REPLACE / DROP ... IF EXISTS /
-- re-runnable GRANT and REVOKE throughout, so it is safe to apply by hand and
-- safe to run more than once. Requires the `minime_app` role from migration
-- 0003. No app restart needed -- Postgres checks privileges and RLS per query,
-- not per connection (migration 0007's note). Until it is applied, creating a
-- run fails with the database's own "relation does not exist" error, which
-- codeagent/runs.py reports as RunStoreUnavailable (fail closed).

-- ---------------------------------------------------------------------
-- runs
-- ---------------------------------------------------------------------
create table if not exists runs (
    -- The client-minted opaque run id (protocol common.json run_id).
    id          text primary key
                    check (id ~ '^[A-Za-z0-9_-]{8,64}$'),
    user_id     text not null,
    device_id   uuid not null,
    -- Vocabulary words, not free text: the shape check keeps a label column
    -- from becoming a place to put a sentence or a path.
    profile     text not null check (profile ~ '^[a-z][a-z0-9_-]{0,31}$'),
    mode        text not null check (mode ~ '^[a-z][a-z0-9_-]{0,31}$'),
    state       text not null default 'created'
                    check (state in (
                        'created', 'running', 'awaiting_approval', 'completed',
                        'paused_budget', 'paused_disconnected', 'paused_error',
                        'cancelled', 'failed', 'expired')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    ended_at    timestamptz,
    -- A protocol error CODE, never a message. NULL = none.
    error_code  text check (error_code is null or error_code ~ '^[a-z][a-z_]{0,63}$'),
    steps       integer not null default 0 check (steps >= 0),
    credits     numeric not null default 0 check (credits >= 0),
    constraint runs_ended_iff_terminal
        check ((state in ('completed', 'cancelled', 'failed', 'expired')) = (ended_at is not null))
);

comment on table runs is
    'S06: content-free metadata of an M2C run -- opaque id, owner, device, '
    'profile, mode, state, timestamps, a protocol error code and two counters. '
    'No title, prompt, text, path or message column: a run''s content lives '
    'only in the encrypted Redis buffer (codeagent/buffer.py). Terminal states '
    'are final (trigger). Never deleted by the app.';

comment on column runs.id is
    'Opaque run id minted by the client in run.start; the same string '
    'usage_ledger.run_id carries. Never parsed.';

comment on column runs.error_code is
    'A protocol/v1 error code (for example quota_exhausted), never a message.';

comment on column runs.credits is
    'Informational running total. usage_ledger (migration 0012) is the '
    'metering truth.';

-- "This device's open runs" (the parallel-run cap, disconnect handling).
create index if not exists runs_device_open_idx
    on runs (device_id) where ended_at is null;

-- The housekeeping sweep: open runs by last activity, and by age (hard cap).
create index if not exists runs_open_updated_idx
    on runs (updated_at) where ended_at is null;
create index if not exists runs_open_created_idx
    on runs (created_at) where ended_at is null;

-- What must hold whoever writes: identity never changes, a terminal state is
-- final, the end stamp and error code are fixed once the run has ended, and
-- the counters never go down. Messages name columns, never values.
create or replace function runs_guard_update() returns trigger
language plpgsql as $$
begin
    if new.id is distinct from old.id
       or new.user_id is distinct from old.user_id
       or new.device_id is distinct from old.device_id
       or new.profile is distinct from old.profile
       or new.created_at is distinct from old.created_at then
        raise exception 'runs: identity columns cannot change'
            using errcode = '42501';
    end if;
    if old.state in ('completed', 'cancelled', 'failed', 'expired') then
        if new.state is distinct from old.state
           or new.ended_at is distinct from old.ended_at
           or new.error_code is distinct from old.error_code then
            raise exception 'runs: a terminal run cannot change state'
                using errcode = '42501';
        end if;
    end if;
    if new.steps < old.steps or new.credits < old.credits then
        raise exception 'runs: counters cannot decrease'
            using errcode = '42501';
    end if;
    return new;
end
$$;

drop trigger if exists runs_guard_update on runs;
create trigger runs_guard_update before update on runs
    for each row execute function runs_guard_update();

alter table runs enable row level security;
alter table runs force row level security;

-- A user sees only their own runs. The housekeeping sweep has no acting user
-- and reads through the trusted internal path.
drop policy if exists runs_select on runs;
create policy runs_select on runs
    for select
    using (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- A run is created on behalf of the account the device belongs to.
drop policy if exists runs_insert on runs;
create policy runs_insert on runs
    for insert
    with check (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- Whose rows may change; the trigger above limits WHAT may change.
drop policy if exists runs_update on runs;
create policy runs_update on runs
    for update
    using (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    )
    with check (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- No delete policy and no delete grant: a run is moved to a terminal state,
-- never removed.
revoke all on runs from minime_app;
grant select, insert, update on runs to minime_app;
