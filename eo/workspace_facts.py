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
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

# "ideas" and "context" — Part 1 of the Data-bubble content work. The
# upcoming tier-2/3 summarizer classifies extracted facts into one of
# {decision, preference, idea, context}; decision/preference already map
# onto "decisions"/"instructions" below, but idea and context had no
# home that wasn't the catch-all "extractions" bucket (explicitly the
# thing we're trying to avoid dumping everything into). No other code
# change needed for this: _coerce_section_bucket() and
# format_facts_for_prompt() already handle arbitrary section names —
# this tuple only controls render order.
PROJECT_SECTION_ORDER = (
    "decisions",
    "entities",
    "components",
    "connections",
    "hardware",
    "extractions",
    "instructions",
    "ideas",
    "context",
)

# Part 3 — where each of eo/fact_summarizer.py's structured `category`
# values lands. decision/preference reuse existing sections (decisions
# already receives D1's routing-metadata writes too, distinguished by
# `source`; instructions is this module's own docstring-established
# home for "custom instructions per notebook"). idea/context use the
# two sections added above for exactly this purpose. Single source of
# truth so eo/fact_summarizer.py's valid-category check and
# api/task_runner.py's write-time lookup can't drift apart.
CATEGORY_TO_SECTION = {
    "decision": "decisions",
    "preference": "instructions",
    "idea": "ideas",
    "context": "context",
}

EMPTY_FACTS = {
    "brand_voice": "",
    "target_user": "",
    "tech_stack": [],
    "custom": {},
    "sections": {},
    "ledger": [],
}


def _key(workspace_id: str) -> str:
    return f"workspace_facts:{workspace_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(text).strip().lower()).strip("_")
    return slug[:max_len] or "item"


def _canonical_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    except Exception:
        return repr(value)


def _empty_section_bucket() -> dict:
    return {"entries": {}, "order": []}


def _coerce_section_bucket(value, section_name: str | None = None) -> dict:
    bucket = _empty_section_bucket()
    if not value:
        return bucket

    if isinstance(value, list):
        iterable = [(item.get("key") or item.get("id") or item.get("entry_key") or _entry_key(section_name or "section", item), item)
                    for item in value if isinstance(item, dict)]
    elif isinstance(value, dict) and "entries" in value:
        entries = value.get("entries") or {}
        if isinstance(entries, list):
            iterable = [(item.get("key") or item.get("id") or item.get("entry_key") or _entry_key(section_name or "section", item), item)
                        for item in entries if isinstance(item, dict)]
        elif isinstance(entries, dict):
            iterable = list(entries.items())
        else:
            iterable = []
    elif isinstance(value, dict):
        iterable = []
        for key, item in value.items():
            if key == "order":
                continue
            if isinstance(item, dict):
                payload = dict(item)
            else:
                payload = {"text": item}
            payload.setdefault("key", key)
            iterable.append((payload.get("key") or key, payload))
    else:
        return bucket

    order = value.get("order") if isinstance(value, dict) else None
    for key, entry in iterable:
        normalized = _normalize_entry(section_name or "section", entry, source=entry.get("source"), source_ref=entry.get("source_ref"))
        bucket["entries"][key] = normalized
        if key not in bucket["order"]:
            bucket["order"].append(key)
    if isinstance(order, list):
        seen = set(bucket["order"])
        ordered = [key for key in order if key in bucket["entries"]]
        ordered.extend([key for key in bucket["order"] if key not in ordered])
        bucket["order"] = ordered
    return bucket


def _entry_key(section: str, entry: dict) -> str:
    explicit = entry.get("key") or entry.get("id") or entry.get("entry_key")
    if explicit:
        return str(explicit)
    for field in ("title", "label", "name"):
        value = entry.get(field)
        if value:
            return f"{section}:{_slug(value)}"
    data = entry.get("data")
    basis = data if data is not None else {
        k: v for k, v in entry.items()
        if k not in {"source", "source_ref", "sources", "first_seen_at", "last_seen_at", "touch_count"}
    }
    return f"{section}:{_canonical_json(basis)}:{_slug(uuid.uuid4().hex[:8])}"


def _normalize_entry(section: str, entry: dict, source: str | None = None, source_ref: str | None = None) -> dict:
    raw = dict(entry or {}) if isinstance(entry, dict) else {"text": str(entry)}
    key = _entry_key(section, raw)
    title = raw.get("title") or raw.get("label") or raw.get("name") or key
    text = (raw.get("text") or raw.get("summary") or "").strip()
    data = raw.get("data")
    if data is None:
        data = {
            k: v for k, v in raw.items()
            if k not in {
                "key", "id", "entry_key", "title", "label", "name", "text", "summary",
                "source", "source_ref", "sources", "first_seen_at", "last_seen_at", "touch_count",
            }
        }
        if not data:
            data = None
    sources = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        sources.append({"source": item.get("source"), "source_ref": item.get("source_ref")})
    if source or source_ref:
        sources.append({"source": source, "source_ref": source_ref})
    deduped_sources = []
    seen = set()
    for item in sources:
        token = (item.get("source"), item.get("source_ref"))
        if token in seen:
            continue
        seen.add(token)
        deduped_sources.append(item)
    now = _now_iso()
    return {
        "key": key,
        "title": title,
        "summary": raw.get("summary") or text or title,
        "text": text,
        "data": data,
        "source": source or raw.get("source"),
        "source_ref": source_ref or raw.get("source_ref"),
        "sources": deduped_sources,
        "first_seen_at": raw.get("first_seen_at") or now,
        "last_seen_at": raw.get("last_seen_at") or now,
        "touch_count": int(raw.get("touch_count") or 1),
    }


def _merge_entry(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for field in ("title", "summary", "text"):
        value = incoming.get(field)
        if value:
            merged[field] = value
    if incoming.get("data") is not None:
        if isinstance(merged.get("data"), dict) and isinstance(incoming["data"], dict):
            combined = dict(merged["data"])
            combined.update(incoming["data"])
            merged["data"] = combined
        else:
            merged["data"] = incoming["data"]
    merged_sources = []
    seen = set()
    for item in list(existing.get("sources") or []) + list(incoming.get("sources") or []):
        token = (item.get("source"), item.get("source_ref"))
        if token in seen:
            continue
        seen.add(token)
        merged_sources.append(item)
    merged["sources"] = merged_sources
    merged["first_seen_at"] = existing.get("first_seen_at") or incoming.get("first_seen_at")
    merged["last_seen_at"] = incoming.get("last_seen_at") or _now_iso()
    merged["touch_count"] = int(existing.get("touch_count") or 1) + 1
    if incoming.get("source"):
        merged["source"] = incoming["source"]
    if incoming.get("source_ref"):
        merged["source_ref"] = incoming["source_ref"]
    return merged


def _merge_sections(existing_sections: dict, incoming_sections: dict) -> dict:
    merged = {}
    for section_name in set(existing_sections or {}) | set(incoming_sections or {}):
        existing_bucket = _coerce_section_bucket((existing_sections or {}).get(section_name), section_name)
        incoming_bucket = _coerce_section_bucket((incoming_sections or {}).get(section_name), section_name)
        bucket = {"entries": dict(existing_bucket["entries"]), "order": list(existing_bucket["order"])}
        for key in incoming_bucket["order"]:
            entry = incoming_bucket["entries"][key]
            if key in bucket["entries"]:
                bucket["entries"][key] = _merge_entry(bucket["entries"][key], entry)
            else:
                bucket["entries"][key] = entry
                bucket["order"].append(key)
        merged[section_name] = bucket
    return merged


def _ledger_entry(section: str, entry: dict, event: str, source: str | None = None, source_ref: str | None = None) -> dict:
    return {
        "event_id": uuid.uuid4().hex,
        "at": _now_iso(),
        "section": section,
        "key": entry["key"],
        "event": event,
        "title": entry.get("title"),
        "summary": entry.get("summary") or entry.get("title") or entry["key"],
        "source": source or entry.get("source"),
        "source_ref": source_ref or entry.get("source_ref"),
    }


def _merge_ledger(existing_ledger: list, incoming_ledger: list) -> list:
    merged = list(existing_ledger or [])
    for item in incoming_ledger or []:
        if isinstance(item, dict):
            merged.append(item)
    return merged


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
    merged["custom"] = dict(merged.get("custom") or {})
    merged["sections"] = _merge_sections({}, merged.get("sections") or {}) if merged.get("sections") else {}
    if not merged.get("sections"):
        merged["sections"] = {}
    merged["ledger"] = list(merged.get("ledger") or [])
    return merged


def set_facts(workspace_id: str, facts: dict) -> dict:
    """Full replace — the settings panel sends the whole object back on
    save (small object, no reason to diff/patch). Unknown top-level keys
    the caller sends are preserved as-is rather than stripped, so a
    future fact type doesn't need this module touched to round-trip."""
    if not workspace_id:
        raise ValueError("workspace_id is required")
    current = get_facts(workspace_id)
    incoming = facts or {}
    for field in ("brand_voice", "target_user", "tech_stack"):
        if field in incoming and incoming[field] is not None:
            current[field] = incoming[field]
    if "custom" in incoming and incoming["custom"] is not None:
        custom = dict(current.get("custom") or {})
        custom.update(incoming["custom"])
        current["custom"] = custom
    if "sections" in incoming and incoming["sections"] is not None:
        current["sections"] = _merge_sections(current.get("sections") or {}, incoming["sections"])
    if "ledger" in incoming and incoming["ledger"] is not None:
        current["ledger"] = _merge_ledger(current.get("ledger") or [], incoming["ledger"])
    for key, value in incoming.items():
        if key in {"brand_voice", "target_user", "tech_stack", "custom", "sections", "ledger"}:
            continue
        if value is not None:
            current[key] = value
    write(_key(workspace_id), current)

    _invalidate_facts_cache(workspace_id, current)
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

    _invalidate_facts_cache(workspace_id, facts, changed_key=key, changed_value=value)
    return facts


def record_section_entries(workspace_id: str, section: str, entries: list, source: str = None,
                           source_ref: str = None, event: str = "upsert") -> dict:
    """Upserts one or more structured entries into a stable section and
    appends a timeline record for each touch. The section store keeps one
    row per underlying fact key; the ledger preserves chronological
    history for the same fact without duplicating the section entry.
    """
    if not workspace_id or not section:
        raise ValueError("workspace_id and section are required")
    current = get_facts(workspace_id)
    sections = dict(current.get("sections") or {})
    bucket = _coerce_section_bucket(sections.get(section), section)
    ledger = list(current.get("ledger") or [])

    for raw_entry in entries or []:
        normalized = _normalize_entry(section, raw_entry, source=source, source_ref=source_ref)
        key = normalized["key"]
        if key in bucket["entries"]:
            bucket["entries"][key] = _merge_entry(bucket["entries"][key], normalized)
        else:
            bucket["entries"][key] = normalized
            bucket["order"].append(key)
        ledger.append(_ledger_entry(section, normalized, event=event, source=source, source_ref=source_ref))

    sections[section] = bucket
    current["sections"] = sections
    current["ledger"] = ledger
    write(_key(workspace_id), current)
    _invalidate_facts_cache(workspace_id, current, changed_key=f"section:{section}", changed_value=len(entries or []))
    return current


def record_section_entry(workspace_id: str, section: str, entry: dict, source: str = None,
                         source_ref: str = None, event: str = "upsert") -> dict:
    return record_section_entries(workspace_id, section, [entry], source=source, source_ref=source_ref, event=event)
def _invalidate_facts_cache(workspace_id: str, facts: dict, changed_key: str = None,
                             changed_value=None) -> None:
    """Shared helper — a fact changing means any cached answer that
    might reference it could now be stale. Fire-and-forget, same
    discipline as every other side-effect in this file's neighbors."""
    try:
        from eo.semantic_cache import invalidate_cache
        if changed_key:
            text = f"{changed_key}: {changed_value}"
        else:
            text = format_facts_for_prompt(workspace_id) or workspace_id
        invalidate_cache(text, workspace_id=workspace_id)
    except Exception as exc:
        print(f"  [workspace_facts] cache invalidation failed, skipped: {exc}")

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
    sections = facts.get("sections") or {}
    rendered_sections = []
    for section_name in PROJECT_SECTION_ORDER + tuple(sorted(k for k in sections if k not in PROJECT_SECTION_ORDER)):
        bucket = _coerce_section_bucket(sections.get(section_name), section_name)
        if not bucket["entries"]:
            continue
        rendered_sections.append(f"[{section_name}]")
        for key in bucket["order"][:10]:
            entry = bucket["entries"].get(key)
            if not entry:
                continue
            summary = entry.get("summary") or entry.get("title") or key
            rendered_sections.append(f"- {key}: {summary}")
            text = (entry.get("text") or "").strip()
            if text and text != summary:
                rendered_sections.append(f"  {text}")
    if rendered_sections:
        lines.append("Sections:")
        lines.extend(rendered_sections)
    ledger = facts.get("ledger") or []
    if ledger:
        lines.append("Timeline:")
        for entry in ledger[-8:]:
            if not isinstance(entry, dict):
                continue
            stamp = entry.get("at") or ""
            section_name = entry.get("section") or "section"
            key = entry.get("key") or "item"
            summary = entry.get("summary") or entry.get("title") or key
            lines.append(f"- {stamp} {section_name}/{key}: {summary}")
    if not lines:
        return ""
    return "--- workspace facts ---\n" + "\n".join(lines)