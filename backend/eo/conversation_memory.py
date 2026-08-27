"""
eo/conversation_memory.py — Part 23. A shared, per-session conversation
transcript, so a follow-up message ("make it shorter", "now add auth too")
has real prior context to work from instead of being treated as the very
first message ever sent.

Two read modes, deliberately different in size/detail:
  - get_full_context(): real prior turns, fuller detail — for the agents
    that actually generate content and need to build on what came before.
  - get_light_context(): compact one-line-per-turn summaries — for the
    Inspector/Panel, so a follow-up's tier/complexity can be re-judged
    against what's already been asked/built, without flooding the
    classifier's prompt with full prior answers or corrupting exact-match
    caching (eo/semantic_cache.py) with a growing wall of unrelated text.

Storage: memory/bus.py, under "conversation:{session_id}" — session-
namespaced, not app_slug-namespaced (see memory/bus.py's _namespaced()
exemption list, extended in this same part), since a single session isn't
reliably tied to one app_slug across its lifetime.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo import (
    chat_store,  # NEW — cross-chat memory sharing (see §4)
    chat_workspace,  # NEW — Part 0 §0.3, session_id -> workspace_id
    rolling_summary,  # NEW — Patch B9, Tier B: folds trimmed turns instead of dropping them
    user_profile,  # NEW — Patch B3, silent per-account personalization
    workspace_facts,  # NEW — Part 0 §0.3, tier-3 memory
)
from memory.bus import read, write

MAX_STORED_TURNS = 20      # hard cap on raw storage growth per session
FULL_CONTEXT_TURNS = 6     # how many recent turns generation agents see
LIGHT_CONTEXT_TURNS = 6    # how many recent turns the classifier sees
FULL_TURN_CHAR_LIMIT = 1500    # per-turn truncation for the full view
LIGHT_TURN_CHAR_LIMIT = 120     # per-turn truncation for the light view


def _key(session_id: str) -> str:
    return f"conversation:{session_id}"


def _workspace_facts_text(session_id: str, owner_id: str = None) -> str:
    """NEW — Part 0 §0.3. session_id and chat_id are the same string
    everywhere in this system (api/server.py's own comment), so a
    session's workspace is just "whichever workspace this chat_id is a
    member of" — eo/chat_workspace.py's workspace_for_chat(). A session
    with no workspace (most ad-hoc chats) simply gets "", same
    no-history-yet convention every other lookup in this module already
    uses, so this is always safe to prepend unconditionally.

    owner_id: FIXED — workspace_for_chat() is now owner_id-scoped, same
    migration as chat_store.py's get_chat()/chat_exists(). Without an
    owner_id we have no ownership context to check, so — same
    fail-quiet convention as the chat_store.py linked-context lookup
    right above this function's call sites — skip the lookup and
    return "" rather than erroring."""
    if not session_id or not owner_id:
        return ""
    ws = chat_workspace.workspace_for_chat(session_id, owner_id)   # FIXED — now passes owner_id
    if not ws:
        return ""
    return workspace_facts.format_facts_for_prompt(ws["id"])


def _user_profile_text(turns: list, owner_id: str = None) -> str:
    """NEW — Patch B3. Silent per-account personalization, structural
    sibling of _workspace_facts_text() right above: workspace_facts is
    scoped to a workspace_id, this is scoped to an owner_id, both feed
    into the same prepend below.

    Topic text is the most recent user turn already sitting in
    `turns` — every existing caller of run_task()/preview_task()
    (api/task_runner.py) calls conversation_memory.append_turn(...,
    "user", task_text) BEFORE dispatch ever reaches this module's
    get_full_context()/get_light_context(), so the current message is
    already the latest "user"-role entry by the time either function
    runs. That means format_profile_for_prompt()'s topic-relevance
    filtering (see that function's own docstring) works out of the box
    here with no new parameter threaded through the many call sites in
    agents/*.py and eo/loop_v4.py/sga.py — same reasoning
    _workspace_facts_text() already leans on for owner_id (fail-quiet
    to "" rather than requiring every caller to be touched)."""
    if not owner_id:
        return ""
    topic_text = next((t["text"] for t in reversed(turns) if t.get("role") == "user"), "")
    return user_profile.format_profile_for_prompt(owner_id, topic_text)


def append_turn(session_id: str, role: str, text: str, owner_id: str = None) -> None:
    """Appends one turn ({"role": "user"|"assistant", "text": ...}) to
    this session's transcript. No-op if session_id is falsy — same
    fail-quiet convention relay/emitter.py already uses for a missing
    session_id, so every existing call site that doesn't have one yet
    stays a harmless no-op instead of erroring."""
    if not session_id or not text:
        return
    turns = read(_key(session_id), default=[])
    turns.append({"role": role, "text": text})
    if len(turns) > MAX_STORED_TURNS:
        dropped = turns[:len(turns) - MAX_STORED_TURNS]
        # NEW — Patch B9 (Tier B): these turns are about to fall out of
        # Tier A's storage window for good. Fold them into the rolling
        # summary instead of just discarding them — fire-and-forget, so
        # the summarizer LLM call never adds latency to this turn.
        # owner_id — NEW, Patch B10: threaded through so the fold's
        # durable-fact routing step can resolve session_id -> workspace.
        rolling_summary.fold_turns_async(session_id, dropped, owner_id=owner_id)
        turns = turns[-MAX_STORED_TURNS:]
    write(_key(session_id), turns)
    if role == "assistant":
        try:
            from agents.note_taker import note_from_latest_turn_async
            user_text = next((t["text"] for t in reversed(turns[:-1]) if t["role"] == "user"), "")
            note_from_latest_turn_async(session_id, owner_id, user_text, text)   
        except Exception as exc:
            print(f"  [Conversation Memory] note-taker dispatch skipped: {exc}")


def get_full_context(session_id: str, owner_id: str = None, max_turns: int = FULL_CONTEXT_TURNS) -> str:
    """... (unchanged from previous fix) ..."""
    if not session_id:
        return ""
    turns = read(_key(session_id), default=[])
    recent = turns[-max_turns:]
    lines = []
    for t in recent:
        text = t["text"]
        if len(text) > FULL_TURN_CHAR_LIMIT:
            text = text[:FULL_TURN_CHAR_LIMIT] + "..."
        lines.append(f"[{t['role']}]: {text}")
    own = "\n\n".join(lines)

    linked = chat_store.get_linked_context_text(session_id, owner_id, max_turns_per_chat=6,
                                                 char_limit=400) if owner_id else ""
    body = linked + "\n\n--- current conversation ---\n\n" + own if (linked and own) else (linked or own)

    # NEW — Patch B9 (Tier B): older material that's already fallen out
    # of `own` above (see append_turn()'s trim) isn't just gone — surface
    # it here, ahead of the full-detail recent turns, so a generation
    # agent still has a (narrower) sense of what happened earlier in a
    # long-running session.
    summary = rolling_summary.get_summary(session_id)
    if summary:
        body = f"--- earlier in this conversation (summarized) ---\n\n{summary}\n\n{body}" if body else summary

    facts = _workspace_facts_text(session_id, owner_id)   # FIXED — now passes owner_id
    profile = _user_profile_text(turns, owner_id)   # NEW — Patch B3
    memory_blocks = "\n\n".join(block for block in (facts, profile) if block)
    if memory_blocks and body:
        return memory_blocks + "\n\n" + body
    return memory_blocks or body


def get_light_context(session_id: str, owner_id: str = None, max_turns: int = LIGHT_CONTEXT_TURNS) -> str:
    """... (unchanged from previous fix) ..."""
    if not session_id:
        return ""
    turns = read(_key(session_id), default=[])
    recent = turns[-max_turns:]
    lines = []
    for t in recent:
        text = t["text"].strip().replace("\n", " ")
        if len(text) > LIGHT_TURN_CHAR_LIMIT:
            text = text[:LIGHT_TURN_CHAR_LIMIT] + "..."
        lines.append(f"- {t['role']}: {text}")
    own = "\n".join(lines)

    linked = chat_store.get_linked_context_text(session_id, owner_id, max_turns_per_chat=3,
                                                 char_limit=150) if owner_id else ""
    body = linked + "\n--- current conversation ---\n" + own if (linked and own) else (linked or own)

    facts = _workspace_facts_text(session_id, owner_id)   # FIXED — now passes owner_id
    profile = _user_profile_text(turns, owner_id)   # NEW — Patch B3
    memory_blocks = "\n".join(block for block in (facts, profile) if block)
    if memory_blocks and body:
        return memory_blocks + "\n" + body
    return memory_blocks or body