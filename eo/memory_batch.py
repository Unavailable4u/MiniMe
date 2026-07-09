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
"""
import os, json, uuid, threading
from datetime import datetime, timezone
from eo import chat_store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCHES_PATH = os.path.join(BASE_DIR, "data", "chats", "_batches.json")
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read():
    if not os.path.exists(BATCHES_PATH):
        return {"batches": []}
    with open(BATCHES_PATH) as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(BATCHES_PATH), exist_ok=True)
    with open(BATCHES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _sync_members(member_ids: list[str]):
    """Every member links to every OTHER member — makes the group mutual."""
    for cid in member_ids:
        others = [m for m in member_ids if m != cid]
        chat_store.set_linked_chats(cid, others)


def list_batches() -> list:
    return _read()["batches"]


def get_batch(batch_id: str) -> dict:
    for b in _read()["batches"]:
        if b["id"] == batch_id:
            return b
    raise FileNotFoundError(batch_id)


def create_batch(name: str, member_chat_ids: list[str]) -> dict:
    if len(member_chat_ids) < 2:
        raise ValueError("A batch needs at least 2 chats.")
    with _lock:
        data = _read()
        batch = {
            "id": f"batch_{uuid.uuid4().hex[:10]}",
            "name": name.strip() or "Untitled batch",
            "member_chat_ids": member_chat_ids,
            "created_at": _now(),
            "updated_at": _now(),
        }
        data["batches"].append(batch)
        _write(data)
    _sync_members(member_chat_ids)
    return batch


def rename_batch(batch_id: str, new_name: str) -> dict:
    with _lock:
        data = _read()
        for b in data["batches"]:
            if b["id"] == batch_id:
                b["name"] = new_name.strip()[:80] or b["name"]
                b["updated_at"] = _now()
                _write(data)
                return b
        raise FileNotFoundError(batch_id)


def unlink_members(batch_id: str, remove_ids: list[str]) -> dict | None:
    """Removes the given chats from the batch. If only one member would be
    left, the whole batch is dissolved (per your rule: a batch of 1 makes
    no sense) — this clears linked_chat_ids for the last member too, and
    the removed chats have their links to the group cleared either way.
    Returns the updated batch, or None if the batch was dissolved."""
    with _lock:
        data = _read()
        batch = next((b for b in data["batches"] if b["id"] == batch_id), None)
        if not batch:
            raise FileNotFoundError(batch_id)

        remaining = [m for m in batch["member_chat_ids"] if m not in remove_ids]

        if len(remaining) <= 1:
            # Dissolve: clear links for EVERY original member, delete the batch.
            for cid in batch["member_chat_ids"]:
                if chat_store.chat_exists(cid):
                    chat_store.set_linked_chats(cid, [])
            data["batches"] = [b for b in data["batches"] if b["id"] != batch_id]
            _write(data)
            return None

        # Otherwise: removed chats get unlinked from the group entirely;
        # remaining members re-sync to each other only.
        for cid in remove_ids:
            if chat_store.chat_exists(cid):
                chat_store.set_linked_chats(cid, [])
        batch["member_chat_ids"] = remaining
        batch["updated_at"] = _now()
        _write(data)
    _sync_members(remaining)
    return batch


def add_member(batch_id: str, chat_id: str) -> dict:
    with _lock:
        data = _read()
        batch = next((b for b in data["batches"] if b["id"] == batch_id), None)
        if not batch:
            raise FileNotFoundError(batch_id)
        if chat_id not in batch["member_chat_ids"]:
            batch["member_chat_ids"].append(chat_id)
        batch["updated_at"] = _now()
        _write(data)
    _sync_members(batch["member_chat_ids"])
    return batch


def delete_batch(batch_id: str) -> None:
    """Deletes the batch entirely and clears linked_chat_ids for every
    member — this does NOT delete the chats themselves, only the grouping,
    matching your 'delete that batch of linked shared memory' request."""
    with _lock:
        data = _read()
        batch = next((b for b in data["batches"] if b["id"] == batch_id), None)
        if not batch:
            raise FileNotFoundError(batch_id)
        data["batches"] = [b for b in data["batches"] if b["id"] != batch_id]
        _write(data)
    for cid in batch["member_chat_ids"]:
        if chat_store.chat_exists(cid):
            chat_store.set_linked_chats(cid, [])


def batch_for_chat(chat_id: str) -> dict | None:
    for b in list_batches():
        if chat_id in b["member_chat_ids"]:
            return b
    return None