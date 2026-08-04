"""
test_cross_chat_memory.py — manual test for steps 2-4 (chat_store.py +
conversation_memory.py wiring), with NO frontend and NO API layer needed.

Run this from your repo ROOT (same level as eo/, agents/, memory/):

    python3 test_cross_chat_memory.py

What it does, step by step:
  1. Creates two chats on disk via chat_store.create_chat() — check
     data/chats/ afterwards, you should see two new chat_....json files
     plus an updated _index.json.
  2. Appends a couple of messages to "Chat A" establishing a fact, the
     same way the /api/chats/{id}/messages endpoint will once step 5
     exists — this writes straight to the chat_....json file.
  3. Links "Chat B" -> "Chat A" via set_linked_chats(), same as what the
     LinkChatsModal will call once the frontend exists (step 7).
  4. Calls conversation_memory.get_full_context(chat_b_id) and
     get_light_context(chat_b_id) — these are the exact functions every
     agent already calls today, just keyed by session_id. If wiring
     worked, you'll see Chat A's fact show up in Chat B's context even
     though Chat B itself has zero turns of its own.

If this prints the expected block, steps 2-4 are proven correct
end-to-end — the frontend (later steps) is just a UI on top of the same
functions this script is calling directly.
"""
import os
import sys

# NEW — this file lives in tests/, one level below the repo root where
# eo/, agents/, memory/ actually are. Without this, `from eo import ...`
# below fails with ModuleNotFoundError when run as
# `python3 tests/test_cross_chat_memory.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo import chat_store
from eo import conversation_memory


def main():
    print("=== 1. Creating two chats ===")
    chat_a = chat_store.create_chat(title="Chat A - Aurora project")
    chat_b = chat_store.create_chat(title="Chat B - should see Aurora")
    print(f"Chat A id: {chat_a['id']}")
    print(f"Chat B id: {chat_b['id']}")

    print("\n=== 2. Appending messages to Chat A ===")
    chat_store.append_message(chat_a["id"], {
        "role": "user",
        "text": "The project is called Aurora and it uses FastAPI.",
    })
    chat_store.append_message(chat_a["id"], {
        "role": "assistant",
        "data": {"result": {"answer": "Got it — Aurora, built on FastAPI."}},
    })
    print("Appended 2 messages to Chat A.")

    print("\n=== 3. Linking Chat B -> Chat A ===")
    chat_store.set_linked_chats(chat_b["id"], [chat_a["id"]])
    updated_b = chat_store.get_chat(chat_b["id"])
    print(f"Chat B's linked_chat_ids: {updated_b['linked_chat_ids']}")
    assert chat_a["id"] in updated_b["linked_chat_ids"], "Linking failed!"

    print("\n=== 4. Reading Chat B's context (should include Chat A's fact) ===")
    full_context = conversation_memory.get_full_context(chat_b["id"])
    light_context = conversation_memory.get_light_context(chat_b["id"])

    print("\n--- get_full_context(chat_b_id) ---")
    print(full_context or "(empty — something's wrong)")

    print("\n--- get_light_context(chat_b_id) ---")
    print(light_context or "(empty — something's wrong)")

    if "Aurora" in full_context and "Aurora" in light_context:
        print("\n✅ SUCCESS: Chat B's context includes Chat A's linked memory.")
    else:
        print("\n❌ FAILED: 'Aurora' not found in Chat B's context — check the wiring.")
        sys.exit(1)

    print("\n=== 5. Cleanup (optional) ===")
    print(f"To remove these test chats: chat_store.delete_chat('{chat_a['id']}') "
          f"and chat_store.delete_chat('{chat_b['id']}')")
    print("Or just leave them — they'll show up in data/chats/ until you delete them.")


if __name__ == "__main__":
    main()