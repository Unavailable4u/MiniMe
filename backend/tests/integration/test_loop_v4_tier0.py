"""
tests/integration/test_loop_v4_tier0.py — Part 11 of the v5 Master
Blueprint's testing plan: "end-to-end: trivial task in, Responder runs,
no Upstash/E2B/git touched."

Mocks the Inspector's classify() and the executor so this runs with no
real network/keys, focused purely on eo/loop_v4.py's own routing
decision -- did tier 0 actually call the Responder-only graph, and did
it avoid ever running any tier-3 agent?

Moved from tests/test_loop_v4_tier0.py (B1 audit) and updated for
Migration Part 12/15: eo/inspector.py's classify() now returns a "path"
string ("instant"/"direct"/"fixed"/"adaptive"), not a "tier" int --
eo/loop_v4.py's _get_decision() converts via PATH_TO_TIER immediately
after calling classify() (`draft["tier"] = PATH_TO_TIER[draft["path"]]`),
so the classify() stub here must supply "path", not "tier" — supplying
the old "tier"-only draft raises KeyError on draft["path"] before the
test gets anywhere near the assertions it's actually checking.
"""
from eo import loop_v4

TIER0_DRAFT = {
    "path": "instant", "directed_task_type": None, "confidence": 0.95,
    "suggested_agents": ["responder"], "reasoning": "trivial factual question",
}


def test_tier0_task_routes_to_responder_only(monkeypatch):
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None, session_id=None: dict(TIER0_DRAFT))
    # check_cache/write_cache (eo/semantic_cache.py) talk to Upstash Vector
    # through their own client, not memory.bus -- so tests/conftest.py's
    # autouse fake_bus fixture never touches them. Without mocking these
    # directly, a real cache hit against a real Vector index (e.g. from
    # this exact task text having been run for real before) short-circuits
    # main() BEFORE it ever reaches classify()/execute_graph, which is
    # silently environment-dependent: whether this test passes would
    # depend on what's actually cached in whoever's real Upstash account.
    monkeypatch.setattr(loop_v4, "check_cache", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "write_cache", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    logged = {}
    monkeypatch.setattr(
        loop_v4.routing_memory, "log_outcome",
        lambda task_text, decision, outcome="": logged.update(decision=decision, outcome=outcome),
    )
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)  # DB5 routing log, not under test here
    monkeypatch.setattr(loop_v4.conversation_memory, "get_light_context", lambda *a, **k: None)
    # staff_task() would otherwise try to write a real brief for
    # "responder" (no key_env set in this test env) since it isn't
    # pre-registered -- not what this test is about, so stub it out.
    # Patch B8: staff_task() is now called with session_id=<real uuid>
    # (main() no longer hard-codes None), so the stub must accept it.
    captured_staff_task_session_id = {}
    monkeypatch.setattr(
        loop_v4, "staff_task",
        lambda decision, task_text=None, session_id=None: (
            captured_staff_task_session_id.__setitem__("value", session_id)
            or [{"role": "responder", "agent_key": None, "brief": ""}]
        ),
    )

    calls = []

    def fake_execute_graph(graph, task_text=None, cycle_num=None, session_id=None):
        calls.append(graph)
        return {"responder": "Paris is the capital of France."}

    monkeypatch.setattr(loop_v4, "execute_graph", fake_execute_graph)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["loop_v4.py", "What's", "the", "capital", "of", "France?"]
        loop_v4.main()
    finally:
        sys.argv = old_argv

    assert calls == [["responder"]], f"expected only the responder graph to run, got {calls}"
    assert logged["outcome"] == "tier-0 responder answered directly"
    assert logged["decision"]["tier"] == 0
    # Patch B8: the CLI path must mint and thread a real (non-None)
    # session_id now, instead of hard-coding None end to end.
    assert captured_staff_task_session_id["value"] is not None


def test_tier0_never_calls_execute_graph_with_tier3_agents(monkeypatch):
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None, session_id=None: dict(TIER0_DRAFT))
    monkeypatch.setattr(loop_v4, "check_cache", lambda *a, **k: None)  # see comment above
    monkeypatch.setattr(loop_v4, "write_cache", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    monkeypatch.setattr(loop_v4.routing_memory, "log_outcome", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4.conversation_memory, "get_light_context", lambda *a, **k: None)
    monkeypatch.setattr(
        loop_v4, "staff_task",
        lambda decision, task_text=None, session_id=None: [{"role": "responder", "agent_key": None, "brief": ""}],
    )

    seen_agents = set()

    def fake_execute_graph(graph, task_text=None, cycle_num=None, session_id=None):
        seen_agents.update(graph)
        return {"responder": "answer"}

    monkeypatch.setattr(loop_v4, "execute_graph", fake_execute_graph)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["loop_v4.py", "trivial question"]
        loop_v4.main()
    finally:
        sys.argv = old_argv

    tier3_only_agents = {"code_writers", "reviewer", "fixer_pool", "gatekeeper", "file_manager"}
    assert not (seen_agents & tier3_only_agents), (
        f"tier 0 must never touch tier-3 agents, but saw: {seen_agents & tier3_only_agents}"
    )
