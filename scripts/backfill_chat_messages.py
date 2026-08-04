"""
scripts/backfill_chat_messages.py — Perf audit item #1, step 1.2.

Copies every chat's existing `chats.messages` JSONB array into the new
`chat_messages` table (migrations/0002_add_chat_messages_table.sql),
one row per message, preserving array order via `seq`.

This script ONLY reads chats.messages and writes chat_messages — it
never touches or clears the old JSONB column. Safe to re-run: each
chat is skipped if chat_messages already has a row count matching that
chat's current messages array length, so a second run after new
messages have arrived (via the still-unchanged append_message()) will
correctly backfill only what's missing. It does NOT attempt to merge
or diff individual messages within a partially-backfilled chat -- if
the counts don't match, the chat's existing chat_messages rows for
that chat are deleted and it's backfilled from scratch, since `seq`
must exactly mirror array order and a partial/interleaved backfill
would be worse than redoing it.

This is intentionally NOT run automatically anywhere -- append_message()
and get_chat() are NOT changed by this step (that's step 1.3). Until
that step lands, chat_messages is a shadow copy only; the JSONB column
in chats remains the sole source of truth the app actually reads from.

Usage (PowerShell):
    python scripts/backfill_chat_messages.py            # do the backfill
    python scripts/backfill_chat_messages.py --dry-run   # report counts only, write nothing
"""
import argparse
import sys
from pathlib import Path

# Matches the convention in scripts/seed_test_note.py -- this script
# lives in scripts/, but `eo` is a top-level package at the project
# root, so the root needs to be on sys.path when this is run directly
# (python scripts/backfill_chat_messages.py) rather than as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eo import db


def _chats_needing_backfill(cur):
    """Every chat, its owner, its message count, and how many rows
    chat_messages already has for it. jsonb_array_length is used
    instead of pulling the array into Python just to len() it --
    cheaper, and avoids deserializing messages this script doesn't
    otherwise need."""
    cur.execute(
        """
        select c.id as chat_id,
               c.owner_id,
               jsonb_array_length(coalesce(c.messages, '[]'::jsonb)) as source_count,
               count(cm.id) as backfilled_count
        from chats c
        left join chat_messages cm on cm.chat_id = c.id
        group by c.id, c.owner_id, c.messages
        order by c.updated_at asc
        """
    )
    return cur.fetchall()


def _backfill_one_chat(cur, chat_id: str, owner_id: str):
    """Re-derives this chat's chat_messages rows from scratch, in
    array order. Deletes any existing (partial/stale) rows for this
    chat_id first -- see module docstring for why partial merge isn't
    attempted."""
    cur.execute("select messages from chats where id = %s", (chat_id,))
    row = cur.fetchone()
    messages = (row["messages"] if row else None) or []

    cur.execute("delete from chat_messages where chat_id = %s", (chat_id,))

    if not messages:
        return 0

    rows = [
        (chat_id, owner_id, seq, msg.get("role"), db.Json(msg))
        for seq, msg in enumerate(messages)
    ]
    cur.executemany(
        """
        insert into chat_messages (chat_id, owner_id, seq, role, payload)
        values (%s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would be backfilled without writing anything.")
    args = parser.parse_args()

    # trusted=True: this is an offline, developer-run admin script that
    # walks every owner's chats in one pass, not a per-user request --
    # see migration 0003 for what this flag does under RLS.
    with db.cursor(trusted=True) as cur:
        candidates = _chats_needing_backfill(cur)
        to_process = [c for c in candidates if c["source_count"] != c["backfilled_count"]]

        print(f"{len(candidates)} total chats, {len(to_process)} need backfilling "
              f"(count mismatch between chats.messages and chat_messages).")

        if args.dry_run:
            for c in to_process:
                print(f"  {c['chat_id']}: source={c['source_count']} "
                      f"backfilled={c['backfilled_count']}")
            print("Dry run -- nothing written.")
            return

        total_rows = 0
        for i, c in enumerate(to_process, 1):
            n = _backfill_one_chat(cur, c["chat_id"], c["owner_id"])
            total_rows += n
            if i % 50 == 0 or i == len(to_process):
                print(f"  [{i}/{len(to_process)}] {c['chat_id']}: wrote {n} rows "
                      f"(running total {total_rows})")

    print(f"Done. Backfilled {total_rows} messages across {len(to_process)} chats.")


if __name__ == "__main__":
    sys.exit(main())
