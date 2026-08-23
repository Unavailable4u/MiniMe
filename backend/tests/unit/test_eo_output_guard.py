"""
tests/unit/test_eo_output_guard.py — Patch 7c.

eo/output_guard.py had zero test coverage before this. It's the
Guardrails-AI wrapper sitting at three separate choke points in the
pipeline (agents/code_writers.py's run(), api/task_runner.py's
_run_tier3_hires(), eo/result_render.py's collect_artifacts()) and, per
the module's own docstrings, every one of those call sites is
OnFailAction.NOOP -- there is no reask, no exception, nothing that would
naturally surface a validator that quietly stopped catching what it's
supposed to catch. That's exactly the "fails silently and expensively"
shape the audit called out: get the boolean wrong in either direction --
a real failure that passes, or a valid value that gets flagged -- and the
caller's existing fail-open/fail-as-is behavior swallows it with no
error, no traceback, nothing in the logs beyond a possible NOOP warning.
These tests exist to pin down that boolean (and the human-readable
reason string) for every branch of all three validators, plus the
threading workaround (_ensure_event_loop) each of the three public
validate_*() functions depends on.

Covers, per public entry point:
  - validate_module_code() / ModuleCodeNonEmpty
  - validate_final_answer() / FinalAnswerWellFormed
  - validate_artifact_entry() / ArtifactEntryWellFormed
  - _ensure_event_loop() -- exercised on a real worker thread, since
    that's the only context where asyncio.get_event_loop() actually
    raises (see the function's own docstring); a main-thread-only test
    would never touch the code path it exists for.
  - get_code_guard() / get_answer_guard() / get_artifact_guard() --
    memoization (each returns the same instance on repeat calls).
  - get_guard() -- confirmed to still raise NotImplementedError, so a
    future accidental re-wiring of the old Part-1 placeholder name gets
    caught immediately instead of silently resurrecting dead code.

Nothing here mocks eo.output_guard itself -- the module is a leaf with
no cross-imports into agents/ or api/ (by design, per its own Part 3
comment), so it can be exercised directly against the real `guardrails`
library with no fakes needed.
"""
import asyncio
import threading

import pytest

import eo.output_guard as output_guard


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_memoized_guards(monkeypatch):
    """Each of the three Guard singletons is a lazily-built module-level
    global. Reset all three before every test so memoization tests (which
    deliberately observe first-vs-second-call identity) can't be
    order-dependent on whatever test ran before them, and so no test
    accidentally reuses a Guard instance another test already touched."""
    monkeypatch.setattr(output_guard, "_code_guard", None)
    monkeypatch.setattr(output_guard, "_answer_guard", None)
    monkeypatch.setattr(output_guard, "_artifact_guard", None)
    yield


# ---------------------------------------------------------------------
# _ensure_event_loop
# ---------------------------------------------------------------------

def test_ensure_event_loop_binds_a_loop_on_a_bare_worker_thread():
    """Mirrors the exact scenario the function's docstring describes:
    every real caller runs on a concurrent.futures.ThreadPoolExecutor
    worker thread, which starts with no event loop bound to it at all --
    confirm that's actually true here, then confirm _ensure_event_loop()
    fixes it."""
    outcome = {}

    def worker():
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop()
        output_guard._ensure_event_loop()
        loop = asyncio.get_event_loop()
        outcome["loop"] = loop
        loop.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert outcome.get("loop") is not None


def test_ensure_event_loop_is_idempotent():
    """Calling it twice on the same thread must not replace an
    already-bound loop with a second one -- validate_*() calls this on
    every single invocation, so a version that churned out a fresh loop
    each time would leak one per call."""
    outcome = {}

    def worker():
        output_guard._ensure_event_loop()
        loop_first = asyncio.get_event_loop()
        output_guard._ensure_event_loop()
        loop_second = asyncio.get_event_loop()
        outcome["same"] = loop_first is loop_second
        loop_first.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert outcome.get("same") is True


# ---------------------------------------------------------------------
# validate_module_code() / ModuleCodeNonEmpty
# ---------------------------------------------------------------------

def test_validate_module_code_passes_real_code():
    ok, reason = output_guard.validate_module_code("def f():\n    return 1\n")
    assert ok is True
    assert reason == ""


def test_validate_module_code_fails_on_empty_string():
    ok, reason = output_guard.validate_module_code("")
    assert ok is False
    assert reason != ""


def test_validate_module_code_fails_on_whitespace_only():
    ok, reason = output_guard.validate_module_code("   \n\t  ")
    assert ok is False
    assert reason != ""


def test_validate_module_code_fails_on_failure_placeholder():
    ok, reason = output_guard.validate_module_code(
        "# CODE WRITER FAILED: model returned empty content."
    )
    assert ok is False
    assert "placeholder" in reason.lower()


def test_validate_module_code_fails_on_placeholder_with_leading_whitespace():
    """The check runs against the stripped text, so a placeholder with
    incidental leading blank lines/whitespace must still be caught --
    not just an exact-match on the raw string."""
    ok, reason = output_guard.validate_module_code(
        "\n\n   # CODE WRITER FAILED: model returned empty content."
    )
    assert ok is False


def test_validate_module_code_passes_code_that_merely_mentions_the_marker():
    """The check is startswith(), not "contains" -- real code that has a
    comment referencing the failure marker somewhere in its body (not as
    the first thing in the string) is legitimate code and must pass.
    This is the boundary that distinguishes "is the placeholder" from
    "contains text similar to the placeholder"."""
    code = (
        "def f():\n"
        "    # NOTE: upstream may emit '# CODE WRITER FAILED' on error\n"
        "    return 1\n"
    )
    ok, reason = output_guard.validate_module_code(code)
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------
# validate_final_answer() / FinalAnswerWellFormed
# ---------------------------------------------------------------------

def test_validate_final_answer_passes_well_formed_markdown():
    answer = "## Summary\n\nAll good, here's a fenced block:\n\n```python\nprint('hi')\n```\n"
    ok, reason = output_guard.validate_final_answer(answer)
    assert ok is True
    assert reason == ""


def test_validate_final_answer_fails_on_empty_string():
    ok, reason = output_guard.validate_final_answer("")
    assert ok is False
    assert "empty" in reason.lower()


def test_validate_final_answer_fails_on_whitespace_only():
    ok, reason = output_guard.validate_final_answer("   \n  ")
    assert ok is False
    assert "empty" in reason.lower()


def test_validate_final_answer_fails_on_leaked_dedup_marker():
    answer = "Here's the answer.\n---DEDUP_NOTES---\n{\"reviewer\": \"folded into implementer\"}"
    ok, reason = output_guard.validate_final_answer(answer)
    assert ok is False
    assert "DEDUP_NOTES" in reason


def test_validate_final_answer_fails_on_unbalanced_fence_markers():
    answer = "Unterminated code block:\n\n```python\nprint('hi')\n"
    ok, reason = output_guard.validate_final_answer(answer)
    assert ok is False
    assert "fence" in reason.lower()


def test_validate_final_answer_passes_multiple_balanced_fences():
    """Two separate fenced blocks (four ``` markers total) is still a
    balanced, well-formed answer -- the check is a straight even/odd
    count, not "at most one fenced block"."""
    answer = (
        "First:\n\n```python\nprint('a')\n```\n\n"
        "Second:\n\n```python\nprint('b')\n```\n"
    )
    ok, reason = output_guard.validate_final_answer(answer)
    assert ok is True
    assert reason == ""


def test_validate_final_answer_leaked_marker_check_runs_even_with_balanced_fences():
    """A leaked marker must fail even when the fence count is otherwise
    perfectly balanced -- the two checks are independent, and a
    regression that short-circuited on the first passing check could
    hide this."""
    answer = "Here's the answer.\n---DEDUP_NOTES---\n```json\n{}\n```\n"
    ok, reason = output_guard.validate_final_answer(answer)
    assert ok is False
    assert "DEDUP_NOTES" in reason


# ---------------------------------------------------------------------
# validate_artifact_entry() / ArtifactEntryWellFormed
# ---------------------------------------------------------------------

def test_validate_artifact_entry_passes_well_formed_entry():
    entry = {"type": "html", "code": "<b>hi</b>", "title": "Demo"}
    ok, reason = output_guard.validate_artifact_entry(entry)
    assert ok is True
    assert reason == ""


def test_validate_artifact_entry_passes_without_optional_title():
    entry = {"type": "svg", "code": "<svg></svg>"}
    ok, reason = output_guard.validate_artifact_entry(entry)
    assert ok is True
    assert reason == ""


def test_validate_artifact_entry_passes_with_title_explicitly_none():
    """title=None is explicitly allowed by the validator (the "title is
    not None and not isinstance(...)" guard skips the type check
    entirely when the key is present but None) -- distinct from the
    "key omitted" case above, and worth pinning down separately since
    it's the one branch of the title check that does NOT require a
    string."""
    entry = {"type": "html", "code": "<b>hi</b>", "title": None}
    ok, reason = output_guard.validate_artifact_entry(entry)
    assert ok is True
    assert reason == ""


def test_validate_artifact_entry_fails_when_not_a_dict():
    ok, reason = output_guard.validate_artifact_entry(["not", "a", "dict"])
    assert ok is False
    assert "object" in reason.lower()


def test_validate_artifact_entry_fails_on_missing_type():
    ok, reason = output_guard.validate_artifact_entry({"code": "<b>hi</b>"})
    assert ok is False
    assert "type" in reason.lower()


def test_validate_artifact_entry_fails_on_empty_type():
    ok, reason = output_guard.validate_artifact_entry({"type": "", "code": "<b>hi</b>"})
    assert ok is False
    assert "type" in reason.lower()


def test_validate_artifact_entry_fails_on_non_string_type():
    ok, reason = output_guard.validate_artifact_entry({"type": 123, "code": "<b>hi</b>"})
    assert ok is False
    assert "type" in reason.lower()


def test_validate_artifact_entry_fails_on_missing_code():
    ok, reason = output_guard.validate_artifact_entry({"type": "html"})
    assert ok is False
    assert "code" in reason.lower()


def test_validate_artifact_entry_fails_on_empty_code():
    ok, reason = output_guard.validate_artifact_entry({"type": "html", "code": "   "})
    assert ok is False
    assert "code" in reason.lower()


def test_validate_artifact_entry_fails_on_non_string_code():
    """This is the exact regression the module's docstring calls out:
    the pre-Part-4 `if not entry.get("code")` truthiness check would let
    a non-empty list/dict through as "truthy" -- confirm the real check
    rejects it."""
    ok, reason = output_guard.validate_artifact_entry(
        {"type": "html", "code": ["not", "a", "string"]}
    )
    assert ok is False
    assert "code" in reason.lower()


def test_validate_artifact_entry_fails_on_non_string_title():
    ok, reason = output_guard.validate_artifact_entry(
        {"type": "html", "code": "<b>hi</b>", "title": 42}
    )
    assert ok is False
    assert "title" in reason.lower()


# ---------------------------------------------------------------------
# Guard memoization
# ---------------------------------------------------------------------

def test_get_code_guard_is_memoized():
    g1 = output_guard.get_code_guard()
    g2 = output_guard.get_code_guard()
    assert g1 is g2


def test_get_answer_guard_is_memoized():
    g1 = output_guard.get_answer_guard()
    g2 = output_guard.get_answer_guard()
    assert g1 is g2


def test_get_artifact_guard_is_memoized():
    g1 = output_guard.get_artifact_guard()
    g2 = output_guard.get_artifact_guard()
    assert g1 is g2


# ---------------------------------------------------------------------
# get_guard() -- retired Part-1 placeholder
# ---------------------------------------------------------------------

def test_get_guard_still_raises_not_implemented():
    """Guards against a future refactor accidentally re-wiring the old
    Part-1 placeholder name instead of one of the three scoped
    get_*_guard() functions -- if this ever starts returning something,
    it means that regression happened silently."""
    with pytest.raises(NotImplementedError):
        output_guard.get_guard()
