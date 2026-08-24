"""
tests/unit/test_eo_knowledge_graph.py — Patch 7e (content/knowledge group).

eo/knowledge_graph.py had zero test coverage before this. It's the
only top-level id prefix ("node:{workspace_id}:{node_id}") sharing the
same Upstash Vector index as agents/memory_search.py's "cyclemem:..."
ids and eo/semantic_cache.py's own entries -- per the module's own
docstring, the whole reason for a dedicated prefix is so a query here
never accidentally matches either of those. That makes the id-shape
helpers and workspace-scoping the highest-value things to pin down,
followed by the "never hard-fail, degrade to None/[]" posture every
public function documents for itself when the underlying Vector/HF
call fails.

Isolation: knowledge_graph.py does `from memory.bus import
vector_index` and `from utils.llm_client import log_usage, embed_text`
(bound names in its own namespace), so tests patch `vector_index`,
`embed_text`, and `log_usage` on the knowledge_graph module object
itself, same gotcha as every other cache/store module in this batch.
`vector_index()` returns something shaped like an upstash_vector.Index
-- FakeIndex/FakeMatch below are minimal stand-ins for just the surface
this module actually calls (fetch/upsert/delete/update/query/range),
never a real Upstash connection.
"""
import pytest

from eo import knowledge_graph

# ---------------------------------------------------------------------
# Fake Upstash Vector Index harness
# ---------------------------------------------------------------------

class FakeMatch:
    def __init__(self, id, metadata=None, score=None, vector=None):
        self.id = id
        self.metadata = metadata
        self.score = score
        self.vector = vector


class FakePage:
    def __init__(self, vectors, next_cursor=""):
        self.vectors = vectors
        self.next_cursor = next_cursor


class FakeIndex:
    """Records calls; hands back canned/queued results. Only implements
    the methods knowledge_graph.py actually calls."""

    def __init__(self):
        self.upserted = []
        self.deleted_ids = []
        self.updated = []
        self.fetch_result = []
        self.query_result = []
        self.pages = []  # list of FakePage, consumed in order by range()
        self.raise_on = set()  # method names that should raise

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise RuntimeError(f"simulated {name} failure")

    def upsert(self, vectors):
        self._maybe_raise("upsert")
        self.upserted.append(vectors)

    def fetch(self, ids, include_metadata=True):
        self._maybe_raise("fetch")
        return self.fetch_result

    def delete(self, ids):
        self._maybe_raise("delete")
        self.deleted_ids.extend(ids)

    def update(self, id, metadata, metadata_update_mode):
        self._maybe_raise("update")
        self.updated.append((id, metadata, metadata_update_mode))
        return True

    def query(self, vector, top_k, include_metadata, filter):
        self._maybe_raise("query")
        self.last_query_filter = filter
        self.last_query_top_k = top_k
        return self.query_result

    def range(self, cursor, limit, include_metadata, include_vectors):
        self._maybe_raise("range")
        page = self.pages.pop(0)
        return page


@pytest.fixture
def fake_index(monkeypatch):
    index = FakeIndex()
    monkeypatch.setattr(knowledge_graph, "vector_index", lambda: index)
    monkeypatch.setattr(knowledge_graph, "embed_text", lambda text: [0.1, 0.2, 0.3])
    monkeypatch.setattr(knowledge_graph, "log_usage", lambda *a, **k: None)
    return index


# ---------------------------------------------------------------------
# _node_vector_id
# ---------------------------------------------------------------------

def test_node_vector_id_shape():
    assert knowledge_graph._node_vector_id("ws_1", "n1") == "node:ws_1:n1"


# ---------------------------------------------------------------------
# write_node
# ---------------------------------------------------------------------

def test_write_node_rejects_an_unknown_node_type(fake_index):
    with pytest.raises(ValueError):
        knowledge_graph.write_node("ws_1", "notes", "bogus_type", "Title", "content", "user")


def test_write_node_returns_the_node_id_on_success(fake_index):
    node_id = knowledge_graph.write_node(
        "ws_1", "notes", "note", "My Title", "some content", "user", node_id="n1",
    )
    assert node_id == "n1"


def test_write_node_generates_a_node_id_when_none_given(fake_index):
    node_id = knowledge_graph.write_node("ws_1", "notes", "note", "Title", "content", "user")
    assert node_id is not None
    assert len(node_id) == 12


def test_write_node_upserts_with_the_workspace_scoped_vector_id(fake_index):
    knowledge_graph.write_node("ws_1", "notes", "note", "Title", "content", "user", node_id="n1")
    [vectors] = fake_index.upserted
    [(vector_id, _vector, _metadata)] = vectors
    assert vector_id == "node:ws_1:n1"


def test_write_node_stores_required_metadata_fields(fake_index):
    knowledge_graph.write_node(
        "ws_1", "research", "source", "A Source", "the content", "researcher",
        tags=["important"], node_id="n1",
    )
    [vectors] = fake_index.upserted
    [(_vector_id, _vector, metadata)] = vectors
    assert metadata["workspace_id"] == "ws_1"
    assert metadata["section"] == "research"
    assert metadata["node_type"] == "source"
    assert metadata["created_by"] == "researcher"
    assert metadata["title"] == "A Source"
    assert metadata["content"] == "the content"
    assert metadata["tags"] == ["important"]


def test_write_node_defaults_tags_to_empty_list(fake_index):
    knowledge_graph.write_node("ws_1", "notes", "note", "Title", "content", "user", node_id="n1")
    [vectors] = fake_index.upserted
    [(_vector_id, _vector, metadata)] = vectors
    assert metadata["tags"] == []


def test_write_node_omits_session_id_when_not_given(fake_index):
    knowledge_graph.write_node("ws_1", "notes", "note", "Title", "content", "user", node_id="n1")
    [vectors] = fake_index.upserted
    [(_vector_id, _vector, metadata)] = vectors
    assert "session_id" not in metadata


def test_write_node_includes_session_id_when_given(fake_index):
    knowledge_graph.write_node(
        "ws_1", "notes", "source", "Title", "content", "user",
        node_id="n1", session_id="sess-1",
    )
    [vectors] = fake_index.upserted
    [(_vector_id, _vector, metadata)] = vectors
    assert metadata["session_id"] == "sess-1"


def test_write_node_returns_none_when_embedding_fails(fake_index, monkeypatch):
    def boom(text):
        raise RuntimeError("embed service down")
    monkeypatch.setattr(knowledge_graph, "embed_text", boom)

    result = knowledge_graph.write_node("ws_1", "notes", "note", "Title", "content", "user")

    assert result is None
    assert fake_index.upserted == []


def test_write_node_returns_none_when_upsert_fails(fake_index):
    fake_index.raise_on.add("upsert")
    result = knowledge_graph.write_node("ws_1", "notes", "note", "Title", "content", "user")
    assert result is None


# ---------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------

def test_get_node_returns_the_merged_metadata_on_a_hit(fake_index):
    fake_index.fetch_result = [FakeMatch("node:ws_1:n1", metadata={"title": "Title", "content": "c"})]

    result = knowledge_graph.get_node("ws_1", "n1")

    assert result["node_id"] == "n1"
    assert result["vector_id"] == "node:ws_1:n1"
    assert result["title"] == "Title"


def test_get_node_returns_none_when_fetch_result_is_empty(fake_index):
    fake_index.fetch_result = []
    assert knowledge_graph.get_node("ws_1", "n1") is None


def test_get_node_returns_none_when_match_has_no_metadata(fake_index):
    fake_index.fetch_result = [FakeMatch("node:ws_1:n1", metadata=None)]
    assert knowledge_graph.get_node("ws_1", "n1") is None


def test_get_node_returns_none_when_fetch_raises(fake_index):
    fake_index.raise_on.add("fetch")
    assert knowledge_graph.get_node("ws_1", "n1") is None


# ---------------------------------------------------------------------
# delete_node
# ---------------------------------------------------------------------

def test_delete_node_deletes_the_workspace_scoped_vector_id(fake_index):
    knowledge_graph.delete_node("ws_1", "n1")
    assert fake_index.deleted_ids == ["node:ws_1:n1"]


def test_delete_node_reraises_on_failure(fake_index):
    fake_index.raise_on.add("delete")
    with pytest.raises(RuntimeError):
        knowledge_graph.delete_node("ws_1", "n1")


# ---------------------------------------------------------------------
# rename_node
# ---------------------------------------------------------------------

def test_rename_node_patches_only_the_title(fake_index):
    from upstash_vector.types import MetadataUpdateMode

    result = knowledge_graph.rename_node("ws_1", "n1", "New Title")

    assert result is True
    [(vector_id, metadata, mode)] = fake_index.updated
    assert vector_id == "node:ws_1:n1"
    assert metadata == {"title": "New Title"}
    assert mode == MetadataUpdateMode.PATCH


def test_rename_node_returns_false_on_failure(fake_index):
    fake_index.raise_on.add("update")
    assert knowledge_graph.rename_node("ws_1", "n1", "New Title") is False


# ---------------------------------------------------------------------
# search_nodes
# ---------------------------------------------------------------------

def test_search_nodes_returns_empty_list_when_embedding_fails(fake_index, monkeypatch):
    def boom(text):
        raise RuntimeError("embed down")
    monkeypatch.setattr(knowledge_graph, "embed_text", boom)

    assert knowledge_graph.search_nodes("ws_1", "query") == []


def test_search_nodes_returns_empty_list_when_query_fails(fake_index):
    fake_index.raise_on.add("query")
    assert knowledge_graph.search_nodes("ws_1", "query") == []


def test_search_nodes_always_scopes_the_filter_to_the_workspace(fake_index):
    fake_index.query_result = []
    knowledge_graph.search_nodes("ws_1", "query")
    assert "workspace_id = 'ws_1'" in fake_index.last_query_filter


def test_search_nodes_adds_section_and_node_type_filters_when_given(fake_index):
    fake_index.query_result = []
    knowledge_graph.search_nodes("ws_1", "query", section="notes", node_type="note")
    assert "section = 'notes'" in fake_index.last_query_filter
    assert "node_type = 'note'" in fake_index.last_query_filter


def test_search_nodes_maps_matches_into_node_dicts(fake_index):
    fake_index.query_result = [
        FakeMatch("node:ws_1:n1", metadata={"title": "A", "tags": []}, score=0.95),
    ]

    results = knowledge_graph.search_nodes("ws_1", "query")

    assert results == [{"node_id": "n1", "vector_id": "node:ws_1:n1", "score": 0.95,
                         "title": "A", "tags": []}]


def test_search_nodes_skips_matches_with_no_metadata(fake_index):
    fake_index.query_result = [
        FakeMatch("node:ws_1:n1", metadata=None, score=0.95),
        FakeMatch("node:ws_1:n2", metadata={"title": "B", "tags": []}, score=0.9),
    ]

    results = knowledge_graph.search_nodes("ws_1", "query")

    assert len(results) == 1
    assert results[0]["node_id"] == "n2"


def test_search_nodes_tag_filter_keeps_only_matches_with_an_overlapping_tag(fake_index):
    fake_index.query_result = [
        FakeMatch("node:ws_1:n1", metadata={"title": "A", "tags": ["urgent"]}, score=0.9),
        FakeMatch("node:ws_1:n2", metadata={"title": "B", "tags": ["archive"]}, score=0.8),
    ]

    results = knowledge_graph.search_nodes("ws_1", "query", tags=["urgent"])

    assert [r["node_id"] for r in results] == ["n1"]


def test_search_nodes_with_no_tags_filter_keeps_everything(fake_index):
    fake_index.query_result = [
        FakeMatch("node:ws_1:n1", metadata={"title": "A", "tags": ["urgent"]}, score=0.9),
        FakeMatch("node:ws_1:n2", metadata={"title": "B", "tags": []}, score=0.8),
    ]

    results = knowledge_graph.search_nodes("ws_1", "query")

    assert len(results) == 2


# ---------------------------------------------------------------------
# list_nodes
# ---------------------------------------------------------------------

def test_list_nodes_pages_through_range_until_cursor_is_empty(fake_index):
    fake_index.pages = [
        FakePage([FakeMatch("node:ws_1:n1", metadata={"workspace_id": "ws_1"})], next_cursor="cursor2"),
        FakePage([FakeMatch("node:ws_1:n2", metadata={"workspace_id": "ws_1"})], next_cursor=""),
    ]

    results = knowledge_graph.list_nodes("ws_1")

    assert [n["node_id"] for n in results] == ["n1", "n2"]


def test_list_nodes_filters_out_vectors_from_other_workspaces(fake_index):
    fake_index.pages = [
        FakePage([
            FakeMatch("node:ws_1:n1", metadata={"workspace_id": "ws_1"}),
            FakeMatch("node:ws_2:n2", metadata={"workspace_id": "ws_2"}),
        ], next_cursor=""),
    ]

    results = knowledge_graph.list_nodes("ws_1")

    assert [n["node_id"] for n in results] == ["n1"]


def test_list_nodes_filters_by_node_type_when_given(fake_index):
    fake_index.pages = [
        FakePage([
            FakeMatch("node:ws_1:n1", metadata={"workspace_id": "ws_1", "node_type": "note"}),
            FakeMatch("node:ws_1:n2", metadata={"workspace_id": "ws_1", "node_type": "source"}),
        ], next_cursor=""),
    ]

    results = knowledge_graph.list_nodes("ws_1", node_type="note")

    assert [n["node_id"] for n in results] == ["n1"]


def test_list_nodes_omits_vector_field_by_default(fake_index):
    fake_index.pages = [
        FakePage([FakeMatch("node:ws_1:n1", metadata={"workspace_id": "ws_1"}, vector=[1, 2, 3])],
                 next_cursor=""),
    ]

    [node] = knowledge_graph.list_nodes("ws_1")

    assert "vector" not in node


def test_list_nodes_includes_vector_field_when_requested(fake_index):
    fake_index.pages = [
        FakePage([FakeMatch("node:ws_1:n1", metadata={"workspace_id": "ws_1"}, vector=[1, 2, 3])],
                 next_cursor=""),
    ]

    [node] = knowledge_graph.list_nodes("ws_1", include_vectors=True)

    assert node["vector"] == [1, 2, 3]


def test_list_nodes_returns_partial_results_on_a_mid_scan_failure(monkeypatch):
    """A Vector hiccup partway through paging must keep what was
    already collected rather than losing it -- same posture the
    module's own docstring documents."""
    good_page = FakePage([FakeMatch("node:ws_1:n1", metadata={"workspace_id": "ws_1"})],
                          next_cursor="cursor2")

    class FailingIndex(FakeIndex):
        def range(self, cursor, limit, include_metadata, include_vectors):
            if cursor == "":
                return good_page
            raise RuntimeError("simulated mid-scan failure")

    monkeypatch.setattr(knowledge_graph, "vector_index", lambda: FailingIndex())

    results = knowledge_graph.list_nodes("ws_1")

    assert [n["node_id"] for n in results] == ["n1"]


def test_list_nodes_returns_empty_list_when_first_range_call_fails(monkeypatch):
    class FailingIndex(FakeIndex):
        def range(self, cursor, limit, include_metadata, include_vectors):
            raise RuntimeError("simulated failure")

    monkeypatch.setattr(knowledge_graph, "vector_index", lambda: FailingIndex())

    assert knowledge_graph.list_nodes("ws_1") == []
