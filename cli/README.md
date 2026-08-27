# MiniMe CLI

A terminal client for MiniMe -- Patch A6 of
`MiniMe_Implementation_Guide_CLI_Skills_MCP_Memory.md`. Talks to the
same backend session/messaging endpoints the web frontend uses
(`POST /api/task`, `/api/chats`, ...) over plain HTTP. No new backend
routes, no new protocol.

## Install

```bash
cd cli
pip install -e .
```

This installs a `minime` console script (see `[project.scripts]` in
`pyproject.toml`).

## Configure

Three values, same as the web frontend's own `.env.local`:

```bash
export MINIME_API_URL=http://localhost:8000               # backend base URL
export MINIME_SUPABASE_URL=https://your-project.supabase.co
export MINIME_SUPABASE_ANON_KEY=your-anon-public-key
```

...or save them once instead of exporting every session:

```bash
minime configure --api-url http://localhost:8000 \
                  --supabase-url https://your-project.supabase.co \
                  --supabase-anon-key your-anon-public-key
```

Never put `SUPABASE_SERVICE_ROLE_KEY` here -- the CLI only ever needs
the public anon key, the same one the browser bundle already ships
with. See `docs/decisions/0002-cli-auth-strategy.md` for why.

## Use

```bash
minime login                       # prompts for email/password
minime ask "summarize today's plan"
minime ask --chat <chat_id> "and what's next"   # continue that chat
minime chats                       # list your chats
minime chat                        # interactive loop, new chat
minime chat --chat <chat_id>       # interactive loop, existing chat
minime whoami
minime logout
```

## Pairing the local daemon (`minime attach`)

If you also have a MiniMe checkout on this machine (for the local
daemon -- see `daemon/README.md`), `minime attach` writes its
`daemon/.env` for you instead of hand-editing it:

```bash
minime configure --daemon-dir /path/to/MiniMe   # once, points at your checkout
cd ~/projects/soil-monitor                      # the project you want the daemon scoped to
minime attach                                   # defaults MINIME_ALLOWED_ROOT to cwd
```

You'll be shown the exact values (root folder, workspace, backend URL,
a freshly generated pairing token) and asked to confirm before
anything is written -- `attach` never silently picks a broad or
unconfirmed root. It also never starts the daemon itself; run `python
-m daemon.minime_daemon` (see `daemon/README.md`) as a separate step
once `daemon/.env` is written.

`--daemon-dir`, `--workspace-id`, and `--backend-ws-url` all override
their respective defaults for a one-off pairing without touching your
saved config. See `docs/decisions/0003-cli-attach-daemon-dir.md` for
why the checkout location is a separate config value from the project
being paired.

Every chat started here shows up in the web UI's sidebar with a
complete transcript (both turns persisted, same as the browser does)
-- it's the same chat store, just a different client.

## Not yet supported from the CLI

Scoped out of Patch A6 on purpose (see the implementation guide):
hire-review preview/confirm (`/api/task/preview`, `/api/task/confirm`),
the pause/approve flow (`approval_roles`, `/api/resume`), and workflow
templates. `minime ask` / `minime chat` always dispatch in the default
one-click `mode="auto"` path -- exactly today's default web-UI
behavior with nothing extra turned on. Skills/MCP introspection
commands (`minime skills ...`, `minime mcp ...`) land in Patch A8 and
depend on this package existing first. (`minime attach`, daemon
pairing, is Patch A7 -- see above, now implemented.)
