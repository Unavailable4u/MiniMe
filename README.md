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
