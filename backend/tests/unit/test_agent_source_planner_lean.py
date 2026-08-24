"""
tests/unit/test_agent_source_planner_lean.py — Patch 7f-4c-1.

Covers agents/source_planner_lean.py's Mode B: plan() and plan_depth()
(the two entry points), plus their shared helpers _skeleton_context(),
_parse_decision(), and _attach_excerpts().

eo.source_index.get_packet() / get_packet_depth() and
eo.knowledge_graph.get_node() are monkeypatched directly on the module
under test (bound-name imports, same posture the rest of this suite
takes with generic_worker.run). The deferred `from agents.generic_worker
import run as run_role` is faked via a sys.modules substitute, matching
test_agent_fact_detector.py's approach. _ensure_role_registered() is
stubbed to a no-op via an autouse fixture for most tests, except the
small dedicated class that exercises it for real through the autouse
`fake_bus` fixture -- same split test_agent_backlink_detector.py makes.
"""
import sys
from unittest.mock import MagicMock

import pytest

from agents import source_planner_lean

# Captured before the autouse fixture below ever runs (module import
# happens at collection time), so TestEnsureRoleRegistered can call the
# real implementation directly instead of the no-op every other test
# in this file gets.
_REAL_ENSURE_ROLE_REGISTERED = source_planner_lean._ensure_role_registered


def _topic(name="Topic A", summary="summary A", content_hint="hint A",
           covers=None, user_corrected=False):
    t = {"name": name, "summary": summary, "content_hint": content_hint,
         "covers": covers if covers is not None else []}
    if user_corrected:
        t["user_corrected"] = True
    return t


@pytest.fixture(autouse=True)
def _fake_ensure_role(monkeypatch):
    monkeypatch.setattr(source_planner_lean, "_ensure_role_registered", lambda: None)


def _fake_generic_worker(text):
    """Injects a fake agents.generic_worker module with a `run` that
    returns {"text": text}, and returns that mock for call inspection."""
    module = type("M", (), {"run": MagicMock(return_value={"text": text})})()
    sys.modules["agents.generic_worker"] = module
    return module.run


def _capturing_generic_worker(text="```json\n{\"needs_excerpts\": []}\n```"):
    captured = {}

    def _run(**kwargs):
        captured.update(kwargs)
        return {"text": text}

    module = type("M", (), {"run": staticmethod(_run)})()
    sys.modules["agents.generic_worker"] = module
    return captured


# ---------------------------------------------------------------------------
# 1. _skeleton_context(): the LLM-facing packet text
# ---------------------------------------------------------------------------
class TestSkeletonContext:
    def test_includes_task_text_header(self):
        ctx = source_planner_lean._skeleton_context({}, "find the config value")
        assert ctx.startswith("TASK: find the config value")

    def test_one_line_per_topic_with_bracketed_id(self):
        topics = {
            "t1": _topic(name="Motors", summary="about motors", content_hint="engineering"),
            "t2": _topic(name="Recipes", summary="about food", content_hint="cooking"),
        }
        ctx = source_planner_lean._skeleton_context(topics, "task")
        assert "[t1] Motors (engineering): about motors" in ctx
        assert "[t2] Recipes (cooking): about food" in ctx

    def test_covers_is_never_included(self):
        topics = {"t1": _topic(covers=["n1", "n2", "n3"])}
        ctx = source_planner_lean._skeleton_context(topics, "task")
        assert "n1" not in ctx
        assert "n2" not in ctx
        assert "covers" not in ctx

    def test_empty_topics_still_has_task_and_header(self):
        ctx = source_planner_lean._skeleton_context({}, "task")
        assert "TASK: task" in ctx
        assert "TOPICS:" in ctx


# ---------------------------------------------------------------------------
# 2. _parse_decision(): degrade-don't-break parsing
# ---------------------------------------------------------------------------
class TestParseDecision:
    def test_no_fenced_block_returns_empty(self):
        assert source_planner_lean._parse_decision("just prose", {"t1"}) == []

    def test_none_raw_returns_empty(self):
        assert source_planner_lean._parse_decision(None, {"t1"}) == []

    def test_malformed_json_in_fence_returns_empty(self):
        raw = "```json\n{not valid json\n```"
        assert source_planner_lean._parse_decision(raw, {"t1"}) == []

    def test_json_that_is_not_a_dict_returns_empty(self):
        raw = '```json\n["t1", "t2"]\n```'
        assert source_planner_lean._parse_decision(raw, {"t1", "t2"}) == []

    def test_needs_excerpts_missing_returns_empty(self):
        raw = '```json\n{"other_key": ["t1"]}\n```'
        assert source_planner_lean._parse_decision(raw, {"t1"}) == []

    def test_needs_excerpts_not_a_list_returns_empty(self):
        raw = '```json\n{"needs_excerpts": "t1"}\n```'
        assert source_planner_lean._parse_decision(raw, {"t1"}) == []

    def test_valid_flagged_ids_returned(self):
        raw = '```json\n{"needs_excerpts": ["t1", "t2"]}\n```'
        result = source_planner_lean._parse_decision(raw, {"t1", "t2", "t3"})
        assert result == ["t1", "t2"]

    def test_ids_outside_valid_set_are_dropped(self):
        raw = '```json\n{"needs_excerpts": ["t1", "unknown_id"]}\n```'
        result = source_planner_lean._parse_decision(raw, {"t1"})
        assert result == ["t1"]

    def test_non_string_ids_are_dropped(self):
        raw = '```json\n{"needs_excerpts": ["t1", 123, null]}\n```'
        result = source_planner_lean._parse_decision(raw, {"t1"})
        assert result == ["t1"]

    def test_empty_needs_excerpts_list_returns_empty(self):
        raw = '```json\n{"needs_excerpts": []}\n```'
        assert source_planner_lean._parse_decision(raw, {"t1"}) == []


# ---------------------------------------------------------------------------
# 3. _attach_excerpts(): mutates topics in place
# ---------------------------------------------------------------------------
class TestAttachExcerpts:
    def test_unflagged_topics_are_left_untouched(self, monkeypatch):
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: None)
        topics = {"t1": _topic(covers=["n1"])}
        source_planner_lean._attach_excerpts("ws1", topics, [])
        assert "excerpts" not in topics["t1"]

    def test_flagged_topic_gets_joined_node_content(self, monkeypatch):
        nodes = {"n1": {"content": "first chunk"}, "n2": {"content": "second chunk"}}
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: nodes.get(nid))
        topics = {"t1": _topic(covers=["n1", "n2"])}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"] == "first chunk\n\nsecond chunk"

    def test_flagged_topic_with_no_covers_gets_empty_excerpt(self, monkeypatch):
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: None)
        topics = {"t1": {"name": "T", "summary": "s", "content_hint": "h"}}  # no "covers" key
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"] == ""

    def test_missing_node_is_skipped_not_erroring(self, monkeypatch):
        nodes = {"n1": {"content": "only this one exists"}}
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: nodes.get(nid))
        topics = {"t1": _topic(covers=["n1", "n_missing"])}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"] == "only this one exists"

    def test_flagged_id_not_present_in_topics_is_skipped_without_error(self, monkeypatch):
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: None)
        topics = {"t1": _topic()}
        source_planner_lean._attach_excerpts("ws1", topics, ["nonexistent_id"])
        assert "excerpts" not in topics["t1"]

    def test_content_truncated_to_max_chars_per_node(self, monkeypatch):
        long_content = "x" * (source_planner_lean.MAX_EXCERPT_CHARS_PER_NODE + 5000)
        monkeypatch.setattr(
            source_planner_lean, "get_node",
            lambda ws, nid: {"content": long_content},
        )
        topics = {"t1": _topic(covers=["n1"])}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert len(topics["t1"]["excerpts"]) == source_planner_lean.MAX_EXCERPT_CHARS_PER_NODE

    def test_empty_node_content_is_skipped(self, monkeypatch):
        nodes = {"n1": {"content": "   "}, "n2": {"content": "real content"}}
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: nodes.get(nid))
        topics = {"t1": _topic(covers=["n1", "n2"])}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"] == "real content"

    def test_user_corrected_topic_gets_note_prepended_when_excerpt_nonempty(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_node",
            lambda ws, nid: {"content": "the actual excerpt text"},
        )
        topics = {"t1": _topic(covers=["n1"], user_corrected=True)}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"].startswith("NOTE:")
        assert topics["t1"]["excerpts"].endswith("the actual excerpt text")

    def test_user_corrected_topic_with_empty_excerpt_gets_no_note(self, monkeypatch):
        monkeypatch.setattr(source_planner_lean, "get_node", lambda ws, nid: None)
        topics = {"t1": _topic(covers=["n1"], user_corrected=True)}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"] == ""

    def test_non_user_corrected_topic_gets_no_note(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_node",
            lambda ws, nid: {"content": "plain excerpt"},
        )
        topics = {"t1": _topic(covers=["n1"], user_corrected=False)}
        source_planner_lean._attach_excerpts("ws1", topics, ["t1"])
        assert topics["t1"]["excerpts"] == "plain excerpt"


# ---------------------------------------------------------------------------
# 4. plan(): Mode B's full entry point
# ---------------------------------------------------------------------------
class TestPlan:
    def test_empty_topics_short_circuits_with_no_llm_call(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet",
            lambda ws, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope, "topics": {}, "connections": [],
            },
        )
        sys.modules.pop("agents.generic_worker", None)
        result = source_planner_lean.plan("ws1", "some task")
        assert result["needs_excerpts"] == []
        assert result["topics"] == {}
        assert "agents.generic_worker" not in sys.modules

    def test_flags_and_attaches_excerpts_for_flagged_topics(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet",
            lambda ws, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope,
                "topics": {
                    "t1": _topic(name="A", covers=["n1"]),
                    "t2": _topic(name="B", covers=["n2"]),
                },
                "connections": [],
            },
        )
        monkeypatch.setattr(
            source_planner_lean, "get_node",
            lambda ws, nid: {"content": f"content of {nid}"},
        )
        _fake_generic_worker('```json\n{"needs_excerpts": ["t1"]}\n```')

        result = source_planner_lean.plan("ws1", "find the exact figure")

        assert result["needs_excerpts"] == ["t1"]
        assert result["topics"]["t1"]["excerpts"] == "content of n1"
        assert "excerpts" not in result["topics"]["t2"]

    def test_task_text_and_topics_passed_into_run_role(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet",
            lambda ws, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope,
                "topics": {"t1": _topic(name="Widgets")}, "connections": [],
            },
        )
        captured = _capturing_generic_worker()

        source_planner_lean.plan("ws1", "what does the widget cost")

        assert captured["role"] == "source_planner_lean"
        assert "what does the widget cost" in captured["task_text"]
        assert "Widgets" in captured["task_text"]
        assert captured["input_keys"] == []
        assert captured["include_conversation_context"] is False

    def test_domain_defaults_to_notes(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet",
            lambda ws, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope,
                "topics": {"t1": _topic()}, "connections": [],
            },
        )
        captured = _capturing_generic_worker()
        source_planner_lean.plan("ws1", "task")
        assert captured["domain"] == "notes"

    def test_custom_domain_is_forwarded(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet",
            lambda ws, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope,
                "topics": {"t1": _topic()}, "connections": [],
            },
        )
        captured = _capturing_generic_worker()
        source_planner_lean.plan("ws1", "task", domain="research")
        assert captured["domain"] == "research"

    def test_session_id_forwarded_to_get_packet_and_run_role(self, monkeypatch):
        received_session_ids = {}

        def fake_get_packet(ws, scope="project", session_id=None):
            received_session_ids["get_packet"] = session_id
            return {"workspace_id": ws, "scope": scope,
                    "topics": {"t1": _topic()}, "connections": []}

        monkeypatch.setattr(source_planner_lean, "get_packet", fake_get_packet)
        captured = _capturing_generic_worker()

        source_planner_lean.plan("ws1", "task", session_id="sess1")

        assert received_session_ids["get_packet"] == "sess1"
        assert captured["session_id"] == "sess1"

    def test_scope_forwarded_to_get_packet(self, monkeypatch):
        received = {}

        def fake_get_packet(ws, scope="project", session_id=None):
            received["scope"] = scope
            return {"workspace_id": ws, "scope": scope, "topics": {}, "connections": []}

        monkeypatch.setattr(source_planner_lean, "get_packet", fake_get_packet)
        source_planner_lean.plan("ws1", "task", scope="chat", session_id="s1")
        assert received["scope"] == "chat"

    def test_no_flags_returns_empty_needs_excerpts_and_no_excerpt_keys(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet",
            lambda ws, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope,
                "topics": {"t1": _topic()}, "connections": [],
            },
        )
        _fake_generic_worker('```json\n{"needs_excerpts": []}\n```')
        result = source_planner_lean.plan("ws1", "task")
        assert result["needs_excerpts"] == []
        assert "excerpts" not in result["topics"]["t1"]


# ---------------------------------------------------------------------------
# 5. plan_depth(): §7b fallback into Mode B for an exhausted tree walk
# ---------------------------------------------------------------------------
class TestPlanDepth:
    def test_not_exhausted_is_a_pure_passthrough_no_llm_call(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet_depth",
            lambda ws, start, depth, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope, "starting_topic_id": start,
                "requested_depth": depth, "reached_depth": depth, "exhausted": False,
                "topics": {"t1": _topic()}, "connections": [],
            },
        )
        sys.modules.pop("agents.generic_worker", None)
        result = source_planner_lean.plan_depth("ws1", "t1", 3, "task")
        assert result["needs_excerpts"] == []
        assert "excerpts" not in result["topics"]["t1"]
        assert "agents.generic_worker" not in sys.modules

    def test_exhausted_calls_role_and_attaches_excerpts(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet_depth",
            lambda ws, start, depth, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope, "starting_topic_id": start,
                "requested_depth": depth, "reached_depth": 1, "exhausted": True,
                "topics": {"t1": _topic(covers=["n1"]), "t2": _topic(name="B")},
                "connections": [],
            },
        )
        monkeypatch.setattr(
            source_planner_lean, "get_node",
            lambda ws, nid: {"content": "deep content"},
        )
        _fake_generic_worker('```json\n{"needs_excerpts": ["t1"]}\n```')

        result = source_planner_lean.plan_depth("ws1", "t1", 5, "find a detail")

        assert result["needs_excerpts"] == ["t1"]
        assert result["topics"]["t1"]["excerpts"] == "deep content"
        assert "excerpts" not in result["topics"]["t2"]

    def test_only_walked_branch_topics_are_judged(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet_depth",
            lambda ws, start, depth, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope, "starting_topic_id": start,
                "requested_depth": depth, "reached_depth": 0, "exhausted": True,
                "topics": {"t1": _topic(name="Only Walked")}, "connections": [],
            },
        )
        captured = _capturing_generic_worker()
        source_planner_lean.plan_depth("ws1", "t1", 2, "task")
        assert "Only Walked" in captured["task_text"]

    def test_domain_and_session_id_forwarded_when_exhausted(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet_depth",
            lambda ws, start, depth, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope, "starting_topic_id": start,
                "requested_depth": depth, "reached_depth": 0, "exhausted": True,
                "topics": {"t1": _topic()}, "connections": [],
            },
        )
        captured = _capturing_generic_worker()
        source_planner_lean.plan_depth(
            "ws1", "t1", 2, "task", session_id="sess2", domain="research"
        )
        assert captured["session_id"] == "sess2"
        assert captured["domain"] == "research"

    def test_returned_dict_preserves_walk_metadata_fields(self, monkeypatch):
        monkeypatch.setattr(
            source_planner_lean, "get_packet_depth",
            lambda ws, start, depth, scope="project", session_id=None: {
                "workspace_id": ws, "scope": scope, "starting_topic_id": start,
                "requested_depth": depth, "reached_depth": 2, "exhausted": False,
                "topics": {}, "connections": [],
            },
        )
        result = source_planner_lean.plan_depth("ws1", "t9", 4, "task")
        assert result["starting_topic_id"] == "t9"
        assert result["requested_depth"] == 4
        assert result["reached_depth"] == 2
        assert result["exhausted"] is False


# ---------------------------------------------------------------------------
# 6. _ensure_role_registered(): exercised for real through the fake bus.
#    Calls _REAL_ENSURE_ROLE_REGISTERED directly (captured at module
#    import time, above) since every test in this file otherwise gets
#    the no-op version from the autouse `_fake_ensure_role` fixture.
# ---------------------------------------------------------------------------
class TestEnsureRoleRegistered:
    def test_registers_brief_when_absent(self, fake_bus):
        assert source_planner_lean.get_role_prompt("source_planner_lean") is None
        _REAL_ENSURE_ROLE_REGISTERED()
        assert source_planner_lean.get_role_prompt("source_planner_lean") is not None

    def test_does_not_overwrite_an_existing_brief(self, fake_bus):
        source_planner_lean.add_role_prompt(
            "source_planner_lean", "a custom pre-existing brief", source="test_seed"
        )
        _REAL_ENSURE_ROLE_REGISTERED()
        assert (
            source_planner_lean.get_role_prompt("source_planner_lean")
            == "a custom pre-existing brief"
        )
