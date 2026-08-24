"""
tests/unit/test_agent_idea_planner.py — Patch 7f-7-4.

Covers agents/idea_planner.py's single entry point, run(): the
original_idea/latest_report/feature_status batched read, the
cycle-1-vs-prior-report user_content branch (including the
DROPPABLE_CONTEXT_MARKER placement for a real prior report), the
build_fallback_chain()-or-FALLBACK_CHAIN selection, markdown-fence
stripping on the raw model response, and the KEYS["current_plan"]
write-back.

generate_text is mocked via the shared `mock_llm` fixture (conftest.py's
sweep-and-patch, since idea_planner.py does `from utils.llm_client import
generate_text` — a bound name in its own module namespace). eo.dynamic_chain
is a lazily-imported, deferred module (see idea_planner.py's own comment on
why it can't be a top-level import), so it's faked via a sys.modules
substitute the same way test_agent_source_planner_lean.py fakes
agents.generic_worker.
"""
import json
import sys
from unittest.mock import MagicMock

import pytest

from agents import idea_planner
from memory.bus import KEYS, read, write


def _fake_dynamic_chain(chain):
    """Injects a fake eo.dynamic_chain module with a build_fallback_chain
    that returns `chain` (or None), and returns the MagicMock for call
    inspection. idea_planner.run() does `from eo.dynamic_chain import
    build_fallback_chain` INSIDE run() (a deferred import — see its own
    module comment), so this must land in sys.modules before run() is
    called, same timing concern test_agent_source_planner_lean.py notes
    for agents.generic_worker.

    Restored (not popped) by the autouse fixture below: popping this key
    would force a fresh disk re-import of eo.dynamic_chain the next time
    anything does `from eo.dynamic_chain import ...` mid-session, which
    walks straight into the exact circular-import landmine
    tests/conftest.py's own module docstring warns about (eo.dynamic_chain
    <- eo.registry <- agents, several of which import back from eo at
    module scope) — outside the one safe import order conftest.py
    establishes once via `import eo.registry` at collection time. Hitting
    that landmine mid-session leaves agents.generic_worker permanently
    partially-initialized (missing PROVIDER_DEFAULT_MODEL) for the rest
    of the pytest process, breaking unrelated later tests that reach
    eo/quota_sentinel.py's real _model_for(). Saving and restoring the
    real module object avoids ever triggering that re-import."""
    mock = MagicMock(return_value=chain)
    module = type("M", (), {"build_fallback_chain": mock})()
    sys.modules["eo.dynamic_chain"] = module
    return mock


@pytest.fixture(autouse=True)
def _clear_fake_dynamic_chain():
    real_module = sys.modules.get("eo.dynamic_chain")
    yield
    if real_module is not None:
        sys.modules["eo.dynamic_chain"] = real_module
    else:
        sys.modules.pop("eo.dynamic_chain", None)


# ---------------------------------------------------------------------------
# 1. user_content construction
# ---------------------------------------------------------------------------
class TestUserContent:
    def test_cycle_1_message_when_no_prior_report(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "a todo app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
             "cycle_goal": "build f1"}
        )
        idea_planner.run()
        _, kwargs = mock_llm.mock.call_args
        args = mock_llm.mock.call_args.args
        user_content = args[1]
        assert "This is cycle 1. No prior report exists yet." in user_content
        assert "Prior cycle report" not in user_content

    def test_prior_report_appended_after_droppable_marker(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "a todo app")
        write(KEYS["latest_report"], {"summary": "cycle 1 done"})
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
             "cycle_goal": "build f1"}
        )
        idea_planner.run()
        from utils.llm_client import DROPPABLE_CONTEXT_MARKER
        user_content = mock_llm.mock.call_args.args[1]
        assert DROPPABLE_CONTEXT_MARKER in user_content
        assert "cycle 1 done" in user_content
        # The droppable block is the LAST thing appended, per the
        # module's own comment on _shrink_prompt_for_retry() dropping
        # exactly this trailing block first.
        assert user_content.rsplit(DROPPABLE_CONTEXT_MARKER, 1)[1].strip().startswith(
            "Prior cycle report:"
        )

    def test_original_idea_always_included(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "a recipe manager")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
             "cycle_goal": "g"}
        )
        idea_planner.run()
        user_content = mock_llm.mock.call_args.args[1]
        assert "Original idea: a recipe manager" in user_content

    def test_missing_feature_status_defaults_to_empty_dict(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
             "cycle_goal": "g"}
        )
        idea_planner.run()
        user_content = mock_llm.mock.call_args.args[1]
        assert "Current feature_status: {}" in user_content

    def test_feature_status_serialized_when_present(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        write(KEYS["feature_status"], {"login": "done", "search": "in_progress"})
        mock_llm.set_json_response(
            {"features": ["login", "search"], "priorities": ["search", "login"],
             "target_feature": "search", "cycle_goal": "g"}
        )
        idea_planner.run()
        user_content = mock_llm.mock.call_args.args[1]
        assert json.dumps({"login": "done", "search": "in_progress"}) in user_content


# ---------------------------------------------------------------------------
# 2. chain selection: build_fallback_chain() result vs FALLBACK_CHAIN
# ---------------------------------------------------------------------------
class TestChainSelection:
    def test_uses_dynamic_chain_when_it_returns_something(self, mock_llm):
        dynamic_chain = [{"provider": "groq", "model": "custom", "key_env": "GROQ_API_KEY_9"}]
        build_mock = _fake_dynamic_chain(dynamic_chain)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1", "cycle_goal": "g"}
        )
        idea_planner.run()
        build_mock.assert_called_once_with("idea_planner")
        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == dynamic_chain

    def test_falls_back_to_static_chain_when_dynamic_chain_is_empty(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1", "cycle_goal": "g"}
        )
        idea_planner.run()
        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == idea_planner.FALLBACK_CHAIN

    def test_falls_back_to_static_chain_when_dynamic_chain_is_empty_list(self, mock_llm):
        _fake_dynamic_chain([])
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1", "cycle_goal": "g"}
        )
        idea_planner.run()
        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == idea_planner.FALLBACK_CHAIN


# ---------------------------------------------------------------------------
# 3. session_id / domain / agent_name forwarding
# ---------------------------------------------------------------------------
class TestForwarding:
    def test_agent_name_is_idea_planner_label(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1", "cycle_goal": "g"}
        )
        idea_planner.run()
        assert mock_llm.mock.call_args.kwargs["agent_name"] == "Idea Planner"

    def test_session_id_and_domain_forwarded(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1", "cycle_goal": "g"}
        )
        idea_planner.run(session_id="sess1", domain="research")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess1"
        assert kwargs["domain"] == "research"

    def test_defaults_are_none_when_not_provided(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_json_response(
            {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1", "cycle_goal": "g"}
        )
        idea_planner.run()
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] is None
        assert kwargs["domain"] is None


# ---------------------------------------------------------------------------
# 4. response parsing (markdown fence stripping) + write-back
# ---------------------------------------------------------------------------
class TestResponseParsing:
    def test_plain_json_parses_and_returns(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        plan = {"features": ["f1", "f2"], "priorities": ["f2", "f1"],
                 "target_feature": "f2", "cycle_goal": "build f2"}
        mock_llm.set_json_response(plan)
        result = idea_planner.run()
        assert result == plan

    def test_fenced_json_with_json_tag_is_stripped(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        plan = {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
                 "cycle_goal": "g"}
        mock_llm.set_response("```json\n" + json.dumps(plan) + "\n```")
        result = idea_planner.run()
        assert result == plan

    def test_fenced_json_without_json_tag_is_stripped(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        plan = {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
                 "cycle_goal": "g"}
        mock_llm.set_response("```\n" + json.dumps(plan) + "\n```")
        result = idea_planner.run()
        assert result == plan

    def test_result_written_to_current_plan_key(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        plan = {"features": ["f1"], "priorities": ["f1"], "target_feature": "f1",
                 "cycle_goal": "g"}
        mock_llm.set_json_response(plan)
        idea_planner.run()
        assert read(KEYS["current_plan"]) == plan

    def test_malformed_json_raises(self, mock_llm):
        _fake_dynamic_chain(None)
        write(KEYS["original_idea"], "an app")
        mock_llm.set_response("not json at all")
        with pytest.raises(json.JSONDecodeError):
            idea_planner.run()
