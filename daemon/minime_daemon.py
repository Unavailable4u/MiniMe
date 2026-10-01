"""
daemon/minime_daemon.py — F2 Part 1 + Part 2.

Part 1 scope (unchanged): load config, prove the configured root
folder is safe, and prove path_guard actually refuses paths outside
it -- all before this process is ever given a network connection.

Part 2 adds the actual backend connection: once the self-checks pass,
this now runs daemon/connection.py's run_forever(), which connects out
to MINIME_BACKEND_WS_URL, pairs via the handshake eo/local_workspace.py
implements on the backend side, and idles on that socket (reconnecting
with backoff on drop) until Ctrl+C/SIGTERM. Still no tool-call message
shape -- that's Part 3.

W7.4 adds an optional preview reverse proxy (daemon/preview_proxy.py),
started next to the websocket connection when MINIME_PREVIEW_TARGET is
set in daemon/.env. It's independent of pairing: if it can't bind its
port the daemon logs why and carries on without it.

Run it:
    cd MiniMe
    pip install -r daemon/requirements.txt
    cp daemon/.env.example daemon/.env
    python -c "import secrets; print(secrets.token_urlsafe(32))"  # paste into .env
    # set MINIME_ALLOWED_ROOT, MINIME_BACKEND_WS_URL, and
    # MINIME_WORKSPACE_ID in daemon/.env
    python -m daemon.minime_daemon

Place this file at: daemon/minime_daemon.py
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from daemon import connection
from daemon.config import ConfigError, DaemonConfig, load_config
from daemon.path_guard import PathGuardError, assert_within_root
from daemon.preview_proxy import PreviewProxy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("minime_daemon")


def _self_check(config: DaemonConfig) -> None:
    """Proves containment actually works against the *real* configured
    root, not just in unit tests against a tmp_path fixture. Logs a
    clear pass/fail for each check rather than asserting, since this
    runs at startup in front of the person running the daemon, not in
    CI.
    """
    root = config.allowed_root
    logger.info("self-check: allowed root is %s", root)

    # 1. A path clearly inside the root must be accepted.
    inside_probe = root / ".minime-daemon-self-check"
    try:
        assert_within_root(inside_probe, root)
        logger.info("self-check PASS: paths inside the root are accepted")
    except PathGuardError as exc:
        logger.error("self-check FAIL (inside-root case): %s", exc)
        raise

    # 2. A path clearly outside the root must be rejected.
    outside_probe = root.parent / "minime-daemon-outside-probe"
    try:
        assert_within_root(outside_probe, root)
        logger.error(
            "self-check FAIL: a path outside the root (%s) was NOT rejected "
            "-- refusing to start",
            outside_probe,
        )
        raise PathGuardError("containment self-check failed")
    except PathGuardError:
        logger.info("self-check PASS: paths outside the root are rejected")

    # 3. A classic traversal attempt must be rejected even though the
    # string itself starts inside the root.
    traversal_probe = root / ".." / ".." / "etc" / "passwd"
    try:
        assert_within_root(traversal_probe, root)
        logger.error(
            "self-check FAIL: a '../..' traversal path was NOT rejected "
            "-- refusing to start"
        )
        raise PathGuardError("traversal self-check failed")
    except PathGuardError:
        logger.info("self-check PASS: '../..' traversal is rejected")


async def _start_preview_proxy(config: DaemonConfig) -> PreviewProxy | None:
    """W7.4: start the preview proxy if it's configured. A proxy that
    can't start (usually: port already in use) must not take the daemon
    down with it -- the daemon's real job is the workspace tools."""
    if config.preview is None:
        return None
    proxy = PreviewProxy(config.preview)
    try:
        await proxy.start()
    except OSError as exc:
        logger.error(
            "preview proxy could not listen on 127.0.0.1:%s (%s) -- continuing without it; "
            "set MINIME_PREVIEW_PORT to a free port",
            config.preview.port,
            exc,
        )
        return None
    logger.info(
        "preview proxy ready: enter %s as the Dev server URL in MiniMe -- it forwards to %s "
        "and adds the inspector",
        proxy.url,
        config.preview.target_origin,
    )
    return proxy


async def _amain() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("config error: %s", exc)
        return 1

    try:
        _self_check(config)
    except PathGuardError:
        logger.error("startup self-check failed, not starting")
        return 1

    logger.info(
        "minime_daemon starting -- root=%s, workspace=%s, backend=%s, "
        "pairing token loaded (%d chars)",
        config.allowed_root,
        config.workspace_id,
        config.backend_ws_url,
        len(config.pairing_token),
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_shutdown(signum: int) -> None:
        logger.info("shutdown signal received (%s), stopping", signum)
        stop_event.set()

    # signal.signal (not loop.add_signal_handler) for the same reason
    # Part 1 used it: it's the one registration API that behaves the
    # same on Windows and Unix, and this daemon is meant to run on
    # whatever machine the user happens to be developing on.
    signal.signal(signal.SIGINT, lambda signum, frame: loop.call_soon_threadsafe(_handle_shutdown, signum))
    signal.signal(signal.SIGTERM, lambda signum, frame: loop.call_soon_threadsafe(_handle_shutdown, signum))

    # NEW — Part 2: replaces Part 1's plain idle-sleep loop. Connects
    # out to the backend, pairs, and reconnects with backoff on drop,
    # until stop_event is set above.
    preview_proxy = await _start_preview_proxy(config)
    try:
        await connection.run_forever(config, stop_event)
    finally:
        if preview_proxy is not None:
            await preview_proxy.stop()

    logger.info("minime_daemon stopped")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
