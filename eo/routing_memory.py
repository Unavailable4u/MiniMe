"""
eo/routing_memory.py — Stage 4 step 7 of the roadmap:
    "Wire eo:routing_outcome and the Cross-Cycle Memory Search feedback
    loop (Part 8.3)."

Two jobs, deliberately mirroring agents/memory_search.py's own
store/retrieve split (same Upstash Vector index, a different id prefix so
the two never collide or get queried against each other):

  log_outcome(task_text, decision, outcome) — call once execution for a
      task has actually finished (or failed). Writes the raw record to
      Redis DB5 under "eo:routing_outcome" (Part 7's schema) AND embeds
      it into Vector, so future Inspector calls can retrieve similar past
      decisions.
  retrieve_similar_outcomes(task_text, top_k=3) — returns a short text
      block of similar past (task, tier, outcome) triples. This is
      OPTIONAL context an Inspector caller can pass into
      eo.inspector.classify()'s `context` parameter — it augments what
      the Inspector sees, it never tells the Inspector what to conclude,
      preserving the "classifies HONESTLY" contract inspector.py's own
      docstring insists on.

`outcome` is intentionally a free-form string, not a fixed enum — Part
8.3 describes this as "a manual or lightly-scripted process," and the
actual judgment of whether a routing decision was later found to be
under/over-routed is exactly the human-in-the-loop step Part 8.3 wants,
not something this module should guess at automatically.
"""
import os
import sys
import json
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, vector_index

ID_PREFIX = "eo_outcome"


def _embed(text: str) -> list:
    """Reuses the exact same HF embedding call agents/memory_search.py
    uses, duplicated rather than imported to keep this module usable even
    if memory_search.py's own dependencies (HUGGINGFACE_API_KEY) aren't
    configured yet -- routing-outcome logging shouldn't be gated on a key
    an EO-only deployment might not have set up."""
    import requests
    api_key = os.getenv("HUGGINGFACE_API_KEY")
    if not api_key:
        raise RuntimeError("HUGGINGFACE_API_KEY not set")
    hf_model = "sentence-transformers/all-MiniLM-L6-v2"
    url = f"https://router.huggingface.co/hf-inference/models/{hf_model}/pipeline/feature-extraction"
    response = requests.post(
        url, headers={"Authorization": f"Bearer {api_key}"},
        json={"inputs": text, "options": {"wait_for_model": True}}, timeout=30,
    )
    response.raise_for_status()
    vec = response.json()
    if isinstance(vec[0], list):
        dims = len(vec[0])
        return [sum(row[i] for row in vec) / len(vec) for i in range(dims)]
    return vec


def log_outcome(task_text: str, decision: dict, outcome: str = "") -> dict:
    """
    Writes the Part 7 DB5 key "eo:routing_outcome" (raw record, always
    happens, no network dependency) and best-effort embeds it into Vector
    for future retrieval (skipped, not raised, if HF/Vector aren't
    configured -- a missing feedback signal is a degradation, not a
    failure worth crashing the caller over, same reasoning
    memory_search.py itself already uses).
    """
    record = {
        "task_text": task_text,
        "tier": decision.get("tier") if decision else None,
        "directed_task_type": decision.get("directed_task_type") if decision else None,
        "confidence": decision.get("confidence") if decision else None,
        "panel_reviewed": decision.get("panel_reviewed", False) if decision else False,
        "outcome": outcome,
        "logged_at": time.time(),
    }
    write("eo:routing_outcome", record)
    try:
        text = f"task: {task_text} | tier: {record['tier']} | outcome: {outcome}"
        vector = _embed(text)
        vector_index().upsert(
            vectors=[(f"{ID_PREFIX}:{int(record['logged_at'] * 1000)}", vector, record)]
        )
    except Exception as exc:
        print(f"  [Routing Memory] embed/upsert skipped ({exc.__class__.__name__}: {exc}) "
              f"— eo:routing_outcome was still written to Redis.")
    return record


def retrieve_similar_outcomes(task_text: str, top_k: int = 3) -> str:
    """Returns a short text block of similar past routing outcomes, or ""
    if retrieval isn't available/configured. Safe to call unconditionally
    -- never raises."""
    try:
        vector = _embed(task_text)
        result = vector_index().query(
            vector=vector, top_k=top_k, include_metadata=True,
            filter=f"outcome != ''",
        )
    except Exception as exc:
        print(f"  [Routing Memory] retrieval skipped ({exc.__class__.__name__}: {exc}).")
        return ""
    lines = []
    for m in result:
        meta = getattr(m, "metadata", None)
        if not meta:
            continue
        lines.append(
            f"- task: {meta.get('task_text', '')!r} -> routed tier {meta.get('tier')}, "
            f"outcome: {meta.get('outcome', '')}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    rec = log_outcome(
        "write a small script that reverses a string",
        {"tier": 1, "directed_task_type": None, "confidence": 0.9},
        outcome="correctly routed, tier-1 lean pipeline completed successfully",
    )
    print(json.dumps(rec, indent=2, default=str))
