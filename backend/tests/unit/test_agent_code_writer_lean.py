"""
tests/unit/test_agent_code_writer_lean.py — Patch 7f-1.

Covers agents/code_writer_lean.py: tier-1 pipeline, second step (1-worker
Code Writer). Things worth locking down per its own docstring/history:

  1. module_spec sourcing — explicit arg is written to tier1_module_spec
     and used; omitted, falls back to reading it off the bus, raising
     MissingDependencyError (a structured error, NOT a bare ValueError —
     see eo/errors.py) when neither exists.
  2. CHAIN (OR-3d): three independent OpenRouter accounts, then a Gemini
     fallback — same "genuinely independent steps" shape
     prompt_writer_lean.py's CHAIN has, not the old fake "same key three
     times" rotation.
  3. _strip_fences() drops a bare language tag on the first line after
     the fence, but only when that first line is alphabetic (a real
     code line starting with e.g. "def " must survive).
  4. run()'s own failure handling: empty model output and a RuntimeError
     from generate_text() both degrade to a commented failure message in
     the returned code, rather than raising out of run() itself.
"""
import pytest

from agents import code_writer_lean
from eo.errors import MissingDependencyError
from memory.bus import KEYS, read, write

# ---------------------------------------------------------------------------
# 1. _strip_fences
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_plain_code_passes_through_unchanged(self):
        code = "def reverse(s):\n    return s[::-1]"
        assert code_writer_lean._strip_fences(code) == code

    def test_fenced_code_with_language_tag_is_unwrapped(self):
        raw = "```python\ndef reverse(s):\n    return s[::-1]\n```"
        result = code_writer_lean._strip_fences(raw)
        assert result == "def reverse(s):\n    return s[::-1]"

    def test_fenced_code_without_language_tag(self):
        raw = "```\ndef reverse(s):\n    return s[::-1]\n```"
        result = code_writer_lean._strip_fences(raw)
        assert result == "def reverse(s):\n    return s[::-1]"

    def test_first_line_that_looks_like_code_is_not_dropped(self):
        # "def" alone wouldn't trip .isalpha() here since the real first
        # line includes non-alpha chars (parens/colons) -- confirms the
        # bare-tag heuristic doesn't eat real code that happens to start
        # with an alphabetic-looking line.
        raw = "```\nprint('hello')\n```"
        result = code_writer_lean._strip_fences(raw)
        assert result == "print('hello')"

    def test_strips_surrounding_whitespace(self):
        raw = "   print('hi')   \n"
        assert code_writer_lean._strip_fences(raw) == "print('hi')"


# ---------------------------------------------------------------------------
# 2. module_spec sourcing
# ---------------------------------------------------------------------------

class TestModuleSpecSourcing:
    def test_no_spec_arg_and_nothing_in_memory_raises_missing_dependency_error(self, fake_bus, mock_llm):
        with pytest.raises(MissingDependencyError) as exc_info:
            code_writer_lean.run()
        assert exc_info.value.required_role == "prompt_writer_lean"

    def test_explicit_spec_is_written_to_memory(self, fake_bus, mock_llm):
        mock_llm.set_response("print('hi')")
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        code_writer_lean.run(module_spec=spec)
        assert read(KEYS["tier1_module_spec"]) == spec

    def test_falls_back_to_stored_spec_when_none_given(self, fake_bus, mock_llm):
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        write(KEYS["tier1_module_spec"], spec)
        mock_llm.set_response("print('hi')")
        result = code_writer_lean.run()
        assert result["name"] == "greeter"


# ---------------------------------------------------------------------------
# 3. CHAIN composition (OR-3d)
# ---------------------------------------------------------------------------

class TestChainComposition:
    def test_chain_has_four_steps(self):
        assert len(code_writer_lean.CHAIN) == 4

    def test_first_three_steps_are_openrouter_on_independent_accounts(self):
        openrouter_steps = code_writer_lean.CHAIN[:3]
        assert all(step["provider"] == "openrouter" for step in openrouter_steps)
        key_envs = [step["key_env"] for step in openrouter_steps]
        assert key_envs == ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3"]
        assert len(set(key_envs)) == 3  # genuinely independent, not one key x3

    def test_last_step_is_gemini_fallback(self):
        last = code_writer_lean.CHAIN[-1]
        assert last["provider"] == "gemini"
        assert last["key_env"] == "GEMINI_API_KEY_1"

    def test_run_uses_the_module_chain(self, fake_bus, mock_llm):
        mock_llm.set_response("print('hi')")
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        code_writer_lean.run(module_spec=spec)
        chain = mock_llm.mock.call_args.kwargs.get("chain") or mock_llm.mock.call_args[0][2]
        assert chain == code_writer_lean.CHAIN


# ---------------------------------------------------------------------------
# 4. run() failure handling
# ---------------------------------------------------------------------------

class TestRunFailureHandling:
    def test_empty_model_output_becomes_a_commented_failure_message(self, fake_bus, mock_llm):
        mock_llm.set_response("")
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        result = code_writer_lean.run(module_spec=spec)
        assert "CODE WRITER FAILED" in result["code"]
        assert "greeter" in result["code"]

    def test_runtime_error_from_generate_text_is_caught_not_raised(self, fake_bus, mock_llm):
        mock_llm.raise_on_call(RuntimeError("all providers exhausted"))
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        result = code_writer_lean.run(module_spec=spec)
        assert "CODE WRITER FAILED" in result["code"]
        assert "all providers exhausted" in result["code"]

    def test_non_runtime_error_still_propagates(self, fake_bus, mock_llm):
        mock_llm.raise_on_call(ValueError("something else entirely"))
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        with pytest.raises(ValueError):
            code_writer_lean.run(module_spec=spec)


# ---------------------------------------------------------------------------
# 5. run() success path / return shape
# ---------------------------------------------------------------------------

class TestRunSuccess:
    def test_returns_name_language_and_stripped_code(self, fake_bus, mock_llm):
        mock_llm.set_response("```python\ndef greet():\n    print('hi')\n```")
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        result = code_writer_lean.run(module_spec=spec)
        assert result["name"] == "greeter"
        assert result["language"] == "python"
        assert result["code"] == "def greet():\n    print('hi')"

    def test_missing_language_defaults_to_python(self, fake_bus, mock_llm):
        mock_llm.set_response("print('hi')")
        spec = {"name": "greeter", "description": "prints hi"}
        result = code_writer_lean.run(module_spec=spec)
        assert result["language"] == "python"

    def test_missing_name_defaults_to_module(self, fake_bus, mock_llm):
        mock_llm.set_response("print('hi')")
        spec = {"description": "prints hi", "language": "python"}
        result = code_writer_lean.run(module_spec=spec)
        assert result["name"] == "module"

    def test_writes_result_to_tier1_code_key(self, fake_bus, mock_llm):
        mock_llm.set_response("print('hi')")
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        code_writer_lean.run(module_spec=spec)
        stored = read(KEYS["tier1_code"])
        assert stored["name"] == "greeter"

    def test_forwards_session_id_path_and_domain(self, fake_bus, mock_llm):
        mock_llm.set_response("print('hi')")
        spec = {"name": "greeter", "description": "prints hi", "language": "python"}
        code_writer_lean.run(module_spec=spec, session_id="sess-3", path="direct", domain="coding")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs.get("session_id") == "sess-3"
        assert kwargs.get("path") == "direct"
        assert kwargs.get("domain") == "coding"
        assert kwargs.get("agent_name") == "Code Writer (lean)"
