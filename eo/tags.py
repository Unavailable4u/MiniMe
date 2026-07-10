"""
eo/tags.py — unified tagging convention (Part 0 §0.4).

Tags are NOT their own subsystem — per the blueprint, they're just a
field riding along on things already stored elsewhere:
  - a node's `tags` metadata (eo/knowledge_graph.py's write_node(), and
    filterable via search_nodes(tags=[...]) — already wired in §0.1)
  - a chat's `tags` field (eo/chat_store.py, added this section)

This module is the read-side convenience layer on top of both: "what
tags exist in this workspace" (autocomplete) and "give me everything
tagged X" (the chat-side half of global search's tag filter). No
separate tag registry — the blueprint is explicit that one shouldn't be
pre-built before there's evidence a scan-and-dedupe approach is too
slow, and at workspace scale (dozens to low hundreds of chats/nodes)
it isn't.
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo import chat_store
from eo import chat_workspace
from eo import knowledge_graph

# Upstash Vector has no cheap "list everything" op, so collecting node
# tags is a similarity query with a broad top_k rather than an exhaustive
# scan — an approximation, same tradeoff eo/knowledge_graph.py's own
# search_nodes() already accepts for doing tag filtering client-side
# instead of as a first-class Vector filter clause.
NODE_TAG_SAMPLE_SIZE = 200


def distinct_tags_for_workspace(workspace_id: str) -> list[str]:
    """Every distinct tag seen so far in this workspace, across both
    chats (linked into the workspace via eo/chat_workspace.py) and nodes
    (eo/knowledge_graph.py). This is what a tag-input autocomplete calls
    — no separate registry to keep in sync, just scan-and-dedupe on read."""
    tags = set()

    try:
        ws = chat_workspace.get_workspace(workspace_id)
    except FileNotFoundError:
        ws = None
    if ws:
        for chat_id in ws["chat_ids"]:
            if chat_store.chat_exists(chat_id):
                tags.update(chat_store.get_chat(chat_id).get("tags") or [])

    # query_text is just the workspace_id itself — the query text barely
    # matters here since every result is going to be inspected for its
    # `tags` metadata regardless of similarity rank; it only needs to be
    # SOME text so embed_text() has something to embed.
    for node in knowledge_graph.search_nodes(
        workspace_id, query_text=workspace_id, top_k=NODE_TAG_SAMPLE_SIZE,
    ):
        tags.update(node.get("tags") or [])

    return sorted(tags)


def chats_with_tag(tag: str) -> list[dict]:
    """Every chat (any workspace) carrying this exact tag — the chat-side
    half of "tag Q3-launch shows up everywhere at once." The node-side
    half is eo/knowledge_graph.py's search_nodes(tags=[tag])."""
    return chat_store.list_chats_by_tag(tag)