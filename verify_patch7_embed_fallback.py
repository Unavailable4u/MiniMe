"""
verify_patch7_embed_fallback.py — run this from your repo root after
applying patch7_embed_fallback.diff and filling in real
HUGGINGFACE_API_KEY_6/_7 values in your .env.

    cd MiniMe
    python verify_patch7_embed_fallback.py

Scope note: this only exercises utils/embedding.py's
embed_text_with_fallback() directly -- NOT the full
memory_search.store_cycle_memory()/duplication_checker.run() paths,
since those also need a live Redis/Upstash session (KEYS reads, app_slug,
vector_index()) that's a separate thing to stand up from the embedding
fallback itself. If you want to test the full path, unset
HUGGINGFACE_API_KEY in your real environment and run an actual cycle,
then check the usage dashboard the same way this script tells you to
below.

Two forced-failure stages, since this is a 3-account chain (unlike the
2-account Gemini chains in patches 4/6):
  Stage 1: pop HUGGINGFACE_API_KEY only -> should land on _6.
  Stage 2: also pop HUGGINGFACE_API_KEY_6 -> should land on _7.
Also checks the returned vector is actually 384-dim -- the one thing
that would silently break Upstash Vector upserts/queries if a fallback
account's model/response shape ever drifted from the primary's, per
utils/embedding.py's own EMBEDDING_MODEL comment.

No "import eo.registry first" workaround needed -- utils.embedding has
zero heavy imports (os, requests only, per its own module docstring),
so importing it directly never touches the
agents.generic_worker/eo.registry chain at all.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

missing = [k for k in ("HUGGINGFACE_API_KEY_6", "HUGGINGFACE_API_KEY_7") if not os.getenv(k)]
if missing:
    raise SystemExit(
        "These Patch-7 keys are not set in your .env yet, fill in at least "
        f"one before running this: {', '.join(missing)}"
    )

from utils.embedding import embed_text_with_fallback, HF_EMBEDDING_KEY_ENVS  # noqa: E402

print("--- HF_EMBEDDING_KEY_ENVS as embed_text_with_fallback() will actually use it ---")
for key_env in HF_EMBEDDING_KEY_ENVS:
    print(" ", key_env, "(set)" if os.getenv(key_env) else "(not set)")

TEST_TEXT = "verify_patch7 test string — checking the embedding fallback chain."


def _check(vector, key_env, expected_key_env, stage_label):
    assert key_env == expected_key_env, (
        f"{stage_label}: expected {expected_key_env} to answer, got {key_env} instead -- "
        "check that the earlier key(s) in the chain are actually invalid/unset, not just "
        "slow (a slow-but-working call wouldn't fall through)."
    )
    assert isinstance(vector, list) and len(vector) == 384, (
        f"{stage_label}: expected a 384-dim vector (matches the Upstash Vector index "
        f"dimension per utils/embedding.py's own comment), got length {len(vector)} instead -- "
        f"if {key_env} is hitting a different embedding model under the hood, this WILL break "
        "upsert()/query() calls against your existing index."
    )
    print(f"{stage_label}: OK — {key_env} answered with a {len(vector)}-dim vector.")


# IMPORTANT: these pops happen AFTER the import above, not before, same
# reasoning as patches 4/5/6's scripts -- utils.embedding itself has no
# load_dotenv() call, but load_dotenv() already ran at the top of this
# script, so order matters less here than in those scripts. Kept in the
# same position anyway for consistency and to avoid the failure mode if
# this script's own load_dotenv() call is ever moved later.
print("\n--- Stage 1: HUGGINGFACE_API_KEY forced unavailable ---")
_real_primary = os.environ.pop("HUGGINGFACE_API_KEY", None)
try:
    vector, key_env = embed_text_with_fallback(TEST_TEXT)
    _check(vector, key_env, "HUGGINGFACE_API_KEY_6", "Stage 1")
finally:
    if _real_primary is not None:
        os.environ["HUGGINGFACE_API_KEY"] = _real_primary

print("\n--- Stage 2: HUGGINGFACE_API_KEY AND _6 forced unavailable ---")
_real_key6 = os.environ.pop("HUGGINGFACE_API_KEY_6", None)
try:
    if _real_primary is not None:
        os.environ.pop("HUGGINGFACE_API_KEY", None)  # keep primary out for this stage too
    vector, key_env = embed_text_with_fallback(TEST_TEXT)
    _check(vector, key_env, "HUGGINGFACE_API_KEY_7", "Stage 2")
finally:
    if _real_primary is not None:
        os.environ["HUGGINGFACE_API_KEY"] = _real_primary
    if _real_key6 is not None:
        os.environ["HUGGINGFACE_API_KEY_6"] = _real_key6

print(
    "\nBoth stages passed. embed_text_with_fallback() correctly falls "
    "through HUGGINGFACE_API_KEY -> _6 -> _7 and every account returns a "
    "vector shape that's actually compatible with your Upstash Vector "
    "index.\n"
    "Check your usage dashboard / Upstash for fresh "
    "usage:huggingface:HUGGINGFACE_API_KEY_6:<today> and "
    "usage:huggingface:HUGGINGFACE_API_KEY_7:<today> entries once you run "
    "a real cycle through memory_search.py/duplication_checker.py -- this "
    "script only proves the chain itself works, not that the two patched "
    "callers are logging against the right key (re-read their diffs in "
    "patch7_embed_fallback.diff if that's in doubt)."
)
