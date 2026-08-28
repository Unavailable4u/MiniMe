"""
eo/capability_entries.py — Patch B1 (CLI-as-Internal-Interface plan, §3.2
"Capability layer" half).

A second, separate store from eo/skill_library.py's skill docs —
deliberately not mixed into that module's registry:skill_library dict
or its Vector-embedded semantic-retrieval path. Two different jobs:

  - skill_library.py answers "how do you do a task of THIS kind" —
    matched by semantic similarity against a task description, read
    hot-path on every unfamiliar task.
  - This module answers "what CAN this system do, and (from Patch B2
    onward) what must it never read or share" — looked up by exact
    tag, not similarity, and read far less often (an agent facing an
    unfamiliar request checks this before falling back to a broader
    introspection read — see eo/introspection.py, Patch B4).

Keeping them as two stores means neither's read/write path has to
carry the other's concerns: skill_library.py's write_skill() never
needs a `tags` field or an `entry_type` discriminator, and this
module's writes never need to touch Vector at all — a tag lookup is
exact-match, not semantic, so there's no embedding step here.

entry_type is deliberately generic from the start, not
capability-only, because Patch B2 (redaction/denylist entries) reuses
this exact store and this exact write path — see that patch's own
notes for why the denylist's *documentation* half belongs here next to
the capability entries, while its *enforcement* half is a separate,
hard-coded check that has no dependency on anything in this file.

Patch B2 also adds one behavior write_capability_entry() didn't have
before: a write of entry_type="redaction" is audit-logged (via
eo/audit_log.py::write_audit()), because per the architecture plan
"changes to it should be logged — it's config now, not just
documentation." Ordinary entry_type="capability" writes are NOT
audit-logged — those aren't a security boundary, and logging every
capability-doc edit would just be noise in the audit trail next to the
redaction changes that actually matter for a security review.

Entry shape (registry:capability_entries, keyed by entry_id):
    {entry_type, title, doc, tags, source, updated_at}

`registry:` prefix, same reasoning as skill_library.py's own: these
entries are a property of the system, not any one project, and
memory/bus.py's own _namespaced() already exempts every
`registry:`-prefixed key from per-app_slug namespacing.
"""
from __future__ import annotations

import re
import time

from eo.audit_log import write_audit
from memory.bus import read, write

# Attributed actor for redaction-entry writes that don't otherwise carry
# a user_id (e.g. a seed/migration script, or an agent writing on a
# human's behalf without a request-scoped user in hand) — same
# "system:<thing>" shape eo/chat_workspace.py's own _AUTO_PROMOTE_ACTOR
# uses for the identical situation.
_SYSTEM_ACTOR = "system:capability_entries"

CAPABILITY_ENTRIES_KEY = "registry:capability_entries"

# Placeholders reflecting what's actually configured in this repo as of
# Patch B1 (frontend/app/components/tabs/*.jsx, eo/registry.py's
# ROLE_PROMPTS_SEED, backend/config/mcp_servers.json) — same "starting
# set, not the definitive one" posture skill_library.py's own
# SKILL_SEED docstring takes. Edit/extend these as real capabilities
# change; nothing else needs to change to add more.
CAPABILITY_SEED = {
    "frontend_renderable_tabs": {
        "entry_type": "capability",
        "title": "What the frontend can render",
        "doc": (
            "The frontend currently renders these tabs: Chat, Build, "
            "Research, Plan, Role Library, Workflow Templates, Local "
            "Workspace, Notebooks, Audit Log, Token Usage, Growth, "
            "Test, and Settings (frontend/app/components/tabs/*.jsx). "
            "An agent answering \"what can you show me\" or deciding "
            "where a result belongs should check this list rather than "
            "guessing or assuming a tab exists."
        ),
        "tags": ["frontend_capabilities"],
        "source": "hand_written",
    },
    "agent_roster": {
        "entry_type": "capability",
        "title": "What roles/agents exist and what each one does",
        "doc": (
            "Known roles include implementer, verifier, researcher, "
            "web_researcher, writer, fact_checker, and "
            "contradiction_detector (eo/registry.py's "
            "ROLE_PROMPTS_SEED, plus anything hired since via "
            "record_role_hire()). Each role's live brief is available "
            "via eo/registry.py's get_role_prompt()/get_role_metadata() "
            "-- this entry is the pointer to that source, not a copy of "
            "it, since briefs change independently of this seed."
        ),
        "tags": ["agent_roster"],
        "source": "hand_written",
    },
    "mcp_capabilities": {
        "entry_type": "capability",
        "title": "What MCP servers are configured",
        "doc": (
            "MCP servers are configured in "
            "backend/config/mcp_servers.json and loaded by "
            "eo/mcp_registry.py at startup -- as of this writing: "
            "github, context7, and web_search, Tier 1 (no filesystem "
            "or shell-equivalent server; see "
            "docs/decisions/0001-cli-skills-mcp-scope.md for why). "
            "Live connection state and tool lists are NOT cached here "
            "-- call eo/capabilities.py's list_mcp_servers()/"
            "mcp_server_status() for that; this entry only answers "
            "'does an MCP capability exist at all,' not 'is it up "
            "right now.'"
        ),
        "tags": ["mcp_capabilities"],
        "source": "hand_written",
    },
}


def _slug(title: str) -> str:
    """Deterministic id from a title -- same shape
    eo/skill_library.py's own _slug() uses, kept local here for the
    same reason that module's docstring gives for its own copy: this
    only needs the one call, and importing it would couple two stores
    that are deliberately kept separate."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    return slug[:60] or f"entry_{int(time.time() * 1000)}"


def _load_entries() -> dict:
    """Bootstraps from CAPABILITY_SEED exactly once -- same
    read-through-bootstrap shape eo/skill_library.py's own
    _load_skills() and eo/registry.py's own _load_prompts() use."""
    existing = read(CAPABILITY_ENTRIES_KEY, default=None)
    if existing is not None:
        return existing
    seeded = {
        entry_id: {**entry, "updated_at": None}
        for entry_id, entry in CAPABILITY_SEED.items()
    }
    write(CAPABILITY_ENTRIES_KEY, seeded)
    return seeded


def list_capability_entries(entry_type: str = "capability",
                             tags: list[str] | None = None) -> list[dict]:
    """Every stored entry matching entry_type, optionally filtered to
    those carrying at least one of `tags`. Exact-match on tags, not
    semantic -- this is a lookup, not a retrieval-by-similarity path,
    deliberately unlike eo/skill_library.py's get_relevant_skill().

    entry_type defaults to "capability" so ordinary callers (an agent
    asking "what can I do") never see redaction entries (Patch B2)
    unless they explicitly ask for entry_type="redaction" -- keeps the
    two kinds separate at the read path too, not just by convention.
    """
    entries = _load_entries()
    tag_set = set(tags) if tags else None
    return sorted(
        (
            {"entry_id": entry_id, **entry}
            for entry_id, entry in entries.items()
            if entry.get("entry_type") == entry_type
            and (tag_set is None or tag_set & set(entry.get("tags") or []))
        ),
        key=lambda e: e["title"],
    )


def get_capability_entry(entry_id: str) -> dict | None:
    """Single-entry counterpart to list_capability_entries() -- full
    record for one entry_id regardless of entry_type, or None if it
    doesn't exist. Same "richer single-record read next to the bulk
    list" role eo/skill_library.py's own get_skill() plays."""
    entry = _load_entries().get(entry_id)
    return {"entry_id": entry_id, **entry} if entry else None


def write_capability_entry(title: str, doc_text: str, tags: list[str],
                            entry_type: str = "capability",
                            source: str = "hand_written",
                            user_id: str | None = None) -> str:
    """Persists a capability (or, from Patch B2, redaction) entry.
    entry_id is derived from `title` (see _slug()), so re-writing the
    same title updates that entry in place -- same "no accumulating
    near-duplicates" reasoning eo/skill_library.py's own write_skill()
    gives for the identical choice.

    No Vector/embedding step here, unlike write_skill() -- this store
    is looked up by tag, never by semantic similarity, so there's
    nothing to embed. Returns the entry_id.

    Patch B2: when entry_type="redaction", this write is additionally
    logged via eo/audit_log.py::write_audit() -- a redaction entry is
    config that changes what an agent is told is off-limits, so per
    the architecture plan it gets the same "what changed and who
    changed it" trail as any other config mutation in this codebase.
    `user_id` attributes the change; if the caller doesn't have a
    request-scoped user in hand (a seed script, an agent writing on a
    human's behalf without one threaded through), it falls back to
    _SYSTEM_ACTOR rather than skipping the audit row -- same posture
    chat_workspace.py's _AUTO_PROMOTE_ACTOR takes for its own
    system-initiated writes. Ordinary capability-entry writes are not
    audited; see this module's docstring for why.
    """
    entry_id = _slug(title)
    entries = _load_entries()
    is_new = entry_id not in entries
    entries[entry_id] = {
        "entry_type": entry_type,
        "title": title,
        "doc": doc_text,
        "tags": list(tags),
        "source": source,
        "updated_at": time.time(),
    }
    write(CAPABILITY_ENTRIES_KEY, entries)

    if entry_type == "redaction":
        write_audit(
            user_id or _SYSTEM_ACTOR,
            "capability_entry.redaction_write",
            "capability_entry",
            entry_id,
            {"title": title, "tags": list(tags), "created": is_new},
        )

    return entry_id
