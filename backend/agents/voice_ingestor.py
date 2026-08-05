"""
agents/voice_ingestor.py — Part 4 §4.2. Deterministic, transcription-only
ingestion of voice notes and meeting recordings into the common artifact
shape agents/pdf_ingestor.py and agents/video_ingestor.py already
produce. faster-whisper runs entirely locally on CPU, no API key,
matching every other Capture ingestor's no-paid-process constraint.

Transcription happens here; deciding what a meeting transcript MEANS
(decisions made, action items assigned) is deliberately NOT this
module's job. Per the notes doc's own conclusion, that's exactly the
research domain's `analyst` role doing the same kind of structured
extraction over different input — hiring `analyst` directly on this
ingestor's output is the right call, not a duplicate
`meeting_summarizer` brief saying the same thing under a different
name. This module only turns audio into text.

Place this file at: agents/voice_ingestor.py
"""

import os

from faster_whisper import WhisperModel

# Same reasoning as agents/video_ingestor.py's SECTION_LENGTH_SECONDS:
# one section per whole recording risks one overlong node; one section
# per whisper segment (a few words each) is far too granular. 10-minute
# chunks match the video ingestor's own choice, for consistent node
# sizing across every ingestor that has a time axis.
SECTION_LENGTH_SECONDS = 600

# "base" is the smallest model that gives usable accuracy on real
# speech without a GPU; int8 compute keeps CPU transcription practical
# for a multi-minute recording. Not configurable per-call — Capture
# ingestors are meant to be zero-config, same as every other row in the
# notes doc's ingestion table.
_MODEL_SIZE = "base"
_model = None


def _get_model() -> WhisperModel:
    """Lazy singleton — loading the model is the expensive part
    (seconds, not milliseconds); every ingest_voice() call after the
    first reuses it instead of reloading."""
    global _model
    if _model is None:
        _model = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def _format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def ingest_voice(path: str) -> dict:
    """Transcribes the audio file at `path` and chunks it into
    SECTION_LENGTH_SECONDS-wide sections. Raises FileNotFoundError if
    the path doesn't exist, matching every other ingestor's contract.
    Any transcription failure (corrupt/unsupported audio, decode error)
    or a transcript with no detected speech collapses to a single
    ValueError — same one-exception-type contract as
    agents/web_clipper.py and agents/video_ingestor.py.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    sections = []
    try:
        segments, _info = _get_model().transcribe(path)
        chunk_start = 0.0
        chunk_lines = []
        for segment in segments:
            if segment.start - chunk_start >= SECTION_LENGTH_SECONDS and chunk_lines:
                sections.append({
                    "heading": _format_timestamp(chunk_start),
                    "content": " ".join(chunk_lines).strip(),
                    "node_refs": [],
                })
                chunk_start = segment.start
                chunk_lines = []
            chunk_lines.append(segment.text.strip())
        if chunk_lines:
            sections.append({
                "heading": _format_timestamp(chunk_start),
                "content": " ".join(chunk_lines).strip(),
                "node_refs": [],
            })
    except Exception as exc:
        raise ValueError(f"could not transcribe {path}: {exc}")

    if not sections:
        raise ValueError(f"no speech detected in {path}")

    return {
        "title": os.path.splitext(os.path.basename(path))[0],
        "sections": sections,
        "metadata": {"source_format": "voice", "source_path": path},
    }


if __name__ == "__main__":
    import sys
    import json
    for p in sys.argv[1:]:
        artifact = ingest_voice(p)
        print(f"--- {p} ---")
        print(json.dumps(artifact, indent=2)[:500])