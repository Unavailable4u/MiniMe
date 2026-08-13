# MiniMe local daemon (F2)

A small companion process that runs **on your own machine** — not part
of the backend's Render/Docker deploy. It's what lets MiniMe read/write/
execute inside one folder you point it at.

## Part 1 scope (this build)

- Loads config: a pairing token and one allowed root folder
  (`daemon/.env`, see `.env.example`).
- Validates the root folder is real, is a directory, and isn't
  something dangerously broad like `/` or your home directory.
- Starts up, runs a self-check proving it actually refuses paths
  outside the configured root (including `../..` traversal and
  symlinks that point outside the root), then idles.
- **No backend connection yet.** That's Part 2 (websocket handshake +
  session registry) and Part 3 (the actual `list_dir`/`read_file` tool
  calls).

## Setup

```bash
cd MiniMe
pip install -r daemon/requirements.txt
cp daemon/.env.example daemon/.env
python -m daemon.config --generate-token   # paste the output into MINIME_PAIRING_TOKEN
```

Edit `daemon/.env` and set `MINIME_ALLOWED_ROOT` to the one project
folder you want the daemon scoped to, e.g.:

```
MINIME_ALLOWED_ROOT=/Users/you/projects/soil-monitor
```

## Run it

```bash
python -m daemon.minime_daemon
```

Expected output on success:

```
... config loaded: allowed root = /Users/you/projects/soil-monitor
... self-check: allowed root is /Users/you/projects/soil-monitor
... self-check PASS: paths inside the root are accepted
... self-check PASS: paths outside the root are rejected
... self-check PASS: '../..' traversal is rejected
... minime_daemon starting -- root=..., pairing token loaded (43 chars)
... no backend connection in this build (F2 Part 1 scope) -- idling until Part 2 wires the websocket handshake
```

Ctrl+C to stop. If it exits immediately with a config error instead,
fix whatever `daemon/.env` issue it names and rerun.

## Tests

```bash
pip install pytest  # if not already installed via backend/requirements.txt
pytest daemon/tests/ -v
```

## Files

- `minime_daemon.py` — entry point: load config, self-check, idle.
- `config.py` — `.env` loading + validation for the two config values.
- `path_guard.py` — the containment boundary (`assert_safe_root`,
  `assert_within_root`) that every later part's tool calls must route
  through.
- `tests/` — unit tests for both of the above.
