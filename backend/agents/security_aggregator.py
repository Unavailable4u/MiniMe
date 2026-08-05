"""
agents/security_aggregator.py — Security Scan Aggregator (new; not one of
the original 19, same category as agents/review_aggregator.py: "agent #8,
deterministic Python, no LLM").

Closes a gap found via testing, not on the original blueprint list:
Scanner Pool (security_scanner.py) assigns ONE module per worker
(round-robin across 5 Cloudflare slots) -- unlike Reviewer Pool, where 3
workers redundantly look at ALL code and need de-duplication ACROSS
workers. Scanner Pool has no cross-worker duplication problem. What it
does have: a single Cloudflare call, for a single module, sometimes
listing the same underlying vulnerability twice in its own `findings`
list, phrased two different ways (e.g. "hardcoded secret in config" and
"API key exposed in source" for the same line of code). Nothing in
security_scanner.py de-duplicates a module's own findings list -- this
agent is that step.

HONEST LIMITATION, stated up front rather than glossed over: this is a
deterministic, no-LLM merge, so it can only catch duplicates that share
enough vocabulary to be detected by word-overlap -- see utils/similarity.py.
Two findings describing the same bug in completely disjoint wording (no
shared significant words at all) will NOT be caught. This trades recall
for the same "cheap and auditable" property review_aggregator.py
presumably already trades for on the Reviewer Pool side -- an LLM-based
semantic merge would catch more, at the cost of a 6th API call per module
and a new source of hallucination on top of the scan itself. Revisit this
tradeoff if false-negative duplicates (missed merges) turn out to be more
common than false positives (wrongly merged, genuinely distinct findings)
once this runs against real output.

Migration Part 26 §4b: the actual similarity scoring (tokenize + Jaccard/
SequenceMatcher) used to be reimplemented here nearly identically to
agents/review_aggregator.py's copy -- now shared via utils/similarity.py.
This file keeps its own SIMILARITY_THRESHOLD and _STOPWORDS (tuned against
measured examples, per the original comment) and passes them in
explicitly; see utils/similarity.py's docstring for why they weren't
collapsed into one shared list/threshold too.

Input:  KEYS["security_scan_results"] -- security_scanner.py's raw output,
        {module_name: {"findings": [...], ["error": "..."]}}
Output: KEYS["security_scan_results"] -- same key, overwritten in place
        (downstream consumers -- file_manager.py's report path, tier-2's
        "security_scan" route -- read this key either way, so this stays
        a drop-in step rather than requiring every reader to know about a
        second key).
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.similarity import similarity as _fuzzy_similarity

# Ordered worst-to-... no, best-to-worst is more useful here: higher number
# wins when merging two findings' severities.
_SEVERITY_RANK = {"critical": 3, "moderate": 2, "minor": 1}

# Similarity threshold above which two findings in the SAME module are
# treated as describing the same underlying issue. Tuned against measured
# examples, not guessed: a genuine duplicate pair ("hardcoded API key" /
# "hardcoded secret key... credential exposure risk") scored 0.48; a
# genuinely distinct pair ("SQL injection" / "unclosed DB connection")
# scored 0.37. 0.42 sits in that gap. Revisit if real output shows this
# threshold merging things it shouldn't, or missing things it should.
SIMILARITY_THRESHOLD = 0.42

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "to", "of", "in", "on",
    "for", "and", "or", "with", "as", "by", "at", "from", "not", "no",
    "if", "there", "which", "may", "can", "could", "should", "would",
}


def _similarity(a: str, b: str) -> float:
    # stem=False matches this module's original _tokens() behavior (no
    # suffix-stemming, plain [a-z0-9]+ tokenization).
    return _fuzzy_similarity(a, b, _STOPWORDS, stem=False)


def _merge_pair(kept: dict, incoming: dict) -> dict:
    """Combines two findings judged to be duplicates. Keeps the
    higher-ranked severity (worse-case wins -- never silently downgrade a
    critical because a differently-worded duplicate called it moderate)
    and the longer description (more detail, not less)."""
    kept_rank = _SEVERITY_RANK.get(kept.get("severity", "minor"), 1)
    new_rank = _SEVERITY_RANK.get(incoming.get("severity", "minor"), 1)
    severity = kept["severity"] if kept_rank >= new_rank else incoming["severity"]
    description = kept["description"] if len(kept.get("description", "")) >= len(incoming.get("description", "")) else incoming["description"]
    merged = dict(kept)
    merged["severity"] = severity
    merged["description"] = description
    merged["_merged_count"] = kept.get("_merged_count", 1) + 1
    return merged


def _dedupe_findings(findings: list) -> tuple:
    """Returns (deduped_list, merge_count). O(n^2) in findings per module,
    which is fine -- a single module's findings list is small (single
    digits in practice), never pool-sized."""
    kept = []
    for finding in findings:
        if not isinstance(finding, dict) or "description" not in finding:
            kept.append(finding)  # malformed entry -- pass through, not this agent's job to fix shape
            continue
        merged_into_existing = False
        for i, existing in enumerate(kept):
            if not isinstance(existing, dict) or "description" not in existing:
                continue
            if _similarity(existing["description"], finding["description"]) >= SIMILARITY_THRESHOLD:
                kept[i] = _merge_pair(existing, finding)
                merged_into_existing = True
                break
        if not merged_into_existing:
            kept.append(finding)
    merge_count = len(findings) - len(kept)
    # Strip the internal bookkeeping field before returning -- callers
    # downstream of this agent don't need to know a finding was merged,
    # only that it no longer appears twice.
    for f in kept:
        if isinstance(f, dict):
            f.pop("_merged_count", None)
    return kept, merge_count


def run(session_id: str = None, tier: int = None) -> dict:
    scan_results = read(KEYS["security_scan_results"], default={})
    if not scan_results:
        return scan_results

    total_merged = 0
    aggregated = {}
    for module_name, result in scan_results.items():
        if not isinstance(result, dict):
            aggregated[module_name] = result
            continue
        findings = result.get("findings", [])
        deduped, merge_count = _dedupe_findings(findings)
        total_merged += merge_count
        new_result = dict(result)
        new_result["findings"] = deduped
        aggregated[module_name] = new_result
        if merge_count:
            print(f"  [Security Aggregator] {module_name}: merged {merge_count} "
                  f"duplicate finding(s) ({len(findings)} -> {len(deduped)})")

    write(KEYS["security_scan_results"], aggregated)
    return aggregated


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))