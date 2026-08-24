"""
tests/integration/test_pii_scrub_ingestors.py — D3 Part 6.

Confirms eo/pii_scrub.py's scrub() is actually wired into both
agents/pdf_ingestor.py (D3 Part 5) and agents/voice_ingestor.py (D3
Part 6): feed each ingestor a synthetic PII string (fake SSN + email)
and check it comes back redacted in the returned artifact, rather than
re-testing scrub() itself (already covered by pii_scrub.py's own
__main__ smoke test).

agents/voice_ingestor.py's transcription is not under test here --
faster-whisper's WhisperModel is swapped for a fake via monkeypatch so
this stays a fast, offline test instead of depending on a real audio
file and a real (if small) model. agents/pdf_ingestor.py has no such
dependency to fake -- pdfplumber only needs a real, valid PDF, so one
is built on the fly with reportlab (already a pinned dependency; see
requirements.txt).
"""
from reportlab.pdfgen import canvas

from agents import voice_ingestor
from agents.pdf_ingestor import ingest_pdf
from agents.voice_ingestor import ingest_voice

# NOTE: 123-45-6789 (the "obvious fake" SSN used in most examples) is
# deliberately excluded by presidio_analyzer.predefined_recognizers
# .UsSsnRecognizer.invalidate_result() as a canonical published
# sample/placeholder -- along with 987-65-4320 and 078-05-1120 -- so it
# is NEVER redacted, on purpose, regardless of scrub()'s own behavior.
# 245-71-8390 is an equally synthetic, non-canonical value that doesn't
# hit that exclusion and is used here instead.
PII_TEXT = "Contact Jane Doe at jane.doe@example.com. SSN: 245-71-8390."


def _make_pdf(path: str, text: str) -> None:
    c = canvas.Canvas(path)
    c.drawString(72, 720, text)
    c.save()


def test_ingest_pdf_redacts_pii(tmp_path):
    pdf_path = str(tmp_path / "note.pdf")
    _make_pdf(pdf_path, PII_TEXT)

    artifact = ingest_pdf(pdf_path)
    content = artifact["sections"][0]["content"]

    assert "Jane Doe" not in content
    assert "jane.doe@example.com" not in content
    assert "245-71-8390" not in content


class _FakeSegment:
    """Minimal stand-in for a faster_whisper transcription segment --
    ingest_voice() only ever reads .start and .text off each one."""

    def __init__(self, start: float, text: str):
        self.start = start
        self.text = text


class _FakeWhisperModel:
    def transcribe(self, path):
        # (segments, info) — same shape _get_model().transcribe() returns.
        return [_FakeSegment(0.0, PII_TEXT)], {}


def test_ingest_voice_redacts_pii(tmp_path, monkeypatch):
    # Content doesn't matter -- transcription itself is faked below --
    # ingest_voice() only needs the path to exist.
    audio_path = str(tmp_path / "meeting.wav")
    open(audio_path, "wb").close()

    monkeypatch.setattr(voice_ingestor, "_get_model", lambda: _FakeWhisperModel())

    artifact = ingest_voice(audio_path)
    content = artifact["sections"][0]["content"]

    assert "Jane Doe" not in content
    assert "jane.doe@example.com" not in content
    assert "245-71-8390" not in content
