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
PROMPT_GUARD_CHAIN = [
    {"provider": "groq", "model": PROMPT_GUARD_MODEL, "key_env": "GROQ_API_KEY"},
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


def score_snippet(text: str, source_label: str = "") -> dict:
    """Returns {"flagged": bool, "reason": str}. `flagged=True` means
    the classifier scored this text as containing injected
    instructions rather than ordinary content -- caller decides what to
    do with that (see filter_snippets() below for the common case: log
    + optionally drop before it reaches the real generation call).

    Deliberately NOT raising on any failure -- a classifier hiccup
    degrades to "not flagged," same fail-open contract as
    eo/output_guard.py's validate_*() functions.
    """
    if not text or not text.strip():
        return {"flagged": False, "reason": ""}
    if not os.environ.get("GROQ_API_KEY"):
        return {"flagged": False, "reason": "GROQ_API_KEY not set — guard skipped"}
    try:
        # prompt-guard models are single-label classifiers, not chat
        # models -- Groq serves them behind the same /chat/completions
        # shape generate_text() already calls, with the classification
        # label as the completion text (documented behavior: response
        # text is one of "BENIGN" / "INJECTION" / "JAILBREAK").
        raw = generate_text(
            system_prompt="",   # prompt-guard models classify the user_content directly
            user_content=text[:4000],   # classifier's own context window is small; snippets this module sees are short anyway
            chain=PROMPT_GUARD_CHAIN,
            agent_name="injection_guard",
            allow_continuation=False,
        )
        label = (raw or "").strip().upper()
        flagged = label in ("INJECTION", "JAILBREAK")
        return {"flagged": flagged, "reason": label if flagged else ""}
    except Exception as exc:
        print(f"  [injection_guard] classification failed for "
              f"{source_label or 'snippet'} (fail-open, treating as "
              f"not flagged): {exc.__class__.__name__}: {exc}")
        return {"flagged": False, "reason": f"guard error: {exc}"}


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
