"""
eo/mcp_client.py — Patch A1: External MCP Client Module.

This is the real Model Context Protocol (JSON-RPC 2.0 over stdio or
HTTP), NOT the daemon's custom websocket protocol in
eo/local_workspace.py -- see docs/decisions/0001-cli-skills-mcp-scope.md
(Patch A0) for why those two are deliberately kept separate: the daemon
already covers local filesystem/shell, with its own propose/confirm
safety gate (eo/local_workspace_tools.py); this module is for reaching
OUTWARD to third-party MCP servers (GitHub, Context7, a web-search
server, etc).

Scope of THIS patch specifically (see the implementation guide's own
"out of scope" note): connect, discover (`tools/list`), and call
(`tools/call`) against a real MCP server, for both transports a
community MCP server actually ships as:
  - stdio  -- a local subprocess (the common case for an npm/pip
    package), talking newline-delimited JSON-RPC over its stdin/stdout.
  - http   -- a remote HTTP MCP endpoint, JSON-RPC request/response
    over POST (SSE streaming responses collapsed to their final `data:`
    JSON-RPC payload, since nothing here needs partial/progress frames
    yet).

Explicitly NOT this patch's job (see the guide's own scope note):
  - WHICH servers are configured / enabled -- that's Patch A2
    (eo/mcp_registry.py + backend/config/mcp_servers.json), which is
    the only intended caller of connect_server() at backend startup.
  - Wiring these tools into an agent's tool-calling loop -- Patch A3.
  - Any propose/confirm-style safety gating on mutating MCP tool calls
    -- Patch A4. Every MCP tool, read-only or mutating, is callable
    here with no gate; A4 decides which of them need one and wraps
    call_mcp_tool() the same way local_workspace_tools.py wraps
    call_daemon() today. This module has no opinion on that.

Connection lifecycle, same "start once, reuse, clean shutdown"
discipline eo/local_workspace.py's `_connections` registry follows for
daemon sockets: connect_server() is idempotent per server_name (a
second call while already connected is a no-op, it does NOT spawn a
second subprocess or open a second HTTP session), list_tools()/
call_mcp_tool() read from that one live connection, and
disconnect_server()/shutdown_all() close it down cleanly. Do not spawn
a new stdio process per tool call -- that's the mistake this module
exists to avoid.

Smoke-tested against GitHub's remote MCP server (the guide's suggested
Tier-1 reference server) -- see
backend/tests/unit/test_eo_mcp_client.py, which fakes both transports
rather than making a real network/subprocess call in CI, same
isolation posture tests/unit/test_eo_worker_pool.py already documents
for a sibling module.

Place this file at: eo/mcp_client.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if python-dotenv isn't installed; env vars can come from the real environment

import httpx

__all__ = [
    "DEFAULT_TOOL_CALL_TIMEOUT_SECONDS",
    "MCPClientError",
    "MCPTool",
    "call_mcp_tool",
    "connect_server",
    "disconnect_server",
    "is_connected",
    "list_tools",
    "shutdown_all",
]

# MCP's own protocol version this client speaks during `initialize`.
# Bump this in one place if/when a newer MCP spec revision is adopted.
MCP_PROTOCOL_VERSION = "2025-06-18"

DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 30.0
_HANDSHAKE_TIMEOUT_SECONDS = 15.0
_STDIO_READ_CHUNK = 4096


class MCPClientError(Exception):
    """Same role eo/local_workspace.py's ToolCallError plays for the
    daemon protocol: one exception type for "the MCP round trip did not
    succeed" (server not connected, handshake failed, tool errored,
    timed out, malformed response), so every call site here can rely on
    catch-MCPClientError instead of catching transport-specific
    exceptions (subprocess errors, httpx errors, JSON decode errors)
    that differ per transport."""


@dataclass
class MCPTool:
    """Normalized shape for one tool a connected server exposes, as
    returned by `tools/list`. Deliberately mirrors the
    name/description/input_schema shape utils/capability_tools.py's own
    internal-tool builders already use (see
    local_workspace_tools.local_workspace_tools()'s docstring) -- Patch
    A3 hands agents a single combined tool list, and it needs internal
    and MCP-sourced tools to look the same to whatever tool-calling loop
    consumes them."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str  # which configured server this tool came from -- an agent-facing
    # tool name alone ("search_issues") doesn't say which server to route the call
    # to, and two servers could plausibly expose a same-named tool.


@dataclass
class _StdioTransport:
    kind: Literal["stdio"] = "stdio"
    process: asyncio.subprocess.Process = None
    _next_id: int = 0
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _reader_task: asyncio.Task | None = None

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.process is None or self.process.returncode is not None:
            raise MCPClientError("stdio MCP server process is not running")

        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        message = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n"

        try:
            self.process.stdin.write(message.encode("utf-8"))
            await self.process.stdin.drain()
            try:
                response = await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError:
                raise MCPClientError(f"MCP server did not respond to {method!r} within {timeout:.0f}s") from None
        finally:
            self._pending.pop(request_id, None)

        if "error" in response:
            err = response["error"] or {}
            raise MCPClientError(err.get("message") or f"{method} failed with no error message")
        return response.get("result") or {}

    async def _read_loop(self) -> None:
        """Runs for the lifetime of the connection, one line of stdout
        per JSON-RPC message (MCP's stdio framing), resolving whichever
        pending future matches that message's `id`. Same
        one-reader-task-per-connection shape daemon/connection.py uses
        for its websocket recv loop, just over a subprocess pipe
        instead of a socket."""
        assert self.process is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break  # process closed stdout -- treat as a clean disconnect
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a stray non-JSON line on stdout (e.g. a startup banner) -- ignore, not fatal
                msg_id = message.get("id")
                future = self._pending.get(msg_id)
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            # Any request still waiting when the process goes away fails loudly
            # instead of hanging forever.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MCPClientError("MCP server process exited while a request was in flight"))

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except TimeoutError:
                self.process.kill()


@dataclass
class _HttpTransport:
    kind: Literal["http"] = "http"
    client: httpx.AsyncClient = None
    url: str = ""
    _next_id: int = 0

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        try:
            resp = await self.client.post(self.url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise MCPClientError(f"HTTP MCP request {method!r} failed: {exc}") from exc

        body = _parse_http_body(resp)
        if body is None:
            raise MCPClientError(f"HTTP MCP server returned an unparseable response for {method!r}")
        if "error" in body:
            err = body["error"] or {}
            raise MCPClientError(err.get("message") or f"{method} failed with no error message")
        return body.get("result") or {}

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def _parse_http_body(resp: httpx.Response) -> dict[str, Any] | None:
    """Most remote MCP servers answer a POST with a plain JSON-RPC
    body; some answer with `text/event-stream` (one JSON-RPC message
    per `data:` line) even for a single-shot call. Handle both rather
    than assuming the server picked the simpler one -- this patch's
    smoke-test server (GitHub MCP) is HTTP+JSON, but the MCP spec
    permits either for the same endpoint."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        last_data = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                last_data = line[len("data:"):].strip()
        if last_data is None:
            return None
        try:
            return json.loads(last_data)
        except json.JSONDecodeError:
            return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None


@dataclass
class _MCPConnection:
    server_name: str
    transport: _StdioTransport | _HttpTransport
    connected_at: float
    server_info: dict[str, Any] = field(default_factory=dict)
    _tools_cache: list[MCPTool] | None = None


# Module-level registry, same "own module owns its own live-connection
# dict" pattern eo/local_workspace.py's `_connections` and
# eo/local_workspace_tools.py's pending-action store both use --
# ephemeral, tied to this process, not persisted.
_connections: dict[str, _MCPConnection] = {}


def is_connected(server_name: str) -> bool:
    return server_name in _connections


async def connect_server(
    server_name: str,
    *,
    transport: Literal["stdio", "http"],
    command: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = _HANDSHAKE_TIMEOUT_SECONDS,
) -> None:
    """Starts (stdio) or opens (http) a connection to one MCP server
    and performs the standard MCP `initialize` handshake. Idempotent:
    calling this again for a `server_name` that's already connected is
    a no-op, so Patch A2's startup loader can call this unconditionally
    for every enabled server without worrying about re-entrancy or
    double-spawning a process.

    `command`/`env` are required for transport="stdio" (e.g.
    `["npx", "-y", "@modelcontextprotocol/server-github"]`); `url`/
    `headers` are required for transport="http". Which of these a given
    server needs is Patch A2's config-loading job, not this function's
    -- this function just validates that the fields its own transport
    needs are actually present.
    """
    if server_name in _connections:
        return

    if transport == "stdio":
        if not command:
            raise MCPClientError(f"stdio transport for {server_name!r} requires a non-empty `command`")
        process_env = {**os.environ, **(env or {})}
        # Resolve the executable through PATH/PATHEXT ourselves rather than
        # handing the bare name straight to create_subprocess_exec(). On
        # Windows, npm-installed CLIs like `npx` are `.cmd` shims; Windows'
        # CreateProcess (which create_subprocess_exec calls directly, no
        # shell involved) can't launch a shim by its extension-less name the
        # way cmd.exe does, so `command=["npx", ...]` fails with an OSError
        # for every stdio server configured that way (context7, web_search,
        # etc.) regardless of whether its own API key/env var is set --
        # this has nothing to do with per-server config. shutil.which()
        # applies PATHEXT (.COM/.EXE/.BAT/.CMD) on Windows and is a no-op
        # correctness-wise on POSIX, so this is safe cross-platform. Falls
        # back to the original bare command if resolution fails, so the
        # underlying OSError still surfaces normally (e.g. genuinely missing
        # `npx`) instead of being masked here.
        resolved_command = list(command)
        resolved_executable = shutil.which(resolved_command[0], path=process_env.get("PATH"))
        if resolved_executable:
            resolved_command[0] = resolved_executable
        try:
            process = await asyncio.create_subprocess_exec(
                *resolved_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=process_env,
            )
        except (OSError, NotImplementedError) as exc:
            # NotImplementedError: Windows' default SelectorEventLoop doesn't
            # implement subprocess transports at all (only ProactorEventLoop
            # does) -- api/server.py now sets that policy on Windows, but a
            # misconfigured/incompatible server should still degrade to "this
            # one server didn't connect" rather than crash the whole app, the
            # same guarantee this function already gave for OSError.
            raise MCPClientError(f"failed to launch stdio MCP server {server_name!r}: {exc}") from exc

        conn_transport = _StdioTransport(process=process)
        conn_transport._reader_task = asyncio.create_task(conn_transport._read_loop())

    elif transport == "http":
        if not url:
            raise MCPClientError(f"http transport for {server_name!r} requires a `url`")
        conn_transport = _HttpTransport(
            client=httpx.AsyncClient(headers=headers or {}),
            url=url,
        )

    else:
        raise MCPClientError(f"unknown MCP transport {transport!r} for server {server_name!r}")

    try:
        init_result = await conn_transport.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "minime-backend", "version": "1"},
            },
            timeout=timeout,
        )
        # MCP requires the client to send this notification (no response
        # expected) to complete the handshake before any other request.
        await _notify(conn_transport, "notifications/initialized", {})
    except MCPClientError:
        await conn_transport.close()
        raise

    _connections[server_name] = _MCPConnection(
        server_name=server_name,
        transport=conn_transport,
        connected_at=time.time(),
        server_info=init_result.get("serverInfo") or {},
    )


async def _notify(transport: _StdioTransport | _HttpTransport, method: str, params: dict[str, Any]) -> None:
    """A JSON-RPC notification -- no `id`, no response expected. Both
    transports' `.request()` always sends an id and awaits a matching
    reply, which doesn't fit `notifications/initialized` (servers don't
    answer it), so this writes the frame directly instead of reusing
    `.request()`."""
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if isinstance(transport, _StdioTransport):
        if transport.process is not None:
            transport.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await transport.process.stdin.drain()
    else:
        try:
            await transport.client.post(transport.url, json=message, timeout=_HANDSHAKE_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            pass  # notifications are fire-and-forget by spec; a failure here isn't fatal to the handshake


def _require_connection(server_name: str) -> _MCPConnection:
    conn = _connections.get(server_name)
    if conn is None:
        raise MCPClientError(
            f"no MCP connection for server {server_name!r} -- call connect_server() first "
            "(Patch A2's startup loader is the intended caller of that)"
        )
    return conn


async def list_tools(server_name: str, *, refresh: bool = False) -> list[MCPTool]:
    """Calls `tools/list` and returns the normalized MCPTool shape.
    Cached on the connection after the first successful call (a
    server's tool list doesn't change mid-connection under normal
    operation) -- pass refresh=True to force a re-fetch."""
    conn = _require_connection(server_name)
    if conn._tools_cache is not None and not refresh:
        return conn._tools_cache

    result = await conn.transport.request("tools/list", {}, timeout=DEFAULT_TOOL_CALL_TIMEOUT_SECONDS)
    raw_tools = result.get("tools") or []
    tools = [
        MCPTool(
            name=t["name"],
            description=t.get("description", ""),
            input_schema=t.get("inputSchema") or {},
            server_name=server_name,
        )
        for t in raw_tools
    ]
    conn._tools_cache = tools
    return tools


async def call_mcp_tool(
    server_name: str,
    tool_name: str,
    params: dict[str, Any],
    timeout: float = DEFAULT_TOOL_CALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """The single call surface the rest of the backend uses (Patch A3's
    agent tool-dispatch loop, and A4's safety-gate wrapper around
    mutating calls). Raises MCPClientError; never returns a partial or
    ambiguous result -- same contract eo/local_workspace.py's
    call_daemon() makes for the daemon protocol, deliberately, so a
    caller that already knows how to handle that one error type from a
    daemon tool call handles an MCP tool call the same way.

    No safety/confirm gating happens here on purpose (see this module's
    own docstring, "out of scope for this patch" / A4) -- every tool
    this connection knows about is callable immediately.
    """
    conn = _require_connection(server_name)
    result = await conn.transport.request(
        "tools/call",
        {"name": tool_name, "arguments": params},
        timeout=timeout,
    )
    if result.get("isError"):
        content = result.get("content") or []
        message = " ".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        ).strip()
        raise MCPClientError(message or f"MCP tool {tool_name!r} on {server_name!r} returned an error")
    return result


async def disconnect_server(server_name: str) -> None:
    conn = _connections.pop(server_name, None)
    if conn is not None:
        await conn.transport.close()


async def shutdown_all() -> None:
    """Clean shutdown of every live MCP connection -- intended for
    backend process shutdown (same "clean shutdown" half of the
    lifecycle contract eo/local_workspace.py's connection registry
    documents for daemon sockets)."""
    for server_name in list(_connections.keys()):
        await disconnect_server(server_name)
