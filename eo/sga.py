"""
eo/sga.py — Starter General Agents (SGA), Layer 0 of the MiniMe v6
architecture. Runs BEFORE the Inspector on every task. Three agents in an
escalating relay — most tasks resolve at Stage 1 alone.

Stage 1: SGA #1 alone. Escalates if it predicts (or takes) >~1s.
Stage 2: SGA #1 + #2 in parallel. Escalates if >~2s combined.
Stage 3: SGA #1 + #2 + #3 in parallel. Aborts entirely if >~3s combined —
         full hand-off to eo/inspector.py, no partial SGA answer used.

Which SGA is "Stage 1" rotates round-robin across calls so token usage
stays balanced across all three dedicated accounts (see _rotate_start()).
"""
import os
import sys
import time
import json
import itertools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from relay.emitter import emit_event

SGA_CHAINS = {
    "sga_1": [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_1"}],
    "sga_2": [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_2"}],
    "sga_3": [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_3"}],
}

SYSTEM_PROMPT = """You are a fast, general-purpose first responder. Try to answer the task \
directly and quickly. If you cannot answer confidently and quickly — because it needs real \
research, multi-step planning, or writing/editing code across files — respond with exactly \
the single word ESCALATE and nothing else. Do not attempt a partial or guessed answer in \
that case."""

# Tuning defaults — not measured yet, see Part 1's note on calibrating
# these against real latency data once live.
STAGE_TIMEOUTS = {1: 1.0, 2: 2.0, 3: 3.0}

_rotation = itertools.cycle(["sga_1", "sga_2", "sga_3"])

def _rotate_start():
    """Round-robin which SGA leads Stage 1, so the three dedicated
    accounts drain evenly over time rather than SGA #1 absorbing nearly
    all of the layer's volume."""
    first = next(_rotation)
    order = ["sga_1", "sga_2", "sga_3"]
    idx = order.index(first)
    return order[idx:] + order[:idx]

def _call_one(agent_key: str, task_text: str, session_id: str = None) -> str:
    return generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=task_text,
        chain=SGA_CHAINS[agent_key],
        agent_name=f"SGA ({agent_key})",
    ).strip()

def attempt(task_text: str, session_id: str = None) -> dict:
    """
    Returns {"resolved": True, "answer": str} on a successful SGA answer,
    or {"resolved": False} if all three stages escalate/time out — the
    caller (eo/loop_v4.py) then falls through to eo/inspector.classify()
    exactly as it does today for every task.
    """
    order = _rotate_start()
    emit_event("agent_start", session_id, agent="sga_relay",
               payload={"label": "SGA — attempting direct answer"})

    started = time.monotonic()
    active = [order[0]]
    stage = 1
    while stage <= 3:
        deadline = STAGE_TIMEOUTS[stage]
        results = {}
        for agent_key in active:
            try:
                results[agent_key] = _call_one(agent_key, task_text, session_id)
            except Exception:
                continue
            if "ESCALATE" not in results[agent_key].upper():
                emit_event("agent_done", session_id, agent="sga_relay",
                           payload={"summary": f"resolved at stage {stage} ({agent_key})"})
                return {"resolved": True, "answer": results[agent_key]}
        elapsed = time.monotonic() - started
        if elapsed > deadline or stage == 3:
            break
        stage += 1
        active = order[:stage]

    emit_event("agent_done", session_id, agent="sga_relay",
               payload={"summary": "escalated to Inspector — no confident SGA answer"})
    return {"resolved": False}


if __name__ == "__main__":
    # Quick standalone smoke test — same pattern as responder.py's own
    # __main__ block. Run: python eo/sga.py
    test_task = "What is 2+2?"
    result = attempt(test_task, session_id="sga_smoke_test")
    print(json.dumps(result, indent=2))