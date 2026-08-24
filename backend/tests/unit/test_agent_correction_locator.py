"""
tests/unit/test_agent_correction_locator.py — Patch 7f-3.

Covers agents/correction_locator.py's locate_correction(): turning a
plain-language correction into a located, proposed JSON Patch
"replace" op against Secondary Data -- never touching Primary Source.
Two-pass posture (module docstring §8b):

  1. No candidate topics in scope -> short-circuit, no LLM call.
  2. _parse_decision(): degrade-don't-break on malformed/missing JSON;
     out-of-scope topic_id dropped; edit fields outside
     _EDITABLE_FIELDS dropped; non-string edit values dropped.
  3. Pass-1-only path: summary alone was enough (needs_source=False,
     edit present) -> _build_op() called directly, no second LLM call.
  4. Pass-2 fallback: pass 1 asks for source, or comes back with no
     usable edit -> real excerpt pulled and a second call made; no
     excerpt available -> gives up with a reason; second pass with no
     edit -> gives up with a reason.
  5. _build_op(): only the edited fields change; a vanished topic
     (concurrent delete) returns None, surfaced as "topic no longer
     exists".
  6. _candidate_topics(): scope_node_ids=None means every topic;
     otherwise only topics whose `covers` intersects scope_node_ids.
"""
from unittest.mock import MagicMock

import pytest

import agents.correction_locator as correction_locator


def _topic(name="Topic A", summary="summary A", content_hint="hint", covers=None):
    return {"name": name, "summary": summary, "content_hint": content_hint, "covers": covers or []}


@pytest.fixture(autouse=True)
def _fake_ensure_role(monkeypatch):
    monkeypatch.setattr(correction_locator, "_ensure_role_registered", lambda: None)


def _install_fake_run_role(*responses):
    """Installs agents.generic_worker with a run() that returns each
    response in `responses` in order (repeats the last if called more
    times than provided)."""
    import sys
    calls = []
    it = iter(responses)
    last = {"text": ""}

    def _run(**kwargs):
        calls.append(kwargs)
        nonlocal last
        try:
            last = {"text": next(it)}
        except StopIteration:
            pass
        return last

    sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run)})()
    return calls


# ---------------------------------------------------------------------------
# 1. No candidate topics -> short-circuit
# ---------------------------------------------------------------------------
class TestNoCandidateTopics:
    def test_empty_topics_returns_reason_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {"topics": {}})
        calls = _install_fake_run_role('{"topic_id": null, "needs_source": false, "edit": null}')
        result = correction_locator.locate_correction("ws1", "fix the name")
        assert result["op"] is None
        assert result["reason"] == "no topics in scope to search"
        assert calls == []


# ---------------------------------------------------------------------------
# 2. _parse_decision()
# ---------------------------------------------------------------------------
class TestParseDecision:
    def test_valid_response_parses_edit(self):
        raw = '```json\n{"topic_id": "t1", "needs_source": false, "edit": {"name": "Corrected"}}\n```'
        decision = correction_locator._parse_decision(raw, {"t1", "t2"})
        assert decision == {"topic_id": "t1", "needs_source": False, "edit": {"name": "Corrected"}}

    def test_no_fenced_block_returns_default(self):
        decision = correction_locator._parse_decision("just prose", {"t1"})
        assert decision == {"topic_id": None, "needs_source": False, "edit": None}

    def test_malformed_json_returns_default(self):
        decision = correction_locator._parse_decision("```json\n{bad json\n```", {"t1"})
        assert decision == {"topic_id": None, "needs_source": False, "edit": None}

    def test_out_of_scope_topic_id_is_dropped(self):
        raw = '```json\n{"topic_id": "t999", "needs_source": false, "edit": {"name": "X"}}\n```'
        decision = correction_locator._parse_decision(raw, {"t1", "t2"})
        assert decision["topic_id"] is None
        assert decision["edit"] is None  # edit dropped too since topic_id is None

    def test_edit_field_outside_editable_fields_is_dropped(self):
        raw = ('```json\n{"topic_id": "t1", "needs_source": false, '
               '"edit": {"name": "OK", "parent": "should_not_be_editable"}}\n```')
        decision = correction_locator._parse_decision(raw, {"t1"})
        assert decision["edit"] == {"name": "OK"}
        assert "parent" not in decision["edit"]

    def test_non_string_edit_value_is_dropped(self):
        raw = '```json\n{"topic_id": "t1", "needs_source": false, "edit": {"name": 123}}\n```'
        decision = correction_locator._parse_decision(raw, {"t1"})
        assert decision["edit"] is None  # only field present was non-string, so edit collapses to None

    def test_needs_source_true_forces_edit_to_none(self):
        raw = '```json\n{"topic_id": "t1", "needs_source": true, "edit": {"name": "X"}}\n```'
        decision = correction_locator._parse_decision(raw, {"t1"})
        assert decision["needs_source"] is True
        assert decision["edit"] is None

    def test_needs_source_true_but_topic_id_none_forces_needs_source_false(self):
        raw = '```json\n{"topic_id": null, "needs_source": true, "edit": null}\n```'
        decision = correction_locator._parse_decision(raw, {"t1"})
        assert decision["needs_source"] is False

    def test_edit_not_a_dict_is_dropped(self):
        raw = '```json\n{"topic_id": "t1", "needs_source": false, "edit": "not a dict"}\n```'
        decision = correction_locator._parse_decision(raw, {"t1"})
        assert decision["edit"] is None

    def test_non_dict_top_level_json_returns_default(self):
        decision = correction_locator._parse_decision("```json\n[1, 2, 3]\n```", {"t1"})
        assert decision == {"topic_id": None, "needs_source": False, "edit": None}


# ---------------------------------------------------------------------------
# 3. Pass-1-only success path
# ---------------------------------------------------------------------------
class TestPassOneSuccess:
    def test_summary_alone_sufficient_builds_op_without_second_call(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic()},
        })
        calls = _install_fake_run_role(
            '```json\n{"topic_id": "t1", "needs_source": false, "edit": {"name": "Corrected Name"}}\n```'
        )
        monkeypatch.setattr(correction_locator, "get_secondary_data", lambda ws: {
            "topics": {"t1": {"name": "Topic A", "summary": "summary A", "parent": "root"}},
        })

        result = correction_locator.locate_correction("ws1", "the name is wrong")
        assert len(calls) == 1  # only pass 1 fired
        assert result["topic_id"] == "t1"
        assert result["op"] == {
            "op": "replace", "path": "/topics/t1",
            "value": {"name": "Corrected Name", "summary": "summary A", "parent": "root"},
        }
        assert result["reason"] is None


# ---------------------------------------------------------------------------
# 4. Pass-2 fallback
# ---------------------------------------------------------------------------
class TestPassTwoFallback:
    def test_needs_source_triggers_second_call_with_excerpt(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        monkeypatch.setattr(correction_locator, "get_node", lambda ws, nid: {"content": "the real source text"})
        monkeypatch.setattr(correction_locator, "get_secondary_data", lambda ws: {
            "topics": {"t1": {"name": "Topic A", "summary": "summary A"}},
        })
        calls = _install_fake_run_role(
            '```json\n{"topic_id": "t1", "needs_source": true, "edit": null}\n```',
            '```json\n{"topic_id": "t1", "needs_source": false, "edit": {"summary": "Corrected from source"}}\n```',
        )

        result = correction_locator.locate_correction("ws1", "the summary is wrong")
        assert len(calls) == 2
        assert "the real source text" in calls[1]["task_text"]
        assert result["op"]["value"]["summary"] == "Corrected from source"

    def test_pass_one_no_usable_edit_also_triggers_pass_two(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        monkeypatch.setattr(correction_locator, "get_node", lambda ws, nid: {"content": "source text"})
        monkeypatch.setattr(correction_locator, "get_secondary_data", lambda ws: {
            "topics": {"t1": {"name": "Topic A", "summary": "summary A"}},
        })
        calls = _install_fake_run_role(
            '```json\n{"topic_id": "t1", "needs_source": false, "edit": null}\n```',  # no edit at all
            '```json\n{"topic_id": "t1", "needs_source": false, "edit": {"name": "Fixed"}}\n```',
        )
        result = correction_locator.locate_correction("ws1", "correction text")
        assert len(calls) == 2
        assert result["op"]["value"]["name"] == "Fixed"

    def test_no_excerpt_available_gives_up_with_reason(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        monkeypatch.setattr(correction_locator, "get_node", lambda ws, nid: None)  # no source content
        _install_fake_run_role(
            '```json\n{"topic_id": "t1", "needs_source": true, "edit": null}\n```',
        )
        result = correction_locator.locate_correction("ws1", "correction text")
        assert result["op"] is None
        assert result["reason"] == "no source excerpt available to verify this correction against"

    def test_second_pass_with_no_edit_gives_up_with_reason(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        monkeypatch.setattr(correction_locator, "get_node", lambda ws, nid: {"content": "source text"})
        _install_fake_run_role(
            '```json\n{"topic_id": "t1", "needs_source": true, "edit": null}\n```',
            '```json\n{"topic_id": "t1", "needs_source": false, "edit": null}\n```',  # excerpt didn't help
        )
        result = correction_locator.locate_correction("ws1", "correction text")
        assert result["op"] is None
        assert result["reason"] == "the source excerpt didn't support this correction"

    def test_no_matching_topic_at_all_gives_up_with_reason(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic()},
        })
        _install_fake_run_role(
            '```json\n{"topic_id": null, "needs_source": false, "edit": null}\n```',
        )
        result = correction_locator.locate_correction("ws1", "correction text")
        assert result["op"] is None
        assert result["reason"] == "couldn't find a matching topic for this correction"


# ---------------------------------------------------------------------------
# 5. _build_op()
# ---------------------------------------------------------------------------
class TestBuildOp:
    def test_only_named_fields_change_others_pass_through(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_secondary_data", lambda ws: {
            "topics": {"t1": {"name": "Old Name", "summary": "Old Summary", "parent": "root",
                              "source_section_ids": ["s1"]}},
        })
        op = correction_locator._build_op("ws1", "t1", {"name": "New Name"})
        assert op == {
            "op": "replace", "path": "/topics/t1",
            "value": {"name": "New Name", "summary": "Old Summary", "parent": "root",
                      "source_section_ids": ["s1"]},
        }

    def test_vanished_topic_returns_none(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_secondary_data", lambda ws: {"topics": {}})
        op = correction_locator._build_op("ws1", "t1", {"name": "New Name"})
        assert op is None

    def test_vanished_topic_surfaces_as_reason_in_locate_correction(self, monkeypatch):
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {
            "topics": {"t1": _topic()},
        })
        monkeypatch.setattr(correction_locator, "get_secondary_data", lambda ws: {"topics": {}})
        _install_fake_run_role(
            '```json\n{"topic_id": "t1", "needs_source": false, "edit": {"name": "X"}}\n```',
        )
        result = correction_locator.locate_correction("ws1", "correction text")
        assert result["op"] is None
        assert result["reason"] == "topic no longer exists"


# ---------------------------------------------------------------------------
# 6. _candidate_topics(): scope filtering
# ---------------------------------------------------------------------------
class TestCandidateTopics:
    def test_none_scope_returns_all_topics(self, monkeypatch):
        all_topics = {"t1": _topic(covers=["n1"]), "t2": _topic(name="B", covers=["n2"])}
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {"topics": all_topics})
        result = correction_locator._candidate_topics("ws1", None)
        assert result == all_topics

    def test_scope_narrows_to_intersecting_topics_only(self, monkeypatch):
        all_topics = {"t1": _topic(covers=["n1"]), "t2": _topic(name="B", covers=["n2"])}
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {"topics": all_topics})
        result = correction_locator._candidate_topics("ws1", {"n1"})
        assert set(result.keys()) == {"t1"}

    def test_scope_with_no_matches_returns_empty(self, monkeypatch):
        all_topics = {"t1": _topic(covers=["n1"])}
        monkeypatch.setattr(correction_locator, "get_packet", lambda *a, **k: {"topics": all_topics})
        result = correction_locator._candidate_topics("ws1", {"n999"})
        assert result == {}
