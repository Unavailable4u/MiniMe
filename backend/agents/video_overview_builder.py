"""
agents/video_overview_builder.py — Part 4 §4.4. Deterministic, no-LLM-call
assembly of Video Overview: static slide-style frames narrated by
agents/tts_synthesizer.py's already-synthesized podcast audio, stitched
into one mp4 via moviepy.

Confirmed before writing this module: there is no pptx-to-image rendering
path anywhere in this codebase. agents/exporter.py's _write_pptx produces
a real, editable python-pptx Presentation, not raster frames -- rendering
a pptx to images needs an external tool (e.g. LibreOffice) this stack
doesn't have. Rather than add that missing link, this module renders
frames directly from the same {title, sections} artifact
graph/adapters.py's markdown_text_to_artifact() already produces from
slide_planner's raw Markdown -- one Pillow-drawn PNG per section, so it
never touches the pptx path at all.

Honest product labeling, matching the notes doc's own discipline (§4.4):
this should be shown to the user as a "narrated slideshow," not "video"
in the cinematic sense -- there is no scene composition and no
word-level sync between narration and slide content, just each slide
getting an equal share of the narration's total runtime.

Dependency note: unlike every other Capture/Generate tool agent in this
domain, moviepy is NOT a pure-local-library dependency -- it shells out to
a real ffmpeg binary, which must be present on the host (confirmed at
/usr/bin/ffmpeg in this environment; verify on deploy). Built against
moviepy 2.x's actual API (`from moviepy import ...`, `with_duration()` /
`with_audio()`) -- moviepy 2.0 restructured both the import path and these
method names from the commonly-remembered 1.x `.editor` / `set_*` shape;
pin moviepy>=2.0 in requirements.txt or these calls will fail against 1.x.

Place this file at: agents/video_overview_builder.py
"""
import os
import textwrap
import tempfile

from PIL import Image, ImageDraw, ImageFont
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

FRAME_SIZE = (1280, 720)
BG_COLOR = (255, 255, 255)
TITLE_COLOR = (20, 20, 20)
BODY_COLOR = (60, 60, 60)
MARGIN = 80
# A single-word slide still gets a readable amount of screen time rather
# than flashing by at its literal 1/N share of the audio.
MIN_SLIDE_SECONDS = 3.0


def _load_fonts():
    """Falls back to Pillow's built-in bitmap font if no TrueType font is
    on the host -- a missing system font degrades legibility, it
    shouldn't crash the whole build."""
    try:
        return (ImageFont.truetype("DejaVuSans-Bold.ttf", 48),
                ImageFont.truetype("DejaVuSans.ttf", 32))
    except OSError:
        return ImageFont.load_default(), ImageFont.load_default()


def _render_slide(heading: str, content: str, out_path: str) -> None:
    """One section -> one PNG frame. Deliberately plain (left-aligned
    title, wrapped body text) -- this is the narrated-slideshow fallback,
    not a design tool. agents/exporter.py's real pptx export is still
    what a user downloads to actually edit slides."""
    title_font, body_font = _load_fonts()
    img = Image.new("RGB", FRAME_SIZE, BG_COLOR)
    draw = ImageDraw.Draw(img)
    draw.text((MARGIN, MARGIN), heading or "", font=title_font, fill=TITLE_COLOR)

    wrapped_lines = []
    for raw_line in (content or "").split("\n"):
        if raw_line.strip():
            wrapped_lines.extend(textwrap.wrap(raw_line, width=60) or [""])
        else:
            wrapped_lines.append("")

    y = MARGIN + 90
    for line in wrapped_lines:
        draw.text((MARGIN, y), line, font=body_font, fill=BODY_COLOR)
        y += 44

    img.save(out_path)


def build_video_overview(slide_artifact: dict, audio_path: str, out_path: str) -> str:
    """
    slide_artifact: the {title, sections} shape
        graph/adapters.py's markdown_text_to_artifact() produces from
        slide_planner's output -- each section becomes one frame.
    audio_path: an mp3 already produced by
        agents/tts_synthesizer.py's synthesize_podcast() for the SAME
        notebook's podcast script. This module doesn't check that the
        audio and slides actually cover the same content -- that
        grounding already happened upstream, once each, in
        podcast_scriptwriter/slide_planner's own briefs.

    Timing model, stated plainly rather than oversold: each slide gets an
    equal share of the audio's total duration (floored at
    MIN_SLIDE_SECONDS each) -- no word-level alignment between narration
    and slide content. The video always runs at least as long as the
    audio (the MIN_SLIDE_SECONDS floor can only make it longer, never
    shorter), so narration is never cut off mid-sentence.

    Raises ValueError if slide_artifact has no sections, FileNotFoundError
    if audio_path doesn't exist -- same one-exception-per-failure-mode
    contract every other tool agent in this domain uses.
    """
    sections = slide_artifact.get("sections") or []
    if not sections:
        raise ValueError("slide_artifact has no sections to render")
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)

    audio_clip = AudioFileClip(audio_path)
    per_slide_seconds = max(audio_clip.duration / len(sections), MIN_SLIDE_SECONDS)

    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_clips = []
        for i, section in enumerate(sections):
            frame_path = os.path.join(tmp_dir, f"slide_{i:03d}.png")
            _render_slide(section.get("heading", ""), section.get("content", ""), frame_path)
            frame_clips.append(ImageClip(frame_path).with_duration(per_slide_seconds))

        video = concatenate_videoclips(frame_clips, method="compose")
        video = video.with_audio(audio_clip)
        video.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac", logger=None)

    audio_clip.close()
    return out_path