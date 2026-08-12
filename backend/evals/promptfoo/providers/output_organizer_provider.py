"""
evals/promptfoo/providers/output_organizer_provider.py — D2 patch 3.

output_organizer needs its OWN provider, separate from role_provider.py.
Traced agents/output_organizer.py directly rather than assuming it fit
role_provider.py's shape, and it doesn't, on two counts:

  1. Not Role-Library-driven at all. It has no ROLE_PROMPTS_SEED entry
     and no eo.registry.REGISTRY entry -- it's called directly from
     api/task_runner.py, outside the resolve()/generic_worker dispatch
     system entirely. Its SYSTEM_PROMPT and CHAIN are hardcoded module
     constants (own docstring: "Deliberately its own module rather than
     a generic_worker role"). get_role_prompt() has nothing to fetch for
     it -- role_provider.py's _reject_real_action_role() guard (patch
     2b) wouldn't even catch this case, since it only checks
     eo.registry.REGISTRY, and output_organizer isn't in there either.
  2. Different calling shape. Every role_provider.py case is
     (role_name, task_text) -> str. organize_final_answer() takes the
     WHOLE finished role_outputs tree (role_name -> raw_output) plus the
     original user_request, and returns a structured
     {"answer": str, "dedup_notes": dict}, not a bare string.

So rather than bend role_provider.py to fit a second, incompatible
shape, this is its own file with its own promptfoo config
(output_organizer.promptfooconfig.yaml) -- same "separate, not
shoehorned into the main config" pattern patch 5's provider-comparison
config already uses for a different reason.

WHY THIS DOESN'T ASSERT ON THE RAW ---DEDUP_NOTES--- MARKER DIRECTLY:
organize_final_answer() (imported and called as-is below, the exact
function api/task_runner.py calls in production) already fully
validates and parses that marker internally via
_parse_organizer_response() before returning -- the raw wire format
never leaves that function. Testing the marker's literal presence would
mean bypassing organize_final_answer() and duplicating its
prompt/section-construction logic here just to capture the model's raw
response before parsing -- a second copy of that logic that WILL drift
the moment output_organizer.py's own section-building changes. Testing
organize_final_answer()'s actual return contract (this file's approach)
covers the same ground -- a missing/malformed marker in the model's raw
output shows up here as an empty dedup_notes dict, exactly the fail-open
behavior _parse_organizer_response()'s own docstring describes as
correct, not as a separate bug this eval needs to catch. If you
specifically need to grade raw marker discipline (not just the parsed
result), that's a deliberate scope call to revisit, not an oversight.
"""
import json
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))  # .../backend

logger = logging.getLogger("promptfoo.output_organizer_provider")
logging.basicConfig(level=logging.INFO, format="[output_organizer_provider] %(levelname)s: %(message)s")


def _coerce_role_outputs(raw) -> dict:
    """Test vars come out of YAML as whatever promptfoo's loader gives
    us -- a native dict if the YAML wrote it as a mapping, or a JSON
    string if the case authored it as a block scalar. Accept either
    rather than forcing one authoring style on every case."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise TypeError(f"role_outputs must be a dict or a JSON string, got {type(raw).__name__}")


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """promptfoo entry point. Required vars: role_outputs (dict or JSON
    string, {role_name: raw_output}), user_request (str). Optional:
    final_role (str)."""
    test_vars = (context or {}).get("vars", {}) or {}

    if "role_outputs" not in test_vars:
        return {"error": "output_organizer_provider: test case is missing required var 'role_outputs'"}
    if "user_request" not in test_vars:
        return {"error": "output_organizer_provider: test case is missing required var 'user_request'"}

    try:
        role_outputs = _coerce_role_outputs(test_vars["role_outputs"])
    except (json.JSONDecodeError, TypeError) as exc:
        return {"error": f"output_organizer_provider: could not parse role_outputs var: {exc}"}

    user_request = test_vars["user_request"]
    final_role = test_vars.get("final_role")

    if len(role_outputs) < 2:
        logger.warning(
            "role_outputs has %d entr(y/ies); organize_final_answer() short-circuits "
            "below 2 (no LLM call, no dedup_notes) -- this case won't exercise the "
            "synthesis prompt at all. Confirm that's what you meant to test.",
            len(role_outputs))

    from agents.output_organizer import organize_final_answer

    try:
        result = organize_final_answer(
            role_outputs=role_outputs,
            user_request=user_request,
            final_role=final_role,
            # No session_id: this is a synthetic eval call, not a real
            # session -- same reasoning role_provider.py's call_api()
            # documents for why it mints its own id rather than passing
            # None. organize_final_answer()'s tracing span is a no-op
            # without one (see _open_organizer_span()'s own guard), so
            # omitting it here is the correct choice, not a gap: there's
            # no real session for a Langfuse trace to attach to.
        )
    except Exception as exc:
        return {"error": f"organize_final_answer() failed for {len(role_outputs)} role_outputs: "
                          f"{type(exc).__name__}: {exc}"}

    # Returned as a JSON string (not a Python dict) because promptfoo's
    # `output` contract is a string/serializable value it hands to
    # assertions verbatim -- tests/asserts/output_organizer_asserts.py's
    # helpers json.loads() this back apart.
    return {
        "output": json.dumps(result),
        "metadata": {
            "role_count": len(role_outputs),
            "dedup_note_count": len(result.get("dedup_notes", {})),
        },
    }
