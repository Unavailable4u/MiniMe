"""
agents/memory_search.py — Cross-Cycle Memory Search (Part 4, agent #0 of
the v5 Master Blueprint).

HuggingFace Inference (sentence-embedding) -> Upstash Vector (DB4).
No fallback listed in the blueprint for this one -- if HF Inference is
down, retrieval just degrades to "no prior context," which is safe (worse
planning, not a broken cycle), so this agent never raises to stop the loop.

Two jobs, both exposed as plain functions so other agents/loop.py can call
either independently:

  store_cycle_memory(cycle_num)  -- call AFTER report_writer.py, embeds this
      cycle's goal + summary + all_tests_passed, upserts into Vector under
      id "cyclemem:{app_slug}:{cycle_num}".

  retrieve_context(query_text, top_k=3) -- call BEFORE idea_planner.py,
      embeds the query (usually the original idea + current feature_status),
      queries Vector for the most similar past cycles FOR THIS APP ONLY
      (filtered by app_slug in metadata), returns a short text block ready
      to drop into idea_planner's prompt.

This agent is read-only with respect to Redis (DB1-3) -- it only ever reads
KEYS it needs and writes to KEYS["retrieved_context"] plus Vector itself.
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, vector_index
from utils.llm_client import log_usage

load_dotenv()

HF_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HF_URL = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}/pipeline/feature-extraction"
ID_PREFIX = "cyclemem"


def _embed(text: str) -> list:
    api_key = os.getenv("HUGGINGFACE_API_KEY")
    if not api_key:
        raise RuntimeError("HUGGINGFACE_API_KEY not set")
    response = requests.post(
        HF_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"inputs": text, "options": {"wait_for_model": True}},
        timeout=30,
    )
    response.raise_for_status()
    vec = response.json()
    # feature-extraction can return a token-level matrix ([tokens][dims]) or
    # already-pooled [dims] depending on the model; mean-pool defensively.
    if isinstance(vec[0], list):
        dims = len(vec[0])
        pooled = [sum(row[i] for row in vec) / len(vec) for i in range(dims)]
        return pooled
    return vec


def _app_slug() -> str:
    return read(KEYS["app_slug"], default=None) or read(KEYS["original_idea"], default="untitled")


def store_cycle_memory(cycle_num: int, session_id: str = None, tier: int = None) -> None:
    report = read(KEYS["latest_report"], default=None)
    plan = read(KEYS["current_plan"], default={})
    if not report:
        return
    slug = _app_slug()
    summary_text = (
        f"cycle_goal: {plan.get('cycle_goal', '')} | "
        f"target_feature: {plan.get('target_feature', '')} | "
        f"all_tests_passed: {report.get('all_tests_passed')} | "
        f"summary: {report.get('summary', '')}"
    )
    try:
        vector = _embed(summary_text)
    except Exception as exc:
        print(f"  [Memory Search] embed failed, skipping store: {exc}")
        return
    # HF feature-extraction has no chat-completion "usage" object to pull a
    # token count from, so this logs a request-only entry (tokens=None) --
    # same pattern duplication_checker.py should use for its HF calls.
    log_usage("huggingface", "HUGGINGFACE_API_KEY", None,
              session_id=session_id, tier=tier, agent_name="Memory Search")
    try:
        vector_index().upsert(
            vectors=[(f"{ID_PREFIX}:{slug}:{cycle_num}", vector, {
                "app_slug": slug,
                "cycle_num": cycle_num,
                "text": summary_text,
            })]
        )
    except Exception as exc:
        print(f"  [Memory Search] vector upsert failed: {exc}")


def retrieve_context(query_text: str, top_k: int = 3, session_id: str = None, tier: int = None) -> str:
    slug = _app_slug()
    try:
        vector = _embed(query_text)
    except Exception as exc:
        print(f"  [Memory Search] retrieval failed, continuing with no context: {exc}")
        write(KEYS["retrieved_context"], "")
        return ""
    # Log right after the embed call itself succeeds -- a downstream
    # Vector query failure shouldn't hide the fact that the billable HF
    # call already happened.
    log_usage("huggingface", "HUGGINGFACE_API_KEY", None,
              session_id=session_id, tier=tier, agent_name="Memory Search")
    try:
        result = vector_index().query(
            vector=vector, top_k=top_k, include_metadata=True,
            filter=f"app_slug = '{slug}'",
        )
    except Exception as exc:
        print(f"  [Memory Search] retrieval failed, continuing with no context: {exc}")
        write(KEYS["retrieved_context"], "")
        return ""
    lines = [m.metadata.get("text", "") for m in result if getattr(m, "metadata", None)]
    context = "\n".join(f"- {line}" for line in lines if line)
    write(KEYS["retrieved_context"], context)
    return context


def run(session_id: str = None, tier: int = None) -> str:
    """Convenience entrypoint for loop.py: retrieve context for the
    upcoming cycle, based on the original idea + feature status."""
    idea = read(KEYS["original_idea"], default="")
    feature_status = read(KEYS["feature_status"], default={})
    query = f"{idea} | feature_status: {json.dumps(feature_status)}"
    return retrieve_context(query, session_id=session_id, tier=tier)


if __name__ == "__main__":
    print(run())