"""
agents/overlapping_checker.py — closes the one gap the MiniMe ingestion
audit found: Backlink Detector's reconciliation pass (agents/
backlink_detector.py:run_after_source_manager()) can reparent a new topic
under an existing one and connect them with a relation, but it never
says "these two are the same fact, collapse them." Two uploads describing
the same thing produce two permanent topic nodes today.

Called from agents/source_manager.py's Mode A pass, right after topic
extraction (_run_mode_a_topic_extraction), BEFORE
run_after_source_manager()'s reconciliation call, so Backlink Detector
can consume this module's tags instead of always treating new topics as
structurally new.

Pipeline per newly-extracted topic:
  1. Embed name + summary (utils.embedding.embed_text — same HF model /
     384-dim / Upstash Vector index eo/knowledge_graph.py already uses).
  2. Query Vector for nearest existing topics in this workspace
     (own id prefix "topic", filtered by workspace_id — never collides
     with knowledge_graph.py's "node:" ids or duplication_checker.py's
     "codechunk:" ids in the same shared index).
  3. Score candidates with utils.similarity.similarity() as a cheap
     pre-filter (word-Jaccard + char-SequenceMatcher, same primitive
     review_aggregator.py and security_aggregator.py already use — but
     with this module's own DEFAULT_STOPWORDS rather than either of
     theirs, since those two hand-tuned sets differ from each other and
     neither is "the" shared one; see utils/similarity.py).
  4. Only LLM-arbitrate the ambiguous band — thin, tag-only output,
     never prose. Below LOW_THRESHOLD: definitely new. Above
     HIGH_THRESHOLD: definitely the same, skip the LLM call entirely.
  5. Upsert this topic's own embedding for future uploads to compare
     against.

Output per topic: {"tag": "new" | "duplicate" | "merge",
"target_topic_id": <id or None>}. "duplicate" means near-identical,
safe to fold in silently; "merge" means related-but-not-identical,
worth keeping both but linking with an explicit same_fact_as relation
band (kept separate from Backlink Detector's four existing relations
so this module never has to write into their vocabulary directly).

Never raises: same posture as every other module downstream of an
upload (source_manager.py, backlink_detector.py) — a failure here
degrades every topic in this batch to "new" and gets logged, since an
upload succeeding shouldn't fail, or block reconciliation, because the
overlap pass couldn't run.

Place this file at: agents/overlapping_checker.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import add_role_prompt, get_role_prompt
from memory.bus import vector_index
from utils.embedding import embed_text
from utils.llm_client import log_usage
from utils.similarity import DEFAULT_STOPWORDS, similarity

ID_PREFIX = "topic"                 # own prefix — never collides with
                                     # knowledge_graph.py's "node:" or
                                     # duplication_checker.py's "codechunk:"
                                     # in the same shared Vector index
TOP_K = 5
LOW_THRESHOLD = 0.55                # below this: not worth an LLM call, tag "new"
HIGH_THRESHOLD = 0.92               # above this: certain duplicate, skip the LLM call
                                     # and tag "duplicate" directly
HF_KEY_ENV = "HUGGINGFACE_API_KEY"

OVERLAPPING_CHECKER_BRIEF = (
    "A NEW topic was just extracted from an upload. Decide if it is the "
    "SAME underlying fact as one of the EXISTING candidate topics you're "
    "given (near-duplicate, safe to fold together), MERGE-worthy (clearly "
    "related/overlapping but not identical — keep both, link them), or "
    "genuinely NEW (none of the candidates are close enough).\n\n"
    "You will be given the NEW topic's name and summary, then a list of "
    "CANDIDATES, each labeled with its id, a similarity score, its name, "
    "and a truncated summary.\n\n"
    "Respond with ONLY a JSON object: "
    '{"tag": "duplicate" | "merge" | "new", "target_topic_id": <id or null>}. '
    "No prose, no explanation, nothing outside that JSON object."
)


def _ensure_role_registered() -> None:
    # Same defensive bootstrap agents/source_manager.py's and
    # agents/backlink_detector.py's own _ensure_role_registered() do for
    # their roles — an already-running deployment that predates this
    # patch still needs this role's brief written the first time it's
    # actually hired. Without this, get_role_prompt("overlapping_checker")
    # returns nothing and _llm_arbitrate()'s run_role() call has no brief
    # to work from.
    if not get_role_prompt("overlapping_checker"):
        add_role_prompt("overlapping_checker", OVERLAPPING_CHECKER_BRIEF,
                         source="overlapping_checker_seed")


def _topic_vector_id(workspace_id: str, topic_id: str) -> str:
    return f"{ID_PREFIX}:{workspace_id}:{topic_id}"


def _candidate_text(name: str, summary: str) -> str:
    return f"{name}\n{summary}".strip()


def _llm_arbitrate(new_topic: dict, candidates: list[dict], session_id: str = None) -> dict:
    """Thin, tag-only arbitration for the ambiguous band only (same
    "thin, non-generative, tag not prose" shape the audit called out as
    cheap to build). Deferred import for the same circular-import
    reason agents/concept_linker.py and agents/backlink_detector.py
    already defer their generic_worker imports.
    """
    _ensure_role_registered()
    from agents.generic_worker import run as run_role

    candidate_block = "\n".join(
        f"- id={c['topic_id']} score={c['score']:.2f} :: {c['name']} — {c['summary'][:200]}"
        for c in candidates
    )
    task_text = (
        f"NEW topic: {new_topic['name']} — {new_topic['summary']}\n\n"
        f"CANDIDATES:\n{candidate_block}"
    )
    result = run_role(
        role="overlapping_checker", task_text=task_text, input_keys=[],
        session_id=session_id, include_conversation_context=False,
        domain="notes",
    )
    import json
    try:
        parsed = json.loads((result.get("text") or "").strip())
        tag = parsed.get("tag")
        if tag not in ("duplicate", "merge", "new"):
            return {"tag": "new", "target_topic_id": None}
        return {"tag": tag, "target_topic_id": parsed.get("target_topic_id")}
    except Exception:
        return {"tag": "new", "target_topic_id": None}


def check_topic(workspace_id: str, topic_id: str, name: str, summary: str,
                 session_id: str = None, tier=None, domain: str = None) -> dict:
    """Returns {"tag": "new"|"duplicate"|"merge", "target_topic_id": str|None}
    for ONE topic. Always upserts this topic's embedding at the end
    (even when tagged duplicate/merge) so later uploads can compare
    against it too — a merged topic still occupies real semantic space.
    """
    try:
        vector = embed_text(_candidate_text(name, summary))
    except Exception as exc:
        print(f"  [Overlapping Checker] embed failed for {topic_id}: {exc}")
        return {"tag": "new", "target_topic_id": None}

    # embed_text() has no logging side effect of its own (same as
    # agents/duplication_checker.py's and eo/knowledge_graph.py's own
    # comments) — log right after the embed call succeeds, so a
    # downstream Vector query failure below doesn't hide that the
    # billable HF call already happened.
    log_usage("huggingface", HF_KEY_ENV, None, session_id=session_id,
               tier=tier, agent_name="Overlapping Checker", domain=domain)

    result = {"tag": "new", "target_topic_id": None}
    try:
        matches = vector_index().query(
            vector=vector, top_k=TOP_K, include_metadata=True,
            filter=f"workspace_id = '{workspace_id}'",
        )
        candidates = []
        for m in matches:
            meta = getattr(m, "metadata", None) or {}
            if meta.get("topic_id") == topic_id:
                continue  # don't match against yourself if already upserted this batch
            score = similarity(
                f"{name} {summary}", f"{meta.get('name','')} {meta.get('summary','')}",
                DEFAULT_STOPWORDS,
            )
            if score >= LOW_THRESHOLD:
                candidates.append({
                    "topic_id": meta.get("topic_id"), "name": meta.get("name", ""),
                    "summary": meta.get("summary", ""), "score": score,
                })
        candidates.sort(key=lambda c: -c["score"])

        if candidates and candidates[0]["score"] >= HIGH_THRESHOLD:
            result = {"tag": "duplicate", "target_topic_id": candidates[0]["topic_id"]}
        elif candidates:
            result = _llm_arbitrate(
                {"name": name, "summary": summary}, candidates[:TOP_K],
                session_id=session_id,
            )
    except Exception as exc:
        print(f"  [Overlapping Checker] query/arbitrate failed for {topic_id}: {exc}")
        # fall through to "new" — still upsert below so future uploads see it

    try:
        vector_index().upsert(vectors=[(
            _topic_vector_id(workspace_id, topic_id), vector,
            {"workspace_id": workspace_id, "topic_id": topic_id,
             "name": name, "summary": summary},
        )])
    except Exception as exc:
        print(f"  [Overlapping Checker] upsert failed for {topic_id}: {exc}")

    return result


def check_batch(workspace_id: str, topics: list[dict], session_id: str = None,
                 tier=None, domain: str = None) -> dict:
    """topics: [{"topic_id": str, "name": str, "summary": str}, ...] — the
    same shape agents/source_manager.py:_topics_to_ops() already builds
    ids for. Returns {topic_id: {"tag": ..., "target_topic_id": ...}}.
    Sequential, not parallel — Mode A's own batch is already small
    (MODE_A_CHUNK_SIZE=8 sections per chunk), and each check needs the
    PRIOR topic's upsert to be visible to catch duplicates within the
    same upload, not just across uploads.
    """
    return {
        t["topic_id"]: check_topic(
            workspace_id, t["topic_id"], t["name"], t["summary"],
            session_id=session_id, tier=tier, domain=domain,
        )
        for t in topics
    }


if __name__ == "__main__":
    pass
