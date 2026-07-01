"""
agents/duplication_checker.py — Duplication/Similarity Checker (Part 4,
agent #7 of the v5 Master Blueprint).

HuggingFace Inference (sentence-embedding) -> Upstash Vector (DB4).
Shares the same Vector index as memory_search.py (#0) but a DIFFERENT id
prefix ("codechunk" vs "cyclemem") so the two never cross-contaminate each
other's queries.

Runs after code_writers.py (same code snapshot dependency_mapper.py and
reviewer.py look at) -- specifically positioned right after the Reviewer
Pool in loop.py, since "this module is 95% the same as one you already
wrote two cycles ago" is exactly the kind of thing that should show up
alongside the rest of the review notes.

For each submitted module this cycle:
  1. Embed its code.
  2. Query Vector for near-duplicates from EARLIER cycles of this same app
     (filtered by app_slug, excludes this cycle's own not-yet-stored chunks).
  3. Flag anything above SIMILARITY_THRESHOLD.
  4. Upsert this module's embedding for future cycles to compare against.

Output, written to KEYS["duplication_report"]:
{
  "flagged": [
    {"module": "task_validator", "similar_to": "validator_service",
     "cycle": 2, "score": 0.94}
  ],
  "summary": "1 likely-duplicate module found."
}
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, vector_index

load_dotenv()

HF_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HF_URL = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}/pipeline/feature-extraction"
ID_PREFIX = "codechunk"
SIMILARITY_THRESHOLD = 0.90


def _embed(text: str) -> list:
    api_key = os.getenv("HUGGINGFACE_API_KEY")
    if not api_key:
        raise RuntimeError("HUGGINGFACE_API_KEY not set")
    response = requests.post(
        HF_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"inputs": text[:4000], "options": {"wait_for_model": True}},
        timeout=30,
    )
    response.raise_for_status()
    vec = response.json()
    if isinstance(vec[0], list):
        dims = len(vec[0])
        return [sum(row[i] for row in vec) / len(vec) for i in range(dims)]
    return vec


def _app_slug() -> str:
    return read(KEYS["app_slug"], default=None) or read(KEYS["original_idea"], default="untitled")


def run() -> dict:
    submitted_code = read(KEYS["submitted_code"], default={})
    cycle_num = read(KEYS["cycle_count"], default=1)
    slug = _app_slug()

    flagged = []
    to_upsert = []

    for module_name, module_data in submitted_code.items():
        code = module_data.get("code", "") if isinstance(module_data, dict) else str(module_data)
        if not code.strip():
            continue
        try:
            vector = _embed(code)
        except Exception as exc:
            print(f"  [Duplication Checker] embed failed for {module_name}: {exc}")
            continue

        try:
            matches = vector_index().query(
                vector=vector, top_k=3, include_metadata=True,
                filter=f"app_slug = '{slug}'",
            )
        except Exception as exc:
            print(f"  [Duplication Checker] query failed for {module_name}: {exc}")
            matches = []

        for m in matches:
            meta = getattr(m, "metadata", None) or {}
            if meta.get("module") == module_name and meta.get("cycle_num") == cycle_num:
                continue  # can't be a dup of itself in the same cycle
            if m.score >= SIMILARITY_THRESHOLD:
                flagged.append({
                    "module": module_name,
                    "similar_to": meta.get("module", "unknown"),
                    "cycle": meta.get("cycle_num"),
                    "score": round(float(m.score), 4),
                })
                break  # one flag per module is enough signal

        to_upsert.append((f"{ID_PREFIX}:{slug}:{module_name}:{cycle_num}", vector, {
            "app_slug": slug, "module": module_name, "cycle_num": cycle_num,
        }))

    if to_upsert:
        try:
            vector_index().upsert(vectors=to_upsert)
        except Exception as exc:
            print(f"  [Duplication Checker] upsert failed: {exc}")

    report = {
        "flagged": flagged,
        "summary": (f"{len(flagged)} likely-duplicate module(s) found."
                    if flagged else "No likely duplicates found."),
    }
    write(KEYS["duplication_report"], report)
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
