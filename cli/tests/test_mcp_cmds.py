"""
cli/tests/test_mcp_cmds.py -- Patch A8.

Exercises `minime mcp list` / `minime mcp status <name>` through
Click's own CliRunner, with ApiClient monkeypatched at the mcp_cmds
module boundary -- same posture test_skills_cmds.py and
test_attach_cmds.py already use.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from minime_cli.api_client import ApiError
from minime_cli.commands import mcp_cmds
from minime_cli.config import ConfigError


class _FakeClient:
    def __init__(self, servers=None, status=None, list_error=None, status_error=None):
        self._servers = servers or []
        self._status = status
        self._list_error = list_error
        self._status_error = status_error

    def list_mcp_servers(self):
        if self._list_error:
            raise self._list_error
        return self._servers

    def mcp_server_status(self, server_name):
        if self._status_error:
            raise self._status_error
        return self._status


@pytest.fixture
def runner():
    return CliRunner()


def test_list_prints_name_state_and_transport(monkeypatch, runner):
    fake = _FakeClient(servers=[
        {"name": "github", "enabled": True, "connected": True, "transport": "http"},
        {"name": "context7", "enabled": True, "connected": False, "transport": "stdio"},
        {"name": "old-server", "enabled": False, "connected": False, "transport": "stdio"},
    ])
    monkeypatch.setattr(mcp_cmds, "_client", lambda: fake)

    result = runner.invoke(mcp_cmds.list_servers_cmd)
    assert result.exit_code == 0
    assert "github\tconnected\thttp" in result.output
    assert "context7\tenabled\tstdio" in result.output
    assert "old-server\tdisabled\tstdio" in result.output


def test_list_with_no_servers_prints_a_friendly_message(monkeypatch, runner):
    monkeypatch.setattr(mcp_cmds, "_client", lambda: _FakeClient(servers=[]))
    result = runner.invoke(mcp_cmds.list_servers_cmd)
    assert result.exit_code == 0
    assert "No MCP servers configured." in result.output


def test_list_turns_an_api_error_into_a_clean_click_exception(monkeypatch, runner):
    monkeypatch.setattr(
        mcp_cmds, "_client", lambda: _FakeClient(list_error=ApiError("500 Internal Server Error")),
    )
    result = runner.invoke(mcp_cmds.list_servers_cmd)
    assert result.exit_code != 0
    assert "500" in result.output


def test_status_prints_the_full_record_including_tools(monkeypatch, runner):
    fake = _FakeClient(status={
        "name": "github",
        "enabled": True,
        "transport": "http",
        "connected": True,
        "default_tool_trust": "read_only",
        "tools": [
            {"name": "search_issues", "description": "Search issues", "trust": "read_only"},
        ],
    })
    monkeypatch.setattr(mcp_cmds, "_client", lambda: fake)

    result = runner.invoke(mcp_cmds.server_status_cmd, ["github"])
    assert result.exit_code == 0
    assert "name:               github" in result.output
    assert "connected:          True" in result.output
    assert "search_issues" in result.output
    assert "[read_only]" in result.output


def test_status_for_an_unknown_server_prints_the_error_field_without_raising(monkeypatch, runner):
    fake = _FakeClient(status={"name": "nonexistent", "error": "not found in mcp_servers.json"})
    monkeypatch.setattr(mcp_cmds, "_client", lambda: fake)

    result = runner.invoke(mcp_cmds.server_status_cmd, ["nonexistent"])
    assert result.exit_code == 0
    assert "nonexistent: not found in mcp_servers.json" in result.output


def test_status_with_a_tools_error_stops_before_printing_a_tools_section(monkeypatch, runner):
    fake = _FakeClient(status={
        "name": "github", "enabled": True, "transport": "http", "connected": True,
        "default_tool_trust": "read_only", "tools_error": "handshake timed out",
    })
    monkeypatch.setattr(mcp_cmds, "_client", lambda: fake)

    result = runner.invoke(mcp_cmds.server_status_cmd, ["github"])
    assert result.exit_code == 0
    assert "tools_error:        handshake timed out" in result.output
    assert "tools:" not in result.output


def test_status_turns_a_config_error_into_a_clean_click_exception(monkeypatch, runner):
    def _raise_config_error():
        raise ConfigError("Missing: MINIME_SUPABASE_URL")
    monkeypatch.setattr(mcp_cmds, "_client", _raise_config_error)
    result = runner.invoke(mcp_cmds.server_status_cmd, ["github"])
    assert result.exit_code != 0
    assert "MINIME_SUPABASE_URL" in result.output
