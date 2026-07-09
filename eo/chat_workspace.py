"""
eo/chat_workspace.py — named containers of chats (UI label: "Projects").
Membership in a workspace automatically keeps its chats mutually linked
for memory sharing, same mechanism as eo/memory_batch.py's _sync_members,
reused here so a workspace behaves like an always-on batch.

Deliberately separate from eo/project_registry.py, which tracks external
codebase roots for Cross-Project File Control — unrelated concept, same
word collision risk, hence the different module/table name.
"""
import os, json, uuid, threading
from datetime import datetime, timezone
from eo import chat_store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACES_PATH = os.path.join(BASE_DIR, "data", "chats", "_workspaces.json")
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read():
    if not os.path.exists(WORKSPACES_PATH):
        return {"workspaces": []}
    with open(WORKSPACES_PATH) as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(WORKSPACES_PATH), exist_ok=True)
    with open(WORKSPACES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _sync(chat_ids):
    for cid in chat_ids:
        others = [m for m in chat_ids if m != cid]
        chat_store.set_linked_chats(cid, others)


def list_workspaces():
    return _read()["workspaces"]


def get_workspace(ws_id):
    for w in _read()["workspaces"]:
        if w["id"] == ws_id:
            return w
    raise FileNotFoundError(ws_id)


def create_workspace(name: str) -> dict:
    with _lock:
        data = _read()
        ws = {"id": f"ws_{uuid.uuid4().hex[:10]}", "name": name.strip() or "Untitled project",
              "chat_ids": [], "created_at": _now(), "updated_at": _now()}
        data["workspaces"].append(ws)
        _write(data)
        return ws


def rename_workspace(ws_id: str, name: str) -> dict:
    with _lock:
        data = _read()
        for w in data["workspaces"]:
            if w["id"] == ws_id:
                w["name"] = name.strip()[:80] or w["name"]
                w["updated_at"] = _now()
                _write(data)
                return w
        raise FileNotFoundError(ws_id)


def add_chat(ws_id: str, chat_id: str) -> dict:
    with _lock:
        data = _read()
        ws = next((w for w in data["workspaces"] if w["id"] == ws_id), None)
        if not ws:
            raise FileNotFoundError(ws_id)
        if chat_id not in ws["chat_ids"]:
            ws["chat_ids"].append(chat_id)
        ws["updated_at"] = _now()
        _write(data)
    _sync(ws["chat_ids"])
    return ws


def remove_chat(ws_id: str, chat_id: str, delete_chat: bool = False) -> dict:
    """remove_chat=False: 'remove from project' — chat stays, just leaves
    the workspace and loses the auto-links to former workspace-mates.
    delete_chat=True: 'delete chat in project' — the chat is gone for
    good (via chat_store.delete_chat), not just ungrouped."""
    with _lock:
        data = _read()
        ws = next((w for w in data["workspaces"] if w["id"] == ws_id), None)
        if not ws:
            raise FileNotFoundError(ws_id)
        ws["chat_ids"] = [c for c in ws["chat_ids"] if c != chat_id]
        ws["updated_at"] = _now()
        _write(data)

    if delete_chat:
        chat_store.delete_chat(chat_id)
    elif chat_store.chat_exists(chat_id):
        chat_store.set_linked_chats(chat_id, [])

    _sync(ws["chat_ids"])
    return ws


def delete_workspace(ws_id: str) -> None:
    """Deletes the workspace container only — member chats survive and
    simply stop auto-sharing memory with each other."""
    with _lock:
        data = _read()
        ws = next((w for w in data["workspaces"] if w["id"] == ws_id), None)
        if not ws:
            raise FileNotFoundError(ws_id)
        data["workspaces"] = [w for w in data["workspaces"] if w["id"] != ws_id]
        _write(data)
    for cid in ws["chat_ids"]:
        if chat_store.chat_exists(cid):
            chat_store.set_linked_chats(cid, [])