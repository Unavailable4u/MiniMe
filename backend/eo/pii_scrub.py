"""
eo/pii_scrub.py — D3 Part 1 scaffolding. Presidio (PII redaction) wrapper.

This module will hold the shared AnalyzerEngine/AnonymizerEngine pair used
to redact PII from ingested source text before it reaches
source_ingestor.write_ingested_source() — i.e. before anything is embedded
into Upstash Vector or written to the knowledge graph. Wired in by:

  - Part 5: agents/pdf_ingestor.py's `ingest_pdf()` output.
  - Part 6: agents/voice_ingestor.py's `ingest_voice()` output, plus a pytest
    checking a synthetic PII string (fake SSN/email) comes back redacted.

No AnalyzerEngine/AnonymizerEngine is constructed yet — this file is just
the import + smoke-test shell. Requires the en_core_web_lg spaCy model
downloaded once; see eo/README-guardrails-presidio.md before using this
module for real.

Place this file at: eo/pii_scrub.py
"""

from presidio_analyzer import AnalyzerEngine  # noqa: F401  (unused until Part 5)
from presidio_anonymizer import AnonymizerEngine  # noqa: F401  (unused until Part 5)


def scrub(text: str) -> str:
    """
    Placeholder. Later parts will replace this with a real
    AnalyzerEngine().analyze(...) -> AnonymizerEngine().anonymize(...) pair
    using the default entity set (names, emails, phone numbers, credit
    cards, etc.).
    """
    raise NotImplementedError(
        "pii_scrub.scrub() is not wired up yet — this is Part 1 scaffolding. "
        "See eo/README-guardrails-presidio.md and Parts 5-6."
    )


if __name__ == "__main__":
    # Smoke test: confirm both presidio packages import and the spaCy model
    # loads. Does not redact anything yet.
    analyzer = AnalyzerEngine()
    print("presidio-analyzer + presidio-anonymizer: import OK, spaCy model loaded.")
    print("pii_scrub.py: no scrub() logic wired in yet (Part 1 only).")
