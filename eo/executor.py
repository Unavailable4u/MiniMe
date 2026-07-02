"""
eo/executor.py — Stage 4 steps 4-5 of the roadmap: the piece that
actually RUNS an execution graph from eo/router.py, for tiers 0-2.

Tier 3 deliberately does NOT go through this module — it keeps calling
loop.py directly, unmodified, exactly as Stage 4.1's roadmap step
required ("confirm the Router can reproduce today's exact 19-agent
sequence with zero behavior change"). loop.py's own multi-cycle,
Gatekeeper-driven control flow is not something this simple sequential
executor tries to reproduce or replace.

For tiers 0-2, execution really is just "call these agent names, in this
order" — no cycling, no Gatekeeper loop — so a plain sequential walk over
the graph is all that's needed.

Two agent names need special handling because, unlike the tier-3 roster
(which always reads its input from memory.bus), they're entry points that
need the raw task text passed in directly the first time:
  - "responder"          (tier 0 — the only agent in its graph)
  - "prompt_writer_lean" (tier 1 — the first agent in its graph)
Every other agent name in a tier-0/1/2 graph reads its input from
memory.bus, exactly like the tier-3 roster does, since the agent before it
in the same graph already wrote it there.

Stage 6 step 4 addition: each step now fires agent_start/agent_done (and
error, on failure) through relay/emitter.py, per Part 6.3's schema. This
is intentionally NOT token streaming yet (Part 6.4's agent_token_chunk) —
that requires instrumenting each agent's own LLM call to stream in
chunks, which belongs to a later step once agent internals are in view.
This step only lights up "agent started / agent finished" per lane.

session_id defaults to None, which makes emit_event() a documented no-op
(relay/emitter.py) — so every existing caller (CLI, tests) that doesn't
pass a session_id keeps working with zero behavior change, just a few
harmless no-op calls.
"""
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import resolve
from relay.emitter import emit_event

TASK_TEXT_ENTRYPOINTS = {"responder", "prompt_writer_lean"}


def _summarize(result, limit: int = 300) -> str:
    """Best-effort human-readable summary for an agent_done payload.
    Results vary in shape (str, dict, ...) across the agent roster, so
    this is deliberately forgiving rather than assuming a schema."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = result.get("code") or result.get("answer") or str(result)
    else:
        text = str(result)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def execute_graph(agent_names: list, task_text: str = None, cycle_num: int = None,
                   session_id: str = None, tier: int = None) -> dict:
    """
    Runs each agent name in `agent_names`, in order. Returns
    {agent_name: result} for every step that ran. Raises immediately (does
    not continue to the next agent) if any step raises — for tiers 0-2 a
    failed step means the whole graph's output is unusable, and silently
    continuing past it would produce a misleading partial result.

    session_id/tier, if given, are forwarded to relay/emitter.py so a
    connected frontend can render live per-agent activity (Part 6.6's
    "live agent activity panel"). Leaving session_id unset keeps this
    function's behavior identical to before Stage 6 step 4.
    """
    results = {}
    for name in agent_names:
        fn = resolve(name)
        print(f"  [Executor] running: {name}")
        emit_event("agent_start", session_id=session_id, agent=name, tier=tier,
                    payload={"label": name})
        started = time.monotonic()
        try:
            if name == "prompt_writer_lean" and task_text:
                # Stage 6 step 6 (Part 6.7): this is the first agent wired
                # to pass session_id/tier into generate_text() so token
                # usage gets logged and a usage_update event fires. Other
                # agents will get the same treatment one at a time -- see
                # llm_client.py's docstring note on why this stays a
                # no-op for anyone not yet passing session_id through.
                result = fn(task_text, session_id=session_id, tier=tier)
            elif name in TASK_TEXT_ENTRYPOINTS and task_text:
                result = fn(task_text)
            elif name == "code_writer_lean":
                result = fn(session_id=session_id, tier=tier)
            elif name == "reviewer_fixer_lean":
                result = fn(session_id=session_id, tier=tier)
            elif name == "code_writers":
                # Stage 6 step 5: the 5-worker pool, called by tier 2's
                # "refactor" directed task. Passes session_id/tier through
                # so each of the 5 parallel workers fires its own
                # agent_start/agent_done (code_writers.py's _write_one_module),
                # letting the frontend show real overlapping activity.
                result = fn(session_id=session_id, tier=tier)
            elif name == "security_scanner":
                   result = fn(session_id=session_id, tier=tier)
            elif name == "fixer_pool":
                result = fn(session_id=session_id, tier=tier)
            elif name == "reviewer":
                result = fn(session_id=session_id, tier=tier)
            elif name == "sandbox_tester_lean":
                result = fn(session_id=session_id, tier=tier)
            elif name == "gatekeeper":
                result = fn(cycle_num, session_id=session_id, tier=tier)
            else:
                result = fn()
        except Exception as exc:
            emit_event("error", session_id=session_id, agent=name, tier=tier,
                        payload={"message": f"{exc.__class__.__name__}: {exc}"})
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        results[name] = result
        print(f"  [Executor] done: {name}")
        emit_event("agent_done", session_id=session_id, agent=name, tier=tier,
                    payload={"summary": _summarize(result), "duration_ms": duration_ms})
    return results


if __name__ == "__main__":
    from eo.router import build_execution_graph
    graph = build_execution_graph(tier=0)
    print(execute_graph(graph, task_text="What is the capital of France?"))