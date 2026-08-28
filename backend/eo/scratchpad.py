"""
eo/scratchpad.py — Patch B7 (CLI-as-Internal-Interface plan, §3.5):
per-task, ephemeral free-form working notes for an agent mid-run.

Explicitly NOT merged with eo/audit_log.py: the audit log is an
append-only, permanent record of config-affecting changes (redaction
entries today — see redaction_guard.py's B2 write path); this module is
the opposite on every axis that matters — mutable, agent-deletable, and
guaranteed to be wiped by the end of the task that created it. A future
session should not "helpfully" consolidate these into one store.

Storage: one memory.bus list per session_id, namespaced the same way
paused_execution:{session_id} already is (see eo/loop_controller.py's
run_with_looping() for that established pattern) — key
"scratchpad:{session_id}". memory/bus.py's _namespaced() has been
extended (this same patch) to exempt "scratchpad:" from app_slug
prefixing for the identical reason paused_execution:/conversation:/
workspace_facts: are already exempt there: a task's own working notes
are a property of the SESSION, not whatever app_slug happens to be
active in whichever call context (agent dispatch, a routes/tasks.py
request, a resume) is touching the scratchpad at that moment. Without
that exemption, a write from one app_slug context and a read from
another would silently land on two different Redis keys — see
_namespaced()'s own docstring for the exact failure shape this class of
bug takes.

Cleanup has two layers, per the plan:
  1. Primary: the agent that wrote a note deletes it itself once it's
     no longer needed (resolve_note()).
  2. Safety net: a max-note-count cap here, in case an agent (or a run
     that errors out mid-way) never calls resolve_note() for some notes.
  3. Backstop: clear_scratchpad() wipes the whole key. Wired into
     eo/loop_controller.py's run_with_looping() at the point it returns
     its finished (non-paused) result — see that function for the call
     site. NOT wired into eo/executor.py's resume_graph(), which
     duplicates run_with_looping()'s own finished-return tail inline
     for the resumed-macro-loop case rather than calling back into
     loop_controller.py; a scratchpad from a run that pauses and later
     resumes to completion is not cleared by this patch. Flagged in
     DEFERRED.md rather than fixed here — resume_graph()'s duplicated
     tail is a pre-existing structural gap, not something this patch's
     scope (add the scratchpad mechanism) should be reaching into
     executor.py to restructure.
"""
import uuid
from datetime import datetime, timezone

from memory.bus import delete, read, write

# Safety-net cap — a named constant so it's easy to tune later, same
# reasoning as eo/tool_budget.py's DEFAULT_TOOL_CALL_BUDGET. Notes
# beyond this count are dropped oldest-first on write(), independent of
# whether any of them have been resolved yet.
MAX_SCRATCHPAD_NOTES = 20


def _key(session_id: str) -> str:
    return f"scratchpad:{session_id}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_note(session_id: str, text: str) -> str:
    """Appends a free-form note to this session's scratchpad and returns
    its note_id, for a later resolve_note() call by whichever agent
    wrote it (or anyone else working the same session_id — there's no
    per-writer ownership tracked here, same trust model as the rest of
    an in-process task run).

    Applies the safety-net cap on every write: once MAX_SCRATCHPAD_NOTES
    is exceeded, the oldest note(s) are dropped first, regardless of
    resolved status — this is the fallback for notes nobody ever
    resolves, not a substitute for resolve_note().
    """
    notes = read(_key(session_id), default=[]) or []
    note_id = uuid.uuid4().hex[:8]
    notes.append({"id": note_id, "text": text, "created_at": _utcnow_iso()})
    if len(notes) > MAX_SCRATCHPAD_NOTES:
        notes = notes[-MAX_SCRATCHPAD_NOTES:]
    write(_key(session_id), notes)
    return note_id


def resolve_note(session_id: str, note_id: str) -> bool:
    """Deletes a single note by id — the primary cleanup path (§3.5):
    an agent resolves its own note once it's no longer needed, rather
    than waiting for the safety-net cap or the end-of-task wipe. Returns
    True if a note with that id existed and was removed, False
    otherwise (unknown id, already resolved, or unknown session)."""
    notes = read(_key(session_id), default=[]) or []
    remaining = [n for n in notes if n.get("id") != note_id]
    if len(remaining) == len(notes):
        return False
    write(_key(session_id), remaining)
    return True


def list_notes(session_id: str) -> list:
    """Returns this session's current notes, oldest first, as-stored.
    Read-only — does not mutate or trim anything (mirrors
    eo/tool_budget.py's over_threshold()/read split: reads never have
    write side effects)."""
    return read(_key(session_id), default=[]) or []


def clear_scratchpad(session_id: str) -> None:
    """Wipes this session's scratchpad entirely. Called on task
    completion (see eo/loop_controller.py's run_with_looping()) — the
    backstop that guarantees no scratchpad key survives past its task,
    even for notes an agent never explicitly resolved. Uses delete()
    rather than write(session_id, []), which would leave an empty-but-
    present namespaced key sitting in Redis forever instead of actually
    removing it — same distinction memory/bus.py's own delete()
    docstring draws, and the same call eo/chat_store.py's delete_chat()
    makes for the same reason."""
    delete(_key(session_id))
