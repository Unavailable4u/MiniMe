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
`from utils.llm_client import embed_text`, `from eo import laya_gate`,
and `from relay.emitter import emit_event` (all bound names in its own
namespace, except laya_gate which is the module itself) -- tests patch
`vector_index`, `embed_text`, `laya_gate.predict`, and `emit_event` on
the semantic_cache module object, same gotcha as every other cache/store
module in this batch. Laya migration audit (2026-09-27): verification
used to be a `generate_text` call returning "YES"/"NO"; it's now
`laya_gate.predict()` returning `{"answers": {"still_accurate":
{"noul": <probability>}}}` -- _fake_verify() below builds that shape.
"""
import time

import pytest

from eo import semantic_cache


def _fake_verify(p_still_accurate):
    """Builds the laya_gate.predict() return shape _verify_still_accurate()
    reads, for a given P(still accurate). Threshold is
    semantic_cache._STILL_ACCURATE_THRESHOLD (0.65 as of this migration) --
    pass something clearly above/below it, not exactly it, so these tests
    don't silently flip if that threshold is retuned later."""
    return lambda *a, **k: {"answers": {"still_accurate": {"noul": p_still_accurate}}}

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
# fingerprint mismatch escalates to Laya verification
# ---------------------------------------------------------------------

def test_check_cache_replays_blindly_when_context_fingerprint_is_unchanged(monkeypatch, fake_index):
    """A hit whose stored context_fingerprint matches the CURRENT
    context must be served without any verification Laya call -- the
    whole point of storing the fingerprint in the first place."""
    fp = semantic_cache._fingerprint("same context")
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": fp,
                                                    "system_version": semantic_cache.SYSTEM_VERSION})]
    verify_called = []

    def fake_predict(*a, **k):
        verify_called.append(1)
        return {"answers": {"still_accurate": {"noul": 0.95}}}
    monkeypatch.setattr(semantic_cache.laya_gate, "predict", fake_predict)

    result = semantic_cache.check_cache("some question", context_text="same context")

    assert result == "cached answer"
    assert verify_called == []


def test_check_cache_escalates_to_verification_when_fingerprint_differs(monkeypatch, fake_index):
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("old context"),
                                                    "system_version": semantic_cache.SYSTEM_VERSION})]
    monkeypatch.setattr(semantic_cache.laya_gate, "predict", _fake_verify(0.95))

    result = semantic_cache.check_cache("some question", context_text="new context")
    assert result == "cached answer"


def test_check_cache_returns_none_when_verification_says_no(monkeypatch, fake_index):
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("old context")})]
    monkeypatch.setattr(semantic_cache.laya_gate, "predict", _fake_verify(0.05))

    assert semantic_cache.check_cache("some question", context_text="new context") is None


def test_check_cache_treats_verification_call_failure_as_not_accurate(monkeypatch, fake_index):
    """_verify_still_accurate()'s own 'when in doubt, say NO' contract
    must hold even when the verification call itself errors -- fail
    closed (miss), not open (stale replay)."""
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "context_fingerprint": semantic_cache._fingerprint("old context")})]

    def boom(*a, **k):
        raise RuntimeError("Laya unavailable")
    monkeypatch.setattr(semantic_cache.laya_gate, "predict", boom)

    assert semantic_cache.check_cache("some question", context_text="new context") is None


def test_check_cache_missing_stored_fingerprint_also_escalates_to_verification(monkeypatch, fake_index):
    """A legacy entry with no context_fingerprint at all (written before
    this trust model existed) must not be treated as a fingerprint
    match -- it should still go through verification rather than being
    replayed blindly."""
    fake_index.query_result = [FakeMatch(score=0.99,
                                          metadata={"answer": "cached answer", "_cached_at": time.time(),
                                                    "system_version": semantic_cache.SYSTEM_VERSION})]
    monkeypatch.setattr(semantic_cache.laya_gate, "predict", _fake_verify(0.95))

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
                                                    "context_fingerprint": fp,
                                                    "system_version": semantic_cache.SYSTEM_VERSION})]
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


# ---------------------------------------------------------------------
# format_reference_block — perf audit follow-up (#4): the bounded
# reuse-as-is / revise-just-the-part / full-rewrite-as-fallback
# instruction that replaced the old open-ended "build on it, refine it,
# or diverge... as this new ask calls for" wrapper both generative call
# sites (api/task_runner.py, eo/loop_v4.py) prepend ahead of a
# get_cached_reference() hit.
# ---------------------------------------------------------------------

def test_format_reference_block_returns_empty_string_for_falsy_input():
    assert semantic_cache.format_reference_block("") == ""
    assert semantic_cache.format_reference_block(None) == ""


def test_format_reference_block_embeds_the_reference_answer_verbatim():
    result = semantic_cache.format_reference_block("Use exponential backoff with a 2s base.")
    assert result.endswith("Use exponential backoff with a 2s base.")


def test_format_reference_block_offers_reuse_as_is_as_the_cheap_default():
    result = semantic_cache.format_reference_block("some prior answer")
    assert "reuse it as-is" in result
    assert "don't rewrite something that's already right" in result


def test_format_reference_block_scopes_a_genuine_change_to_the_relevant_part():
    result = semantic_cache.format_reference_block("some prior answer")
    assert "revise just that part" in result


def test_format_reference_block_keeps_a_full_rewrite_as_a_fallback_only():
    result = semantic_cache.format_reference_block("some prior answer")
    assert "Only write a full new answer if this ask genuinely calls for a different" in result


def test_format_reference_block_no_longer_uses_the_old_open_ended_wording():
    # Regression guard: the pre-patch instruction set no ceiling at all
    # ("build on it, refine it, or diverge... as this new ask calls
    # for") -- confirms this phrasing was actually replaced, not just
    # supplemented.
    result = semantic_cache.format_reference_block("some prior answer")
    assert "diverge from it as this new ask calls for" not in result
    assert "don't just repeat it verbatim" not in result


def test_format_reference_block_is_callable_unconditionally():
    # Regression guard for the call-site refactor: both
    # api/task_runner.py and eo/loop_v4.py now do
    # `task_text + format_reference_block(reference_answer)`
    # unconditionally rather than branching on reference_answer
    # themselves first -- confirm that's actually safe end to end.
    task_text = "write a deployment checklist"
    assert task_text + semantic_cache.format_reference_block(None) == task_text
    assert task_text + semantic_cache.format_reference_block("prior checklist text") != task_text


# ---------------------------------------------------------------------
# _verify_still_accurate -- per-field input budget (Laya migration)
#
# Laya's English checkpoint reads only ~300 tokens for the WHOLE state, so
# each field is clipped separately; otherwise a long cached answer would
# silently crowd out the current context.
# ---------------------------------------------------------------------

def test_verify_clips_each_field_to_its_own_budget(monkeypatch):
    seen = {}

    def fake_predict(state, questions, site="unknown"):
        seen["state"], seen["site"] = state, site
        return {"answers": {"still_accurate": {"noul": 0.95}}}
    monkeypatch.setattr(semantic_cache.laya_gate, "predict", fake_predict)

    semantic_cache._verify_still_accurate("Q" * 5000, "A" * 5000, "C" * 5000)

    st = seen["state"]
    assert len(st["original_question"]) == semantic_cache._Q_CHARS
    assert len(st["cached_answer"]) == semantic_cache._ANSWER_CHARS
    assert len(st["current_context"]) == semantic_cache._CONTEXT_CHARS
    assert seen["site"] == "semantic_cache._verify_still_accurate"


def test_verify_keeps_the_tail_of_the_context_and_both_ends_of_the_answer(monkeypatch):
    seen = {}
    monkeypatch.setattr(semantic_cache.laya_gate, "predict",
                        lambda state, q, site="u": seen.update(state=state)
                        or {"answers": {"still_accurate": {"noul": 0.95}}})

    context = "OLD " * 500 + "LATEST CORRECTION"
    answer = "ANSWER-START " + "x" * 2000 + " ANSWER-END"
    semantic_cache._verify_still_accurate("q", answer, context)

    assert seen["state"]["current_context"].endswith("LATEST CORRECTION")
    assert seen["state"]["cached_answer"].startswith("ANSWER-START")
    assert seen["state"]["cached_answer"].endswith("ANSWER-END")


def test_verify_short_inputs_pass_through_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(semantic_cache.laya_gate, "predict",
                        lambda state, q, site="u": seen.update(state=state)
                        or {"answers": {"still_accurate": {"noul": 0.95}}})
    semantic_cache._verify_still_accurate("what is X", "X is Y", "")
    assert seen["state"] == {"original_question": "what is X",
                             "cached_answer": "X is Y",
                             "current_context": "(none)"}
