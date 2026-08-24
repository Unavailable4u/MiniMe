"""
tests/unit/test_eo_routing_memory.py — Patch 7e (content/knowledge group).

eo/routing_memory.py had zero test coverage before this. It's the Stage
4 step 7 feedback loop: log_outcome() always writes the raw "eo:routing_
outcome" record to Redis (Part 7's schema, no network dependency) and
*best-effort* embeds/upserts it into Vector, degrading silently rather
than raising if embedding/Vector isn't configured -- same "a missing
feedback signal is a degradation, not a failure" posture
agents/memory_search.py already uses. retrieve_similar_outcomes()
mirrors that: any embed/query failure returns "" rather than raising.
These tests pin both halves of that contract, plus the id-prefix shape
(ID_PREFIX, so routing outcomes never collide with knowledge_graph.py's
or semantic_cache.py's own ids in the same shared Vector index) and the
metadata-driven text rendering in retrieve_similar_outcomes().

Isolation: routing_memory.py does `from memory.bus import write,
vector_index` and `from utils.embedding import embed_text as _embed`
(bound names in its own namespace) -- tests patch `write`,
`vector_index`, and `_embed` on the routing_memory module object
itself, same gotcha as every other cache/store module in this batch.
fake_bus (conftest, autouse) already makes the real `write()` call
safe even when not explicitly patched, but tests that assert on the
raw record patch `write` directly so they don't depend on FakeRedis's
JSON round-trip.
"""
import pytest

from eo import routing_memory

# ---------------------------------------------------------------------
# Fake Upstash Vector Index harness (matches the shape this module
# actually calls: upsert(vectors=[...]) and query(...))
# ---------------------------------------------------------------------

class FakeMatch:
    def __init__(self, metadata=None):
        self.metadata = metadata


class FakeIndex:
    def __init__(self):
        self.upserted = []
        self.query_result = []
        self.raise_on_upsert = False
        self.raise_on_query = False

    def upsert(self, vectors):
        if self.raise_on_upsert:
            raise RuntimeError("simulated upsert failure")
        self.upserted.append(vectors)

    def query(self, vector, top_k, include_metadata, filter):
        if self.raise_on_query:
            raise RuntimeError("simulated query failure")
        self.last_top_k = top_k
        self.last_filter = filter
        return self.query_result


@pytest.fixture
def fake_index(monkeypatch):
    index = FakeIndex()
    monkeypatch.setattr(routing_memory, "vector_index", lambda: index)
    monkeypatch.setattr(routing_memory, "_embed", lambda text: [0.1, 0.2, 0.3])
    return index


# ---------------------------------------------------------------------
# log_outcome — the raw write always happens
# ---------------------------------------------------------------------

def test_log_outcome_always_writes_the_raw_record_to_the_fixed_key(monkeypatch, fake_index):
    seen = {}
    monkeypatch.setattr(routing_memory, "write", lambda key, value: seen.update({"key": key, "value": value}))

    routing_memory.log_outcome(
        "write a login form",
        {"path": "direct", "directed_task_type": "code", "confidence": 0.8, "panel_reviewed": True},
        outcome="correctly routed",
    )

    assert seen["key"] == "eo:routing_outcome"
    assert seen["value"]["task_text"] == "write a login form"
    assert seen["value"]["path"] == "direct"
    assert seen["value"]["outcome"] == "correctly routed"


def test_log_outcome_pulls_fields_from_the_decision_dict(monkeypatch, fake_index):
    seen = {}
    monkeypatch.setattr(routing_memory, "write", lambda key, value: seen.update({"value": value}))

    routing_memory.log_outcome(
        "task", {"path": "panel", "directed_task_type": "research", "confidence": 0.42, "panel_reviewed": True},
    )

    assert seen["value"]["directed_task_type"] == "research"
    assert seen["value"]["confidence"] == 0.42
    assert seen["value"]["panel_reviewed"] is True


def test_log_outcome_with_no_decision_defaults_fields_to_none_or_false(monkeypatch, fake_index):
    """decision is Optional per the docstring shape -- a None decision
    must not raise, and every decision-derived field falls back to
    None/False rather than blowing up on `.get()` against None."""
    seen = {}
    monkeypatch.setattr(routing_memory, "write", lambda key, value: seen.update({"value": value}))

    routing_memory.log_outcome("task", None, outcome="skipped")

    assert seen["value"]["path"] is None
    assert seen["value"]["directed_task_type"] is None
    assert seen["value"]["confidence"] is None
    assert seen["value"]["panel_reviewed"] is False


def test_log_outcome_defaults_outcome_to_empty_string(monkeypatch, fake_index):
    seen = {}
    monkeypatch.setattr(routing_memory, "write", lambda key, value: seen.update({"value": value}))
    routing_memory.log_outcome("task", {"path": "direct"})
    assert seen["value"]["outcome"] == ""


def test_log_outcome_returns_the_same_record_it_wrote(monkeypatch, fake_index):
    monkeypatch.setattr(routing_memory, "write", lambda key, value: None)
    record = routing_memory.log_outcome("task", {"path": "direct"}, outcome="ok")
    assert record["task_text"] == "task"
    assert record["outcome"] == "ok"


# ---------------------------------------------------------------------
# log_outcome — best-effort Vector embed/upsert
# ---------------------------------------------------------------------

def test_log_outcome_upserts_into_vector_under_the_id_prefix(monkeypatch, fake_index):
    monkeypatch.setattr(routing_memory, "write", lambda key, value: None)
    routing_memory.log_outcome("task", {"path": "direct"}, outcome="ok")

    assert len(fake_index.upserted) == 1
    vectors = fake_index.upserted[0]
    vec_id, vector, metadata = vectors[0]
    assert vec_id.startswith(f"{routing_memory.ID_PREFIX}:")
    assert metadata["outcome"] == "ok"


def test_log_outcome_still_returns_the_record_when_vector_upsert_fails(monkeypatch, fake_index):
    """The write() call and its return value must never be at the mercy
    of Vector being unavailable -- per the module's own docstring, this
    is a 'skipped, not raised' degradation."""
    monkeypatch.setattr(routing_memory, "write", lambda key, value: None)
    fake_index.raise_on_upsert = True

    record = routing_memory.log_outcome("task", {"path": "direct"}, outcome="ok")

    assert record["task_text"] == "task"
    assert fake_index.upserted == []


def test_log_outcome_still_writes_raw_record_when_embed_itself_fails(monkeypatch, fake_index):
    def boom(text):
        raise RuntimeError("HF unavailable")
    monkeypatch.setattr(routing_memory, "_embed", boom)

    seen = {}
    monkeypatch.setattr(routing_memory, "write", lambda key, value: seen.update({"value": value}))

    record = routing_memory.log_outcome("task", {"path": "direct"}, outcome="ok")

    assert seen["value"]["task_text"] == "task"
    assert record["outcome"] == "ok"
    assert fake_index.upserted == []


# ---------------------------------------------------------------------
# retrieve_similar_outcomes
# ---------------------------------------------------------------------

def test_retrieve_similar_outcomes_returns_empty_string_when_no_results(fake_index):
    fake_index.query_result = []
    assert routing_memory.retrieve_similar_outcomes("some task") == ""


def test_retrieve_similar_outcomes_renders_one_line_per_match(fake_index):
    fake_index.query_result = [
        FakeMatch(metadata={"task_text": "reverse a string", "path": "direct", "outcome": "correctly routed"}),
        FakeMatch(metadata={"task_text": "build a CRUD app", "path": "panel", "outcome": "under-routed, needed panel review"}),
    ]
    result = routing_memory.retrieve_similar_outcomes("some task")
    lines = result.split("\n")
    assert len(lines) == 2
    assert "reverse a string" in lines[0]
    assert "routed path direct" in lines[0]
    assert "correctly routed" in lines[0]
    assert "under-routed, needed panel review" in lines[1]


def test_retrieve_similar_outcomes_skips_matches_with_no_metadata(fake_index):
    class NoMetaMatch:
        metadata = None

    fake_index.query_result = [NoMetaMatch()]
    assert routing_memory.retrieve_similar_outcomes("some task") == ""


def test_retrieve_similar_outcomes_passes_top_k_through_to_the_query(fake_index):
    fake_index.query_result = []
    routing_memory.retrieve_similar_outcomes("some task", top_k=7)
    assert fake_index.last_top_k == 7


def test_retrieve_similar_outcomes_filters_out_blank_outcome_entries(fake_index):
    """The query filter itself ("outcome != ''") is what keeps
    not-yet-judged records out of retrieval results -- pin that the
    filter string is actually passed through, since a typo here would
    silently return every outcome (including blank ones) instead."""
    fake_index.query_result = []
    routing_memory.retrieve_similar_outcomes("some task")
    assert fake_index.last_filter == "outcome != ''"


def test_retrieve_similar_outcomes_returns_empty_string_when_embed_fails(monkeypatch, fake_index):
    def boom(text):
        raise RuntimeError("HF unavailable")
    monkeypatch.setattr(routing_memory, "_embed", boom)
    assert routing_memory.retrieve_similar_outcomes("some task") == ""


def test_retrieve_similar_outcomes_returns_empty_string_when_query_fails(fake_index):
    fake_index.raise_on_query = True
    assert routing_memory.retrieve_similar_outcomes("some task") == ""
