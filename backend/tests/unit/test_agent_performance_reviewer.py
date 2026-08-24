"""
tests/unit/test_agent_performance_reviewer.py — Patch 7f-3.

Covers agents/performance_reviewer.py's run(): generates a profiling
harness via generate_text(), executes it through
agents.sandbox_tester._run_one_module() (imported directly into this
module's namespace, so it's patched there -- see module docstring's
"same function the tier-3 pool already uses" note), and writes an
advisory (never gating) result.

  1. _select_module_to_profile(): first module (insertion order) with
     a PASSING test_results entry; skips "_fixer_error"; returns
     (None, None) when nothing qualifies.
  2. run()'s early-exit paths: no fixed_code/submitted_code at all,
     and fixed_code present but nothing passing yet -- both advisory
     "errors", never raised exceptions (this role is advisory-only
     per its own docstring).
  3. run()'s fixed_code-over-submitted_code fallback preference.
  4. _extract_json_result(): last PARSEABLE JSON line wins, handles
     both str and list[str] stdout shapes, returns None on no valid
     JSON anywhere.
  5. Harness-generation failure (generate_text raises RuntimeError)
     degrades to a harness that prints a JSON error object instead of
     propagating the exception -- _run_one_module still gets called
     with that harness.
  6. run()'s bus write and emit_event calls happen on every path.
"""
import json

import pytest

import agents.performance_reviewer as performance_reviewer


PASSING_MODULE = {"name": "validator", "code": "def validate(x): return x"}


@pytest.fixture(autouse=True)
def _fake_emit_event(monkeypatch):
    calls = []
    monkeypatch.setattr(performance_reviewer, "emit_event", lambda *a, **k: calls.append((a, k)) or True)
    return calls


def _seed(monkeypatch, fixed_code=None, submitted_code=None, test_results=None):
    from memory.bus import KEYS
    store = {
        KEYS["fixed_code"]: fixed_code,
        KEYS["submitted_code"]: submitted_code or {},
        KEYS["test_results"]: test_results or {},
    }

    def _read(key, default=None):
        return store.get(key, default) if store.get(key) is not None else default

    monkeypatch.setattr(performance_reviewer, "read", _read)


# ---------------------------------------------------------------------------
# 1. _select_module_to_profile()
# ---------------------------------------------------------------------------
class TestSelectModuleToProfile:
    def test_returns_first_passing_module_in_insertion_order(self):
        fixed_code = {"mod_a": {"code": "a"}, "mod_b": {"code": "b"}}
        test_results = {"mod_a": {"passed": False}, "mod_b": {"passed": True}}
        name, data = performance_reviewer._select_module_to_profile(fixed_code, test_results)
        assert name == "mod_b"
        assert data == {"code": "b"}

    def test_skips_fixer_error_sentinel_key(self):
        fixed_code = {"_fixer_error": "boom", "mod_a": {"code": "a"}}
        test_results = {"_fixer_error": {"passed": True}, "mod_a": {"passed": True}}
        name, data = performance_reviewer._select_module_to_profile(fixed_code, test_results)
        assert name == "mod_a"

    def test_no_passing_module_returns_none_none(self):
        fixed_code = {"mod_a": {"code": "a"}}
        test_results = {"mod_a": {"passed": False}}
        name, data = performance_reviewer._select_module_to_profile(fixed_code, test_results)
        assert (name, data) == (None, None)

    def test_module_with_no_test_result_at_all_is_skipped(self):
        fixed_code = {"mod_a": {"code": "a"}}
        test_results = {}
        name, data = performance_reviewer._select_module_to_profile(fixed_code, test_results)
        assert (name, data) == (None, None)


# ---------------------------------------------------------------------------
# 2. run()'s early-exit paths (advisory, never raises)
# ---------------------------------------------------------------------------
class TestRunEarlyExit:
    def test_no_fixed_code_or_submitted_code_returns_advisory_error(self, fake_bus, monkeypatch):
        _seed(monkeypatch, fixed_code=None, submitted_code={}, test_results={})
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)
        result = performance_reviewer.run()
        assert result["passed"] is False
        assert result["module"] is None
        assert "nothing to profile yet" in result["error"]

    def test_no_passing_module_yet_returns_advisory_error_not_missing_dependency(self, fake_bus, monkeypatch):
        _seed(monkeypatch, fixed_code={"mod_a": {"code": "a"}}, test_results={"mod_a": {"passed": False}})
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)
        result = performance_reviewer.run()
        assert result["passed"] is False
        assert result["module"] is None
        assert "nothing safe to profile" in result["error"]


# ---------------------------------------------------------------------------
# 3. fixed_code-over-submitted_code fallback
# ---------------------------------------------------------------------------
class TestFixedCodeFallback:
    def test_uses_submitted_code_when_fixed_code_is_missing(self, fake_bus, monkeypatch):
        _seed(
            monkeypatch,
            fixed_code=None,
            submitted_code={"mod_a": {"code": "a"}},
            test_results={"mod_a": {"passed": True}},
        )
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)
        monkeypatch.setattr(performance_reviewer, "generate_text", lambda *a, **k: "import json\nprint('{}')")
        monkeypatch.setattr(
            performance_reviewer, "_run_one_module",
            lambda name, data: (name, {"passed": True, "stdout": "{}", "stderr": "", "error": None}),
        )
        result = performance_reviewer.run()
        assert result["module"] == "mod_a"

    def test_prefers_fixed_code_over_submitted_code_when_both_present(self, fake_bus, monkeypatch):
        _seed(
            monkeypatch,
            fixed_code={"mod_fixed": {"code": "a"}},
            submitted_code={"mod_submitted": {"code": "b"}},
            test_results={"mod_fixed": {"passed": True}, "mod_submitted": {"passed": True}},
        )
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)
        monkeypatch.setattr(performance_reviewer, "generate_text", lambda *a, **k: "import json\nprint('{}')")
        monkeypatch.setattr(
            performance_reviewer, "_run_one_module",
            lambda name, data: (name, {"passed": True, "stdout": "{}", "stderr": "", "error": None}),
        )
        result = performance_reviewer.run()
        assert result["module"] == "mod_fixed"


# ---------------------------------------------------------------------------
# 4. _extract_json_result()
# ---------------------------------------------------------------------------
class TestExtractJsonResult:
    def test_last_valid_json_line_wins(self):
        stdout = 'not json\n{"elapsed_seconds": 0.5}\n{"elapsed_seconds": 1.2, "peak_memory_kb": 100}'
        result = performance_reviewer._extract_json_result(stdout)
        assert result == {"elapsed_seconds": 1.2, "peak_memory_kb": 100}

    def test_list_of_lines_is_normalized(self):
        stdout = ["setup output", '{"elapsed_seconds": 0.9}']
        result = performance_reviewer._extract_json_result(stdout)
        assert result == {"elapsed_seconds": 0.9}

    def test_no_valid_json_returns_none(self):
        assert performance_reviewer._extract_json_result("just some text\nmore text") is None

    def test_empty_stdout_returns_none(self):
        assert performance_reviewer._extract_json_result("") is None
        assert performance_reviewer._extract_json_result(None) is None

    def test_blank_lines_are_skipped(self):
        stdout = '\n\n{"elapsed_seconds": 0.3}\n\n'
        result = performance_reviewer._extract_json_result(stdout)
        assert result == {"elapsed_seconds": 0.3}


# ---------------------------------------------------------------------------
# 5. Harness-generation failure degrades gracefully
# ---------------------------------------------------------------------------
class TestHarnessGenerationFailure:
    def test_generate_text_runtime_error_produces_error_harness_not_a_raise(self, fake_bus, monkeypatch):
        _seed(
            monkeypatch,
            fixed_code={"mod_a": {"code": "a"}},
            test_results={"mod_a": {"passed": True}},
        )
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)

        def _raise(*a, **k):
            raise RuntimeError("all providers exhausted")

        monkeypatch.setattr(performance_reviewer, "generate_text", _raise)

        captured_harness = {}

        def _fake_run_one_module(name, module_data):
            captured_harness["code"] = module_data["code"]
            return name, {"passed": False, "stdout": "", "stderr": "", "error": "harness failed"}

        monkeypatch.setattr(performance_reviewer, "_run_one_module", _fake_run_one_module)

        result = performance_reviewer.run()  # must not raise
        assert "profiling harness generation failed" in captured_harness["code"]
        assert result["module"] == "mod_a"


# ---------------------------------------------------------------------------
# 6. Bus write and emit_event on every path
# ---------------------------------------------------------------------------
class TestBusWriteAndEvents:
    def test_result_written_to_performance_review_key(self, fake_bus, monkeypatch, _fake_emit_event):
        _seed(monkeypatch, fixed_code=None, submitted_code={}, test_results={})
        writes = {}
        monkeypatch.setattr(performance_reviewer, "write", lambda key, val: writes.__setitem__(key, val))
        result = performance_reviewer.run()
        assert writes[performance_reviewer.KEYS["performance_review"]] == result

    def test_emit_event_fires_agent_start_and_agent_done(self, fake_bus, monkeypatch, _fake_emit_event):
        _seed(monkeypatch, fixed_code=None, submitted_code={}, test_results={})
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)
        performance_reviewer.run(session_id="sess-1")
        event_types = [call[0][0] for call in _fake_emit_event]
        assert "agent_start" in event_types
        assert "agent_done" in event_types

    def test_module_result_includes_parsed_result_from_stdout(self, fake_bus, monkeypatch, _fake_emit_event):
        _seed(
            monkeypatch,
            fixed_code={"mod_a": {"code": "a"}},
            test_results={"mod_a": {"passed": True}},
        )
        monkeypatch.setattr(performance_reviewer, "write", lambda *a, **k: None)
        monkeypatch.setattr(performance_reviewer, "generate_text", lambda *a, **k: "import json\nprint('ok')")
        monkeypatch.setattr(
            performance_reviewer, "_run_one_module",
            lambda name, data: (name, {
                "passed": True, "stdout": '{"elapsed_seconds": 0.4}', "stderr": "", "error": None,
            }),
        )
        result = performance_reviewer.run()
        assert result["parsed_result"] == {"elapsed_seconds": 0.4}
        assert result["module"] == "mod_a"
