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
import re
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

# --- Patch B2 wiring: fact_summarizer.py's profile_signals[].type ------
# Maps each structured signal type the extraction pipeline can emit to
# the bucket it lands in here. "format_preference" is deliberately
# absent from this dict — it doesn't go into a keyed bucket at all, it
# drives set_output_pref() instead (see SIGNAL_CATEGORIES's own
# docstring note on why output_prefs isn't a keyed bucket like the
# other four). Single source of truth so eo/fact_summarizer.py's
# valid-type check and api/task_runner.py's write-time routing can't
# drift apart, same discipline workspace_facts.py's CATEGORY_TO_SECTION
# already established for the workspace-fact side.
PROFILE_SIGNAL_TYPE_TO_CATEGORY = {
    "expertise_signal": "domains",
    "error_pattern": "error_patterns",
    "like": "likes",
    "dislike": "dislikes",
}
FORMAT_PREFERENCE_SIGNAL_TYPE = "format_preference"
PROFILE_SIGNAL_TYPES = tuple(PROFILE_SIGNAL_TYPE_TO_CATEGORY) + (FORMAT_PREFERENCE_SIGNAL_TYPE,)


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

# --- prompt-injection gating (Patch B3) --------------------------------
# A single inferred observation (INFERRED_STARTING_CONFIDENCE, 0.2) is
# exactly the "one offhand comment" this module's own docstring warns
# must not calcify into a trait — that warning is about *storage*, but
# the same posture applies double to *surfacing* it to a generation
# agent as if it were settled. Gate prompt injection one step higher,
# at the first-corroboration confidence, so it takes at least one
# repeat observation (or an explicit statement, which jumps straight
# to EXPLICIT_CONFIDENCE) before a signal is trusted enough to steer a
# response. Reuses the same curve constants rather than a hand-picked
# number, so this gate can't silently drift out of sync with
# _next_confidence()'s own steps.
PROMPT_MIN_CONFIDENCE = INFERRED_STARTING_CONFIDENCE + INFERRED_CONFIDENCE_STEP

# Per-category cap on how many entries format_profile_for_prompt()
# will inject — "pulls only the profile slices relevant to the current
# topic, not a full dump" (Patch B3's own goal). Even after topic
# filtering, keep this bounded so a heavily-corroborated account can
# never crowd the system prompt.
PROMPT_MAX_ENTRIES_PER_CATEGORY = 5

# Display label per signal category, in the order they're rendered.
CATEGORY_PROMPT_LABELS = {
    "domains": "Expertise",
    "likes": "Likes",
    "dislikes": "Dislikes",
    "error_patterns": "Watch for (recurring mistake pattern)",
}


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


def apply_profile_signal(owner_id: str, signal: dict, source: str | None = None) -> dict | None:
    """Patch B2's write-side entry point — takes one already-validated
    signal dict shaped like eo/fact_summarizer.py's
    `profile_signals[]` entries (`{"type", "key", "value",
    "explicit"}`) and routes it to the right primitive above, using
    PROFILE_SIGNAL_TYPE_TO_CATEGORY as the single source of truth for
    "which bucket does this type land in."

    Deliberately lives here rather than in api/task_runner.py's caller
    so the type->category mapping and the actual write stay next to
    each other — a future third signal source (e.g. a direct in-chat
    statement handled outside the summarizer) can call this same
    function instead of re-deriving the routing.

    Returns the updated profile dict, or None for an unrecognized
    `signal["type"]` (fail-open at the call site's discretion — this
    function itself still raises on missing owner_id/value the way
    record_signal()/set_output_pref() already do, since those are
    caller bugs, not just an unfamiliar signal shape)."""
    if not isinstance(signal, dict):
        return None
    signal_type = signal.get("type")
    value = signal.get("value")
    explicit = bool(signal.get("explicit"))

    if signal_type == FORMAT_PREFERENCE_SIGNAL_TYPE:
        return set_output_pref(owner_id, value, explicit=explicit, source=source)

    category = PROFILE_SIGNAL_TYPE_TO_CATEGORY.get(signal_type)
    if not category:
        return None
    return record_signal(owner_id, category, signal.get("key"), value=value,
                          explicit=explicit, source=source)


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


def override_profile_fact(owner_id: str, field: str, new_value, key: str | None = None,
                           reason: str | None = None, source: str | None = None) -> dict:
    """Patch B4 — the dedicated explicit-correction entry point.

    This is the direct fix for "one wrong guess became permanent
    forever": whenever the user's own words directly contradict a
    stored value, this sets the new value at EXPLICIT_CONFIDENCE
    immediately (never gradually, never gated behind repeated
    corroboration) and appends an audit entry to the `corrections`
    log — one clear correction outweighs any number of weak prior
    inferences.

    `field` is either one of SIGNAL_CATEGORIES ("domains", "likes",
    "dislikes", "error_patterns"), in which case `key` is required
    and identifies the topic/pattern being corrected, or
    "output_prefs", in which case `key` is ignored (it's a single
    current-value record, not a keyed bucket — see
    SIGNAL_CATEGORIES's docstring note).

    Deliberately a thin wrapper: the actual confidence/overwrite
    behavior already lives in record_signal()/set_output_pref() (via
    explicit=True), so this function's only real job is (1) read the
    prior value before it's overwritten, (2) call the right B1
    primitive to overwrite it, and (3) decide whether that overwrite
    is correction-worthy — i.e. a prior value was actually present
    and materially different from the new one — before appending to
    the corrections log. A correction against a field that had never
    been set (nothing to contradict) still applies the explicit value
    but is not logged as a correction, since there was no prior guess
    to override.
    """
    if not owner_id or not field:
        raise ValueError("owner_id and field are required")
    if field != "output_prefs" and field not in SIGNAL_CATEGORIES:
        raise ValueError(f"unknown profile field: {field!r}")
    if field != "output_prefs" and not key:
        raise ValueError("key is required when overriding a domains/likes/dislikes/error_patterns fact")

    if field == "output_prefs":
        prior = get_profile(owner_id)["output_prefs"]
        old_value = prior.get("default_format")
        profile = set_output_pref(owner_id, new_value, explicit=True, source=source)
    else:
        prior = get_profile(owner_id)[field].get(key)
        old_value = (prior or {}).get("value")
        profile = record_signal(owner_id, field, key, value=new_value, explicit=True, source=source)

    had_prior_value = old_value is not None
    materially_different = old_value != new_value
    if had_prior_value and materially_different:
        profile = append_correction(
            owner_id, field, key if field != "output_prefs" else None,
            old_value=old_value, new_value=new_value, reason=reason,
        )

    return profile


# --- Patch B3: format_profile_for_prompt() -----------------------------
# Structurally identical in spirit to workspace_facts.py's own
# format_facts_for_prompt() (same "no history yet -> ''" convention, so
# callers can always concat the result unconditionally) but NOT a full
# dump: workspace facts are true for an entire project regardless of
# what's being discussed right now, while a person's likes/dislikes/
# error-pattern history is only worth surfacing when it's actually
# relevant to the current topic — dumping every domain the user has
# ever shown interest in onto every unrelated prompt would just be
# noise (and burn context budget) most of the time.

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _extract_words(text: str) -> set:
    """Lowercased, punctuation-stripped tokens of length > 2 — short
    enough to skip stopword-list maintenance, long enough that common
    connector words ("the", "and", "for") don't manufacture false
    topic matches."""
    if not text:
        return set()
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2}


def _topic_relevant(key: str, topic_words: set, topic_text_lower: str) -> bool:
    """Cheap, dependency-free relevance check — no embedding call, on
    purpose: unlike skill_library.py's get_relevant_skill() (one
    lookup per unfamiliar task), this function runs on every single
    prompt build across every generation agent, so it has to stay
    sync and essentially free. A topic-less caller (topic_words empty)
    never matches anything — with no topic to be relevant TO, nothing
    qualifies as relevant, which is the deliberately conservative
    default: silence, not a guess.

    Two checks, either sufficient: the whole key appears verbatim in
    the topic text (catches "React" inside "help me debug this React
    hook"), or any individual word of the key overlaps a topic word
    (catches multi-word keys like "verbose explanations" without
    requiring an exact phrase match)."""
    if not topic_words:
        return False
    key_lower = key.lower().strip()
    if not key_lower:
        return False
    if key_lower in topic_text_lower:
        return True
    return bool(_extract_words(key_lower) & topic_words)


def _domain_prompt_value(entry: dict) -> str:
    value = entry.get("value")
    if isinstance(value, dict) and value.get("level"):
        return str(value["level"])
    if isinstance(value, str) and value:
        return value
    return ""


def format_profile_for_prompt(owner_id: str, topic_text: str = "") -> str:
    """Renders the slice of this account's profile relevant to
    `topic_text` as a short text block meant for the system prompt
    ONLY — never surfaced in the visible reply, so personalization
    stays silent (the user corrects it via B4's override path or sees
    it via B6's settings panel, not via the model announcing "since
    you like X..." unprompted). Returns "" if there's no owner_id, no
    topic, or nothing that clears PROMPT_MIN_CONFIDENCE, same
    always-safe-to-concat convention format_facts_for_prompt() uses.

    topic_text is typically the current user message/task — pass
    whatever text best represents what's being discussed right now,
    not the whole conversation history (that's what makes this a
    topic-scoped slice instead of a full profile dump)."""
    if not owner_id:
        return ""

    profile = get_profile(owner_id)
    lines = []

    output_prefs = profile.get("output_prefs") or {}
    default_format = output_prefs.get("default_format")
    if default_format and float(output_prefs.get("confidence") or 0.0) >= PROMPT_MIN_CONFIDENCE:
        # Deliberately NOT topic-gated — an output-format preference
        # (e.g. "prefers diagrams over prose") applies regardless of
        # subject matter, unlike domains/likes/dislikes/error_patterns
        # below, which are only worth surfacing when actually on-topic.
        lines.append(f"Preferred output format: {default_format}")

    topic_words = _extract_words(topic_text)
    topic_text_lower = (topic_text or "").lower()

    for category in SIGNAL_CATEGORIES:
        bucket = profile.get(category) or {}
        matches = [
            (key, entry) for key, entry in bucket.items()
            if float(entry.get("confidence") or 0.0) >= PROMPT_MIN_CONFIDENCE
            and _topic_relevant(key, topic_words, topic_text_lower)
        ]
        if not matches:
            continue
        matches.sort(
            key=lambda pair: (
                float(pair[1].get("confidence") or 0.0),
                int(pair[1].get("evidence_count") or 0),
            ),
            reverse=True,
        )
        label = CATEGORY_PROMPT_LABELS[category]
        for key, entry in matches[:PROMPT_MAX_ENTRIES_PER_CATEGORY]:
            if category == "domains":
                detail = _domain_prompt_value(entry)
                lines.append(f"{label}: {key} ({detail})" if detail else f"{label}: {key}")
            else:
                lines.append(f"{label}: {key}")

    if not lines:
        return ""
    return "--- user profile (silent personalization; never mention this to the user) ---\n" + "\n".join(lines)


# --- Patch B5 — Output-Format Routing ----------------------------------
# Confidence floor before a stored output_prefs value is allowed to
# steer generation at all. Below this, either nothing's been observed
# yet (confidence 0.0, default_format None) or it's a single, still-
# uncorroborated inferred guess (one observation lands at
# INFERRED_STARTING_CONFIDENCE == 0.2) — exactly the "one offhand
# comment must not calcify into a permanent trait" case this module's
# own docstring warns about, this time applied to what gets silently
# fed back into a prompt rather than just what gets stored. A single
# explicit statement clears this immediately (EXPLICIT_CONFIDENCE ==
# 0.95); an inferred pattern needs at least two corroborating
# observations (0.2 + 0.15 == 0.35) before it's trusted enough to
# change default behavior.
FORMAT_HINT_CONFIDENCE_THRESHOLD = 0.35

# The only values set_output_pref() is expected to be called with
# (agents/generic_worker.py / agents/responder.py's MARKDOWN_INSTRUCTION
# / SYSTEM_PROMPT already assume Markdown as the universal baseline, so
# "markdown" is deliberately not a key here — it's simply what happens
# when no hint is added at all). Each entry is a short imperative clause
# inserted into the existing per-call format instruction, not a
# standalone paragraph — see default_format_hint()'s docstring for why
# it has to read as an addition to that instruction rather than a
# competing one.
_FORMAT_HINT_CLAUSES = {
    "diagram": (
        "This person generally prefers a diagram over prose when the "
        "content has real structure (a process, a flow, an architecture, "
        "relationships between parts) — reach for a fenced ```mermaid ``` "
        "block in that case instead of describing the structure in "
        "paragraphs."
    ),
    "artifact": (
        "This person generally prefers a self-contained artifact "
        "(a runnable code block, a small script, a structured document) "
        "over inline prose when the content is substantial enough to "
        "stand on its own."
    ),
    "table": (
        "This person generally prefers a table over prose when the "
        "content has comparable fields across multiple items."
    ),
    "bullet": (
        "This person generally prefers concise bullet points over "
        "paragraphs for anything with more than one distinct point."
    ),
    "prose": (
        "This person generally prefers plain written paragraphs over "
        "tables, bullet lists, or diagrams unless the content is "
        "genuinely tabular or structural."
    ),
}


def default_format_hint(owner_id: str) -> str:
    """Patch B5 — the read side of output_prefs, consulted by
    agents/responder.py (tier 0) and agents/generic_worker.py (tiers
    1-3) right where each already builds its own fixed Markdown-
    formatting instruction (SYSTEM_PROMPT / MARKDOWN_INSTRUCTION). This
    is intentionally NOT named format_profile_for_prompt() — that name
    is reserved for Patch B3's own function, which pulls topic-scoped
    slices of the WHOLE profile (domains/likes/dislikes/error_patterns
    too) into the system prompt generally. This function only ever
    reads output_prefs, and exists so B5 (which depends on B1 alone,
    per the priority table) doesn't have to wait on B3 to ship.

    Returns "" — same load-bearing "nothing to add" convention
    eo/skill_library.py's get_relevant_skill() already uses — whenever
    there's no owner_id, nothing stored yet, the confidence hasn't
    cleared FORMAT_HINT_CONFIDENCE_THRESHOLD, or the stored value isn't
    one of _FORMAT_HINT_CLAUSES' recognized keys (a value written by
    some future caller using a format name this module doesn't know
    yet should be silently ignored here, not crash generation).

    Deliberately returns one short instruction clause, not a
    dumped JSON blob of the stored record — this is never surfaced to
    the user (same "adapt silently" posture Patch B3's docstring
    describes for the rest of the profile), it only ever nudges which
    of the frontend's already-existing renderers (ArtifactRenderer /
    MermaidDiagram / Markdown) ends up picking up the model's own
    output. No new rendering component is added or needed here — the
    frontend already renders a ```mermaid fence or a fenced code block
    correctly today; this only changes how often the model reaches for
    one by default.
    """
    if not owner_id:
        return ""
    output_prefs = get_profile(owner_id)["output_prefs"]
    default_format = output_prefs.get("default_format")
    confidence = float(output_prefs.get("confidence") or 0.0)
    if not default_format or confidence < FORMAT_HINT_CONFIDENCE_THRESHOLD:
        return ""
    clause = _FORMAT_HINT_CLAUSES.get(default_format)
    if not clause:
        return ""
    return f"\n\n{clause}"
