"""
cli/tests/test_daemon_bridge.py -- Patch A7.

Builds a fake "MiniMe checkout" on disk (just enough of daemon/ for
load_daemon_modules() to accept it and import real code from it), since
these tests should not depend on the actual daemon/ package location
relative to cli/ -- that's the exact assumption Decision 0003 says not
to make.
"""
from __future__ import annotations

import sys

import pytest

from minime_cli.daemon_bridge import DaemonBridgeError, load_daemon_modules, write_daemon_env

_FAKE_CONFIG_PY = '''
def generate_pairing_token():
    return "fake-token-from-fake-checkout"
'''

_FAKE_PATH_GUARD_PY = '''
class PathGuardError(Exception):
    pass


def assert_safe_root(root):
    from pathlib import Path
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise PathGuardError(f"not a directory: {resolved}")
    return resolved
'''


def _make_fake_checkout(tmp_path):
    checkout = tmp_path / "fake_minime_checkout"
    daemon_pkg = checkout / "daemon"
    daemon_pkg.mkdir(parents=True)
    (daemon_pkg / "__init__.py").write_text("")
    (daemon_pkg / "config.py").write_text(_FAKE_CONFIG_PY)
    (daemon_pkg / "path_guard.py").write_text(_FAKE_PATH_GUARD_PY)
    return checkout


@pytest.fixture(autouse=True)
def _clean_daemon_module_cache():
    """Each fake checkout defines its own daemon.config/daemon.path_guard
    -- don't let one test's imported module linger in sys.modules and
    get reused (import_module is a no-op if the name is already
    present) for a different checkout in a later test."""
    for name in ("daemon", "daemon.config", "daemon.path_guard"):
        sys.modules.pop(name, None)
    yield
    for name in ("daemon", "daemon.config", "daemon.path_guard"):
        sys.modules.pop(name, None)


def test_missing_daemon_dir_raises_actionable_error():
    with pytest.raises(DaemonBridgeError, match="Don't know where your MiniMe checkout is"):
        load_daemon_modules(None)


def test_daemon_dir_without_daemon_config_raises(tmp_path):
    empty_dir = tmp_path / "not_a_checkout"
    empty_dir.mkdir()
    with pytest.raises(DaemonBridgeError, match="doesn't look like a MiniMe checkout"):
        load_daemon_modules(str(empty_dir))


def test_loads_real_modules_from_checkout(tmp_path):
    checkout = _make_fake_checkout(tmp_path)

    bridge = load_daemon_modules(str(checkout))

    assert bridge.daemon_dir == checkout.resolve()
    assert bridge.env_path == checkout.resolve() / "daemon" / ".env"
    # Proves these are the real imported modules, not stand-ins --
    # calling into them actually runs the fake checkout's own code.
    assert bridge.config.generate_pairing_token() == "fake-token-from-fake-checkout"
    resolved = bridge.path_guard.assert_safe_root(tmp_path)
    assert resolved == tmp_path.resolve()


def test_switching_checkouts_does_not_reuse_stale_module(tmp_path):
    """Regression guard for the exact failure mode Decision 0003 warns
    about: a second --daemon-dir pointing at a DIFFERENT checkout must
    not silently keep validating against the first one."""
    checkout_a = _make_fake_checkout(tmp_path / "a")
    (checkout_a / "daemon" / "config.py").write_text(
        'def generate_pairing_token():\n    return "token-from-a"\n'
    )
    checkout_b = _make_fake_checkout(tmp_path / "b")
    (checkout_b / "daemon" / "config.py").write_text(
        'def generate_pairing_token():\n    return "token-from-b"\n'
    )

    bridge_a = load_daemon_modules(str(checkout_a))
    assert bridge_a.config.generate_pairing_token() == "token-from-a"

    bridge_b = load_daemon_modules(str(checkout_b))
    assert bridge_b.config.generate_pairing_token() == "token-from-b"


def test_write_daemon_env_writes_all_four_values_and_locks_permissions(tmp_path):
    env_path = tmp_path / "daemon" / ".env"

    write_daemon_env(
        env_path,
        pairing_token="tok123",
        allowed_root="/some/project",
        backend_ws_url="ws://localhost:8000",
        workspace_id="ws-abc",
    )

    contents = env_path.read_text()
    assert "MINIME_PAIRING_TOKEN=tok123" in contents
    assert "MINIME_ALLOWED_ROOT=/some/project" in contents
    assert "MINIME_BACKEND_WS_URL=ws://localhost:8000" in contents
    assert "MINIME_WORKSPACE_ID=ws-abc" in contents
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_write_daemon_env_overwrites_stale_previous_values(tmp_path):
    """attach's whole point: a re-pairing must not leave any value from
    a previous pairing sitting next to freshly-generated ones."""
    env_path = tmp_path / "daemon" / ".env"
    write_daemon_env(
        env_path, pairing_token="old-token", allowed_root="/old/project",
        backend_ws_url="ws://old-host:8000", workspace_id="old-ws",
    )

    write_daemon_env(
        env_path, pairing_token="new-token", allowed_root="/new/project",
        backend_ws_url="ws://new-host:8000", workspace_id="new-ws",
    )

    contents = env_path.read_text()
    assert "old-token" not in contents
    assert "old/project" not in contents
    assert "old-host" not in contents
    assert "old-ws" not in contents
    assert "MINIME_PAIRING_TOKEN=new-token" in contents
