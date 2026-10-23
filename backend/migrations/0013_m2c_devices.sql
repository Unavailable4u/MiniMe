-- 0013_m2c_devices.sql
--
-- S03 (device pairing and credentials): the server-side registry behind the
-- M2C device-code login (`m2c login`). Two tables:
--
--   device_pairings -- one row per in-flight `m2c login`: the hashed device
--                      code the terminal polls with, the hashed short user
--                      code a person types into the web app, and a status
--                      (pending -> approved -> consumed, or denied). Rows are
--                      short-lived (about 10 minutes) and are purged a day
--                      after they expire.
--   devices         -- the registry of paired devices: who owns each one,
--                      its name / OS / client version, the SHA-256 of its
--                      long-lived credential, and when it was last seen or
--                      revoked.
--
-- Design notes:
--   - NO CONTENT. These tables hold identifiers and three short labels the
--     client reports about itself (device_name, os, client_version) -- never
--     prompts, file contents, file paths or command lines. The CHECK
--     constraints on the label columns cap their length so the columns
--     cannot be used to smuggle anything bigger (build plan section O1 /
--     11.4).
--   - SECRETS ARE NEVER STORED. device_code, user_code and the device
--     credential exist in plaintext only in the HTTP exchange that hands
--     them out. The tables hold SHA-256 hex digests only. In particular the
--     credential is generated at the moment the terminal collects it (the
--     poll that flips a pairing from 'approved' to 'consumed') and its hash
--     is written in the same transaction, so there is never a moment when a
--     plaintext credential sits waiting in the database.
--   - ONE-SHOT COLLECTION. 'consumed' is terminal. The poll that wins the
--     atomic approved -> consumed update is the only one that is ever handed
--     a credential; a replayed device code finds a consumed row and gets
--     nothing (eo/devices.py).
--   - NO FOREIGN KEYS, on purpose. devices.user_id is `text`, like owner_id
--     and usage_ledger.user_id elsewhere in this schema, and there is no
--     reference to auth.users: the device tables stay independent of the
--     account tables and of billing history, so revoking or later deleting a
--     device can never touch anything else.
--   - Revocation is a timestamp, not a delete: revoked_at keeps the history
--     (and lets the token endpoint check "revoked" and stamp last_seen_at in a
--     single atomic statement). minime_app cannot delete from devices.
--   - TABLE-LEVEL GRANTS ONLY, plus triggers. tests/static/test_sql_grants.py
--     (B01) replays these migrations to check the app's SQL against the app
--     role's privileges, and it models table-level GRANT/REVOKE statements
--     only. So the app role gets UPDATE on the whole table, and what must
--     never change is guarded by BEFORE UPDATE triggers instead of column
--     grants: devices may change only last_seen_at and revoked_at (and a
--     revocation can never be undone); a pairing's codes, labels and expiry
--     never change, its status only moves forward (pending -> approved or
--     denied, approved -> consumed), and its approving user is set once, at
--     approval. A pairing row may be deleted only a day after it expires.
--     The trigger functions are ordinary CREATE FUNCTION statements (not DO
--     blocks), never include a row value in a message (a pairing row holds
--     code hashes), and apply to every role including the trusted internal
--     path; an administrator who really needs to bypass one can
--     `alter table ... disable trigger`.
--   - Row-level security is enabled AND forced on both tables, same posture
--     as 0003 and 0012. devices: a user sees and revokes only their own
--     rows. device_pairings: trusted-internal only -- the two callers are an
--     unauthenticated terminal (no user identity yet) and the approval
--     endpoint (the row has no owner until it is approved), so no policy
--     keyed on a user id could be correct there; no end-user connection can
--     read a pairing row.
--   - GRANTs live in THIS file, not a follow-up -- see migration 0007's
--     header for exactly the bug (every query rejected at the privilege
--     check, before RLS is even evaluated) that splitting them caused. Each
--     table's GRANT/REVOKE statements come after its CREATE TABLE, so
--     replaying the grants never names a table that does not exist yet.
--
-- HOW TO APPLY: run as the `postgres` role, same as every migration here
--   psql "$DATABASE_URL" -f migrations/0013_m2c_devices.sql
-- or paste the whole file into the Supabase SQL editor (it runs as
-- `postgres`). It is plain SQL -- no psql meta-commands, no transaction
-- control -- and uses IF NOT EXISTS / CREATE OR REPLACE / DROP ... IF EXISTS /
-- re-runnable GRANT and REVOKE throughout, so it is safe to apply by hand and
-- safe to run more than once. Requires the `minime_app` role from migration
-- 0003. No app restart needed -- Postgres checks privileges and RLS per
-- query, not per connection (migration 0007's note). Until it is applied the
-- device routes fail with the database's own "relation does not exist" error.

-- ---------------------------------------------------------------------
-- devices
-- ---------------------------------------------------------------------
create table if not exists devices (
    id              uuid primary key default gen_random_uuid(),
    user_id         text not null,
    device_name     text not null check (char_length(device_name) between 1 and 64),
    os              text not null check (char_length(os) between 1 and 64),
    client_version  text not null check (char_length(client_version) between 1 and 32),
    -- SHA-256 (lowercase hex) of the 256-bit device credential. Unique:
    -- it is the lookup key for POST /v1/code/devices/token.
    credential_hash text not null unique
                        check (credential_hash ~ '^[0-9a-f]{64}$'),
    created_at      timestamptz not null default now(),
    last_seen_at    timestamptz,
    revoked_at      timestamptz
);

comment on table devices is
    'S03: registry of paired M2C devices -- owner, name/OS/client version '
    'labels, SHA-256 of the device credential, last-seen and revoked '
    'timestamps. No content, no plaintext secrets, no foreign keys '
    '(eo/devices.py). Only last_seen_at and revoked_at can change.';

comment on column devices.credential_hash is
    'SHA-256 hex of the device credential. The credential itself is shown to '
    'the device once, at pairing, and never stored.';

comment on column devices.last_seen_at is
    'Last time the device exchanged its credential for a token '
    '(POST /v1/code/devices/token). Not a live-presence signal.';

comment on column devices.revoked_at is
    'Set when the owner revokes the device. A revoked device can no longer '
    'get a token; NULL = active. Permanent: it cannot be cleared or changed. '
    'Rows are never deleted.';

-- "This account's devices", newest first (GET /api/devices).
create index if not exists devices_user_created_idx
    on devices (user_id, created_at desc);

-- Only last_seen_at and revoked_at may ever change, and a revocation is final.
-- Messages name columns, never values.
create or replace function devices_guard_update() returns trigger
language plpgsql as $$
begin
    if new.id is distinct from old.id
       or new.user_id is distinct from old.user_id
       or new.device_name is distinct from old.device_name
       or new.os is distinct from old.os
       or new.client_version is distinct from old.client_version
       or new.credential_hash is distinct from old.credential_hash
       or new.created_at is distinct from old.created_at then
        raise exception 'devices: only last_seen_at and revoked_at may change'
            using errcode = '42501';
    end if;
    if old.revoked_at is not null and new.revoked_at is distinct from old.revoked_at then
        raise exception 'devices: a revoked device cannot be un-revoked'
            using errcode = '42501';
    end if;
    return new;
end
$$;

drop trigger if exists devices_guard_update on devices;
create trigger devices_guard_update before update on devices
    for each row execute function devices_guard_update();

alter table devices enable row level security;
alter table devices force row level security;

-- Users read only their own devices. The token endpoint and S04's
-- handshake have no signed-in user (the device presents a credential or a
-- device JWT), so they read through the trusted internal path.
drop policy if exists devices_select on devices;
create policy devices_select on devices
    for select
    using (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- A device row is created by the poll that hands the credential to the
-- terminal -- a request with no user identity -- so INSERT is trusted-only.
drop policy if exists devices_insert on devices;
create policy devices_insert on devices
    for insert
    with check (current_setting('app.trusted_internal', true) = 'true');

-- Revocation is the owner's action; the token endpoint also stamps
-- last_seen_at on the trusted path. The trigger above limits WHAT can
-- change; this policy limits WHOSE rows.
drop policy if exists devices_update on devices;
create policy devices_update on devices
    for update
    using (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    )
    with check (
        user_id = current_setting('app.current_user_id', true)
        or current_setting('app.trusted_internal', true) = 'true'
    );

-- No delete policy and no delete grant: revoking keeps the history.
revoke all on devices from minime_app;
grant select, insert, update on devices to minime_app;

-- ---------------------------------------------------------------------
-- device_pairings
-- ---------------------------------------------------------------------
create table if not exists device_pairings (
    id                uuid primary key default gen_random_uuid(),
    -- SHA-256 hex of the device_code the terminal polls with.
    device_code_hash  text not null unique
                          check (device_code_hash ~ '^[0-9a-f]{64}$'),
    -- SHA-256 hex of the normalised user code a person types in the web app.
    user_code_hash    text not null unique
                          check (user_code_hash ~ '^[0-9a-f]{64}$'),
    -- What the terminal reported about itself; becomes the devices row.
    device_name       text not null check (char_length(device_name) between 1 and 64),
    os                text not null check (char_length(os) between 1 and 64),
    client_version    text not null check (char_length(client_version) between 1 and 32),
    status            text not null default 'pending'
                          check (status in ('pending', 'approved', 'denied', 'consumed')),
    -- The approving account. Set on approval; an approved or consumed
    -- pairing always has one.
    user_id           text,
    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null,
    approved_at       timestamptz,
    consumed_at       timestamptz,
    constraint device_pairings_expiry_order check (expires_at > created_at),
    constraint device_pairings_approved_has_user
        check (status not in ('approved', 'consumed') or user_id is not null)
);

comment on table device_pairings is
    'S03: in-flight device-code logins. Hashes of the device code and user '
    'code only (never the codes), the labels the terminal reported, and a '
    'status that moves pending -> approved -> consumed (or denied). Expires '
    'after about 10 minutes; purged a day later. Trusted-internal access '
    'only (eo/devices.py).';

-- Purging expired rows (and nothing else scans by time).
create index if not exists device_pairings_expires_idx
    on device_pairings (expires_at);

-- Codes, labels and expiry never change; status only moves forward; the
-- approving user is set exactly once, at approval. Messages name columns,
-- never values (a pairing row holds code hashes).
create or replace function device_pairings_guard_update() returns trigger
language plpgsql as $$
begin
    if new.id is distinct from old.id
       or new.device_code_hash is distinct from old.device_code_hash
       or new.user_code_hash is distinct from old.user_code_hash
       or new.device_name is distinct from old.device_name
       or new.os is distinct from old.os
       or new.client_version is distinct from old.client_version
       or new.created_at is distinct from old.created_at
       or new.expires_at is distinct from old.expires_at then
        raise exception 'device_pairings: codes, labels and expiry cannot change'
            using errcode = '42501';
    end if;
    if new.status is distinct from old.status and not (
           (old.status = 'pending' and new.status in ('approved', 'denied'))
        or (old.status = 'approved' and new.status = 'consumed')) then
        raise exception 'device_pairings: illegal status change'
            using errcode = '42501';
    end if;
    if new.user_id is distinct from old.user_id and not (
           old.user_id is null and old.status = 'pending' and new.status = 'approved') then
        raise exception 'device_pairings: the approving user is set once, at approval'
            using errcode = '42501';
    end if;
    return new;
end
$$;

drop trigger if exists device_pairings_guard_update on device_pairings;
create trigger device_pairings_guard_update before update on device_pairings
    for each row execute function device_pairings_guard_update();

-- A pairing is deleted only by the housekeeping sweep, a day after it expired;
-- a live or just-expired one is kept so a late poll still gets a clean answer.
create or replace function device_pairings_guard_delete() returns trigger
language plpgsql as $$
begin
    if old.expires_at >= now() - interval '1 day' then
        raise exception 'device_pairings: only pairings expired more than a day ago may be deleted'
            using errcode = '42501';
    end if;
    return old;
end
$$;

drop trigger if exists device_pairings_guard_delete on device_pairings;
create trigger device_pairings_guard_delete before delete on device_pairings
    for each row execute function device_pairings_guard_delete();

alter table device_pairings enable row level security;
alter table device_pairings force row level security;

-- Every caller is either an unauthenticated terminal or the approval
-- endpoint acting on a row nobody owns yet, so there is no user id a policy
-- could compare against: the whole table is trusted-internal. An end-user
-- connection (app.trusted_internal unset) sees and changes nothing.
drop policy if exists device_pairings_internal on device_pairings;
create policy device_pairings_internal on device_pairings
    for all
    using (current_setting('app.trusted_internal', true) = 'true')
    with check (current_setting('app.trusted_internal', true) = 'true');

revoke all on device_pairings from minime_app;
grant select, insert, update, delete on device_pairings to minime_app;
