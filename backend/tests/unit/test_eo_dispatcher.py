"""
tests/unit/test_eo_dispatcher.py — Patch 7e-S2.

eo/dispatcher.py had zero test coverage before this. next_step() is the
deterministic routing engine that decides, after each role finishes,
which role_plan index runs next -- plain "advance one" (no
next_destination), a "recheck" (revisit to an already-run role earlier
in the plan), or an "escalate" (a genuinely new role, either already
present later in role_plan or appended on the fly). It also enforces
the revisit cap (MAX_STAGE_REVISITS = 3) and rejects hallucinated
escalation targets that aren't in known_roles.

Style/isolation notes:
  - memory.bus.read/write need no mocking: tests/conftest.py's autouse
    fake_bus fixture already swaps memory.bus.redis for an in-memory
    FakeRedis before every test, and dispatcher.py's `from memory.bus
    import write, read` resolves those names at call time same as any
    other caller -- so visit counts and route_trace really round-trip
    through the fake bus exactly like production.
  - relay.emitter.emit_event is bound into eo.dispatcher's OWN
    namespace at module top level (`from relay.emitter import
    emit_event`), so per conftest.py's documented gotcha, it's patched
    as eo.dispatcher.emit_event, not relay.emitter.emit_event.
  - Every test picks its own role_plan/idx by hand rather than
    reaching for a shared fixture -- next_step()'s behavior is entirely
    a function of its four arguments plus bus state, so per-test
    literals are clearer than indirection through a fixture here.
"""
from unittest.mock import MagicMock

import pytest

from eo import dispatcher
from eo.dispatcher import MAX_STAGE_REVISITS, next_step
from memory.bus import read as bus_read


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(dispatcher, "emit_event", mock)
    return mock


# ---------------------------------------------------------------------------
# No next_destination -- plain advance
# ---------------------------------------------------------------------------

def test_no_named_destination_advances_by_one():
    role_plan = ["idea_planner", "prompt_writer", "reviewer"]
    idx, reason = next_step({}, role_plan, idx=0, session_id="s1")
    assert (idx, reason) == (1, "plan")


def test_no_next_destination_key_at_all_advances():
    """agent_result with no 'next_destination' key behaves the same as an
    empty dict -- .get() returns None either way."""
    role_plan = ["idea_planner", "prompt_writer"]
    idx, reason = next_step({"text": "done"}, role_plan, idx=0, session_id="s1")
    assert (idx, reason) == (1, "plan")


def test_agent_result_not_a_dict_treated_as_no_destination():
    """isinstance guard: a non-dict agent_result (e.g. a bare string some
    caller passed by mistake) must not raise -- it's treated exactly
    like "no next_destination"."""
    role_plan = ["idea_planner", "prompt_writer"]
    idx, reason = next_step("not a dict", role_plan, idx=0, session_id="s1")
    assert (idx, reason) == (1, "plan")


def test_advance_past_end_of_plan_returns_none():
    role_plan = ["idea_planner", "prompt_writer"]
    idx, reason = next_step({}, role_plan, idx=1, session_id="s1")
    assert (idx, reason) == (None, "plan")


def test_plan_advance_logs_route_trace(mock_emit):
    role_plan = ["a", "b", "c"]
    next_step({}, role_plan, idx=0, session_id="sess-plan")
    trace = bus_read("route_trace:sess-plan", default=[])
    assert trace == [{"destination": "b", "reason": "plan"}]
    mock_emit.assert_called_once()
    assert mock_emit.call_args.kwargs["payload"] == {"destination": "b", "reason": "plan"}


def test_plan_advance_past_end_does_not_log_route_trace():
    """_log_route() no-ops when destination is None (nothing meaningful
    to show in the Routing Trace card)."""
    role_plan = ["a"]
    next_step({}, role_plan, idx=0, session_id="sess-end")
    assert bus_read("route_trace:sess-end", default=[]) == []


def test_no_session_id_skips_route_trace_write():
    role_plan = ["a", "b"]
    idx, reason = next_step({}, role_plan, idx=0, session_id=None)
    assert (idx, reason) == (1, "plan")
    # No session_id means no key to have been written under -- nothing
    # to assert against bus_read directly, but _visit_count/_record_visit
    # (called on the recheck/escalate paths, not this one) must also not
    # explode with session_id=None; covered by test_recheck below.


# ---------------------------------------------------------------------------
# Recheck -- named destination already ran, at or before idx
# ---------------------------------------------------------------------------

def test_recheck_finds_earlier_role():
    role_plan = ["idea_planner", "prompt_writer", "reviewer"]
    result = {"next_destination": "idea_planner"}
    idx, reason = next_step(result, role_plan, idx=2, session_id="s2")
    assert (idx, reason) == (0, "recheck")


def test_recheck_at_current_idx_itself():
    """named in role_plan[:idx+1] includes idx itself -- a role can
    "recheck" its own just-finished slot (e.g. self-correction loop)."""
    role_plan = ["a", "b", "c"]
    result = {"next_destination": "b"}
    idx, reason = next_step(result, role_plan, idx=1, session_id="s2")
    assert (idx, reason) == (1, "recheck")


def test_recheck_picks_latest_occurrence_when_role_appears_twice():
    """max(i for ... if role_plan[i] == named) -- if a role name occurs
    more than once at-or-before idx (e.g. after a prior escalate
    appended a duplicate), recheck targets the LATEST occurrence, not
    the first."""
    role_plan = ["reviewer", "fixer", "reviewer", "sandbox_tester"]
    result = {"next_destination": "reviewer"}
    idx, reason = next_step(result, role_plan, idx=2, session_id="s2")
    assert (idx, reason) == (2, "recheck")


def test_recheck_without_session_id_does_not_raise():
    role_plan = ["a", "b"]
    result = {"next_destination": "a"}
    idx, reason = next_step(result, role_plan, idx=1, session_id=None)
    assert (idx, reason) == (0, "recheck")


# ---------------------------------------------------------------------------
# Escalate -- named destination exists later in the plan (not yet run)
# ---------------------------------------------------------------------------

def test_escalate_to_role_later_in_plan():
    role_plan = ["idea_planner", "prompt_writer", "reviewer"]
    result = {"next_destination": "reviewer"}
    idx, reason = next_step(result, role_plan, idx=0, session_id="s3")
    assert (idx, reason) == (2, "escalate")


def test_escalate_uses_first_occurrence_after_idx_not_a_later_one():
    """role_plan.index(named, idx + 1) -- when the named role appears
    MORE THAN ONCE after idx, escalate targets the FIRST of those
    occurrences, not a later duplicate."""
    role_plan = ["a", "reviewer", "b", "reviewer"]
    result = {"next_destination": "reviewer"}
    idx, reason = next_step(result, role_plan, idx=0, session_id="s3")
    assert (idx, reason) == (1, "escalate")


# ---------------------------------------------------------------------------
# Escalate -- genuinely new role, not anywhere in role_plan
# ---------------------------------------------------------------------------

def test_escalate_appends_new_known_role():
    role_plan = ["idea_planner", "prompt_writer"]
    result = {"next_destination": "security_reviewer"}
    idx, reason = next_step(result, role_plan, idx=1, session_id="s4",
                             known_roles={"idea_planner", "prompt_writer", "security_reviewer"})
    assert (idx, reason) == (2, "escalate")
    assert role_plan == ["idea_planner", "prompt_writer", "security_reviewer"]


def test_escalate_new_role_mutates_role_plan_in_place():
    """Documented contract: role_plan may be mutated in place (appended
    to) -- callers rely on the SAME list object growing, not a new one
    being returned."""
    role_plan = ["a"]
    original_list_id = id(role_plan)
    next_step({"next_destination": "z"}, role_plan, idx=0, session_id="s4",
              known_roles={"a", "z"})
    assert id(role_plan) == original_list_id
    assert role_plan == ["a", "z"]


def test_unknown_role_with_known_roles_none_still_escalates():
    """known_roles=None means no rejection is applied at all (back-compat
    for callers that don't pass it yet) -- a brand-new name is appended
    exactly like a known one would be."""
    role_plan = ["a"]
    idx, reason = next_step({"next_destination": "totally_new"}, role_plan, idx=0,
                             session_id="s4", known_roles=None)
    assert (idx, reason) == (1, "escalate")
    assert role_plan == ["a", "totally_new"]


def test_hallucinated_role_rejected_when_not_in_known_roles(mock_emit):
    role_plan = ["idea_planner", "prompt_writer"]
    idx, reason = next_step({"next_destination": "made_up_role"}, role_plan, idx=0,
                             session_id="s5", known_roles={"idea_planner", "prompt_writer"})
    # Rejected -> falls back to plain advance (idx+1), plan unchanged.
    assert (idx, reason) == (1, "plan")
    assert role_plan == ["idea_planner", "prompt_writer"]
    events = [c.args[0] for c in mock_emit.call_args_list]
    assert "hallucinated_role_rejected" in events


def test_hallucinated_role_rejection_payload(mock_emit):
    role_plan = ["a", "b"]
    next_step({"next_destination": "ghost"}, role_plan, idx=0, session_id="s5",
              known_roles={"a", "b"})
    reject_call = next(c for c in mock_emit.call_args_list
                        if c.args[0] == "hallucinated_role_rejected")
    assert reject_call.kwargs["payload"] == {"attempted_role": "ghost"}


def test_hallucinated_role_rejected_at_end_of_plan_returns_none():
    role_plan = ["a"]
    idx, reason = next_step({"next_destination": "ghost"}, role_plan, idx=0,
                             session_id="s5", known_roles={"a"})
    assert (idx, reason) == (None, "plan")


# ---------------------------------------------------------------------------
# Revisit cap -- MAX_STAGE_REVISITS
# ---------------------------------------------------------------------------

def test_revisit_under_cap_succeeds_and_increments_count():
    role_plan = ["a", "b", "a"]
    for _ in range(MAX_STAGE_REVISITS):
        idx, reason = next_step({"next_destination": "a"}, role_plan, idx=1, session_id="s6")
        assert reason == "recheck"
    from eo.dispatcher import _visit_count
    assert _visit_count("s6", "a") == MAX_STAGE_REVISITS


def test_revisit_at_cap_is_blocked_and_falls_back_to_plan(mock_emit):
    role_plan = ["a", "b", "a"]
    # Drive the visit count up to the cap first.
    for _ in range(MAX_STAGE_REVISITS):
        next_step({"next_destination": "a"}, role_plan, idx=1, session_id="s7")

    # One more attempt at the same role must now be blocked.
    idx, reason = next_step({"next_destination": "a"}, role_plan, idx=1, session_id="s7")
    assert (idx, reason) == (2, "plan")
    events = [c.args[0] for c in mock_emit.call_args_list]
    assert "revisit_cap_reached" in events


def test_revisit_cap_reached_payload(mock_emit):
    role_plan = ["x", "y", "x"]
    for _ in range(MAX_STAGE_REVISITS):
        next_step({"next_destination": "x"}, role_plan, idx=1, session_id="s8")
    next_step({"next_destination": "x"}, role_plan, idx=1, session_id="s8")
    cap_call = next(c for c in mock_emit.call_args_list
                     if c.args[0] == "revisit_cap_reached")
    assert cap_call.kwargs["payload"] == {"stage": "x", "cap": MAX_STAGE_REVISITS}


def test_revisit_cap_does_not_record_the_blocked_attempt():
    """The cap-exceeded branch returns early (before _record_visit()), so
    the visit count doesn't keep climbing past the cap on repeated
    blocked attempts."""
    from eo.dispatcher import _visit_count
    role_plan = ["a", "b", "a"]
    for _ in range(MAX_STAGE_REVISITS + 5):
        next_step({"next_destination": "a"}, role_plan, idx=1, session_id="s9")
    assert _visit_count("s9", "a") == MAX_STAGE_REVISITS


def test_revisit_cap_without_session_id_does_not_raise():
    """_visit_count()/_record_visit() both short-circuit to 0/no-op when
    session_id is falsy, so the cap check never even fires without one."""
    role_plan = ["a", "b", "a"]
    for _ in range(MAX_STAGE_REVISITS + 2):
        idx, reason = next_step({"next_destination": "a"}, role_plan, idx=1, session_id=None)
        assert reason == "recheck"


def test_visit_count_is_scoped_per_session():
    """Two different session_ids revisiting the same role name must not
    share a visit count -- each session gets its own budget."""
    from eo.dispatcher import _visit_count
    role_plan = ["a", "b", "a"]
    for _ in range(MAX_STAGE_REVISITS):
        next_step({"next_destination": "a"}, role_plan, idx=1, session_id="session-A")
    # A fresh session, same role name, same role_plan shape -- should
    # still be allowed since its own count starts at 0.
    idx, reason = next_step({"next_destination": "a"}, role_plan, idx=1, session_id="session-B")
    assert reason == "recheck"
    assert _visit_count("session-A", "a") == MAX_STAGE_REVISITS
    assert _visit_count("session-B", "a") == 1
