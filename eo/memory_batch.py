"""
eo/memory_batch.py — named, mutual-membership groups of chats that share
memory with EACH OTHER (unlike chat_store.set_linked_chats, which is
one-directional per chat). A batch is the "group of 5 linked chats" the UI
shows as one unit.

Relationship to chat_store.linked_chat_ids: creating/editing a batch is
the ONLY thing that should write linked_chat_ids for its members going
forward — it keeps every member's linked_chat_ids in sync so membership
stays symmetric. Chats not in a batch can still use the old manual
per-chat linking UI if you want to keep that power-user path available.

--- Part 8.2 migration notes -----------------------------------------------
Migrated from data/chats/_batches.json to Postgres (batches +
batch_members tables, see part8_schema.sql), scoped by owner_id. Every
function now takes owner_id, slotted in right after any existing
identifier argument — same convention as chat_store.py / chat_workspace.py.

Behavior tightening vs. the old file version: member_chat_ids passed to
create_batch/add_member are now filtered down to chat IDs that actually
exist AND are owned by owner_id before being stored (batch_members has a
real foreign key to chats). The old version would silently store bogus
IDs in member_chat_ids itself and only the downstream linking ignored
them — this closes that gap rather than reproducing it, matching the
same filter chat_store.set_linked_chats already applies elsewhere.
-----------------------------------------------------------------------------
"""
import uuid
from datetime import datetime, timezone
from eo import db
from eo import chat_store


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None


def _row_to_batch(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "member_chat_ids": row.get("member_chat_ids") or [],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


_BATCH_SELECT = """
    select b.id, b.name, b.created_at, b.updated_at,
           coalesce(array_agg(bm.chat_id) filter (where bm.chat_id is not null), '{}') as member_chat_ids
    from batches b
    left join batch_members bm on bm.batch_id = b.id
"""


def _sync_members(member_ids: list[str], owner_id: str):
    """Every member links to every OTHER member — makes the group mutual."""
    for cid in member_ids:
        others = [m for m in member_ids if m != cid]
        chat_store.set_linked_chats(cid, owner_id, others)


def _owned_chat_ids(chat_ids: list[str], owner_id: str) -> list[str]:
    """Filters a candidate chat_id list down to ones that exist and are
    owned by owner_id — preserves input order."""
    if not chat_ids:
        return []
    with db.cursor() as cur:
        cur.execute("select id from chats where owner_id = %s and id = any(%s)",
                     (owner_id, chat_ids))
        valid = {r["id"] for r in cur.fetchall()}
    return [c for c in chat_ids if c in valid]


def list_batches(owner_id: str) -> list:
    with db.cursor() as cur:
        cur.execute(
            _BATCH_SELECT + " where b.owner_id = %s group by b.id order by b.updated_at desc",
            (owner_id,),
        )
        rows = cur.fetchall()
    return [_row_to_batch(r) for r in rows]


def get_batch(batch_id: str, owner_id: str) -> dict:
    with db.cursor() as cur:
        cur.execute(
            _BATCH_SELECT + " where b.id = %s and b.owner_id = %s group by b.id",
            (batch_id, owner_id),
        )
        row = cur.fetchone()
    if not row:
        raise FileNotFoundError(batch_id)
    return _row_to_batch(row)


def create_batch(owner_id: str, name: str, member_chat_ids: list[str]) -> dict:
    if len(member_chat_ids) < 2:
        raise ValueError("A batch needs at least 2 chats.")
    valid_ids = _owned_chat_ids(member_chat_ids, owner_id)
    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    clean_name = name.strip() or "Untitled batch"

    with db.cursor() as cur:
        cur.execute(
            "insert into batches (id, name, owner_id) values (%s, %s, %s)",
            (batch_id, clean_name, owner_id),
        )
        for cid in valid_ids:
            cur.execute(
                "insert into batch_members (batch_id, chat_id) values (%s, %s)",
                (batch_id, cid),
            )

    _sync_members(valid_ids, owner_id)
    return get_batch(batch_id, owner_id)


def rename_batch(batch_id: str, owner_id: str, new_name: str) -> dict:
    clean_name = new_name.strip()[:80]
    with db.cursor() as cur:
        if clean_name:
            cur.execute(
                "update batches set name = %s, updated_at = %s where id = %s and owner_id = %s "
                "returning id",
                (clean_name, _now(), batch_id, owner_id),
            )
        else:
            cur.execute(
                "update batches set updated_at = %s where id = %s and owner_id = %s returning id",
                (_now(), batch_id, owner_id),
            )
        if not cur.fetchone():
            raise FileNotFoundError(batch_id)
    return get_batch(batch_id, owner_id)


def unlink_members(batch_id: str, owner_id: str, remove_ids: list[str]) -> dict | None:
    """Removes the given chats from the batch. If only one member
    would be left, the whole batch is dissolved (a batch of 1 makes no
    sense) — clears linked_chat_ids for the last member too. Returns
    the updated batch, or None if the batch was dissolved."""
    batch = get_batch(batch_id, owner_id)  # raises FileNotFoundError if missing
    remaining = [m for m in batch["member_chat_ids"] if m not in remove_ids]

    if len(remaining) <= 1:
        for cid in batch["member_chat_ids"]:
            if chat_store.chat_exists(cid, owner_id):
                chat_store.set_linked_chats(cid, owner_id, [])
        with db.cursor() as cur:
            cur.execute("delete from batches where id = %s and owner_id = %s", (batch_id, owner_id))
        return None

    for cid in remove_ids:
        if chat_store.chat_exists(cid, owner_id):
            chat_store.set_linked_chats(cid, owner_id, [])
    with db.cursor() as cur:
        cur.execute(
            "delete from batch_members where batch_id = %s and chat_id = any(%s)",
            (batch_id, remove_ids),
        )
        cur.execute("update batches set updated_at = %s where id = %s", (_now(), batch_id))

    _sync_members(remaining, owner_id)
    return get_batch(batch_id, owner_id)


def add_member(batch_id: str, owner_id: str, chat_id: str) -> dict:
    batch = get_batch(batch_id, owner_id)  # raises FileNotFoundError if missing
    if not _owned_chat_ids([chat_id], owner_id):
        # Matches the old permissive behavior of not raising on an
        # unrecognized chat_id — it's simply not added.
        return batch

    with db.cursor() as cur:
        cur.execute(
            "insert into batch_members (batch_id, chat_id) values (%s, %s) "
            "on conflict (batch_id, chat_id) do nothing",
            (batch_id, chat_id),
        )
        cur.execute("update batches set updated_at = %s where id = %s", (_now(), batch_id))

    updated = get_batch(batch_id, owner_id)
    _sync_members(updated["member_chat_ids"], owner_id)
    return updated


def delete_batch(batch_id: str, owner_id: str) -> None:
    """Deletes the batch entirely and clears linked_chat_ids for every
    member — does NOT delete the chats themselves, only the grouping."""
    batch = get_batch(batch_id, owner_id)  # raises FileNotFoundError if missing
    with db.cursor() as cur:
        cur.execute("delete from batches where id = %s and owner_id = %s", (batch_id, owner_id))
    for cid in batch["member_chat_ids"]:
        if chat_store.chat_exists(cid, owner_id):
            chat_store.set_linked_chats(cid, owner_id, [])


def batch_for_chat(chat_id: str, owner_id: str) -> dict | None:
    with db.cursor() as cur:
        cur.execute(
            "select batch_id from batch_members where chat_id = %s limit 1",
            (chat_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return get_batch(row["batch_id"], owner_id)
    except FileNotFoundError:
        return None