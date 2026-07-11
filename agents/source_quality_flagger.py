"""
agents/source_quality_flagger.py — Source quality + plagiarism/near-duplicate
flagging (Part 3 §3.8).

REAL_ACTION_ROLES tool agent, two independent checks, both deterministic
(zero LLM judgment calls -- same posture as contradiction_prefilter.py:
this is a narrowing/flagging pass, not a verdict):

1. Quality flags -- plain rule checks over KEYS["academic_search_report"]'s
   already-fetched metadata (venue, citation_count, year, source). No new
   external calls; a "citation count" or "venue" API is a real thing this
   COULD grow into, but nothing here was asked to add one, so this stays
   scoped to what's already on the bus, same discipline
   contradiction_prefilter.py followed for the same reason.

2. Plagiarism/near-duplicate check -- literal reuse, not a re-implementation,
   of agents/duplication_checker.py's embedding infrastructure: same
   shared Upstash Vector index, same embed_text()/log_usage() call shape,
   same imported SIMILARITY_THRESHOLD constant (so the two modules can
   never silently drift to different notions of "too similar"). Only
   difference is the id-prefix namespace ("sourcetext" vs
   duplication_checker's "codechunk") and the scope filter (this
   workspace's paper abstracts, vs a coding app's own code chunks across
   cycles) -- exactly the isolation duplication_checker.py's own
   docstring describes for why memory_search.py's "cyclemem" prefix and
   its own "codechunk" prefix never cross-contaminate. A near-duplicate
   here means two indexed abstracts are suspiciously close -- either the
   same paper indexed twice under different metadata, or a genuine
   plagiarism/redundant-citation signal worth a human or contradiction_
   detector-style judgment pass, not an automatic verdict.

Part 0 writes: one "finding" node + a "flags" edge back to the paper's
own node per quality-flagged paper; a direct "possible_duplicate_source"
edge between two papers' own nodes per near-duplicate pair (no
intermediate node needed there -- it's already a two-paper relationship,
same shape contradiction_prefilter.py's "possible_contradiction" edges
use for exactly the same reason).

Hand-off: writes its own `stage_output:{session_id}:source_quality_flagger`
entry directly (not via generic_worker.py's LEGACY_BUS_KEY_MAP bridge --
that bridge is only needed when a real-action module DOESN'T write
stage_output itself; this one does), so any later generic_worker role
that lists it in input_keys (researcher, fact_checker, consensus_meter,
writer, editor -- anything hired after it) sees the flags automatically
and can weight or avoid a flagged source accordingly.

Result written to KEYS["source_quality_report"]:
{
  "quality_flags": [{"paper_id", "title", "flags": [...]}],
  "near_duplicates": [{"paper_a", "paper_b", "score"}],
  "summary": "...",
}
"""
import os
import sys
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, vector_index, get_current_app_slug
from utils.llm_client import log_usage, embed_text
from eo.knowledge_graph import write_node
from eo.graph_edges import create_edge
from eo.errors import MissingDependencyError
from agents.duplication_checker import SIMILARITY_THRESHOLD  # literal reuse, not a re-derived constant

load_dotenv()

ID_PREFIX = "sourcetext"
HF_KEY_ENV = "HUGGINGFACE_API_KEY"

# Only flag a zero-citation paper if it's had reasonable time to accrue
# some -- a paper from this year having zero citations is normal, not a
# quality signal.
MIN_AGE_FOR_ZERO_CITATION_FLAG = 2


def _workspace_id() -> str:
    # Same session-isolation reasoning as academic_search.py's own
    # _workspace_id().
    return get_current_app_slug() or read(KEYS["original_idea"], default="untitled")


def _quality_flags(paper: dict, current_year: int) -> list:
    """Plain rule checks, deliberately conservative -- each one only
    fires on an unambiguous signal already in the data, never a
    fabricated score. A paper can end up with zero, one, or several
    flags; an empty list means nothing here looked wrong, not that the
    source was actively verified as high-quality."""
    flags = []
    if not paper.get("venue"):
        flags.append("no publication venue listed")
    citation_count = paper.get("citation_count")
    year = paper.get("year")
    if citation_count == 0 and year and (current_year - year) >= MIN_AGE_FOR_ZERO_CITATION_FLAG:
        flags.append(f"zero citations after {current_year - year} year(s)")
    if paper.get("source") == "arxiv" and not paper.get("doi") and not paper.get("venue"):
        flags.append("appears to be an unreviewed preprint with no listed peer-reviewed venue")
    return flags


def _format_summary(quality_flags: list, near_duplicates: list) -> str:
    lines = []
    if quality_flags:
        lines.append(f"{len(quality_flags)} source(s) with quality flags:")
        for q in quality_flags:
            lines.append(f"- \"{q['title']}\": {'; '.join(q['flags'])}")
    else:
        lines.append("No quality flags found by the deterministic checks.")
    lines.append("")
    if near_duplicates:
        lines.append(f"{len(near_duplicates)} near-duplicate source pair(s):")
        for d in near_duplicates:
            lines.append(f"- \"{d['paper_a']}\" vs \"{d['paper_b']}\" (similarity {d['score']})")
    else:
        lines.append("No near-duplicate sources found.")
    lines.append("")
    lines.append(
        "These are deterministic heuristic flags, not confirmed verdicts -- "
        "weight or verify accordingly rather than discarding a source outright "
        "on a single flag."
    )
    return "\n".join(lines)


def run(session_id: str = None, tier: int = None, domain: str = None) -> dict:
    report = read(KEYS["academic_search_report"])
    papers = (report or {}).get("papers") or []
    if not papers:
        raise MissingDependencyError(required_role="academic_search")

    workspace_id = _workspace_id()
    current_year = datetime.now(timezone.utc).year

    # --- 1. Quality flags ---
    quality_flags = []
    for paper in papers:
        flags = _quality_flags(paper, current_year)
        if not flags:
            continue
        quality_flags.append({
            "paper_id": paper.get("paper_id"), "node_id": paper.get("node_id"),
            "title": paper.get("title"), "flags": flags,
        })
        if paper.get("node_id"):
            finding_id = write_node(
                workspace_id=workspace_id, section="research", node_type="finding",
                title=f"Quality flag: {paper.get('title') or 'untitled'}",
                content="; ".join(flags), created_by="source_quality_flagger",
                tags=["quality_flag"], session_id=session_id, tier=tier,
            )
            if finding_id:
                try:
                    create_edge(f"node:{workspace_id}:{finding_id}",
                                f"node:{workspace_id}:{paper['node_id']}",
                                relation="flags", created_by="source_quality_flagger")
                except ValueError:
                    pass

    # --- 2. Plagiarism / near-duplicate check (reuses duplication_checker.py's
    # embedding infrastructure -- see module docstring) ---
    near_duplicates = []
    to_upsert = []
    for paper in papers:
        abstract = (paper.get("abstract") or "").strip()
        if not abstract:
            continue
        try:
            vector = embed_text(abstract[:4000])
        except Exception as exc:
            print(f"  [Source Quality Flagger] embed failed for {paper.get('title')}: {exc}")
            continue
        log_usage("huggingface", HF_KEY_ENV, None, session_id=session_id,
                  tier=tier, agent_name="Source Quality Flagger", domain=domain)

        try:
            matches = vector_index().query(
                vector=vector, top_k=3, include_metadata=True,
                filter=f"workspace_id = '{workspace_id}'",
            )
        except Exception as exc:
            print(f"  [Source Quality Flagger] query failed for {paper.get('title')}: {exc}")
            matches = []

        for m in matches:
            meta = getattr(m, "metadata", None) or {}
            if meta.get("paper_id") == paper.get("paper_id"):
                continue  # can't be a near-duplicate of itself
            if m.score >= SIMILARITY_THRESHOLD:
                near_duplicates.append({
                    "paper_a": paper.get("title"), "paper_b": meta.get("title", "unknown"),
                    "score": round(float(m.score), 4),
                })
                a_id, b_id = paper.get("node_id"), meta.get("node_id")
                if a_id and b_id:
                    try:
                        create_edge(f"node:{workspace_id}:{a_id}", f"node:{workspace_id}:{b_id}",
                                    relation="possible_duplicate_source",
                                    created_by="source_quality_flagger")
                    except ValueError:
                        pass
                break  # one flag per paper is enough signal, same as duplication_checker.py

        to_upsert.append((f"{ID_PREFIX}:{workspace_id}:{paper.get('paper_id')}", vector, {
            "workspace_id": workspace_id, "paper_id": paper.get("paper_id"),
            "title": paper.get("title"), "node_id": paper.get("node_id"),
        }))

    if to_upsert:
        try:
            vector_index().upsert(vectors=to_upsert)
        except Exception as exc:
            print(f"  [Source Quality Flagger] upsert failed: {exc}")

    summary_text = _format_summary(quality_flags, near_duplicates)
    result = {
        "quality_flags": quality_flags,
        "near_duplicates": near_duplicates,
        "summary": f"{len(quality_flags)} quality flag(s), {len(near_duplicates)} near-duplicate pair(s).",
    }
    write(KEYS["source_quality_report"], result)

    if session_id:
        write(f"stage_output:{session_id}:source_quality_flagger", summary_text)

    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))