"""
tests/unit/test_agent_topic_note_writer.py — Patch 7f-7-2a.

Covers agents/topic_note_writer.py's generate_topic_note(): resolves a
single topic via eo/source_index.py's get_packet_depth(requested_depth=0),
pulls that topic's real source content (_excerpt_for_topic() -- a
covers-walk through eo/knowledge_graph.py's get_node(), truncated per
source, falling back to summary/content_hint when no node content
exists), ensures the "topic_note_writer" role is registered
(_ensure_role_registered(), defensive re-add via eo/registry.py's
get_role_prompt/add_role_prompt), and runs it through the deferred
agents.generic_worker.run import with include_conversation_context=False.
The raw response is either the literal string "NONE" (nothing worth
writing), a fenced/unfenced JSON object with title/content/tags, or
something unparseable -- all three collapse to a plain None return
except a valid object with both title and content, which becomes an
eo/note_candidates.py:propose_note() call whose return value is passed
straight back to the caller.

get_packet_depth, get_node, get_role_prompt/add_role_prompt, and
note_candidates.propose_note are all bound names in this module's own
namespace and are monkeypatched directly. The deferred
agents.generic_worker.run import is faked via a stand-in module object
in sys.modules, same shape test_agent_podcast_scriptwriter.py already
uses for that exact import pattern.
"""
import sys
from unittest.mock import MagicMock

import pytest

from agents import topic_note_writer


def _topic(name="Some Topic", covers=None, summary="", content_hint=""):
    return {"name": name, "covers": covers or [], "summary": summary, "content_hint": content_hint}


def _packet(topic_id, topic):
    return {"topics": {topic_id: topic}}


def _fake_generic_worker(text):
    module = type("M", (), {"run": MagicMock(return_value={"text": text})})()
    sys.modules["agents.generic_worker"] = module
    return module.run


def _fenced(obj_json):
    return f"```json\n{obj_json}\n```"


@pytest.fixture(autouse=True)
def _fake_role_prompt(monkeypatch):
    """Defaults to an already-registered role, so add_role_prompt is
    only exercised by the tests that specifically want it."""
    monkeypatch.setattr(topic_note_writer, "get_role_prompt", lambda role: "existing brief")
    add_mock = MagicMock()
    monkeypatch.setattr(topic_note_writer, "add_role_prompt", add_mock)
    return add_mock


@pytest.fixture(autouse=True)
def _fake_note_candidates(monkeypatch):
    mock = MagicMock(return_value={"candidate_id": "note_abc123"})
    monkeypatch.setattr(topic_note_writer.note_candidates, "propose_note", mock)
    return mock


# ---------------------------------------------------------------------------
# 1. _ensure_role_registered(): defensive registration
# ---------------------------------------------------------------------------
class TestEnsureRoleRegistered:
    def test_does_not_re_add_when_already_registered(self, monkeypatch, _fake_role_prompt):
        monkeypatch.setattr(topic_note_writer, "get_role_prompt", lambda role: "already there")
        topic_note_writer._ensure_role_registered()
        _fake_role_prompt.assert_not_called()

    def test_adds_the_seed_brief_when_missing(self, monkeypatch, _fake_role_prompt):
        monkeypatch.setattr(topic_note_writer, "get_role_prompt", lambda role: None)
        topic_note_writer._ensure_role_registered()
        _fake_role_prompt.assert_called_once_with(
            "topic_note_writer", topic_note_writer.TOPIC_NOTE_WRITER_BRIEF,
            source="topic_note_writer_seed",
        )


# ---------------------------------------------------------------------------
# 2. _excerpt_for_topic(): covers-walk, truncation, fallback
# ---------------------------------------------------------------------------
class TestExcerptForTopic:
    def test_pulls_content_from_each_covered_node(self, monkeypatch):
        nodes = {
            "n1": {"content": "first source content"},
            "n2": {"content": "second source content"},
        }
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: nodes.get(nid))
        topic = _topic(covers=["n1", "n2"])
        result = topic_note_writer._excerpt_for_topic("ws1", topic)
        assert "first source content" in result
        assert "second source content" in result

    def test_missing_node_is_skipped(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: None)
        topic = _topic(covers=["n1"], summary="fallback summary")
        result = topic_note_writer._excerpt_for_topic("ws1", topic)
        assert result == "fallback summary"

    def test_content_truncated_per_source(self, monkeypatch):
        long_content = "x" * 10000
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": long_content})
        topic = _topic(covers=["n1"])
        result = topic_note_writer._excerpt_for_topic("ws1", topic)
        assert len(result) <= topic_note_writer.MAX_CONTENT_CHARS_PER_SOURCE

    def test_falls_back_to_summary_when_no_node_content(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": ""})
        topic = _topic(covers=["n1"], summary="a summary")
        result = topic_note_writer._excerpt_for_topic("ws1", topic)
        assert result == "a summary"

    def test_falls_back_to_content_hint_when_no_summary_either(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: None)
        topic = _topic(covers=[], summary="", content_hint="a hint")
        result = topic_note_writer._excerpt_for_topic("ws1", topic)
        assert result == "a hint"

    def test_no_covers_and_no_fallback_text_returns_empty_string(self, monkeypatch):
        topic = _topic(covers=[], summary="", content_hint="")
        result = topic_note_writer._excerpt_for_topic("ws1", topic)
        assert result == ""


# ---------------------------------------------------------------------------
# 3. generate_topic_note(): no real content -> None, no LLM call
# ---------------------------------------------------------------------------
class TestNoContent:
    def test_empty_excerpt_returns_none_without_running_the_role(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_packet_depth",
                             lambda *a, **k: _packet("t1", _topic(covers=[])))
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: None)
        called = []
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        assert called == []


# ---------------------------------------------------------------------------
# 4. generate_topic_note(): role response handling
# ---------------------------------------------------------------------------
class TestRoleResponse:
    def _setup_packet(self, monkeypatch, node_content="real content here"):
        monkeypatch.setattr(topic_note_writer, "get_packet_depth",
                             lambda *a, **k: _packet("t1", _topic(name="Photosynthesis", covers=["n1"])))
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": node_content})

    def test_none_response_returns_none_without_proposing(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker("NONE")
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_none_response_is_case_insensitive_and_whitespace_tolerant(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker("  none  ")
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_no_json_fence_returns_none(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker("just plain prose, no code block")
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_malformed_json_inside_fence_returns_none(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker("```json\nnot actually json\n```")
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_json_array_instead_of_object_returns_none(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced('["not", "an", "object"]'))
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_missing_title_returns_none(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced('{"content": "some content", "tags": []}'))
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_missing_content_returns_none(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced('{"title": "A Title", "tags": []}'))
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_blank_title_or_content_after_strip_returns_none(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced('{"title": "   ", "content": "real", "tags": []}'))
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()

    def test_valid_response_proposes_note_with_stripped_fields(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced(
            '{"title": "  Photosynthesis  ", "content": "  It converts light. ", "tags": ["biology"]}'
        ))
        topic_note_writer.generate_topic_note("ws1", "t1")
        _fake_note_candidates.assert_called_once_with(
            workspace_id="ws1", title="Photosynthesis", content="It converts light.",
            tags=["biology"], proposed_by="topic_note_writer",
        )

    def test_non_list_tags_default_to_empty_list(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced('{"title": "T", "content": "C", "tags": "not-a-list"}'))
        topic_note_writer.generate_topic_note("ws1", "t1")
        assert _fake_note_candidates.call_args.kwargs["tags"] == []

    def test_missing_tags_key_defaults_to_empty_list(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_generic_worker(_fenced('{"title": "T", "content": "C"}'))
        topic_note_writer.generate_topic_note("ws1", "t1")
        assert _fake_note_candidates.call_args.kwargs["tags"] == []

    def test_returns_the_propose_note_result(self, monkeypatch, _fake_note_candidates):
        self._setup_packet(monkeypatch)
        _fake_note_candidates.return_value = {"candidate_id": "note_xyz"}
        _fake_generic_worker(_fenced('{"title": "T", "content": "C"}'))
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result == {"candidate_id": "note_xyz"}

    def test_unfenced_plain_json_still_returns_none_since_regex_requires_fence(self, monkeypatch, _fake_note_candidates):
        # _JSON_BLOCK_RE only matches ```json ... ``` fenced blocks --
        # a bare JSON object with no fence at all doesn't match, and the
        # module makes no attempt to json.loads() the raw text directly.
        self._setup_packet(monkeypatch)
        _fake_generic_worker('{"title": "T", "content": "C"}')
        result = topic_note_writer.generate_topic_note("ws1", "t1")
        assert result is None
        _fake_note_candidates.assert_not_called()


# ---------------------------------------------------------------------------
# 5. generate_topic_note(): call shape into generic_worker.run / get_packet_depth
# ---------------------------------------------------------------------------
class TestCallShape:
    def test_get_packet_depth_called_with_zero_depth_and_scope(self, monkeypatch):
        captured = {}

        def _fake_get_packet_depth(workspace_id, starting_topic_id=None, requested_depth=None,
                                    scope=None, session_id=None):
            captured.update(
                workspace_id=workspace_id, starting_topic_id=starting_topic_id,
                requested_depth=requested_depth, scope=scope, session_id=session_id,
            )
            return _packet(starting_topic_id, _topic(covers=["n1"]))

        monkeypatch.setattr(topic_note_writer, "get_packet_depth", _fake_get_packet_depth)
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": "content"})
        _fake_generic_worker("NONE")

        topic_note_writer.generate_topic_note("ws1", "t1", scope="notebook", session_id="s1")

        assert captured["workspace_id"] == "ws1"
        assert captured["starting_topic_id"] == "t1"
        assert captured["requested_depth"] == 0
        assert captured["scope"] == "notebook"
        assert captured["session_id"] == "s1"

    def test_bad_topic_id_raises_key_error(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_packet_depth",
                             lambda *a, **k: {"topics": {}})
        with pytest.raises(KeyError):
            topic_note_writer.generate_topic_note("ws1", "nonexistent")

    def test_run_role_called_with_fixed_role_and_notes_domain(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_packet_depth",
                             lambda *a, **k: _packet("t1", _topic(name="My Topic", covers=["n1"])))
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": "the excerpt"})
        run_mock = _fake_generic_worker("NONE")

        topic_note_writer.generate_topic_note("ws1", "t1")

        _, kwargs = run_mock.call_args
        assert kwargs["role"] == "topic_note_writer"
        assert kwargs["input_keys"] == []
        assert kwargs["session_id"] is None
        assert kwargs["include_conversation_context"] is False
        assert kwargs["domain"] == "notes"
        assert "My Topic" in kwargs["task_text"]
        assert "the excerpt" in kwargs["task_text"]

    def test_untitled_topic_gets_default_title_in_task_text(self, monkeypatch):
        monkeypatch.setattr(topic_note_writer, "get_packet_depth",
                             lambda *a, **k: _packet("t1", {"covers": ["n1"]}))
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": "content"})
        run_mock = _fake_generic_worker("NONE")

        topic_note_writer.generate_topic_note("ws1", "t1")

        assert "Untitled topic" in run_mock.call_args.kwargs["task_text"]

    def test_ensure_role_registered_runs_before_the_role_call(self, monkeypatch, _fake_role_prompt):
        monkeypatch.setattr(topic_note_writer, "get_role_prompt", lambda role: None)
        monkeypatch.setattr(topic_note_writer, "get_packet_depth",
                             lambda *a, **k: _packet("t1", _topic(covers=["n1"])))
        monkeypatch.setattr(topic_note_writer, "get_node", lambda ws, nid: {"content": "content"})
        _fake_generic_worker("NONE")

        topic_note_writer.generate_topic_note("ws1", "t1")

        _fake_role_prompt.assert_called_once()
