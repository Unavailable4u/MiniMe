"""
agents/review_aggregator.py — Review Aggregator (Part 4, agent #8).

Deterministic Python, no LLM call, by design (same reasoning as
file_manager.py) -- merging 3 independent opinions into one verdict is a
mechanical operation, not a judgment call, so it stays out of model hands.

Called internally by reviewer.py after the 3-parallel Reviewer Pool
finishes; not wired into loop.py directly.

Dedupe fix: exact-string (module, description) matching under-merges in
practice, since 3 independent reviewers phrase the same bug differently
("references an undefined global variable" vs. "ignores its 'todos'
parameter" -- same bug, zero string overlap on a strict key). Switched to
fuzzy matching within each module -- still fully deterministic stdlib
Python, no LLM call, keeping this module's original design constraint
intact.

Migration Part 26 §4b: the actual similarity scoring (tokenize + Jaccard/
SequenceMatcher) used to be reimplemented here nearly identically to
agents/security_aggregator.py's copy -- now shared via utils/similarity.py.
This file keeps its own SIMILARITY_THRESHOLD and _STOPWORDS (hand-tuned
against real examples, per the original comment) and passes them in
explicitly; see utils/similarity.py's docstring for why they weren't
collapsed into one shared list/threshold too.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.similarity import similarity as _fuzzy_similarity

SIMILARITY_THRESHOLD = 0.35

_STOPWORDS = {
    "a", "an", "the", "is", "are", "this", "that", "and", "or", "to", "of",
    "in", "on", "for", "with", "without", "which", "can", "when", "not",
    "its", "it's", "instead", "leading", "before", "calling", "uses", "use",
}


def _similarity(a: str, b: str) -> float:
    # stem=True matches this module's original _tokenize() behavior
    # (light suffix-stemming so "raises"/"raise" count as the same token).
    return _fuzzy_similarity(a, b, _STOPWORDS, stem=True)


def aggregate_reviews(member_reviews: list) -> dict:
    """
    member_reviews: list of up to 3 dicts, each shaped like
        {"issues": [{"module": ..., "severity": ..., "description": ...}, ...],
         "summary": "..."}
    (one per Reviewer Pool worker; a worker that failed entirely should be
    excluded from this list by the caller before it gets here.)

    Returns one merged dict in the same shape, which is what
    fixer_tester.py already expects from KEYS["review_notes"].

    Aggregation rule:
    - Issues are grouped by module, then fuzzy-matched by description
      (SIMILARITY_THRESHOLD) -- similar-enough descriptions within the
      same module are treated as the same underlying issue, not just
      exact string matches.
    - If the same underlying issue was flagged by multiple members at
      different severities, keep the highest severity seen (critical >
      moderate > minor) -- never silently downgrade a real bug because
      one of the three reviewers missed it or phrased it more mildly.
    - Each merged issue also carries flagged_by_count -- how many of the
      (up to 3) reviewers independently raised it. An issue all 3
      reviewers agreed on is a stronger signal than one only 1 caught;
      this makes that visible downstream instead of flattening it away.
    - Summary is the concatenation of each member's one-line verdict,
      labeled by member index, so a human can see where reviewers agreed
      or disagreed.
    """
    severity_rank = {"critical": 3, "moderate": 2, "minor": 1}

    # module -> list of merged-issue dicts (each also tracks its own
    # description so later issues in the same module can be compared
    # against it for fuzzy matching).
    by_module = {}
    summaries = []

    for i, review in enumerate(member_reviews):
        if not review:
            continue

        summary = review.get("summary", "").strip()
        if summary:
            summaries.append(f"Reviewer {i + 1}: {summary}")

        for issue in review.get("issues", []):
            module = issue.get("module", "unknown")
            description = issue.get("description", "").strip()
            severity = issue.get("severity", "minor")
            if not description:
                continue

            bucket = by_module.setdefault(module, [])

            # Look for an existing merged issue in this module similar
            # enough to be the same underlying bug.
            match = None
            for existing in bucket:
                if _similarity(existing["description"], description) >= SIMILARITY_THRESHOLD:
                    match = existing
                    break

            if match is None:
                bucket.append({
                    "module": module,
                    "severity": severity,
                    "description": description,
                    "flagged_by_count": 1,
                })
            else:
                match["flagged_by_count"] += 1
                existing_rank = severity_rank.get(match["severity"], 0)
                new_rank = severity_rank.get(severity, 0)
                if new_rank > existing_rank:
                    match["severity"] = severity
                # Keep the longer/more detailed description of the two --
                # a more specific restatement is more useful downstream
                # than whichever one happened to arrive first.
                if len(description) > len(match["description"]):
                    match["description"] = description

    merged_issues = [issue for bucket in by_module.values() for issue in bucket]

    return {
        "issues": merged_issues,
        "summary": " | ".join(summaries) if summaries else "No reviewers returned usable output.",
    }


if __name__ == "__main__":
    # Quick manual smoke test -- includes both an exact-match case and a
    # reworded-but-same-bug case, to confirm both dedupe correctly.
    example = [
        {"issues": [
            {"module": "auth", "severity": "moderate", "description": "no input validation"},
            {"module": "todo_api", "severity": "critical", "description": "references an undefined global variable 'storage', causing a NameError"},
        ], "summary": "Mostly fine, a couple issues."},
        {"issues": [
            {"module": "auth", "severity": "critical", "description": "no input validation"},
            {"module": "todo_api", "severity": "moderate", "description": "delete_all ignores its 'todos' parameter and uses undefined global 'storage' instead"},
        ], "summary": "Found a critical bug."},
        {"issues": [], "summary": "Looks clean to me."},
    ]
    import json
    print(json.dumps(aggregate_reviews(example), indent=2))