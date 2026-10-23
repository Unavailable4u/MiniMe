# MiniMe

## Setup

After cloning, run the one-time setup script:

```bash
./scripts/setup.sh
```

This wires `.githooks/` as this clone's git hooks path
(`core.hooksPath`) so the local pre-commit secret scan (Gitleaks) is
active — see `.githooks/pre-commit` and `.gitleaks.toml`. This step is
required per clone: `core.hooksPath` is a local git config value, not
something the repository can set for you automatically.

CI runs the full secret + static-analysis scan (Gitleaks + Semgrep,
uploaded as SARIF to GitHub's Security tab) on every push regardless —
see `.github/workflows/security-scan.yml` — but the local pre-commit hook
catches a leaked secret before it ever reaches a commit, which is worth
having too.

## Running with Docker

Both services need their env files first: copy `backend/.env.example` to
`backend/.env` and `frontend/.env.example` to `frontend/.env.local`, then
fill them in.

`docker-compose.yml` is **production-like** and is the default. Each
service runs from its own self-contained image, with no source mounted in
and no reload. The backend's runtime state (`/app/data`, `/app/apps`) lives
in named volumes, so it survives `docker compose down` and rebuilds.

```bash
docker compose up --build
```

For **local development** (backend source bind-mounted, live reload), add
the dev overlay. It needs Docker Compose 2.24.4 or newer (older versions
silently ignore a tag the overlay relies on; check `docker compose version`):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

In dev the backend runs as your own user so it can write into `./backend`.
That is uid/gid 1000 by default; on Linux with a different uid, put
`DEV_UID=$(id -u)` and `DEV_GID=$(id -g)` (as literal numbers) in a
repo-root `.env`. macOS and Windows need nothing. Only the backend is
reloaded: for frontend work, run `npm run dev` in `frontend/` on the host.

## Backend tests

```bash
cd backend
pytest tests/static -v                   # not in pytest.ini's default testpaths: name it
pytest tests/unit tests/integration -v   # real-database tests are skipped unless set up below
```

**SQL-versus-grants check (no database needed).**
`backend/tests/static/test_sql_grants.py` replays `backend/migrations/*.sql` in
order to work out what the app role (`minime_app`) may do to each table, reads
every SQL statement in the modules listed in its `SQL_MODULES`, and fails if one
needs a privilege the role lacks (for example `UPDATE` on
`workspace_code_file_versions`, which migration 0009 deliberately revokes). The
failure names the statement, the table and the missing privilege. To check
another module, add its path to `SQL_MODULES`.

**Real-database tests.**
`backend/tests/integration/test_workspace_code_files_pg.py` runs the code-file
store against a real Postgres, connected as `minime_app`, so the grants and
row-level security are actually enforced. It is skipped unless
`MINIME_TEST_DATABASE_URL` is set. Use a scratch database, never your real
project: the tests create and delete workspaces named `pgtest-*`. Using a local
Postgres:

```bash
export ADMIN_URL=postgresql://postgres:ADMIN_PASSWORD@localhost:5432/postgres
export SCRATCH=${ADMIN_URL%/*}/minime_test

psql "$ADMIN_URL" -c "create database minime_test"
psql "$ADMIN_URL" -c "create role minime_app login password 'CHOOSE_A_PASSWORD'"
# Supabase owns the auth schema; on plain Postgres, stub the one table the base schema references.
psql "$SCRATCH" -c "create schema auth" -c "create table auth.users (id uuid primary key)"
# Every file in order: 0000_base_schema.sql first, then 0001 ... latest.
for f in backend/migrations/*.sql; do psql "$SCRATCH" -v ON_ERROR_STOP=1 -q -f "$f"; done
# The test user (workspaces.owner_id references auth.users; the app role cannot write that schema).
psql "$SCRATCH" -c "insert into auth.users (id) values ('00000000-0000-4000-8000-00000000b001')"

cd backend
MINIME_TEST_DATABASE_URL=postgresql://minime_app:CHOOSE_A_PASSWORD@localhost:5432/minime_test \
  pytest tests/integration/test_workspace_code_files_pg.py -v
```

Create the role yourself, as above: migration 0003 only creates `minime_app` if it
is missing, with the password written in that file. The test fails (it does not
skip) if the URL connects as a superuser or table owner, or if the database
still lets `minime_app` `UPDATE` the version-history table, because in those
cases the tests would pass without testing the grants.

`backend/migrations/0000_base_schema.sql` is the schema that existed before
migration 0001 (it was originally run by hand in the Supabase SQL editor). It is
already applied on the live project and is safe to re-run; it exists so a scratch
database can be built from the repo alone. `scripts/public.sql` is a Supabase
"context only" dump for reading, not for running.
