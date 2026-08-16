"""
tests/integration/test_parallel_execution.py — migrated from
scripts/test_parallel_execution.py (B1 manual/scripts sector migration).

Moved to tests/integration/, NOT tests/manual/, because this file was
already fully mocked at every network-facing edge before the move (see
the per-layer breakdown below) — it needs no live credentials and no
real Postgres/Redis/Pusher/LLM connection, so there's no reason for it
to sit in the "hits real infra, run by hand" tier. Letting it run in CI
is a strict improvement: it's a genuine regression test for the
Panel-synthesis consensus rule, the hard gatekeeper, and the executor's
concurrent-dispatch logic.

Exercises Steps 1-5 of the parallel-execution work together, real logic
under test everywhere except the network-facing edges each layer
genuinely has:

  - eo/panel.py's run_panel()/_merge_parallel_groups() (Step 2): the 3
    Panel LLM calls are mocked (utils.llm_client.generate_text, patched
    on eo.panel where it's imported), with staged parallel_groups
    outputs -- including one member's lone-voice / malformed guess --
    to confirm the "at least 2 of 3 agree" synthesis rule actually holds
    end-to-end, not just against the hand-built vote dicts a narrower
    unit test would use.

  - eo/router.py's sanitize_parallel_groups() (Step 3): called directly,
    unmocked (it's a pure function) -- adversarial input exercised
    directly against the real hard gatekeeper.

  - eo/executor.py's _run_loop()/_run_concurrent_group() (Steps 2/4/5):
    eo.registry.resolve() and eo.dispatcher.next_step() are mocked (via
    eo.executor's own already-bound references -- both are imported at
    call time inside execute_graph()/_run_concurrent_group(), so
    patching the source module's attribute is what actually takes
    effect; see the SCOPE NOTE below for why this is necessary at all).
    relay.emitter.emit_event is mocked too, so this never depends on
    Pusher being configured, and captures every event fired for
    assertions. memory.bus.write/get_current_app_slug are mocked for
    the one scenario that pauses (the pause snapshot write is the only
    other real I/O _run_loop() does) -- on top of, not instead of, the
    autouse fake_bus fixture in tests/conftest.py, since that fixture
    only swaps the underlying Redis client and these two scenarios want
    the calls themselves to no-op.

SCOPE NOTE, same posture scripts/test_proactive_suggestions.py already
documents for its own boundary: eo/executor.py's own import chain pulls
in the ENTIRE agents/ package (eo.registry -> agents/*.py -> groq,
cerebras_cloud_sdk, openai, e2b, upstash_redis, psycopg2, pusher, ...),
because REAL_ACTION_ROLES/REGISTRY are built at module-load time from
every agent module, not lazily. That's an unavoidable cost of importing
eo.executor at all (there's no way to test Steps 4/5 without it) --
this module pays that cost once, at import time, rather than trying to
stub the whole agents/ package out module-by-module. It does NOT stand
up a real Postgres/Redis/Pusher/LLM-provider connection anywhere below:
every actual network-reaching call these scenarios would otherwise make
is mocked at the call site, per layer above.
"""
import json
import time
from unittest.mock import patch

import eo.panel as panel
import eo.executor as executor
import eo.dispatcher as dispatcher
import memory.bus as bus
from eo.panel import run_panel
from eo.router import sanitize_parallel_groups, build_execution_graph_from_hires, MAX_PARALLEL_GROUP_SIZE
from eo.errors import MissingDependencyError
from eo.agent_dependencies import AGENT_DEPENDENCIES


# ---------------------------------------------------------------------------
# Shared fakes used by the executor scenarios (C/D/E below).
# ---------------------------------------------------------------------------

def _make_fakes(sleep_seconds: float = 0.0):
    """Returns (fake_resolve, fake_next_step, fake_emit_event, call_log,
    start_times, events) -- a fresh, independent set for each scenario so
    nothing leaks between them."""
    call_log = []
    start_times = {}
    events = []

    def fake_worker(role, task_text=None, input_keys=None, session_id=None,
                     key_override=None, include_conversation_context=True,
                     domain=None, chain_override=None):
        start_times[role] = time.monotonic()
        call_log.append(role)
        if sleep_seconds:
            time.sleep(sleep_seconds)
        # Deliberately no "next_destination" -- every scenario below just
        # wants plain forward progress through the plan, not escalation.
        return {"text": f"output for {role}"}

    def fake_resolve(_agent_name):
        # Every position in every scenario below resolves to the same
        # fake generic_worker -- Steps 2-5 never care WHICH module a role
        # would really resolve to, only whether role_names[idx] is a str
        # or a list, so one fake callable for every position is enough.
        return fake_worker

    def fake_next_step(agent_result, role_plan, idx, session_id=None, known_roles=None):
        nxt = idx + 1
        return (nxt if nxt < len(role_plan) else None), "plan"

    def fake_emit_event(event_type, session_id=None, agent=None, path=None, payload=None):
        events.append({"type": event_type, "agent": agent, "payload": payload})
        return True

    return fake_resolve, fake_next_step, fake_emit_event, call_log, start_times, events


# =============================================================================
# SCENARIO 1 (Step 2): run_panel() end-to-end with mocked LLM calls --
# 2-of-3 consensus survives (exact + superset agreement), a lone-voice
# guess (member C) is dropped, and a genuinely malformed group inside
# member C's own raw JSON (a role never in ITS OWN suggested_agents)
# is already gone before synthesis even runs, per eo/inspector.py
# Step 1's own loose-validate discipline.
# =============================================================================

def test_panel_synthesis_two_of_three_consensus():
    draft = {
        "path": "adaptive", "tier": 3, "directed_task_type": None, "confidence": 0.7,
        "suggested_agents": ["worker_a", "worker_b", "worker_c", "reviewer"],
        "reasoning": "draft: independent workers feeding one reviewer",
        "domain": None,
        "execution_order": ["worker_a", "worker_b", "worker_c", "reviewer"],
        "parallel_groups": [["worker_a", "worker_b"]],
    }
    member_b_json = json.dumps({
        "path": "adaptive", "directed_task_type": None, "confidence": 0.8,
        "suggested_agents": ["worker_a", "worker_b", "worker_c", "reviewer"],
        "reasoning": "member B agrees, thinks worker_c is independent too",
        "domain": None,
        "execution_order": ["worker_a", "worker_b", "worker_c", "reviewer"],
        # Superset of the draft's own {worker_a, worker_b} -- still
        # counts as agreement on the underlying claim per
        # _merge_parallel_groups()'s subset/superset rule.
        "parallel_groups": [["worker_a", "worker_b", "worker_c"]],
    })
    member_c_json = json.dumps({
        "path": "adaptive", "directed_task_type": None, "confidence": 0.65,
        "suggested_agents": ["worker_a", "worker_b", "worker_c", "reviewer"],
        "reasoning": "member C disagrees on shape, plus one malformed guess",
        "domain": None,
        "execution_order": ["worker_a", "worker_b", "worker_c", "reviewer"],
        "parallel_groups": [
            # "nonexistent_role" isn't in THIS vote's own suggested_agents
            # -- eo/inspector.py's _validate() drops it down to a
            # 1-member (worker_a-only) group, which is itself then
            # dropped for being under size 2. Never reaches the merge
            # step at all.
            ["worker_a", "nonexistent_role"],
            # A real, well-formed group -- but nobody else proposed it,
            # so it's a lone voice and must be dropped by the "2 of 3"
            # rule regardless of being well-formed.
            ["reviewer", "worker_c"],
        ],
    })

    def fake_generate_text(system_prompt, user_content, chain, agent_name, **kwargs):
        if "(B)" in agent_name:
            return member_b_json
        if "(C)" in agent_name:
            return member_c_json
        raise AssertionError(f"unexpected agent_name in mocked generate_text: {agent_name!r}")

    with patch.object(panel, "generate_text", fake_generate_text):
        synthesized = run_panel("build 3 independent workers, then review", draft)

    assert synthesized["parallel_groups"] == [["worker_a", "worker_b"]], \
        "worker_a+worker_b should survive (2-of-3, exact+superset agreement)"
    assert ["reviewer", "worker_c"] not in synthesized["parallel_groups"], \
        "member C's lone-voice [reviewer, worker_c] should not survive"
    assert synthesized["execution_order"] == ["worker_a", "worker_b", "worker_c", "reviewer"], \
        "synthesized execution_order should still be the plain flat union (Step 2 doesn't touch it)"
    assert synthesized["panel_votes"][2]["parallel_groups"] == [["reviewer", "worker_c"]], \
        "panel_votes should carry all 3 members' own (pre-merge) parallel_groups untouched"


# =============================================================================
# SCENARIO 2 (Step 3): sanitize_parallel_groups() -- the hard
# gatekeeper, called directly with a mix of legitimate and
# adversarial candidates in one pass.
# =============================================================================

def test_sanitize_parallel_groups_hard_gatekeeper():
    hires = [{"role": r, "agent_key": "K", "brief": "b"}
             for r in ["a", "b", "c", "d", "e", "approver", "f"]]
    execution_order = ["a", "b", "c", "d", "e", "approver", "f"]

    candidates = [
        ["a", "b"],                        # legitimate -> survives
        ["c", "approver"],                 # contains approval_roles member -> dropped
        ["b", "d"],                        # overlaps 'b' (already claimed) -> dropped whole
        ["e"],                              # singleton -> dropped
        ["a", "b", "c", "d", "f"],          # oversized (5 > MAX_PARALLEL_GROUP_SIZE) -> dropped
        "not-a-list",                        # malformed type -> ignored, no raise
        ["f", "ghost_role"],                # references a never-hired role -> dropped
        ["b", "b", "b"],                    # dedupes to a singleton -> dropped
    ]
    result = sanitize_parallel_groups(candidates, execution_order, ["approver"], hires)

    assert ["a", "b"] in result, "legitimate group [a, b] should survive as a nested list"
    assert all(not isinstance(e, list) for e in result if e != ["a", "b"]), \
        "no other group should survive (every remaining entry should be a flat role)"
    assert sorted(x for e in result for x in (e if isinstance(e, list) else [e])) == sorted(execution_order), \
        "every original role should still be accounted for, nothing dropped from the order itself"
    assert MAX_PARALLEL_GROUP_SIZE == 4, \
        "MAX_PARALLEL_GROUP_SIZE should still be the cap used above (sanity on the constant itself)"


# =============================================================================
# SCENARIO 3 (Steps 2/4): eo/executor.py actually dispatches an
# independent-role group CONCURRENTLY, not sequentially, and fires
# exactly one parallel_group_dispatched event for it.
# =============================================================================

def test_executor_dispatches_independent_roles_concurrently():
    SLEEP = 0.25
    fake_resolve, fake_next_step, fake_emit_event, call_log, start_times, events = _make_fakes(SLEEP)

    role_names = ["role_x", ["role_a", "role_b", "role_c"], "role_y"]
    agent_names = ["generic_worker", "generic_worker", "generic_worker"]

    with patch.object(executor, "resolve", fake_resolve), \
         patch.object(executor, "list_known_roles", lambda: []), \
         patch.object(executor, "emit_event", fake_emit_event), \
         patch.object(dispatcher, "next_step", fake_next_step):
        t0 = time.monotonic()
        result3 = executor.execute_graph(
            agent_names, role_names=role_names, task_text="build 3 things, then finish",
            session_id="sess-concurrency", path="adaptive",
        )
        elapsed = time.monotonic() - t0

    group_starts = [start_times[r] for r in ("role_a", "role_b", "role_c")]
    spread = max(group_starts) - min(group_starts)

    # Sequential would be 5 dispatches * SLEEP ~= 1.25s; genuinely
    # concurrent is 3 dispatches' worth (role_x, the group as ONE slot,
    # role_y) ~= 0.75s. Give generous headroom for scheduler jitter
    # without letting a truly-sequential run slip through as a pass.
    assert set(call_log) == {"role_x", "role_a", "role_b", "role_c", "role_y"}, "all 5 roles should run"
    assert spread < 0.15, \
        "group members should start within a tight window of each other (genuinely concurrent)"
    assert elapsed < SLEEP * 4, \
        "total wall time should be close to 3 dispatches' worth, not 5 (concurrency actually saved time)"
    assert sum(1 for e in events if e["type"] == "parallel_group_dispatched") == 1, \
        "exactly one parallel_group_dispatched event should fire"
    dispatched_event = next(e for e in events if e["type"] == "parallel_group_dispatched")
    assert set(dispatched_event["payload"]["roles"]) == {"role_a", "role_b", "role_c"}, \
        "the event's payload should name exactly the 3 group members"
    assert isinstance(result3, dict) and result3.get("status") != "paused" \
        and set(result3.keys()) == {"role_x", "role_a", "role_b", "role_c", "role_y"}, \
        "execute_graph() should return real results for every role (no pause)"


# =============================================================================
# SCENARIO 4 (Step 5): approval_roles member folded into a group --
# simulating Steps 2/3 both having a bug and letting one slip
# through. The executor's own backstop must degrade this to
# sequential and never actually concurrently dispatch the checkpoint
# role.
# =============================================================================

def test_approval_role_never_grouped_even_if_it_slips_through():
    fake_resolve4, fake_next_step4, fake_emit_event4, call_log4, _, events4 = _make_fakes(0.0)
    role_names4 = ["role_p", ["role_q", "role_r_APPROVAL"], "role_s"]
    agent_names4 = ["generic_worker", "generic_worker", "generic_worker"]

    with patch.object(executor, "resolve", fake_resolve4), \
         patch.object(executor, "resolve_role", lambda r: "generic_worker"), \
         patch.object(executor, "list_known_roles", lambda: []), \
         patch.object(executor, "emit_event", fake_emit_event4), \
         patch.object(dispatcher, "next_step", fake_next_step4), \
         patch.object(bus, "write", lambda *a, **kw: None), \
         patch.object(bus, "get_current_app_slug", lambda: "test_app"):
        result4 = executor.execute_graph(
            agent_names4, role_names=role_names4, task_text="t",
            session_id="sess-approval", path="adaptive",
            approval_roles={"role_r_APPROVAL"},
        )

    assert result4 == {"status": "paused", "paused_at_role": "role_r_APPROVAL"}, \
        "run should pause exactly at the approval_roles member"
    assert call_log4 == ["role_p", "role_q", "role_r_APPROVAL"], \
        "role_p (before the group) and role_q (the group's safe member) should both run"
    assert "role_s" not in call_log4, "role_s (after the pause point) should never run"
    assert all(e["type"] != "parallel_group_dispatched" for e in events4), \
        "NO parallel_group_dispatched event should fire -- the group should be degraded before dispatch"


# =============================================================================
# SCENARIO 5 (Steps 3+4 together): the full pipeline against a mixed
# bag of legitimate + adversarial parallel_groups -- sanitize, build
# the nested execution_order, then actually execute it. Malformed
# candidates degrade safely; the one legitimate group among them
# still runs concurrently.
# =============================================================================

def test_full_pipeline_sanitize_build_execute_against_adversarial_output():
    hires5 = [{"role": r, "agent_key": "K", "brief": "b"} for r in ["a", "b", "c", "approver"]]
    execution_order5 = ["a", "b", "c", "approver"]
    approval_roles5 = {"approver"}
    adversarial_groups = [
        ["a", "a", "b"],            # dedupes to a legitimate 2-member group
        ["b", "c", "approver"],     # overlaps 'b' AND has an approval member -> dropped
        ["nonexistent"],            # unhired singleton -> dropped
        42,                          # malformed type -> ignored
        None,                        # malformed type -> ignored
    ]

    sanitized5 = sanitize_parallel_groups(adversarial_groups, execution_order5, approval_roles5, hires5)
    assert sanitized5 == [["a", "b"], "c", "approver"], "exactly one nested group should survive: [a, b]"

    agent_names5, role_names5, key_overrides5 = build_execution_graph_from_hires(hires5, sanitized5)

    fake_resolve5, fake_next_step5, fake_emit_event5, call_log5, start_times5, events5 = _make_fakes(0.0)
    with patch.object(executor, "resolve", fake_resolve5), \
         patch.object(executor, "list_known_roles", lambda: []), \
         patch.object(executor, "emit_event", fake_emit_event5), \
         patch.object(dispatcher, "next_step", fake_next_step5), \
         patch.object(bus, "write", lambda *a, **kw: None), \
         patch.object(bus, "get_current_app_slug", lambda: "test_app"):
        result5 = executor.execute_graph(
            agent_names5, role_names=role_names5, task_text="t",
            session_id="sess-e2e", path="adaptive", approval_roles=approval_roles5,
        )

    assert result5 == {"status": "paused", "paused_at_role": "approver"}, \
        "execution should pause at 'approver' after running the group + 'c' with no errors"
    assert call_log5 == ["a", "b", "c", "approver"], \
        "every role up through the pause point should run exactly once, nothing duplicated or skipped"
    assert any(e["type"] == "parallel_group_dispatched" and set(e["payload"]["roles"]) == {"a", "b"}
               for e in events5), \
        "the surviving [a, b] group should fire its own dispatched event"


# =============================================================================
# SCENARIO 6 (Patch 7.2): a role with a KNOWN AGENT_DEPENDENCIES
# prerequisite gets that prerequisite spliced in and run BEFORE
# dispatch, on the strength of the static graph alone -- its own fn is
# never even given the chance to raise MissingDependencyError. Proves
# prerequisite ordering doesn't rely on the reactive except-branch for
# an edge this static list already knows about.
# =============================================================================

def test_proactive_dependency_insertion_for_known_edge():
    fake_resolve6, fake_next_step6, fake_emit_event6, call_log6, _, events6 = _make_fakes(0.0)

    assert AGENT_DEPENDENCIES["test_writer"] == ["implementer"], \
        "sanity check on the fixture this test relies on -- see eo/agent_dependencies.py"

    role_names6 = ["test_writer"]
    agent_names6 = ["generic_worker"]

    with patch.object(executor, "resolve", fake_resolve6), \
         patch.object(executor, "resolve_role", lambda r: "generic_worker"), \
         patch.object(executor, "list_known_roles", lambda: []), \
         patch.object(executor, "emit_event", fake_emit_event6), \
         patch.object(dispatcher, "next_step", fake_next_step6):
        result6 = executor.execute_graph(
            agent_names6, role_names=role_names6, task_text="t",
            session_id="sess-proactive", path="adaptive",
        )

    assert call_log6 == ["implementer", "test_writer"], \
        "implementer should be spliced in and run BEFORE test_writer, not after a failure"
    assert result6 == {"implementer": {"text": "output for implementer"},
                        "test_writer": {"text": "output for test_writer"}}
    requested6 = [e for e in events6 if e["type"] == "agent_requested_role"]
    assert len(requested6) == 1 and requested6[0]["payload"]["requested_role"] == "implementer", \
        "exactly one proactive-insertion event should fire, naming the prerequisite"
    assert all(e["type"] != "error" for e in events6), \
        "no error event should fire -- test_writer's fn is never given the chance to raise at all"


# =============================================================================
# SCENARIO 7 (Patch 7.2): the reactive `except MissingDependencyError`
# branch is still the safety net for an edge that ISN'T in
# AGENT_DEPENDENCIES -- the proactive check can't know to insert a
# prerequisite it has no entry for, so the agent has to raise it
# itself, same as before Patch 7.2 existed.
# =============================================================================

def test_reactive_self_heal_still_works_for_edge_not_in_static_graph():
    assert "custom_role" not in AGENT_DEPENDENCIES, \
        "this pair must be genuinely absent from the static graph for the test to prove anything"

    call_log7 = []
    events7 = []
    attempts7 = {"custom_role": 0}

    def fake_worker7(role, task_text=None, input_keys=None, session_id=None,
                      key_override=None, include_conversation_context=True,
                      domain=None, chain_override=None):
        if role == "custom_role":
            attempts7["custom_role"] += 1
            if attempts7["custom_role"] == 1:
                # First attempt: nothing has run yet, and this exact
                # (role, prereq) pair is deliberately absent from
                # AGENT_DEPENDENCIES -- the proactive check has nothing to
                # insert here, so it has to reach this agent's own raise.
                raise MissingDependencyError("unlisted_prereq", "needs unlisted_prereq first")
        call_log7.append(role)
        return {"text": f"output for {role}"}

    def fake_resolve7(_agent_name):
        return fake_worker7

    def fake_next_step7(agent_result, role_plan, idx, session_id=None, known_roles=None):
        nxt = idx + 1
        return (nxt if nxt < len(role_plan) else None), "plan"

    def fake_emit_event7(event_type, session_id=None, agent=None, path=None, payload=None):
        events7.append({"type": event_type, "agent": agent, "payload": payload})
        return True

    role_names7 = ["custom_role"]
    agent_names7 = ["generic_worker"]

    with patch.object(executor, "resolve", fake_resolve7), \
         patch.object(executor, "resolve_role", lambda r: "generic_worker"), \
         patch.object(executor, "list_known_roles", lambda: []), \
         patch.object(executor, "emit_event", fake_emit_event7), \
         patch.object(dispatcher, "next_step", fake_next_step7):
        result7 = executor.execute_graph(
            agent_names7, role_names=role_names7, task_text="t",
            session_id="sess-reactive", path="adaptive",
        )

    assert call_log7 == ["unlisted_prereq", "custom_role"], \
        "unlisted_prereq should still get spliced in and run first, via the reactive except-branch"
    assert attempts7["custom_role"] == 2, \
        "custom_role's own fn should be invoked twice: once where it raises, once where it succeeds"
    assert result7 == {"unlisted_prereq": {"text": "output for unlisted_prereq"},
                        "custom_role": {"text": "output for custom_role"}}
