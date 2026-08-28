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
     module by Patch B5a (each now imports list_capabilities() for its
     own observability/validation touchpoint — see each module's own
     B5a docstring note for why none of the three had a direct
     skill_library/mcp_registry import to redirect in the first place).
     Not touched by this patch [B0].

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
Scope note: the human-facing CLI (cli/minime_cli/) was removed in
Patch C0 (see MiniMe-Patch-Series-C-Plan.md, Track 1) — it was a
standalone HTTP client with no in-process relationship to this module,
so its removal changes nothing here. "The CLI's command surface
becomes the shared interface" (architecture plan §3.1) is realized
in-process, through this module and its callers, not through any CLI
package.

B-series added read-only introspection over the repo (list_directory/
read_file/search_text, gated by redaction_guard). Series C's
run_data_command() extends the same "one shared in-process surface,
capped reads, fail-loud on ambiguity" discipline to per-task artifact
data (eo/data_store.py) instead of the filesystem. It does not go
through redaction_guard — task artifacts aren't secrets — but copies
the same byte/match caps so a role can't accidentally re-ingest an
entire artifact just by asking a slightly broad question.
"""
from __future__ import annotations

from typing import Any

from eo.capability_entries import list_capability_entries as _list_capability_entries
from eo.data_handler import run_data_command as _run_data_command
from eo.introspection import list_directory as _list_directory
from eo.introspection import read_file as _read_file
from eo.introspection import search_text as _search_text
from eo.mcp_registry import list_mcp_servers as _list_mcp_servers
from eo.mcp_registry import mcp_server_status as _mcp_server_status
from eo.registry import get_role_metadata as _get_role_metadata
from eo.skill_library import ensure_skill_for_task as _ensure_skill_for_task
from eo.skill_library import get_relevant_skill as _get_relevant_skill
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


def get_relevant_skill(task_text: str) -> str:
    """Patch C7 — pass-through to eo/skill_library.py's
    get_relevant_skill(). Registered here so agents/generic_worker.py
    can go through this module's shared surface for its own skill
    lookup instead of importing eo/skill_library.py directly (B5a
    already did the equivalent for dispatcher/executor/router's own
    observability touchpoint; this closes the one remaining direct
    import C7 flagged). Degrades to \"\" on any retrieval failure or
    genuine no-match — see the wrapped function's own docstring."""
    return _get_relevant_skill(task_text)


def ensure_skill_for_task(task_text: str) -> str:
    """Patch C7 — pass-through to eo/skill_library.py's
    ensure_skill_for_task(). See get_relevant_skill() above for why
    this is registered here rather than left as a direct
    agents/generic_worker.py -> eo/skill_library.py import."""
    return _ensure_skill_for_task(task_text)


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


def get_role_scope(role_name: str, user_id: str | None = None) -> list[str]:
    """Patch B3 — a role's capability_tags, straight from
    eo/registry.py's per-role metadata store (the same store
    list_role_metadata()/get_role_metadata() already read/write; B3
    just adds this one field to it). Returns [] both for a role that
    exists but has no tags set, and for a role_name that's never been
    briefed at all — the caller (capabilities_for_role() below, or an
    eo agent calling this directly) never needs to distinguish "no
    scope configured" from "unknown role"; either way the answer to
    "what's this role allowed to see" is the same empty scope.

    This is deliberately the ONE place "how much should this agent
    introspect" gets answered, per the architecture plan's own framing
    for §3.2's role-scoping bullet — not scattered raw directory paths
    or ad-hoc checks in eo/registry.py or anywhere else that happens
    to have a role_name in scope.
    """
    metadata = _get_role_metadata(role_name, user_id=user_id)
    if not metadata:
        return []
    return list(metadata.get("capability_tags") or [])


def capabilities_for_role(role_name: str, user_id: str | None = None) -> list[dict[str, Any]]:
    """Patch B3 — composes get_role_scope() with list_capabilities():
    exactly the capability entries visible to this role, given its own
    capability_tags. A role with an empty scope (no tags set, or an
    unknown role_name) sees nothing through this function — this is
    intentionally NOT "no tags means see everything"; a role has to be
    explicitly scoped to something before it gets anything back here.

    This is the function eo agents call for "what am I, specifically,
    allowed to know about" — list_capabilities() itself stays the
    unscoped, "what can this system do at all" answer for callers that
    aren't asking on behalf of a particular role (e.g. an admin-facing
    route).
    """
    tags = get_role_scope(role_name, user_id=user_id)
    if not tags:
        return []
    return list_capabilities(tags=tags)


def list_directory(path: str) -> dict:
    """Patch B4 — pass-through to eo/introspection.py's list_directory().
    Registered here, alongside every other capability/lookup function
    on this module's shared surface, per this module's own docstring:
    introspection is "the fallback path when the capability layer
    doesn't precisely cover what an agent needs to know" (§3.3), so it
    belongs next to the capability lookups above, not off on its own.
    An agent should call list_capabilities()/capabilities_for_role()
    first and only reach for this when that comes back empty or
    insufficient — Patch B5b wires that ordering into the actual agent
    call path; this function itself has no opinion about call order."""
    return _list_directory(path)


def read_file(path: str) -> dict:
    """Patch B4 — pass-through to eo/introspection.py's read_file().
    See list_directory() above for the fallback-path framing this
    shares."""
    return _read_file(path)


def search_text(pattern: str, root: str) -> dict:
    """Patch B4 — pass-through to eo/introspection.py's search_text().
    See list_directory() above for the fallback-path framing this
    shares."""
    return _search_text(pattern, root)


def run_data_command(session_id: str, command: str, author_role: str | None = None) -> str:
    """Patch C2 — pass-through to eo/data_handler.py's
    run_data_command(). Registered here next to B4's
    list_directory/read_file/search_text per this module's own
    docstring: same shared-in-process-surface principle, applied to
    per-task artifact data (eo/data_store.py) instead of the
    filesystem. Unlike the B4 trio, this does not go through
    redaction_guard — see this module's docstring for why — but shares
    their same capped-read discipline underneath."""
    return _run_data_command(session_id, command, author_role=author_role)
