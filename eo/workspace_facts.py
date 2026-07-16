"""
eo/workspace_facts.py — workspace-level memory ("facts"), Part 0 Section
0.3. This is the third tier of the three-tier memory model:

    Tier 1 — conversation-level: eo/conversation_memory.py
    Tier 2 — section-level (a workspace's linked chats): eo/chat_workspace.py
    Tier 3 — workspace-level facts true across the whole project: HERE

Storage: one memory-bus key per workspace, same bus module.py already
uses everywhere else (eo/conversation_memory.py's "conversation:
{session_id}", agents/memory_search.py's Vector ids) — no new storage
technology, just a new key shape:

    workspace_facts:{workspace_id}

Stores a small STRUCTURED object, not free text — {brand_voice,
target_user, tech_stack, custom}. `custom` is a free-form dict, same
"don't over-specify the schema" philosophy as eo/graph_edges.py's
free-form `relation` field: any domain can stash a fact it cares about
under `custom` without this module needing a schema change for every
new fact type.

Who writes: mostly the user directly, via a per-workspace settings
panel (also the natural home for "custom instructions per notebook" —
store the pinned persona/tone/goal in the same object, under `custom`).
Agents can propose additions via propose_fact() instead of writing
directly — same accept/reject shape as the Notes domain's silent
note-taking agent, so an agent-suggested fact never silently overwrites
something the user set on purpose.

Who reads: eo/conversation_memory.py's get_full_context()/
get_light_context(), at prompt-build time, via format_facts_for_prompt()
below — see that module for how workspace_id gets resolved from a
session_id.
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

EMPTY_FACTS = {"brand_voice": "", "target_user": "", "tech_stack": [], "custom": {}}


def _key(workspace_id: str) -> str:
    return f"workspace_facts:{workspace_id}"


def get_facts(workspace_id: str) -> dict:
    """Always returns the full shape (brand_voice/target_user/tech_stack/
    custom), even if nothing's been set yet — callers never need to
    check for missing keys."""
    if not workspace_id:
        return dict(EMPTY_FACTS)
    stored = read(_key(workspace_id), default=None)
    if not stored:
        return dict(EMPTY_FACTS)
    merged = dict(EMPTY_FACTS)
    merged.update(stored)
    return merged


def set_facts(workspace_id: str, facts: dict) -> dict:
    """Full replace — the settings panel sends the whole object back on
    save (small object, no reason to diff/patch). Unknown top-level keys
    the caller sends are preserved as-is rather than stripped, so a
    future fact type doesn't need this module touched to round-trip."""
    if not workspace_id:
        raise ValueError("workspace_id is required")
    current = get_facts(workspace_id)
    current.update(facts or {})
    write(_key(workspace_id), current)
    return current


def update_custom_fact(workspace_id: str, key: str, value) -> dict:
    """Convenience for setting a single `custom` entry without the
    caller having to read-modify-write the whole object themselves —
    e.g. a Plan-domain role stashing "deploy_target": "vercel" without
    needing to know or preserve brand_voice/target_user/tech_stack."""
    if not workspace_id or not key:
        raise ValueError("workspace_id and key are required")
    facts = get_facts(workspace_id)
    facts["custom"][key] = value
    write(_key(workspace_id), facts)
    return facts


# NOT CURRENTLY CALLED ANYWHERE — audited 2026-07-16. This function, and
# the accept/reject/list machinery below it, are fully wired end to end
# on every OTHER layer: the API routes (GET/PUT .../facts,
# .../facts/candidates, accept/reject) and the frontend FactsView UI
# (agent-suggested-facts panel with accept/discard buttons) are both
# live and correct. What's missing is a caller: no agent in agents/
# invokes propose_fact() today.
#
# It's not a simple wiring gap, either. Every agent that scans a real
# NOTEBOOK (this module's actual workspace_id, from eo/chat_workspace.py)
# is note_clusterer.py, and its job is topic clustering, not fact
# extraction — it has no signal that maps to brand_voice/tech_stack.
# Every OTHER agent that resolves something called "workspace_id"
# (contradiction_prefilter.py, source_quality_flagger.py,
# dataset_analyst.py) is actually scoped to the tier-3 build/research
# PIPELINE's per-session id (get_current_app_slug() or original_idea),
# which is a different, unrelated value that happens to share a name —
# calling propose_fact() from one of those would write candidates under
# an id no real notebook will ever read back.
#
# So this needs a small, purpose-built agent (same shape as
# note_clusterer.py: real workspace_id param, deterministic-first) that
# doesn't exist yet, not a one-line hookup into something already here.
# Left dormant on purpose until that agent is built — do not wire this
# into an unrelated pipeline agent just to make it "used."
def propose_fact(workspace_id: str, key: str, value, proposed_by: str) -> dict:
    """Agent-proposed addition, held separately under
    `workspace_facts_candidates:{workspace_id}` until the user accepts
    it — same accept/reject shape as the Notes domain's silent
    note-taking agent proposing candidate notes. Does NOT touch the
    live facts object, so a proposal can never silently overwrite
    something the user set on purpose."""
    if not workspace_id or not key:
        raise ValueError("workspace_id and key are required")
    candidates_key = f"workspace_facts_candidates:{workspace_id}"
    candidates = read(candidates_key, default=[])
    candidates.append({"key": key, "value": value, "proposed_by": proposed_by})
    write(candidates_key, candidates)
    return candidates


def list_candidates(workspace_id: str) -> list:
    return read(f"workspace_facts_candidates:{workspace_id}", default=[])


def accept_candidate(workspace_id: str, index: int) -> dict:
    """User accepts a proposed fact into `custom`, and it's removed from
    the pending list either way (accepted or not — a rejected proposal
    shouldn't linger for future acceptance either; propose again if
    still relevant)."""
    candidates_key = f"workspace_facts_candidates:{workspace_id}"
    candidates = read(candidates_key, default=[])
    if index < 0 or index >= len(candidates):
        raise IndexError(f"no candidate at index {index}")
    accepted = candidates.pop(index)
    write(candidates_key, candidates)
    return update_custom_fact(workspace_id, accepted["key"], accepted["value"])


def reject_candidate(workspace_id: str, index: int) -> None:
    candidates_key = f"workspace_facts_candidates:{workspace_id}"
    candidates = read(candidates_key, default=[])
    if index < 0 or index >= len(candidates):
        raise IndexError(f"no candidate at index {index}")
    candidates.pop(index)
    write(candidates_key, candidates)


def format_facts_for_prompt(workspace_id: str) -> str:
    """Renders the facts object as a short text block ready to prepend
    to a generation agent's context — the one thing
    eo/conversation_memory.py actually needs from this module. Returns
    "" if nothing's been set, same "no history yet" convention
    conversation_memory.py's own get_full_context() uses, so prepending
    is always a plain string concat with no extra empty-check needed by
    callers of THAT module either."""
    facts = get_facts(workspace_id)
    lines = []
    if facts.get("brand_voice"):
        lines.append(f"Brand voice: {facts['brand_voice']}")
    if facts.get("target_user"):
        lines.append(f"Target user: {facts['target_user']}")
    if facts.get("tech_stack"):
        lines.append(f"Tech stack: {', '.join(facts['tech_stack'])}")
    for k, v in (facts.get("custom") or {}).items():
        lines.append(f"{k}: {v}")
    if not lines:
        return ""
    return "--- workspace facts ---\n" + "\n".join(lines)