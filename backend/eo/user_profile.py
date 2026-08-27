"""
eo/user_profile.py — Patch B1. Per-account behavior/personalization
memory. Structural sibling of eo/workspace_facts.py, but scoped to a
person across every workspace they touch, not to one project:

    workspace_facts.py  — Tier 3, scoped to a workspace_id
    user_profile.py     — HERE, scoped to an owner_id

Storage: one memory-bus key per account, same bus module every other
eo/*_facts-shaped store already uses — no new storage technology, just
a new key shape:

    user_profile:{owner_id}

Stores a small STRUCTURED record, not free text:
  - domains        — per-topic expertise level, e.g. "React": {level,
                      confidence, evidence_count}
  - likes/dislikes — topic -> {confidence, evidence_count}
  - error_patterns — recurring mistake pattern -> {confidence,
                      evidence_count, last_seen_at}
  - output_prefs   — {default_format, confidence, evidence_count}
  - corrections    — flat audit log of every explicit overwrite (who
                      said what, what it replaced), append-only

Who writes: eo/fact_summarizer.py's extended classification (Patch
B2) is the main inferred-signal writer; a direct user statement in
chat ("actually, I prefer diagrams over text") is an explicit-signal
writer via the same entry points here, and Patch B4's
override_profile_fact() is the dedicated explicit-correction path
built on top of this module's primitives.

Who reads: Patch B3's format_profile_for_prompt() (system-prompt
injection only, never surfaced to the user directly) and Patch B6's
read-only settings-panel endpoint.

--- The one design rule this module exists to enforce ---

Confidence must rise with repeated CORROBORATING evidence, not on a
single observation. A single offhand comment ("I guess I don't love
Python") must not calcify into a permanent "dislikes: Python" trait
after one mention. This module's write path (record_signal() below)
bakes that in from the start, rather than leaving it to whichever
future caller happens to write the first draft of the scoring — see
record_signal()'s docstring for the exact curve.

Explicit signals (the user states something about themselves
directly) are the one deliberate exception: they set confidence high
immediately and overwrite whatever was there, same "one clear
correction outweighs many weak prior inferences" posture Patch B4
formalizes into its own named entry point. That entry point is a thin
wrapper over record_signal(..., explicit=True) plus a corrections-log
append — the actual confidence/overwrite behavior lives here so B4
doesn't need to re-derive it.
"""
import os
import sys
import uuid
from datetime import UTC, datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

# Categories that store a per-key dict of evidence-scored entries.
# output_prefs is deliberately NOT in this set — it's a single
# current-value record (you have exactly one default output format at
# a time), not a set of independent topics you might hold several of
# at once the way domains/likes/dislikes/error_patterns are.
SIGNAL_CATEGORIES = ("domains", "likes", "dislikes", "error_patterns")

# --- confidence curve -------------------------------------------------
# First inferred observation of a topic lands at a low starting
# confidence; each additional corroborating observation nudges it up,
# with diminishing returns, capped below 1.0 so an inferred signal can
# never fully match the certainty of something the user stated
# outright (EXPLICIT_CONFIDENCE below). An explicit statement always
# wins immediately regardless of how much or little inferred history
# exists for that key.
INFERRED_STARTING_CONFIDENCE = 0.2
INFERRED_CONFIDENCE_STEP = 0.15
INFERRED_CONFIDENCE_CAP = 0.85
EXPLICIT_CONFIDENCE = 0.95


def _empty_profile() -> dict:
    """Builds a brand-new dict with its own independent nested
    containers every call. get_profile()'s two early-return branches
    (no owner_id / nothing stored yet) MUST call this rather than
    `dict(EMPTY_PROFILE)` — see workspace_facts.py's own
    _empty_facts() for the exact cross-account leak this avoids: a
    shallow copy would share the same nested dict/list objects across
    every never-written account for the life of the process."""
    return {
        "domains": {},
        "likes": {},
        "dislikes": {},
        "error_patterns": {},
        "output_prefs": {
            "default_format": None,
            "confidence": 0.0,
            "evidence_count": 0,
        },
        "corrections": [],
    }


EMPTY_PROFILE = _empty_profile()


def _key(owner_id: str) -> str:
    return f"user_profile:{owner_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def get_profile(owner_id: str) -> dict:
    """Always returns the full shape (domains/likes/dislikes/
    error_patterns/output_prefs/corrections), even if nothing's been
    stored yet — every caller (B2's writer, B3's prompt formatter,
    B6's settings read) can index straight into it without a defensive
    key check."""
    if not owner_id:
        return _empty_profile()
    stored = read(_key(owner_id), default=None)
    if not stored:
        return _empty_profile()
    merged = _empty_profile()
    merged.update(stored)
    for category in SIGNAL_CATEGORIES:
        merged[category] = dict(merged.get(category) or {})
    output_prefs = _empty_profile()["output_prefs"]
    output_prefs.update(merged.get("output_prefs") or {})
    merged["output_prefs"] = output_prefs
    merged["corrections"] = list(merged.get("corrections") or [])
    return merged


def _next_confidence(existing_entry: dict | None, explicit: bool) -> tuple[float, int]:
    """The evidence-count-gated curve described in this module's
    docstring. Returns (new_confidence, new_evidence_count).

    - explicit=True always jumps straight to EXPLICIT_CONFIDENCE,
      evidence_count reset to 1 — an explicit statement isn't "one
      more piece of corroborating evidence" for a prior inferred
      guess, it supersedes it.
    - explicit=False on a prior EXPLICIT entry does not walk the
      confidence back down — a later offhand inferred signal must not
      erode something the user told us directly. It still increments
      evidence_count for bookkeeping, but confidence stays pinned at
      EXPLICIT_CONFIDENCE.
    - explicit=False on a prior inferred (or absent) entry advances
      one step up the capped curve.
    """
    prior_confidence = float((existing_entry or {}).get("confidence") or 0.0)
    prior_evidence_count = int((existing_entry or {}).get("evidence_count") or 0)
    prior_was_explicit = bool((existing_entry or {}).get("explicit"))

    if explicit:
        return EXPLICIT_CONFIDENCE, 1

    new_evidence_count = prior_evidence_count + 1
    if prior_was_explicit:
        return prior_confidence, new_evidence_count

    if prior_evidence_count <= 0:
        return INFERRED_STARTING_CONFIDENCE, new_evidence_count

    stepped = prior_confidence + INFERRED_CONFIDENCE_STEP
    return min(stepped, INFERRED_CONFIDENCE_CAP), new_evidence_count


def record_signal(owner_id: str, category: str, key: str, value=None,
                   explicit: bool = False, source: str | None = None) -> dict:
    """Core upsert for domains/likes/dislikes/error_patterns. `key` is
    the topic/pattern (e.g. "React", "verbose explanations"); `value`
    is category-specific payload merged into the entry as-is (for
    domains this is typically {"level": "intermediate"} — anything
    else is passed straight through as the entry's `value` field so a
    future category-specific shape doesn't require a schema change
    here).

    Every call — explicit or inferred — bumps evidence_count and
    last_seen_at; only explicit calls overwrite the visible value
    immediately (see _next_confidence()). Whether this counted as an
    overwrite of a materially different prior value is exactly what
    Patch B4's override_profile_fact() checks before appending to the
    corrections log — that bookkeeping lives in B4's wrapper, not
    here, so this primitive stays reusable for the plain inferred
    path B2 drives without always paying for a corrections-log read.
    """
    if not owner_id or not category or not key:
        raise ValueError("owner_id, category, and key are required")
    if category not in SIGNAL_CATEGORIES:
        raise ValueError(f"unknown signal category: {category!r}")

    profile = get_profile(owner_id)
    bucket = profile[category]
    existing = bucket.get(key)
    confidence, evidence_count = _next_confidence(existing, explicit)
    now = _now_iso()

    entry = dict(existing or {})
    entry.update({
        "key": key,
        "value": value if value is not None else entry.get("value"),
        "confidence": confidence,
        "evidence_count": evidence_count,
        "explicit": explicit or bool(entry.get("explicit")),
        "source": source or entry.get("source"),
        "first_seen_at": entry.get("first_seen_at") or now,
        "last_seen_at": now,
    })
    bucket[key] = entry
    write(_key(owner_id), profile)
    return profile


def set_output_pref(owner_id: str, default_format: str, explicit: bool = False,
                     source: str | None = None) -> dict:
    """output_prefs is a single current-value record rather than a
    keyed bucket (see SIGNAL_CATEGORIES's docstring note), so it gets
    its own setter instead of going through record_signal()'s
    per-key bucket path — but reuses the exact same confidence curve
    via _next_confidence() so a repeatedly-inferred format preference
    still gains confidence the same gated way a repeated domain/like
    signal does."""
    if not owner_id or not default_format:
        raise ValueError("owner_id and default_format are required")

    profile = get_profile(owner_id)
    existing = profile["output_prefs"]
    same_value = existing.get("default_format") == default_format
    confidence, evidence_count = _next_confidence(
        existing if same_value else None, explicit,
    )
    now = _now_iso()
    profile["output_prefs"] = {
        "default_format": default_format,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "explicit": explicit or (same_value and bool(existing.get("explicit"))),
        "source": source or (existing.get("source") if same_value else None),
        "first_seen_at": existing.get("first_seen_at") if same_value else now,
        "last_seen_at": now,
    }
    write(_key(owner_id), profile)
    return profile


def append_correction(owner_id: str, field: str, key: str | None,
                       old_value, new_value, reason: str | None = None) -> dict:
    """Appends one entry to the append-only `corrections` audit log.
    Deliberately a thin, standalone primitive rather than folded into
    record_signal()/set_output_pref() themselves — Patch B4's
    override_profile_fact() is the caller that decides WHEN a change
    is correction-worthy (old value present and materially different
    from new value); this function only knows how to write the log
    entry once that decision's been made, so record_signal()'s plain
    inferred-evidence path (called far more often, by B2) never pays
    for a log append it doesn't need."""
    if not owner_id or not field:
        raise ValueError("owner_id and field are required")

    profile = get_profile(owner_id)
    profile["corrections"].append({
        "correction_id": f"corr_{uuid.uuid4().hex[:10]}",
        "at": _now_iso(),
        "field": field,
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    })
    write(_key(owner_id), profile)
    return profile


def list_corrections(owner_id: str) -> list:
    return get_profile(owner_id)["corrections"]
