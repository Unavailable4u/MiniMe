"""
daemon/minime_daemon.py — F2 Part 1: standalone daemon scaffolding.

Scope for this part, deliberately narrow: load config, start up, prove
the configured root folder is safe, and prove path_guard actually
refuses paths outside it. No websocket, no backend connection, no
pairing handshake yet -- that's Part 2. This file's only job is to be
something you can run locally and trust before it's ever given a
network connection.

Run it:
    cd MiniMe
    pip install -r daemon/requirements.txt
    cp daemon/.env.example daemon/.env
    python -c "import secrets; print(secrets.token_urlsafe(32))"  # paste into .env
    # set MINIME_ALLOWED_ROOT in daemon/.env to a real project folder
    python -m daemon.minime_daemon

What "starts up and does nothing else yet" means concretely here: it
loads config, logs its own status, runs a handful of self-checks
against the configured root (see _self_check below) so a misconfigured
root is caught immediately and loudly instead of surfacing later as a
confusing Part 3/4 bug, then idles. Ctrl+C to stop.

Place this file at: daemon/minime_daemon.py
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

from daemon.config import ConfigError, DaemonConfig, load_config
from daemon.path_guard import PathGuardError, assert_within_root

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("minime_daemon")

_running = True


def _handle_shutdown(signum, frame) -> None:  # noqa: ANN001 -- signal handler sig
    global _running
    logger.info("shutdown signal received (%s), stopping", signum)
    _running = False


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


def main() -> int:
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
        "minime_daemon starting -- root=%s, pairing token loaded (%d chars)",
        config.allowed_root,
        len(config.pairing_token),
    )
    logger.info(
        "no backend connection in this build (F2 Part 1 scope) -- "
        "idling until Part 2 wires the websocket handshake"
    )

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    while _running:
        time.sleep(1)

    logger.info("minime_daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
