"""
tests/unit/test_agent_reviewer_fixer_lean.py — Patch 7f-3.

Covers agents/reviewer_fixer_lean.py's run(): the tier-1 combined
review+fix pass over a single small code module.

  1. Module resolution: an explicit `module` arg is written to
     KEYS["tier1_code"] and used; omitting it falls back to reading
     KEYS["tier1_code"] from the bus; neither present raises
     MissingDependencyError("code_writer_lean", ...).
  2. CHAIN shape: exactly 6 steps -- 2 Groq, 3 OpenRouter (using the
     3 distinct key slots), 1 Gemini fallback -- generate_text is
     called with this exact chain every time (Patch 8.9's fix).
  3. Parsing: valid JSON response updates code + issues_found;
     malformed JSON degrades to the ORIGINAL code with a explanatory
     issue, never propagating garbage downstream.
  4. Output shape and bus writes: tier1_review_notes gets just
     issues_found; tier1_fixed_code gets the full result; the
     function's return value matches tier1_fixed_code exactly.
  5. Missing "code" in parsed JSON falls back to the module's own
     original code rather than becoming empty/None.
"""
import json

import pytest

import agents.reviewer_fixer_lean as reviewer_fixer_lean
from eo.errors import MissingDependencyError


SAMPLE_MODULE = {"name": "validator", "language": "python", "code": "def validate(x):\n    return x"}


# ---------------------------------------------------------------------------
# 1. Module resolution
# ---------------------------------------------------------------------------
class TestModuleResolution:
    def test_explicit_module_arg_is_written_to_tier1_code(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": SAMPLE_MODULE["code"]})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        from memory.bus import read, KEYS
        assert read(KEYS["tier1_code"]) == SAMPLE_MODULE

    def test_no_module_arg_falls_back_to_reading_tier1_code(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["tier1_code"], SAMPLE_MODULE)
        mock_llm.set_json_response({"issues_found": [], "code": SAMPLE_MODULE["code"]})
        result = reviewer_fixer_lean.run()
        assert result["name"] == "validator"

    def test_no_module_arg_and_nothing_on_bus_raises_missing_dependency_error(self, fake_bus, mock_llm):
        with pytest.raises(MissingDependencyError) as exc_info:
            reviewer_fixer_lean.run()
        assert exc_info.value.required_role == "code_writer_lean"


# ---------------------------------------------------------------------------
# 2. CHAIN shape (Patch 8.9 fix: real per-step key redundancy)
# ---------------------------------------------------------------------------
class TestChainShape:
    def test_chain_has_exactly_six_steps(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert len(chain) == 6

    def test_first_two_steps_are_groq(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert chain[0]["provider"] == "groq"
        assert chain[1]["provider"] == "groq"
        assert chain[0]["model"] != chain[1]["model"]

    def test_openrouter_steps_use_three_distinct_key_envs(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        chain = mock_llm.mock.call_args.kwargs["chain"]
        openrouter_steps = [s for s in chain if s["provider"] == "openrouter"]
        assert len(openrouter_steps) == 3
        key_envs = {s["key_env"] for s in openrouter_steps}
        assert key_envs == {"OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3"}

    def test_last_step_is_gemini_fallback(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert chain[-1]["provider"] == "gemini"
        assert chain[-1]["key_env"] == "GEMINI_API_KEY_1"

    def test_agent_name_is_reviewer_fixer_lean_label(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert mock_llm.mock.call_args.kwargs["agent_name"] == "Reviewer+Fixer (lean)"


# ---------------------------------------------------------------------------
# 3. Parsing: valid vs malformed JSON
# ---------------------------------------------------------------------------
class TestParsing:
    def test_valid_json_updates_code_and_issues(self, fake_bus, mock_llm):
        mock_llm.set_json_response({
            "issues_found": ["off-by-one in loop bound"],
            "code": "def validate(x):\n    return x is not None",
        })
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert result["code"] == "def validate(x):\n    return x is not None"
        assert result["issues_found"] == ["off-by-one in loop bound"]

    def test_malformed_json_falls_back_to_original_code(self, fake_bus, mock_llm):
        mock_llm.set_response("this is not valid JSON at all {{{")
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert result["code"] == SAMPLE_MODULE["code"]
        assert result["issues_found"] == ["Reviewer+Fixer output was not valid JSON — kept original code."]

    def test_fenced_json_response_is_stripped_before_parsing(self, fake_bus, mock_llm):
        mock_llm.set_response(
            '```json\n{"issues_found": [], "code": "def validate(x):\\n    return bool(x)"}\n```'
        )
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert result["code"] == "def validate(x):\n    return bool(x)"

    def test_missing_code_field_falls_back_to_original_module_code(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": ["something"]})  # no "code" key at all
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert result["code"] == SAMPLE_MODULE["code"]

    def test_missing_issues_found_field_defaults_to_empty_list(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"code": "def validate(x):\n    return x"})  # no issues_found key
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert result["issues_found"] == []


# ---------------------------------------------------------------------------
# 4. Output shape and bus writes
# ---------------------------------------------------------------------------
class TestOutputAndBusWrites:
    def test_result_includes_name_and_language_from_module(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        assert result["name"] == "validator"
        assert result["language"] == "python"

    def test_missing_name_defaults_to_module(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        result = reviewer_fixer_lean.run(module={"code": "x = 1"})
        assert result["name"] == "module"

    def test_missing_language_defaults_to_python(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        result = reviewer_fixer_lean.run(module={"code": "x = 1"})
        assert result["language"] == "python"

    def test_tier1_review_notes_written_with_only_issues_found(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": ["issue one"], "code": "x = 1"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        from memory.bus import read, KEYS
        assert read(KEYS["tier1_review_notes"]) == {"issues_found": ["issue one"]}

    def test_tier1_fixed_code_matches_return_value(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x = 1"})
        result = reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        from memory.bus import read, KEYS
        assert read(KEYS["tier1_fixed_code"]) == result

    def test_session_id_and_path_and_domain_are_forwarded(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE, session_id="sess-1", path="fixed", domain="coding")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["path"] == "fixed"
        assert kwargs["domain"] == "coding"

    def test_user_content_is_json_serialized_module(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"issues_found": [], "code": "x"})
        reviewer_fixer_lean.run(module=SAMPLE_MODULE)
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert json.loads(user_content) == SAMPLE_MODULE
