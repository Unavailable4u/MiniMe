"""
tests/unit/test_eo_semantic_cache.py — Patch 7e (content/knowledge group).

eo/semantic_cache.py had zero test coverage before this despite bundling
two independently risky behaviors: (1) the trust model that decides
whether a near-match is replayed blindly vs. re-verified against a
context fingerprint vs. re-verified with an LLM call vs. dropped
entirely, and (2) app/workspace scoping, whose entire purpose is
keeping a build-pipeline's cached answers and a notebook's cached
answers from ever leaking into or purging each other. A bug in either
is the kind that "fails silently and expensively" -- a stale or
wrongly-scoped answer gets served with no error anywhere. These tests
pin both.

Isolation: semantic_cache.py does `from memory.bus import vector_index`,
`from utils.llm_client import embed_text, generate_text`, and `from
relay.emitter import emit_event` (all bound names in its own
namespace) -- tests patch `vector_index`, `embed_text`, `generate_text`,
and `emit_event` on the semantic_cache module object itself, same
gotcha as every other cache/store module in this batch.
"""
import time

import pytest

from eo import semantic_cache

# ---------------------------------------------------------------------
# Fake Upstash Vector Index harness
# ---------------------------------------------------------------------

class FakeMatch:
    def __init__(self, score, metadata=None, id="fake-id"):
        self.score = score
        self.metadata = metadata or {}
        self.id = id


class FakeIndex:
    def __init__(self):
        self.query_result = []
        self.upserted = []
        self.deleted_ids = []
        self.last_query_filter = None
        self.last_query_top_k = None

    def query(self, vector, top_k, include_metadata, filter):
        self.last_query_filter = filter
        self.last_query_top_k = top_k
        return self.query_result

    def upsert(self, vectors):
        self.upserted.append(vectors)

    def delete(self, ids):
        self.deleted_ids.extend(ids)


@pytest.fixture
def fake_index(monkeypatch):
    index = FakeIndex()
    monkeypatch.setattr(semantic_cache, "vector_index", lambda: index)
    monkeypatch.setattr(semantic_cache, "embed_text", lambda text: [0.1, 0.2, 0.3])
    monkeypatch.setattr(semantic_cache, "emit_event", lambda *a, **k: None)
    return index


# ---------------------------------------------------------------------
# _scope_filter / _scope_metadata — app vs. workspace vs. legacy global
# ---------------------------------------------------------------------

def test_scope_filter_app_scope():
    assert semantic_cache._scope_filter("app", "my-app") == "app = 'my-app'"


def test_scope_filter_workspace_scope():
    assert semantic_cache._scope_filter("workspace", "ws-1") == "workspace = 'ws-1'"


def test_scope_filter_falls_back_to_legacy_global_bucket_when_no_scope():
    assert semantic_cache._scope_filter(None, None) == "project = 'global'"


def test_scope_metadata_matches_scope_filter_shape():
    assert semantic_cache._scope_metadata("app", "my-app") == {"app": "my-app"}
    assert semantic_cache._scope_metadata("workspace", "ws-1") == {"workspace": "ws-1"}
    assert semantic_cache._scope_metadata(None, None) == {"project": "global"}


# ---------------------------------------------------------------------
# _fingerprint
# ---------------------------------------------------------------------

def test_fingerprint_is_deterministic_for_the_same_text():
    assert semantic_cache._fingerprint("some context") == semantic_cache._fingerprint("some context")


def test_fingerprint_differs_for_different_text():
    assert semantic_cache._fingerprint("context A") != semantic_cache._fingerprint("context B")


def test_fingerprint_normalizes_surrounding_whitespace():
    """Two context strings differing only in leading/trailing whitespace
    must fingerprint identically -- otherwise trivial re-formatting of
    the same context would spuriously force a re-verify LLM call."""
    assert semantic_cache._fingerprint("  same text  ") == semantic_cache._fingerprint("same text")


def test_fingerprint_treats_none_the_same_as_empty_string():
    assert semantic_cache._fingerprint(None) == semantic_cache._fingerprint("")


# ---------------------------------------------------------------------
# check_cache — no match / below threshold / expired / empty answer
# ---------------------------------------------------------------------

def test_check_cache_returns_none_when_index_has_no_results(fake_index):
    fake_index.query_result = []
    assert semantic_cache.check_cache("some question") is None


def test_check_cache_returns_none_when_top_match_is_below_similarity_threshold(fake_index):
    fake_index.query_result = [FakeMatch(score=semantic_cache.SIMILARITY_THRESHOLD - 0.01,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("")})]
    assert semantic_cache.check_cache("some question") is None


def test_check_cache_returns_none_when_entry_has_expired(fake_index):
    stale_time = time.time() - semantic_cache.CACHE_TTL_SECONDS - 1
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": stale_time,
                                                    "context_fingerprint": semantic_cache._fingerprint("")})]
    assert semantic_cache.check_cache("some question") is None


def test_check_cache_returns_none_when_metadata_has_no_answer(fake_index):
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("")})]
    assert semantic_cache.check_cache("some question") is None


def test_check_cache_returns_none_when_embed_raises(monkeypatch, fake_index):
    def boom(text):
        raise RuntimeError("HF unavailable")
    monkeypatch.setattr(semantic_cache, "embed_text", boom)
    assert semantic_cache.check_cache("some question") is None


# ---------------------------------------------------------------------
# check_cache — trust model: fingerprint match replays blindly,
# fingerprint mismatch escalates to LLM verification
# ---------------------------------------------------------------------

def test_check_cache_replays_blindly_when_context_fingerprint_is_unchanged(monkeypatch, fake_index):
    """A hit whose stored context_fingerprint matches the CURRENT
    context must be served without any verification LLM call -- the
    whole point of storing the fingerprint in the first place."""
    fp = semantic_cache._fingerprint("same context")
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": fp})]
    verify_called = []
    monkeypatch.setattr(semantic_cache, "generate_text", lambda **k: verify_called.append(1) or "YES")

    result = semantic_cache.check_cache("some question", context_text="same context")

    assert result == "cached answer"
    assert verify_called == []


def test_check_cache_escalates_to_verification_when_fingerprint_differs(monkeypatch, fake_index):
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("old context")})]
    monkeypatch.setattr(semantic_cache, "generate_text", lambda **k: "YES")

    result = semantic_cache.check_cache("some question", context_text="new context")
    assert result == "cached answer"


def test_check_cache_returns_none_when_verification_says_no(monkeypatch, fake_index):
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("old context")})]
    monkeypatch.setattr(semantic_cache, "generate_text", lambda **k: "NO")

    assert semantic_cache.check_cache("some question", context_text="new context") is None


def test_check_cache_treats_verification_call_failure_as_not_accurate(monkeypatch, fake_index):
    """_verify_still_accurate()'s own 'when in doubt, say NO' contract
    must hold even when the verification call itself errors -- fail
    closed (miss), not open (stale replay)."""
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("old context")})]

    def boom(**k):
        raise RuntimeError("LLM unavailable")
    monkeypatch.setattr(semantic_cache, "generate_text", boom)

    assert semantic_cache.check_cache("some question", context_text="new context") is None


def test_check_cache_missing_stored_fingerprint_also_escalates_to_verification(monkeypatch, fake_index):
    """A legacy entry with no context_fingerprint at all (written before
    this trust model existed) must not be treated as a fingerprint
    match -- it should still go through verification rather than being
    replayed blindly."""
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time()})]
    monkeypatch.setattr(semantic_cache, "generate_text", lambda **k: "YES")

    assert semantic_cache.check_cache("some question", context_text="anything") == "cached answer"


# ---------------------------------------------------------------------
# check_cache — scope filter selection (app vs. workspace vs. neither)
# ---------------------------------------------------------------------

def test_check_cache_queries_under_app_scope_when_app_slug_given(fake_index):
    fake_index.query_result = []
    semantic_cache.check_cache("q", app_slug="my-app")
    assert fake_index.last_query_filter == "app = 'my-app'"


def test_check_cache_queries_under_workspace_scope_when_workspace_id_given(fake_index):
    fake_index.query_result = []
    semantic_cache.check_cache("q", workspace_id="ws-1")
    assert fake_index.last_query_filter == "workspace = 'ws-1'"


def test_check_cache_queries_the_legacy_global_bucket_when_neither_given(fake_index):
    fake_index.query_result = []
    semantic_cache.check_cache("q")
    assert fake_index.last_query_filter == "project = 'global'"


# ---------------------------------------------------------------------
# check_cache — cache_hit event emission
# ---------------------------------------------------------------------

def test_check_cache_emits_cache_hit_event_on_a_fingerprint_match(monkeypatch, fake_index):
    fp = semantic_cache._fingerprint("same context")
    fake_index.query_result = [FakeMatch(score=0.97,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": fp})]
    events = []
    monkeypatch.setattr(semantic_cache, "emit_event",
                         lambda name, session_id=None, agent=None, payload=None: events.append((name, payload)))

    semantic_cache.check_cache("q", context_text="same context", session_id="sess-1")

    assert events[0][0] == "cache_hit"
    assert events[0][1]["verified"] is False
    assert events[0][1]["similarity"] == 0.97


def test_check_cache_does_not_emit_an_event_on_a_miss(monkeypatch, fake_index):
    fake_index.query_result = []
    events = []
    monkeypatch.setattr(semantic_cache, "emit_event",
                         lambda name, session_id=None, agent=None, payload=None: events.append(name))
    semantic_cache.check_cache("q")
    assert events == []


# ---------------------------------------------------------------------
# write_cache
# ---------------------------------------------------------------------

def test_write_cache_upserts_with_app_scope_metadata(fake_index):
    semantic_cache.write_cache("q", "the answer", app_slug="my-app")
    vectors = fake_index.upserted[0]
    entry = vectors[0]
    assert entry["metadata"]["answer"] == "the answer"
    assert entry["metadata"]["app"] == "my-app"


def test_write_cache_upserts_with_workspace_scope_metadata(fake_index):
    semantic_cache.write_cache("q", "the answer", workspace_id="ws-1")
    entry = fake_index.upserted[0][0]
    assert entry["metadata"]["workspace"] == "ws-1"


def test_write_cache_stores_the_context_fingerprint(fake_index):
    semantic_cache.write_cache("q", "the answer", context_text="some context")
    entry = fake_index.upserted[0][0]
    assert entry["metadata"]["context_fingerprint"] == semantic_cache._fingerprint("some context")


def test_write_cache_does_nothing_when_embed_fails(monkeypatch, fake_index):
    def boom(text):
        raise RuntimeError("HF unavailable")
    monkeypatch.setattr(semantic_cache, "embed_text", boom)
    semantic_cache.write_cache("q", "the answer")
    assert fake_index.upserted == []


# ---------------------------------------------------------------------
# invalidate_cache
# ---------------------------------------------------------------------

def test_invalidate_cache_deletes_matches_at_or_above_the_invalidation_threshold(fake_index):
    fake_index.query_result = [
        FakeMatch(score=semantic_cache.INVALIDATION_THRESHOLD, id="a"),
        FakeMatch(score=semantic_cache.INVALIDATION_THRESHOLD - 0.01, id="b"),
        FakeMatch(score=0.99, id="c"),
    ]
    purged = semantic_cache.invalidate_cache("a correction", workspace_id="ws-1")
    assert purged == 2
    assert set(fake_index.deleted_ids) == {"a", "c"}


def test_invalidate_cache_scopes_the_purge_to_the_given_workspace(fake_index):
    fake_index.query_result = []
    semantic_cache.invalidate_cache("a correction", workspace_id="ws-1")
    assert fake_index.last_query_filter == "workspace = 'ws-1'"


def test_invalidate_cache_scopes_the_purge_to_the_given_app(fake_index):
    fake_index.query_result = []
    semantic_cache.invalidate_cache("a correction", app_slug="my-app")
    assert fake_index.last_query_filter == "app = 'my-app'"


def test_invalidate_cache_never_purges_across_scopes_by_default(fake_index):
    """No app_slug/workspace_id given must purge the legacy global
    bucket ONLY -- never a cross-scope wildcard, per the module's own
    docstring guarantee that a workspace correction can't reach into
    an unrelated app's cache or vice versa."""
    fake_index.query_result = []
    semantic_cache.invalidate_cache("a correction")
    assert fake_index.last_query_filter == "project = 'global'"


def test_invalidate_cache_does_not_delete_when_nothing_meets_the_threshold(fake_index):
    fake_index.query_result = [FakeMatch(score=semantic_cache.INVALIDATION_THRESHOLD - 0.01, id="a")]
    purged = semantic_cache.invalidate_cache("a correction", workspace_id="ws-1")
    assert purged == 0
    assert fake_index.deleted_ids == []


def test_invalidate_cache_returns_zero_when_embed_fails(monkeypatch, fake_index):
    def boom(text):
        raise RuntimeError("HF unavailable")
    monkeypatch.setattr(semantic_cache, "embed_text", boom)
    assert semantic_cache.invalidate_cache("a correction", workspace_id="ws-1") == 0


def test_invalidate_cache_returns_zero_when_query_raises(monkeypatch, fake_index):
    def boom(vector, top_k, include_metadata, filter):
        raise RuntimeError("Vector unavailable")
    fake_index.query = boom
    assert semantic_cache.invalidate_cache("a correction", workspace_id="ws-1") == 0
