"""
tests/unit/test_agent_dataset_analyst.py — Patch 7f-4d-1.

Covers agents/dataset_analyst.py's run() and its helpers: resolving a
dataset path (explicit arg -> bus key -> filename regex in task_text),
the size/extension gate, the LLM analysis-code generation step
(dynamic fallback chain, static FALLBACK_CHAIN when the dynamic one
comes back empty, and the RuntimeError -> error-JSON degrade path),
the base64 embedding preamble that hands the dataset file to
agents.sandbox_tester._run_one_module() unmodified, and
_extract_json_result()'s last-valid-JSON-line parsing (including the
list-vs-str stdout normalization bugfix its own docstring documents).

generate_text is faked via the shared `mock_llm` fixture (bound-name
import, same as every other agent in this suite). _run_one_module and
emit_event are monkeypatched directly on the module. The deferred
`from eo.dynamic_chain import build_fallback_chain` import is faked via
a sys.modules substitute, same approach test_agent_fact_detector.py
takes for its own deferred agents.generic_worker import.
"""
import base64
import json
import sys

import pytest

import agents.dataset_analyst as dataset_analyst


@pytest.fixture(autouse=True)
def _fake_emit_event(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dataset_analyst, "emit_event",
        lambda event, **kwargs: calls.append((event, kwargs)),
    )
    return calls


@pytest.fixture
def fake_run_one_module(monkeypatch):
    mock_result = {"passed": True, "stdout": '{"summary": "ok"}', "stderr": "", "error": None}
    captured = {}

    def _run(module_name, module_data):
        captured["module_name"] = module_name
        captured["module_data"] = module_data
        return module_name, dict(mock_result)

    monkeypatch.setattr(dataset_analyst, "_run_one_module", _run)
    return captured, mock_result


@pytest.fixture
def fake_dynamic_chain(monkeypatch):
    fake = type("M", (), {"build_fallback_chain": staticmethod(lambda role: [])})()
    sys.modules["eo.dynamic_chain"] = fake
    return fake


def _write_dataset(tmp_path, name="sales.csv", content="region,total\nEast,100\n"):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# 1. _strip_fences(): output-cleanup, same shape as code_writer_lean.py's
# ---------------------------------------------------------------------------
class TestStripFences:
    def test_plain_code_unchanged(self):
        assert dataset_analyst._strip_fences("print(1)") == "print(1)"

    def test_strips_bare_fence(self):
        code = "```\nprint(1)\n```"
        assert dataset_analyst._strip_fences(code) == "print(1)"

    def test_strips_language_tagged_fence(self):
        code = "```python\nprint(1)\n```"
        assert dataset_analyst._strip_fences(code) == "print(1)"

    def test_strips_surrounding_whitespace(self):
        code = "   \nprint(1)\n   "
        assert dataset_analyst._strip_fences(code) == "print(1)"


# ---------------------------------------------------------------------------
# 2. _extract_json_result(): last-valid-JSON-line parsing
# ---------------------------------------------------------------------------
class TestExtractJsonResult:
    def test_none_stdout_returns_none(self):
        assert dataset_analyst._extract_json_result(None) is None

    def test_empty_string_stdout_returns_none(self):
        assert dataset_analyst._extract_json_result("") is None

    def test_single_json_line(self):
        result = dataset_analyst._extract_json_result('{"summary": "ok"}')
        assert result == {"summary": "ok"}

    def test_takes_last_parseable_line_not_just_the_last_line(self):
        stdout = 'not json\n{"summary": "real result"}\ntrailing garbage'
        result = dataset_analyst._extract_json_result(stdout)
        assert result == {"summary": "real result"}

    def test_no_parseable_line_returns_none(self):
        stdout = "just some text\nmore text, no json here"
        assert dataset_analyst._extract_json_result(stdout) is None

    def test_list_stdout_is_joined_before_parsing(self):
        # Patch 8 bugfix: stdout can arrive as a list of strings.
        stdout = ["first line", '{"summary": "from a list"}']
        result = dataset_analyst._extract_json_result(stdout)
        assert result == {"summary": "from a list"}

    def test_blank_lines_are_skipped(self):
        stdout = '{"summary": "value"}\n\n\n'
        result = dataset_analyst._extract_json_result(stdout)
        assert result == {"summary": "value"}


# ---------------------------------------------------------------------------
# 3. _resolve_dataset_path(): explicit -> bus key -> task-text regex
# ---------------------------------------------------------------------------
class TestResolveDatasetPath:
    def test_explicit_path_wins_outright(self, fake_bus):
        dataset_analyst.write(dataset_analyst.KEYS["dataset_path"], "bus_path.csv")
        result = dataset_analyst._resolve_dataset_path("analyze other.csv", "explicit.csv")
        assert result == "explicit.csv"

    def test_falls_back_to_bus_key_when_no_explicit_path(self, fake_bus):
        dataset_analyst.write(dataset_analyst.KEYS["dataset_path"], "bus_path.csv")
        result = dataset_analyst._resolve_dataset_path("analyze this", None)
        assert result == "bus_path.csv"

    def test_falls_back_to_filename_in_task_text(self, fake_bus):
        result = dataset_analyst._resolve_dataset_path("analyze sales.csv for trends", None)
        assert result == "sales.csv"

    def test_task_text_regex_matches_tsv_and_json_too(self, fake_bus):
        assert dataset_analyst._resolve_dataset_path("look at data.tsv", None) == "data.tsv"
        assert dataset_analyst._resolve_dataset_path("look at data.json", None) == "data.json"

    def test_no_match_anywhere_returns_none(self, fake_bus):
        result = dataset_analyst._resolve_dataset_path("just analyze the numbers", None)
        assert result is None

    def test_none_task_text_does_not_raise(self, fake_bus):
        result = dataset_analyst._resolve_dataset_path(None, None)
        assert result is None


# ---------------------------------------------------------------------------
# 4. run(): input-gap short-circuits (no LLM call, no sandbox run)
# ---------------------------------------------------------------------------
class TestRunInputGaps:
    def test_no_dataset_file_found_returns_failed_result(self, fake_bus, mock_llm):
        result = dataset_analyst.run(task_text="analyze nothing.csv")
        assert result["passed"] is False
        assert "No readable dataset file found" in result["error"]
        assert mock_llm.mock.call_count == 0

    def test_dataset_path_pointing_at_nonexistent_file_fails_gracefully(self, fake_bus, mock_llm):
        result = dataset_analyst.run(dataset_path="/tmp/does_not_exist_xyz.csv")
        assert result["passed"] is False
        assert mock_llm.mock.call_count == 0

    def test_unsupported_extension_is_rejected(self, fake_bus, mock_llm, tmp_path):
        path = _write_dataset(tmp_path, name="notes.txt", content="hello")
        result = dataset_analyst.run(dataset_path=path)
        assert result["passed"] is False
        assert "Unsupported dataset type" in result["error"]
        assert mock_llm.mock.call_count == 0

    def test_oversized_dataset_is_rejected(self, fake_bus, mock_llm, tmp_path, monkeypatch):
        monkeypatch.setattr(dataset_analyst, "MAX_DATASET_BYTES", 5)
        path = _write_dataset(tmp_path, content="way more than five bytes")
        result = dataset_analyst.run(dataset_path=path)
        assert result["passed"] is False
        assert "over this agent's 5-byte cap" in result["error"]
        assert mock_llm.mock.call_count == 0

    def test_all_gap_results_have_parsed_result_none(self, fake_bus, mock_llm):
        result = dataset_analyst.run(task_text="nothing here")
        assert result["parsed_result"] is None

    def test_gap_results_are_still_written_to_the_bus(self, fake_bus, mock_llm, monkeypatch):
        writes = {}
        monkeypatch.setattr(dataset_analyst, "write", lambda key, val: writes.__setitem__(key, val))
        result = dataset_analyst.run(task_text="nothing here")
        assert writes[dataset_analyst.KEYS["dataset_analysis"]] == result

    def test_gap_results_still_emit_agent_start_and_done(self, fake_bus, mock_llm, _fake_emit_event):
        dataset_analyst.run(task_text="nothing here", session_id="sess1")
        events = [c[0] for c in _fake_emit_event]
        assert events == ["agent_start", "agent_done"]


# ---------------------------------------------------------------------------
# 5. run(): the successful end-to-end path
# ---------------------------------------------------------------------------
class TestRunSuccessPath:
    def test_dataset_is_base64_embedded_and_generated_code_appended(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path, content="region,total\nEast,100\n")
        mock_llm.set_response("print('analysis')")

        dataset_analyst.run(dataset_path=path)

        captured, _ = fake_run_one_module
        full_code = captured["module_data"]["code"]
        assert "print('analysis')" in full_code
        expected_b64 = base64.b64encode(b"region,total\nEast,100\n").decode("ascii")
        assert expected_b64 in full_code
        assert captured["module_data"]["language"] == "python"
        assert captured["module_name"] == "dataset_analysis"

    def test_llm_generated_fences_are_stripped_before_use(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path)
        mock_llm.set_response("```python\nprint('fenced')\n```")

        dataset_analyst.run(dataset_path=path)

        captured, _ = fake_run_one_module
        full_code = captured["module_data"]["code"]
        assert "print('fenced')" in full_code
        assert "```" not in full_code

    def test_parsed_result_extracted_from_sandbox_stdout(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        captured, mock_result = fake_run_one_module
        mock_result["stdout"] = '{"summary": "125 rows analyzed"}'
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        result = dataset_analyst.run(dataset_path=path)

        assert result["parsed_result"] == {"summary": "125 rows analyzed"}
        assert result["passed"] is True

    def test_dynamic_chain_used_when_non_empty(
        self, fake_bus, mock_llm, fake_run_one_module, tmp_path, monkeypatch
    ):
        custom_chain = [{"provider": "groq", "model": "custom-model", "key_env": "K"}]
        fake_mod = type("M", (), {"build_fallback_chain": staticmethod(lambda role: custom_chain)})()
        sys.modules["eo.dynamic_chain"] = fake_mod
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        dataset_analyst.run(dataset_path=path)

        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == custom_chain

    def test_static_fallback_chain_used_when_dynamic_chain_empty(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        dataset_analyst.run(dataset_path=path)

        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == dataset_analyst.FALLBACK_CHAIN

    def test_generate_text_called_with_filename_in_system_prompt(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path, name="my_data.csv")
        mock_llm.set_response("print('x')")

        dataset_analyst.run(dataset_path=path)

        system_prompt = mock_llm.mock.call_args.args[0]
        assert "my_data.csv" in system_prompt

    def test_task_text_defaults_to_summarize_when_omitted(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path, name="my_data.csv")
        mock_llm.set_response("print('x')")

        dataset_analyst.run(dataset_path=path)

        user_content = json.loads(mock_llm.mock.call_args.args[1])
        assert "my_data.csv" in user_content["task"]

    def test_explicit_task_text_forwarded_as_is(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        dataset_analyst.run(task_text="find the average of the total column", dataset_path=path)

        user_content = json.loads(mock_llm.mock.call_args.args[1])
        assert user_content["task"] == "find the average of the total column"

    def test_session_id_path_and_domain_forwarded_to_generate_text(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path
    ):
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        dataset_analyst.run(
            dataset_path=path, session_id="sess-1", path="node.child", domain="research",
        )

        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["path"] == "node.child"
        assert kwargs["domain"] == "research"
        assert kwargs["agent_name"] == "Dataset Analyst"

    def test_result_written_to_bus_on_success(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain, tmp_path, monkeypatch
    ):
        writes = {}
        monkeypatch.setattr(dataset_analyst, "write", lambda key, val: writes.__setitem__(key, val))
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        result = dataset_analyst.run(dataset_path=path)

        assert writes[dataset_analyst.KEYS["dataset_analysis"]] == result

    def test_agent_start_and_done_emitted_with_expected_payload_shape(
        self, fake_bus, mock_llm, fake_run_one_module, fake_dynamic_chain,
        tmp_path, _fake_emit_event,
    ):
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        dataset_analyst.run(dataset_path=path, session_id="sess-1")

        start_event, start_kwargs = _fake_emit_event[0]
        done_event, done_kwargs = _fake_emit_event[1]
        assert start_event == "agent_start"
        assert start_kwargs["agent"] == "dataset_analyst"
        assert done_event == "agent_done"
        assert "duration_ms" in done_kwargs["payload"]
        assert done_kwargs["payload"]["summary"] == "passed"

    def test_done_summary_reflects_failure_reason_when_not_passed(
        self, fake_bus, mock_llm, fake_dynamic_chain, tmp_path, monkeypatch, _fake_emit_event
    ):
        def _run(module_name, module_data):
            return module_name, {
                "passed": False, "stdout": "", "stderr": "boom",
                "error": "sandbox exploded",
            }

        monkeypatch.setattr(dataset_analyst, "_run_one_module", _run)
        path = _write_dataset(tmp_path)
        mock_llm.set_response("print('x')")

        dataset_analyst.run(dataset_path=path)

        done_event, done_kwargs = _fake_emit_event[1]
        assert "sandbox exploded" in done_kwargs["payload"]["summary"]


# ---------------------------------------------------------------------------
# 6. run(): generate_text failure degrades to an error-JSON script
# ---------------------------------------------------------------------------
class TestRunGenerateTextFailure:
    def test_runtime_error_degrades_to_error_printing_code(
        self, fake_bus, fake_run_one_module, fake_dynamic_chain, tmp_path, monkeypatch
    ):
        def _raise(*args, **kwargs):
            raise RuntimeError("all providers exhausted")

        monkeypatch.setattr(dataset_analyst, "generate_text", _raise)
        path = _write_dataset(tmp_path)

        dataset_analyst.run(dataset_path=path)

        captured, _ = fake_run_one_module
        full_code = captured["module_data"]["code"]
        assert "analysis code generation failed" in full_code
        assert "all providers exhausted" in full_code
        # still valid enough Python to be handed to the sandbox unmodified
        assert "import json" in full_code
