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

Anti-drift note (Patch B10, not this patch): repeatedly summarizing a
summary can compound small errors, and this module does not attempt to
fix that on its own — Patch B10 periodically re-grounds this summary
against raw stored turns, and reroutes durable-fact-shaped content into
Tier C instead of letting it decay in here. This patch only builds the
fold-on-drop mechanism itself.
"""
import os
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

MAX_SUMMARY_CHARS = 2000   # hard cap on the maintained summary's size


def _key(session_id: str) -> str:
    return f"conversation:{session_id}:summary"


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


def fold_turns(session_id: str, turns: list) -> str | None:
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
    """
    if not session_id or not turns:
        return None

    from agents.generic_worker import run as run_role  # deferred, see module docstring

    existing = get_summary(session_id)
    excerpt = _turns_to_excerpt(turns)
    task_text = (
        "Update the EXISTING SUMMARY below so it also reflects the NEW "
        f"TURN(S), staying under {MAX_SUMMARY_CHARS} characters. Output "
        "only the updated summary text — no preamble, no headers, no "
        "commentary about what changed.\n\n"
        f"EXISTING SUMMARY:\n{existing or '(none yet)'}\n\n"
        f"NEW TURN(S):\n{excerpt}"
    )
    try:
        result = run_role(
            role="rolling_summarizer",
            task_text=task_text,
            input_keys=[],
            session_id=session_id,
            # The task_text above already carries the only context this
            # role needs (the existing summary + the new turn(s)) —
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


def fold_turns_async(session_id: str, turns: list) -> None:
    """Fire-and-forget wrapper — what conversation_memory.append_turn()
    actually calls at the moment a turn is about to be trimmed off Tier
    A. Kept separate from fold_turns() itself so a caller that WANTS to
    block on the result (e.g. a test) still can, same split
    agents/note_taker.py's note_from_latest_turn/_async pair already
    uses, and for the same reason: the summarizer LLM call must never
    add latency to the chat response that triggered it."""
    if not session_id or not turns:
        return
    threading.Thread(
        target=fold_turns,
        args=(session_id, turns),
        daemon=True,
    ).start()
