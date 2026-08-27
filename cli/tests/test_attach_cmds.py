"""
cli/tests/test_attach_cmds.py -- Patch A7.

Exercises `minime attach` through Click's own CliRunner, with
load_daemon_modules/write_daemon_env and the ApiClient monkeypatched at
the attach_cmds module boundary -- these tests are about attach's own
flow (confirmation, workspace resolution, ws-url derivation), not a
re-test of daemon_bridge.py (see test_daemon_bridge.py) or ApiClient
(no such test module exists yet; api_client.py is a thin, uncached
requests wrapper).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from minime_cli.commands import attach_cmds
from minime_cli.config import Config
from minime_cli.daemon_bridge import DaemonBridgeError


class _FakePathGuard:
    class PathGuardError(Exception):
        pass

    @staticmethod
    def assert_safe_root(root):
        resolved = Path(root).resolve()
        if resolved == Path("/"):
            raise _FakePathGuard.PathGuardError("too broad")
        return resolved


class _FakeDaemonConfig:
    @staticmethod
    def generate_pairing_token():
        return "fake-generated-token"


def _fake_bridge(tmp_path):
    daemon_dir = tmp_path / "checkout"
    return SimpleNamespace(
        daemon_dir=daemon_dir,
        env_path=daemon_dir / "daemon" / ".env",
        config=_FakeDaemonConfig,
        path_guard=_FakePathGuard,
    )


@pytest.fixture
def fake_cfg():
    return Config(
        api_url="http://localhost:8000",
        supabase_url="https://proj.supabase.co",
        supabase_anon_key="anon-key",
        daemon_dir="/fake/daemon/dir",
    )


@pytest.fixture(autouse=True)
def _patch_load_config(monkeypatch, fake_cfg):
    monkeypatch.setattr(attach_cmds, "load_config", lambda: fake_cfg)


def test_missing_daemon_dir_is_a_clean_cli_error(monkeypatch, fake_cfg):
    def _raise(_daemon_dir):
        raise DaemonBridgeError("Don't know where your MiniMe checkout is.")

    monkeypatch.setattr(attach_cmds, "load_daemon_modules", _raise)

    result = CliRunner().invoke(attach_cmds.attach, [])

    assert result.exit_code != 0
    assert "Don't know where your MiniMe checkout is" in result.output


def test_declining_confirmation_does_not_write_env(monkeypatch, tmp_path):
    bridge = _fake_bridge(tmp_path)
    monkeypatch.setattr(attach_cmds, "load_daemon_modules", lambda d: bridge)
    monkeypatch.setattr(
        attach_cmds, "_resolve_workspace_id", lambda client, explicit: "ws-123"
    )
    written = {}
    monkeypatch.setattr(
        attach_cmds, "write_daemon_env",
        lambda *a, **kw: written.setdefault("called", True),
    )

    result = CliRunner().invoke(attach_cmds.attach, [str(tmp_path)], input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert "called" not in written


def test_yes_flag_skips_prompt_and_writes(monkeypatch, tmp_path):
    bridge = _fake_bridge(tmp_path)
    monkeypatch.setattr(attach_cmds, "load_daemon_modules", lambda d: bridge)
    monkeypatch.setattr(
        attach_cmds, "_resolve_workspace_id", lambda client, explicit: "ws-123"
    )
    calls = {}

    def _fake_write(env_path, **kwargs):
        calls["env_path"] = env_path
        calls.update(kwargs)

    monkeypatch.setattr(attach_cmds, "write_daemon_env", _fake_write)

    result = CliRunner().invoke(attach_cmds.attach, [str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert calls["env_path"] == bridge.env_path
    assert calls["pairing_token"] == "fake-generated-token"
    assert calls["allowed_root"] == str(Path(tmp_path).resolve())
    assert calls["workspace_id"] == "ws-123"
    assert calls["backend_ws_url"] == "ws://localhost:8000"  # derived from api_url
    assert "Wrote" in result.output


def test_explicit_workspace_id_skips_the_workspace_prompt(monkeypatch, tmp_path):
    bridge = _fake_bridge(tmp_path)
    monkeypatch.setattr(attach_cmds, "load_daemon_modules", lambda d: bridge)

    def _resolve_should_not_be_called_for_listing(client, explicit):
        assert explicit == "explicit-ws-id"
        return explicit

    monkeypatch.setattr(attach_cmds, "_resolve_workspace_id", _resolve_should_not_be_called_for_listing)
    monkeypatch.setattr(attach_cmds, "write_daemon_env", lambda *a, **kw: None)

    result = CliRunner().invoke(
        attach_cmds.attach, [str(tmp_path), "--workspace-id", "explicit-ws-id", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "explicit-ws-id" in result.output


def test_unsafe_root_is_rejected_before_any_write(monkeypatch, tmp_path):
    bridge = _fake_bridge(tmp_path)
    monkeypatch.setattr(attach_cmds, "load_daemon_modules", lambda d: bridge)
    write_called = {}
    monkeypatch.setattr(
        attach_cmds, "write_daemon_env", lambda *a, **kw: write_called.setdefault("x", True)
    )

    result = CliRunner().invoke(attach_cmds.attach, ["/"])

    assert result.exit_code != 0
    assert "write_called" not in locals() or "x" not in write_called


@pytest.mark.parametrize(
    "api_url, expected",
    [
        ("http://localhost:8000", "ws://localhost:8000"),
        ("https://api.minime.example", "wss://api.minime.example"),
    ],
)
def test_default_backend_ws_url_derivation(api_url, expected):
    assert attach_cmds._default_backend_ws_url(api_url) == expected


def test_default_backend_ws_url_rejects_unknown_scheme():
    import click

    with pytest.raises(click.ClickException):
        attach_cmds._default_backend_ws_url("ftp://weird")
