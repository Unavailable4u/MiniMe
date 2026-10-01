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

## Preview proxy (W7.4, optional)

Lets MiniMe's workbench preview **your own dev server** with the
inspector working, without editing your app. Off unless you set
`MINIME_PREVIEW_TARGET`.

```
MINIME_PREVIEW_TARGET=http://localhost:5173     # your dev server (this machine only)
MINIME_FRONTEND_ORIGIN=http://localhost:3000    # where MiniMe's web app runs
# MINIME_PREVIEW_PORT=5199                      # optional; keep it fixed
```

Restart the daemon and it logs `preview proxy ready: enter
http://127.0.0.1:5199 ...`. Paste that address into the preview's
**Dev server URL** box (Preview → Dev server URL) instead of your dev
server's own. The proxy, on `127.0.0.1` only:

- strips `X-Frame-Options` and the CSP `frame-ancestors` directive (the
  rest of your CSP is kept), so the page can render inside the preview;
- injects the inspector `<script>` into HTML *pages* (not into JSON,
  scripts, or HTML fragments your app `fetch()`es), so the crosshair
  works. If you already pasted the snippet by hand it isn't added twice;
- tunnels WebSockets, so Vite / webpack / Next hot reload keeps working;
- rewrites `Host`/`Origin`/`Referer` and redirect `Location`s so your dev
  server sees its own address.

It does **not** make clicks map to source lines by itself: your build
still needs `@locator/babel-jsx` / `@locator/webpack-loader` (or `data-mm`
attributes) -- see the preview's "Set up click-to-code" panel.

Safety: it forwards only to the one loopback target you configured,
refuses requests whose `Host` isn't `127.0.0.1:<port>` / `localhost:<port>`
(DNS-rebinding), and is not usable as a general proxy. Details are in
`preview_proxy.py`'s header.

Known limits: a CSP set via `<meta http-equiv>` is left alone; an app that
sends `Cross-Origin-Embedder-Policy: require-corp` will block the
injected script; `Set-Cookie` `Domain=` attributes aren't rewritten;
HTTP/1.1 only; a page over 16 MB isn't edited (502). If the daemon can't
bind the port it logs why and keeps running without the proxy.

To try the proxy alone, without pairing to the backend:

```bash
python -m daemon.preview_proxy --target http://localhost:5173 --frontend-origin http://localhost:3000
```

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
- `preview_proxy.py` — W7.4's reverse proxy (also runnable on its own:
  `python -m daemon.preview_proxy --help`).
- `tests/` — unit tests for the above; `test_preview_proxy.py` also runs
  the real proxy in front of a fake dev server on loopback sockets.
