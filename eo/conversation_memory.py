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
from memory.bus import read, write
from eo import chat_store   # NEW — cross-chat memory sharing (see §4)
from eo import chat_workspace   # NEW — Part 0 §0.3, session_id -> workspace_id
from eo import workspace_facts  # NEW — Part 0 §0.3, tier-3 memory

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

    facts = _workspace_facts_text(session_id, owner_id)   # FIXED — now passes owner_id
    if facts and body:
        return facts + "\n\n" + body
    return facts or body


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
    if facts and body:
        return facts + "\n" + body
    return facts or body