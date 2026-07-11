"""
agents/contradiction_prefilter.py — Contradiction/gap detector, deterministic
half (Part 3 §3.6).

REAL_ACTION_ROLES tool agent, modeled on duplication_checker.py/
academic_search.py's shape: makes zero LLM calls, only plain Python over
KEYS["extraction_table"] (agents/extraction_table_builder.py's output,
Part 3 §3.5), then writes Part 0 nodes/edges the same way academic_search.py
does.

This is deliberately ONLY the narrowing step. Comparing every paper's
outcome against every other paper's outcome (an O(n^2) pair count) is
exactly the kind of judgment call a plain string/keyword heuristic
CANNOT safely make on its own -- "increase" vs "decrease" is a cheap,
noisy signal, not a verdict. So this module's only job is to cut a large
pair count down to a short, plausible candidate list (and a short list
of coverage gaps) that a real reasoning pass can then actually examine.
The judgment itself belongs to "contradiction_detector" -- deliberately
NOT a dedicated module (see eo/registry.py's REAL_ACTION_ROLES comment),
which runs through agents/generic_worker.py like any other reasoning
role.

Hand-off mechanism (no changes needed to generic_worker.py): this module
writes its candidate list as a plain-text summary directly to
`stage_output:{session_id}:contradiction_prefilter` -- the exact bus key
generic_worker.py's own input_keys loop already reads for any role
listed ahead of it in the plan. As long as "contradiction_prefilter" is
hired ahead of "contradiction_detector" in the execution order (see
eo/structure.py's STRUCTURE_TEMPLATES["research"]), contradiction_detector
sees this module's output automatically, through the same mechanism
every other generic_worker-to-generic_worker hand-off already uses.

Result also written to KEYS["contradiction_candidates"]:
{
  "candidate_pairs": [{"paper_a", "paper_b", "population", "outcome_a",
                        "outcome_b", "reason"}],
  "candidate_gaps": [{"field", "reason"}],
  "summary": "...",
}
"""
import os
import sys
import re
import json
from collections import defaultdict

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, get_current_app_slug
from eo.knowledge_graph import write_node
from eo.graph_edges import create_edge
from eo.errors import MissingDependencyError

# Deliberately small and literal -- a false negative here just means
# contradiction_detector's LLM pass never sees that pair (safe: the LLM
# pass is the real judgment, this is only a cheap narrowing step). A
# false positive costs one extra pair in the LLM's context, not a wrong
# answer, since the LLM still has to actually judge it.
POSITIVE_TERMS = (
    "increase", "improv", "effective", "benefit", "positive", "reduc",
    "significant", "support", "success",
)
NEGATIVE_TERMS = (
    "decrease", "no effect", "null result", "ineffective", "no significant",
    "negative", "did not", "failed to", "no association", "no difference",
)

MIN_PAPERS_FOR_GAP_CHECK = 3
FIELD_NAMES = ["sample_size", "methodology", "population", "outcome", "effect_size"]


def _workspace_id() -> str:
    # Same session-isolation reasoning as academic_search.py's own
    # _workspace_id().
    return get_current_app_slug() or read(KEYS["original_idea"], default="untitled")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _polarity(text: str) -> str | None:
    """Returns "positive", "negative", or None (no clear signal) --
    never guesses when neither term set matches, since an unclear
    outcome shouldn't be forced into a candidate pair."""
    norm = _normalize(text)
    if not norm:
        return None
    has_pos = any(term in norm for term in POSITIVE_TERMS)
    has_neg = any(term in norm for term in NEGATIVE_TERMS)
    if has_pos and not has_neg:
        return "positive"
    if has_neg and not has_pos:
        return "negative"
    return None   # both or neither matched -- genuinely ambiguous, skip


def _find_candidate_pairs(rows: list) -> list:
    """Groups papers by normalized population (papers with no stated
    population can't be safely compared, so they're excluded from this
    pass entirely -- not flagged as a gap here either, that's a
    separate, deliberate check in _find_candidate_gaps()). Within each
    population group, a candidate pair is any two papers whose outcome
    polarity is opposite."""
    by_population = defaultdict(list)
    for row in rows:
        population = _normalize(row.get("population"))
        if not population:
            continue
        by_population[population].append(row)

    pairs = []
    for population, group in by_population.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                pol_a = _polarity(a.get("outcome"))
                pol_b = _polarity(b.get("outcome"))
                if pol_a and pol_b and pol_a != pol_b:
                    pairs.append({
                        "paper_a": a, "paper_b": b, "population": population,
                        "reason": f"outcome reads {pol_a} vs {pol_b} for the same population",
                    })
    return pairs


def _find_candidate_gaps(rows: list) -> list:
    """Two independent, deterministic coverage checks -- neither is a
    judgment call, both are plain counting: (1) a field left null across
    more than half of all papers, (2) a field with only ONE distinct
    non-null value across every paper (methodology/population monoculture
    -- every source studied the same narrow angle). Skipped entirely
    below MIN_PAPERS_FOR_GAP_CHECK, since "3 out of 3 papers agree" isn't
    a meaningful signal the way "3 out of 30" is."""
    total = len(rows)
    if total < MIN_PAPERS_FOR_GAP_CHECK:
        return []

    gaps = []
    for field in FIELD_NAMES:
        values = [row.get(field) for row in rows]
        non_null = [v for v in values if v not in (None, "", [])]
        if len(non_null) < total / 2:
            gaps.append({
                "field": field,
                "reason": f"'{field}' is missing/unstated in {total - len(non_null)} of {total} papers",
            })
            continue   # already flagged for missingness; don't double-flag the same field
        distinct = {_normalize(v) if isinstance(v, str) else json.dumps(v, sort_keys=True) for v in non_null}
        if field in ("methodology", "population") and len(distinct) == 1:
            gaps.append({
                "field": field,
                "reason": f"every paper with a stated '{field}' reports the same one — no diversity of angle",
            })
    return gaps


def _format_summary(pairs: list, gaps: list) -> str:
    """Plain text, not JSON -- this is what generic_worker.py's context
    concatenation renders directly ahead of contradiction_detector's own
    prompt, so it needs to already read like a briefing, not a data dump
    the LLM has to re-parse."""
    lines = []
    if pairs:
        lines.append(f"{len(pairs)} candidate contradiction pair(s) (population match, opposite outcome polarity):")
        for p in pairs:
            a, b = p["paper_a"], p["paper_b"]
            lines.append(
                f"- [{p['population']}] \"{a.get('title')}\" (outcome: {a.get('outcome')}) "
                f"vs \"{b.get('title')}\" (outcome: {b.get('outcome')})"
            )
    else:
        lines.append("No candidate contradiction pairs found by the deterministic pre-filter.")
    lines.append("")
    if gaps:
        lines.append(f"{len(gaps)} candidate coverage gap(s):")
        for g in gaps:
            lines.append(f"- {g['reason']}")
    else:
        lines.append("No candidate coverage gaps found by the deterministic pre-filter.")
    lines.append("")
    lines.append(
        "These are unverified CANDIDATES from a keyword/counting heuristic, not "
        "confirmed findings -- judge each one on its actual merits; dismiss any "
        "that don't hold up, and note anything genuine this pass missed."
    )
    return "\n".join(lines)


def run(session_id: str = None, tier: int = None, domain: str = None) -> dict:
    table = read(KEYS["extraction_table"])
    rows = (table or {}).get("papers") or []
    if not rows:
        raise MissingDependencyError(required_role="extraction_table_builder")

    workspace_id = _workspace_id()
    pairs = _find_candidate_pairs(rows)
    gaps = _find_candidate_gaps(rows)

    # Part 0 edges: one "possible_contradiction" edge per candidate pair
    # (a distinct free-form relation from "contradicts" -- see
    # eo/graph_edges.py's docstring -- so a later, human- or LLM-confirmed
    # contradiction can use "contradicts" without colliding with an
    # unverified candidate). Skips silently if either paper's node_id is
    # missing (e.g. that node's embed failed upstream, same posture
    # write_node() itself takes toward embed failures).
    edges_written = 0
    for pair in pairs:
        from_id, to_id = pair["paper_a"].get("node_id"), pair["paper_b"].get("node_id")
        if not from_id or not to_id:
            continue
        try:
            create_edge(f"node:{workspace_id}:{from_id}", f"node:{workspace_id}:{to_id}",
                        relation="possible_contradiction", created_by="contradiction_prefilter")
            edges_written += 1
        except ValueError:
            continue

    # Part 0 nodes: one "finding" node per candidate gap, so it's
    # searchable/linkable the same way any other research artifact is.
    gap_node_ids = []
    for gap in gaps:
        node_id = write_node(
            workspace_id=workspace_id, section="research", node_type="finding",
            title=f"Coverage gap: {gap['field']}", content=gap["reason"],
            created_by="contradiction_prefilter", tags=["gap", gap["field"]],
            session_id=session_id, tier=tier,
        )
        if node_id:
            gap_node_ids.append(node_id)

    summary_text = _format_summary(pairs, gaps)
    candidates = {
        "candidate_pairs": [
            {
                "paper_a": p["paper_a"].get("title"), "paper_b": p["paper_b"].get("title"),
                "population": p["population"], "outcome_a": p["paper_a"].get("outcome"),
                "outcome_b": p["paper_b"].get("outcome"), "reason": p["reason"],
            }
            for p in pairs
        ],
        "candidate_gaps": gaps,
        "edges_written": edges_written,
        "gap_node_ids": gap_node_ids,
        "summary": f"{len(pairs)} candidate contradiction pair(s), {len(gaps)} candidate gap(s).",
    }
    write(KEYS["contradiction_candidates"], candidates)

    # The actual hand-off to contradiction_detector (agents/generic_worker.py,
    # via its normal input_keys mechanism) -- see module docstring.
    if session_id:
        write(f"stage_output:{session_id}:contradiction_prefilter", summary_text)

    return candidates


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))