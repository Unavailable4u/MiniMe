"""
eo/capabilities.py — Patch B0 (CLI-as-Internal-Interface plan, Part 1).

Single, shared, in-process entry point for "what can this system do."

Before this patch, the only two callers of eo/skill_library.py's
list_skills()/get_skill() and eo/mcp_registry.py's
list_mcp_servers()/mcp_server_status() were api/routes/tasks.py and
api/routes/mcp.py — each importing the underlying module directly.
That was fine while the only consumer was an HTTP route, but it meant
there was no single place an `eo` agent (dispatcher/executor/router)
could call into for the same information without either duplicating
the import or reaching past the route layer.

This module is that single place. It is deliberately a thin
re-export/wrapper layer with zero new behavior in this patch — every
function here is a plain, undecorated pass-through to the existing
skill_library / mcp_registry function of the same job. Two kinds of
callers are meant to use it:

  1. api/routes/tasks.py and api/routes/mcp.py — updated by this same
     patch to import from here instead of from eo/skill_library.py /
     eo/mcp_registry.py directly, so the route stays a thin wrapper
     one layer further out.
  2. eo/dispatcher.py, eo/executor.py, eo/router.py — wired to this
     module by Patch B5a. Not touched by this patch.

Later patches extend this module rather than replacing it:
  - B1 (this patch) adds list_capabilities() — tagged capability-layer
    lookups, backed by the new eo/capability_entries.py store. This is
    deliberately a *second* store from skill_library.py's, not folded
    into it — see capability_entries.py's own module docstring for why.
  - B3 adds get_role_scope() / capabilities_for_role() (role-scoping by tag).
  - B4 adds list_directory() / read_file() / search_text() (read-only
    introspection, gated by B2's redaction_guard), registered here so
    they're discoverable through the same shared surface as everything
    else — the plan's own framing for why introspection lives next to
    capability lookups rather than off on its own.

Scope note: this module intentionally does not touch cli/ — the CLI
package (cli/minime_cli/) is a standalone HTTP client
(see cli/minime_cli/api_client.py's own docstring) with no in-process
relationship to the backend, so "the CLI's command surface becomes the
shared interface" (architecture plan §3.1) is implemented here, one
layer below where the plan doc's own wording might suggest — the
CLI's job stays "mirror this command surface as an HTTP client for
humans," not participate in the in-process call graph itself.
"""
from __future__ import annotations

from typing import Any

from eo.capability_entries import list_capability_entries as _list_capability_entries
from eo.mcp_registry import list_mcp_servers as _list_mcp_servers
from eo.mcp_registry import mcp_server_status as _mcp_server_status
from eo.skill_library import get_skill as _get_skill
from eo.skill_library import list_skills as _list_skills


def list_skills() -> dict:
    """Every stored skill, keyed by skill_id. Pass-through to
    eo/skill_library.py's list_skills() — see that function's own
    docstring for the return shape."""
    return _list_skills()


def get_skill(skill_id: str) -> dict | None:
    """Single-skill detail lookup. Pass-through to
    eo/skill_library.py's get_skill() — returns None if skill_id
    doesn't exist."""
    return _get_skill(skill_id)


def list_mcp_servers() -> list[dict[str, Any]]:
    """Every configured MCP server and its enabled/connected state.
    Pass-through to eo/mcp_registry.py's list_mcp_servers()."""
    return _list_mcp_servers()


async def mcp_server_status(server_name: str) -> dict[str, Any]:
    """Detailed status (and live tool list, if connected) for one MCP
    server. Pass-through to eo/mcp_registry.py's mcp_server_status() —
    async because that function is (it may need to query a live
    connection). Returns {"error": ...} for an unknown server_name
    rather than raising — same contract as the wrapped function."""
    return await _mcp_server_status(server_name)


def list_capabilities(tags: list[str] | None = None) -> list[dict[str, Any]]:
    """What this system can do, optionally filtered to entries carrying
    at least one of `tags`. This is the "check here first, a targeted
    lookup, before falling back to a broader read" layer the
    architecture plan's §3.2 describes — an agent facing an unfamiliar
    request should call this before eo/introspection.py's read-only
    functions (Patch B4), not the other way around.

    Only ever returns entry_type="capability" entries — redaction
    entries (Patch B2) are a deliberately separate, non-default lookup
    (eo/capability_entries.py's own get_capability_entry() /
    list_capability_entries(entry_type="redaction", ...)), not reachable
    through this general-purpose function.
    """
    return _list_capability_entries(entry_type="capability", tags=tags)
