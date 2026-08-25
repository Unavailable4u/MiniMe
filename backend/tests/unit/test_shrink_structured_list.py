"""
tests/unit/test_shrink_structured_list.py -- regression coverage for
Bug fix (2026-08-26): _shrink_prompt_for_retry()'s new
STRUCTURED_LIST_MARKER path in utils/llm_client.py.

Background: a further shrink beyond DROPPABLE_CONTEXT_MARKER's tail-drop
used to fall straight to a blind proportional/flat character cut, which
can land mid-object inside a caller's JSON array (e.g.
agents/hardware_speccer.py's finalized `parts` list, whose entries are
referenced by id elsewhere in wiring.edges). This suite tests the pure
shrink functions directly -- no network, no generate_text() -- confirming:
  - a real id is never left partially cut into an invalid string
  - the parsed list is always valid JSON before and after shrinking
  - narrative prefix text is trimmed before any list element is dropped
  - the fallback to blind slicing still works for callers/content that
    don't use the marker, or whose tail isn't parseable JSON
  - hardware_speccer.py's Call 2 prompt actually uses the marker
"""
import json

import pytest

from utils.llm_client import (
    DROPPABLE_CONTEXT_MARKER,
    STRUCTURED_LIST_MARKER,
    _shrink_prompt_for_retry,
    _shrink_structured_list_prompt,
    _target_length_for_shrink,
)


def _make_parts(n):
    return [
        {"id": f"part_{i:03d}", "name": f"Widget {i}", "dimensions_mm": [10, 10, 10]}
        for i in range(n)
    ]


class _LimitExc(Exception):
    """Stands in for the real 413 body shape parsed by
    _REQUEST_TOO_LARGE_LIMIT_PATTERN."""


def _limit_exc(limit, requested):
    return _LimitExc(f"Limit {limit}, Requested {requested}")


# ---------------------------------------------------------------------
# _shrink_structured_list_prompt() in isolation
# ---------------------------------------------------------------------

def test_structured_shrink_trims_prefix_before_touching_list():
    parts = _make_parts(20)
    prefix = "Task: design a widget. " * 40
    content = f"{prefix}{STRUCTURED_LIST_MARKER}{json.dumps(parts)}"
    # A modest overage -- small enough that trimming the prefix alone
    # should be able to reach the target without dropping any parts.
    exc = _limit_exc(limit=len(content) - 50, requested=len(content))

    shrunk = _shrink_structured_list_prompt(content, exc)

    assert shrunk is not None
    new_prefix, _, tail = shrunk.partition(STRUCTURED_LIST_MARKER)
    result_list = json.loads(tail)  # must still be valid, complete JSON
    assert result_list == parts  # no part dropped -- prefix trim was enough
    assert len(new_prefix) < len(prefix)  # but the prefix WAS actually trimmed
    assert len(shrunk) < len(content)


def test_structured_shrink_drops_whole_trailing_elements_only():
    parts = _make_parts(50)
    prefix = "Task: design a widget."
    content = f"{prefix}{STRUCTURED_LIST_MARKER}{json.dumps(parts)}"
    # A large overage that trimming the (already-short) prefix to its
    # floor cannot satisfy -- must fall through to dropping list
    # elements.
    exc = _limit_exc(limit=len(content) // 4, requested=len(content))

    shrunk = _shrink_structured_list_prompt(content, exc)

    assert shrunk is not None
    _, _, tail = shrunk.partition(STRUCTURED_LIST_MARKER)
    result_list = json.loads(tail)  # every remaining entry is complete, valid JSON
    assert len(result_list) < len(parts)
    # Every surviving entry is byte-for-byte one of the originals --
    # never a partial/mutated object.
    assert all(entry in parts for entry in result_list)
    # Elements are dropped from the END, not the start or middle.
    assert result_list == parts[: len(result_list)]
    assert len(shrunk) < len(content)


def test_structured_shrink_never_empties_the_list():
    parts = _make_parts(3)
    content = f"x{STRUCTURED_LIST_MARKER}{json.dumps(parts)}"
    # An impossibly small target -- even one bare element won't fit.
    exc = _limit_exc(limit=1, requested=len(content))

    shrunk = _shrink_structured_list_prompt(content, exc)

    assert shrunk is not None
    _, _, tail = shrunk.partition(STRUCTURED_LIST_MARKER)
    result_list = json.loads(tail)
    assert len(result_list) == 1  # stops at one element, never zero


def test_structured_shrink_returns_none_when_tail_is_not_json():
    content = f"prefix text{STRUCTURED_LIST_MARKER}not actually json"
    exc = _limit_exc(limit=5, requested=100)

    assert _shrink_structured_list_prompt(content, exc) is None


def test_structured_shrink_returns_none_when_list_is_empty():
    content = f"prefix text{STRUCTURED_LIST_MARKER}[]"
    exc = _limit_exc(limit=5, requested=100)

    assert _shrink_structured_list_prompt(content, exc) is None


def test_structured_shrink_returns_none_when_already_under_target():
    parts = _make_parts(2)
    content = f"short{STRUCTURED_LIST_MARKER}{json.dumps(parts)}"
    # Target length comes out larger than current content -- nothing to do.
    exc = _limit_exc(limit=len(content) * 10, requested=len(content))

    assert _shrink_structured_list_prompt(content, exc) is None


# ---------------------------------------------------------------------
# _shrink_prompt_for_retry() end-to-end dispatch/ordering
# ---------------------------------------------------------------------

def test_shrink_prompt_prefers_structured_path_over_blind_slice():
    parts = _make_parts(50)
    prefix = "Task: design a widget."
    content = f"{prefix}{STRUCTURED_LIST_MARKER}{json.dumps(parts)}"
    exc = _limit_exc(limit=len(content) // 4, requested=len(content))

    shrunk = _shrink_prompt_for_retry(content, exc)

    # Must still contain the marker and a parseable list -- proof the
    # structured path ran instead of a blind character slice (which
    # would almost certainly have cut mid-object and left the marker's
    # tail unparseable).
    assert STRUCTURED_LIST_MARKER in shrunk
    _, _, tail = shrunk.partition(STRUCTURED_LIST_MARKER)
    json.loads(tail)  # would raise if this were a blind mid-object cut


def test_shrink_prompt_drops_droppable_context_before_structured_marker_is_reached():
    """Mirrors hardware_speccer.py's real prompt shape: prefix, then the
    structured parts list, then an optional droppable hw_reference_context
    tail. The FIRST shrink must drop the droppable tail whole (not touch
    the parts list at all), leaving a clean, still-parseable structured
    prompt behind for any later shrink attempt on the same step."""
    parts = _make_parts(10)
    prefix = "Task: design a widget."
    hw_ref = "Precedent: a similar prior build used these components..."
    content = (
        f"{prefix}{STRUCTURED_LIST_MARKER}{json.dumps(parts)}"
        f"{DROPPABLE_CONTEXT_MARKER}{hw_ref}"
    )
    exc = _limit_exc(limit=len(content) - 10, requested=len(content))

    shrunk = _shrink_prompt_for_retry(content, exc)

    assert DROPPABLE_CONTEXT_MARKER not in shrunk
    assert hw_ref not in shrunk
    assert STRUCTURED_LIST_MARKER in shrunk
    _, _, tail = shrunk.partition(STRUCTURED_LIST_MARKER)
    assert json.loads(tail) == parts  # parts list untouched by this first shrink

    # A second shrink attempt on this same (now marker-clean) content
    # must fall through correctly to the structured path -- the earlier
    # bug this guards against is the two markers' relative order
    # breaking JSON parsing on a subsequent attempt.
    exc2 = _limit_exc(limit=len(shrunk) // 3, requested=len(shrunk))
    shrunk2 = _shrink_prompt_for_retry(shrunk, exc2)
    _, _, tail2 = shrunk2.partition(STRUCTURED_LIST_MARKER)
    json.loads(tail2)  # still valid JSON -- would raise on the old bug


def test_shrink_prompt_falls_back_to_blind_slice_without_marker():
    """No STRUCTURED_LIST_MARKER at all -- completely unchanged legacy
    behavior for every caller that doesn't opt in."""
    content = "relevant context. " * 500
    exc = _limit_exc(limit=100, requested=1000)

    shrunk = _shrink_prompt_for_retry(content, exc)

    target_len = _target_length_for_shrink(content, exc)
    assert shrunk == content[:target_len]


def test_shrink_prompt_falls_back_to_blind_slice_when_tail_unparseable():
    """Marker present but its tail isn't real JSON (caller bug, or
    content shape changed) -- must not raise, must still make forward
    progress via the ordinary blind-slice path."""
    content = f"prefix{STRUCTURED_LIST_MARKER}not json at all, just text"
    exc = _limit_exc(limit=10, requested=len(content))

    shrunk = _shrink_prompt_for_retry(content, exc)

    assert len(shrunk) < len(content)


# ---------------------------------------------------------------------
# hardware_speccer.py actually uses the marker
# ---------------------------------------------------------------------

def test_hardware_speccer_wiring_prompt_uses_structured_list_marker():
    import inspect

    from agents import hardware_speccer

    source = inspect.getsource(hardware_speccer)
    assert "STRUCTURED_LIST_MARKER" in source
    # Confirm it's actually applied to the parts list specifically, not
    # just imported and unused.
    assert "wiring_user_prompt" in source
    idx = source.index("wiring_user_prompt = (")
    snippet = source[idx: idx + 600]
    assert "STRUCTURED_LIST_MARKER" in snippet
    assert "json.dumps(parts)" in snippet
