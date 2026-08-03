"""
Parallel-execution work, Step 6: end-to-end mocked verification.

Exercises Steps 1-5 together, real logic under test everywhere except the
network-facing edges each layer genuinely has:

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
    other real I/O _run_loop() does).

SCOPE NOTE, same posture scripts/test_proactive_suggestions.py already
documents for its own boundary: eo/executor.py's own import chain pulls
in the ENTIRE agents/ package (eo.registry -> agents/*.py -> groq,
cerebras_cloud_sdk, openai, e2b, upstash_redis, psycopg2, pusher, ...),
because REAL_ACTION_ROLES/REGISTRY are built at module-load time from
every agent module, not lazily. That's an unavoidable cost of importing
eo.executor at all (there's no way to test Steps 4/5 without it) --
this script pays that cost once, at import time, rather than trying to
stub the whole agents/ package out module-by-module. It does NOT stand
up a real Postgres/Redis/Pusher/LLM-provider connection anywhere below:
every actual network-reaching call this script's own scenarios would
otherwise make is mocked at the call site, per function above.

Usage (bash):
    python scripts/test_parallel_execution.py

Usage (PowerShell):
    python scripts/test_parallel_execution.py
"""
import sys
import time
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eo.panel as panel
import eo.executor as executor
import eo.dispatcher as dispatcher
import memory.bus as bus
from eo.panel import run_panel
from eo.router import sanitize_parallel_groups, build_execution_graph_from_hires, MAX_PARALLEL_GROUP_SIZE


_any_issue = False


def check(label, cond):
    global _any_issue
    status = "OK" if cond else "FAIL"
    if not cond:
        _any_issue = True
    print(f"  [{status}] {label}")


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
                     domain=None):
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


def main() -> None:
    # =======================================================================
    # SCENARIO 1 (Step 2): run_panel() end-to-end with mocked LLM calls --
    # 2-of-3 consensus survives (exact + superset agreement), a lone-voice
    # guess (member C) is dropped, and a genuinely malformed group inside
    # member C's own raw JSON (a role never in ITS OWN suggested_agents)
    # is already gone before synthesis even runs, per eo/inspector.py
    # Step 1's own loose-validate discipline.
    # =======================================================================
    print("=" * 70)
    print("SCENARIO 1: Panel synthesis -- 2-of-3 consensus, lone voice dropped,")
    print("malformed member-C group filtered at the Inspector-schema layer")
    print("=" * 70)

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

    print("synthesized parallel_groups:", synthesized["parallel_groups"])
    check("worker_a+worker_b survives (2-of-3, exact+superset agreement)",
          synthesized["parallel_groups"] == [["worker_a", "worker_b"]])
    check("member C's lone-voice [reviewer, worker_c] did NOT survive",
          ["reviewer", "worker_c"] not in synthesized["parallel_groups"])
    check("synthesized execution_order is still the plain flat union (Step 2 doesn't touch it)",
          synthesized["execution_order"] == ["worker_a", "worker_b", "worker_c", "reviewer"])
    check("panel_votes carries all 3 members' own (pre-merge) parallel_groups untouched",
          synthesized["panel_votes"][2]["parallel_groups"] ==
          [["reviewer", "worker_c"]])  # member C's own vote, post-inspector-validate

    # =======================================================================
    # SCENARIO 2 (Step 3): sanitize_parallel_groups() -- the hard
    # gatekeeper, called directly with a mix of legitimate and
    # adversarial candidates in one pass.
    # =======================================================================
    print("\n" + "=" * 70)
    print("SCENARIO 2: sanitize_parallel_groups() hard gatekeeper --")
    print("adversarial and malformed candidates, all in one pass")
    print("=" * 70)

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
    print("result:", result)
    check("legitimate group [a, b] survived as a nested list",
          ["a", "b"] in result)
    check("no other group survived (every remaining entry is a flat role)",
          all(not isinstance(e, list) for e in result if e != ["a", "b"]))
    check("every original role still accounted for, nothing dropped from the order itself",
          sorted(x for e in result for x in (e if isinstance(e, list) else [e])) ==
          sorted(execution_order))
    check("MAX_PARALLEL_GROUP_SIZE is respected as the cap used above (sanity on the constant itself)",
          MAX_PARALLEL_GROUP_SIZE == 4)

    # =======================================================================
    # SCENARIO 3 (Steps 2/4): eo/executor.py actually dispatches an
    # independent-role group CONCURRENTLY, not sequentially, and fires
    # exactly one parallel_group_dispatched event for it.
    # =======================================================================
    print("\n" + "=" * 70)
    print("SCENARIO 3: independent roles actually dispatch concurrently")
    print("=" * 70)

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

    print(f"elapsed={elapsed:.3f}s, call_log={call_log}")
    group_starts = [start_times[r] for r in ("role_a", "role_b", "role_c")]
    spread = max(group_starts) - min(group_starts)
    print(f"group start spread={spread:.4f}s")

    # Sequential would be 5 dispatches * SLEEP ~= 1.25s; genuinely
    # concurrent is 3 dispatches' worth (role_x, the group as ONE slot,
    # role_y) ~= 0.75s. Give generous headroom for scheduler jitter
    # without letting a truly-sequential run slip through as a pass.
    check("all 5 roles ran", set(call_log) == {"role_x", "role_a", "role_b", "role_c", "role_y"})
    check("group members started within a tight window of each other (genuinely concurrent)",
          spread < 0.15)
    check("total wall time is close to 3 dispatches' worth, not 5 (concurrency actually saved time)",
          elapsed < SLEEP * 4)
    check("exactly one parallel_group_dispatched event fired",
          sum(1 for e in events if e["type"] == "parallel_group_dispatched") == 1)
    dispatched_event = next(e for e in events if e["type"] == "parallel_group_dispatched")
    check("the event's payload names exactly the 3 group members",
          set(dispatched_event["payload"]["roles"]) == {"role_a", "role_b", "role_c"})
    check("execute_graph() returned real results for every role (no pause)",
          isinstance(result3, dict) and result3.get("status") != "paused"
          and set(result3.keys()) == {"role_x", "role_a", "role_b", "role_c", "role_y"})

    # =======================================================================
    # SCENARIO 4 (Step 5): approval_roles member folded into a group --
    # simulating Steps 2/3 both having a bug and letting one slip
    # through. The executor's own backstop must degrade this to
    # sequential and never actually concurrently dispatch the checkpoint
    # role.
    # =======================================================================
    print("\n" + "=" * 70)
    print("SCENARIO 4: approval_roles member never gets grouped, even if it")
    print("slips past Steps 2/3 -- degrades to sequential and pauses correctly")
    print("=" * 70)

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

    print("result:", result4)
    print("call_log:", call_log4)
    print("events:", [e["type"] for e in events4])
    check("run paused exactly at the approval_roles member",
          result4 == {"status": "paused", "paused_at_role": "role_r_APPROVAL"})
    check("role_p (before the group) and role_q (the group's safe member) both ran",
          call_log4 == ["role_p", "role_q", "role_r_APPROVAL"])
    check("role_s (after the pause point) never ran",
          "role_s" not in call_log4)
    check("NO parallel_group_dispatched event fired -- the group was degraded before dispatch",
          all(e["type"] != "parallel_group_dispatched" for e in events4))

    # =======================================================================
    # SCENARIO 5 (Steps 3+4 together): the full pipeline against a mixed
    # bag of legitimate + adversarial parallel_groups -- sanitize, build
    # the nested execution_order, then actually execute it. Malformed
    # candidates degrade safely; the one legitimate group among them
    # still runs concurrently.
    # =======================================================================
    print("\n" + "=" * 70)
    print("SCENARIO 5: full pipeline (sanitize -> build graph -> execute) against")
    print("adversarial model output -- degrades safely, doesn't lose or corrupt")
    print("the legitimate group sitting alongside the garbage")
    print("=" * 70)

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
    print("sanitized execution_order:", sanitized5)
    check("exactly one nested group survived: [a, b]", sanitized5 == [["a", "b"], "c", "approver"])

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

    print("result:", result5)
    print("call_log:", call_log5)
    check("execution paused at 'approver' after running the group + 'c' with no errors",
          result5 == {"status": "paused", "paused_at_role": "approver"})
    check("every role up through the pause point ran exactly once, nothing duplicated or skipped",
          call_log5 == ["a", "b", "c", "approver"])
    check("the surviving [a, b] group fired its own dispatched event",
          any(e["type"] == "parallel_group_dispatched" and set(e["payload"]["roles"]) == {"a", "b"}
              for e in events5))

    print("\n" + "=" * 70)
    if _any_issue:
        print("One or more checks FAILED -- see above.")
        sys.exit(1)
    print("All checks passed: independent roles dispatch concurrently, approval_roles")
    print("members are never grouped (even adversarially), and malformed Panel output")
    print("degrades safely to sequential execution end-to-end.")


if __name__ == "__main__":
    main()
