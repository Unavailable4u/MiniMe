"""
agents/video_ingestor.py — Part 4 §4.2. Deterministic, no-LLM-call
YouTube/video-link ingestion into the same common artifact shape
agents/pdf_ingestor.py and agents/web_clipper.py already produce
({title, sections, metadata}).

Transcript only, by design — no video download or storage (the notes
doc is explicit about this: it's a text-ingestion feature, not a media
one). yt-dlp resolves the video id and pulls title/metadata without
downloading the video stream itself (extract_flat / skip_download);
youtube_transcript_api pulls the transcript directly, no video file
ever touches disk.

Place this file at: agents/video_ingestor.py
"""

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

# Segments are chunked into sections by elapsed time rather than kept as
# one giant block or one node per caption line (a caption line is a
# handful of words -- far too granular to be a useful node, and a whole
# hour-long transcript as one section runs back into the same
# too-long-for-one-node problem pdf_ingestor.py's per-page split avoids).
SECTION_LENGTH_SECONDS = 600  # 10-minute chunks


def _video_id(url: str) -> str:
    """yt-dlp already knows every URL shape (youtu.be, /watch?v=,
    /shorts/, /embed/, ...) -- extract_info with download=False resolves
    the id without a network fetch of the video itself, so this is
    reused instead of hand-rolling a second regex-based URL parser that
    could drift from yt-dlp's own idea of what a valid video URL is.
    """
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or "id" not in info:
        raise ValueError(f"could not resolve a video id from {url}")
    return info["id"], info.get("title") or url


def _format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def ingest_video(url: str) -> dict:
    """Fetches the transcript for `url` and chunks it into
    SECTION_LENGTH_SECONDS-wide sections. Raises ValueError if the video
    can't be resolved or has no available transcript (disabled by the
    uploader, or genuinely none exists) -- same single-exception-type
    contract as agents/web_clipper.py's clip_url(), for the same reason:
    one thing for the calling endpoint to catch and turn into a 400.
    """
    video_id, title = _video_id(url)

    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
    except (TranscriptsDisabled, NoTranscriptFound) as exc:
        raise ValueError(f"no transcript available for {url}: {exc}")

    if not transcript:
        raise ValueError(f"empty transcript for {url}")

    sections = []
    chunk_start = 0.0
    chunk_lines = []
    for entry in transcript:
        if entry["start"] - chunk_start >= SECTION_LENGTH_SECONDS and chunk_lines:
            sections.append({
                "heading": f"{_format_timestamp(chunk_start)}",
                "content": " ".join(chunk_lines).strip(),
                "node_refs": [],
            })
            chunk_start = entry["start"]
            chunk_lines = []
        chunk_lines.append(entry["text"])
    if chunk_lines:
        sections.append({
            "heading": f"{_format_timestamp(chunk_start)}",
            "content": " ".join(chunk_lines).strip(),
            "node_refs": [],
        })

    return {
        "title": title,
        "sections": sections,
        "metadata": {"source_format": "video", "source_url": url, "video_id": video_id},
    }


if __name__ == "__main__":
    import sys
    import json
    for u in sys.argv[1:]:
        artifact = ingest_video(u)
        print(f"--- {u} ---")
        print(json.dumps(artifact, indent=2)[:500])