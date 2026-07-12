"""
agents/note_taker.py — Notes domain: Part 4 §4.6, the silent note-taking
agent.

Same accept/reject discipline eo/workspace_facts.py's own docstring
promises ("same accept/reject shape as the Notes domain's silent
note-taking agent"): this module never writes a note directly into the
knowledge graph. It only ever calls eo/note_candidates.py's
propose_note() — a human has to accept a candidate (via the API surface
that module backs) before it becomes a real write_node() call. "Silent"
describes HOW it decides to propose (in the background, without being
asked), not that it skips the human in the loop.

Runs through generic_worker like every other reasoning role (Part 10) —
"note_taker" is just another entry in eo/registry.py's ROLE_PROMPTS_SEED,
briefed to answer either the literal word NONE, or a single fenced
```json block ({"title", "content", "tags"}) — same fenced-json-output
precedent eo/registry.py's "marketplace_review_batch" role already
establishes, so no new response-format plumbing is needed in
generic_worker.py itself.

Two call shapes, matching the two triggers Part 4 §4.6 asks for:
  - note_from_latest_turn() / note_from_latest_turn_async() — cheap,
    scoped to just the exchange that just happened. The async wrapper is
    fired automatically, in a background thread, from
    eo/conversation_memory.py's append_turn() every time an assistant
    turn lands (see that module) — deliberately NOT awaited/blocking, so
    the note-taker never adds latency to a chat response.
  - scan_conversation() — broader, reads the same recent-turns window
    every other generation role sees (get_full_context()), for an
    explicit "scan for notes" user action that wants to catch something
    the lightweight per-turn pass might have judged not (yet) worth a
    note in isolation. Blocking — the user pressed a button and is
    waiting on a result, unlike the silent background pass above.

Both funnel through _propose_from_context(), the shared core.

Place this file at: agents/note_taker.py
"""
import os
import sys
import json
import re
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo import chat_workspace
from eo import note_candidates

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _resolve_workspace(session_id: str):
    """session_id/chat_id are the same string everywhere in this system
    (api/server.py's own comment) — same workspace lookup
    eo/conversation_memory.py's _workspace_facts_text() already does.
    Returns None if this chat isn't in any workspace, same "nothing to
    do" convention that function uses — a note has nowhere to file
    without a workspace_id."""
    if not session_id:
        return None
    ws = chat_workspace.workspace_for_chat(session_id)
    return ws["id"] if ws else None


def _propose_from_context(session_id: str, workspace_id: str, context_text: str) -> dict | None:
    """The shared core both entry points below call: asks the
    'note_taker' role (generic_worker, deferred-imported for the same
    circular-import reason eo.panel is deferred inside
    agents/generic_worker.py's own run() — generic_worker imports
    eo.conversation_memory, and conversation_memory's append_turn()
    deferred-imports THIS module, so importing generic_worker at this
    module's top level would close that loop) whether `context_text`
    contains anything worth a note, and if so, proposes it via
    eo/note_candidates.py's propose_note().

    Returns the created candidate dict, or None if the model judged
    nothing note-worthy (or the answer couldn't be parsed — treated the
    same as NONE: silence, not a crash, since a malformed note-taker
    response must never surface as a user-visible error).
    """
    if not workspace_id or not context_text:
        return None

    from agents.generic_worker import run as run_role   # deferred, see above

    task_text = (
        "Decide whether the conversation excerpt below contains anything "
        "worth saving as a permanent note.\n\n" + context_text
    )
    result = run_role(
        role="note_taker",
        task_text=task_text,
        input_keys=[],
        session_id=session_id,
        # context_text above already IS the conversation context —
        # generic_worker's own Part 23 prepend would just duplicate it.
        include_conversation_context=False,
        domain="notes",
    )
    raw = (result.get("text") or "").strip()
    if raw.upper() == "NONE":
        return None

    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    title = (parsed.get("title") or "").strip()
    content = (parsed.get("content") or "").strip()
    if not title or not content:
        return None
    tags = parsed.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    return note_candidates.propose_note(
        workspace_id=workspace_id,
        title=title,
        content=content,
        tags=tags,
        proposed_by="note_taker",
    )


def note_from_latest_turn(session_id: str, user_text: str, assistant_text: str):
    """The automatic, per-turn trigger's synchronous half — scoped to
    just this one exchange (not the full recent-turns window
    scan_conversation() reads), the natural cheap unit of work for
    "does what JUST happened contain something worth keeping." Swallows
    any exception rather than propagating it: a note-taker failure must
    never surface as a user-visible error for the chat turn that
    triggered it. Returns the candidate (or None) mainly so tests / a
    synchronous caller can inspect the result — the actual background
    trigger uses the _async wrapper below and ignores this return value.
    """
    workspace_id = _resolve_workspace(session_id)
    if not workspace_id:
        return None
    excerpt = f"[user]: {user_text}\n\n[assistant]: {assistant_text}"
    try:
        return _propose_from_context(session_id, workspace_id, excerpt)
    except Exception as exc:
        print(f"  [Note Taker] background pass failed, skipped: {exc}")
        return None


def note_from_latest_turn_async(session_id: str, user_text: str, assistant_text: str) -> None:
    """Fire-and-forget wrapper — what eo/conversation_memory.py's
    append_turn() actually calls. Kept separate from
    note_from_latest_turn() itself so a caller that WANTS to block on
    the result (e.g. a test, or a future synchronous caller) still can."""
    threading.Thread(
        target=note_from_latest_turn,
        args=(session_id, user_text, assistant_text),
        daemon=True,
    ).start()


def scan_conversation(session_id: str) -> dict:
    """The explicit, on-demand trigger — an API endpoint calls this
    directly. Reads the same recent-turns window every content-
    generating role sees (eo/conversation_memory.py's get_full_context())
    rather than just the latest exchange, so it can catch something
    worth a note that only makes sense in light of several turns, not
    just the most recent one.
    """
    from eo import conversation_memory   # deferred for consistency with
    # this module's other cross-package imports, though not actually
    # cyclic in this direction (conversation_memory only imports THIS
    # module's functions, not the reverse, at its own deferred call site)
    workspace_id = _resolve_workspace(session_id)
    if not workspace_id:
        return None
    context_text = conversation_memory.get_full_context(session_id)
    return _propose_from_context(session_id, workspace_id, context_text)