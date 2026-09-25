"""
eo/injection_guard.py — Patch 12. Scores scraped/external text for
prompt-injection or jailbreak attempts before it's interpolated into an
LLM prompt, using Groq's meta-llama/llama-prompt-guard-2-86m (the
larger, more accurate of the two prompt-guard sizes Groq serves free-
tier: RPM 30 / RPD 14.4K / TPM 15K / TPD 500K -- comfortably above this
module's real call volume, since it's one short classification call per
scraped chunk, not per generation).

Why this exists: component_spec_lookup.py, part_price_finder.py, and
web_researcher.py all pull raw text from the open web (DigiKey/Mouser
listings, ST/vendor datasheets, Tavily/Exa search snippets) straight
into an LLM prompt with zero filtering. A scraped page containing
hidden instructions ("ignore previous instructions and...") lands in
that prompt exactly like real product data would. This is a purpose-
built classifier for exactly that pattern, run as a cheap pre-filter,
not a hard blocker -- see FLAG_ONLY below for why a false positive here
should never silently delete real product data.
"""
import os

from utils.llm_client import generate_text

PROMPT_GUARD_MODEL = "meta-llama/llama-prompt-guard-2-86m"
# Bug fix (2026-08-27, injection-guard audit): this step had no explicit
# max_tokens, so every call fell through to llm_client._max_tokens_for()'s
# flat DEFAULT_MAX_TOKENS (8192) -- but llama-prompt-guard-2-86m is an
# 86M-param single-label classifier whose actual Groq-enforced ceiling is
# 512 ("max_tokens must be less than or equal to 512" per the API's own
# 400 response). Every single call was therefore malformed from the
# start and failed before it ever reached the model, meaning this guard
# has never actually screened anything -- score_snippet()'s fail-open
# except-clause below silently treats every failure as "not flagged," so
# scraped web content has been passing through completely unchecked. The
# model only ever needs to emit one label word ("BENIGN"/"INJECTION"/
# "JAILBREAK"), so a small explicit value both fixes the 400 and keeps
# this cheap classification call cheap.
PROMPT_GUARD_MAX_TOKENS = 8
PROMPT_GUARD_CHAIN = [
    {"provider": "groq", "model": PROMPT_GUARD_MODEL, "key_env": "GROQ_API_KEY",
     "max_tokens": PROMPT_GUARD_MAX_TOKENS},
]

# Fail-open by design: a classifier outage (Groq down, key missing,
# malformed response) should never block a real DigiKey/Tavily lookup
# from reaching the LLM it was already headed to -- the injection-guard
# call is defense in depth, not a hard dependency this pipeline should
# break on. Same "never raises, caller decides" posture eo/output_guard.py
# already established for its own three choke points.
_FLAG_ONLY = True  # keep True until you've watched false-positive rate
                    # on real traffic; a hard block (drop the snippet
                    # entirely) is a one-line change once you trust it.

# Audit fix 2026-09-25: this guard has never actually screened anything.
# llama-prompt-guard-2-86m's context window is 512 TOKENS, and every call
# sent up to 4,000 CHARACTERS (roughly 1,000+ tokens for real prose) --
# Groq rejected every single one with "Please reduce the length of the
# messages or completion" (a 400, silently swallowed by the fail-open
# except-clause below as "not flagged"). ~3.2 chars/token is a safe average
# for English text; 1,200 chars keeps each chunk comfortably under the
# 512-token ceiling with room for the label token(s) in the completion.
_CHUNK_CHARS = 1200
# A scraped page can be very long; screening the whole thing chunk-by-chunk
# would turn one "cheap pre-filter" call into dozens. This is a defense-in-
# depth pass, not a full-document audit, so cap how much of any one snippet
# gets classified -- injected instructions are typically front-loaded (they
# need to be seen before the real content) or, if not, are still on a page
# whose EARLY chunks already carry plenty of signal.
_MAX_CHUNKS_PER_SNIPPET = 4


def _classify_chunk(chunk: str) -> str:
    """One raw classification call for a single, already-sized chunk. Returns
    the upper-cased label text ("BENIGN"/"INJECTION"/"JAILBREAK"/""), letting
    any exception propagate to the caller's own try/except."""
    raw = generate_text(
        system_prompt="",   # prompt-guard models classify the user_content directly
        user_content=chunk,
        chain=PROMPT_GUARD_CHAIN,
        agent_name="injection_guard",
        allow_continuation=False,
    )
    return (raw or "").strip().upper()


def score_snippet(text: str, source_label: str = "") -> dict:
    """Returns {"flagged": bool, "reason": str}. `flagged=True` means the
    classifier scored ANY chunk of this text as containing injected
    instructions rather than ordinary content -- caller decides what to do
    with that (see filter_snippets() below for the common case: log +
    optionally drop before it reaches the real generation call).

    Audit fix 2026-09-25: text longer than the classifier's real 512-token
    window is split into _CHUNK_CHARS-sized chunks (see that constant's own
    comment) instead of being truncated to a length that still overflowed
    the model on anything but very short snippets. The whole snippet is
    flagged if any chunk is.

    Deliberately NOT raising on any failure -- a classifier hiccup degrades
    to "not flagged" for that chunk, same fail-open contract as
    eo/output_guard.py's validate_*() functions.
    """
    if not text or not text.strip():
        return {"flagged": False, "reason": ""}
    if not os.environ.get("GROQ_API_KEY"):
        return {"flagged": False, "reason": "GROQ_API_KEY not set — guard skipped"}
    chunks = [text[i:i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)]
    chunks = chunks[:_MAX_CHUNKS_PER_SNIPPET] or [text[:_CHUNK_CHARS]]
    for chunk in chunks:
        try:
            label = _classify_chunk(chunk)
        except Exception as exc:
            print(f"  [injection_guard] classification failed for "
                  f"{source_label or 'snippet'} (fail-open for this chunk, "
                  f"treating as not flagged): {exc.__class__.__name__}: {exc}")
            continue
        if label in ("INJECTION", "JAILBREAK"):
            return {"flagged": True, "reason": label}
    return {"flagged": False, "reason": ""}


def filter_snippets(snippets: list, text_key: str = "snippet",
                     label_key: str = "url") -> list:
    """Convenience wrapper for the common case in web_researcher.py /
    part_price_finder.py: given a list of {"url"/"title", "snippet",
    ...} dicts, scores each snippet and either drops it (_FLAG_ONLY=False)
    or leaves it in place with a "_injection_flagged": True marker
    (_FLAG_ONLY=True) that callers can choose to act on (e.g. exclude
    flagged snippets from the extraction prompt while still logging
    they existed, or surface them in a report). Never raises; a
    scoring failure on one snippet doesn't drop the rest of the batch.
    """
    out = []
    for s in snippets:
        text = s.get(text_key, "")
        label = s.get(label_key, "")
        result = score_snippet(text, source_label=label)
        if result["flagged"]:
            print(f"  [injection_guard] flagged snippet from {label!r}: "
                  f"{result['reason']}")
            if _FLAG_ONLY:
                out.append({**s, "_injection_flagged": True})
                continue
            else:
                continue  # dropped entirely
        out.append(s)
    return out
