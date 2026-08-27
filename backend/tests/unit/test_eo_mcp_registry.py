"""
tests/unit/test_eo_mcp_registry.py — Patch A2.

Two concerns, tested separately:

  - Config loading/validation/env-expansion (load_server_configs,
    classify_tool) is pure and synchronous -- tested against small
    temp JSON files, no mcp_client involvement at all.
  - connect_configured_servers()/mcp_server_status() actually call
    into eo.mcp_client -- those are monkeypatched to fake awaitables
    rather than performing a real handshake, same "fake the transport,
    exercise the real call/response wiring" trade-off
    test_eo_mcp_client.py documents for A1's own tests. This module
    doesn't re-test A1's handshake logic; it only tests that IT calls
    connect_server() with the right arguments and handles success/
    failure per server correctly.

Same shared-module-state discipline as test_eo_mcp_client.py: clear
eo.mcp_client's `_connections` dict before and after every test that
touches connection state.
"""
import json

import pytest

import eo.mcp_client as mcp_client
from eo.mcp_client import MCPClientError
from eo.mcp_registry import (
    MCPRegistryError,
    classify_tool,
    connect_configured_servers,
    list_mcp_servers,
    load_server_configs,
    mcp_server_status,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_module_state():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


def _write_config(tmp_path, servers):
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"_comment": ["test fixture"], "servers": servers}))
    return str(path)


_GITHUB_ENTRY = {
    "name": "github",
    "enabled": True,
    "transport": "http",
    "url": "https://api.githubcopilot.com/mcp/",
    "headers": {"Authorization": "Bearer ${TEST_GITHUB_TOKEN}"},
    "default_tool_trust": "mutating",
    "tool_trust_overrides": {"search_issues": "read_only"},
}

_CONTEXT7_ENTRY = {
    "name": "context7",
    "enabled": False,
    "transport": "stdio",
    "command": ["npx", "-y", "@upstash/context7-mcp"],
    "env": {"CONTEXT7_API_KEY": "${TEST_CONTEXT7_KEY}"},
    "default_tool_trust": "read_only",
}


# ---------------------------------------------------------------------
# Config loading / validation
# ---------------------------------------------------------------------

def test_load_server_configs_parses_both_transports(tmp_path):
    path = _write_config(tmp_path, [_GITHUB_ENTRY, _CONTEXT7_ENTRY])
    configs = load_server_configs(path)
    assert {c.name for c in configs} == {"github", "context7"}

    github = next(c for c in configs if c.name == "github")
    assert github.transport == "http"
    assert github.enabled is True
    assert github.default_tool_trust == "mutating"
    assert github.tool_trust_overrides == {"search_issues": "read_only"}

    context7 = next(c for c in configs if c.name == "context7")
    assert context7.transport == "stdio"
    assert context7.enabled is False
    assert context7.command == ["npx", "-y", "@upstash/context7-mcp"]


def test_env_placeholders_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GITHUB_TOKEN", "s3cr3t")
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    configs = load_server_configs(path)
    assert configs[0].headers["Authorization"] == "Bearer s3cr3t"


def test_unset_env_var_expands_to_empty_string_not_error(tmp_path, monkeypatch):
    # CONTEXT7_API_KEY-shaped case: an unset var is not a load-time
    # error (see eo.mcp_registry._expand's own docstring on why) -- a
    # real connection attempt is what should fail, not config loading.
    monkeypatch.delenv("TEST_GITHUB_TOKEN", raising=False)
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    configs = load_server_configs(path)
    assert configs[0].headers["Authorization"] == "Bearer "


@pytest.mark.parametrize("bad_field,bad_value", [
    ("transport", "websocket"),
    ("default_tool_trust", "sometimes"),
])
def test_invalid_enum_fields_raise_registry_error(tmp_path, bad_field, bad_value):
    entry = dict(_GITHUB_ENTRY)
    entry[bad_field] = bad_value
    path = _write_config(tmp_path, [entry])
    with pytest.raises(MCPRegistryError):
        load_server_configs(path)


def test_stdio_without_command_raises(tmp_path):
    entry = dict(_CONTEXT7_ENTRY)
    del entry["command"]
    path = _write_config(tmp_path, [entry])
    with pytest.raises(MCPRegistryError, match="requires a non-empty 'command'"):
        load_server_configs(path)


def test_http_without_url_raises(tmp_path):
    entry = dict(_GITHUB_ENTRY)
    del entry["url"]
    path = _write_config(tmp_path, [entry])
    with pytest.raises(MCPRegistryError, match="requires a 'url'"):
        load_server_configs(path)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(MCPRegistryError, match="not found"):
        load_server_configs(str(tmp_path / "does_not_exist.json"))


# ---------------------------------------------------------------------
# classify_tool -- Patch A4's future accessor
# ---------------------------------------------------------------------

def test_classify_tool_uses_override_then_default(tmp_path):
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    assert classify_tool("github", "search_issues", path=path) == "read_only"  # override
    assert classify_tool("github", "create_pull_request", path=path) == "mutating"  # default


def test_classify_tool_unknown_server_or_tool_fails_closed(tmp_path):
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    assert classify_tool("some_other_server", "anything", path=path) == "mutating"


# ---------------------------------------------------------------------
# connect_configured_servers / list_mcp_servers / mcp_server_status
# ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_connect_configured_servers_skips_disabled(tmp_path, monkeypatch):
    calls = []

    async def fake_connect_server(name, **kwargs):
        calls.append(name)

    monkeypatch.setattr(mcp_client, "connect_server", fake_connect_server)
    path = _write_config(tmp_path, [_GITHUB_ENTRY, _CONTEXT7_ENTRY])  # context7 is enabled=False

    results = await connect_configured_servers(path)
    assert calls == ["github"]
    assert results == {"github": None}


@pytest.mark.anyio
async def test_connect_configured_servers_continues_past_one_failure(tmp_path, monkeypatch):
    enabled_context7 = dict(_CONTEXT7_ENTRY, enabled=True)
    path = _write_config(tmp_path, [_GITHUB_ENTRY, enabled_context7])

    async def fake_connect_server(name, **kwargs):
        if name == "github":
            raise MCPClientError("bad credentials")
        # context7 "succeeds"

    monkeypatch.setattr(mcp_client, "connect_server", fake_connect_server)

    results = await connect_configured_servers(path)
    # Both servers were attempted -- github's failure didn't short-circuit context7.
    assert results["github"] == "bad credentials"
    assert results["context7"] is None


@pytest.mark.anyio
async def test_connect_configured_servers_passes_through_config_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GITHUB_TOKEN", "s3cr3t")
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    captured = {}

    async def fake_connect_server(name, **kwargs):
        captured[name] = kwargs

    monkeypatch.setattr(mcp_client, "connect_server", fake_connect_server)
    await connect_configured_servers(path)

    assert captured["github"]["transport"] == "http"
    assert captured["github"]["url"] == "https://api.githubcopilot.com/mcp/"
    assert captured["github"]["headers"] == {"Authorization": "Bearer s3cr3t"}


def test_list_mcp_servers_reports_connection_state(tmp_path):
    path = _write_config(tmp_path, [_GITHUB_ENTRY, _CONTEXT7_ENTRY])
    mcp_client._connections["github"] = object()  # fake a live connection without a real handshake

    servers = {s["name"]: s for s in list_mcp_servers(path)}
    assert servers["github"]["connected"] is True
    assert servers["github"]["enabled"] is True
    assert servers["context7"]["connected"] is False
    assert servers["context7"]["enabled"] is False


@pytest.mark.anyio
async def test_mcp_server_status_unknown_server(tmp_path):
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    status = await mcp_server_status("nonexistent", path)
    assert status == {"name": "nonexistent", "error": "not found in mcp_servers.json"}


@pytest.mark.anyio
async def test_mcp_server_status_not_connected_skips_tool_list(tmp_path):
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    status = await mcp_server_status("github", path)
    assert status["connected"] is False
    assert "tools" not in status


@pytest.mark.anyio
async def test_mcp_server_status_connected_includes_classified_tools(tmp_path, monkeypatch):
    path = _write_config(tmp_path, [_GITHUB_ENTRY])
    mcp_client._connections["github"] = object()

    async def fake_list_tools(name, refresh=False):
        return [
            mcp_client.MCPTool(name="search_issues", description="Search issues", input_schema={}, server_name="github"),
            mcp_client.MCPTool(name="create_pull_request", description="Open a PR", input_schema={}, server_name="github"),
        ]

    monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)

    status = await mcp_server_status("github", path)
    tools = {t["name"]: t["trust"] for t in status["tools"]}
    assert tools == {"search_issues": "read_only", "create_pull_request": "mutating"}
