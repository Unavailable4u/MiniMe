"""
tests/unit/test_agent_deploy_config_writer.py — Patch 7f-5.

Covers agents/deploy_config_writer.py: _get_project_tree() (missing app
dir -> empty list, real files -> sorted relative paths), _strip_fences()
(same shape as every other agent's fence-stripper in this codebase), and
run_deploy_config_writer()'s happy path (project tree + module_specs +
task_text folded into the user prompt, JSON plan parsed and written to
the bus, emit_event payload shape), the dynamic-chain-empty ->
FALLBACK_CHAIN fallback (same pattern architecture_diagrammer.py /
hardware_speccer.py use), and the unparseable-JSON -> safe render.yaml
fallback plan.

generate_text is faked via the shared `mock_llm` fixture (bound-name
import). The deferred `from eo.dynamic_chain import build_fallback_chain`
import is faked via a sys.modules substitute, same approach
test_agent_dataset_analyst.py takes for the identical deferred-import
shape in agents/dataset_analyst.py.
"""
import json
import sys

import pytest

from agents import deploy_config_writer
from memory.bus import KEYS, read, set_app_slug, write


@pytest.fixture(autouse=True)
def _fake_emit_event(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy_config_writer, "emit_event",
        lambda event, session_id, agent=None, payload=None: calls.append(
            (event, session_id, agent, payload)
        ),
    )
    return calls


@pytest.fixture
def fake_dynamic_chain(monkeypatch):
    fake = type("M", (), {"build_fallback_chain": staticmethod(lambda role: [])})()
    monkeypatch.setitem(sys.modules, "eo.dynamic_chain", fake)
    return fake


VALID_PLAN = {
    "platform": "render", "config_filename": "render.yaml",
    "config_content": "services:\n  - type: web\n", "reason": "backend service detected",
}


# ---------------------------------------------------------------------------
# 1. _get_project_tree(): missing dir, real files, sorted output
# ---------------------------------------------------------------------------
class TestGetProjectTree:
    def test_missing_directory_returns_empty_list(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        assert deploy_config_writer._get_project_tree(str(missing)) == []

    def test_returns_sorted_relative_paths(self, tmp_path):
        import os
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x")
        (tmp_path / "README.md").write_text("x")

        tree = deploy_config_writer._get_project_tree(str(tmp_path))

        assert tree == sorted(tree)
        assert "README.md" in tree
        assert os.path.join("src", "main.py") in tree


# ---------------------------------------------------------------------------
# 2. _strip_fences(): output-cleanup, same shape as every other agent's
# ---------------------------------------------------------------------------
class TestStripFences:
    def test_plain_text_unchanged(self):
        assert deploy_config_writer._strip_fences('{"a": 1}') == '{"a": 1}'

    def test_strips_bare_fence(self):
        assert deploy_config_writer._strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_strips_json_tagged_fence(self):
        assert deploy_config_writer._strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_strips_surrounding_whitespace(self):
        assert deploy_config_writer._strip_fences('   {"a": 1}   ') == '{"a": 1}'


# ---------------------------------------------------------------------------
# 3. run_deploy_config_writer(): happy path
# ---------------------------------------------------------------------------
class TestRunHappyPath:
    def test_parses_json_plan_and_writes_to_bus(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_json_response(VALID_PLAN)

        plan = deploy_config_writer.run_deploy_config_writer()

        assert plan == VALID_PLAN
        assert read(deploy_config_writer.DEPLOY_CONFIG_PLAN_KEY) == VALID_PLAN

    def test_strips_fences_before_parsing(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_response("```json\n" + json.dumps(VALID_PLAN) + "\n```")

        plan = deploy_config_writer.run_deploy_config_writer()

        assert plan == VALID_PLAN

    def test_project_tree_included_when_app_slug_set(self, fake_bus, mock_llm, fake_dynamic_chain,
                                                       tmp_path, monkeypatch):
        monkeypatch.setattr(deploy_config_writer, "APPS_ROOT", str(tmp_path))
        set_app_slug("myapp")
        app_dir = tmp_path / "myapp"
        app_dir.mkdir()
        (app_dir / "main.py").write_text("print('hi')")
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer()

        user_prompt = mock_llm.mock.call_args.args[1]
        assert "main.py" in user_prompt

    def test_no_app_slug_produces_empty_tree_without_crashing(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_json_response(VALID_PLAN)
        plan = deploy_config_writer.run_deploy_config_writer()
        assert plan == VALID_PLAN

    def test_module_specs_included_in_prompt(self, fake_bus, mock_llm, fake_dynamic_chain):
        write(KEYS["module_specs"], {"modules": [{"name": "api", "language": "python"}]})
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer()

        user_prompt = mock_llm.mock.call_args.args[1]
        assert '"api"' in user_prompt

    def test_task_text_appended_when_given(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer(task_text="deploy this to render")

        user_prompt = mock_llm.mock.call_args.args[1]
        assert "deploy this to render" in user_prompt

    def test_task_text_omitted_when_not_given(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer()

        user_prompt = mock_llm.mock.call_args.args[1]
        assert "Original task" not in user_prompt

    def test_session_id_and_tier_and_domain_forwarded(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer(session_id="sess-1", tier=2, domain="deploy")

        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["tier"] == 2
        assert kwargs["domain"] == "deploy"
        assert kwargs["agent_name"] == "Deploy Config Writer"

    def test_emits_deploy_config_proposed_with_platform_and_filename(
        self, fake_bus, mock_llm, fake_dynamic_chain, _fake_emit_event
    ):
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer(session_id="sess-1")

        event, session_id, agent, payload = _fake_emit_event[0]
        assert event == deploy_config_writer.EventType.DEPLOY_CONFIG_PROPOSED
        assert session_id == "sess-1"
        assert agent == "deploy_config_writer"
        assert payload == {"platform": "render", "config_filename": "render.yaml"}


# ---------------------------------------------------------------------------
# 4. run_deploy_config_writer(): dynamic vs static fallback chain
# ---------------------------------------------------------------------------
class TestChainSelection:
    def test_static_fallback_chain_used_when_dynamic_chain_empty(
        self, fake_bus, mock_llm, fake_dynamic_chain
    ):
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer()

        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == deploy_config_writer.FALLBACK_CHAIN

    def test_dynamic_chain_used_when_non_empty(self, fake_bus, mock_llm, monkeypatch):
        custom_chain = [{"provider": "groq", "model": "custom", "key_env": "K"}]
        fake_mod = type("M", (), {"build_fallback_chain": staticmethod(lambda role: custom_chain)})()
        monkeypatch.setitem(sys.modules, "eo.dynamic_chain", fake_mod)
        mock_llm.set_json_response(VALID_PLAN)

        deploy_config_writer.run_deploy_config_writer()

        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == custom_chain


# ---------------------------------------------------------------------------
# 5. run_deploy_config_writer(): unparseable JSON -> safe fallback plan
# ---------------------------------------------------------------------------
class TestUnparseableFallback:
    def test_unparseable_json_produces_render_yaml_fallback(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_response("this is not json")

        plan = deploy_config_writer.run_deploy_config_writer()

        assert plan["platform"] == "render"
        assert plan["config_filename"] == "render.yaml"
        assert "fallback" in plan["reason"]

    def test_fallback_plan_is_still_written_to_bus(self, fake_bus, mock_llm, fake_dynamic_chain):
        mock_llm.set_response("not json")

        plan = deploy_config_writer.run_deploy_config_writer()

        assert read(deploy_config_writer.DEPLOY_CONFIG_PLAN_KEY) == plan
