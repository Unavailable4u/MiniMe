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
commands (`minime skills ...`, `minime mcp ...`) land in Patch A8, and
`minime attach` (daemon pairing) lands in Patch A7 -- both depend on
this package existing first.
