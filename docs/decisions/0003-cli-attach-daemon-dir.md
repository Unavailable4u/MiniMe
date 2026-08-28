# 0003 — How `minime attach` locates the daemon it's pairing

**Status:** superseded — the human-facing CLI (`cli/minime_cli/`),
including `minime attach`, was removed in Patch C0; see
`MiniMe-Patch-Series-C-Plan.md`, Track 1. Left here as the historical
record of why A7 was built the way it was.

**Status (as originally decided):** accepted
**Context:** Implementation Guide, Patch A7 (`minime attach`).

## The question

Patch A7 needs `minime attach` to reuse `daemon/config.py`'s
`generate_pairing_token()` and `daemon/path_guard.py`'s
`assert_safe_root()` rather than re-implementing the token shape or the
root-safety checks a second time in the CLI. But `cli/` is a standalone
installable package (its own `pyproject.toml`, no dependency on
`daemon/` or `backend/` — see `api_client.py`'s own docstring), because
none of A6's original commands (`ask`, `chat`, `chats`, `login`, ...)
need anything from either. So: how does a command that lives in that
standalone package get at code that lives in `daemon/`, on disk, at
runtime?

Two things are easy to conflate here and shouldn't be:
- **PROJECT_PATH** — the folder `minime attach` is pairing the daemon
  *to* (becomes `MINIME_ALLOWED_ROOT`). This is very often the current
  directory, and A7's own goal text says so.
- **the MiniMe checkout** — the folder containing `daemon/config.py`
  itself, whose `daemon/.env` this command writes. This is a
  completely different path, and there is no reason to assume it's the
  same as, or even near, PROJECT_PATH — someone pairing the daemon to
  `~/projects/soil-monitor` is not standing inside their MiniMe clone
  when they do it.

## Decision

Treat "where is the MiniMe checkout" as a fourth CLI config value —
`MINIME_DAEMON_DIR` — with the same env-var-first-then-config-file
precedence `minime_cli/config.py` already uses for `MINIME_API_URL` and
the Supabase values, settable via `minime configure --daemon-dir`. Like
the Supabase values (and unlike `api_url`), it has **no safe default**:
the CLI package and a checkout are not guaranteed to be adjacent, or
even on the same machine's Python installation, so guessing a relative
path (e.g. `../..`) would silently do the wrong thing far more often
than it would help.

At `attach` time, `minime_cli/daemon_bridge.py`:
1. Resolves `MINIME_DAEMON_DIR` (or `--daemon-dir`), and fails loudly
   with an actionable message if it's unset.
2. Confirms the folder actually contains `daemon/config.py` — not just
   any folder — before trusting it.
3. Adds it to `sys.path[0]` and imports the real `daemon.config` /
   `daemon.path_guard` modules from it.

This means `assert_safe_root()`'s disallowed-roots list and
`generate_pairing_token()`'s token shape are used, never duplicated —
if either changes later, `minime attach` picks up the change for free
instead of silently validating against a stale copy.

## Why not the alternatives

- **Assume the CLI is always run from inside the checkout.** True for
  a developer running `pip install -e cli/` straight out of their
  clone, but the CLI's own docstrings already anticipate it being
  installed separately (`pip install minime-cli` on a machine that
  only runs the daemon, or vice versa). Baking in "cwd must be the
  checkout" would also collide with PROJECT_PATH defaulting to cwd —
  the two would have to be the same folder, which defeats the point of
  pairing an arbitrary project.
- **Reimplement the validation logic in the CLI.** Rejected outright —
  this is the exact "second, parallel system" failure mode Decision 1
  in `0001-cli-skills-mcp-scope.md` already warned against for local
  file/exec capability. A duplicated safety check is a liability the
  moment the original changes and the copy doesn't.
- **Make `cli/` depend on `daemon/` as a real package dependency.**
  Would force every `minime ask`/`minime chat` install to also carry
  the daemon's own dependencies (`websockets`, etc. — see
  `daemon/requirements.txt`) for a capability most CLI installs will
  never use. The `sys.path` bridge keeps that dependency **optional
  and lazy**: it's only ever imported inside `attach`'s own code path.
