"""
tests/unit/test_agent_academic_search.py — Patch 7f-4b-1.

Covers agents/academic_search.py's run(): cleans the raw instruction
sentence into a bare topic query, fans out to the requested source(s)
(Semantic Scholar/arXiv/CrossRef/OpenAlex — all four by default), dedups
by DOI/title across sources (filling in whatever the first hit was
missing), writes each surviving paper as a knowledge-graph node, then
writes a "cites" edge between any two papers that are BOTH present in
this result set, and reports counts + a summary back onto the bus.

write_node()/create_edge() are faked at the module level (bound names
academic_search.write_node / academic_search.create_edge), same
approach test_agent_duplication_checker.py uses for vector_index() —
this module never touches the real graph store or embeddings. The four
_search_* functions talk to requests.get() directly (no shared client
object to fake), so they're covered with a fake `requests.get`, and
run()-level tests fake SOURCE_FNS's entries directly to avoid any HTTP
concern at that layer.
"""
from unittest.mock import MagicMock

import pytest

import agents.academic_search as academic_search


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _paper(title="Some Paper", source="semantic_scholar", **overrides):
    base = {
        "title": title, "authors": ["A. Author"], "year": 2023,
        "abstract": "an abstract", "doi": None, "venue": "Venue",
        "citation_count": 5, "source": source, "_cites": [],
    }
    base.update(overrides)
    return base


class _FakeResponse:
    def __init__(self, json_data=None, text="", status=200, raise_exc=None):
        self._json_data = json_data
        self.text = text
        self.status_code = status
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json_data


@pytest.fixture
def fake_graph(monkeypatch):
    """Fakes the knowledge-graph writes academic_search.run() makes,
    returning incrementing node ids by default."""
    counter = {"n": 0}

    def _write_node(**kwargs):
        counter["n"] += 1
        return f"node-{counter['n']}"

    write_node_mock = MagicMock(side_effect=_write_node)
    create_edge_mock = MagicMock(side_effect=lambda *a, **k: {"edge_id": "edge-1"})
    monkeypatch.setattr(academic_search, "write_node", write_node_mock)
    monkeypatch.setattr(academic_search, "create_edge", create_edge_mock)
    return write_node_mock, create_edge_mock


@pytest.fixture(autouse=True)
def _fake_workspace(request, monkeypatch):
    # TestWorkspaceResolution below exercises _workspace_id() itself, so
    # it must not have this blanket fake applied over it.
    if request.cls is not None and request.cls.__name__ == "TestWorkspaceResolution":
        return
    monkeypatch.setattr(academic_search, "_workspace_id", lambda: "my-app")


def _seed_sources(monkeypatch, **by_source):
    """Replace academic_search.SOURCE_FNS's values with fakes that return
    a fixed paper list for named sources (ignoring query/limit args) and
    an empty list for everything else, then patch the module dict itself
    so run() only ever sees these fakes."""
    def _make(papers):
        return lambda query, limit: papers

    fixed = {name: _make([]) for name in academic_search.SOURCE_FNS}
    for name, papers in by_source.items():
        fixed[name] = _make(papers)
    monkeypatch.setattr(academic_search, "SOURCE_FNS", fixed)
    return fixed


# ---------------------------------------------------------------------------
# 1. _clean_query
# ---------------------------------------------------------------------------
class TestCleanQuery:
    def test_strips_find_prefix_up_to_colon(self):
        result = academic_search._clean_query("Find recent papers about: sparse attention transformers")
        assert result == "sparse attention transformers"

    def test_strips_search_for_prefix(self):
        assert academic_search._clean_query("search for: quantum computing") == "quantum computing"

    def test_strips_search_prefix_without_for(self):
        assert academic_search._clean_query("search: quantum computing") == "quantum computing"

    def test_strips_look_up_prefix(self):
        assert academic_search._clean_query("look up: foo bar") == "foo bar"

    def test_strips_get_prefix(self):
        assert academic_search._clean_query("get: baz") == "baz"

    def test_strips_show_me_prefix(self):
        assert academic_search._clean_query("show me: qux") == "qux"

    def test_is_case_insensitive(self):
        assert academic_search._clean_query("FIND papers about: Topic") == "Topic"

    def test_leaves_text_without_recognized_lead_in_shape_untouched(self):
        assert academic_search._clean_query("no colon here at all") == "no colon here at all"

    def test_leaves_bare_topic_untouched(self):
        assert academic_search._clean_query("sparse attention transformers") == "sparse attention transformers"

    def test_none_input_returns_empty_string(self):
        assert academic_search._clean_query(None) == ""

    def test_empty_string_input_returns_empty_string(self):
        assert academic_search._clean_query("") == ""

    def test_only_first_colon_lead_in_is_stripped(self):
        # Conservative-on-purpose: only the recognized instruction lead-in
        # is stripped, not every colon in the string.
        result = academic_search._clean_query("Find papers about: attention: a survey")
        assert result == "attention: a survey"


# ---------------------------------------------------------------------------
# 2. Individual source functions
# ---------------------------------------------------------------------------
class TestSemanticScholar:
    def test_parses_fields_and_references_into_cites(self, monkeypatch):
        payload = {"data": [{
            "title": "Attention Is All You Need",
            "authors": [{"name": "A. Vaswani"}, {"name": ""}],
            "year": 2017, "abstract": "We propose a new architecture",
            "externalIds": {"DOI": "10.1/abc"}, "venue": "NeurIPS",
            "citationCount": 100,
            "references": [{"title": "Prior Work"}, {"title": ""}],
        }]}
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: _FakeResponse(payload))
        papers = academic_search._search_semantic_scholar("attention", 10)
        assert len(papers) == 1
        p = papers[0]
        assert p["title"] == "Attention Is All You Need"
        assert p["authors"] == ["A. Vaswani"]
        assert p["year"] == 2017
        assert p["doi"] == "10.1/abc"
        assert p["venue"] == "NeurIPS"
        assert p["citation_count"] == 100
        assert p["source"] == "semantic_scholar"
        assert p["_cites"] == ["Prior Work"]

    def test_missing_optional_fields_default_sensibly(self, monkeypatch):
        payload = {"data": [{"title": "Bare Paper"}]}
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: _FakeResponse(payload))
        papers = academic_search._search_semantic_scholar("q", 10)
        assert papers[0]["authors"] == []
        assert papers[0]["abstract"] == ""
        assert papers[0]["doi"] is None
        assert papers[0]["_cites"] == []

    def test_request_exception_returns_empty_list(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("network down")
        monkeypatch.setattr(academic_search.requests, "get", _raise)
        assert academic_search._search_semantic_scholar("q", 10) == []

    def test_http_error_status_returns_empty_list(self, monkeypatch):
        resp = _FakeResponse({"data": []}, raise_exc=RuntimeError("500"))
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: resp)
        assert academic_search._search_semantic_scholar("q", 10) == []


class TestArxiv:
    _ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>  A Padded Title  </title>
    <summary>  An abstract with whitespace  </summary>
    <author><name>Jane Doe</name></author>
    <author><name>John Roe</name></author>
    <published>2021-05-04T00:00:00Z</published>
  </entry>
</feed>"""

    def test_parses_entry_fields(self, monkeypatch):
        monkeypatch.setattr(academic_search.requests, "get",
                             lambda *a, **k: _FakeResponse(text=self._ATOM))
        papers = academic_search._search_arxiv("q", 5)
        assert len(papers) == 1
        p = papers[0]
        assert p["title"] == "A Padded Title"
        assert p["abstract"] == "An abstract with whitespace"
        assert p["authors"] == ["Jane Doe", "John Roe"]
        assert p["year"] == 2021
        assert p["doi"] is None
        assert p["venue"] == "arXiv"
        assert p["citation_count"] is None
        assert p["source"] == "arxiv"
        assert p["_cites"] == []

    def test_missing_published_date_yields_none_year(self, monkeypatch):
        atom = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>No Date Paper</title>
    <summary>abstract</summary>
  </entry>
</feed>"""
        monkeypatch.setattr(academic_search.requests, "get",
                             lambda *a, **k: _FakeResponse(text=atom))
        papers = academic_search._search_arxiv("q", 5)
        assert papers[0]["year"] is None

    def test_no_entries_returns_empty_list(self, monkeypatch):
        atom = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        monkeypatch.setattr(academic_search.requests, "get",
                             lambda *a, **k: _FakeResponse(text=atom))
        assert academic_search._search_arxiv("q", 5) == []

    def test_request_exception_returns_empty_list(self, monkeypatch):
        def _raise(*a, **k):
            raise TimeoutError("slow")
        monkeypatch.setattr(academic_search.requests, "get", _raise)
        assert academic_search._search_arxiv("q", 5) == []

    def test_malformed_xml_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(academic_search.requests, "get",
                             lambda *a, **k: _FakeResponse(text="<not><valid"))
        assert academic_search._search_arxiv("q", 5) == []


class TestCrossref:
    def test_parses_items(self, monkeypatch):
        payload = {"message": {"items": [{
            "title": ["A CrossRef Title"],
            "author": [{"given": "Ada", "family": "Lovelace"}, {"given": "", "family": ""}],
            "issued": {"date-parts": [[2010, 3]]},
            "container-title": ["Journal of Things"],
            "DOI": "10.2/xyz", "is-referenced-by-count": 42,
        }]}}
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: _FakeResponse(payload))
        papers = academic_search._search_crossref("q", 5)
        assert len(papers) == 1
        p = papers[0]
        assert p["title"] == "A CrossRef Title"
        assert p["authors"] == ["Ada Lovelace"]
        assert p["year"] == 2010
        assert p["doi"] == "10.2/xyz"
        assert p["venue"] == "Journal of Things"
        assert p["citation_count"] == 42
        assert p["source"] == "crossref"

    def test_missing_title_and_date_parts_default_to_none(self, monkeypatch):
        payload = {"message": {"items": [{"author": [], "issued": {}}]}}
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: _FakeResponse(payload))
        papers = academic_search._search_crossref("q", 5)
        assert papers[0]["title"] is None
        assert papers[0]["year"] is None
        assert papers[0]["venue"] is None

    def test_request_exception_returns_empty_list(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("down")
        monkeypatch.setattr(academic_search.requests, "get", _raise)
        assert academic_search._search_crossref("q", 5) == []


class TestOpenAlex:
    def test_parses_results_and_strips_doi_url(self, monkeypatch):
        payload = {"results": [{
            "title": "An OpenAlex Paper",
            "authorships": [{"author": {"display_name": "Grace Hopper"}}, {"author": None}],
            "publication_year": 2019,
            "abstract_inverted_index": {"Hello": [0], "world": [1]},
            "doi": "https://doi.org/10.3/qrs",
            "primary_location": {"source": {"display_name": "Some Venue"}},
            "cited_by_count": 7,
        }]}
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: _FakeResponse(payload))
        papers = academic_search._search_openalex("q", 5)
        assert len(papers) == 1
        p = papers[0]
        assert p["title"] == "An OpenAlex Paper"
        assert p["authors"] == ["Grace Hopper"]
        assert p["year"] == 2019
        assert p["abstract"] == "Hello world"
        assert p["doi"] == "10.3/qrs"
        assert p["venue"] == "Some Venue"
        assert p["citation_count"] == 7
        assert p["source"] == "openalex"

    def test_missing_doi_and_location_default_to_none(self, monkeypatch):
        payload = {"results": [{"title": "T", "authorships": [], "primary_location": None}]}
        monkeypatch.setattr(academic_search.requests, "get", lambda *a, **k: _FakeResponse(payload))
        papers = academic_search._search_openalex("q", 5)
        assert papers[0]["doi"] is None
        assert papers[0]["venue"] is None

    def test_request_exception_returns_empty_list(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("down")
        monkeypatch.setattr(academic_search.requests, "get", _raise)
        assert academic_search._search_openalex("q", 5) == []


class TestReconstructOpenalexAbstract:
    def test_empty_index_returns_empty_string(self):
        assert academic_search._reconstruct_openalex_abstract({}) == ""
        assert academic_search._reconstruct_openalex_abstract(None) == ""

    def test_reorders_words_by_position(self):
        # "world hello" scrambled: word->positions inverted index.
        idx = {"hello": [1], "world": [0]}
        assert academic_search._reconstruct_openalex_abstract(idx) == "world hello"

    def test_word_repeated_at_multiple_positions(self):
        idx = {"the": [0, 2], "cat": [1]}
        assert academic_search._reconstruct_openalex_abstract(idx) == "the cat the"


# ---------------------------------------------------------------------------
# 3. run() — query cleaning, empty input
# ---------------------------------------------------------------------------
class TestRunEmptyOrMissingQuery:
    def test_no_task_text_writes_empty_report_and_short_circuits(self, fake_bus, fake_graph):
        write_node_mock, create_edge_mock = fake_graph
        result = academic_search.run(task_text=None)
        assert result == {"papers": [], "edges_written": 0, "summary": "No search query provided."}
        write_node_mock.assert_not_called()
        create_edge_mock.assert_not_called()

    def test_blank_task_text_writes_empty_report(self, fake_bus, fake_graph):
        result = academic_search.run(task_text="   ")
        assert result["papers"] == []

    def test_empty_report_is_still_written_to_bus(self, fake_bus, fake_graph, monkeypatch):
        writes = {}
        monkeypatch.setattr(academic_search, "write", lambda key, val: writes.__setitem__(key, val))
        result = academic_search.run(task_text=None)
        assert writes[academic_search.KEYS["academic_search_report"]] == result


# ---------------------------------------------------------------------------
# 4. run() — source fan-out
# ---------------------------------------------------------------------------
class TestRunSourceFanout:
    def test_default_sources_is_all_four(self, fake_bus, fake_graph, monkeypatch):
        called = []
        fixed = {}
        for name in academic_search.SOURCE_FNS:
            fixed[name] = (lambda n: (lambda query, limit: called.append(n) or []))(name)
        monkeypatch.setattr(academic_search, "SOURCE_FNS", fixed)
        academic_search.run(task_text="topic")
        assert sorted(called) == sorted(["semantic_scholar", "arxiv", "crossref", "openalex"])

    def test_sources_param_narrows_which_functions_are_called(self, fake_bus, fake_graph, monkeypatch):
        called = []
        fixed = {name: (lambda n: (lambda q, l: called.append(n) or []))(name)
                 for name in academic_search.SOURCE_FNS}
        monkeypatch.setattr(academic_search, "SOURCE_FNS", fixed)
        academic_search.run(task_text="topic", sources=["arxiv"])
        assert called == ["arxiv"]

    def test_unknown_source_name_is_silently_ignored(self, fake_bus, fake_graph, monkeypatch):
        _seed_sources(monkeypatch)
        # Should not raise even though "nonexistent" isn't in SOURCE_FNS.
        result = academic_search.run(task_text="topic", sources=["nonexistent"])
        assert result["papers"] == []

    def test_cleaned_query_not_raw_task_text_is_sent_to_sources(self, fake_bus, fake_graph, monkeypatch):
        received = {}

        def _spy(query, limit):
            received["query"] = query
            return []

        fixed = {name: _spy for name in academic_search.SOURCE_FNS}
        monkeypatch.setattr(academic_search, "SOURCE_FNS", fixed)
        academic_search.run(task_text="Find recent papers about: sparse attention")
        assert received["query"] == "sparse attention"

    def test_max_results_per_source_passed_as_limit(self, fake_bus, fake_graph, monkeypatch):
        received = {}

        def _spy(query, limit):
            received["limit"] = limit
            return []

        fixed = {name: _spy for name in academic_search.SOURCE_FNS}
        monkeypatch.setattr(academic_search, "SOURCE_FNS", fixed)
        academic_search.run(task_text="topic", sources=["arxiv"])
        assert received["limit"] == academic_search.MAX_RESULTS_PER_SOURCE


# ---------------------------------------------------------------------------
# 5. run() — dedup across sources
# ---------------------------------------------------------------------------
class TestRunDedup:
    def test_same_doi_from_two_sources_is_merged_into_one_paper(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="Same Paper", source="semantic_scholar", doi="10.1/xyz", abstract="", citation_count=None)
        p2 = _paper(title="Same Paper (alt casing)", source="crossref", doi="10.1/XYZ", abstract="filled in", citation_count=99)
        _seed_sources(monkeypatch, semantic_scholar=[p1], crossref=[p2])
        result = academic_search.run(task_text="topic")
        assert len(result["papers"]) == 1
        merged = result["papers"][0]
        # First hit's title wins; missing fields filled from the second.
        assert merged["title"] == "Same Paper"
        assert merged["abstract"] == "filled in"
        assert merged["citation_count"] == 99

    def test_dedup_falls_back_to_title_when_no_doi(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="No DOI Paper", source="arxiv", doi=None)
        p2 = _paper(title="No DOI Paper", source="openalex", doi=None)
        _seed_sources(monkeypatch, arxiv=[p1], openalex=[p2])
        result = academic_search.run(task_text="topic")
        assert len(result["papers"]) == 1

    def test_title_dedup_is_case_and_whitespace_insensitive(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="  Some Title  ", source="arxiv", doi=None)
        p2 = _paper(title="some title", source="openalex", doi=None)
        _seed_sources(monkeypatch, arxiv=[p1], openalex=[p2])
        result = academic_search.run(task_text="topic")
        assert len(result["papers"]) == 1

    def test_different_dois_stay_separate(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="Paper A", doi="10.1/a")
        p2 = _paper(title="Paper B", doi="10.1/b")
        _seed_sources(monkeypatch, semantic_scholar=[p1, p2])
        result = academic_search.run(task_text="topic")
        assert len(result["papers"]) == 2

    def test_paper_with_no_title_is_dropped(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title=None, doi=None)
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        result = academic_search.run(task_text="topic")
        assert result["papers"] == []

    def test_existing_abstract_is_not_overwritten_by_a_second_hit(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="Kept Abstract", doi="10.1/keep", abstract="original", source="semantic_scholar")
        p2 = _paper(title="Kept Abstract", doi="10.1/keep", abstract="should not win", source="crossref")
        _seed_sources(monkeypatch, semantic_scholar=[p1], crossref=[p2])
        result = academic_search.run(task_text="topic")
        assert result["papers"][0]["abstract"] == "original"

    def test_existing_citation_count_is_not_overwritten_when_already_set(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="Cited", doi="10.1/cited", citation_count=10, source="semantic_scholar")
        p2 = _paper(title="Cited", doi="10.1/cited", citation_count=999, source="crossref")
        _seed_sources(monkeypatch, semantic_scholar=[p1], crossref=[p2])
        result = academic_search.run(task_text="topic")
        assert result["papers"][0]["citation_count"] == 10


# ---------------------------------------------------------------------------
# 6. run() — node writing
# ---------------------------------------------------------------------------
class TestRunNodeWriting:
    def test_write_node_called_with_expected_fields(self, fake_bus, fake_graph, monkeypatch):
        write_node_mock, _ = fake_graph
        p1 = _paper(title="Node Paper", doi="10.1/n", year=2022, abstract="abs text", source="arxiv")
        _seed_sources(monkeypatch, arxiv=[p1])
        academic_search.run(task_text="topic", session_id="sess-1", tier=2)
        write_node_mock.assert_called_once()
        kwargs = write_node_mock.call_args.kwargs
        assert kwargs["workspace_id"] == "my-app"
        assert kwargs["section"] == "research"
        assert kwargs["node_type"] == "source"
        assert kwargs["title"] == "Node Paper"
        assert kwargs["content"] == "abs text"
        assert kwargs["created_by"] == "academic_search"
        assert kwargs["tags"] == ["arxiv", "2022"]
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["tier"] == 2

    def test_tags_have_no_year_entry_when_year_missing(self, fake_bus, fake_graph, monkeypatch):
        write_node_mock, _ = fake_graph
        p1 = _paper(title="No Year", doi="10.1/ny", year=None, source="crossref")
        _seed_sources(monkeypatch, crossref=[p1])
        academic_search.run(task_text="topic")
        assert write_node_mock.call_args.kwargs["tags"] == ["crossref"]

    def test_content_falls_back_to_title_when_abstract_missing(self, fake_bus, fake_graph, monkeypatch):
        write_node_mock, _ = fake_graph
        p1 = _paper(title="Titled Only", doi="10.1/t", abstract="")
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        academic_search.run(task_text="topic")
        assert write_node_mock.call_args.kwargs["content"] == "Titled Only"

    def test_untitled_placeholder_never_actually_reached_since_no_title_is_dropped_earlier(self, fake_bus, fake_graph, monkeypatch):
        # Sanity: a paper missing a title never reaches write_node at all
        # (dropped during dedup, see TestRunDedup), so "Untitled" nodes
        # are not something run() currently produces from source data.
        p1 = _paper(title=None, doi="10.1/no-title")
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        write_node_mock, _ = fake_graph
        academic_search.run(task_text="topic")
        write_node_mock.assert_not_called()

    def test_papers_out_shape_matches_written_node(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="Shape Paper", doi="10.1/shape", authors=["X"], year=2020,
                     abstract="a", venue="V", citation_count=3, source="openalex")
        _seed_sources(monkeypatch, openalex=[p1])
        result = academic_search.run(task_text="topic")
        out = result["papers"][0]
        assert out["node_id"] == "node-1"
        assert out["title"] == "Shape Paper"
        assert out["authors"] == ["X"]
        assert out["year"] == 2020
        assert out["abstract"] == "a"
        assert out["doi"] == "10.1/shape"
        assert out["venue"] == "V"
        assert out["citation_count"] == 3
        assert out["source"] == "openalex"
        assert out["paper_id"] == "doi:10.1/shape"

    def test_write_node_failure_still_yields_a_paper_with_none_node_id(self, fake_bus, fake_graph, monkeypatch):
        monkeypatch.setattr(academic_search, "write_node", lambda **k: None)
        p1 = _paper(title="Failed Embed", doi="10.1/fail")
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        result = academic_search.run(task_text="topic")
        assert result["papers"][0]["node_id"] is None


# ---------------------------------------------------------------------------
# 7. run() — citation edges
# ---------------------------------------------------------------------------
class TestRunCitationEdges:
    def test_edge_written_between_two_papers_both_present(self, fake_bus, fake_graph, monkeypatch):
        write_node_mock, create_edge_mock = fake_graph
        citing = _paper(title="Citing Paper", doi="10.1/citing", _cites=["Cited Paper"])
        cited = _paper(title="Cited Paper", doi="10.1/cited")
        _seed_sources(monkeypatch, semantic_scholar=[citing, cited])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 1
        create_edge_mock.assert_called_once()
        assert create_edge_mock.call_args.kwargs["relation"] == "cites"
        assert create_edge_mock.call_args.kwargs["created_by"] == "academic_search"

    def test_edge_ids_use_workspace_prefixed_node_ids(self, fake_bus, fake_graph, monkeypatch):
        _, create_edge_mock = fake_graph
        citing = _paper(title="Citing Paper", doi="10.1/citing", _cites=["Cited Paper"])
        cited = _paper(title="Cited Paper", doi="10.1/cited")
        _seed_sources(monkeypatch, semantic_scholar=[citing, cited])
        academic_search.run(task_text="topic")
        call = create_edge_mock.call_args
        from_id, to_id = call.args[0], call.args[1]
        assert from_id.startswith("node:my-app:")
        assert to_id.startswith("node:my-app:")

    def test_citation_to_paper_not_in_result_set_is_skipped(self, fake_bus, fake_graph, monkeypatch):
        write_node_mock, create_edge_mock = fake_graph
        citing = _paper(title="Lonely Citer", doi="10.1/lonely", _cites=["Paper We Never Fetched"])
        _seed_sources(monkeypatch, semantic_scholar=[citing])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 0
        create_edge_mock.assert_not_called()

    def test_self_citation_is_never_written(self, fake_bus, fake_graph, monkeypatch):
        _, create_edge_mock = fake_graph
        weird = _paper(title="Self Citer", doi="10.1/self", _cites=["Self Citer"])
        _seed_sources(monkeypatch, semantic_scholar=[weird])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 0
        create_edge_mock.assert_not_called()

    def test_citation_title_matching_is_case_and_whitespace_insensitive(self, fake_bus, fake_graph, monkeypatch):
        citing = _paper(title="Citer", doi="10.1/citer", _cites=["  Target Paper  "])
        cited = _paper(title="target paper", doi="10.1/target")
        _seed_sources(monkeypatch, semantic_scholar=[citing, cited])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 1

    def test_paper_with_no_node_id_produces_no_outgoing_edges(self, fake_bus, fake_graph, monkeypatch):
        # from_id resolves via title_to_node_id, which stores whatever
        # write_node() returned -- if that was None (embed failure), no
        # edge should be attempted from this paper.
        monkeypatch.setattr(academic_search, "write_node", lambda **k: None)
        _, create_edge_mock = fake_graph
        citing = _paper(title="Citer", doi="10.1/citer", _cites=["Cited"])
        cited = _paper(title="Cited", doi="10.1/cited")
        _seed_sources(monkeypatch, semantic_scholar=[citing, cited])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 0
        create_edge_mock.assert_not_called()

    def test_create_edge_value_error_is_caught_and_does_not_count_or_crash(self, fake_bus, fake_graph, monkeypatch):
        monkeypatch.setattr(academic_search, "create_edge",
                             MagicMock(side_effect=ValueError("cannot create edge across workspaces")))
        citing = _paper(title="Citer", doi="10.1/citer", _cites=["Cited"])
        cited = _paper(title="Cited", doi="10.1/cited")
        _seed_sources(monkeypatch, semantic_scholar=[citing, cited])
        result = academic_search.run(task_text="topic")  # should not raise
        assert result["edges_written"] == 0

    def test_multiple_citations_from_one_paper_all_counted(self, fake_bus, fake_graph, monkeypatch):
        citing = _paper(title="Multi Citer", doi="10.1/multi", _cites=["Ref One", "Ref Two"])
        ref1 = _paper(title="Ref One", doi="10.1/ref1")
        ref2 = _paper(title="Ref Two", doi="10.1/ref2")
        _seed_sources(monkeypatch, semantic_scholar=[citing, ref1, ref2])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 2

    def test_no_cites_means_no_edges(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="Standalone", doi="10.1/standalone", _cites=[])
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        result = academic_search.run(task_text="topic")
        assert result["edges_written"] == 0


# ---------------------------------------------------------------------------
# 8. run() — summary, bus write, workspace resolution
# ---------------------------------------------------------------------------
class TestRunSummaryAndBus:
    def test_summary_reflects_paper_and_edge_counts_and_source_count(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="P", doi="10.1/p")
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        result = academic_search.run(task_text="topic", sources=["semantic_scholar", "arxiv"])
        assert result["summary"] == (
            "1 paper(s) found across 2 source(s), 0 citation edge(s) written."
        )

    def test_report_written_to_bus_under_academic_search_report_key(self, fake_bus, fake_graph, monkeypatch):
        writes = {}
        monkeypatch.setattr(academic_search, "write", lambda key, val: writes.__setitem__(key, val))
        p1 = _paper(title="P", doi="10.1/p")
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        result = academic_search.run(task_text="topic")
        assert writes[academic_search.KEYS["academic_search_report"]] == result

    def test_run_returns_the_same_report_it_writes(self, fake_bus, fake_graph, monkeypatch):
        p1 = _paper(title="P", doi="10.1/p")
        _seed_sources(monkeypatch, semantic_scholar=[p1])
        result = academic_search.run(task_text="topic")
        assert set(result.keys()) == {"papers", "edges_written", "summary"}


class TestWorkspaceResolution:
    def test_uses_current_app_slug_when_set(self, fake_bus, monkeypatch):
        monkeypatch.setattr(academic_search, "get_current_app_slug", lambda: "slug-from-context")
        assert academic_search._workspace_id() == "slug-from-context"

    def test_falls_back_to_original_idea_when_no_app_slug(self, fake_bus, monkeypatch):
        monkeypatch.setattr(academic_search, "get_current_app_slug", lambda: None)
        monkeypatch.setattr(academic_search, "read",
                             lambda key, default=None: "my idea" if key == academic_search.KEYS["original_idea"] else default)
        assert academic_search._workspace_id() == "my idea"

    def test_falls_back_to_untitled_when_nothing_set(self, fake_bus, monkeypatch):
        monkeypatch.setattr(academic_search, "get_current_app_slug", lambda: None)
        monkeypatch.setattr(academic_search, "read", lambda key, default=None: default)
        assert academic_search._workspace_id() == "untitled"
