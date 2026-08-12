"""
evals/promptfoo/tests/asserts/output_organizer_asserts.py — D2 patch 3.

Custom promptfoo Python assertions (https://www.promptfoo.dev/docs/
configuration/expected-outputs/python/) for output_organizer_provider.py's
output, which is organize_final_answer()'s real {"answer", "dedup_notes"}
return value, JSON-serialized. Each function here takes (output, context)
and returns True/False, a float score, or a dict with at least
{"pass": bool, "score": float, "reason": str}.
"""
import json


def _parse(output: str) -> dict:
    """Every assert below needs the same parse; a case whose provider
    call errored never reaches an assert at all (promptfoo short-
    circuits a case to failed on a provider `error` result), so `output`
    here is always the real JSON string from a successful call_api()
    return -- a JSONDecodeError here is a genuine test bug (wrong
    provider wired up, or output_organizer_provider.py's return shape
    changed), not a case to silently tolerate."""
    return json.loads(output)


def get_assert(output: str, context) -> dict:
    """Structural check: valid JSON, non-empty `answer` string, and a
    `dedup_notes` that's a dict (empty is fine -- that's the correct
    result for role_outputs with no genuine overlap). This is the
    baseline every case should include; the more specific checks below
    are opt-in per case via their own named functions."""
    try:
        parsed = _parse(output)
    except json.JSONDecodeError as exc:
        return {"pass": False, "score": 0.0, "reason": f"output is not valid JSON: {exc}"}

    if not isinstance(parsed.get("answer"), str) or not parsed["answer"].strip():
        return {"pass": False, "score": 0.0, "reason": "answer is missing, empty, or not a string"}
    if not isinstance(parsed.get("dedup_notes"), dict):
        return {"pass": False, "score": 0.0, "reason": "dedup_notes is missing or not an object"}

    return {"pass": True, "score": 1.0, "reason": "valid {answer, dedup_notes} shape"}


def code_blocks_preserved(output: str, context) -> dict:
    """Byte-for-byte fenced-code/Mermaid preservation check (Master
    Guide D2's explicit call-out for this case set). Reads
    `expected_verbatim_blocks` from the test's vars -- a list of exact
    strings (each usually one fenced ```...``` block, including the
    backticks) that must appear byte-for-byte, unmodified, somewhere in
    the merged answer. SYSTEM_PROMPT's own rule 5 ("Preserve any fenced
    code block or Mermaid diagram byte-for-byte") is what this is
    grading -- a model that reformats indentation, changes a fence's
    language tag, or "helpfully" reflows code inside the block should
    fail this."""
    try:
        parsed = _parse(output)
    except json.JSONDecodeError as exc:
        return {"pass": False, "score": 0.0, "reason": f"output is not valid JSON: {exc}"}

    answer = parsed.get("answer", "")
    expected_blocks = context["vars"].get("expected_verbatim_blocks", [])
    if not expected_blocks:
        return {"pass": False, "score": 0.0,
                "reason": "test case is missing 'expected_verbatim_blocks' var -- nothing to check"}

    missing = [block for block in expected_blocks if block not in answer]
    if missing:
        return {"pass": False, "score": 0.0,
                "reason": f"{len(missing)}/{len(expected_blocks)} expected block(s) not found "
                          f"byte-for-byte in the merged answer"}
    return {"pass": True, "score": 1.0, "reason": "all expected fenced blocks preserved byte-for-byte"}


def dedup_notes_cover_expected_roles(output: str, context) -> dict:
    """For a case deliberately constructed with near-duplicate content
    across roles (see tests/output_organizer.yaml's "restated point"
    fixtures): checks that dedup_notes has an entry for each role name
    listed in the test's `expected_dedup_roles` var. This is a coarser,
    deterministic complement to the llm-rubric check in the same test
    file -- it confirms the MECHANISM fired (something was flagged as
    folded) even before grading whether the fold was semantically
    correct."""
    try:
        parsed = _parse(output)
    except json.JSONDecodeError as exc:
        return {"pass": False, "score": 0.0, "reason": f"output is not valid JSON: {exc}"}

    dedup_notes = parsed.get("dedup_notes", {})
    expected_roles = context["vars"].get("expected_dedup_roles", [])
    if not expected_roles:
        return {"pass": False, "score": 0.0,
                "reason": "test case is missing 'expected_dedup_roles' var -- nothing to check"}

    missing = [role for role in expected_roles if role not in dedup_notes]
    if missing:
        return {"pass": False, "score": 0.0,
                "reason": f"expected dedup_notes entr(y/ies) for {missing}, got keys {list(dedup_notes.keys())}"}
    return {"pass": True, "score": 1.0, "reason": f"dedup_notes covers all expected roles {expected_roles}"}


def no_unexpected_dedup(output: str, context) -> dict:
    """Inverse check for a case built with genuinely distinct per-role
    content (no real overlap): dedup_notes should be empty. Catches the
    opposite failure mode from dedup_notes_cover_expected_roles -- a
    model that over-eagerly folds unrelated roles' content together and
    silently drops real, unique information (SYSTEM_PROMPT rule 3:
    "Never drop real content just to shorten the answer")."""
    try:
        parsed = _parse(output)
    except json.JSONDecodeError as exc:
        return {"pass": False, "score": 0.0, "reason": f"output is not valid JSON: {exc}"}

    dedup_notes = parsed.get("dedup_notes", {})
    if dedup_notes:
        return {"pass": False, "score": 0.0,
                "reason": f"expected no dedup folding, but got entries for {list(dedup_notes.keys())} "
                          f"-- check whether real, distinct content was dropped"}
    return {"pass": True, "score": 1.0, "reason": "no unexpected dedup folding"}
