"""
m2c/src/m2c/trust/ -- Patch C26: folder trust.

A project folder is attacker-controlled content (a cloned repository).  A few of its files are configuration with power:
project settings (they can allow commands without asking), hooks (they run commands), skills (they add instructions the model
follows).  Nothing in them is used until the person has said "I trust this folder", and the answer holds only while those
files stay exactly as they were.

    fingerprint.py  a SHA-256 over the covered files, and what makes a folder untrustworthy (symlinks, too many files)
    store.py        ``<M2C home>/trust.json`` (0600, atomic) and ``project_trust_state(root, store)``: the decision
    prompt.py       the question as a pure key state machine, its text, and the "ignored" status note

The terminal chat (tui/session.py) evaluates the state once at startup, asks when ``needs_prompt``, and only then loads the
project layer of the policy rules.  Headless mode never asks: it will use ``project_trust_state(...).trusted`` as it is.
C13 (hooks) and C14 (skills) ask the same question through the session's ``project_trusted``.

Standard library only.
"""
from .fingerprint import (
    MAX_DEPTH, MAX_ENTRIES, MAX_FILES, MAX_TOTAL_BYTES, TRUST_ROOTS, Fingerprint, compute_fingerprint, fingerprint_matches,
)
from .prompt import Answer, Pending, TrustPromptMachine, describe_prompt, ignored_note
from .store import (
    REASONS, TrustEntry, TrustState, TrustStore, TrustStoreError, canonical_root, project_trust_state,
)

__all__ = [
    "Answer", "Fingerprint", "MAX_DEPTH", "MAX_ENTRIES", "MAX_FILES", "MAX_TOTAL_BYTES", "Pending", "REASONS", "TRUST_ROOTS",
    "TrustEntry", "TrustPromptMachine", "TrustState", "TrustStore", "TrustStoreError", "canonical_root", "compute_fingerprint",
    "describe_prompt", "fingerprint_matches", "ignored_note", "project_trust_state",
]
