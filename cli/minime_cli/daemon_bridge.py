"""
minime_cli/daemon_bridge.py -- Patch A7's actual "reuse, don't reinvent"
mechanism.

`cli/` is deliberately a standalone installable (its own pyproject.toml,
no dependency on `daemon/` or `backend/` -- see api_client.py's own
docstring), because `minime ask` / `minime chat` need nothing from
either. `minime attach` is the one command that's different: pairing
the local daemon inherently means editing a file that lives inside a
MiniMe checkout's `daemon/` folder, on this same machine, in exactly
the shape that checkout's own `daemon/config.py` will read back at
daemon startup.

Rather than duplicating `generate_pairing_token()`'s token shape or
`assert_safe_root()`'s disallowed-roots list here -- and risking the
two copies quietly disagreeing about what's "safe" the next time one
of them changes -- this module locates a real MiniMe checkout on disk
and imports `daemon.config` / `daemon.path_guard` directly from it, the
same modules `python -m daemon.minime_daemon` (run from inside that
checkout, per daemon/README.md) already uses.

See docs/decisions/0003-cli-attach-daemon-dir.md for why locating the
checkout is a config value (MINIME_DAEMON_DIR / `minime configure
--daemon-dir`) rather than the CLI guessing at a relative path -- the
CLI package and the checkout it pairs are not guaranteed to be
adjacent, or even on the same Python installation.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


class DaemonBridgeError(RuntimeError):
    """Raised when no daemon dir was configured, or the configured one
    doesn't actually look like a MiniMe checkout. Caught at the CLI's
    top level (same posture as ConfigError/AuthError) and turned into
    a one-line, actionable stderr message instead of an ImportError
    three frames deep in whatever tries to use the bridge next."""


@dataclass(frozen=True)
class DaemonBridge:
    daemon_dir: Path  # the MiniMe checkout root (the folder *containing* daemon/)
    env_path: Path  # daemon_dir / "daemon" / ".env" -- what `attach` writes
    config: ModuleType  # the real daemon.config module, imported live
    path_guard: ModuleType  # the real daemon.path_guard module, imported live


def load_daemon_modules(daemon_dir: str | None) -> DaemonBridge:
    """Resolves `daemon_dir`, sanity-checks it actually contains a
    `daemon/config.py` (not just an empty or wrong folder), and imports
    the real `daemon.config` / `daemon.path_guard` modules from it.

    Inserts `daemon_dir` at `sys.path[0]` rather than appending it, so
    a same-named module already importable elsewhere on the path can't
    shadow the real one this command needs to call.
    """
    if not daemon_dir:
        raise DaemonBridgeError(
            "Don't know where your MiniMe checkout is. Set MINIME_DAEMON_DIR "
            "to the folder that contains daemon/ (e.g. the root of your "
            "`git clone` of MiniMe), run `minime configure --daemon-dir "
            "/path/to/MiniMe` to save it, or pass --daemon-dir explicitly."
        )

    resolved = Path(daemon_dir).expanduser().resolve()
    daemon_pkg = resolved / "daemon"
    config_module_path = daemon_pkg / "config.py"
    if not config_module_path.is_file():
        raise DaemonBridgeError(
            f"{resolved} doesn't look like a MiniMe checkout -- expected to "
            f"find {config_module_path}. Point --daemon-dir / MINIME_DAEMON_DIR "
            "at the folder that CONTAINS daemon/, not the daemon/ folder itself."
        )

    resolved_str = str(resolved)
    if resolved_str not in sys.path:
        sys.path.insert(0, resolved_str)

    # Force a fresh import resolved against sys.path as it stands right
    # now, rather than reusing whatever "daemon"/"daemon.config" may
    # already sit in sys.modules. import_module() is a no-op for an
    # already-imported name -- it does NOT re-resolve the path -- so
    # without this, a second `minime attach` run against a DIFFERENT
    # --daemon-dir in the same process (a later CLI invocation reusing
    # a warm interpreter, or these very tests) would silently keep
    # validating against the FIRST checkout's daemon.config. Popping by
    # name (not reload()) is required: reload() re-executes the
    # existing module object using its own cached __spec__/__loader__,
    # which still points at the old file -- it does not re-discover the
    # module from sys.path either.
    for name in ("daemon", "daemon.config", "daemon.path_guard"):
        sys.modules.pop(name, None)

    daemon_config = importlib.import_module("daemon.config")
    daemon_path_guard = importlib.import_module("daemon.path_guard")

    return DaemonBridge(
        daemon_dir=resolved,
        env_path=daemon_pkg / ".env",
        config=daemon_config,
        path_guard=daemon_path_guard,
    )


def write_daemon_env(env_path: Path, *, pairing_token: str, allowed_root: str,
                      backend_ws_url: str, workspace_id: str) -> None:
    """Writes a complete daemon/.env from scratch, in the same
    key=value shape as daemon/.env.example.

    Deliberately a full overwrite, not a merge: `attach`'s whole point
    is producing one complete, internally-consistent set of the four
    values `daemon.config.load_config()` requires -- a merge could
    leave a stale value from a previous pairing sitting next to three
    freshly-generated ones (e.g. an old MINIME_WORKSPACE_ID next to a
    brand-new MINIME_PAIRING_TOKEN), which is exactly the kind of
    inconsistent state a human hand-editing the file could introduce,
    and this command exists to avoid that.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        "# Written by `minime attach` -- see daemon/.env.example for what\n"
        "# each of these means.\n"
        f"MINIME_PAIRING_TOKEN={pairing_token}\n"
        f"MINIME_ALLOWED_ROOT={allowed_root}\n"
        f"MINIME_BACKEND_WS_URL={backend_ws_url}\n"
        f"MINIME_WORKSPACE_ID={workspace_id}\n"
    )
    # 0600: holds a live pairing secret, same posture as
    # minime_cli/config.py's own CREDENTIALS_FILE.
    env_path.chmod(0o600)
