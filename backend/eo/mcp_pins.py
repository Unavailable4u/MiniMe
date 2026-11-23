"""
eo/mcp_pins.py — Patch SX-26: re-approve an MCP server when a tool changes.

An MCP server is third-party code, and what it tells us about its own tools
(name, description, input schema) can change at any time -- for example after
an `npx -y ...` package update, or if the remote service is compromised. A
tool that was harmless when someone reviewed it could quietly be given new
instructions in its description.

This module keeps a "pin" for every tool a person has reviewed: the sha256 of
the tool's canonical JSON (name, description, input_schema). The pins live in
one checked-in file, backend/config/mcp_tool_pins.json, so approving a change
is an ordinary commit that someone reviews, and the history shows who approved
what and when.

The rule, applied every time a server connects
(eo/mcp_registry.py::connect_configured_servers):

  - the server must have an entry in the pin file;
  - every tool it offers must have a pin, and its fingerprint must match;
  - every pinned tool must still be offered.

Anything else (no entry, a changed description or schema, a new tool, a tool
that disappeared) means the server is NOT approved: it is disconnected, none
of its tools can be listed or called, and the reason is logged. It stays that
way until someone reviews the change and commits new pins:

    python -m eo.mcp_pins --server github            # show what changed
    python -m eo.mcp_pins --server github --write    # approve it (then commit)

The checks look at tool definitions only when a server connects. A server that
changes its tool list in the middle of a connection is caught the next time it
connects (backend restart).

Place this file at: eo/mcp_pins.py
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from typing import Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eo.mcp_client as mcp_client

__all__ = [
    "DEFAULT_PINS_PATH",
    "MCPPinError",
    "check_tools",
    "diff_pins",
    "load_pins",
    "tool_fingerprint",
    "verify_server",
    "write_pins",
]

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../backend

# Overridable via env for tests and for isolated deployments, same convention
# as eo/mcp_registry.py's MCP_SERVERS_CONFIG_PATH.
DEFAULT_PINS_PATH = os.environ.get(
    "MCP_TOOL_PINS_PATH",
    os.path.join(_BASE_DIR, "config", "mcp_tool_pins.json"),
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_COMMENT = [
    "Patch SX-26 -- approved MCP tool definitions. Loaded by eo/mcp_pins.py",
    "every time an MCP server connects. Each value is the sha256 of one tool's",
    "canonical JSON (name, description, input_schema). A server that is not",
    "listed here, or whose tools differ from what is listed, is disconnected.",
    "Do not edit by hand: review a server's tools with",
    "'python -m eo.mcp_pins --server NAME', approve them with '--write', and",
    "commit the result.",
]


class MCPPinError(Exception):
    """A server is not approved (or the pin file is unusable). The message is
    safe to log and show: it names servers and tools, never a tool's
    description or schema, because those come from a third party."""


# ---------------------------------------------------------------------
# Fingerprints and the pin file
# ---------------------------------------------------------------------

def tool_fingerprint(name: str, description: str, input_schema: dict[str, Any]) -> str:
    """sha256 (hex) of the canonical JSON of one tool definition: keys sorted,
    no extra whitespace, so the same definition always gives the same value."""
    canonical = json.dumps(
        {"name": name, "description": description, "input_schema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprints(tools: list[Any]) -> dict[str, str]:
    return {t.name: tool_fingerprint(t.name, t.description, t.input_schema) for t in tools}


def load_pins(path: str | None = None) -> dict[str, dict[str, str]]:
    """Returns {server_name: {tool_name: fingerprint}}. A missing file means
    "nothing is approved yet" (an empty dict), not an error. A file that exists
    but is malformed raises MCPPinError: guessing at what it meant could approve
    something nobody reviewed."""
    pins_path = path or DEFAULT_PINS_PATH
    if not os.path.exists(pins_path):
        return {}
    try:
        with open(pins_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        raise MCPPinError(f"MCP tool pin file {pins_path} cannot be read: {exc}") from exc

    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        raise MCPPinError(f"MCP tool pin file {pins_path}: 'servers' must be an object")
    pins: dict[str, dict[str, str]] = {}
    for server_name, entry in servers.items():
        tools = entry.get("tools") if isinstance(entry, dict) else None
        if not isinstance(tools, dict):
            raise MCPPinError(f"MCP tool pin file: server {server_name!r} needs a 'tools' object")
        for tool_name, fingerprint in tools.items():
            if not isinstance(fingerprint, str) or not _SHA256_HEX.match(fingerprint):
                raise MCPPinError(
                    f"MCP tool pin file: pin for {server_name!r}/{tool_name!r} is not a sha256 hex string"
                )
        pins[server_name] = dict(tools)
    return pins


def write_pins(pins: dict[str, dict[str, str]], path: str | None = None) -> None:
    """Writes the whole pin file (sorted, LF line endings, so the diff in
    review shows only what actually changed)."""
    pins_path = path or DEFAULT_PINS_PATH
    document = {
        "_comment": _COMMENT,
        "servers": {name: {"tools": dict(sorted(tools.items()))} for name, tools in sorted(pins.items())},
    }
    with open(pins_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(document, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------

def diff_pins(approved: dict[str, str] | None, offered: dict[str, str]) -> dict[str, list[str]]:
    """Compares approved {tool: fingerprint} with what a server offers now.
    Returns the tool names under "added", "changed" and "removed" (sorted)."""
    approved = approved or {}
    return {
        "added": sorted(n for n in offered if n not in approved),
        "changed": sorted(n for n in offered if n in approved and offered[n] != approved[n]),
        "removed": sorted(n for n in approved if n not in offered),
    }


def check_tools(server_name: str, tools: list[Any], pins: dict[str, dict[str, str]]) -> None:
    """Raises MCPPinError unless `tools` (what the server offers) matches the
    approved pins for `server_name` exactly."""
    if server_name not in pins:
        raise MCPPinError(
            f"MCP server {server_name!r} is not approved: it has no entry in the tool pin file. "
            f"Review it with: python -m eo.mcp_pins --server {server_name}"
        )
    diff = diff_pins(pins[server_name], _fingerprints(tools))
    problems = []
    if diff["changed"]:
        problems.append("changed since approval: " + ", ".join(map(repr, diff["changed"])))
    if diff["added"]:
        problems.append("new, not approved: " + ", ".join(map(repr, diff["added"])))
    if diff["removed"]:
        problems.append("approved but no longer offered: " + ", ".join(map(repr, diff["removed"])))
    if problems:
        raise MCPPinError(
            f"MCP server {server_name!r} needs re-approval ({'; '.join(problems)}). "
            f"Review with: python -m eo.mcp_pins --server {server_name}"
        )


async def verify_server(server_name: str, *, pins_path: str | None = None) -> None:
    """Fetches the connected server's tool list fresh (never from cache) and
    checks it against the pin file. Raises MCPPinError if the server is not
    approved, MCPClientError if its tool list cannot be fetched. The caller
    (connect_configured_servers) disconnects the server on either."""
    pins = load_pins(pins_path)
    tools = await mcp_client.list_tools(server_name, refresh=True)
    check_tools(server_name, tools, pins)


# ---------------------------------------------------------------------
# Command line: review and approve (python -m eo.mcp_pins)
# ---------------------------------------------------------------------

def _printable(text: str, limit: int = 300) -> str:
    """JSON-quoted and cut short, so a hostile description cannot put control
    characters or terminal escape sequences on the reviewer's screen."""
    cut = text if len(text) <= limit else text[:limit] + "..."
    return json.dumps(cut)


async def _review(args: argparse.Namespace) -> int:
    import eo.mcp_registry as mcp_registry  # imported here: mcp_registry imports this module

    configs = [c for c in mcp_registry.load_server_configs(args.config) if c.enabled]
    if args.server:
        configs = [c for c in configs if c.name == args.server]
        if not configs:
            print(f"No enabled server named {args.server!r} in the MCP server config.")
            return 2

    pins = load_pins(args.pins)
    new_pins = {name: dict(tools) for name, tools in pins.items()}
    differs = False
    try:
        for server in configs:
            try:
                await mcp_client.connect_server(
                    server.name,
                    transport=server.transport,
                    command=server.command,
                    env=server.env,
                    url=server.url,
                    headers=server.headers,
                )
                tools = await mcp_client.list_tools(server.name, refresh=True)
            except mcp_client.MCPClientError as exc:
                print(f"[{server.name}] could not connect: {exc}")
                differs = True
                continue
            offered = _fingerprints(tools)
            diff = diff_pins(pins.get(server.name), offered)
            print(f"[{server.name}] {len(offered)} tools offered"
                  + ("" if server.name in pins else " (server not approved yet)"))
            by_name = {t.name: t for t in tools}
            for label in ("added", "changed"):
                for tool_name in diff[label]:
                    print(f"  {label.upper()}: {tool_name}  description={_printable(by_name[tool_name].description)}")
            for tool_name in diff["removed"]:
                print(f"  REMOVED: {tool_name}")
            if not any(diff.values()) and server.name in pins:
                print("  no changes")
                continue
            differs = True
            new_pins[server.name] = offered
    finally:
        await mcp_client.shutdown_all()

    if args.write and differs:
        write_pins(new_pins, args.pins)
        print(f"Wrote {args.pins or DEFAULT_PINS_PATH}. Review the file diff, then commit it.")
        return 0
    if differs:
        print("Not written. Read the tools above; if you approve them, run again with --write.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eo.mcp_pins",
        description="Show how MCP tool definitions differ from the approved pins, and approve them with --write.",
    )
    parser.add_argument("--server", help="only this server (default: every enabled server)")
    parser.add_argument("--write", action="store_true", help="save the tools shown as the new approved pins")
    parser.add_argument("--config", help="path to mcp_servers.json (default: the configured one)")
    parser.add_argument("--pins", help="path to the pin file (default: backend/config/mcp_tool_pins.json)")
    return asyncio.run(_review(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
