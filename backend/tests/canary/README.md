# "No content at rest" canary test (Patch M2C-Q06)

MiniMe's promise: a user's code, prompts, file contents, tool outputs and the model's replies are never stored at rest
on the server. They pass through memory, and the run buffer may hold them in Redis only as sealed (encrypted) records
that are deleted when the run ends. This package checks that promise by *planting canaries* and looking everywhere.

## What it does

1. Builds run-time-random canaries (`cnry-<label>-<16 hex>`, plus "awkward" ones with spaces, quotes, a backslash and
   an accented letter). No canary is a literal in the source, so nothing here looks like a secret to gitleaks.
2. Plants them in the prompt, in project files that tools read, in tool outputs, in the model's reply, in tool
   arguments (a path and a shell command) and in an `M2C.md` instruction file loaded into `run.start`.
3. Runs a scripted session end to end in one process: the real server (router, run handlers, loop, broker, emitter,
   metering, wiring) behind a real WebSocket, and the real device (`m2c` session, link, tool runner, file tools,
   policy, checkpoints) in a temp project. Only the model is fake (a streaming script that reads, edits and runs a
   command). Scenarios: finish, cancel, disconnect and resume, model failure, tool error, quota pause, ledger failure.
4. Scans every place the server could leave content and fails closed on any hit, in every encoded form it can think
   of (JSON, JSON-in-JSON, URL, base64 and base64url at three alignments, hex, repr, unicode escapes, UTF-16, split
   across log lines, and the random token on its own).

Scanned: every Postgres row of every table; every server and device log line; every Sentry event (the SDK is
initialised with a capturing transport and an error path is forced with a canary in the exception message); every
Redis key and value after each run ended (nothing may remain after a terminal state, and while a run is active only
sealed records may be stored); ledger rows (numbers only). The device's own local store is **expected** to hold
content and is scanned as a positive control: if it does not, the scanner is broken.

`test_scanner_selftest.py` proves the scanner itself: it plants a canary in each scanned location, in each encoded
form, and requires a hit. A scanner that cannot see is worse than none, so a blind scan is refused
(`rows_visible_check`), and a missing dependency is an error, never a skip.

## Where it lives, and why

`backend/tests/canary/`, not the repo root. It needs the backend's fixtures and pins (`backend/requirements.txt`,
`backend/pytest.ini`) and imports `eo` and `codeagent`; `m2c` is imported from `m2c/src` (installed, or found on
the path from a plain checkout).

## Run it

Install in two steps, with `--no-deps` for m2c (it pins `rich==15.0.0` and `prompt_toolkit` for the terminal UI, which the
backend's `rich==14.3.4` cannot satisfy, so pip cannot resolve the two together, and the canary test imports neither library):

```bash
cd backend && pip install -r requirements.txt && pip install --no-deps -e ../m2c
```

Because of `--no-deps`, a plain `pip check` lists m2c's own two unmet requirements (`prompt-toolkit` not installed, `rich==15.0.0`
against 14.3.4); that is expected. CI's `pip check` step accepts exactly those two lines and fails on anything else, for example a
`websockets` that no longer matches m2c's pin. Do not install m2c without `--no-deps`: pip would replace the backend's `rich` with 15.0.0.

**Fake-recorder mode** (no database; the default). The run store and the usage ledger are in-memory twins, and every
SQL statement and parameter either would send is recorded and scanned instead of table rows:

```bash
cd backend && pytest tests/canary -v
```

**Real-database mode.** Set `MINIME_TEST_DATABASE_URL` to a *scratch* database migrated through the latest file in
`backend/migrations/` (0000 to 0014 today), connected as the unprivileged `minime_app` role (see "Real-database
tests" in the root README for how to create it). The run store and ledger are the real ones, and every statement is
recorded as well. Because `minime_app` is held back by row-level security, the final scan of all tables uses a second
connection, `MINIME_TEST_SCAN_DATABASE_URL`, whose role is the table owner, a superuser or `BYPASSRLS`. If it is
unset the scan reuses the first URL, and `rows_visible_check` then fails the run rather than scanning blind.

```bash
cd backend
MINIME_TEST_DATABASE_URL=postgresql://minime_app:CHOOSE_A_PASSWORD@localhost:5432/minime_test \
MINIME_TEST_SCAN_DATABASE_URL=postgresql://postgres:ADMIN_PASSWORD@localhost:5432/minime_test \
  pytest tests/canary -v
```

The first line of the output says which mode ran. The package is not in `pytest.ini`'s default `testpaths`: name it,
as above. CI runs both modes in the `canary` job of `.github/workflows/ci.yml`.
In CI each pass writes a JUnit report and goes through the skip gate (`.github/scripts/check_skips.py`) with this
package's own allow list, `skip-allowlist.txt`: one scope per mode, so a test skipped in the wrong mode (for example
the real-database tests because the database URL went missing) fails the job instead of passing quietly.

## When it fails

A failure reads `CONTENT AT REST: N canary hit(s)` and names the place that was scanned ("server logs", "redis:
every key and value left after the run ended", "recorded SQL ...", "error-reporting payloads", ...), the canary's label
(which of prompt, file, tool output, reply, path, command or instruction file it was) and the encoded form that
matched, with an excerpt. Do not weaken the scanner. Fix the leak, or, if the
leak is understood and tracked, record it in `KNOWN_LEAKS` / `KNOWN_LEFTOVER_KEYS` in `test_no_content_at_rest.py`;
those entries run as strict `xfail`, so they fail again the moment the leak is fixed and the entry must be removed.

Known today: `metering.py` `RunBudgetStore.mark_warned` leaves `run_guard:m2c_warned:<scope>:<run_id>` in Redis
after a quota-paused run ends (value `1`, 6 h TTL; `run_manager._clear_guard` does not clear it). It holds no
content, but it breaks "nothing remains after a terminal state".
