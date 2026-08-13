"""
eo/pii_scrub.py — D3 Part 5. Presidio (PII redaction) wrapper.

Holds the shared AnalyzerEngine/AnonymizerEngine pair used to redact PII
from ingested source text before it reaches
source_ingestor.write_ingested_source() — i.e. before anything is embedded
into Upstash Vector or written to the knowledge graph. Wired in by:

  - Part 5 (this patch): agents/pdf_ingestor.py's `ingest_pdf()` — scrubs
    each page's text individually, before `_join_pages()` runs, and the
    guessed title. Scrubbing is done inside pdf_ingestor.py itself (the
    plan's own "ingest_pdf()'s output" wording), not inside
    source_ingestor.write_ingested_source() — that function is the shared
    node-writing step every Capture ingestor feeds into (pdf, and voice
    next), and it has no per-format knowledge of what "output" means for
    a given ingestor (page boundaries here; something else for voice), so
    scrubbing belongs at the point where each ingestor still knows its own
    shape, not in the generic downstream writer.
  - Part 6: agents/voice_ingestor.py's `ingest_voice()` output, plus a
    pytest checking a synthetic PII string (fake SSN/email) comes back
    redacted from both ingestors.

Why scrub per-page rather than on the joined `content` string: Presidio's
anonymizer replaces each detected entity with a same-meaning but
different-LENGTH placeholder (e.g. "John Smith" -> "<PERSON>"), which
would shift every character offset after it. pdf_ingestor.py's
`_join_pages()` records `page_breaks[i]["start_offset"]`/`"char_count"`
for exactly this kind of downstream consumer (jump-to-page, citations) —
scrubbing each page's text BEFORE `_join_pages()` runs means those offsets
are computed from the already-redacted text and stay accurate, instead of
scrubbing the joined string afterward and silently invalidating every
offset past the first redaction.

No explicit `entities=` list is passed to `analyzer.analyze()` — Presidio
runs its full built-in recognizer set by default (covers names, emails,
phone numbers, credit cards, SSNs, IP addresses, IBANs, and more), which
is what the D3 plan's "default entity set" means; a caller that wants a
narrower/wider set can still pass `entities=` through `scrub()`.

Requires the en_core_web_lg spaCy model downloaded once; see
eo/README-guardrails-presidio.md.

Place this file at: eo/pii_scrub.py
"""

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

_analyzer = None
_anonymizer = None


def _get_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    """Lazily built, then reused across calls — AnalyzerEngine construction
    loads the spaCy model, which is expensive enough (real disk + model
    init cost) that it's not worth paying per page/section in an ingestion
    loop, same "build once, reuse" reasoning eo/output_guard.py's
    get_code_guard() / get_answer_guard() / get_artifact_guard() already
    apply to their own Guards."""
    global _analyzer, _anonymizer
    if _analyzer is None:
        _analyzer = AnalyzerEngine()
    if _anonymizer is None:
        _anonymizer = AnonymizerEngine()
    return _analyzer, _anonymizer


def scrub(text: str, entities: list[str] | None = None) -> str:
    """Redacts PII from `text` using Presidio's AnalyzerEngine ->
    AnonymizerEngine pair. Returns `text` unchanged (not an error) when it's
    empty/whitespace-only, since that's a normal case for a blank PDF page
    (see pdf_ingestor.py's _extract_page_text()) rather than something
    worth spending an analyze() call on.

    entities: optional allow-list of Presidio entity types (e.g.
    ["EMAIL_ADDRESS", "PHONE_NUMBER"]) to restrict detection to. None
    (the default) means Presidio's full built-in recognizer set — see
    this module's docstring for why that's the right default here.

    Anonymized in place with Presidio's own default per-entity
    replacement strings (e.g. "<PERSON>", "<EMAIL_ADDRESS>") rather than a
    single generic "[REDACTED]" — keeping the entity type visible in the
    replacement is what lets a reader (or a future re-identification
    audit) tell a redacted name from a redacted SSN without re-running
    analysis.
    """
    if not text or not text.strip():
        return text
    analyzer, anonymizer = _get_engines()
    results = analyzer.analyze(text=text, language="en", entities=entities)
    if not results:
        return text
    anonymized = anonymizer.anonymize(text=text, analyzer_results=results)
    return anonymized.text


if __name__ == "__main__":
    sample = (
        "Please reach out to Jane Doe at jane.doe@example.com or "
        "555-123-4567. Her SSN on file is 123-45-6789."
    )
    redacted = scrub(sample)
    print(f"original:  {sample}")
    print(f"redacted:  {redacted}")
    assert "Jane Doe" not in redacted
    assert "jane.doe@example.com" not in redacted
    assert "123-45-6789" not in redacted
    print("pii_scrub.py: scrub() redacted all three PII samples as expected.")

    empty_ok = scrub("   ")
    print(f"blank input passthrough -> {empty_ok!r}")
