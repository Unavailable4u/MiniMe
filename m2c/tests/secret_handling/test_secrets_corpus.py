"""
m2c/tests/secret_handling/test_secrets_corpus.py -- Patch C15: the false-positive budget, stated and enforced.

THE BUDGET
----------
Redaction has two ways to be wrong.  A false negative leaks a credential to the server; it is the failure this whole
patch exists to prevent, so the budget for it is ZERO on every kind the detectors claim: the secret corpus below
(built at run time from random pieces, 30 kinds x 40 seeds) must be caught 100 %.  A false positive hides a harmless
span from the model, which then has to ask the person for it: annoying, never dangerous.  So the budget for it is a
ceiling, not zero, and it is set from how often a developer's tool output is actually hit:

* ``FALSE_POSITIVE_LINE_BUDGET = 0.5 %``: at most one line in two hundred of the ordinary-text corpus may be changed.
  The corpus is deliberately hard (about a third of its lines contain a word like ``password`` or ``token``), so
  real output, which is mostly logs and source with far fewer such words, is hit much less often than that.
* Git commit hashes (any length), SHA-256 digests and UUIDs are NEVER redacted, anywhere in the ordinary text
  (zero tolerance, asserted per category): the model needs them to read ``git log``, lock files and traces.

The categories of ordinary text: git hashes and logs, checksums, UUIDs (also next to credential words), base64 images
(also next to credential words), lock files, minified code, long identifiers, source code full of credential-shaped
words, logs and traces, and config files.  See ``secret_fakes.ordinary_corpus``.
"""
from __future__ import annotations

import pytest
from secret_fakes import BUILDERS, leaked, make, ordinary_corpus

from m2c.secrets.redact import redact

FALSE_POSITIVE_LINE_BUDGET = 0.005
ZERO_TOLERANCE = ["git_hashes", "git_log", "sha256_checksums", "uuids", "uuid_near_keywords", "hash_near_keywords",
                  "base64_images", "base64_images_near_keywords", "lock_files", "minified_code", "long_identifiers",
                  "logs_and_traces", "config_files"]
SEED_COUNT = 40


def _lines(items):
    for item in items:
        yield from item.split("\n")


def test_the_corpus_is_big_enough_to_mean_something():
    corpus = ordinary_corpus()
    total = sum(len(list(_lines(v))) for v in corpus.values())
    assert total >= 450
    assert set(ZERO_TOLERANCE) <= set(corpus)


@pytest.mark.parametrize("category", ZERO_TOLERANCE)
def test_hashes_uuids_images_locks_minified_code_and_identifiers_are_never_touched(category):
    for item in ordinary_corpus()[category]:
        result = redact(item)
        assert result.text == item and result.findings == (), item[:80]


def test_the_false_positive_rate_on_ordinary_developer_text_is_within_budget():
    corpus = ordinary_corpus()
    total = changed = 0
    offenders = []
    for category, items in corpus.items():
        for line in _lines(items):
            total += 1
            if redact(line).text != line:
                changed += 1
                offenders.append((category, line[:70]))
    rate = changed / total
    assert rate <= FALSE_POSITIVE_LINE_BUDGET, f"{changed}/{total} = {rate:.2%} lines changed: {offenders}"


def test_whole_documents_are_within_budget_too_not_only_single_lines():
    corpus = ordinary_corpus()
    document = "\n".join(line for items in corpus.values() for line in _lines(items))
    out = redact(document).text
    before, after = document.split("\n"), out.split("\n")
    assert len(before) == len(after)
    changed = sum(1 for a, b in zip(before, after) if a != b)
    assert changed / len(before) <= FALSE_POSITIVE_LINE_BUDGET


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_no_secret_of_any_kind_is_missed_over_many_random_seeds(name):
    for seed in range(SEED_COUNT):
        fake = make(name, 1000 + seed)
        result = redact(fake.text)
        assert not leaked(result.text, fake.secret), f"{name} seed {seed}: a secret survived"


def test_secrets_are_caught_inside_ordinary_text_not_only_alone():
    corpus = ordinary_corpus()
    filler = [line for items in corpus.values() for line in _lines(items)]
    for i, name in enumerate(sorted(BUILDERS)):
        fake = make(name, 77)
        lines = filler[i * 7:i * 7 + 12] + fake.text.split("\n") + filler[i * 7 + 12:i * 7 + 20]
        out = redact("\n".join(lines)).text
        assert not leaked(out, fake.secret), name
