# M2C protocol and tool-schema contract (v1)

Patch P00 of the M2C build plan. This directory is the **single source of truth**
for what the device client (`m2c/`) and the server (`backend/codeagent/`) say to
each other. Both sides are built against it in parallel; neither may define a
message, field, error code or tool schema that is not here.

If this file and `protocol/v1/*.json` disagree, the JSON wins. If the JSON and
`m2c_build_plan.md` (Overrides O1-O8) disagree, the build plan wins and the JSON
is a bug.

```
protocol/
  README.md            this file
  requirements.txt     pinned deps for validate.py and its test (jsonschema, pytest)
  validate.py          reference reader: schemas, fixtures, client-copy check/sync
  v1/
    envelope.json      the envelope; routes payload -> <t>.json; run_id/seq rules
    common.json        shared definitions (ids, enums, history items)
    <t>.json           payload schema for each of the 15 message types
    run_event.json     payload schema for run_event, with one $def per subtype
    tool_definition.json   meta-schema for tools/*.json
    tools/<name>.json  the nine v0 tools: description, input_schema, risk
  fixtures/
    01_plain_chat.jsonl  02_edit_with_approval.jsonl
    03_disconnect_resume.jsonl  04_quota_error.jsonl     golden transcripts
    invalid_examples.jsonl                               must all be rejected
```

## 1. Envelope

Every message, both directions, is one JSON object:

```json
{ "v": 1, "t": "run_event", "id": "<uuid>", "run_id": "<opaque>", "seq": 7,
  "ts": "2026-11-02T09:00:00.250Z", "payload": { "type": "text_delta", "text": "..." } }
```

| Field | Rule |
|---|---|
| `v` | Always `1` in this directory. |
| `t` | Message type (below). `payload` is validated against `v1/<t>.json`. |
| `id` | Lower-case UUID minted by the sender. Unique per message. |
| `run_id` | Opaque. **Required** on run-scoped messages, **forbidden** on `hello`, `welcome`, `ping`, `pong`, optional on `error`. Minted by the **client** in `run.start` (UUID or ULID recommended); the server validates it, scopes it to the device's user, and rejects a collision with `invalid_message`. Never parse it. |
| `seq` | Integer, **server stream events only**: `run_event`, `tool_proposed`, `budget_warning`. Starts at 1 per run, increases by exactly 1, no gaps. Forbidden on everything else (including `error`). |
| `ts` | RFC 3339 date-time with offset. |
| `payload` | Object; closed (`additionalProperties: false`) for every type. |

## 2. Messages

**Client to server**

| `t` | Payload | Notes |
|---|---|---|
| `hello` | `protocol_versions[]`, `device_credential`, `client_version`, `capabilities[]` | First message on every connection. Carries the credential: never log it. |
| `run.start` | `profile` ("code"), `mode`, `cwd_label`, `instructions`, `rules[]`, `memory_index`, `tool_catalog_version`, `model_pref`, optional `history[]` | `cwd_label` is a label, never a path. `rules` are instruction rules the client matched to the files in play, not permission rules (those never leave the device). |
| `user_message` | `text` | |
| `tool_result` | `call_id`, `ok`, `output`, `truncated`, `trust`, optional `exit_code`, `duration_ms` | Output is already redacted and truncated by the client. |
| `tool_stream` | `call_id`, `stream` (stdout/stderr), `chunk` | Live view only; the model sees the final `tool_result`. |
| `approval` | `call_id`, `decision` (allow/deny), `scope` (once/session) | Reports the device's decision; local policy is final. A denied call gets this and **no** `tool_result`. |
| `cancel` | `{}` | |
| `run.resume` | `last_seq`, optional `state` (same shape as `run.start`) | `last_seq` = highest seq the device received. Send `state` (with `history` from the device log) when the server's buffer may have expired (O1). |
| `ping` | optional `nonce` | |

**Server to client**

| `t` | Payload | Notes |
|---|---|---|
| `welcome` | `protocol`, `min_protocol`, `token_ttl_s`, `limits{max_parallel_runs, max_message_bytes?}`, optional `server_version` | `min_protocol` is the oldest version the server still accepts. |
| `run_event` | `type` + subtype fields (section 3) | Stream; has `seq`. |
| `tool_proposed` | `call_id`, `tool`, `input`, `risk` | Stream; has `seq`. `input` is checked against `v1/tools/<tool>.json` for the nine v0 tools; other names (MCP, later catalogs) pass the schema and the client fails closed. |
| `budget_warning` | `scope` (run/account), `spent_pct`, optional `reset_at` | Stream; has `seq`. 80% on both surfaces (build plan 11.5). |
| `error` | `code`, `message`, optional `ref_id`, `details` | No `seq`. `message` is fixed, sanitized text. |
| `pong` | optional `nonce` | |

## 3. `run_event` subtypes

| `payload.type` | Fields | Class |
|---|---|---|
| `text_delta` | `text` | content |
| `text_done` | optional `stop_reason` (end_turn / tool_use / max_tokens) | metadata |
| `tool_started` | `call_id`, `tool` | metadata |
| `tool_finished` | `call_id`, `tool`, `status` (ok/error/denied/timeout/disconnected/cancelled), optional `duration_ms` | metadata |
| `state_changed` | `state`, optional `reason` | metadata |
| `usage` | `model`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `credits`, optional `run_credits_total` | metadata |
| `notice` | `code`, `severity`, `message` | metadata |

Run states: `created`, `running`, `awaiting_approval`, `completed`, `paused_budget`,
`paused_disconnected`, `paused_error`, `cancelled`, `failed`, `expired`. Terminal:
`completed`, `cancelled`, `failed`, `expired`; nothing follows them.

`awaiting_approval` is **advisory**: the server cannot see local policy, so it enters
that state when it proposes a call above R0 and leaves it when the approval or result
arrives. Only the device decides whether anyone is asked.

`tool_finished` is sent **exactly once** per proposed call. `tool_started` is optional
(an auto-allowed call may go straight to `tool_finished`).

## 4. Error codes

Stable strings. A client that meets an unknown code treats it as `internal`.

| Code | Meaning | Client reaction |
|---|---|---|
| `unauthorized` | Credential missing, invalid or expired. | Re-login. |
| `device_revoked` | The device was revoked. | Stop, tell the user, offer `m2c login`. |
| `quota_exhausted` | The shared pool (or run ceiling) is used up. Always followed by `state_changed` -> `paused_budget`, reason `quota_exhausted`. `details.reset_at` when known. | Show reset time. Never retry blindly; never downgrade silently. |
| `run_not_found` | Unknown `run_id` (resume, message). | Start a new run, re-sending history from the device log. |
| `protocol_unsupported` | No common version. `details.min_protocol` / `max_protocol`. A server that no longer speaks v1 still answers a v1 `hello` with this, in v1 shape, before closing. | Tell the user to update. |
| `rate_limited` | `details.retry_after_s`. | Back off. |
| `provider_unavailable` | Every model in the chain failed. | Run goes to `paused_error`; offer retry. |
| `tool_timeout` | A call got no result in time (`details.call_id`). | The call is closed with `tool_finished` `timeout`. |
| `invalid_message` | The server rejected a message; `ref_id` names it. | Bug on one side; log it. |
| `internal` | Server fault. | Retry with backoff. |

## 5. Sequence, replay and resume

- The server numbers its run stream 1, 2, 3... The device remembers the highest `seq`
  it received per run.
- After a disconnect: new connection, `hello` / `welcome`, then `run.resume{last_seq}`.
  The server re-delivers every event with `seq > last_seq` verbatim (same `id`, same
  `seq`) and carries on. Duplicates are ignored by `seq`.
- A call cut off by a disconnect is closed by the server with `tool_finished` status
  `disconnected` and the run goes `paused_disconnected` (product guide 13.5); the model
  is told on resume. **A client executes a replayed `tool_proposed` only if no later
  `tool_finished` for that `call_id` is in the replay.**
- If the server's buffer has expired, `run.resume` carries `state` (including `history`
  from the device log) and the run continues (O1). The device log is the truth.
- Duplicate `tool_result` for a `call_id` are ignored by the server.
- Fixture `03_disconnect_resume.jsonl` is this flow end to end.

## 6. Where content may live (O1, build plan 11.4)

Every payload (and every `run_event` subtype) carries `x-m2c-data-class`:

- **`content`**: may hold prompts, file contents, paths, commands, tool output. In
  flight only. The server may hold it only in the encrypted, expiring run buffer and
  **must never write it to Postgres, logs or metrics**. Units: `run.start`, `run.resume`,
  `user_message`, `tool_result`, `tool_stream`, `tool_proposed`, `run_event:text_delta`.
- **`metadata`**: may be persisted and logged. Closed objects, bounded strings, and no
  property that could carry a path or content (`path`, `file`, `command`, `output`,
  `text`, `input`, ...). `cwd_label` is a label; `error.message` and `notice.message`
  are server-authored fixed text.

Servers must persist by **projecting onto the schema's known properties**, never by
storing a raw payload (tolerant readers, section 8, would otherwise let a stray field
through). `hello.device_credential` is marked `x-m2c-secret` and is never logged.

`backend/tests/unit/test_protocol_contract.py` fails if a new message or subtype is not
classified, if the set of `content` units changes without that test being edited, or if a
`metadata` unit grows a path-like or content-like property. WP Q06 (canary) is the runtime
counterpart.

## 7. Tool schemas v0

`tools/<name>.json` = `{name, version, risk, [risk_max], description, input_schema}`.
`name`, `description`, `input_schema` are what a model adapter hands to a provider (the
schemas use only the keyword subset every provider accepts); `risk` follows product guide
7.2-7.3.

| Tool | Risk | Required input |
|---|---|---|
| `read_file` | R0 | `path` (optional `start_line`, `end_line`) |
| `list_dir` | R0 | `path` (optional `max_depth`) |
| `glob` | R0 | `pattern` (optional `path`, `max_results`) |
| `grep` | R0 | `pattern` (optional `path`, `glob`, `context_lines`, `case_insensitive`, `max_matches`) |
| `edit_file` | R1 | `path`, `old_string`, `new_string` (optional `replace_all`) |
| `write_file` | R1 | `path`, `content` |
| `delete_path` | R1, `risk_max` R4 | `path` |
| `bash` | R2 | `command` (optional `timeout_s`, `reason`) |
| `plan_exit` | R0 | `plan` |

A call to a tool carries its `risk`, or its `risk_max` where it has one (`delete_path`:
R1 inside the project with a checkpoint, R4 otherwise). `tool_proposed` enforces this and
validates `input` per tool. The catalog value is a **floor**: path-dependent escalation
(outside the project, protected paths) is the client policy engine's job, which may raise
a call's risk and never lowers it. Input objects are closed: an unknown argument is a
rejection. `validate_tool_input()` fails closed for a tool not in the catalog.

`run.start.tool_catalog_version` (`"v0"`) says which catalog the client can execute.

## 8. Versioning rule

- **Additive changes only inside `v1`**: a new optional field, a new message type, a new
  `run_event` subtype, a new error code or enum value, a new tool or catalog version.
  Each is a schema edit here plus a synced client copy.
- **Anything else is breaking** and creates `protocol/v2/`: removing or renaming a field,
  making an optional field required, changing a type or meaning, narrowing an enum,
  re-ordering the `seq` rules.
- **Negotiation.** The client lists the versions it speaks in `hello.protocol_versions`;
  the server answers `welcome.protocol` (the one in use) and `welcome.min_protocol` (the
  oldest it still accepts). The server raises `min_protocol` only when it ships a new
  major; an older client then gets `protocol_unsupported`.
- **Tolerant readers.** The schemas are strict (`additionalProperties: false`) so tests
  catch typos. A production receiver must survive a newer peer, so it validates with
  `validate_message(msg, tolerant=True)`: unknown **optional** properties are ignored
  (and dropped before storing); everything else, and all model-produced tool input,
  stays strict. Clients ignore unknown server message types and `run_event` subtypes;
  servers answer an unknown client type with `invalid_message`.
- Widening an enum that a *client sends* (`mode`, `profile`, `tool_catalog_version`) is
  additive only once servers are upgraded first.

## 9. Fixtures and replay

`fixtures/*.jsonl`: one envelope per line. A transcript is the ordered record, both
directions interleaved, of the messages **delivered** on a device's connections; a `hello`
starts a new connection. Direction follows from `t`.

| File | Shows |
|---|---|
| `01_plain_chat` | hello/welcome, a run with streamed text, usage, completion, ping/pong |
| `02_edit_with_approval` | an R0 read (no prompt), then an R1 `edit_file` with one approval and result |
| `03_disconnect_resume` | a cut tool call, second connection, `run.resume`, replay, retry, completion |
| `04_quota_error` | an 80% `budget_warning`, then `quota_exhausted` and `paused_budget` |

- **Server tests**: feed the client lines in order, assert the server's lines (ids and
  `ts` are not asserted; `type`, `seq`, `call_id` and payload are).
- **Client tests**: a fake server plays the server lines; assert the client's lines.
- `check_transcript()` enforces the rules above (seq runs, call pairing, resume cursor,
  terminal states) on every fixture.
- `invalid_examples.jsonl`: `{case, expect:{validator, path}, message}`. Each message
  must be rejected, on that keyword and at that path, so no case passes by accident.
- Fixture credentials start with `m2c-fixture-` and are not secrets.

## 10. Tooling

```bash
pip install -r protocol/requirements.txt
python protocol/validate.py                     # schemas + fixtures + invalid examples + client copy
python protocol/validate.py --check-client-copy # only the client copy
python protocol/validate.py --sync-client       # copy protocol/v1/**/*.json into the client package
cd backend && pytest tests/unit/test_protocol_contract.py --noconftest -v
```

Exit codes: 0 ok, 1 a check failed, 2 usage error (unknown flag, missing jsonschema, or
`--sync-client` with no client package).

**Client copy.** The client carries a copy of the schemas at
`m2c/src/m2c/protocol/v1/` so it works when installed on its own. `protocol/v1/` is the
source; never edit the copy.

- `--check-client-copy` passes when that directory does not exist yet (WP C01 creates
  the package in parallel with P00), and otherwise compares every `*.json` byte for
  byte, reporting missing, extra and differing files. Other files a packager adds beside
  them are ignored.
- `--sync-client` mirrors `protocol/v1/**/*.json` into it, removing stale `.json` files.
  It refuses (exit 2) if `m2c/src/m2c/` does not exist: this tool never creates the
  client package. After C01 lands, run it once and commit the result; from then on CI
  fails on any drift.

**CI.** The `protocol` job in `.github/workflows/ci.yml` installs
`protocol/requirements.txt` and runs `validate.py` and the contract test. The same test
file is collected by the normal backend job and skips itself there
(`pytest.importorskip("jsonschema")`), because `jsonschema` is deliberately not in
`backend/requirements.txt` yet; adding it there is a recommended follow-up once the
server imports the schemas at runtime.

## 11. Changing the contract

1. Edit `protocol/v1` (additive only, section 8). Give every new payload or subtype an
   `x-m2c-data-class`; every new tool a `tools/<name>.json` **and** a branch in
   `tool_proposed.json`.
2. Add or extend a fixture and an `invalid_examples.jsonl` case.
3. `python protocol/validate.py --sync-client` (once the client package exists).
4. Update `CONTENT_UNITS` in the contract test only if a `content` unit is genuinely new,
   and say why in the patch description.

## 12. Decisions this patch had to make

The sources are silent or disagree on these; each is a one-line change if you disagree.

- **Names.** Tool names are the work package's (`glob`, `grep`, `delete_path`, `bash`);
  the feature guide's 5.2 table uses `glob_search`, `grep_search`, `delete_file`,
  `run_command`. The feature guide's overrides table also mentions `minicore/protocol.py`
  and `/api/codeagent/*`; the build plan (this directory, `backend/codeagent/`,
  `/v1/code/*`) wins.
- **Modes** are `plan`, `ask`, `accept-edits` (build plan C06), not the feature guide's
  Ask / Edit / Auto columns. `auto` and `bypass` are later, additive.
- **`bash` has no `background`** argument in v0 (the design note's example has one);
  background shells are catalogued v1 (C16).
- **`run_id` is client-minted**, because the envelope has no other correlation field.
- **`hello.protocol_versions`** (a list) replaces the design note's single `protocol`.
- **Approval** is `decision` allow/deny plus `scope` once/session (the design note's
  `allow_once` was a sketch).
- **`error` never carries `seq`**; `quota_exhausted` is followed by `paused_budget`.
- **Not covered by the nine client message types**: changing mode mid-run (Shift+Tab),
  a token-refresh message. Both can be added later as new types.
