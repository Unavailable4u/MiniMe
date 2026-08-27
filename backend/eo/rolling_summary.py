"""
eo/rolling_summary.py — Patch B9, Tier B: a rolling summary sitting
between the two memory tiers that already existed:

  - Tier A (eo/conversation_memory.py): the last MAX_STORED_TURNS turns,
    full detail, kept exactly as-is.
  - Tier C (eo/workspace_facts.py): durable, structured, hand-confirmed
    facts — narrow, precise, never fuzzy.

Before this patch, anything older than MAX_STORED_TURNS simply fell off
the end of conversation_memory's stored list (see append_turn()'s
`turns[-MAX_STORED_TURNS:]` trim) and was gone for good. That's fine for
a short chat, but a long-running session loses everything before its
most recent ~20 turns, even material a later turn plainly depends on
("like we discussed earlier...").

This module doesn't change what gets stored in Tier A. It hooks the
*moment a turn is about to be trimmed off* (conversation_memory.py's
append_turn(), see the call into fold_turns_async() there) and folds
that turn's text into a running, LLM-maintained summary instead of
letting it vanish. Older material progressively narrows (summary-of-
summary); the most recent MAX_STORED_TURNS stay in full detail in Tier
A untouched.

Storage: memory/bus.py, under "conversation:{session_id}:summary" — the
same "conversation:" prefix conversation_memory.py's own module
docstring documents as session-namespaced (see memory/bus.py's
_namespaced() exemption list), so this key is exempt from app_slug
namespacing for the identical reason Tier A's own key is.

--- Patch B10 — anti-drift re-grounding -------------------------------

Repeatedly summarizing a summary can compound small errors: each fold()
above only ever sees the *previous summary text*, not the original
turns, so a subtle misreading two folds ago can silently persist and
sharpen with every further edit. This patch adds two independent
mitigations on top of the fold mechanism above, without changing that
mechanism's storage shape or its callers' contract:

  1. Re-grounding. Alongside the summary itself, this module now keeps a
     small rolling buffer of the RAW excerpts that fed the last few
     folds (`conversation:{session_id}:summary_raw_buffer`, capped to
     REGROUND_EVERY_FOLDS entries — exactly as much raw material as a
     re-ground pass below actually uses). Every REGROUND_EVERY_FOLDS-th
     fold, instead of asking the model for one more incremental edit on
     top of the existing summary (summary-of-summary-of-summary
     chaining), it's asked to rewrite the summary from scratch using
     that raw buffer as ground truth — carrying forward older material
     from the existing summary that the raw buffer doesn't cover, but
     correcting anything the raw turns show was misremembered. This
     bounds how many consecutive "trust the last summary" edits can
     happen before the next fold checks back against real transcript
     text.

  2. Fact routing. Before either kind of fold runs, each turn being
     folded is passed through the same "is this a durable fact"
     judgment eo/fact_summarizer.py already makes for completed tier-2/3
     tasks (see api/task_runner.py's _maybe_extract_content_fact()).
     Anything that reads as a decision, a stated preference, an idea, or
     standing project context is written straight into
     eo/workspace_facts.py's structured store (Tier C) instead of
     depending on the fuzzy summary to carry it forward accurately —
     Tier C entries are stored once, keyed, and never silently reworded
     by a later summarization pass the way prose in Tier B can be. This
     is deliberately best-effort and additive, not subtractive: the fold
     into Tier B still happens either way (some of the same material may
     end up captured in both places), since reliably stripping just the
     "factual part" back out of a raw turn's prose is its own hard
     problem and not one this patch takes on — Tier C simply becomes the
     non-degrading copy of record for anything it manages to catch.

Both mitigations are wrapped fail-open (matching every other side-effect
in this file's neighbors, e.g. eo/workspace_facts.py's own
_invalidate_facts_cache()): a routing or re-ground failure never blocks
the plain incremental fold from happening.
"""
import os
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo import chat_workspace, fact_summarizer, workspace_facts  # NEW — Patch B10
from memory.bus import read, write

MAX_SUMMARY_CHARS = 2000   # hard cap on the maintained summary's size

# Patch B10 — how often (in folds) to re-ground against raw turns instead
# of chaining another incremental edit onto the existing summary text.
REGROUND_EVERY_FOLDS = 6

# The raw buffer only needs to hold enough material for the NEXT
# re-ground pass, not the session's entire history — older raw text than
# that is exactly what re-grounding itself is meant to have already
# folded into the summary by the time it ages out of this buffer.
RAW_BUFFER_MAX_ENTRIES = REGROUND_EVERY_FOLDS


def _key(session_id: str) -> str:
    return f"conversation:{session_id}:summary"


def _raw_buffer_key(session_id: str) -> str:
    return f"conversation:{session_id}:summary_raw_buffer"


def _fold_count_key(session_id: str) -> str:
    return f"conversation:{session_id}:summary_fold_count"


def get_summary(session_id: str) -> str:
    """Returns the current rolling summary for this session, or "" if
    nothing has been folded into it yet — same no-history-yet
    convention every other read in this system uses, so callers never
    need a None-check before concatenating this into a prompt."""
    if not session_id:
        return ""
    return read(_key(session_id), default="")


def _turns_to_excerpt(turns: list) -> str:
    """Formats one or more {"role", "text"} turn dicts the same
    [role]: text shape conversation_memory.get_full_context() already
    uses, so the summarizer role sees a familiar transcript excerpt
    rather than a different ad-hoc format."""
    return "\n\n".join(f"[{t['role']}]: {t['text']}" for t in turns)


def _route_durable_facts(session_id: str, owner_id: str | None, turns: list) -> None:
    """Patch B10, mitigation 2 — see module docstring. Best-effort and
    fail-open: called from fold_turns() before either kind of fold
    below, but a failure here (no workspace, classifier error, storage
    error) must never block the fold itself, so every failure mode is
    caught and logged rather than raised.

    owner_id is required to resolve session_id -> workspace_id (same
    ownership-scoped lookup eo/conversation_memory.py's own
    _workspace_facts_text() uses) — without it there's no ownership
    context to check, so this is a silent no-op, same convention as
    that function's own missing-owner_id branch. Most existing callers
    of fold_turns()/fold_turns_async() predate B10 and don't pass
    owner_id yet; this simply means fact-routing doesn't fire for them,
    not that the fold breaks."""
    if not session_id or not owner_id:
        return
    try:
        workspace = chat_workspace.workspace_for_chat(session_id, owner_id)
        if not workspace:
            return

        user_text = "\n".join(t["text"] for t in turns if t.get("role") == "user")
        assistant_text = "\n".join(t["text"] for t in turns if t.get("role") == "assistant")
        if not user_text and not assistant_text:
            return

        fact = fact_summarizer.extract_fact(user_text, assistant_text, session_id=session_id)
        if not fact:
            return
        section = workspace_facts.CATEGORY_TO_SECTION.get(fact["category"])
        if not section:
            return  # unreachable in practice — extract_fact() already validates category

        workspace_facts.record_section_entry(
            workspace["id"],
            section,
            {
                "title": fact["title"],
                "summary": fact["summary"],
                "data": {"category": fact["category"], "source": "rolling_summary_fold"},
            },
            source="rolling_summary_fold",   # distinct from "chat_summarizer" (Part 3) and "chat_task_runner" (D1)
            source_ref=session_id,
            event="upsert",
        )
    except Exception as exc:
        print(f"  [Rolling Summary] durable-fact routing failed, skipped: {exc}")


def _build_incremental_task_text(existing: str, excerpt: str) -> str:
    return (
        "Update the EXISTING SUMMARY below so it also reflects the NEW "
        f"TURN(S), staying under {MAX_SUMMARY_CHARS} characters. Output "
        "only the updated summary text — no preamble, no headers, no "
        "commentary about what changed.\n\n"
        f"EXISTING SUMMARY:\n{existing or '(none yet)'}\n\n"
        f"NEW TURN(S):\n{excerpt}"
    )


def _build_reground_task_text(existing: str, raw_buffer: list) -> str:
    """Patch B10, mitigation 1 — see module docstring. Unlike the
    incremental prompt above, this one asks for a full rewrite grounded
    in raw_buffer (the actual transcript excerpts behind the last few
    folds), with the existing summary supplied only as background for
    older material the raw buffer no longer covers — not as the primary
    thing being edited."""
    raw_text = "\n\n---\n\n".join(raw_buffer)
    return (
        "The EXISTING SUMMARY below has been through several rounds of "
        "incremental updates and may have drifted from what actually "
        "happened. Rewrite it FROM SCRATCH using the RAW TURNS as ground "
        f"truth, staying under {MAX_SUMMARY_CHARS} characters. Preserve "
        "any older material from the existing summary that the raw turns "
        "don't cover or contradict; correct anything the raw turns show "
        "was misremembered or dropped in error. Output only the "
        "rewritten summary text — no preamble, no headers, no commentary "
        "about what changed.\n\n"
        f"EXISTING SUMMARY:\n{existing or '(none yet)'}\n\n"
        f"RAW TURNS:\n{raw_text}"
    )


def fold_turns(session_id: str, turns: list, owner_id: str | None = None) -> str | None:
    """Synchronous half: asks the 'rolling_summarizer' role
    (generic_worker, deferred-imported for the same circular-import
    reason agents/note_taker.py's own _propose_from_context() defers
    it — generic_worker imports eo.conversation_memory, and this module
    is called from conversation_memory.append_turn()) to fold `turns`
    (the turn(s) about to be trimmed off Tier A) into the existing
    summary, then stores and returns the updated text.

    No-op (returns None) on a falsy session_id or an empty turns list —
    fail-quiet, same convention conversation_memory.append_turn() and
    note_taker.py's own dispatch already use, so a summarizer failure
    or a call with nothing to fold never surfaces as a user-visible
    error for the chat turn that triggered it.

    Patch B10: before folding, `turns` is checked for durable-fact
    content and routed to Tier C (see _route_durable_facts()); every
    REGROUND_EVERY_FOLDS-th call re-grounds the summary against raw
    turns instead of chaining another incremental edit (see
    _build_reground_task_text()). owner_id is new and optional, only
    needed for the fact-routing step — omitting it degrades gracefully
    to B9's original incremental-only behavior.
    """
    if not session_id or not turns:
        return None

    _route_durable_facts(session_id, owner_id, turns)

    from agents.generic_worker import run as run_role  # deferred, see module docstring

    existing = get_summary(session_id)
    excerpt = _turns_to_excerpt(turns)

    raw_buffer = read(_raw_buffer_key(session_id), default=[])
    raw_buffer.append(excerpt)
    raw_buffer = raw_buffer[-RAW_BUFFER_MAX_ENTRIES:]
    write(_raw_buffer_key(session_id), raw_buffer)

    fold_count = read(_fold_count_key(session_id), default=0) + 1
    write(_fold_count_key(session_id), fold_count)

    reground = fold_count % REGROUND_EVERY_FOLDS == 0
    if reground:
        role = "rolling_summary_reground"
        task_text = _build_reground_task_text(existing, raw_buffer)
    else:
        role = "rolling_summarizer"
        task_text = _build_incremental_task_text(existing, excerpt)

    try:
        result = run_role(
            role=role,
            task_text=task_text,
            input_keys=[],
            session_id=session_id,
            # The task_text above already carries the only context this
            # role needs (the existing summary + the new turn(s), or the
            # existing summary + the raw buffer on a re-ground pass) —
            # generic_worker's own Part 23 conversation-context prepend
            # would just re-inject the same turns this call exists to
            # get AHEAD of, and would grow with every fold instead of
            # staying bounded.
            include_conversation_context=False,
            domain="memory",
        )
    except Exception as exc:
        print(f"  [Rolling Summary] fold failed, skipped: {exc}")
        return None

    updated = (result.get("text") or "").strip()
    if not updated:
        return None
    if len(updated) > MAX_SUMMARY_CHARS:
        updated = updated[:MAX_SUMMARY_CHARS].rstrip() + "..."

    write(_key(session_id), updated)
    return updated


def fold_turns_async(session_id: str, turns: list, owner_id: str | None = None) -> None:
    """Fire-and-forget wrapper — what conversation_memory.append_turn()
    actually calls at the moment a turn is about to be trimmed off Tier
    A. Kept separate from fold_turns() itself so a caller that WANTS to
    block on the result (e.g. a test) still can, same split
    agents/note_taker.py's note_from_latest_turn/_async pair already
    uses, and for the same reason: the summarizer LLM call must never
    add latency to the chat response that triggered it.

    owner_id: Patch B10 — threaded through to fold_turns() so its
    fact-routing step can resolve a workspace; optional, defaults to
    None (no fact-routing, matches B9's original behavior) for any
    existing call site that hasn't been updated to pass it yet."""
    if not session_id or not turns:
        return
    threading.Thread(
        target=fold_turns,
        args=(session_id, turns),
        kwargs={"owner_id": owner_id},
        daemon=True,
    ).start()
