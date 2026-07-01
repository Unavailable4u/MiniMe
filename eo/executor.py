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
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import resolve

TASK_TEXT_ENTRYPOINTS = {"responder", "prompt_writer_lean"}


def execute_graph(agent_names: list, task_text: str = None, cycle_num: int = None) -> dict:
    """
    Runs each agent name in `agent_names`, in order. Returns
    {agent_name: result} for every step that ran. Raises immediately (does
    not continue to the next agent) if any step raises — for tiers 0-2 a
    failed step means the whole graph's output is unusable, and silently
    continuing past it would produce a misleading partial result.
    """
    results = {}
    for name in agent_names:
        fn = resolve(name)
        print(f"  [Executor] running: {name}")
        if name in TASK_TEXT_ENTRYPOINTS and task_text:
            result = fn(task_text)
        elif name == "gatekeeper":
            result = fn(cycle_num)
        else:
            result = fn()
        results[name] = result
        print(f"  [Executor] done: {name}")
    return results


if __name__ == "__main__":
    from eo.router import build_execution_graph
    graph = build_execution_graph(tier=0)
    print(execute_graph(graph, task_text="What is the capital of France?"))
