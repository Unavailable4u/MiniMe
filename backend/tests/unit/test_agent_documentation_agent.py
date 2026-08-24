"""
tests/unit/test_agent_documentation_agent.py — Patch 7f-7-1.

Covers agents/documentation_agent.py's run(): a single-step
generate_text() call (Mistral-first CHAIN, no separate call_with_retry
wrapper -- see the module's own perf-audit comment) that turns a
batched read_many() of {idea, feature_status, this_cycle_summary,
file_map} into README markdown, writes KEYS["doc_output"], optionally
writes README.md to disk under apps/<slug>/ when that directory
already exists, and writes a knowledge-graph node for it -- both of
those last two steps gated on get_current_app_slug() returning a slug
at all, and the on-disk write additionally gated on the app directory
already existing.

generate_text/read_many/write are patched via mock_llm / direct
monkeypatch of the module's own bound names (conftest.py's usual
pattern). get_current_app_slug and write_node are both function-local,
deferred imports inside run() itself (`from memory.bus import
get_current_app_slug`, `from eo.knowledge_graph import write_node`) --
patched at their SOURCE module (memory.bus / eo.knowledge_graph)
rather than on agents.documentation_agent, since no bound copy of
either name exists on this module until run() actually executes.
"""
import json
from unittest.mock import MagicMock

import pytest

import memory.bus as bus_module
from agents import documentation_agent
from eo import knowledge_graph


def _doc_response(readme_markdown="# My App\n\nDoes things."):
    return json.dumps({"readme_markdown": readme_markdown})


@pytest.fixture(autouse=True)
def _fake_read_many(monkeypatch):
    """Defaults to a full, non-empty read; individual tests override via
    monkeypatch.setattr(documentation_agent, "read_many", ...) directly
    when they need different content."""
    def _read_many(keys, default=None):
        return {
            documentation_agent.KEYS["original_idea"]: "an app that does things",
            documentation_agent.KEYS["feature_status"]: {"login": "done"},
            documentation_agent.KEYS["latest_report"]: {"summary": "added login"},
            documentation_agent.KEYS["file_map"]: {"app.py": "main entry"},
        }
    monkeypatch.setattr(documentation_agent, "read_many", _read_many)


@pytest.fixture(autouse=True)
def _fake_write(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(documentation_agent, "write", mock)
    return mock


@pytest.fixture(autouse=True)
def _fake_app_slug(monkeypatch):
    """Defaults to no slug (the safest default -- no disk write, no
    knowledge-graph write). Tests that want the slug-gated paths
    override this directly."""
    monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: None)


@pytest.fixture(autouse=True)
def _fake_write_node(monkeypatch):
    mock = MagicMock(return_value="node1")
    monkeypatch.setattr(knowledge_graph, "write_node", mock)
    return mock


# ---------------------------------------------------------------------------
# 1. _strip_fences(): fenced vs unfenced JSON responses
# ---------------------------------------------------------------------------
class TestStripFences:
    def test_plain_json_passes_through_unchanged(self):
        text = '{"readme_markdown": "hi"}'
        assert documentation_agent._strip_fences(text) == text

    def test_json_fenced_block_is_unwrapped(self):
        text = '```json\n{"readme_markdown": "hi"}\n```'
        result = documentation_agent._strip_fences(text)
        assert result == '{"readme_markdown": "hi"}'

    def test_plain_fenced_block_without_json_language_tag(self):
        text = '```\n{"readme_markdown": "hi"}\n```'
        result = documentation_agent._strip_fences(text)
        assert result == '{"readme_markdown": "hi"}'

    def test_surrounding_whitespace_is_stripped(self):
        text = '  \n{"readme_markdown": "hi"}\n  '
        assert documentation_agent._strip_fences(text) == '{"readme_markdown": "hi"}'


# ---------------------------------------------------------------------------
# 2. run(): batched read_many() usage and generate_text() call shape
# ---------------------------------------------------------------------------
class TestGenerateTextCall:
    def test_generate_text_called_with_system_prompt_and_chain(self, monkeypatch):
        captured = {}

        def _fake_generate_text(system_prompt, user_prompt, chain, **kwargs):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            captured["chain"] = chain
            captured["kwargs"] = kwargs
            return _doc_response()

        monkeypatch.setattr(documentation_agent, "generate_text", _fake_generate_text)

        documentation_agent.run(session_id="s1", tier=3, domain="coding")

        assert captured["system_prompt"] == documentation_agent.SYSTEM_PROMPT
        assert captured["chain"] == documentation_agent.CHAIN
        assert captured["kwargs"]["agent_name"] == "Documentation Agent"
        assert captured["kwargs"]["session_id"] == "s1"
        assert captured["kwargs"]["tier"] == 3
        assert captured["kwargs"]["domain"] == "coding"

    def test_user_prompt_includes_idea_feature_status_summary_and_file_map(self, monkeypatch):
        captured = {}

        def _fake_generate_text(system_prompt, user_prompt, chain, **kwargs):
            captured["user_prompt"] = user_prompt
            return _doc_response()

        monkeypatch.setattr(documentation_agent, "generate_text", _fake_generate_text)
        documentation_agent.run()

        payload = json.loads(captured["user_prompt"])
        assert payload["idea"] == "an app that does things"
        assert payload["feature_status"] == {"login": "done"}
        assert payload["this_cycle_summary"] == "added login"
        assert payload["file_map"] == {"app.py": "main entry"}

    def test_missing_report_summary_defaults_to_empty_string(self, monkeypatch):
        monkeypatch.setattr(documentation_agent, "read_many", lambda keys, default=None: {
            documentation_agent.KEYS["original_idea"]: "idea",
            documentation_agent.KEYS["feature_status"]: {},
            documentation_agent.KEYS["latest_report"]: {},  # no "summary" key
            documentation_agent.KEYS["file_map"]: {},
        })
        captured = {}

        def _fake_generate_text(system_prompt, user_prompt, chain, **kwargs):
            captured["user_prompt"] = user_prompt
            return _doc_response()

        monkeypatch.setattr(documentation_agent, "generate_text", _fake_generate_text)
        documentation_agent.run()

        payload = json.loads(captured["user_prompt"])
        assert payload["this_cycle_summary"] == ""

    def test_none_values_from_read_many_default_to_empty(self, monkeypatch):
        # read_many's own `default` param can legitimately hand back None
        # for any/all of these keys -- run() must coalesce each to its
        # own empty default ("" / {}), not propagate None into the prompt.
        monkeypatch.setattr(documentation_agent, "read_many", lambda keys, default=None: {
            documentation_agent.KEYS["original_idea"]: None,
            documentation_agent.KEYS["feature_status"]: None,
            documentation_agent.KEYS["latest_report"]: None,
            documentation_agent.KEYS["file_map"]: None,
        })
        captured = {}

        def _fake_generate_text(system_prompt, user_prompt, chain, **kwargs):
            captured["user_prompt"] = user_prompt
            return _doc_response()

        monkeypatch.setattr(documentation_agent, "generate_text", _fake_generate_text)
        documentation_agent.run()

        payload = json.loads(captured["user_prompt"])
        assert payload["idea"] == ""
        assert payload["feature_status"] == {}
        assert payload["this_cycle_summary"] == ""
        assert payload["file_map"] == {}


# ---------------------------------------------------------------------------
# 3. run(): doc_output write and return value
# ---------------------------------------------------------------------------
class TestDocOutput:
    def test_writes_parsed_doc_to_doc_output_key(self, monkeypatch, _fake_write):
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: _doc_response("# Hello"))
        documentation_agent.run()
        _fake_write.assert_called_once_with(
            documentation_agent.KEYS["doc_output"], {"readme_markdown": "# Hello"},
        )

    def test_returns_the_parsed_doc(self, monkeypatch):
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: _doc_response("# Returned"))
        result = documentation_agent.run()
        assert result == {"readme_markdown": "# Returned"}

    def test_fenced_response_is_unwrapped_before_parsing(self, monkeypatch):
        monkeypatch.setattr(
            documentation_agent, "generate_text",
            lambda *a, **k: '```json\n{"readme_markdown": "# Fenced"}\n```',
        )
        result = documentation_agent.run()
        assert result == {"readme_markdown": "# Fenced"}

    def test_malformed_json_response_raises(self, monkeypatch):
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: "not valid json at all")
        with pytest.raises(json.JSONDecodeError):
            documentation_agent.run()


# ---------------------------------------------------------------------------
# 4. run(): no app slug -> neither disk write nor knowledge-graph write
# ---------------------------------------------------------------------------
class TestNoAppSlug:
    def test_no_slug_skips_knowledge_graph_write(self, monkeypatch, _fake_write_node):
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: None)
        monkeypatch.setattr(documentation_agent, "generate_text", lambda *a, **k: _doc_response())
        documentation_agent.run()
        _fake_write_node.assert_not_called()

    def test_no_slug_does_not_touch_filesystem(self, monkeypatch, tmp_path):
        monkeypatch.setattr(documentation_agent, "APPS_ROOT", str(tmp_path))
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: None)
        monkeypatch.setattr(documentation_agent, "generate_text", lambda *a, **k: _doc_response())
        documentation_agent.run()
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 5. run(): app slug present -> knowledge-graph write always happens
# ---------------------------------------------------------------------------
class TestKnowledgeGraphWrite:
    def test_slug_present_writes_knowledge_graph_node(self, monkeypatch, _fake_write_node):
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "my-app")
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: _doc_response("# My App"))
        documentation_agent.run(session_id="s1", tier=2)

        _fake_write_node.assert_called_once()
        call_kwargs = _fake_write_node.call_args.kwargs
        assert call_kwargs["workspace_id"] == "my-app"
        assert call_kwargs["section"] == "coding"
        assert call_kwargs["node_type"] == "note"
        assert call_kwargs["title"] == "README"
        assert call_kwargs["content"] == "# My App"
        assert call_kwargs["created_by"] == "documentation_agent"
        assert call_kwargs["session_id"] == "s1"
        assert call_kwargs["tier"] == 2

    def test_knowledge_graph_write_happens_even_when_app_dir_missing(self, monkeypatch, tmp_path, _fake_write_node):
        # Filesystem write is gated on the app dir existing; the
        # knowledge-graph write is gated only on a slug existing at all
        # -- these are two independently-checked conditions.
        monkeypatch.setattr(documentation_agent, "APPS_ROOT", str(tmp_path))
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "no-such-app")
        monkeypatch.setattr(documentation_agent, "generate_text", lambda *a, **k: _doc_response())
        documentation_agent.run()
        _fake_write_node.assert_called_once()

    def test_content_defaults_to_empty_string_when_readme_markdown_absent(self, monkeypatch, _fake_write_node):
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "my-app")
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: json.dumps({}))
        documentation_agent.run()
        assert _fake_write_node.call_args.kwargs["content"] == ""


# ---------------------------------------------------------------------------
# 6. run(): app slug present AND app dir exists -> README.md written to disk
# ---------------------------------------------------------------------------
class TestFilesystemWrite:
    def test_writes_readme_when_app_directory_exists(self, monkeypatch, tmp_path):
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        monkeypatch.setattr(documentation_agent, "APPS_ROOT", str(tmp_path))
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "my-app")
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: _doc_response("# On Disk"))

        documentation_agent.run()

        readme_path = app_dir / "README.md"
        assert readme_path.exists()
        assert readme_path.read_text(encoding="utf-8") == "# On Disk"

    def test_skips_disk_write_when_app_directory_does_not_exist(self, monkeypatch, tmp_path):
        # tmp_path itself exists but tmp_path/no-such-app does not.
        monkeypatch.setattr(documentation_agent, "APPS_ROOT", str(tmp_path))
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "no-such-app")
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: _doc_response())

        documentation_agent.run()

        assert not (tmp_path / "no-such-app" / "README.md").exists()

    def test_overwrites_existing_readme_content(self, monkeypatch, tmp_path):
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        readme_path = app_dir / "README.md"
        readme_path.write_text("old content", encoding="utf-8")

        monkeypatch.setattr(documentation_agent, "APPS_ROOT", str(tmp_path))
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "my-app")
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: _doc_response("# Fresh content"))

        documentation_agent.run()

        assert readme_path.read_text(encoding="utf-8") == "# Fresh content"

    def test_missing_readme_markdown_writes_empty_file(self, monkeypatch, tmp_path):
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        monkeypatch.setattr(documentation_agent, "APPS_ROOT", str(tmp_path))
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "my-app")
        monkeypatch.setattr(documentation_agent, "generate_text",
                             lambda *a, **k: json.dumps({}))

        documentation_agent.run()

        readme_path = app_dir / "README.md"
        assert readme_path.exists()
        assert readme_path.read_text(encoding="utf-8") == ""
