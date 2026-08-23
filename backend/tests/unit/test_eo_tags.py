"""
tests/unit/test_eo_tags.py — Patch 7e-1 (part of it).

eo/tags.py had zero test coverage before this, and zero callers anywhere
else in the codebase -- which is exactly why nobody noticed both public
functions here raised TypeError unconditionally: they called
chat_workspace.get_workspace()/chat_store.chat_exists()/
chat_store.get_chat()/chat_store.list_chats_by_tag() with the pre-RLS
arity (no user_id/owner_id), which none of those functions have taken
since the Postgres/RLS migration. See tags.py's own module docstring for
the full bugfix writeup.

These tests exercise the FIXED module: user_id/owner_id required and
threaded through on every call, plus the actual crash this module shipped
with, pinned as a regression test so this can't silently regress back to
the broken arity.

Isolation: every chat_workspace/chat_store/knowledge_graph call is
monkeypatched directly on the eo.tags module object (tags.py does
`from eo import chat_store` etc., so patching eo.chat_store.foo would NOT
reach the bound name tags.py actually calls -- same gotcha conftest.py
documents for generate_text). No real DB or vector store involved.
"""
import pytest

import eo.tags as tags


# ---------------------------------------------------------------------
# distinct_tags_for_workspace
# ---------------------------------------------------------------------

def test_distinct_tags_for_workspace_requires_user_id():
    """Regression test for the original bug: calling with only a
    workspace_id (the pre-fix call shape) must fail fast at the Python
    level (TypeError: missing argument), not reach chat_workspace and
    blow up there with a less obvious traceback."""
    with pytest.raises(TypeError):
        tags.distinct_tags_for_workspace("ws_1")


def test_distinct_tags_for_workspace_passes_user_id_to_get_workspace(monkeypatch):
    seen = {}

    def fake_get_workspace(ws_id, user_id):
        seen["ws_id"] = ws_id
        seen["user_id"] = user_id
        raise FileNotFoundError(ws_id)

    monkeypatch.setattr(tags.chat_workspace, "get_workspace", fake_get_workspace)
    monkeypatch.setattr(tags.knowledge_graph, "search_nodes", lambda *a, **k: [])

    tags.distinct_tags_for_workspace("ws_1", "user_1")

    assert seen == {"ws_id": "ws_1", "user_id": "user_1"}


def test_distinct_tags_for_workspace_unknown_workspace_returns_only_node_tags(monkeypatch):
    """get_workspace() raising FileNotFoundError (workspace doesn't
    exist, or this user has no access -- same exception either way per
    _require_access()) must not propagate; the function falls back to
    node-only tags rather than crashing or returning early with nothing."""
    monkeypatch.setattr(
        tags.chat_workspace, "get_workspace",
        lambda ws_id, user_id: (_ for _ in ()).throw(FileNotFoundError(ws_id)),
    )
    monkeypatch.setattr(
        tags.knowledge_graph, "search_nodes",
        lambda *a, **k: [{"tags": ["from-node"]}],
    )

    result = tags.distinct_tags_for_workspace("ws_missing", "user_1")

    assert result == ["from-node"]


def test_distinct_tags_for_workspace_merges_and_dedupes_chat_and_node_tags(monkeypatch):
    monkeypatch.setattr(
        tags.chat_workspace, "get_workspace",
        lambda ws_id, user_id: {"chat_ids": ["c1", "c2"]},
    )
    monkeypatch.setattr(tags.chat_store, "chat_exists", lambda chat_id, user_id: True)

    chats = {
        "c1": {"tags": ["alpha", "beta"]},
        "c2": {"tags": ["beta", "gamma"]},
    }
    monkeypatch.setattr(tags.chat_store, "get_chat", lambda chat_id, user_id: chats[chat_id])
    monkeypatch.setattr(
        tags.knowledge_graph, "search_nodes",
        lambda *a, **k: [{"tags": ["gamma", "delta"]}],
    )

    result = tags.distinct_tags_for_workspace("ws_1", "user_1")

    assert result == ["alpha", "beta", "delta", "gamma"]  # sorted, deduped


def test_distinct_tags_for_workspace_skips_chats_that_dont_exist(monkeypatch):
    """chat_exists() gating a stale chat_id (deleted chat still linked
    in the workspace's chat_ids array) must skip straight past it
    without calling get_chat() at all -- get_chat() left un-mocked would
    raise/None-explode if it were reached for c_deleted."""
    monkeypatch.setattr(
        tags.chat_workspace, "get_workspace",
        lambda ws_id, user_id: {"chat_ids": ["c_live", "c_deleted"]},
    )
    monkeypatch.setattr(
        tags.chat_store, "chat_exists",
        lambda chat_id, user_id: chat_id == "c_live",
    )
    monkeypatch.setattr(
        tags.chat_store, "get_chat",
        lambda chat_id, user_id: {"tags": ["live-tag"]} if chat_id == "c_live" else pytest.fail(
            "get_chat() must not be called for a chat that doesn't exist"
        ),
    )
    monkeypatch.setattr(tags.knowledge_graph, "search_nodes", lambda *a, **k: [])

    result = tags.distinct_tags_for_workspace("ws_1", "user_1")

    assert result == ["live-tag"]


def test_distinct_tags_for_workspace_chat_with_no_tags_field_does_not_raise(monkeypatch):
    """get_chat() returning a chat dict with no "tags" key (or an
    explicit None) must fall back to an empty list, not raise on
    NoneType being iterated/unioned."""
    monkeypatch.setattr(
        tags.chat_workspace, "get_workspace",
        lambda ws_id, user_id: {"chat_ids": ["c1"]},
    )
    monkeypatch.setattr(tags.chat_store, "chat_exists", lambda chat_id, user_id: True)
    monkeypatch.setattr(tags.chat_store, "get_chat", lambda chat_id, user_id: {"tags": None})
    monkeypatch.setattr(tags.knowledge_graph, "search_nodes", lambda *a, **k: [])

    assert tags.distinct_tags_for_workspace("ws_1", "user_1") == []


def test_distinct_tags_for_workspace_passes_workspace_id_as_query_text(monkeypatch):
    """search_nodes()'s query_text is documented as "just the
    workspace_id itself" -- pin that down explicitly since it's easy to
    accidentally pass something else (e.g. a real search string) during
    a future refactor."""
    seen = {}

    def fake_search_nodes(workspace_id, query_text, top_k, **kwargs):
        seen["workspace_id"] = workspace_id
        seen["query_text"] = query_text
        seen["top_k"] = top_k
        return []

    monkeypatch.setattr(
        tags.chat_workspace, "get_workspace",
        lambda ws_id, user_id: (_ for _ in ()).throw(FileNotFoundError(ws_id)),
    )
    monkeypatch.setattr(tags.knowledge_graph, "search_nodes", fake_search_nodes)

    tags.distinct_tags_for_workspace("ws_42", "user_1")

    assert seen["workspace_id"] == "ws_42"
    assert seen["query_text"] == "ws_42"
    assert seen["top_k"] == tags.NODE_TAG_SAMPLE_SIZE


def test_distinct_tags_for_workspace_node_with_no_tags_field_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        tags.chat_workspace, "get_workspace",
        lambda ws_id, user_id: (_ for _ in ()).throw(FileNotFoundError(ws_id)),
    )
    monkeypatch.setattr(
        tags.knowledge_graph, "search_nodes",
        lambda *a, **k: [{"node_id": "n1"}],  # no "tags" key at all
    )

    assert tags.distinct_tags_for_workspace("ws_1", "user_1") == []


# ---------------------------------------------------------------------
# chats_with_tag
# ---------------------------------------------------------------------

def test_chats_with_tag_requires_owner_id():
    """Same regression coverage as distinct_tags_for_workspace: the
    pre-fix single-arg call shape must fail fast with TypeError."""
    with pytest.raises(TypeError):
        tags.chats_with_tag("launch")


def test_chats_with_tag_forwards_owner_id_and_tag_in_order(monkeypatch):
    seen = {}

    def fake_list_chats_by_tag(owner_id, tag):
        seen["owner_id"] = owner_id
        seen["tag"] = tag
        return [{"id": "c1", "tags": [tag]}]

    monkeypatch.setattr(tags.chat_store, "list_chats_by_tag", fake_list_chats_by_tag)

    result = tags.chats_with_tag("Q3-launch", "user_1")

    assert seen == {"owner_id": "user_1", "tag": "Q3-launch"}
    assert result == [{"id": "c1", "tags": ["Q3-launch"]}]


def test_chats_with_tag_returns_empty_list_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(tags.chat_store, "list_chats_by_tag", lambda owner_id, tag: [])

    assert tags.chats_with_tag("nonexistent-tag", "user_1") == []
