-- 0008_add_llm_call_log.sql
--
-- Cost-per-task + latency logging. utils/llm_client.py's log_usage() /
-- _log_usage() already write usage:{provider}:{key_id}:{date} to Upstash
-- on every real chat-completion call -- a TTL'd (2 days), incrementing
-- DAILY counter keyed by account, built for quota_sentinel.py's rate-limit
-- checks and good at that job. It cannot answer "what did task X cost end
-- to end" or "what's p95 latency for report_writer on Groq" after the
-- fact: by the time anything reads that key, this call's contribution has
-- already been folded into the day's running total, there's no wall-clock
-- timing captured anywhere in that path, and it lives in a key-value
-- store with an expiry, not a queryable table.
--
-- This migration adds the table utils/llm_client.py's new
-- _record_call_log() writes one row to, per real chat-completion call,
-- immediately alongside (not instead of) the existing Upstash write --
-- see that function's own docstring for the call sites (_run_chain_step()
-- for the non-streaming path, _walk_chain_once_stream()'s "done" branch
-- for streaming).
--
-- Design notes:
--   - No owner_id / user scoping column at all, unlike every other table
--     in this schema. This is an internal, cross-user telemetry record --
--     one row describes one provider call made on behalf of a task, not
--     a piece of data any single end user owns or should be able to
--     query directly through a normal request-scoped connection. Every
--     write here runs deep inside the LLM fallback chain with no acting
--     user_id in scope in the first place (see _record_call_log()'s own
--     docstring). The RLS policy below therefore only has a
--     trusted_internal branch, no owner/membership branch -- there is no
--     legitimate normal-user read path for this table today. A future
--     per-workspace cost dashboard would need its own deliberately-added
--     policy branch, not an accidental side effect of this one.
--   - session_id is the task/run identifier already threaded through
--     every generate_text()/stream_completion() call site in this
--     codebase (see api/task_runner.py, where it's assigned once per
--     task run and passed down) -- "what did task X cost" is
--     `select sum(cost_usd) from llm_call_log where session_id = 'X'`,
--     no join required. Left nullable: a handful of callers (the CLI
--     path, per task_runner.py's own comment) don't have one.
--   - input_tokens/output_tokens/total_tokens/cost_usd/latency_ms are all
--     nullable, independently. A row with tokens but no cost_usd means
--     "we know what this cost in tokens, but this provider/model has no
--     price-table entry in utils/llm_client.py yet" -- see
--     _compute_cost_usd()'s own docstring for why that's NULL, not 0.
--     Don't SUM(cost_usd) and assume the result is total spend without
--     also checking for NULL rows in the same window; a NULL is a gap in
--     the price table, not a confirmed free call.
--   - Append-only from the app's perspective, same as audit_log
--     (migration 0003) -- _record_call_log() only ever INSERTs, nothing
--     in this codebase updates or deletes a row here. update/delete are
--     revoked from minime_app below for the same reason audit_log's are:
--     insurance against a future bug silently rewriting history, not a
--     restriction anyone needs to work around today.
--   - GRANT lives in THIS file, not deferred to a later migration --
--     see migration 0007's own header comment for exactly the bug that
--     splitting a new table's RLS policy (0006) from its GRANT (0003, a
--     different file, already run) caused: every query rejected at the
--     privilege check, before RLS is even evaluated. Not repeating that
--     here.
--
-- HOW TO APPLY:
--   psql "$DATABASE_URL" -f migrations/0008_add_llm_call_log.sql
-- Uses IF NOT EXISTS / CREATE TABLE IF NOT EXISTS throughout, so it's
-- safe to run more than once. Must be run as a role with rights to
-- GRANT on this table (`postgres`, same as every other migration here --
-- see migration 0003's header for why migrations run as a different role
-- than the app itself).

create table if not exists llm_call_log (
    id             uuid primary key default gen_random_uuid(),
    created_at     timestamptz not null default now(),
    session_id     text,
    agent_name     text not null default 'Agent',
    provider       text not null,
    model          text,
    key_id         text,
    tier           smallint,
    path           text,
    domain         text,
    input_tokens   integer,
    output_tokens  integer,
    total_tokens   integer,
    cost_usd       numeric(18, 8),
    latency_ms     integer,
    finish_reason  text,
    error          text
);

comment on table llm_call_log is
    'One row per real agent->LLM chat-completion call (cost-per-task + '
    'latency logging). Written by utils/llm_client.py''s '
    '_record_call_log(), alongside (never instead of) the existing '
    'Upstash usage:{provider}:{key_id}:{date} counter log_usage() writes '
    '-- that counter still feeds quota_sentinel.py''s live rate-limit '
    'gating; this table is the durable, queryable, one-row-per-call '
    'record it cannot be, used for "cost per task", "p50/p95 latency per '
    'agent/provider", and Part 7 ARPU/margin reporting. See this '
    'migration''s own header comment for column-nullability notes before '
    'aggregating cost_usd.';

-- "What did task X cost end to end" -- the primary lookup this table
-- exists to answer.
create index if not exists llm_call_log_session_id_idx
    on llm_call_log (session_id);

-- "p50/p95 latency for report_writer on Groq" -- filters on agent_name +
-- provider (+ optionally model) before aggregating latency_ms/cost_usd.
create index if not exists llm_call_log_agent_provider_model_idx
    on llm_call_log (agent_name, provider, model);

-- Time-range scans for cost dashboards / ARPU-margin reporting over a
-- billing period, independent of any particular task or agent.
create index if not exists llm_call_log_created_at_idx
    on llm_call_log (created_at);

alter table llm_call_log enable row level security;
alter table llm_call_log force row level security;

drop policy if exists llm_call_log_scope on llm_call_log;
create policy llm_call_log_scope on llm_call_log
    for all
    using (current_setting('app.trusted_internal', true) = 'true');

-- See migration 0007's header comment for exactly why this grant lives
-- here, in the same migration that creates the table, rather than in a
-- follow-up file.
grant select, insert on llm_call_log to minime_app;
revoke update, delete on llm_call_log from minime_app;
