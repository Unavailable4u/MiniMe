"""
tests/unit/test_hw_reference.py — Phase 0, Patch 0.5 of the Mech/
Enclosure implementation guide: covers the hardware reference-design
RAG slice end to end (Patches 0.1-0.4) --

  - eo/hw_reference.write_hw_reference() -- required-field guard,
    embed-failure/upsert-failure degrade-don't-raise posture, metadata
    shape.
  - eo/hw_reference.search_hw_references() -- prefix isolation (never
    returns a node:/eo_outcome: hit even when one is sitting in the
    same fake index and superficially matches the metadata filter),
    graceful no-hit/embed-failure/query-failure degradation to [].
  - agents/web_researcher.py's "hw_reference" scope -- indexes every
    result under the CALLER's own canonical generic_name (resolved via
    component_dimension_table.lookup_curated_dimensions()), never the
    source's own ad-hoc title wording; falls back to "general" when
    generic_name is missing rather than erroring.
  - agents/hardware_speccer._build_hw_reference_context() -- anecdotal/
    "not IPC-2221" framing text present when matches exist; "" (no
    prompt change at all) when nothing matched anywhere, and when
    search_hw_references() itself raises.

Same "fake out the SDK object itself" approach tests/unit/
test_mech_validator.py already uses for Sandbox -- _FakeVectorIndex
below stands in for the real Upstash Vector `Index`, so these tests
exercise this slice's OWN filtering/resolution/framing logic without
a real Upstash/HuggingFace network call. fake_bus (tests/conftest.py,
autouse) already isolates memory.bus.redis, which is all
log_usage()/research_cache.py need underneath.
"""
import pytest

from agents import hardware_speccer, web_researcher
from eo import hw_reference

# ---------------------------------------------------------------------------
# Fakes -- stand in for the real Upstash Vector Index and the HF embed call.
# ---------------------------------------------------------------------------

class _FakeMatch:
    def __init__(self, id, metadata, score=0.9):
        self.id = id
        self.metadata = metadata
        self.score = score


class _FakeVectorIndex:
    """In-memory stand-in for the `Index` object memory.bus.vector_index()
    returns. query()'s filter handling is deliberately narrow -- just
    enough to exercise search_hw_references()'s own
    `filter="generic_name != ''"` clause, not a real Upstash Vector
    filter-language implementation."""

    def __init__(self):
        self._store = {}  # vector_id -> (vector, metadata)
        self.upsert_calls = []
        self.raise_on_upsert = None
        self.raise_on_query = None

    def seed(self, vector_id, metadata):
        """Directly plant an entry, bypassing write_hw_reference() --
        used to simulate a DIFFERENT prefix's record (e.g. a
        knowledge_graph node: entry) already sitting in the shared
        index."""
        self._store[vector_id] = ([0.0], metadata)

    def upsert(self, vectors):
        if self.raise_on_upsert:
            raise self.raise_on_upsert
        for vector_id, vector, metadata in vectors:
            self._store[vector_id] = (vector, metadata)
            self.upsert_calls.append((vector_id, vector, metadata))

    def query(self, vector, top_k, include_metadata=True, filter=None):
        if self.raise_on_query:
            raise self.raise_on_query
        results = []
        for vector_id, (v, meta) in self._store.items():
            if filter == "generic_name != ''" and not meta.get("generic_name"):
                continue
            results.append(_FakeMatch(vector_id, meta))
        return results[:top_k]


@pytest.fixture
def fake_index(monkeypatch):
    index = _FakeVectorIndex()
    monkeypatch.setattr(hw_reference, "vector_index", lambda: index)
    return index


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """Deterministic, network-free embed_text() stand-in shared by every
    test in this file -- individual tests override with monkeypatch when
    they need embed_text() to raise."""
    monkeypatch.setattr(hw_reference, "embed_text", lambda text: [0.1, 0.2, 0.3])


# ---------------------------------------------------------------------------
# write_hw_reference (Patch 0.1)
# ---------------------------------------------------------------------------

def test_write_hw_reference_success_upserts_under_hw_ref_prefix(fake_index):
    ref_id = hw_reference.write_hw_reference({
        "workspace_id": "ws1",
        "generic_name": "28BYJ-48 Stepper",
        "content": "A forum post about driving this stepper from an ESP32.",
        "title": "Stepper + ESP32 build log",
        "source_url": "https://example.com/post",
        "dimension_ref_id": "stepper_28byj48",
    })
    assert ref_id is not None
    assert len(fake_index.upsert_calls) == 1
    vector_id, _vector, metadata = fake_index.upsert_calls[0]
    assert vector_id == f"hw_ref:ws1:{ref_id}"
    assert metadata["generic_name"] == "28BYJ-48 Stepper"
    assert metadata["workspace_id"] == "ws1"
    assert metadata["dimension_ref_id"] == "stepper_28byj48"
    assert metadata["content"].startswith("A forum post")


def test_write_hw_reference_missing_required_field_returns_none_and_skips_upsert(fake_index):
    assert hw_reference.write_hw_reference({
        "workspace_id": "ws1", "generic_name": "28BYJ-48 Stepper",
        # no "content"
    }) is None
    assert fake_index.upsert_calls == []


def test_write_hw_reference_embed_failure_degrades_to_none(fake_index, monkeypatch):
    monkeypatch.setattr(hw_reference, "embed_text",
                         lambda text: (_ for _ in ()).throw(RuntimeError("HF down")))
    ref_id = hw_reference.write_hw_reference({
        "workspace_id": "ws1", "generic_name": "DS18B20", "content": "some text",
    })
    assert ref_id is None
    assert fake_index.upsert_calls == []  # never reached the vector store at all


def test_write_hw_reference_upsert_failure_degrades_to_none(fake_index):
    fake_index.raise_on_upsert = RuntimeError("Vector unreachable")
    ref_id = hw_reference.write_hw_reference({
        "workspace_id": "ws1", "generic_name": "DS18B20", "content": "some text",
    })
    assert ref_id is None


# ---------------------------------------------------------------------------
# search_hw_references (Patch 0.3)
# ---------------------------------------------------------------------------

def test_search_hw_references_empty_generic_name_short_circuits(fake_index):
    assert hw_reference.search_hw_references("") == []
    assert hw_reference.search_hw_references(None) == []
    assert fake_index.upsert_calls == []


def test_search_hw_references_no_hits_returns_empty_cleanly(fake_index):
    # Index has nothing at all -- must return [] with no error, not raise.
    assert hw_reference.search_hw_references("28BYJ-48 Stepper") == []


def test_search_hw_references_returns_only_hw_ref_entries(fake_index):
    hw_reference.write_hw_reference({
        "workspace_id": "ws1", "generic_name": "28BYJ-48 Stepper",
        "content": "real hw_ref entry",
    })
    # A DIFFERENT prefix's record, planted directly -- even though it
    # happens to carry a non-empty "generic_name"-shaped field (worst
    # case for the metadata filter alone), the id-prefix check must
    # still keep it out.
    fake_index.seed("node:ws1:abc123", {
        "generic_name": "should never leak", "workspace_id": "ws1",
        "section": "research", "node_type": "source",
    })
    fake_index.seed("eo_outcome:1700000000000", {
        "generic_name": "also should never leak", "task_text": "...", "outcome": "ok",
    })

    results = hw_reference.search_hw_references("28BYJ-48 Stepper")
    assert len(results) == 1
    assert results[0]["vector_id"].startswith("hw_ref:")
    assert results[0]["generic_name"] == "28BYJ-48 Stepper"


def test_search_hw_references_embed_failure_returns_empty(fake_index, monkeypatch):
    monkeypatch.setattr(hw_reference, "embed_text",
                         lambda text: (_ for _ in ()).throw(RuntimeError("HF down")))
    assert hw_reference.search_hw_references("28BYJ-48 Stepper") == []


def test_search_hw_references_query_failure_returns_empty(fake_index):
    fake_index.raise_on_query = RuntimeError("Vector unreachable")
    assert hw_reference.search_hw_references("28BYJ-48 Stepper") == []


# ---------------------------------------------------------------------------
# agents/web_researcher.py -- "hw_reference" scope indexing path (Patch 0.2)
# ---------------------------------------------------------------------------

def test_hw_reference_scope_indexes_under_callers_canonical_name(monkeypatch):
    """The source's own title/snippet wording ('Cheap stepper driver
    hack') must never become the indexed generic_name -- it always
    indexes under whatever canonical name the caller passed in."""
    written = []
    monkeypatch.setattr(web_researcher, "write_hw_reference",
                         lambda mech_ref: written.append(mech_ref) or "ref123")
    monkeypatch.setattr(web_researcher, "lookup_curated_dimensions",
                         lambda generic_name, aliases=None: {"dimension_ref_id": "stepper_28byj48"})
    monkeypatch.setattr(web_researcher, "web_search",
                         lambda query, domains=None, max_results=8, agent_name=None: [
                             {"url": "https://example.com/a",
                              "title": "Cheap stepper driver hack",
                              "snippet": "Ad-hoc source wording, not canonical."},
                         ])

    report = web_researcher.run(
        task_text="28BYJ-48 stepper projects", scope="hw_reference",
        generic_name="28BYJ-48 Stepper", aliases=["28byj48"],
        force_refresh=True,
    )

    assert report["scope"] == "hw_reference"
    assert len(written) == 1
    assert written[0]["generic_name"] == "28BYJ-48 Stepper"
    assert written[0]["dimension_ref_id"] == "stepper_28byj48"
    assert written[0]["source_url"] == "https://example.com/a"
    # The source's own wording still rides along as title/content, just
    # never becomes the indexed generic_name.
    assert written[0]["title"] == "Cheap stepper driver hack"


def test_hw_reference_scope_still_indexes_when_curated_table_has_no_match(monkeypatch):
    """G1a's curated table is small/hand-curated -- an unresolved
    generic_name must still index (dimension_ref_id just stays absent),
    not get silently dropped. See _index_hw_references()'s own
    docstring."""
    written = []
    monkeypatch.setattr(web_researcher, "write_hw_reference",
                         lambda mech_ref: written.append(mech_ref) or "ref123")
    monkeypatch.setattr(web_researcher, "lookup_curated_dimensions",
                         lambda generic_name, aliases=None: None)
    monkeypatch.setattr(web_researcher, "web_search",
                         lambda query, domains=None, max_results=8, agent_name=None: [
                             {"url": "https://example.com/a", "title": "t", "snippet": "s"},
                         ])

    web_researcher.run(
        task_text="some obscure part", scope="hw_reference",
        generic_name="Some Obscure Part", force_refresh=True,
    )
    assert len(written) == 1
    assert written[0]["dimension_ref_id"] is None


def test_hw_reference_scope_without_generic_name_falls_back_to_general(monkeypatch):
    node_written = []
    monkeypatch.setattr(web_researcher, "write_node",
                         lambda **kwargs: node_written.append(kwargs) or "node123")
    hw_written = []
    monkeypatch.setattr(web_researcher, "write_hw_reference",
                         lambda mech_ref: hw_written.append(mech_ref) or "ref123")
    monkeypatch.setattr(web_researcher, "web_search",
                         lambda query, domains=None, max_results=8, agent_name=None: [
                             {"url": "https://example.com/a", "title": "t", "snippet": "s"},
                         ])

    report = web_researcher.run(
        task_text="28BYJ-48 stepper projects", scope="hw_reference",
        generic_name=None, force_refresh=True,
    )
    assert report["scope"] == "general"
    assert len(node_written) == 1
    assert hw_written == []


# ---------------------------------------------------------------------------
# agents/hardware_speccer._build_hw_reference_context (Patch 0.4)
# ---------------------------------------------------------------------------

def test_build_hw_reference_context_frames_matches_as_anecdotal(monkeypatch):
    monkeypatch.setattr(
        hw_reference, "search_hw_references",
        lambda generic_name, aliases=None, top_k=2, mobility_type=None: (
            [{"title": "Forum build log", "content": "Drove it fine off 5V.",
              "source_url": "https://example.com/x", "generic_name": generic_name}]
            if generic_name == "28BYJ-48 Stepper" else []
        ),
    )
    parts = [
        {"generic_name": "28BYJ-48 Stepper", "aliases": ["28byj48"]},
        {"generic_name": "ESP32 Dev Board", "aliases": []},
    ]
    block = hardware_speccer._build_hw_reference_context(parts)
    assert "28BYJ-48 Stepper" in block
    assert "anecdotal" in block.lower()
    assert "IPC-2221" in block
    assert "Forum build log" in block
    # The part with no matches shouldn't get a fabricated entry.
    assert "ESP32 Dev Board" not in block


def test_build_hw_reference_context_empty_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(hw_reference, "search_hw_references",
                         lambda generic_name, aliases=None, top_k=2, mobility_type=None: [])
    parts = [{"generic_name": "ESP32 Dev Board", "aliases": []}]
    # "" means wiring_user_prompt += "" is a no-op -- same prompt as
    # before this phase existed (the "no regression" done-when).
    assert hardware_speccer._build_hw_reference_context(parts) == ""


def test_build_hw_reference_context_degrades_on_search_failure(monkeypatch):
    def _boom(generic_name, aliases=None, top_k=2, mobility_type=None):
        raise RuntimeError("Vector unreachable")

    monkeypatch.setattr(hw_reference, "search_hw_references", _boom)
    parts = [{"generic_name": "28BYJ-48 Stepper", "aliases": []}]
    assert hardware_speccer._build_hw_reference_context(parts) == ""


def test_build_hw_reference_context_skips_parts_without_generic_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        hw_reference, "search_hw_references",
        lambda generic_name, aliases=None, top_k=2, mobility_type=None: calls.append(generic_name) or [],
    )
    parts = [{"generic_name": ""}, {"generic_name": None}, "not a dict"]
    assert hardware_speccer._build_hw_reference_context(parts) == ""
    assert calls == []  # never even queried for the gap-having parts


def test_build_hw_reference_context_forwards_mobility_type(monkeypatch):
    """Patch A.5 (Mech View standalone implementation guide, Phase A):
    the archetype's own mobility_type must reach every
    search_hw_references() call so a wheeled robot's own precedent
    search doesn't pull handheld-gadget reference builds for the same
    generic part."""
    seen = []
    monkeypatch.setattr(
        hw_reference, "search_hw_references",
        lambda generic_name, aliases=None, top_k=2, mobility_type=None: (
            seen.append((generic_name, mobility_type)) or []
        ),
    )
    parts = [{"generic_name": "28BYJ-48 Stepper", "aliases": []}]
    hardware_speccer._build_hw_reference_context(parts, mobility_type="wheeled")
    assert seen == [("28BYJ-48 Stepper", "wheeled")]


def test_query_text_includes_mobility_type_except_static_default():
    """Patch A.5: mobility_type folds into the embedding query text only
    when set and not the safe "static" default, so a `full`/`static`
    caller's query text is byte-for-byte unchanged from before this
    patch (no regression for the common case)."""
    base = hw_reference._query_text("28BYJ-48 Stepper", ["28byj48"])
    assert hw_reference._query_text("28BYJ-48 Stepper", ["28byj48"], None) == base
    assert hw_reference._query_text("28BYJ-48 Stepper", ["28byj48"], "static") == base
    wheeled = hw_reference._query_text("28BYJ-48 Stepper", ["28byj48"], "wheeled")
    assert wheeled != base
    assert "wheeled" in wheeled
