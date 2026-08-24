"""
tests/unit/test_agent_mind_mapper.py — Patch 7f-2.

Covers agents/mind_mapper.py's two entry points, per the module's own
bug #6 fix history:

  1. generate_mindmap() — one "mapper" role call over a Mode C packet,
     typed {"kind": "mermaid"|"markdown", "text": str} result, one
     silent retry when the first attempt isn't a validly-fenced,
     heuristically-sane Mermaid answer.
  2. generate_suggested_route() — pure deterministic reshaping of
     Backlink Detector's own "prerequisite-of" connections into a
     flowchart, no LLM call at all.

_attempt() is exercised both directly (patching agents.generic_worker.run,
same pattern test_agent_workflow_suggester.py uses) and indirectly via
generate_mindmap() with _attempt() itself monkeypatched, to keep the
retry-logic tests independent of the fence-extraction regex.
utils.mermaid_lint.looks_valid_mermaid is real (deterministic, no
mocking needed) -- see that module for what it does and doesn't catch.
"""
import pytest

import agents.mind_mapper as mind_mapper


VALID_FLOWCHART = "flowchart TD\n  A[Start] --> B[End]"
BROKEN_FLOWCHART_NO_EDGES = "flowchart TD\n  A[Just one node]"


# ---------------------------------------------------------------------------
# 1. _context_for()
# ---------------------------------------------------------------------------

class TestContextFor:
    def test_builds_one_section_per_topic_with_summary(self):
        topics = {
            "t1": {"name": "DC Motors", "summary": "A summary about motors."},
        }
        result = mind_mapper._context_for(topics, [])
        assert "--- DC Motors ---" in result
        assert "A summary about motors." in result

    def test_falls_back_to_content_hint_when_no_summary(self):
        topics = {"t1": {"name": "Topic", "content_hint": "hint text here"}}
        result = mind_mapper._context_for(topics, [])
        assert "hint text here" in result

    def test_topic_with_no_body_text_is_omitted(self):
        topics = {"t1": {"name": "Empty Topic"}}
        result = mind_mapper._context_for(topics, [])
        assert result == ""

    def test_missing_name_defaults_to_untitled_topic(self):
        topics = {"t1": {"summary": "some body"}}
        result = mind_mapper._context_for(topics, [])
        assert "--- Untitled topic ---" in result

    def test_body_truncated_to_max_chars_per_topic(self):
        long_body = "x" * (mind_mapper.MAX_CONTENT_CHARS_PER_TOPIC + 500)
        topics = {"t1": {"name": "Long", "summary": long_body}}
        result = mind_mapper._context_for(topics, [])
        # the header line adds extra chars, so just check the body chunk itself
        body_chunk = result.split("---\n", 1)[1]
        assert len(body_chunk) == mind_mapper.MAX_CONTENT_CHARS_PER_TOPIC

    def test_relationships_section_included_when_both_endpoints_resolve(self):
        topics = {
            "t1": {"name": "A", "summary": "body a"},
            "t2": {"name": "B", "summary": "body b"},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"}]
        result = mind_mapper._context_for(topics, connections)
        assert "--- Known relationships ---" in result
        assert "A -> B: elaborates-on" in result

    def test_relationship_missing_relation_defaults_to_related(self):
        topics = {
            "t1": {"name": "A", "summary": "body a"},
            "t2": {"name": "B", "summary": "body b"},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2"}]
        result = mind_mapper._context_for(topics, connections)
        assert "A -> B: related" in result

    def test_relationship_with_unresolvable_endpoint_is_dropped(self):
        topics = {"t1": {"name": "A", "summary": "body a"}}
        connections = [{"from_topic": "t1", "to_topic": "missing", "relation": "restates"}]
        result = mind_mapper._context_for(topics, connections)
        assert "Known relationships" not in result


# ---------------------------------------------------------------------------
# 2. _attempt() — one role call, classified into {kind, text}
# ---------------------------------------------------------------------------

class TestAttempt:
    def test_fenced_mermaid_response_classified_as_mermaid(self, monkeypatch):
        monkeypatch.setattr(
            "agents.generic_worker.run",
            lambda **kw: {"text": f"```mermaid\n{VALID_FLOWCHART}\n```"},
        )
        result = mind_mapper._attempt("some task text")
        assert result == {"kind": "mermaid", "text": VALID_FLOWCHART}

    def test_unfenced_response_classified_as_markdown(self, monkeypatch):
        monkeypatch.setattr(
            "agents.generic_worker.run",
            lambda **kw: {"text": "Here's a description with no fence."},
        )
        result = mind_mapper._attempt("some task text")
        assert result["kind"] == "markdown"
        assert "no fence" in result["text"]

    def test_empty_response_classified_as_markdown_empty_string(self, monkeypatch):
        monkeypatch.setattr("agents.generic_worker.run", lambda **kw: {"text": None})
        result = mind_mapper._attempt("task")
        assert result == {"kind": "markdown", "text": ""}


# ---------------------------------------------------------------------------
# 3. generate_mindmap() — scope resolution, retry logic
# ---------------------------------------------------------------------------

class TestGenerateMindmap:
    def _packet(self, topics=None, connections=None):
        return {"topics": topics or {}, "connections": connections or []}

    def test_raises_lookup_error_when_scope_has_no_readable_content(self, monkeypatch):
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project": self._packet())
        with pytest.raises(LookupError):
            mind_mapper.generate_mindmap("ws-1")

    def test_valid_first_attempt_is_returned_without_retry(self, monkeypatch):
        topics = {"t1": {"name": "A", "summary": "body"}}
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project": self._packet(topics))
        calls = {"n": 0}

        def fake_attempt(task_text):
            calls["n"] += 1
            return {"kind": "mermaid", "text": VALID_FLOWCHART}

        monkeypatch.setattr(mind_mapper, "_attempt", fake_attempt)
        result = mind_mapper.generate_mindmap("ws-1")
        assert result == {"kind": "mermaid", "text": VALID_FLOWCHART}
        assert calls["n"] == 1

    def test_unfenced_first_attempt_triggers_one_retry(self, monkeypatch):
        topics = {"t1": {"name": "A", "summary": "body"}}
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project": self._packet(topics))
        responses = [
            {"kind": "markdown", "text": "prose, no fence"},
            {"kind": "mermaid", "text": VALID_FLOWCHART},
        ]

        def fake_attempt(task_text):
            return responses.pop(0)

        monkeypatch.setattr(mind_mapper, "_attempt", fake_attempt)
        result = mind_mapper.generate_mindmap("ws-1")
        assert result == {"kind": "mermaid", "text": VALID_FLOWCHART}

    def test_syntactically_broken_fenced_mermaid_also_triggers_retry(self, monkeypatch):
        # bug-fix regression: fenced but heuristically-broken (no edges)
        # must retry too, not just an outright missing fence.
        topics = {"t1": {"name": "A", "summary": "body"}}
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project": self._packet(topics))
        responses = [
            {"kind": "mermaid", "text": BROKEN_FLOWCHART_NO_EDGES},
            {"kind": "mermaid", "text": VALID_FLOWCHART},
        ]
        calls = {"n": 0}

        def fake_attempt(task_text):
            calls["n"] += 1
            return responses.pop(0)

        monkeypatch.setattr(mind_mapper, "_attempt", fake_attempt)
        result = mind_mapper.generate_mindmap("ws-1")
        assert calls["n"] == 2
        assert result["text"] == VALID_FLOWCHART

    def test_gives_up_as_markdown_after_one_failed_retry(self, monkeypatch):
        topics = {"t1": {"name": "A", "summary": "body"}}
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project": self._packet(topics))
        monkeypatch.setattr(mind_mapper, "_attempt", lambda task_text: {"kind": "markdown", "text": "still prose"})

        result = mind_mapper.generate_mindmap("ws-1")
        assert result["kind"] == "markdown"

    def test_source_node_ids_scopes_topics_by_covers(self, monkeypatch):
        topics = {
            "t1": {"name": "In scope", "summary": "s", "covers": ["node-a"]},
            "t2": {"name": "Out of scope", "summary": "s", "covers": ["node-b"]},
        }
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project": self._packet(topics))
        captured = {}

        def fake_attempt(task_text):
            captured["task_text"] = task_text
            return {"kind": "mermaid", "text": VALID_FLOWCHART}

        monkeypatch.setattr(mind_mapper, "_attempt", fake_attempt)
        mind_mapper.generate_mindmap("ws-1", source_node_ids=["node-a"])
        assert "In scope" in captured["task_text"]
        assert "Out of scope" not in captured["task_text"]

    def test_connections_scoped_to_topics_in_the_resolved_set(self, monkeypatch):
        topics = {
            "t1": {"name": "In scope", "summary": "s", "covers": ["node-a"]},
            "t2": {"name": "Out of scope", "summary": "s", "covers": ["node-b"]},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        monkeypatch.setattr(mind_mapper, "get_packet",
                             lambda ws, scope="project": self._packet(topics, connections))
        captured = {}

        def fake_attempt(task_text):
            captured["task_text"] = task_text
            return {"kind": "mermaid", "text": VALID_FLOWCHART}

        monkeypatch.setattr(mind_mapper, "_attempt", fake_attempt)
        mind_mapper.generate_mindmap("ws-1", source_node_ids=["node-a"])
        assert "Known relationships" not in captured["task_text"]


# ---------------------------------------------------------------------------
# 4. _sanitize_label()
# ---------------------------------------------------------------------------

class TestSanitizeLabel:
    def test_strips_double_quotes(self):
        assert mind_mapper._sanitize_label('Weird "Quoted" Name') == "Weird Quoted Name"

    def test_none_defaults_to_untitled_topic(self):
        assert mind_mapper._sanitize_label(None) == "Untitled topic"

    def test_blank_string_defaults_to_untitled_topic(self):
        assert mind_mapper._sanitize_label("   ") == "Untitled topic"

    def test_strips_surrounding_whitespace(self):
        assert mind_mapper._sanitize_label("  Motors  ") == "Motors"


# ---------------------------------------------------------------------------
# 5. generate_suggested_route() — deterministic prerequisite-of flowchart
# ---------------------------------------------------------------------------

class TestGenerateSuggestedRoute:
    def _packet(self, topics=None, connections=None):
        return {"topics": topics or {}, "connections": connections or []}

    def test_raises_lookup_error_when_no_prerequisite_edges(self, monkeypatch):
        monkeypatch.setattr(mind_mapper, "get_packet", lambda ws, scope="project", session_id=None: self._packet())
        with pytest.raises(LookupError):
            mind_mapper.generate_suggested_route("ws-1")

    def test_only_prerequisite_of_relation_is_used(self, monkeypatch):
        topics = {"t1": {"name": "A"}, "t2": {"name": "B"}}
        connections = [
            {"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"},
            {"from_topic": "t1", "to_topic": "t2", "relation": "prerequisite-of"},
        ]
        monkeypatch.setattr(mind_mapper, "get_packet",
                             lambda ws, scope="project", session_id=None: self._packet(topics, connections))
        result = mind_mapper.generate_suggested_route("ws-1")
        assert result["kind"] == "mermaid"
        assert result["text"].count("-->") == 1

    def test_edges_referencing_unresolvable_topics_are_dropped(self, monkeypatch):
        topics = {"t1": {"name": "A"}}
        connections = [{"from_topic": "t1", "to_topic": "missing", "relation": "prerequisite-of"}]
        monkeypatch.setattr(mind_mapper, "get_packet",
                             lambda ws, scope="project", session_id=None: self._packet(topics, connections))
        with pytest.raises(LookupError):
            mind_mapper.generate_suggested_route("ws-1")

    def test_flowchart_header_and_stable_short_node_ids(self, monkeypatch):
        topics = {"t1": {"name": "A"}, "t2": {"name": "B"}}
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "prerequisite-of"}]
        monkeypatch.setattr(mind_mapper, "get_packet",
                             lambda ws, scope="project", session_id=None: self._packet(topics, connections))
        result = mind_mapper.generate_suggested_route("ws-1")
        assert result["text"].startswith("flowchart TD")
        assert "t0" in result["text"] and "t1" in result["text"]

    def test_duplicate_pairs_are_deduped(self, monkeypatch):
        topics = {"t1": {"name": "A"}, "t2": {"name": "B"}}
        connections = [
            {"from_topic": "t1", "to_topic": "t2", "relation": "prerequisite-of"},
            {"from_topic": "t1", "to_topic": "t2", "relation": "prerequisite-of"},
        ]
        monkeypatch.setattr(mind_mapper, "get_packet",
                             lambda ws, scope="project", session_id=None: self._packet(topics, connections))
        result = mind_mapper.generate_suggested_route("ws-1")
        assert result["text"].count("-->") == 1
