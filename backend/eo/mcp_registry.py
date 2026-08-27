"""
eo/mcp_registry.py — Patch A2: MCP Server Registry & Config.

This is the "which MCP servers exist and are turned on" layer that
sits on top of Patch A1's transport-and-handshake client
(eo/mcp_client.py). A1's own docstring names this module and its
config file explicitly as the intended next step -- nothing here
duplicates what A1 already built; this module is a thin loader +
lifecycle-wiring layer over it.

What this patch owns:
  - Reading backend/config/mcp_servers.json (this repo's convention for
    small, checked-in JSON config -- see eo/graph_edges.py's
    _edges.json / eo/chat_workspace.py's _workspaces.json for the same
    "one JSON file, no DB migration needed" shape, though those two are
    read/write app data and this one is read-only static config).
  - Expanding `${VAR_NAME}` placeholders in that file's `env`/`headers`
    values against real environment variables at connect time, so no
    real secret is ever checked into mcp_servers.json itself -- same
    split every provider key in backend/.env.example already follows.
  - connect_configured_servers(): calls eo.mcp_client.connect_server()
    once per enabled server at backend startup (wired into
    api/server.py's `_lifespan`). One server failing to connect (bad
    token, `npx` not on PATH, rate-limited handshake, etc.) is logged
    and skipped, not fatal to the other two or to the backend starting
    up at all -- same "one down LLM provider doesn't take out the
    others" posture utils/llm_client.py's provider pool already has.
  - classify_tool(): the per-tool read_only/mutating lookup Patch A4's
    safety gate will call. The classification data itself lives in
    mcp_servers.json (a property of "how much do we trust this
    server's tools", decided once at config time) -- this function is
    just the accessor. Unknown server or unknown tool both resolve to
    "mutating" (fail closed): a tool this registry has no opinion on
    must not silently run unconfirmed once A4 lands.
  - list_mcp_servers() / mcp_server_status(): read-only introspection
    for later patches (A8's `minime mcp list` / `minime mcp status`,
    and any future web-UI panel) to call. No new HTTP routes are added
    by this patch -- wiring these into api/routes/* is left to
    whichever patch actually needs the endpoint (A3 for the agent tool
    list, A8 for the CLI).

Explicitly NOT this patch's job (see the guide's own scope note, and
eo/mcp_client.py's docstring):
  - Wiring MCP tools into an agent's tool-calling loop -- Patch A3.
  - Actually enforcing the read_only/mutating split at call time
    (propose/confirm gating) -- Patch A4. classify_tool() only answers
    "what IS this tool", it doesn't stop anything from being called.
  - Adding a filesystem MCP server, or any Tier 3/4 server (shell,
    process manager, package manager, application launcher) -- see
    docs/decisions/0001-cli-skills-mcp-scope.md and this module's own
    config file comment for why.

Place this file at: eo/mcp_registry.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eo.mcp_client as mcp_client
from eo.mcp_client import MCPClientError

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "MCPRegistryError",
    "MCPServerConfig",
    "classify_tool",
    "connect_configured_servers",
    "list_mcp_servers",
    "load_server_configs",
    "mcp_server_status",
]

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../backend

# Overridable via env for tests (see test_eo_mcp_registry.py) and for
# anyone running multiple isolated deployments off one checkout, same
# override-the-default-path convention eo.registry's own
# ROLE_LIBRARY_SCOPE env var comment describes for a sibling piece of
# config.
DEFAULT_CONFIG_PATH = os.environ.get(
    "MCP_SERVERS_CONFIG_PATH",
    os.path.join(_BASE_DIR, "config", "mcp_servers.json"),
)

ToolTrust = Literal["read_only", "mutating"]
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPRegistryError(Exception):
    """Malformed mcp_servers.json (missing required field for a given
    transport, unknown transport, etc.) -- a config-authoring mistake,
    not a runtime connection failure. Raised at load time so a typo in
    the JSON file is caught immediately rather than surfacing later as
    a confusing MCPClientError from deep inside eo.mcp_client."""


@dataclass(frozen=True)
class MCPServerConfig:
    """One entry from mcp_servers.json, validated and ready to hand to
    eo.mcp_client.connect_server() (its `command`/`env`/`url`/`headers`
    fields already have `${VAR}` placeholders expanded -- the caller
    doesn't need to know that expansion happened)."""

    name: str
    enabled: bool
    transport: Literal["stdio", "http"]
    command: list[str] | None
    env: dict[str, str]
    url: str | None
    headers: dict[str, str]
    default_tool_trust: ToolTrust
    tool_trust_overrides: dict[str, ToolTrust] = field(default_factory=dict)


def _expand(value: str) -> str:
    """Replaces every `${VAR_NAME}` in `value` with os.environ's
    current value for VAR_NAME, or "" if unset. Deliberately does NOT
    treat an unset var as an error here -- some values are optional
    (e.g. CONTEXT7_API_KEY, see backend/.env.example: the free tier
    works with it blank). A var that's genuinely required for a given
    server will make that server's own handshake fail with a real,
    specific MCPClientError once connect_server() actually tries it --
    that's a more honest failure than this loader guessing which vars
    "must" be set."""
    return _VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _expand_dict(d: dict[str, str] | None) -> dict[str, str]:
    return {k: _expand(v) for k, v in (d or {}).items()}


def _parse_entry(raw: dict[str, Any]) -> MCPServerConfig:
    name = raw.get("name")
    if not name:
        raise MCPRegistryError(f"mcp_servers.json entry missing required 'name': {raw!r}")

    transport = raw.get("transport")
    if transport not in ("stdio", "http"):
        raise MCPRegistryError(f"server {name!r}: 'transport' must be 'stdio' or 'http', got {transport!r}")

    command = raw.get("command")
    url = raw.get("url")
    if transport == "stdio" and not command:
        raise MCPRegistryError(f"server {name!r}: transport=stdio requires a non-empty 'command' list")
    if transport == "http" and not url:
        raise MCPRegistryError(f"server {name!r}: transport=http requires a 'url'")

    default_tool_trust = raw.get("default_tool_trust", "mutating")
    if default_tool_trust not in ("read_only", "mutating"):
        raise MCPRegistryError(
            f"server {name!r}: 'default_tool_trust' must be 'read_only' or 'mutating', got {default_tool_trust!r}"
        )
    overrides = raw.get("tool_trust_overrides", {}) or {}
    for tool_name, trust in overrides.items():
        if trust not in ("read_only", "mutating"):
            raise MCPRegistryError(
                f"server {name!r}: tool_trust_overrides[{tool_name!r}] must be "
                f"'read_only' or 'mutating', got {trust!r}"
            )

    return MCPServerConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        transport=transport,
        command=list(command) if command else None,
        env=_expand_dict(raw.get("env")),
        url=_expand(url) if url else None,
        headers=_expand_dict(raw.get("headers")),
        default_tool_trust=default_tool_trust,
        tool_trust_overrides=dict(overrides),
    )


def load_server_configs(path: str | None = None) -> list[MCPServerConfig]:
    """Reads and validates every entry in mcp_servers.json. Raises
    MCPRegistryError on a malformed file -- this is checked-in config,
    not user input, so failing loudly (rather than silently skipping a
    bad entry) is the right default: a typo here should be caught in
    review/CI, not discovered as "why isn't GitHub MCP connected"
    weeks later.

    `_comment` (a documentation-only array, see the config file itself)
    is ignored -- not every JSON key needs to describe a server.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(config_path):
        raise MCPRegistryError(f"MCP server config not found at {config_path}")
    with open(config_path) as f:
        raw = json.load(f)
    return [_parse_entry(entry) for entry in raw.get("servers", [])]


def _config_by_name(path: str | None = None) -> dict[str, MCPServerConfig]:
    return {c.name: c for c in load_server_configs(path)}


def classify_tool(server_name: str, tool_name: str, *, path: str | None = None) -> ToolTrust:
    """Patch A4's accessor: is this tool read-only (safe to run freely)
    or mutating (needs the propose/confirm gate)? Fails closed --
    a server or tool this registry doesn't know about is "mutating",
    never "read_only", so a config-loading hiccup or a server that
    exposes a new tool after this file was written can never
    accidentally grant free-run access."""
    configs = _config_by_name(path)
    server = configs.get(server_name)
    if server is None:
        return "mutating"
    return server.tool_trust_overrides.get(tool_name, server.default_tool_trust)


async def connect_configured_servers(path: str | None = None) -> dict[str, str | None]:
    """The backend-startup loader Patch A2's own goal describes:
    connects every `enabled` server through eo.mcp_client.connect_server().
    Intended caller: api/server.py's `_lifespan`, once, at startup --
    same "start once, reuse" discipline eo.mcp_client's own docstring
    asks of connect_server() itself, just one level up.

    Returns {server_name: error_message_or_None} for every enabled
    server, so the caller can log a summary without this function
    raising and aborting backend startup over one bad server -- a
    missing GITHUB_MCP_TOKEN should mean "no GitHub tools available
    this run", not "the whole API process refuses to start".
    """
    results: dict[str, str | None] = {}
    for server in load_server_configs(path):
        if not server.enabled:
            continue
        try:
            await mcp_client.connect_server(
                server.name,
                transport=server.transport,
                command=server.command,
                env=server.env,
                url=server.url,
                headers=server.headers,
            )
            results[server.name] = None
        except MCPClientError as exc:
            # Logged, not raised -- see docstring above. print() matches
            # this codebase's existing convention for startup-path
            # diagnostics (see api/server.py's own SIGINT handler
            # logging, and daemon/connection.py's connection-state
            # logging) rather than introducing a new logging setup for
            # just this one module.
            print(f"[mcp_registry] failed to connect MCP server {server.name!r}: {exc}")
            results[server.name] = str(exc)
    return results


def list_mcp_servers(path: str | None = None) -> list[dict[str, Any]]:
    """Cheap, synchronous summary -- config-derived fields plus whether
    a live connection currently exists (eo.mcp_client.is_connected()
    is a plain dict lookup, no RPC). Intended for A8's `minime mcp
    list` and any future "connected services" panel; does not itself
    attempt to connect anything."""
    return [
        {
            "name": s.name,
            "enabled": s.enabled,
            "transport": s.transport,
            "connected": mcp_client.is_connected(s.name),
            "default_tool_trust": s.default_tool_trust,
        }
        for s in load_server_configs(path)
    ]


async def mcp_server_status(server_name: str, path: str | None = None) -> dict[str, Any]:
    """Detailed status for one server, including its live tool list --
    unlike list_mcp_servers(), this is async because a connected
    server's tool list may need a `tools/list` round trip on first
    call (eo.mcp_client.list_tools() caches after that). Intended for
    A8's `minime mcp status <name>`.

    Returns {"error": "..."} instead of raising for an unknown server
    name or a server that's enabled but not currently connected --
    both are ordinary states for a CLI status command to display, not
    exceptional ones.
    """
    configs = _config_by_name(path)
    server = configs.get(server_name)
    if server is None:
        return {"name": server_name, "error": "not found in mcp_servers.json"}

    status: dict[str, Any] = {
        "name": server.name,
        "enabled": server.enabled,
        "transport": server.transport,
        "connected": mcp_client.is_connected(server.name),
        "default_tool_trust": server.default_tool_trust,
    }
    if not status["connected"]:
        return status

    try:
        tools = await mcp_client.list_tools(server.name)
    except MCPClientError as exc:
        status["tools_error"] = str(exc)
        return status

    status["tools"] = [
        {
            "name": t.name,
            "description": t.description,
            "trust": classify_tool(server.name, t.name, path=path),
        }
        for t in tools
    ]
    return status
