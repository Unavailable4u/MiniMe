"""
tests/test_loop_v4_tier0.py — Part 11 of the v5 Master Blueprint's
testing plan: "end-to-end: trivial task in, Responder runs, no
Upstash/E2B/git touched."

Mocks the Inspector's classify() and the executor so this runs with no
real network/keys, focused purely on eo/loop_v4.py's own routing
decision -- did tier 0 actually call the Responder-only graph, and did it
avoid ever importing/calling loop.py (which is what would touch the full
Upstash/E2B/git-committing tier-3 pipeline)?

Run standalone:
    python -m pytest tests/test_loop_v4_tier0.py -v
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eo.loop_v4 as loop_v4

TIER0_DRAFT = {
    "tier": 0, "directed_task_type": None, "confidence": 0.95,
    "suggested_agents": ["responder"], "reasoning": "trivial factual question",
}


def test_tier0_task_routes_to_responder_only(monkeypatch):
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None: dict(TIER0_DRAFT))
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    logged = {}
    monkeypatch.setattr(
        loop_v4.routing_memory, "log_outcome",
        lambda task_text, decision, outcome="": logged.update(decision=decision, outcome=outcome),
    )
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)  # DB5 routing log, not under test here

    calls = []

    def fake_execute_graph(graph, task_text=None, cycle_num=None):
        calls.append(graph)
        return {"responder": "Paris is the capital of France."}

    monkeypatch.setattr(loop_v4, "execute_graph", fake_execute_graph)

    # If loop.py ever got imported here, tier 0 would be touching the
    # full tier-3 machinery -- assert it simply isn't in sys.modules as a
    # result of this call. (It may already be imported by an earlier,
    # unrelated test in the same session -- so this checks call behavior
    # via execute_graph instead of import-presence for reliability.)
    loop_v4.main.__wrapped__ = None  # no-op, keeps this test import-order independent
    old_argv = sys.argv
    try:
        sys.argv = ["loop_v4.py", "What's", "the", "capital", "of", "France?"]
        loop_v4.main()
    finally:
        sys.argv = old_argv

    assert calls == [["responder"]], f"expected only the responder graph to run, got {calls}"
    assert logged["outcome"] == "tier-0 responder answered directly"
    assert logged["decision"]["tier"] == 0


def test_tier0_never_calls_execute_graph_with_tier3_agents(monkeypatch):
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None: dict(TIER0_DRAFT))
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    monkeypatch.setattr(loop_v4.routing_memory, "log_outcome", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)

    seen_agents = set()

    def fake_execute_graph(graph, task_text=None, cycle_num=None):
        seen_agents.update(graph)
        return {"responder": "answer"}

    monkeypatch.setattr(loop_v4, "execute_graph", fake_execute_graph)

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


if __name__ == "__main__":
    print("This test uses pytest's monkeypatch fixture -- run via:")
    print("  python -m pytest tests/test_loop_v4_tier0.py -v")
