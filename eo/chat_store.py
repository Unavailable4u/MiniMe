"""
eo/chat_store.py — Postgres-backed, per-owner storage for the UI.

This is deliberately separate from eo/conversation_memory.py:
  - conversation_memory.py / memory/bus.py (Redis) is the AGENTS' short-term
    working memory — capped at MAX_STORED_TURNS, keyed by session_id, used
    to give follow-up prompts recent context.
  - chat_store.py (this file) is the UI's durable, uncapped record of a
    chat: every message, every Working Panel snapshot, chat title, and
    which other chats it's linked to for cross-chat memory sharing. It's
    what gets listed in the sidebar and reloaded after a refresh.

chat_id and session_id are the SAME string everywhere in this system — the
sidebar creates a chat_id, and that value is passed to the existing
session_id parameter on /api/task, eo/executor.py's Pusher channel, and
eo/conversation_memory.py unchanged.

--- Part 8.2 migration notes -----------------------------------------------
Migrated from one global data/chats/ file tree to Postgres (see
part8_schema.sql), scoped by owner_id. Every function's signature changed
by exactly one thing: it now takes owner_id, slotted in right after any
existing identifier argument (e.g. get_chat(chat_id) -> get_chat(chat_id,
owner_id)) — same convention used throughout this migration. Return
shapes are unchanged from before, so every existing caller needs exactly
one change: pass owner_id, threaded from the authenticated request.

Ownership is enforced by the query itself (WHERE ... AND owner_id = %s),
not as a separate check bolted on after a fetch — a chat that exists but
belongs to someone else looks identical to a chat that doesn't exist at
all (raises FileNotFoundError either way), which is the correct behavior
for both cases: nothing about this API should ever confirm a chat_id
belongs to a specific other user.

Row-level security is enabled on the underlying tables but has no
policies yet (see part8_schema.sql) — until Part 8.3 adds those, this
module's own WHERE-clause scoping IS the access control. Do not remove
the owner_id filter from any query on the assumption RLS has you covered.
-----------------------------------------------------------------------------
"""
import uuid
from datetime import datetime, timezone

from eo import db
from eo.audit_log import write_audit


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    """Postgres timestamptz columns come back as datetime objects via
    psycopg2; every existing caller of this module expects the old
    ISO-string shape, so we convert at this boundary rather than
    changing every call site."""
    return value.isoformat() if value is not None else None


def _clean_tags(tags: list | None) -> list:
    """Strip/dedupe/cap — unchanged from the file-based version."""
    if not tags:
        return []
    seen = []
    for t in tags:
        t = (t or "").strip()
        if t and t not in seen:
            seen.append(t)
    return seen[:25]


def new_chat_id() -> str:
    return f"chat_{uuid.uuid4().hex[:12]}"


def _row_to_chat(row: dict, include_messages: bool = True) -> dict:
    """RealDictCursor row -> the same dict shape callers have always
    gotten back from this module."""
    out = {
        "id": row["id"],
        "title": row["title"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "linked_chat_ids": row.get("linked_chat_ids") or [],
        "tags": row.get("tags") or [],
        "template_id": row.get("template_id"),
        # Part 8.4: needed by api/server.py to check the chat's workspace's
        # attribution-visibility setting before returning messages. Purely
        # additive — every existing caller ignores keys it doesn't use.
        "workspace_id": row.get("workspace_id"),
    }
    if include_messages:
        out["messages"] = row.get("messages") or []
    if "message_count" in row:
        out["message_count"] = row["message_count"]
    return out


_CHAT_COLUMNS = (
    "id, title, created_at, updated_at, linked_chat_ids, tags, template_id, "
    "messages, workspace_id"
)


def export_chats(owner_id: str, chat_ids: list) -> list[dict]:
    """Part 8.7: the portable-backup half of backup/restore. Deliberately
    filters to owner_id even if chat_ids contains ids belonging to other
    users (e.g. a workspace's full chat_ids list, which can include
    collaborators' chats) — a user's export must only ever contain THEIR
    OWN data, never a collaborator's, matching the guide's "scoped by
    owner_id" requirement exactly. This is a plain SQL filter, not a
    second permission check bolted on after the fact — same discipline
    every other function in this module already follows.

    Returns full chat dicts (including messages) — not the {title,
    sections} artifact shape from graph/adapters.py, since these are raw
    chats, not knowledge-graph nodes. A raw JSON export of the actual
    chat rows is the more faithful "portable backup of your data"
    instrument here; the artifact shape is for docx/pptx/etc. file
    export via agents/exporter.py, which is a separate, still-open
    piece of work (that module wasn't in scope this session)."""
    if not chat_ids:
        return []
    with db.cursor() as cur:
        cur.execute(
            f"select {_CHAT_COLUMNS} from chats where owner_id = %s and id = any(%s)",
            (owner_id, list(chat_ids)),
        )
        rows = cur.fetchall()
    return [_row_to_chat(r) for r in rows]


def restore_chats(owner_id: str, exported_chats: list[dict], workspace_id: str | None = None) -> list[dict]:
    """Part 8.7: recreates each exported chat as a brand-new row owned
    by owner_id. Deliberately does NOT reuse the original chat ids —
    restoring into a live database where the original id might already
    exist (re-importing your own backup without first deleting the
    original, or importing into a different Supabase project entirely)
    must never silently overwrite or collide with existing data. Fresh
    ids, same content, same discipline create_chat() already uses.

    linked_chat_ids is intentionally NOT restored — those ids point at
    the OLD chat ids, which no longer exist post-restore (the new ones
    have fresh ids). Re-establishing links across a restored set would
    need a two-pass id-remapping step this function doesn't attempt;
    simpler and safer to land every restored chat unlinked and let the
    person manually re-link if they care, than to silently produce
    dangling or wrong links.

    workspace_id, if given, attaches every restored chat to that
    workspace directly in the same insert — caller (chat_workspace.py /
    api/server.py) is responsible for checking the caller actually has
    edit access to that workspace before calling this."""
    restored = []
    with db.cursor() as cur:
        for chat in exported_chats:
            chat_id = new_chat_id()
            cur.execute(
                f"""
                insert into chats (id, title, owner_id, tags, template_id, messages,
                                    linked_chat_ids, workspace_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                returning {_CHAT_COLUMNS}
                """,
                (chat_id, chat.get("title", "Restored Chat"), owner_id,
                 _clean_tags(chat.get("tags")), chat.get("template_id"),
                 db.Json(chat.get("messages") or []), [], workspace_id),
            )
            restored.append(_row_to_chat(cur.fetchone()))
    return restored



def create_chat(owner_id: str, title: str = "New Chat", tags: list | None = None,
                 template_id: str | None = None) -> dict:
    """Creates an empty chat row. Returns the chat dict."""
    chat_id = new_chat_id()
    with db.cursor() as cur:
        cur.execute(
            f"""
            insert into chats (id, title, owner_id, tags, template_id, messages, linked_chat_ids)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning {_CHAT_COLUMNS}
            """,
            (chat_id, title, owner_id, _clean_tags(tags), template_id, db.Json([]), []),
        )
        row = cur.fetchone()
    write_audit(owner_id, "chat.create", "chat", chat_id, {"title": title})
    return _row_to_chat(row)


def find_chat_for_template(owner_id: str, template_id: str) -> dict | None:
    """The chat this template already has, if any — reused by every
    subsequent run instead of minting a new chat each time. Most
    recently updated wins if somehow more than one exists."""
    with db.cursor() as cur:
        cur.execute(
            """
            select id, title, created_at, updated_at, linked_chat_ids, tags, template_id,
                   jsonb_array_length(messages) as message_count
            from chats
            where owner_id = %s and template_id = %s
            order by updated_at desc
            limit 1
            """,
            (owner_id, template_id),
        )
        row = cur.fetchone()
    return _row_to_chat(row, include_messages=False) if row else None


def list_chats(owner_id: str) -> list:
    """Sidebar listing — most recently updated first."""
    with db.cursor() as cur:
        cur.execute(
            """
            select id, title, created_at, updated_at, linked_chat_ids, tags, template_id,
                   jsonb_array_length(messages) as message_count
            from chats
            where owner_id = %s
            order by updated_at desc
            """,
            (owner_id,),
        )
        rows = cur.fetchall()
    return [_row_to_chat(r, include_messages=False) for r in rows]


def list_chats_by_tag(owner_id: str, tag: str) -> list:
    """Every chat (any workspace) this owner has carrying this exact
    tag — unchanged behavior, now a WHERE clause instead of a Python
    filter over the whole index."""
    with db.cursor() as cur:
        cur.execute(
            """
            select id, title, created_at, updated_at, linked_chat_ids, tags, template_id,
                   jsonb_array_length(messages) as message_count
            from chats
            where owner_id = %s and %s = any(tags)
            order by updated_at desc
            """,
            (owner_id, tag),
        )
        rows = cur.fetchall()
    return [_row_to_chat(r, include_messages=False) for r in rows]


def get_chat(chat_id: str, owner_id: str) -> dict:
    with db.cursor() as cur:
        cur.execute(f"select {_CHAT_COLUMNS} from chats where id = %s and owner_id = %s",
                     (chat_id, owner_id))
        row = cur.fetchone()
    if not row:
        raise FileNotFoundError(f"Unknown chat_id: {chat_id!r}")
    return _row_to_chat(row)


def chat_exists(chat_id: str, owner_id: str) -> bool:
    with db.cursor() as cur:
        cur.execute("select 1 from chats where id = %s and owner_id = %s", (chat_id, owner_id))
        return cur.fetchone() is not None


def set_chat_tags(chat_id: str, owner_id: str, tags: list) -> dict:
    """Full replace — the tag editor UI sends the whole list back on
    save, same convention as before."""
    with db.cursor() as cur:
        cur.execute(
            f"""
            update chats set tags = %s, updated_at = %s
            where id = %s and owner_id = %s
            returning {_CHAT_COLUMNS}
            """,
            (_clean_tags(tags), _now(), chat_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise FileNotFoundError(f"Unknown chat_id: {chat_id!r}")
    return _row_to_chat(row)


def append_message(chat_id: str, owner_id: str, message: dict) -> dict:
    """Appends one message to the chat, updates updated_at, and
    auto-titles from the first user message. Creates the chat if it
    doesn't exist yet, so the frontend can call this immediately after
    createChat() without a race.

    The `select ... for update` below takes a real row-level lock for
    the duration of this transaction — unlike the old threading.Lock(),
    this correctly serializes concurrent writers even across multiple
    server processes/replicas, not just threads in one process (closes
    the second gap flagged in Part 8.1)."""
    message = {**message, "ts": message.get("ts") or _now().isoformat()}

    with db.cursor() as cur:
        cur.execute("select title, messages from chats where id = %s and owner_id = %s for update",
                     (chat_id, owner_id))
        existing = cur.fetchone()

        if existing is None:
            title = "New Chat"
            if message.get("role") == "user":
                text = (message.get("text") or "").strip().replace("\n", " ")
                if text:
                    title = text[:60] + ("..." if len(text) > 60 else "")
            cur.execute(
                f"""
                insert into chats (id, title, owner_id, messages)
                values (%s, %s, %s, %s)
                returning {_CHAT_COLUMNS}
                """,
                (chat_id, title, owner_id, db.Json([message])),
            )
            return _row_to_chat(cur.fetchone())

        messages = (existing["messages"] or []) + [message]
        title = existing["title"]
        if title == "New Chat" and message.get("role") == "user":
            text = (message.get("text") or "").strip().replace("\n", " ")
            if text:
                title = text[:60] + ("..." if len(text) > 60 else "")

        cur.execute(
            f"""
            update chats set messages = %s, title = %s, updated_at = %s
            where id = %s and owner_id = %s
            returning {_CHAT_COLUMNS}
            """,
            (db.Json(messages), title, _now(), chat_id, owner_id),
        )
        return _row_to_chat(cur.fetchone())


def rename_chat(chat_id: str, owner_id: str, new_title: str) -> dict:
    new_title = new_title.strip()[:120]
    with db.cursor() as cur:
        if new_title:
            cur.execute(
                f"update chats set title = %s, updated_at = %s where id = %s and owner_id = %s "
                f"returning {_CHAT_COLUMNS}",
                (new_title, _now(), chat_id, owner_id),
            )
        else:
            # Empty new title: keep the existing title (matches the
            # original's `new_title.strip()[:120] or chat["title"]`),
            # but updated_at still moves, same as before.
            cur.execute(
                f"update chats set updated_at = %s where id = %s and owner_id = %s "
                f"returning {_CHAT_COLUMNS}",
                (_now(), chat_id, owner_id),
            )
        row = cur.fetchone()
    if not row:
        raise FileNotFoundError(f"Unknown chat_id: {chat_id!r}")
    return _row_to_chat(row)


def set_linked_chats(chat_id: str, owner_id: str, linked_chat_ids: list) -> dict:
    """Sets which OTHER chats this chat shares memory with. One-
    directional by design — linking A -> B does not automatically link
    B -> A. A linked_chat_id is only kept if it's also owned by
    owner_id and exists — same filter as the original's chat_exists()
    check, now additionally scoped so one user can never link to
    another user's chat."""
    with db.cursor() as cur:
        cur.execute("select id from chats where owner_id = %s and id = any(%s)",
                     (owner_id, linked_chat_ids))
        existing_ids = {r["id"] for r in cur.fetchall()}
        clean_links = [c for c in linked_chat_ids if c != chat_id and c in existing_ids]

        cur.execute(
            f"""
            update chats set linked_chat_ids = %s, updated_at = %s
            where id = %s and owner_id = %s
            returning {_CHAT_COLUMNS}
            """,
            (clean_links, _now(), chat_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise FileNotFoundError(f"Unknown chat_id: {chat_id!r}")
    return _row_to_chat(row)


def delete_chat(chat_id: str, owner_id: str) -> None:
    """Deletes the chat row (a no-op if it doesn't exist or isn't
    owned by owner_id, matching the original's silent-no-op-on-missing
    behavior), strips it out of every other of this owner's chats'
    linked_chat_ids, and clears its Redis working memory."""
    with db.cursor() as cur:
        cur.execute("delete from chats where id = %s and owner_id = %s returning id",
                     (chat_id, owner_id))
        if not cur.fetchone():
            return
        write_audit(owner_id, "chat.delete", "chat", chat_id, {})
        cur.execute(
            """
            update chats set linked_chat_ids = array_remove(linked_chat_ids, %s)
            where owner_id = %s and %s = any(linked_chat_ids)
            """,
            (chat_id, owner_id, chat_id),
        )

    try:
        from memory.bus import delete as bus_delete
        bus_delete(f"conversation:{chat_id}")
    except Exception:
        pass  # bus.py needs Upstash env vars; don't let a missing .env break chat deletion


# --- cross-chat memory sharing (§4) --------------------------------------

def _extract_answer_text(message: dict) -> str:
    """Same shape logic as api/task_runner.py's _extract_answer_text, but
    over a stored message dict rather than a live response."""
    data = message.get("data") or {}
    result = data.get("result") or {}
    if "answer" in result:
        return str(result["answer"])
    if "code" in result:
        return str(result["code"])
    if "output" in result:
        return str(result["output"])
    return str(data.get("message") or "")


def get_linked_context_text(chat_id: str, owner_id: str, max_turns_per_chat: int = 6,
                             char_limit: int = 400) -> str:
    """Builds a labeled block of recent turns from every chat that
    chat_id links TO (its linked_chat_ids), for injection into the
    agents' context. Returns "" if the chat has no links or doesn't
    exist (or isn't owned by owner_id)."""
    if not chat_exists(chat_id, owner_id):
        return ""
    chat = get_chat(chat_id, owner_id)
    linked_ids = chat.get("linked_chat_ids") or []
    if not linked_ids:
        return ""

    blocks = []
    for linked_id in linked_ids:
        if not chat_exists(linked_id, owner_id):
            continue
        linked = get_chat(linked_id, owner_id)
        recent = linked["messages"][-max_turns_per_chat:]
        lines = []
        for m in recent:
            text = m.get("text", "") if m.get("role") == "user" else _extract_answer_text(m)
            text = (text or "").strip().replace("\n", " ")
            if len(text) > char_limit:
                text = text[:char_limit] + "..."
            if text:
                lines.append(f"- {m.get('role')}: {text}")
        if lines:
            blocks.append(f"[Shared memory from chat \"{linked['title']}\"]\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def estimate_batch_context_tokens(owner_id: str, chat_ids: list, max_turns_per_chat: int = 6,
                                   char_limit: int = 400) -> dict:
    """Rough token-cost estimate for the create-batch modal, called
    BEFORE any batch exists. owner_id is added as the first argument
    (no natural chat_id to slot it after, unlike the rest of this
    module) — every candidate chat_id is checked against owner_id, so
    a batch can never be estimated (or created) across chats the
    caller doesn't own."""
    valid_ids = [cid for cid in chat_ids if chat_exists(cid, owner_id)]
    blocks_by_chat = {}
    for cid in valid_ids:
        chat = get_chat(cid, owner_id)
        recent = chat["messages"][-max_turns_per_chat:]
        lines = []
        for m in recent:
            text = m.get("text", "") if m.get("role") == "user" else _extract_answer_text(m)
            text = (text or "").strip().replace("\n", " ")
            if len(text) > char_limit:
                text = text[:char_limit] + "..."
            if text:
                lines.append(f"- {m.get('role')}: {text}")
        blocks_by_chat[cid] = (
            f"[Shared memory from chat \"{chat['title']}\"]\n" + "\n".join(lines) if lines else ""
        )

    CHARS_PER_TOKEN = 4
    per_chat_tokens = {}
    for cid in valid_ids:
        others_text = "\n\n".join(
            blocks_by_chat[other] for other in valid_ids if other != cid and blocks_by_chat[other]
        )
        per_chat_tokens[cid] = len(others_text) // CHARS_PER_TOKEN

    values = list(per_chat_tokens.values())
    return {
        "member_count": len(valid_ids),
        "per_chat_tokens": per_chat_tokens,
        "max_tokens_per_message": max(values) if values else 0,
        "avg_tokens_per_message": (sum(values) // len(values)) if values else 0,
    }


# --- Part 8.3: collaborator access resolver --------------------------------
#
# Every function above this line is still strictly owner_id-scoped, on
# purpose — nothing about their behavior changed. This is the one new
# function added for collaborator sharing: given a chat_id and the id of
# whoever is actually making the request, it figures out whether that
# person has access via workspace membership (when they're not the
# chat's real owner), and if so, what role. api/server.py's chat routes
# call this FIRST, then dispatch to the existing owner-scoped functions
# above using the chat's REAL owner_id — never the requester's own id,
# since these functions have no other concept of "someone else's chat,
# accessed with permission."
def resolve_chat_access(chat_id: str, requesting_user_id: str) -> tuple[str, str] | None:
    """Returns (real_owner_id, role) where role is 'owner', 'editor', or
    'viewer', or None if requesting_user_id has no access to this chat
    at all (doesn't exist, or exists but isn't shared with them).

    A private chat (is_private=true) is never resolved this way for
    anyone but its real owner — collaborator access never overrides
    privacy, regardless of workspace role."""
    if chat_exists(chat_id, requesting_user_id):
        return (requesting_user_id, "owner")

    with db.cursor() as cur:
        cur.execute(
            "select owner_id, workspace_id, is_private from chats where id = %s",
            (chat_id,),
        )
        row = cur.fetchone()
    if not row or not row["workspace_id"] or row["is_private"]:
        return None

    # Local import avoids a circular import at module load time
    # (chat_workspace.py already imports chat_store.py).
    from eo import chat_workspace
    role = chat_workspace.member_role(row["workspace_id"], requesting_user_id)
    if role is None:
        return None
    return (row["owner_id"], role)