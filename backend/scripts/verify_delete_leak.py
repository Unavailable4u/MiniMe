"""
verify_delete_leak.py — direct, minimal repro of the audit's #1 finding:
memory.bus.delete() doesn't exist, so DELETE /api/chats/{id} silently
fails to clear the chat's Redis conversation history.

Run from the `backend/` directory so `memory.bus` importable:
    cd MiniMe/backend
    python /path/to/verify_delete_leak.py <base_url> <bearer_token>

Example:
    python verify_delete_leak.py http://localhost:8000 "$(cat /tmp/token.txt)"

What it does:
  1. Creates a chat via the real API.
  2. Appends a message (this is what populates the Redis
     "conversation:{chat_id}" key via conversation_memory.append_turn,
     same as a real /api/task run would).
  3. Confirms the key exists in Redis directly (bypassing the app layer).
  4. Deletes the chat via DELETE /api/chats/{id} (the real endpoint).
  5. Checks Redis again. If the key still exists, the leak is confirmed
     live in your current deployment, not just in the source code.

This talks to Redis directly via memory.bus's raw `redis` client and
`_namespaced()` helper, the same way scripts/cleanup_test_data.py already
does — it does NOT rely on the broken bus.delete() being fixed.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from memory.bus import _namespaced, redis  # raw client + key-prefix helper


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    token = sys.argv[2]
    headers = {"Authorization": f"Bearer {token}"}

    print("1. Creating a test chat via POST /api/chats ...")
    resp = requests.post(f"{base_url}/api/chats", json={"title": "delete-leak-repro"}, headers=headers)
    resp.raise_for_status()
    chat_id = resp.json()["id"]
    print(f"   chat_id = {chat_id}")

    print("2. Appending a message so conversation memory gets populated ...")
    resp = requests.post(
        f"{base_url}/api/chats/{chat_id}/messages",
        json={"message": {"role": "user", "text": "hello, this is a leak-repro message"}},
        headers=headers,
    )
    resp.raise_for_status()

    # append_message() -> conversation_memory.append_turn() only fires
    # from the real agent pipeline (api/task_runner.py), not from this
    # plain UI-message endpoint. If your build's append_turn() is only
    # called from /api/task, run one cheap task against this chat_id
    # instead of/in addition to the raw message above, e.g.:
    #   POST /api/task {"session_id": chat_id, "task": "say hi", "tier_override": 0}
    # Adjust to match whatever your lightest real task-trigger route is.

    conv_key = _namespaced(f"conversation:{chat_id}")
    print(f"3. Checking Redis directly for key: {conv_key}")
    exists_before = redis.exists(conv_key)
    print(f"   exists before delete: {bool(exists_before)}")

    print("4. Deleting the chat via DELETE /api/chats/{chat_id} ...")
    resp = requests.delete(f"{base_url}/api/chats/{chat_id}", headers=headers)
    resp.raise_for_status()
    print(f"   response: {resp.json()}")

    print("5. Checking Redis again ...")
    exists_after = redis.exists(conv_key)
    print(f"   exists after delete: {bool(exists_after)}")

    print()
    if exists_before and exists_after:
        print("LEAK CONFIRMED: the conversation key survived chat deletion.")
        print("This matches the audit finding — memory.bus.delete() is missing,")
        print("chat_store.delete_chat()'s `from memory.bus import delete` raises")
        print("ImportError, and it's silently swallowed by the bare except.")
    elif not exists_before:
        print("INCONCLUSIVE: the conversation key was never created in step 2 —")
        print("append_message() alone may not trigger conversation_memory.append_turn()")
        print("in your build. Trigger a real /api/task call against this chat_id")
        print("instead, then rerun.")
    else:
        print("NOT REPRODUCED: the key is gone after delete. Either the fix has")
        print("already shipped, or something else is clearing it — worth checking")
        print("chat_store.delete_chat()'s bus_delete import didn't just get fixed.")

    # Manual cleanup, since the whole point of this script is that the
    # normal delete path might not have actually cleared this key.
    if exists_after:
        print(f"\nCleaning up leftover key manually: {conv_key}")
        redis.delete(conv_key)


if __name__ == "__main__":
    main()
