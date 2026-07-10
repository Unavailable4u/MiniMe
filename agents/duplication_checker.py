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
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, vector_index
from utils.llm_client import log_usage, embed_text

load_dotenv()

ID_PREFIX = "codechunk"
SIMILARITY_THRESHOLD = 0.90
HF_KEY_ENV = "HUGGINGFACE_API_KEY"

# Migration Part 26 §4a: this module used to hand-roll its own _embed(),
# POSTing to the HF inference URL directly -- the same pattern
# agents/memory_search.py's own comment flagged as something this file
# "should use for its HF calls" and never got fixed. Now routes through
# the shared utils.llm_client.embed_text(), matching memory_search.py.
# embed_text() already mean-pools an unpooled [seq_len][dim] response the
# same way this file's old _embed() did, so nothing is lost there. Two
# things embed_text() does NOT do that the old _embed() did, preserved at
# the call site below instead: truncating to 4000 chars, and logging
# usage (embed_text() itself stays a pure embed call with no logging
# side effect, same as it already is for memory_search.py's two callers).
#
# (eo/routing_memory.py has a THIRD hand-rolled _embed() copy, left alone
# on purpose -- see its own comment for why -- so it's not part of this
# merge.)


def _app_slug() -> str:
    # Migration Part B (session isolation fix): was
    # read(KEYS["app_slug"], ...), which -- "app_slug" being exempt from
    # memory.bus's namespacing -- always reads the raw, UNSCOPED global
    # Redis record instead of this run's own session-scoped slug (see
    # api/task_runner.py's _run_tier3_hires() and memory/bus.py's
    # set_app_slug()/get_current_app_slug()). That let this vector-index
    # filter mix duplication results across unrelated sessions.
    from memory.bus import get_current_app_slug
    return get_current_app_slug() or read(KEYS["original_idea"], default="untitled")


def run(session_id: str = None, tier=None, domain: str = None) -> dict:
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
            # Truncated to 4000 chars -- embed_text() itself doesn't
            # truncate (its two other callers, memory_search.py and
            # eo/semantic_cache.py, only ever embed short text), but code
            # snippets can be much longer, so this file still truncates
            # at its own call site to preserve the old _embed()'s behavior.
            vector = embed_text(code[:4000])
        except Exception as exc:
            print(f"  [Duplication Checker] embed failed for {module_name}: {exc}")
            continue
        # embed_text() has no logging side effect of its own (same as
        # memory_search.py's two embed_text() call sites) -- log right
        # after the embed call succeeds, same reasoning as
        # memory_search.py's own comment: a downstream Vector query
        # failure shouldn't hide that the billable HF call already happened.
        log_usage("huggingface", HF_KEY_ENV, None, session_id=session_id,
                   tier=tier, agent_name="Duplication Checker", domain=domain)

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