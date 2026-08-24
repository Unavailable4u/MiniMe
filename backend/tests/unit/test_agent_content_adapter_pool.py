"""
tests/unit/test_agent_content_adapter_pool.py — Patch 7f-7-4.

Covers agents/content_adapter_pool.py:
  - _strip_fences(): the pure post-processing helper
  - _write_one_variant(): platform-prompt selection, brand-voice/context
    prepend, and the RuntimeError-degrades-to-string error path
  - _derive_brief_from_task_text(): parse-or-fall-back brief synthesis
  - run(): brief sourcing (existing content_targets vs derived), fixed
    pool size (5 / 8-expanded, NOT scaled to len(platforms)), key_env
    round-robin over fewer workers than platforms, and the
    KEYS["platform_content"] write-back

generate_text is mocked via the shared `mock_llm` fixture (conftest.py's
sweep-and-patch — content_adapter_pool.py does `from utils.llm_client
import generate_text`, a bound name in its own module namespace).
eo.worker_pool._select_workers is imported here under the bound name
`_select_workers_for_role`, so it's monkeypatched directly on the module
under test rather than on eo.worker_pool, same posture
test_agent_source_planner_lean.py takes with its own bound-name imports.
conversation_memory is imported as a module reference (`from eo import
conversation_memory`), so get_full_context is monkeypatched on that
module object directly — the patch reaches content_adapter_pool.py's own
`conversation_memory.get_full_context(...)` call site without needing a
sweep, since both names point at the same module object.

Every test below either passes session_id=None (relay.emitter.emit_event
is a documented no-op with no session_id — see emitter.py's own
docstring) or accepts the no-op silently, so nothing here needs a Pusher
mock.
"""
import json

import pytest

import agents.content_adapter_pool as content_adapter_pool
from memory.bus import write, read, KEYS


# ---------------------------------------------------------------------------
# 1. _strip_fences(): pure helper, no mocking needed
# ---------------------------------------------------------------------------
class TestStripFences:
    def test_plain_text_is_unchanged(self):
        assert content_adapter_pool._strip_fences("just plain text") == "just plain text"

    def test_surrounding_whitespace_is_trimmed(self):
        assert content_adapter_pool._strip_fences("  hello  \n") == "hello"

    def test_code_fence_is_stripped(self):
        assert content_adapter_pool._strip_fences("```\nfenced content\n```") == "fenced content"

    def test_surrounding_quotes_are_stripped(self):
        assert content_adapter_pool._strip_fences('"quoted content"') == "quoted content"

    def test_single_char_is_not_treated_as_a_quote_pair(self):
        # len(text) > 1 guard: a lone quote character should survive as-is
        assert content_adapter_pool._strip_fences('"') == '"'

    def test_fence_then_quotes_both_stripped(self):
        result = content_adapter_pool._strip_fences('```\n"double wrapped"\n```')
        assert result == "double wrapped"

    def test_empty_string_stays_empty(self):
        assert content_adapter_pool._strip_fences("") == ""


# ---------------------------------------------------------------------------
# 2. _write_one_variant(): per-platform generation
# ---------------------------------------------------------------------------
class TestWriteOneVariant:
    def test_known_platform_uses_its_specific_prompt(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("adapted tweet")
        content_adapter_pool._write_one_variant("twitter", "core msg", "GROQ_API_KEY", 1)
        system_prompt = mock_llm.mock.call_args.args[0]
        assert "X/Twitter" in system_prompt
        assert "280 characters" in system_prompt

    def test_unknown_platform_falls_back_to_default_prompt(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("adapted content")
        content_adapter_pool._write_one_variant("reddit_post", "core msg", "GROQ_API_KEY", 1)
        system_prompt = mock_llm.mock.call_args.args[0]
        assert "Platform: reddit_post" in system_prompt
        assert "No specific format rules are on file" in system_prompt

    def test_returns_platform_and_stripped_content(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("```\nclean content\n```")
        platform, content = content_adapter_pool._write_one_variant(
            "linkedin", "core msg", "GROQ_API_KEY", 1
        )
        assert platform == "linkedin"
        assert content == "clean content"

    def test_core_message_always_in_user_content(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("x")
        content_adapter_pool._write_one_variant("facebook", "launch our new widget",
                                                  "GROQ_API_KEY", 1)
        user_content = mock_llm.mock.call_args.args[1]
        assert "CORE MESSAGE:\nlaunch our new widget" in user_content

    def test_brand_voice_context_prepended_when_present(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "we are playful and bold")
        mock_llm.set_response("x")
        content_adapter_pool._write_one_variant("facebook", "core msg", "GROQ_API_KEY", 1,
                                                  session_id="sess1")
        user_content = mock_llm.mock.call_args.args[1]
        assert user_content.index("we are playful and bold") < user_content.index("CORE MESSAGE:")

    def test_no_brand_voice_block_when_context_empty(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("x")
        content_adapter_pool._write_one_variant("facebook", "core msg", "GROQ_API_KEY", 1)
        user_content = mock_llm.mock.call_args.args[1]
        assert "Brand voice" not in user_content

    def test_empty_model_response_becomes_failure_marker(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("   ")
        platform, content = content_adapter_pool._write_one_variant(
            "twitter", "core msg", "GROQ_API_KEY", 1
        )
        assert "CONTENT ADAPTER FAILED" in content
        assert "twitter" in content

    def test_runtime_error_from_generate_text_degrades_to_string(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.raise_on_call(RuntimeError("every provider in the chain failed"))
        platform, content = content_adapter_pool._write_one_variant(
            "twitter", "core msg", "GROQ_API_KEY", 1
        )
        assert platform == "twitter"
        assert "CONTENT ADAPTER FAILED" in content
        assert "every provider in the chain failed" in content

    def test_single_step_chain_uses_the_given_key_env(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        mock_llm.set_response("x")
        content_adapter_pool._write_one_variant("twitter", "core msg", "OPENROUTER_API_KEY_3", 1)
        chain = mock_llm.mock.call_args.args[2]
        assert chain == [{"provider": "openrouter", "model": "openrouter/free",
                           "key_env": "OPENROUTER_API_KEY_3"}]


# ---------------------------------------------------------------------------
# 3. _derive_brief_from_task_text(): parse-or-fall-back brief synthesis
# ---------------------------------------------------------------------------
class TestDeriveBriefFromTaskText:
    def test_valid_brief_is_parsed_and_written(self, mock_llm):
        mock_llm.set_json_response(
            {"core_message": "we launched v2", "platforms": ["twitter", "linkedin"]}
        )
        brief = content_adapter_pool._derive_brief_from_task_text("announce v2 launch")
        assert brief == {"core_message": "we launched v2", "platforms": ["twitter", "linkedin"]}
        assert read(KEYS["content_targets"]) == brief

    def test_fenced_json_response_is_parsed(self, mock_llm):
        mock_llm.set_response(
            '```json\n{"core_message": "m", "platforms": ["facebook"]}\n```'
        )
        brief = content_adapter_pool._derive_brief_from_task_text("task")
        assert brief == {"core_message": "m", "platforms": ["facebook"]}

    def test_empty_platforms_list_triggers_fallback(self, mock_llm):
        mock_llm.set_json_response({"core_message": "m", "platforms": []})
        brief = content_adapter_pool._derive_brief_from_task_text("announce something")
        assert brief == {"core_message": "announce something", "platforms": ["twitter"]}

    def test_malformed_json_triggers_fallback(self, mock_llm):
        mock_llm.set_response("not valid json")
        brief = content_adapter_pool._derive_brief_from_task_text("announce something")
        assert brief == {"core_message": "announce something", "platforms": ["twitter"]}

    def test_fallback_with_no_task_text_uses_default_message(self, mock_llm):
        mock_llm.set_response("not valid json")
        brief = content_adapter_pool._derive_brief_from_task_text(None)
        assert brief["core_message"] == "Announce the update."
        assert brief["platforms"] == ["twitter"]

    def test_generate_text_exception_triggers_fallback(self, mock_llm):
        mock_llm.raise_on_call(RuntimeError("all providers down"))
        brief = content_adapter_pool._derive_brief_from_task_text("announce something")
        assert brief == {"core_message": "announce something", "platforms": ["twitter"]}


# ---------------------------------------------------------------------------
# 4. run(): brief sourcing, fixed pool size, round-robin, write-back
# ---------------------------------------------------------------------------
class TestRun:
    def test_existing_content_targets_are_used_without_deriving(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: ["GROQ_API_KEY"])
        write(KEYS["content_targets"], {"core_message": "existing brief", "platforms": ["twitter"]})
        mock_llm.set_response("adapted")
        results = content_adapter_pool.run()
        assert results == {"twitter": "adapted"}
        user_content = mock_llm.mock.call_args.args[1]
        assert "existing brief" in user_content

    def test_missing_content_targets_derives_from_task_text(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: ["GROQ_API_KEY"])
        # First call (brief derivation) returns a brief; subsequent calls
        # (per-platform variant writes) return plain content.
        mock_llm.set_sequence([
            json.dumps({"core_message": "derived msg", "platforms": ["twitter"]}),
            "adapted tweet",
        ])
        results = content_adapter_pool.run(task_text="announce our launch")
        assert results == {"twitter": "adapted tweet"}

    def test_default_worker_count_is_5(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        captured = {}

        def fake_select(role_tag, count, key_override, **kw):
            captured["role_tag"] = role_tag
            captured["count"] = count
            return ["GROQ_API_KEY"]

        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role", fake_select)
        write(KEYS["content_targets"], {"core_message": "m", "platforms": ["twitter"]})
        mock_llm.set_response("x")
        content_adapter_pool.run()
        assert captured["count"] == 5
        assert captured["role_tag"] == "content_writer"

    def test_expanded_worker_count_is_8(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        captured = {}
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: (
                                 captured.__setitem__("count", count), ["GROQ_API_KEY"])[1])
        write(KEYS["content_targets"], {"core_message": "m", "platforms": ["twitter"]})
        mock_llm.set_response("x")
        content_adapter_pool.run(expanded=True)
        assert captured["count"] == 8

    def test_worker_count_not_scaled_to_platform_count(self, mock_llm, monkeypatch):
        # Fixed pool size regardless of len(platforms) -- NOT scaled up
        # for a brief naming more platforms than there are workers.
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        captured = {}
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: (
                                 captured.__setitem__("count", count), ["KEY_A", "KEY_B"])[1])
        write(KEYS["content_targets"], {
            "core_message": "m",
            "platforms": ["twitter", "linkedin", "facebook", "instagram_caption",
                          "press_release", "blog_intro"],
        })
        mock_llm.set_response("x")
        results = content_adapter_pool.run()
        assert captured["count"] == 5
        assert set(results.keys()) == {
            "twitter", "linkedin", "facebook", "instagram_caption",
            "press_release", "blog_intro",
        }

    def test_key_override_forwarded_to_select_workers(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        captured = {}
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: (
                                 captured.__setitem__("key_override", key_override), ["KEY_A"])[1])
        write(KEYS["content_targets"], {"core_message": "m", "platforms": ["twitter"]})
        mock_llm.set_response("x")
        content_adapter_pool.run(key_override="PINNED_KEY")
        assert captured["key_override"] == "PINNED_KEY"

    def test_results_written_to_platform_content_key(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: ["KEY_A"])
        write(KEYS["content_targets"], {"core_message": "m", "platforms": ["twitter", "linkedin"]})
        mock_llm.set_response("same content")
        results = content_adapter_pool.run()
        assert read(KEYS["platform_content"]) == results
        assert results == {"twitter": "same content", "linkedin": "same content"}

    def test_session_id_and_domain_forwarded_to_variant_calls(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: ["KEY_A"])
        write(KEYS["content_targets"], {"core_message": "m", "platforms": ["twitter"]})
        mock_llm.set_response("x")
        content_adapter_pool.run(session_id="sess9", domain="marketing")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess9"
        assert kwargs["domain"] == "marketing"

    def test_core_message_falls_back_to_task_text_when_brief_omits_it(self, mock_llm, monkeypatch):
        monkeypatch.setattr(content_adapter_pool.conversation_memory, "get_full_context",
                             lambda session_id, owner_id=None: "")
        monkeypatch.setattr(content_adapter_pool, "_select_workers_for_role",
                             lambda role_tag, count, key_override, **kw: ["KEY_A"])
        write(KEYS["content_targets"], {"platforms": ["twitter"]})
        mock_llm.set_response("x")
        content_adapter_pool.run(task_text="fallback core message")
        user_content = mock_llm.mock.call_args.args[1]
        assert "fallback core message" in user_content
