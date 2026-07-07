"""
utils/similarity.py — Shared fuzzy-match primitive for deduplication.

Extracted from agents/review_aggregator.py and agents/security_aggregator.py
(Migration Part 26 §4b), which had independently reimplemented the same
"max(word-level Jaccard overlap, character-level SequenceMatcher ratio)"
approach for deciding whether two differently-worded descriptions describe
the same underlying issue.

Deliberately NOT merged here: each caller's actual dedupe/merge loop
(review_aggregator.py's aggregate_reviews() vs. security_aggregator.py's
_dedupe_findings()/_merge_pair()) stays in its own file, since they build
different output shapes (issues-by-module with flagged_by_count vs. a
flat findings list with _merged_count) -- only the similarity *scoring*
itself was truly duplicated logic.

Each caller also keeps its own hand-tuned STOPWORDS set and
SIMILARITY_THRESHOLD (0.35 for review_aggregator.py, 0.42 for
security_aggregator.py -- both tuned against real examples per their own
comments). This module takes both as parameters rather than imposing one
generic list/threshold, so a future "improvement" here can't silently
drift two independently-tuned thresholds at once.
"""
import re
from difflib import SequenceMatcher


def tokenize(text: str, stopwords: set, stem: bool = False, min_len: int = 2) -> set:
    """Word-level tokenization with stopword filtering, and optional light
    suffix-stemming (so "raises"/"raise", "validating"/"validate" count as
    the same token -- crude but cheap, good enough for "close enough", not
    correct linguistics).

    stem=True matches review_aggregator.py's original _tokenize() (which
    stemmed and allowed apostrophes/underscores in tokens via `[a-z0-9_']+`).
    stem=False matches security_aggregator.py's original _tokens() (no
    stemming, plain `[a-z0-9]+`).

    min_len=2 (drop tokens of length <= 2) matches both callers' original
    behavior exactly.
    """
    pattern = r"[a-z0-9_']+" if stem else r"[a-z0-9]+"
    words = re.findall(pattern, text.lower())
    tokens = set()
    for w in words:
        if w in stopwords or len(w) <= min_len:
            continue
        if stem:
            for suffix in ("ing", "ed", "es", "s"):
                if w.endswith(suffix) and len(w) > len(suffix) + 2:
                    w = w[: -len(suffix)]
                    break
        tokens.add(w)
    return tokens


def similarity(a: str, b: str, stopwords: set, stem: bool = False) -> float:
    """max(word-level Jaccard overlap, character-level SequenceMatcher
    ratio) -- catches both "reworded but same vocabulary" (Jaccard's
    strength) and "near-identical phrasing, different words" (SequenceMatcher's
    strength) without either signal alone missing cases the other would catch.

    If either side tokenizes to nothing, this returns the char-level ratio
    alone. That's a no-op unification, not a behavior change: both original
    implementations were already equivalent to this --
    review_aggregator.py short-circuited to `SequenceMatcher(...).ratio()`
    in that case, and security_aggregator.py fed `jaccard = 0.0` into
    `max(jaccard, ratio)`, which is the same result since ratio is always
    >= 0.
    """
    ta = tokenize(a, stopwords, stem=stem)
    tb = tokenize(b, stopwords, stem=stem)
    char_ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    if not ta or not tb:
        return char_ratio
    jaccard = len(ta & tb) / len(ta | tb)
    return max(jaccard, char_ratio)