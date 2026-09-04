"""
seed_load_test_data.py — creates realistic test data via the real HTTP API
(not direct DB writes), so it goes through the exact same code paths a
real user would hit. Needed before any of the k6 scenarios are meaningful:
you can't test pagination/page-cache/relink cost against empty chats.

Usage:
    python seed_load_test_data.py <base_url> <bearer_token> \
        --chats 20 --messages-per-chat 150 --batch-size 6

Creates:
  - `--chats` chats, each with `--messages-per-chat` messages (so at
    limit=60/page, each chat has several before_seq pages to paginate
    through — needed to exercise chat_page_cache.py's HIT_THRESHOLD).
  - One batch grouping the first `--batch-size` of those chats (to
    exercise memory_batch._sync_members' relink fan-out).

Prints the created chat_ids and batch_id at the end — feed the first
one into k6's CHAT_ID env var for the pagination/cache scenarios.
"""
import argparse
import sys

import requests


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("base_url")
    p.add_argument("token")
    p.add_argument("--chats", type=int, default=20)
    p.add_argument("--messages-per-chat", type=int, default=150)
    p.add_argument("--batch-size", type=int, default=6)
    args = p.parse_args()

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"}
    session = requests.Session()
    session.headers.update(headers)

    chat_ids = []
    for i in range(args.chats):
        resp = session.post(f"{base_url}/api/chats", json={"title": f"loadtest-chat-{i}"})
        resp.raise_for_status()
        chat_id = resp.json()["id"]
        chat_ids.append(chat_id)

        for j in range(args.messages_per_chat):
            role = "user" if j % 2 == 0 else "assistant"
            resp = session.post(
                f"{base_url}/api/chats/{chat_id}/messages",
                json={"message": {
                    "role": role,
                    "text": f"seed message {j} in chat {i} — {'x' * 40}",
                }},
            )
            resp.raise_for_status()

        print(f"chat {i+1}/{args.chats}: {chat_id} ({args.messages_per_chat} messages)")

    batch_id = None
    if args.batch_size >= 2 and len(chat_ids) >= args.batch_size:
        member_ids = chat_ids[:args.batch_size]
        resp = session.post(
            f"{base_url}/api/batches",
            json={"name": "loadtest-batch", "member_chat_ids": member_ids},
        )
        resp.raise_for_status()
        batch_id = resp.json()["id"]
        print(f"\nbatch: {batch_id} ({args.batch_size} members)")

    print("\n--- summary ---")
    print(f"chat_ids ({len(chat_ids)}):")
    for cid in chat_ids:
        print(f"  {cid}")
    if batch_id:
        print(f"batch_id: {batch_id}")
    print(f"\nFirst chat (use for k6 CHAT_ID): {chat_ids[0]}")


if __name__ == "__main__":
    main()
