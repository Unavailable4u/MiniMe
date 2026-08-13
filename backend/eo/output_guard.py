"""
eo/output_guard.py — D3 Part 2. Guardrails AI wrapper.

Part 2 wires in the first of three planned choke points: agents/code_writers.py's
run(), right before `write(KEYS["submitted_code"], results)` (~line 363) --
NOT `specs = json.loads(raw_text)` (~line 285) as Part 1's plan originally
assumed. That line parses a spec list ({"modules": [...]}) inside
_derive_specs_from_task_text(), a fallback that only fires when
prompt_writer didn't already run -- it has nothing to do with what
sandbox_tester.py consumes. The real module-code map is
{module_name: code_string} (a plain string per module, no "language" key --
also a correction from the original plan), built up in run()'s
as_completed() loop and written to KEYS["submitted_code"] right after.

Guards each per-module code string against a minimal check: is it
non-empty, and is it not code_writers.py's own
"# CODE WRITER FAILED: ..." placeholder sneaking through as if it were
real code. Guardrails' JSON-schema tooling (Guard.for_pydantic / for_rail)
doesn't fit here -- sandbox_tester.py's input is a dict of already-parsed
Python strings, not raw JSON text from an LLM -- so this uses a custom
Validator via Guardrails' plugin API (register_validator) with
Guard.for_string instead.

Part 3 adds the second choke point: api/task_runner.py's _run_tier3_hires(),
right after `organize_final_answer()` returns `organized["answer"]` (~line
479) -- validates the merged final chat answer is well-formed text/markdown
before it's written to eo/chat_store.py / chat_workspace.py. Same
Guard.for_string + custom Validator shape as Part 2's ModuleCodeNonEmpty
above (the input here is also a single already-generated string, not raw
JSON text an LLM turn could reask into), and the same OnFailAction.NOOP +
caller-decides-the-fallback contract. What's checked:

  - non-empty (an organizer call that returned "" would otherwise silently
    blank out what was, pre-synthesis, a real per-role answer);
  - doesn't contain the literal "---DEDUP_NOTES---" marker
    agents/output_organizer.py's own DEDUP_NOTES_MARKER uses to separate the
    answer from its trailing JSON notes -- that string showing up inside
    `answer` means _parse_organizer_response()'s partition() on that marker
    didn't behave as expected and the notes payload leaked into user-facing
    text, which is the "leaked internal role-routing artifact" case Part 3's
    plan calls out;
  - has a balanced number of ``` fence markers -- an odd count means a
    fenced code/diagram block was left open, the cheap dependency-free
    stand-in for "no broken structure" (a full markdown parse isn't worth
    adding here for one structural check).

Part 4 adds the third and final choke point: eo/result_render.py's
collect_artifacts(), once per {"artifacts": [...]} entry a role attached to
its own output -- before that entry is added to the flat list handed to the
frontend's ArtifactRenderer.jsx. This is the one Part 1's plan called out as
mattering most, since html/svg artifacts are rendered straight into a
sandboxed iframe's srcDoc (see ArtifactRenderer.jsx's own security comment --
sandbox="allow-scripts" only, no allow-same-origin -- the guard here is
defense in depth on top of that, not a substitute for it).

Same Guard + custom Validator shape as Parts 2-3, except the value being
validated is a dict (one artifact entry), not a string -- so this uses the
generic `Guard().use(...)` constructor instead of `Guard.for_string(...)`.
collect_artifacts() already had a truthiness-only check before Part 4
(`if not entry.get("type") or not entry.get("code")`), which is what let a
non-string "code" (e.g. a role emitting a list or nested dict by mistake)
through as long as it was truthy -- ArtifactEntryWellFormed below replaces
that with a real type check on "type"/"code" (and "title", when present).
The plan's own wording says "type/content keys"; the actual field this
codebase's collect_artifacts()/ArtifactRenderer.jsx use is "code", not
"content" -- another Part-1-plan-vs-actual-code correction, same spirit as
Part 2's line-number/shape corrections above.

Place this file at: eo/output_guard.py
"""

import asyncio
import importlib.metadata

from guardrails import Guard
from guardrails.types import OnFailAction
from guardrails.validator_base import FailResult, PassResult, Validator, register_validator


def _ensure_event_loop() -> None:
    """Guardrails' validator_service dispatches through asyncio internally
    and calls asyncio.get_event_loop() to find one to run on. That's fine
    on the main thread (which gets an implicit loop), but every caller of
    validate_module_code() / validate_final_answer() / validate_artifact_entry()
    below runs on a worker thread instead: eo/executor.py's
    _run_concurrent_group() dispatches each role (hardware_speccer,
    code_writers, ...) onto its own concurrent.futures.ThreadPoolExecutor
    thread, and agents/code_writers.py's run() -- where validate_module_code()
    is actually called from, inside its as_completed() loop -- is itself
    running on one of those role-worker threads, not the process's main
    thread.

    A plain worker thread never has an event loop bound to it, so
    asyncio.get_event_loop() raises there (Python 3.10+: "There is no
    current event loop in thread ..."), guardrails' validator_service
    catches that and silently falls back to synchronous validation --
    which is where the repeated "Could not obtain an event loop. Falling
    back to synchronous validation." UserWarning in the logs comes from.
    The fallback is functionally harmless here (every validator above is
    plain sync code, OnFailAction.NOOP never reasks), but it's a warning
    fired once per module/answer/artifact for no real reason, and relying
    on an undocumented fallback path is fragile if guardrails ever changes
    that behavior.

    Binding a fresh event loop to the current thread before validate() is
    called removes the need for that fallback entirely, without changing
    any actual validation behavior or touching the caller's own threading
    model (executor.py / code_writers.py keep using
    concurrent.futures.ThreadPoolExecutor exactly as before -- this just
    gives whichever thread happens to call into this module something for
    asyncio.get_event_loop() to find)."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@register_validator(name="module-code-nonempty", data_type="string")
class ModuleCodeNonEmpty(Validator):
    """Fails on empty/whitespace-only code, or on code_writers.py's own
    "# CODE WRITER FAILED: ..." placeholder reaching here as if it were
    real code (which would only happen if a future refactor of
    _write_one_module() stopped checking `if not code:` before returning
    it -- this is a backstop for that, not the primary check)."""

    FAILURE_MARKER = "# CODE WRITER FAILED"

    def _validate(self, value, metadata):
        text = (value or "").strip()
        if not text:
            return FailResult(error_message="module code is empty")
        if text.startswith(self.FAILURE_MARKER):
            return FailResult(
                error_message="module code is the failure placeholder, not real code"
            )
        return PassResult()


_code_guard = None


def get_code_guard() -> Guard:
    """Lazily built, then reused across calls -- Guard construction has a
    small fixed cost (registering the validator, telemetry setup) not
    worth paying once per module in a 5-way parallel loop."""
    global _code_guard
    if _code_guard is None:
        _code_guard = Guard.for_string(
            validators=[ModuleCodeNonEmpty(on_fail=OnFailAction.NOOP)]
        )
    return _code_guard


def validate_module_code(code: str) -> tuple[bool, str]:
    """Used by agents/code_writers.py's run(), right before
    write(KEYS["submitted_code"], results). Returns (is_valid, reason) --
    reason is "" when is_valid is True.

    OnFailAction.NOOP means this never raises or reasks (there's no LLM
    turn left to reask into at this point in the pipeline); the caller
    decides what to do with a failing module. code_writers.py's existing
    fail-open pattern (swap in a "# CODE WRITER FAILED: ..." placeholder
    and keep going) is what Part 2 wires this into -- not a new pattern.
    """
    _ensure_event_loop()
    outcome = get_code_guard().validate(code)
    if outcome.validation_passed:
        return True, ""
    reasons = "; ".join(
        s.failure_reason for s in (outcome.validation_summaries or []) if s.failure_reason
    ) or "validation failed"
    return False, reasons


# NEW -- Part 3. Matches agents/output_organizer.py's own
# DEDUP_NOTES_MARKER constant. Hardcoded here rather than imported --
# output_guard.py stays a leaf module with no cross-imports into agents/,
# the same "mirror the string, don't import the module" approach
# ModuleCodeNonEmpty above takes with code_writers.py's own placeholder
# text.
_DEDUP_NOTES_MARKER = "---DEDUP_NOTES---"


@register_validator(name="final-answer-well-formed", data_type="string")
class FinalAnswerWellFormed(Validator):
    """Fails on an empty final answer, on the internal DEDUP_NOTES marker
    leaking into it (output_organizer's marker-partition not behaving as
    expected), or on an unbalanced number of ``` fence markers (a fenced
    code/diagram block left open)."""

    LEAKED_MARKER = _DEDUP_NOTES_MARKER

    def _validate(self, value, metadata):
        text = value or ""
        stripped = text.strip()
        if not stripped:
            return FailResult(error_message="final answer is empty")
        if self.LEAKED_MARKER in text:
            return FailResult(
                error_message=(
                    "final answer leaked the internal "
                    f"{self.LEAKED_MARKER!r} marker -- output_organizer's "
                    "marker-parsing likely failed"
                )
            )
        if text.count("```") % 2 != 0:
            return FailResult(
                error_message="final answer has an unbalanced number of ``` fence markers"
            )
        return PassResult()


_answer_guard = None


def get_answer_guard() -> Guard:
    """Lazily built, then reused across calls -- same reasoning as
    get_code_guard() above."""
    global _answer_guard
    if _answer_guard is None:
        _answer_guard = Guard.for_string(
            validators=[FinalAnswerWellFormed(on_fail=OnFailAction.NOOP)]
        )
    return _answer_guard


def validate_final_answer(answer: str) -> tuple[bool, str]:
    """Used by api/task_runner.py's _run_tier3_hires(), right after
    organize_final_answer() returns organized["answer"]. Returns
    (is_valid, reason) -- reason is "" when is_valid is True.

    OnFailAction.NOOP, same reasoning as validate_module_code() above:
    there's no LLM turn left to reask into once organize_final_answer()
    has already returned, so this never raises -- the caller decides what
    to do with a failing answer. task_runner.py's existing fail-open
    pattern (keep whatever `answer` was already set to before this call --
    the un-organized final_role text computed earlier in the function) is
    what Part 3 wires this into, rather than inventing a new fallback
    shape.
    """
    _ensure_event_loop()
    outcome = get_answer_guard().validate(answer)
    if outcome.validation_passed:
        return True, ""
    reasons = "; ".join(
        s.failure_reason for s in (outcome.validation_summaries or []) if s.failure_reason
    ) or "validation failed"
    return False, reasons


def get_guard():
    """
    Kept for backward compat with anything that imported the Part-1
    placeholder name -- superseded by get_code_guard() (Part 2),
    get_answer_guard() (Part 3), and get_artifact_guard() (Part 4) above/
    below, each scoped to its own choke point rather than one shared
    Guard trying to cover three unrelated shapes.
    """
    raise NotImplementedError(
        "output_guard.get_guard() was Part 1's placeholder and was never "
        "wired up -- use get_code_guard() / get_answer_guard() / "
        "get_artifact_guard() instead."
    )


# NEW -- Part 4. Mirrors ArtifactRenderer.jsx's TYPE_LABELS keys
# (html/svg/python/react) for documentation purposes only -- NOT enforced
# as an allow-list below. ArtifactRenderer.jsx already degrades an
# unrecognized "type" gracefully (falls back to a read-only source card,
# see its own final ternary branch), so rejecting a future/unimplemented
# type here would throw away a valid entry the frontend can already
# handle; the guard's job is catching malformed entries, not gatekeeping
# which types are "allowed".
_ARTIFACT_KNOWN_TYPES_FOR_REFERENCE = ("html", "svg", "python", "react")


@register_validator(name="artifact-entry-well-formed", data_type="object")
class ArtifactEntryWellFormed(Validator):
    """Fails when an artifact entry isn't a dict, is missing a non-empty
    string "type" or "code", or has a "title" that's present but not a
    string. "code" is the field ArtifactRenderer.jsx hands straight to an
    iframe's srcDoc for html/svg types (via wrapAsHtmlDoc()) or to
    Sandpack for react -- a non-string value there (list/dict/int a role
    emitted by mistake) is what this backstops against; the pre-Part-4
    `if not entry.get("code")` check in collect_artifacts() would have let
    any truthy non-string through."""

    def _validate(self, value, metadata):
        if not isinstance(value, dict):
            return FailResult(error_message="artifact entry is not an object")

        artifact_type = value.get("type")
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            return FailResult(error_message='artifact entry is missing a non-empty "type"')

        code = value.get("code")
        if not isinstance(code, str) or not code.strip():
            return FailResult(error_message='artifact entry is missing non-empty string "code"')

        title = value.get("title", "")
        if title is not None and not isinstance(title, str):
            return FailResult(error_message='artifact entry\'s "title" must be a string when present')

        return PassResult()


_artifact_guard = None


def get_artifact_guard() -> Guard:
    """Lazily built, then reused across calls -- same reasoning as
    get_code_guard() / get_answer_guard() above. Uses the generic
    Guard().use(...) constructor (not Guard.for_string()) since the value
    validated here is a dict, not a string."""
    global _artifact_guard
    if _artifact_guard is None:
        _artifact_guard = Guard().use(ArtifactEntryWellFormed(on_fail=OnFailAction.NOOP))
    return _artifact_guard


def validate_artifact_entry(entry) -> tuple[bool, str]:
    """Used by eo/result_render.py's collect_artifacts(), once per entry
    in a role's raw_output["artifacts"] list -- before that entry is
    appended to the flat list handed back to api/task_runner.py (and from
    there, to the frontend's ArtifactRenderer.jsx). Returns
    (is_valid, reason) -- reason is "" when is_valid is True.

    OnFailAction.NOOP, same reasoning as validate_module_code() /
    validate_final_answer() above: there's no LLM turn left to reask into
    once a role's raw_output already exists, so this never raises --
    collect_artifacts()'s existing "skip anything malformed rather than
    erroring the whole run over one entry from one role" behavior is what
    Part 4 wires this into; it just replaces the old truthiness-only
    check with a real shape check.
    """
    _ensure_event_loop()
    outcome = get_artifact_guard().validate(entry)
    if outcome.validation_passed:
        return True, ""
    reasons = "; ".join(
        s.failure_reason for s in (outcome.validation_summaries or []) if s.failure_reason
    ) or "validation failed"
    return False, reasons


if __name__ == "__main__":
    print(f"guardrails-ai version: {importlib.metadata.version('guardrails-ai')}")

    ok, reason = validate_module_code("def f():\n    return 1\n")
    print(f"valid code sample       -> passed={ok}")

    ok, reason = validate_module_code(
        "# CODE WRITER FAILED: model returned empty content."
    )
    print(f"failure-placeholder sample -> passed={ok} reason={reason!r}")

    ok, reason = validate_module_code("   ")
    print(f"empty sample             -> passed={ok} reason={reason!r}")

    ok, reason = validate_final_answer("## Summary\n\nAll good, here's a fenced block:\n\n```python\nprint('hi')\n```\n")
    print(f"valid final answer       -> passed={ok}")

    ok, reason = validate_final_answer(
        "Here's the answer.\n---DEDUP_NOTES---\n{\"reviewer\": \"folded into implementer\"}"
    )
    print(f"leaked-marker sample     -> passed={ok} reason={reason!r}")

    ok, reason = validate_final_answer("Unterminated code block:\n\n```python\nprint('hi')\n")
    print(f"unbalanced-fence sample  -> passed={ok} reason={reason!r}")

    ok, reason = validate_final_answer("")
    print(f"empty final answer       -> passed={ok} reason={reason!r}")

    ok, reason = validate_artifact_entry({"type": "html", "code": "<b>hi</b>", "title": "Demo"})
    print(f"valid artifact entry     -> passed={ok}")

    ok, reason = validate_artifact_entry({"type": "html", "code": ["not", "a", "string"]})
    print(f"non-string code sample   -> passed={ok} reason={reason!r}")

    ok, reason = validate_artifact_entry({"type": "", "code": "<b>hi</b>"})
    print(f"empty type sample        -> passed={ok} reason={reason!r}")

    ok, reason = validate_artifact_entry({"code": "<b>hi</b>"})
    print(f"missing type sample      -> passed={ok} reason={reason!r}")