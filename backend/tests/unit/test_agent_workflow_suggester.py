"""
tests/unit/test_agent_workflow_suggester.py — Patch 7f-1.

Covers agents/workflow_suggester.py. This module has two independent
jobs (see its own module docstring):

  1. suggest_workflows() — whole-notebook DETECTION pass, 0-4 procedures,
     empty is a valid/expected result, raises LookupError on empty scope.
  2. build_topic_workflow() — single-topic SYNTHESIS, always returns
     exactly one workflow, never raises, falls back to a generic
     hardcoded mastery sequence on any failure.

Both share the same {title, description, steps, mermaid} validation
helpers (_parse_steps/_parse_workflow) and the same bug-#6 discipline:
never hand back something pretending to be a diagram when it isn't one.
These tests exercise the pure helpers directly (fast, no mocking needed)
and the two public entry points with `plan`/generic_worker.run faked out.
"""
import json

import pytest

from agents import workflow_suggester

VALID_MERMAID = (
    "flowchart TD\n"
    "  S1[Close field circuit] --> S2[Set starter to first position]\n"
    "  S2 --> S3{Fuse blown?}"
)


# ---------------------------------------------------------------------------
# 1. _parse_steps
# ---------------------------------------------------------------------------

class TestParseSteps:
    def test_valid_steps_pass_through(self):
        raw = [
            {"id": "S1", "label": "Close field circuit", "type": "step"},
            {"id": "S2", "label": "Fuse blown?", "type": "decision"},
        ]
        result = workflow_suggester._parse_steps(raw)
        assert result == raw

    def test_none_input_returns_empty_list(self):
        assert workflow_suggester._parse_steps(None) == []

    def test_non_dict_items_are_dropped(self):
        raw = ["not a dict", {"id": "S1", "label": "ok", "type": "step"}, 42]
        result = workflow_suggester._parse_steps(raw)
        assert len(result) == 1
        assert result[0]["id"] == "S1"

    def test_missing_id_is_dropped(self):
        raw = [{"label": "no id here", "type": "step"}]
        assert workflow_suggester._parse_steps(raw) == []

    def test_missing_label_is_dropped(self):
        raw = [{"id": "S1", "type": "step"}]
        assert workflow_suggester._parse_steps(raw) == []

    def test_duplicate_ids_keep_only_the_first(self):
        raw = [
            {"id": "S1", "label": "first", "type": "step"},
            {"id": "S1", "label": "duplicate", "type": "step"},
        ]
        result = workflow_suggester._parse_steps(raw)
        assert len(result) == 1
        assert result[0]["label"] == "first"

    def test_invalid_type_normalizes_to_step(self):
        raw = [{"id": "S1", "label": "weird type", "type": "loop-forever"}]
        result = workflow_suggester._parse_steps(raw)
        assert result[0]["type"] == "step"

    def test_missing_type_defaults_to_step(self):
        raw = [{"id": "S1", "label": "no type given"}]
        result = workflow_suggester._parse_steps(raw)
        assert result[0]["type"] == "step"

    def test_decision_type_is_preserved(self):
        raw = [{"id": "S1", "label": "a question", "type": "decision"}]
        result = workflow_suggester._parse_steps(raw)
        assert result[0]["type"] == "decision"


# ---------------------------------------------------------------------------
# 2. _parse_workflow
# ---------------------------------------------------------------------------

class TestParseWorkflow:
    def _valid_item(self, **overrides):
        item = {
            "title": "Starting a DC Shunt Motor",
            "description": "Sequence for safely starting the motor.",
            "steps": [
                {"id": "S1", "label": "Close field circuit", "type": "step"},
                {"id": "S2", "label": "Set starter", "type": "step"},
                {"id": "S3", "label": "Fuse blown?", "type": "decision"},
            ],
            "mermaid": VALID_MERMAID,
        }
        item.update(overrides)
        return item

    def test_valid_item_parses_successfully(self):
        result = workflow_suggester._parse_workflow(self._valid_item())
        assert result["title"] == "Starting a DC Shunt Motor"
        assert result["mermaid"] == VALID_MERMAID
        assert len(result["steps"]) == 3

    def test_non_dict_returns_none(self):
        assert workflow_suggester._parse_workflow("not a dict") is None
        assert workflow_suggester._parse_workflow(None) is None

    def test_missing_title_returns_none(self):
        item = self._valid_item(title="")
        assert workflow_suggester._parse_workflow(item) is None

    def test_missing_steps_returns_none(self):
        item = self._valid_item(steps=[])
        assert workflow_suggester._parse_workflow(item) is None

    def test_missing_mermaid_returns_none(self):
        item = self._valid_item(mermaid="")
        assert workflow_suggester._parse_workflow(item) is None

    def test_fenced_mermaid_block_is_unwrapped(self):
        item = self._valid_item(mermaid=f"```mermaid\n{VALID_MERMAID}\n```")
        result = workflow_suggester._parse_workflow(item)
        assert result["mermaid"] == VALID_MERMAID

    def test_bug6_a_flowchart_with_zero_edges_is_dropped_but_steps_survive(self):
        # looks_valid_mermaid() rejects a flowchart/graph with no edge
        # tokens -- per bug #6's discipline, the workflow itself is NOT
        # dropped (title/description/steps still usable as a checklist),
        # only the mermaid field is nulled out.
        item = self._valid_item(mermaid="flowchart TD\n  S1[Just one node, no arrows]")
        result = workflow_suggester._parse_workflow(item)
        assert result is not None
        assert result["mermaid"] is None
        assert len(result["steps"]) == 3

    def test_description_defaults_to_empty_string(self):
        item = self._valid_item(description=None)
        result = workflow_suggester._parse_workflow(item)
        assert result["description"] == ""


# ---------------------------------------------------------------------------
# 3. _find_topic
# ---------------------------------------------------------------------------

class TestFindTopic:
    def _topics(self):
        return {
            "t1": {"name": "DC Motors", "covers": ["node-a"]},
            "t2": {"name": "Alternators", "covers": ["node-b"]},
        }

    def test_case_insensitive_exact_match(self):
        tid, topic = workflow_suggester._find_topic(self._topics(), "dc motors", None)
        assert tid == "t1"
        assert topic["name"] == "DC Motors"

    def test_no_match_returns_none_none(self):
        tid, topic = workflow_suggester._find_topic(self._topics(), "Transformers", None)
        assert tid is None
        assert topic is None

    def test_scoped_to_source_node_ids_excludes_non_covering_topics(self):
        tid, topic = workflow_suggester._find_topic(self._topics(), "Alternators", ["node-a"])
        assert tid is None
        assert topic is None

    def test_scoped_match_succeeds_when_node_id_covers(self):
        tid, topic = workflow_suggester._find_topic(self._topics(), "Alternators", ["node-b"])
        assert tid == "t2"


# ---------------------------------------------------------------------------
# 4. _generic_fallback_workflow / _slugify_topic_label
# ---------------------------------------------------------------------------

class TestGenericFallback:
    def test_always_returns_six_steps_with_a_decision(self):
        result = workflow_suggester._generic_fallback_workflow("Thermodynamics")
        assert len(result["steps"]) == 6
        assert any(s["type"] == "decision" for s in result["steps"])
        assert result["mermaid"]

    def test_blank_label_falls_back_to_this_topic(self):
        result = workflow_suggester._generic_fallback_workflow("")
        assert "this topic" in result["title"].lower()

    def test_quotes_in_label_are_stripped_from_mermaid(self):
        result = workflow_suggester._generic_fallback_workflow('Weird "Quoted" Topic')
        assert '"' not in result["mermaid"]


class TestSlugifyTopicLabel:
    def test_lowercases_and_collapses_non_alnum(self):
        assert workflow_suggester._slugify_topic_label("DC Motors & Generators!") == "dc_motors_generators"

    def test_blank_label_returns_topic(self):
        assert workflow_suggester._slugify_topic_label("") == "topic"
        assert workflow_suggester._slugify_topic_label(None) == "topic"


# ---------------------------------------------------------------------------
# 5. suggest_workflows() — detection pass
# ---------------------------------------------------------------------------

class TestSuggestWorkflows:
    def _fake_plan(self, topics):
        def _plan(workspace_id, task_text, scope="project", **kwargs):
            return {"topics": topics}
        return _plan

    def test_raises_lookup_error_when_scope_has_no_readable_content(self, monkeypatch):
        monkeypatch.setattr(workflow_suggester, "plan", self._fake_plan({}))
        with pytest.raises(LookupError):
            workflow_suggester.suggest_workflows("ws-1")

    def test_empty_workflows_list_is_a_valid_result(self, monkeypatch):
        topics = {"t1": {"name": "History", "summary": "some descriptive text"}}
        monkeypatch.setattr(workflow_suggester, "plan", self._fake_plan(topics))

        def fake_run_role(**kwargs):
            return {"text": '```json\n{"workflows": []}\n```'}

        monkeypatch.setattr("agents.generic_worker.run", fake_run_role)
        result = workflow_suggester.suggest_workflows("ws-1")
        assert result == {"workflows": []}

    def test_valid_workflows_are_parsed_and_capped_at_max(self, monkeypatch):
        topics = {"t1": {"name": "Motors", "summary": "a starting procedure exists"}}
        monkeypatch.setattr(workflow_suggester, "plan", self._fake_plan(topics))

        one_workflow = {
            "title": "Start Sequence",
            "description": "desc",
            "steps": [{"id": "S1", "label": "Do it", "type": "step"}],
            "mermaid": "flowchart TD\n  S1[Do it] --> S1",
        }
        payload = {"workflows": [one_workflow] * 6}  # more than MAX_WORKFLOWS

        def fake_run_role(**kwargs):
            return {"text": f"```json\n{json.dumps(payload)}\n```"}

        monkeypatch.setattr("agents.generic_worker.run", fake_run_role)
        result = workflow_suggester.suggest_workflows("ws-1")
        assert len(result["workflows"]) == workflow_suggester.MAX_WORKFLOWS

    def test_source_node_ids_scopes_topics_by_covers(self, monkeypatch):
        topics = {
            "t1": {"name": "In scope", "summary": "text", "covers": ["node-a"]},
            "t2": {"name": "Out of scope", "summary": "text", "covers": ["node-b"]},
        }
        monkeypatch.setattr(workflow_suggester, "plan", self._fake_plan(topics))
        captured = {}

        def fake_run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": '```json\n{"workflows": []}\n```'}

        monkeypatch.setattr("agents.generic_worker.run", fake_run_role)
        workflow_suggester.suggest_workflows("ws-1", source_node_ids=["node-a"])
        assert "In scope" in captured["task_text"]
        assert "Out of scope" not in captured["task_text"]

    def test_unparseable_model_response_yields_empty_workflows_not_an_error(self, monkeypatch):
        topics = {"t1": {"name": "Topic", "summary": "text"}}
        monkeypatch.setattr(workflow_suggester, "plan", self._fake_plan(topics))

        def fake_run_role(**kwargs):
            return {"text": "not json at all, no fence"}

        monkeypatch.setattr("agents.generic_worker.run", fake_run_role)
        result = workflow_suggester.suggest_workflows("ws-1")
        assert result == {"workflows": []}


# ---------------------------------------------------------------------------
# 6. build_topic_workflow() — single-topic synthesis, never raises/empty
# ---------------------------------------------------------------------------

class TestBuildTopicWorkflow:
    def test_falls_back_to_generic_when_plan_raises(self, monkeypatch):
        def _raising_plan(*a, **k):
            raise RuntimeError("workspace unreadable")

        monkeypatch.setattr(workflow_suggester, "plan", _raising_plan)
        result = workflow_suggester.build_topic_workflow("ws-1", "Some Topic")
        assert result["topic_id"] is None
        assert result["topic_key"] == "some_topic"
        assert len(result["steps"]) == 6  # the hardcoded generic sequence

    def test_falls_back_to_generic_when_topic_label_does_not_match(self, monkeypatch):
        monkeypatch.setattr(
            workflow_suggester, "plan",
            lambda *a, **k: {"topics": {"t1": {"name": "Unrelated Topic"}}},
        )
        result = workflow_suggester.build_topic_workflow("ws-1", "Nonexistent Topic")
        assert result["topic_id"] is None
        assert result["topic_key"] == "nonexistent_topic"

    def test_falls_back_to_generic_when_model_response_does_not_parse(self, monkeypatch):
        monkeypatch.setattr(
            workflow_suggester, "plan",
            lambda *a, **k: {"topics": {"t1": {"name": "My Topic"}}},
        )
        monkeypatch.setattr(
            "agents.generic_worker.run",
            lambda **k: {"text": "garbage, no json fence"},
        )
        result = workflow_suggester.build_topic_workflow("ws-1", "My Topic")
        assert len(result["steps"]) == 6

    def test_successful_match_uses_real_topic_id(self, monkeypatch):
        monkeypatch.setattr(
            workflow_suggester, "plan",
            lambda *a, **k: {"topics": {"t1": {"name": "My Topic", "summary": "text"}}},
        )
        payload = {
            "workflow": {
                "title": "My Topic — Mastery Path",
                "description": "desc",
                "steps": [{"id": "S1", "label": "Step one", "type": "step"}],
                "mermaid": "flowchart TD\n  S1[Step one] --> S1",
            }
        }

        def fake_run_role(**kwargs):
            return {"text": f"```json\n{json.dumps(payload)}\n```"}

        monkeypatch.setattr("agents.generic_worker.run", fake_run_role)
        result = workflow_suggester.build_topic_workflow("ws-1", "My Topic")
        assert result["topic_id"] == "t1"
        assert result["topic_key"] == "t1"
        assert result["title"] == "My Topic — Mastery Path"

    def test_blank_topic_label_defaults_to_this_topic(self, monkeypatch):
        monkeypatch.setattr(
            workflow_suggester, "plan",
            lambda *a, **k: {"topics": {}},
        )
        result = workflow_suggester.build_topic_workflow("ws-1", "")
        assert result["topic_key"] == "this_topic"

    def test_never_raises_even_on_generic_worker_exception(self, monkeypatch):
        monkeypatch.setattr(
            workflow_suggester, "plan",
            lambda *a, **k: {"topics": {"t1": {"name": "My Topic"}}},
        )

        def _raising_run(**kwargs):
            raise RuntimeError("provider outage")

        monkeypatch.setattr("agents.generic_worker.run", _raising_run)
        result = workflow_suggester.build_topic_workflow("ws-1", "My Topic")
        assert result is not None
        assert len(result["steps"]) == 6
