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

Later parts still to come:
  - Part 3: api/task_runner.py, right after `organize_final_answer()` returns
    `organized["answer"]` -- validates the final chat answer is well-formed
    text/markdown before it's written to eo/chat_store.py / chat_workspace.py.
  - Part 4: eo/result_render.py's `collect_artifacts()` -- validates each
    {"artifacts": [...]} entry's shape before it's handed to the frontend's
    ArtifactRenderer.jsx.

Place this file at: eo/output_guard.py
"""

import importlib.metadata

from guardrails import Guard
from guardrails.types import OnFailAction
from guardrails.validator_base import FailResult, PassResult, Validator, register_validator


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
    outcome = get_code_guard().validate(code)
    if outcome.validation_passed:
        return True, ""
    reasons = "; ".join(
        s.failure_reason for s in (outcome.validation_summaries or []) if s.failure_reason
    ) or "validation failed"
    return False, reasons


def get_guard():
    """
    Placeholder for Parts 3-4's Guards (final chat answer text, CO2
    artifact payload shape). Not used by Part 2 -- see get_code_guard()
    above for that one.
    """
    raise NotImplementedError(
        "output_guard.get_guard() is not wired up yet -- Parts 3 and 4 "
        "will add their own Guard constructors here, following "
        "get_code_guard()'s pattern above."
    )


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