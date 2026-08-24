"""
tests/unit/test_eo_executor.py — sub-patch 7e-S1 of the Structural
test-coverage group (audit patch 7e). eo/executor.py had zero tests
despite being the module that actually runs every execution graph
eo/router.py and eo/panel.py build — every dispatch decision,
MissingDependencyError self-heal, ChainExhaustedError degrade, and
human-in-the-loop pause/resume checkpoint lives here.

Scope of this file:
  - The small pure helpers (_flatten_role_names, _merge_group_next_
    destinations, _extract_image, _apply_recheck_retry) get direct,
    exhaustive unit tests.
  - _run_loop() (the actual step-dispatch loop, factored out of
    execute_graph() so resume_graph() can re-enter it) is exercised
    directly with a hand-written, deterministic `next_step` stand-in
    instead of the real eo/dispatcher.py -- this file is about
    executor.py's OWN behavior (dispatch-branch call shapes, error
    handling, pause bookkeeping, agent_names/role_names growth), not
    the Dispatcher's routing decisions, which already have their own
    coverage in test_eo_router.py / test_eo_dispatcher-shaped files.
  - execute_graph() and resume_graph() each get a thin wiring test
    confirming they call into _run_loop() (and, for resume_graph(),
    into the real snapshot read/decision-branch logic) correctly --
    not a re-test of every _run_loop() branch already covered above.

_run_concurrent_group() (the ThreadPoolExecutor-based parallel-group
path) and the Langfuse tracing context managers are deliberately left
out of this sub-patch -- both need substantially heavier mocking
(threading + OTEL context, and a real/fake Langfuse client
respectively) than the sequential dispatch loop this file focuses on,
and neither fires on an ordinary (non-template, tracing-off) run.

Every test below explicitly sets TRACING_ENABLED-independent behavior
by simply never configuring Langfuse env vars -- tests/conftest.py's
fake_bus fixture (autouse) already isolates memory.bus, and
TRACING_ENABLED itself defaults to False with no LANGFUSE_* env vars
set (see eo/tracing.py), so the tracing context managers here are
already no-ops without needing an explicit patch.
"""
import pytest

from eo import executor
from eo.errors import MissingDependencyError
from utils.llm_client import ChainExhaustedError


def _sequential_next_step(result, role_names, idx, session_id=None, known_roles=None):
    """Deterministic dispatcher stand-in: always advances one position,
    ignoring the result's content entirely. Good enough for every test
    here that isn't specifically about escalation/pausing, where we
    only care that _run_loop() dispatches the right roles in the right
    order and bookkeeps results correctly -- not about what a REAL
    next_destination-parsing Dispatcher would have decided."""
    next_idx = idx + 1
    return (next_idx if next_idx < len(role_names) else None, "plan")


@pytest.fixture(autouse=True)
def _quiet_emit(monkeypatch):
    """Every test in this file cares about dispatch/bookkeeping
    behavior, not about the Pusher event stream -- replace emit_event
    with a recorder so nothing here depends on real Pusher config, and
    individual tests can still assert on what got emitted via the
    returned list."""
    events = []
    monkeypatch.setattr(executor, "emit_event",
                          lambda event_type, **kwargs: events.append((event_type, kwargs)))
    return events


# ---------------------------------------------------------------------------
# _flatten_role_names
# ---------------------------------------------------------------------------

class TestFlattenRoleNames:
    def test_flat_list_of_strings_untouched(self):
        assert executor._flatten_role_names(["a", "b", "c"]) == {"a", "b", "c"}

    def test_nested_group_is_flattened(self):
        assert executor._flatten_role_names(["a", ["b", "c"], "d"]) == {"a", "b", "c", "d"}

    def test_all_entries_are_groups(self):
        assert executor._flatten_role_names([["a", "b"], ["c"]]) == {"a", "b", "c"}

    def test_empty_list_returns_empty_set(self):
        assert executor._flatten_role_names([]) == set()

    def test_duplicate_roles_collapse_in_the_set(self):
        assert executor._flatten_role_names(["a", ["a", "b"]]) == {"a", "b"}


# ---------------------------------------------------------------------------
# _merge_group_next_destinations
# ---------------------------------------------------------------------------

class TestMergeGroupNextDestinations:
    def test_majority_vote_wins(self):
        assert executor._merge_group_next_destinations(["x", "y", "x"]) == "x"

    def test_tie_first_non_none_vote_by_order_wins(self):
        # "y" and "x" are tied 1-1; "y" appears first in member order.
        assert executor._merge_group_next_destinations(["y", "x"]) == "y"

    def test_all_none_votes_return_none(self):
        assert executor._merge_group_next_destinations([None, None]) is None

    def test_empty_votes_return_none(self):
        assert executor._merge_group_next_destinations([]) is None

    def test_none_votes_mixed_with_real_votes_are_ignored(self):
        assert executor._merge_group_next_destinations([None, "z", None]) == "z"


# ---------------------------------------------------------------------------
# _extract_image
# ---------------------------------------------------------------------------

class TestExtractImage:
    def test_valid_small_data_uri_is_returned(self):
        result = {"image": "data:image/png;base64,abc123"}
        assert executor._extract_image(result) == "data:image/png;base64,abc123"

    def test_non_dict_result_returns_none(self):
        assert executor._extract_image("just some text") is None

    def test_dict_without_image_key_returns_none(self):
        assert executor._extract_image({"text": "hello"}) is None

    def test_non_string_image_value_returns_none(self):
        assert executor._extract_image({"image": 12345}) is None

    def test_empty_string_image_returns_none(self):
        assert executor._extract_image({"image": ""}) is None

    def test_oversized_image_returns_none(self):
        oversized = "x" * (executor.MAX_IMAGE_DATA_URI_CHARS + 1)
        assert executor._extract_image({"image": oversized}) is None

    def test_image_exactly_at_limit_is_kept(self):
        exact = "x" * executor.MAX_IMAGE_DATA_URI_CHARS
        assert executor._extract_image({"image": exact}) == exact


# ---------------------------------------------------------------------------
# _apply_recheck_retry
# ---------------------------------------------------------------------------

class TestApplyRecheckRetry:
    def test_noop_when_reason_is_not_recheck(self, monkeypatch):
        import eo.panel as panel_module
        calls = []
        monkeypatch.setattr(panel_module, "_best_match", lambda *a, **k: calls.append(1))
        key_overrides = {"reviewer": "acct_a"}
        executor._apply_recheck_retry(key_overrides, ["reviewer"], 0, "plan")
        assert calls == []
        assert key_overrides == {"reviewer": "acct_a"}

    def test_noop_when_next_idx_is_none(self, monkeypatch):
        import eo.panel as panel_module
        calls = []
        monkeypatch.setattr(panel_module, "_best_match", lambda *a, **k: calls.append(1))
        executor._apply_recheck_retry({}, ["reviewer"], None, "recheck")
        assert calls == []

    def test_recheck_picks_a_new_key_excluding_the_last_one(self, monkeypatch):
        import eo.panel as panel_module
        import eo.quota_sentinel as quota_module
        monkeypatch.setattr(quota_module, "get_quota_snapshot", lambda: {"snapshot": True})
        seen = {}

        def fake_best_match(role, snapshot, exclude=None):
            seen["role"], seen["snapshot"], seen["exclude"] = role, snapshot, exclude
            return "acct_b"

        monkeypatch.setattr(panel_module, "_best_match", fake_best_match)
        key_overrides = {"reviewer": "acct_a"}
        executor._apply_recheck_retry(key_overrides, ["reviewer"], 0, "recheck")

        assert key_overrides["reviewer"] == "acct_b"
        assert seen["role"] == "reviewer"
        assert seen["snapshot"] == {"snapshot": True}
        assert seen["exclude"] == {"acct_a"}

    def test_recheck_with_no_prior_override_excludes_nothing(self, monkeypatch):
        import eo.panel as panel_module
        import eo.quota_sentinel as quota_module
        monkeypatch.setattr(quota_module, "get_quota_snapshot", dict)
        seen = {}

        def fake_best_match(role, snapshot, exclude=None):
            seen["exclude"] = exclude
            return "acct_x"

        monkeypatch.setattr(panel_module, "_best_match", fake_best_match)
        key_overrides = {}
        executor._apply_recheck_retry(key_overrides, ["reviewer"], 0, "recheck")

        assert seen["exclude"] is None
        assert key_overrides["reviewer"] == "acct_x"

    def test_recheck_with_no_match_found_leaves_override_untouched(self, monkeypatch):
        import eo.panel as panel_module
        import eo.quota_sentinel as quota_module
        monkeypatch.setattr(quota_module, "get_quota_snapshot", dict)
        monkeypatch.setattr(panel_module, "_best_match", lambda *a, **k: None)
        key_overrides = {"reviewer": "acct_a"}
        executor._apply_recheck_retry(key_overrides, ["reviewer"], 0, "recheck")
        assert key_overrides["reviewer"] == "acct_a"


# ---------------------------------------------------------------------------
# _run_loop -- the sequential dispatch loop, exercised directly
# ---------------------------------------------------------------------------

def _base_run_loop_kwargs(**overrides):
    kwargs = dict(
        agent_names=["generic_worker"], role_names=["role_a"], idx=0, results={},
        auto_inserted={}, stage_revisits={}, task_text="do the thing",
        session_id="sess-1", path="adaptive", mode=None, key_overrides={},
        project_unique_name=None, expanded=False, approval_roles=set(),
        next_step=_sequential_next_step, no_conversation_context_roles=set(),
        domain=None, scope=None, workspace_id=None,
    )
    kwargs.update(overrides)
    return kwargs


class TestRunLoopHappyPath:
    def test_two_step_linear_plan_completes_and_returns_all_results(self, monkeypatch):
        monkeypatch.setattr(executor, "resolve", lambda name: (
            lambda **kw: {"text": f"output-for-{kw['role']}"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["role_a", "role_b"],
        ))

        assert results == {
            "role_a": {"text": "output-for-role_a"},
            "role_b": {"text": "output-for-role_b"},
        }

    def test_generic_worker_receives_earlier_roles_as_input_keys(self, monkeypatch):
        seen_input_keys = {}

        def fake_generic_worker(**kw):
            seen_input_keys[kw["role"]] = kw["input_keys"]
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["role_a", "role_b"],
        ))

        assert seen_input_keys["role_a"] == []
        assert seen_input_keys["role_b"] == ["role_a"]

    def test_no_conversation_context_roles_is_forwarded_correctly(self, monkeypatch):
        seen_flags = {}

        def fake_generic_worker(**kw):
            seen_flags[kw["role"]] = kw["include_conversation_context"]
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["role_a", "role_b"],
            no_conversation_context_roles={"role_b"},
        ))

        assert seen_flags == {"role_a": True, "role_b": False}

    def test_agent_start_and_agent_done_events_fire_per_step(self, monkeypatch, _quiet_emit):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs())

        event_types = [e[0] for e in _quiet_emit]
        assert event_types == ["agent_start", "agent_done"]


class TestRunLoopEscalation:
    def test_next_step_escalating_to_a_new_role_grows_agent_names_in_lockstep(self, monkeypatch):
        calls = []

        def fake_generic_worker(**kw):
            calls.append(kw["role"])
            if kw["role"] == "role_a":
                return {"text": "ok", "next_destination": "role_b"}
            return {"text": "ok"}

        def escalating_next_step(result, role_names, idx, session_id=None, known_roles=None):
            dest = result.get("next_destination") if isinstance(result, dict) else None
            if dest:
                role_names.append(dest)
                return (len(role_names) - 1, "escalate")
            return (None, "plan")

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        agent_names = ["generic_worker"]
        role_names = ["role_a"]
        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=agent_names, role_names=role_names,
            next_step=escalating_next_step,
        ))

        assert calls == ["role_a", "role_b"]
        assert results.keys() == {"role_a", "role_b"}
        # agent_names must have grown to match the escalated role_names,
        # or the next loop iteration would index past its own end.
        assert agent_names == ["generic_worker", "generic_worker"]
        assert role_names == ["role_a", "role_b"]


class TestRunLoopMissingDependencyError:
    def test_adaptive_path_self_heals_by_inserting_the_prerequisite(self, monkeypatch):
        call_log = []

        def fake_generic_worker(**kw):
            role = kw["role"]
            call_log.append(role)
            if role == "main" and call_log.count("main") == 1:
                raise MissingDependencyError("prereq")
            return {"text": f"done-{role}"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["main"], path="adaptive",
        ))

        # prereq ran before main, main ran (and succeeded) after.
        assert call_log == ["main", "prereq", "main"]
        assert results["prereq"] == {"text": "done-prereq"}
        assert results["main"] == {"text": "done-main"}

    def test_non_adaptive_path_re_raises_instead_of_self_healing(self, monkeypatch, _quiet_emit):
        def fake_generic_worker(**kw):
            raise MissingDependencyError("prereq")

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        with pytest.raises(MissingDependencyError):
            executor._run_loop(**_base_run_loop_kwargs(
                agent_names=["generic_worker"], role_names=["main"], path="fixed",
            ))

        assert any(e[0] == "error" for e in _quiet_emit)

    def test_over_budget_re_raises_even_on_adaptive_path(self, monkeypatch):
        def fake_generic_worker(**kw):
            raise MissingDependencyError("prereq")

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        auto_inserted = {("main", "prereq"): executor.MAX_AUTO_INSERTS_PER_STEP}
        with pytest.raises(MissingDependencyError):
            executor._run_loop(**_base_run_loop_kwargs(
                agent_names=["generic_worker"], role_names=["main"], path="adaptive",
                auto_inserted=auto_inserted,
            ))

    def test_when_prerequisite_already_failed_role_is_skipped_not_re_raised(self, monkeypatch):
        def fake_generic_worker(**kw):
            if kw["role"] == "main":
                raise MissingDependencyError("prereq")
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        # "prereq" already ran (idx 0) and is recorded as failed -- "main"
        # (idx 1) should be skipped rather than re-attempting the insert.
        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["prereq", "main"], idx=1, path="adaptive",
            results={"prereq": {"status": "failed", "role": "prereq", "reason": "boom"}},
        ))

        assert results["main"] == {
            "status": "failed", "role": "main",
            "reason": "prerequisite 'prereq' failed",
        }


class TestRunLoopChainExhaustedError:
    def test_role_is_recorded_as_failed_and_run_continues(self, monkeypatch):
        def fake_generic_worker(**kw):
            if kw["role"] == "role_a":
                raise ChainExhaustedError("every provider failed")
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["role_a", "role_b"],
        ))

        assert results["role_a"] == {
            "status": "failed", "role": "role_a", "reason": "every provider failed",
        }
        assert results["role_b"] == {"text": "ok"}

    def test_degraded_role_does_not_emit_agent_done_or_pause(self, monkeypatch, _quiet_emit):
        def fake_generic_worker(**kw):
            raise ChainExhaustedError("boom")

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["role_a"],
            approval_roles={"role_a"},
        ))

        event_types = [e[0] for e in _quiet_emit]
        assert "agent_done" not in event_types
        assert "awaiting_approval" not in event_types
        assert any(t == "error" for t in event_types)


class TestRunLoopPause:
    def test_approval_role_pauses_before_calling_next_step(self, monkeypatch, _quiet_emit):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        next_step_calls = []

        def spy_next_step(*a, **k):
            next_step_calls.append((a, k))
            return (None, "plan")

        result = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["role_a", "role_b"],
            approval_roles={"role_a"},
            next_step=spy_next_step,
        ))

        assert result == {"status": "paused", "paused_at_role": "role_a"}
        assert next_step_calls == []  # dispatcher never consulted once paused
        assert any(e[0] == "awaiting_approval" for e in _quiet_emit)

    def test_pause_snapshot_is_persisted_on_the_bus(self, monkeypatch, fake_bus):
        from memory.bus import read as bus_read

        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["role_a"],
            approval_roles={"role_a"}, session_id="sess-pause",
        ))

        snapshot = bus_read("paused_execution:sess-pause")
        assert snapshot["idx"] == 0
        assert snapshot["role_names"] == ["role_a"]
        assert snapshot["path"] == "adaptive"

    def test_manual_pause_request_flag_pauses_and_is_consumed(self, monkeypatch, fake_bus):
        from memory.bus import read as bus_read
        from memory.bus import write as bus_write

        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        bus_write("pause_requested:sess-manual", True)

        result = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["role_a"],
            approval_roles=set(), session_id="sess-manual",
        ))

        assert result == {"status": "paused", "paused_at_role": "role_a"}
        assert bus_read("pause_requested:sess-manual", default=None) is None

    def test_non_approval_role_does_not_pause(self, monkeypatch):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        result = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["role_a"],
            approval_roles={"some_other_role"},
        ))

        assert result == {"role_a": {"text": "ok"}}


class TestRunLoopGroupApprovalBackstop:
    def test_group_with_approval_role_member_degrades_to_sequential(self, monkeypatch):
        """Step 5's belt-and-braces backstop: a concurrent group
        (role_names[idx] is a list) containing an approval_roles member
        must never actually be dispatched as a group -- it's spliced
        back into ordinary sequential slots so the single-role pause
        checkpoint (already tested above) covers it."""
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        result = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=[["role_a", "role_b"]],
            approval_roles={"role_a"},
        ))

        # Degraded to sequential: role_a is dispatched alone, and pauses
        # before role_b ever runs -- exactly the single-role pause path.
        assert result == {"status": "paused", "paused_at_role": "role_a"}


class TestRunLoopProactiveDependencyInsertion:
    """Patch 7.2 -- eo/agent_dependencies.py's static AGENT_DEPENDENCIES
    graph is consulted BEFORE dispatch on the adaptive path, separate
    from (and in addition to) the reactive MissingDependencyError
    self-heal tested above."""

    def test_missing_static_dependency_is_inserted_before_dispatch(self, monkeypatch):
        monkeypatch.setattr(executor, "AGENT_DEPENDENCIES", {"main": ("prereq",)})
        call_log = []

        def fake_generic_worker(**kw):
            call_log.append(kw["role"])
            return {"text": f"done-{kw['role']}"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["main"], path="adaptive",
        ))

        assert call_log == ["prereq", "main"]
        assert results["prereq"] == {"text": "done-prereq"}
        assert results["main"] == {"text": "done-main"}

    def test_dependency_already_satisfied_is_not_re_inserted(self, monkeypatch):
        monkeypatch.setattr(executor, "AGENT_DEPENDENCIES", {"main": ("prereq",)})
        call_log = []

        def fake_generic_worker(**kw):
            call_log.append(kw["role"])
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["prereq", "main"], idx=1, path="adaptive",
            results={"prereq": {"text": "already ran fine"}},
        ))

        # Only "main" actually dispatches through this call -- "prereq"
        # was already satisfied, not re-inserted/re-run.
        assert call_log == ["main"]

    def test_dependency_that_already_failed_skips_dispatch_without_raising(self, monkeypatch):
        monkeypatch.setattr(executor, "AGENT_DEPENDENCIES", {"main": ("prereq",)})
        call_log = []

        def fake_generic_worker(**kw):
            call_log.append(kw["role"])
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker", "generic_worker"],
            role_names=["prereq", "main"], idx=1, path="adaptive",
            results={"prereq": {"status": "failed", "role": "prereq", "reason": "boom"}},
        ))

        assert call_log == []  # "main" never actually dispatched
        assert results["main"] == {
            "status": "failed", "role": "main",
            "reason": "prerequisite 'prereq' failed",
        }

    def test_non_adaptive_path_ignores_static_dependencies_entirely(self, monkeypatch):
        monkeypatch.setattr(executor, "AGENT_DEPENDENCIES", {"main": ("prereq",)})
        call_log = []

        def fake_generic_worker(**kw):
            call_log.append(kw["role"])
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)

        results = executor._run_loop(**_base_run_loop_kwargs(
            agent_names=["generic_worker"], role_names=["main"], path="fixed",
        ))

        # No prerequisite splicing on a statically-built graph -- "main"
        # dispatches directly, "prereq" never appears at all.
        assert call_log == ["main"]
        assert results == {"main": {"text": "ok"}}


class TestRunLoopDispatchCallShapes:
    """Each dispatch branch below passes a DIFFERENT set of kwargs to its
    agent function -- eo/executor.py's own comments flag several of
    these as previously-silent bugs (e.g. idea_planner/prompt_writer/
    test_writer/report_writer used to get nothing but a bare fn() call).
    One test per representative branch, asserting the exact kwargs the
    branch is documented to pass."""

    def _run_single_role(self, monkeypatch, current_name, role="the_role", task_text="task",
                          path="fixed", mode=None, key_overrides=None, scope=None,
                          workspace_id=None):
        captured = {}

        def fake_fn(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"text": "ok"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_fn)
        monkeypatch.setattr(executor, "resolve_role", lambda role: current_name)
        monkeypatch.setattr(executor, "list_known_roles", list)

        executor._run_loop(**_base_run_loop_kwargs(
            agent_names=[current_name], role_names=[role], task_text=task_text,
            path=path, mode=mode, key_overrides=key_overrides or {},
            scope=scope, workspace_id=workspace_id,
        ))
        return captured

    def test_responder_gets_raw_task_text_positionally(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "responder", task_text="what is the capital of France?")
        assert captured["args"] == ("what is the capital of France?",)
        assert captured["kwargs"]["session_id"] == "sess-1"
        assert captured["kwargs"]["path"] == "fixed"

    def test_prompt_writer_lean_gets_raw_task_text_positionally(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "prompt_writer_lean", task_text="build a todo app")
        assert captured["args"] == ("build a todo app",)
        assert captured["kwargs"]["domain"] is None

    def test_code_writers_gets_task_text_as_fallback_seed(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "code_writers", task_text="build X")
        assert captured["kwargs"]["task_text"] == "build X"
        assert captured["kwargs"]["session_id"] == "sess-1"
        assert "expanded" in captured["kwargs"]

    def test_file_manager_writeback_only_gets_project_unique_name(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "file_manager_writeback")
        assert captured["kwargs"] == {"project_unique_name": None}

    def test_structure_architect_gets_tier_derived_from_path(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "structure_architect", path="adaptive")
        assert captured["kwargs"]["tier"] == 3  # PATH_TO_TIER["adaptive"]
        assert captured["kwargs"]["task_text"] == "task"

    def test_hardware_speccer_receives_workspace_id(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "hardware_speccer",
                                          path="adaptive", workspace_id="ws-42")
        assert captured["kwargs"]["workspace_id"] == "ws-42"
        assert captured["kwargs"]["tier"] == 3

    def test_web_researcher_defaults_scope_to_general(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "web_researcher", scope=None)
        assert captured["kwargs"]["scope"] == "general"

    def test_web_researcher_forwards_explicit_scope(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "web_researcher", scope="hackernews")
        assert captured["kwargs"]["scope"] == "hackernews"

    def test_academic_search_gets_task_text_and_tier(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "academic_search", task_text="find papers", path="direct")
        assert captured["kwargs"]["task_text"] == "find papers"
        assert captured["kwargs"]["tier"] == 1

    def test_unscoped_tier_agent_gets_no_task_text(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "dependency_mapper", path="adaptive")
        assert "task_text" not in captured["kwargs"]
        assert captured["kwargs"]["tier"] == 3

    def test_previously_bare_role_now_gets_session_and_domain(self, monkeypatch):
        # Migration Part 2 §2.6's fix: idea_planner/prompt_writer/
        # test_writer/report_writer used to fall through to the bare
        # `else: fn()` branch and got nothing at all.
        captured = self._run_single_role(monkeypatch, "report_writer")
        assert captured["kwargs"] == {"session_id": "sess-1", "domain": None}
        assert captured["args"] == ()

    def test_unrecognized_agent_falls_through_to_bare_call(self, monkeypatch):
        captured = self._run_single_role(monkeypatch, "some_future_real_action_module")
        assert captured["args"] == ()
        assert captured["kwargs"] == {}


# ---------------------------------------------------------------------------
# execute_graph -- thin wiring test over _run_loop
# ---------------------------------------------------------------------------

class TestExecuteGraph:
    def test_wires_role_names_defaulting_to_agent_names(self, monkeypatch):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step", _sequential_next_step)

        results = executor.execute_graph(["generic_worker"], task_text="hi")
        assert results == {"generic_worker": {"text": "ok"}}

    def test_does_not_mutate_caller_supplied_lists(self, monkeypatch):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step", _sequential_next_step)

        original_agents = ["generic_worker"]
        original_roles = ["role_a"]
        executor.execute_graph(original_agents, role_names=original_roles, task_text="hi")

        assert original_agents == ["generic_worker"]
        assert original_roles == ["role_a"]


# ---------------------------------------------------------------------------
# resume_graph
# ---------------------------------------------------------------------------

def _write_pause_snapshot(session_id, **overrides):
    from memory.bus import write as bus_write
    snapshot = {
        "agent_names": ["generic_worker"],
        "role_names": ["role_a"],
        "idx": 0,
        "results": {"role_a": {"text": "first draft"}},
        "key_overrides": {},
        "auto_inserted": {},
        "stage_revisits": {},
        "path": "adaptive",
        "task_text": "do the thing",
        "project_unique_name": None,
        "mode": None,
        "approval_roles": ["role_a"],
        "no_conversation_context_roles": [],
        "domain": None,
        "scope": None,
        "workspace_id": None,
        "app_slug": None,
    }
    snapshot.update(overrides)
    bus_write(f"paused_execution:{session_id}", snapshot)
    return snapshot


class TestResumeGraph:
    def test_raises_key_error_when_no_paused_snapshot_exists(self, fake_bus):
        with pytest.raises(KeyError):
            executor.resume_graph("no-such-session", {"action": "approve"})

    def test_unknown_action_raises_value_error(self, fake_bus):
        _write_pause_snapshot("sess-unknown")
        with pytest.raises(ValueError):
            executor.resume_graph("sess-unknown", {"action": "not_a_real_action"})

    def test_approve_advances_past_the_paused_role_and_deletes_snapshot(self, monkeypatch, fake_bus):
        from memory.bus import read as bus_read

        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step",
                              lambda *a, **k: (None, "plan"))

        _write_pause_snapshot("sess-approve")
        result = executor.resume_graph("sess-approve", {"action": "approve"})

        assert result == {"role_a": {"text": "first draft"}}
        assert bus_read("paused_execution:sess-approve", default=None) is None

    def test_edit_overwrites_the_paused_roles_text_before_continuing(self, monkeypatch, fake_bus):
        from memory.bus import read as bus_read

        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step",
                              lambda *a, **k: (None, "plan"))

        _write_pause_snapshot("sess-edit")
        result = executor.resume_graph("sess-edit", {"action": "edit", "text": "edited version"})

        assert result == {"role_a": {"text": "edited version"}}
        # Edited text is also persisted to stage_output so any later
        # generic_worker step reading it via input_keys sees the edit.
        assert bus_read("stage_output:sess-edit:role_a") == {"text": "edited version"}

    def test_reject_redo_re_runs_the_same_role(self, monkeypatch, fake_bus):
        """role_a is re-dispatched from scratch. The snapshot's own
        approval_roles still lists role_a, so -- correctly -- the redo
        pauses again for re-review rather than sailing straight through;
        see test_reject_redo_re_run_that_clears_approval_completes
        below for the "redo, then no further approval needed" case."""
        call_count = {"n": 0}

        def fake_generic_worker(**kw):
            call_count["n"] += 1
            return {"text": "redone"}

        monkeypatch.setattr(executor, "resolve", lambda name: fake_generic_worker)
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step",
                              lambda *a, **k: (None, "plan"))

        _write_pause_snapshot("sess-redo", stage_revisits={})
        result = executor.resume_graph("sess-redo", {"action": "reject_redo"})

        assert call_count["n"] == 1
        assert result == {"status": "paused", "paused_at_role": "role_a"}

    def test_reject_redo_increments_stage_revisits_on_the_fresh_snapshot(self, monkeypatch, fake_bus):
        from memory.bus import read as bus_read

        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "redone"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step",
                              lambda *a, **k: (None, "plan"))

        _write_pause_snapshot("sess-redo-count", stage_revisits={"role_a": 1})
        executor.resume_graph("sess-redo-count", {"action": "reject_redo"})

        # The redo paused again (role_a still in approval_roles), so a
        # fresh snapshot exists -- carrying the incremented count.
        new_snapshot = bus_read("paused_execution:sess-redo-count")
        assert new_snapshot["stage_revisits"]["role_a"] == 2

    def test_reject_redo_that_clears_approval_completes_without_pausing(self, monkeypatch, fake_bus):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "redone"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step",
                              lambda *a, **k: (None, "plan"))

        _write_pause_snapshot("sess-redo-clear", stage_revisits={}, approval_roles=[])
        result = executor.resume_graph("sess-redo-clear", {"action": "reject_redo"})

        assert result == {"role_a": {"text": "redone"}}

    def test_reject_redo_past_the_cap_raises_and_deletes_snapshot(self, monkeypatch, fake_bus):
        from memory.bus import read as bus_read

        _write_pause_snapshot(
            "sess-redo-cap",
            stage_revisits={"role_a": executor.MAX_STAGE_REVISITS},
        )

        with pytest.raises(RuntimeError):
            executor.resume_graph("sess-redo-cap", {"action": "reject_redo"})

        assert bus_read("paused_execution:sess-redo-cap", default=None) is None

    def test_approve_emits_execution_resumed_event(self, monkeypatch, fake_bus, _quiet_emit):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step",
                              lambda *a, **k: (None, "plan"))

        _write_pause_snapshot("sess-event")
        executor.resume_graph("sess-event", {"action": "approve"})

        assert any(e[0] == "execution_resumed" for e in _quiet_emit)

    def test_approve_can_pause_again_on_a_later_role(self, monkeypatch, fake_bus):
        monkeypatch.setattr(executor, "resolve", lambda name: (lambda **kw: {"text": "ok"}))
        monkeypatch.setattr(executor, "resolve_role", lambda role: "generic_worker")
        monkeypatch.setattr(executor, "list_known_roles", list)
        import eo.dispatcher as dispatcher_module
        monkeypatch.setattr(dispatcher_module, "next_step", _sequential_next_step)

        _write_pause_snapshot(
            "sess-double-pause",
            agent_names=["generic_worker", "generic_worker"],
            role_names=["role_a", "role_b"],
            approval_roles=["role_a", "role_b"],
        )
        result = executor.resume_graph("sess-double-pause", {"action": "approve"})

        assert result == {"status": "paused", "paused_at_role": "role_b"}
