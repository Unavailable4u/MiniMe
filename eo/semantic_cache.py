"""
eo/semantic_cache.py — Semantic Cache (blueprint §16.2). Checked BEFORE
eo/sga.py's attempt(), so a near-duplicate task can skip the entire SGA
relay, not just the Inspector.

Shares the same Upstash Vector index as agents/memory_search.py (see
memory/bus.py's vector_index() docstring), but uses a "project" metadata
field instead of memory_search.py's "app_slug" field — deliberately
different field names so a filter query here never accidentally matches
memory_search.py's cyclemem entries, or vice versa.
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import vector_index
from utils.llm_client import embed_text

SIMILARITY_THRESHOLD = 0.93
CACHE_TTL_SECONDS = 60 * 60 * 48  # 48h default, tune later


def check_cache(task_text: str, app_slug: str = None) -> dict | None:
    """Returns a cached answer if a close-enough match exists, else None.
    Scoped by app_slug when given, so a cached answer from one project
    never leaks into another (blueprint §24, risk #6)."""
    try:
        vector = embed_text(task_text)
    except Exception:
        # Embedding failure (timeout, cold start, HF outage, etc.) should
        # degrade to "cache miss," not crash the whole task pipeline.
        return None
    index = vector_index()
    results = index.query(vector=vector, top_k=1, include_metadata=True,
                          filter=f"project = '{app_slug}'" if app_slug else "project = 'global'")
    if not results:
        return None
    top = results[0]
    if top.score >= SIMILARITY_THRESHOLD:
        return top.metadata.get("answer")
    return None


def write_cache(task_text: str, answer: str, app_slug: str = None) -> None:
    try:
        vector = embed_text(task_text)
    except Exception:
        # Best-effort cache write; losing one write isn't worth crashing the task.
        return
    index = vector_index()
    index.upsert(vectors=[{
        "id": f"semcache_{hash(task_text)}",
        "vector": vector,
        "metadata": {"answer": answer, "project": app_slug or "global"},
    }])