"""
agents/review_aggregator.py — Review Aggregator (Part 4, agent #8).

Deterministic Python, no LLM call, by design (same reasoning as
file_manager.py) -- merging 3 independent opinions into one verdict is a
mechanical operation, not a judgment call, so it stays out of model hands.

Called internally by reviewer.py after the 3-parallel Reviewer Pool
finishes; not wired into loop.py directly.
"""


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
    - Union of all issues across members, deduped on (module, description).
    - If the same (module, description) pair was flagged by multiple
      members at different severities, keep the highest severity seen
      (critical > moderate > minor) -- never silently downgrade a real bug
      because one of the three reviewers missed it.
    - Summary is the concatenation of each member's one-line verdict,
      labeled by member index, so a human can see where reviewers agreed
      or disagreed.
    """
    severity_rank = {"critical": 3, "moderate": 2, "minor": 1}

    merged_issues = {}
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

            dedupe_key = (module, description)
            existing = merged_issues.get(dedupe_key)

            if existing is None:
                merged_issues[dedupe_key] = {
                    "module": module,
                    "severity": severity,
                    "description": description,
                }
            else:
                existing_rank = severity_rank.get(existing["severity"], 0)
                new_rank = severity_rank.get(severity, 0)
                if new_rank > existing_rank:
                    existing["severity"] = severity

    return {
        "issues": list(merged_issues.values()),
        "summary": " | ".join(summaries) if summaries else "No reviewers returned usable output.",
    }


if __name__ == "__main__":
    # Quick manual smoke test
    example = [
        {"issues": [{"module": "auth", "severity": "moderate", "description": "no input validation"}],
         "summary": "Mostly fine, one moderate issue."},
        {"issues": [{"module": "auth", "severity": "critical", "description": "no input validation"}],
         "summary": "Found a critical bug."},
        {"issues": [], "summary": "Looks clean to me."},
    ]
    import json
    print(json.dumps(aggregate_reviews(example), indent=2))