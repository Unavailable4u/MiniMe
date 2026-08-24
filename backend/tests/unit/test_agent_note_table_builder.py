"""
tests/unit/test_agent_note_table_builder.py — Patch 7f-7-2c.

Covers agents/note_table_builder.py's build_table(): reads a workspace's
Secondary Data topic tree via agents/source_planner_lean.py:plan()
(scope="project"), and — when that packet has topics — runs one
ThreadPoolExecutor worker per topic (_run_extraction/_extract_one_item),
merging results back in the packet's own topic order (not
as_completed() order) into one row per topic. FIX per the module's own
docstring: when plan() comes back with NO topics at all (a workspace
that never went through Notebooks' clustering, e.g. a Research
project), build_table() falls back to eo/knowledge_graph.py's
list_nodes() and extracts one row per raw node instead, with node_type
acting as a real filter on that path only. Raises ValueError when
field_names is empty, or when the workspace has neither topics nor any
ingested nodes.

Also covers the shared worker-pool helpers this module reuses from the
extraction_table_builder.py shape: _eligible_pool()/_select_workers()
(fairness rotation over eo/registry.py's AGENT_CAPABILITIES, ranked by
eo/quota_sentinel.py's get_quota_snapshot()) and _extract_one_item()'s
JSON-parse / fence-strip / degrade-to-null-fields-with-extraction_error
behavior on a bad or failing generate_text() call — same posture
test_agent_extraction_table_builder.py already takes for its
near-identical sibling.

generate_text is faked via the shared `mock_llm` fixture (bound-name
import). AGENT_CAPABILITIES, get_quota_snapshot, plan, and list_nodes
are all bound names in this module's own namespace and are
monkeypatched directly.
"""
import json
from unittest.mock import MagicMock

import pytest

import agents.note_table_builder as ntb


@pytest.fixture(autouse=True)
def _fixed_pool(monkeypatch):
    """Deterministic single-account pool (see module docstring above for
    why this mirrors test_agent_extraction_table_builder.py's fixture)."""
    pool = {
        f"FAKE_KEY_{i}": {"provider": "groq", "natural_roles": ["note_table_builder"]}
        for i in range(1, 6)
    }
    monkeypatch.setattr(ntb, "AGENT_CAPABILITIES", pool)
    monkeypatch.setattr(ntb, "get_quota_snapshot", dict)
    return pool


def _packet(topics):
    return {"topics": topics}


def _topic(name="T", excerpts=None, summary="", covers=None):
    t = {"name": name, "summary": summary, "covers": covers or []}
    if excerpts is not None:
        t["excerpts"] = excerpts
    return t


def _node(node_id, title="N", content="content", tags=None, node_type="source"):
    return {"node_id": node_id, "title": title, "content": content, "tags": tags or [], "node_type": node_type}


# ---------------------------------------------------------------------------
# 1. build_table(): guard clauses
# ---------------------------------------------------------------------------
class TestGuards:
    def test_empty_field_names_raises_value_error(self, mock_llm):
        with pytest.raises(ValueError):
            ntb.build_table("ws1", [])

    def test_no_topics_and_no_nodes_raises_value_error(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        monkeypatch.setattr(ntb, "list_nodes", lambda ws, node_type=None: [])
        with pytest.raises(ValueError):
            ntb.build_table("ws1", ["field_a"])


# ---------------------------------------------------------------------------
# 2. build_table(): topic path (Secondary Data / Notebooks clustering)
# ---------------------------------------------------------------------------
class TestTopicPath:
    def test_extracts_one_row_per_topic_in_packet_order(self, monkeypatch, mock_llm):
        topics = {
            "t1": _topic(name="Alpha", excerpts="alpha content"),
            "t2": _topic(name="Beta", excerpts="beta content"),
        }
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet(topics))
        mock_llm.set_json_response({"field_a": "value"})

        result = ntb.build_table("ws1", ["field_a"])

        assert [r["topic_id"] for r in result["rows"]] == ["t1", "t2"]
        assert [r["title"] for r in result["rows"]] == ["Alpha", "Beta"]
        assert all(r["field_a"] == "value" for r in result["rows"])

    def test_falls_back_to_summary_when_no_excerpts(self, monkeypatch, mock_llm):
        topics = {"t1": _topic(name="Alpha", excerpts=None, summary="just a summary")}
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet(topics))
        captured = {}

        def _fake_generate_text(system_prompt, user_content, chain, **kwargs):
            captured["user_content"] = user_content
            return json.dumps({"field_a": None})

        monkeypatch.setattr(ntb, "generate_text", _fake_generate_text)
        ntb.build_table("ws1", ["field_a"])
        assert "just a summary" in captured["user_content"]

    def test_field_names_and_summary_shape(self, monkeypatch, mock_llm):
        topics = {"t1": _topic(excerpts="content")}
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet(topics))
        mock_llm.set_json_response({"field_a": None})

        result = ntb.build_table("ws1", ["field_a", "field_b"])

        assert result["field_names"] == ["field_a", "field_b"]
        assert "1 topic" in result["summary"]

    def test_node_type_is_accepted_but_unused_on_topic_path(self, monkeypatch, mock_llm):
        topics = {"t1": _topic(excerpts="content")}
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet(topics))
        list_nodes_mock = MagicMock()
        monkeypatch.setattr(ntb, "list_nodes", list_nodes_mock)
        mock_llm.set_json_response({"field_a": None})

        ntb.build_table("ws1", ["field_a"], node_type="source")

        # Topic path never touches list_nodes() at all -- node_type only
        # matters on the raw-node fallback path.
        list_nodes_mock.assert_not_called()

    def test_plan_called_with_project_scope_and_field_list_in_task_text(self, monkeypatch, mock_llm):
        captured = {}

        def _fake_plan(workspace_id, task_text=None, scope=None, session_id=None):
            captured.update(workspace_id=workspace_id, task_text=task_text, scope=scope)
            return _packet({"t1": _topic(excerpts="content")})

        monkeypatch.setattr(ntb, "plan", _fake_plan)
        mock_llm.set_json_response({"field_a": None})

        ntb.build_table("ws1", ["field_a", "field_b"])

        assert captured["workspace_id"] == "ws1"
        assert captured["scope"] == "project"
        assert "field_a" in captured["task_text"]
        assert "field_b" in captured["task_text"]


# ---------------------------------------------------------------------------
# 3. build_table(): raw-node fallback (no topics at all)
# ---------------------------------------------------------------------------
class TestRawNodeFallback:
    def test_falls_back_to_list_nodes_when_no_topics(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        nodes = [_node("n1", title="Source One", content="c1"), _node("n2", title="Source Two", content="c2")]
        monkeypatch.setattr(ntb, "list_nodes", lambda ws, node_type=None: nodes)
        mock_llm.set_json_response({"field_a": "value"})

        result = ntb.build_table("ws1", ["field_a"])

        assert {r["node_id"] for r in result["rows"]} == {"n1", "n2"}
        assert "2 source" in result["summary"]

    def test_node_type_filter_is_passed_through_to_list_nodes(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        captured = {}

        def _fake_list_nodes(workspace_id, node_type=None):
            captured["node_type"] = node_type
            return [_node("n1")]

        monkeypatch.setattr(ntb, "list_nodes", _fake_list_nodes)
        mock_llm.set_json_response({"field_a": None})

        ntb.build_table("ws1", ["field_a"], node_type="source")

        assert captured["node_type"] == "source"

    def test_blank_node_type_passed_as_none(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        captured = {}

        def _fake_list_nodes(workspace_id, node_type=None):
            captured["node_type"] = node_type
            return [_node("n1")]

        monkeypatch.setattr(ntb, "list_nodes", _fake_list_nodes)
        mock_llm.set_json_response({"field_a": None})

        ntb.build_table("ws1", ["field_a"], node_type="")

        assert captured["node_type"] is None

    def test_rows_include_tags_from_the_node(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        monkeypatch.setattr(ntb, "list_nodes", lambda ws, node_type=None: [_node("n1", tags=["x", "y"])])
        mock_llm.set_json_response({"field_a": None})

        result = ntb.build_table("ws1", ["field_a"])
        assert result["rows"][0]["tags"] == ["x", "y"]

    def test_node_with_no_tags_defaults_to_empty_list(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        node = _node("n1")
        node["tags"] = None
        monkeypatch.setattr(ntb, "list_nodes", lambda ws, node_type=None: [node])
        mock_llm.set_json_response({"field_a": None})

        result = ntb.build_table("ws1", ["field_a"])
        assert result["rows"][0]["tags"] == []

    def test_empty_topics_and_empty_nodes_raises_value_error(self, monkeypatch, mock_llm):
        monkeypatch.setattr(ntb, "plan", lambda *a, **k: _packet({}))
        monkeypatch.setattr(ntb, "list_nodes", lambda ws, node_type=None: [])
        with pytest.raises(ValueError):
            ntb.build_table("ws1", ["field_a"])


# ---------------------------------------------------------------------------
# 4. _extract_one_item(): parsing, fence-stripping, degrade path
# ---------------------------------------------------------------------------
class TestExtractOneItem:
    def test_parses_clean_json_response(self, mock_llm):
        mock_llm.set_json_response({"field_a": "value", "field_b": None})
        iid, fields = ntb._extract_one_item("t1", "Title", "content", "SOME_KEY", ["field_a", "field_b"])
        assert iid == "t1"
        assert fields == {"field_a": "value", "field_b": None}

    def test_strips_fenced_json_before_parsing(self, mock_llm):
        mock_llm.set_response('```json\n{"field_a": "v"}\n```')
        _, fields = ntb._extract_one_item("t1", "Title", "content", "SOME_KEY", ["field_a"])
        assert fields["field_a"] == "v"

    def test_extra_keys_in_response_are_dropped(self, mock_llm):
        mock_llm.set_json_response({"field_a": "v", "unexpected": "drop me"})
        _, fields = ntb._extract_one_item("t1", "Title", "content", "SOME_KEY", ["field_a"])
        assert set(fields.keys()) == {"field_a"}

    def test_runtime_error_degrades_to_null_fields_with_error_flag(self, monkeypatch):
        def _raise(*a, **k):
            raise RuntimeError("all providers exhausted")
        monkeypatch.setattr(ntb, "generate_text", _raise)

        _, fields = ntb._extract_one_item("t1", "Title", "content", "SOME_KEY", ["field_a", "field_b"])

        assert fields["extraction_error"] is True
        assert fields["field_a"] is None
        assert fields["field_b"] is None

    def test_unparseable_json_degrades_to_null_fields_with_error_flag(self, mock_llm):
        mock_llm.set_response("not json at all")
        _, fields = ntb._extract_one_item("t1", "Title", "content", "SOME_KEY", ["field_a"])
        assert fields["extraction_error"] is True

    def test_missing_title_defaults_to_untitled_in_prompt(self, monkeypatch):
        captured = {}

        def _fake_generate_text(system_prompt, user_content, chain, **kwargs):
            captured["user_content"] = user_content
            return json.dumps({"field_a": None})

        monkeypatch.setattr(ntb, "generate_text", _fake_generate_text)
        ntb._extract_one_item("t1", None, "content", "SOME_KEY", ["field_a"])
        assert '"Untitled"' in captured["user_content"]

    def test_content_truncated_to_4000_chars_in_prompt(self, monkeypatch):
        captured = {}

        def _fake_generate_text(system_prompt, user_content, chain, **kwargs):
            captured["user_content"] = user_content
            return json.dumps({"field_a": None})

        monkeypatch.setattr(ntb, "generate_text", _fake_generate_text)
        ntb._extract_one_item("t1", "T", "x" * 10000, "SOME_KEY", ["field_a"])
        payload = json.loads(captured["user_content"])
        assert len(payload["content"]) == 4000


# ---------------------------------------------------------------------------
# 5. _eligible_pool() / _select_workers(): fairness rotation
# ---------------------------------------------------------------------------
class TestSelectWorkers:
    def test_eligible_pool_only_returns_tagged_accounts(self, _fixed_pool, monkeypatch):
        mixed = dict(_fixed_pool)
        mixed["OTHER_KEY"] = {"provider": "groq", "natural_roles": ["verifier"]}
        monkeypatch.setattr(ntb, "AGENT_CAPABILITIES", mixed)
        assert "OTHER_KEY" not in ntb._eligible_pool()
        assert set(ntb._eligible_pool()) == set(_fixed_pool.keys())

    def test_no_eligible_accounts_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(ntb, "AGENT_CAPABILITIES", {})
        with pytest.raises(RuntimeError, match="no accounts tagged"):
            ntb._select_workers(5)

    def test_ranks_by_ascending_quota_pct(self, monkeypatch):
        snapshot = {
            "FAKE_KEY_1": {"pct": 0.9}, "FAKE_KEY_2": {"pct": 0.1},
            "FAKE_KEY_3": {"pct": 0.5}, "FAKE_KEY_4": {"pct": 0.3},
            "FAKE_KEY_5": {"pct": 0.7},
        }
        monkeypatch.setattr(ntb, "get_quota_snapshot", lambda: snapshot)
        result = ntb._select_workers(3)
        assert result == ["FAKE_KEY_2", "FAKE_KEY_4", "FAKE_KEY_3"]

    def test_missing_snapshot_entries_treated_as_zero_usage(self, monkeypatch):
        monkeypatch.setattr(ntb, "get_quota_snapshot", lambda: {"FAKE_KEY_3": {"pct": 0.5}})
        result = ntb._select_workers(5)
        assert result[-1] == "FAKE_KEY_3"


# ---------------------------------------------------------------------------
# 6. _strip_fences(): fenced vs unfenced responses
# ---------------------------------------------------------------------------
class TestStripFences:
    def test_plain_json_passes_through_unchanged(self):
        text = '{"a": 1}'
        assert ntb._strip_fences(text) == text

    def test_json_fenced_block_is_unwrapped(self):
        text = '```json\n{"a": 1}\n```'
        assert ntb._strip_fences(text) == '{"a": 1}'

    def test_plain_fenced_block_without_json_tag(self):
        text = '```\n{"a": 1}\n```'
        assert ntb._strip_fences(text) == '{"a": 1}'
