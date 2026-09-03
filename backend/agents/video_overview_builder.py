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
import re
import tempfile
import textwrap

from moviepy import AudioFileClip, ImageClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont

FRAME_SIZE = (1280, 720)
MARGIN = 90
# A single-word slide still gets a readable amount of screen time rather
# than flashing by at its literal 1/N share of the audio.
MIN_SLIDE_SECONDS = 3.0

# ---------------------------------------------------------------------------
# BUGFIX (rendering audit): _render_slide() used to draw `content` onto the
# frame completely raw. `content` here is whatever prose slide_planner (via
# markdown_text_to_artifact() -> agents/importer.py's parse_markdown_text())
# put under a "## " heading -- real Markdown: "**bold**", "#### " sub-
# headings, "- " bullets, "| a | b |" tables, and (as the reported bug's own
# screenshot showed) an entire ```mermaid fenced code block, if the section
# happened to contain one. None of that is meaningful to a human as literal
# text on a video frame -- "**Frame**: Cast-iron..." should read as
# "Frame: Cast-iron...", not with the asterisks burned into the pixels, and
# a ```mermaid block should not be dumped as raw node syntax at all since
# this renderer has no diagram engine.
#
# This is a *frame-rendering* concern only: it never touches slide_text
# itself (still real Markdown, still what the Presentation panel and
# agents/exporter.py's pptx export show/use), just the plain-text lines
# handed to ImageDraw.text() for this one fallback "narrated slideshow".
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s*(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
_LIST_MARKER_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_LIST_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|___)(.+?)\1")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)|(?<!_)_(?!_)(.+?)_(?!_)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _strip_inline_markdown(line: str) -> str:
    """Removes inline emphasis/code/link syntax, keeping the human-readable
    text underneath -- e.g. '**Frame**: cast-iron' -> 'Frame: cast-iron',
    '[HOST A](notecite://x)' -> 'HOST A'."""
    line = _LINK_RE.sub(r"\1", line)
    line = _BOLD_ITALIC_RE.sub(r"\2", line)
    line = _BOLD_RE.sub(r"\2", line)
    line = _ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), line)
    line = _INLINE_CODE_RE.sub(r"\1", line)
    return line


def _table_row_to_line(row: str) -> str:
    """'| Component | Material | Purpose |' -> 'Component — Material — Purpose'.
    Markdown table syntax read literally is noise on a video frame; the
    cell values themselves are still worth keeping."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    cells = [_strip_inline_markdown(c) for c in cells if c.strip()]
    return "  —  ".join(cells)


def markdown_to_plain_lines(content: str) -> list[str]:
    """Turns one section's raw Markdown `content` into a flat list of
    plain-text lines suitable for ImageDraw.text() -- no '#', '**', '|',
    '```' syntax left in the output. Exported (not prefixed with '_') so
    tests can exercise it directly without going through a full frame
    render.

    Fenced code blocks are dropped rather than rendered verbatim: a
    ```mermaid (or any other) fence is source-code syntax with no
    meaningful plain-text form, and this module has no diagram engine to
    actually render it as one. A single placeholder line marks that a
    diagram was present, so the slide doesn't just silently lose a
    section -- the real diagram is still visible in the Presentation
    panel (which renders ```mermaid properly via MermaidDiagram) and in
    the exported pptx/docx.
    """
    lines: list[str] = []
    in_fence = False
    fence_lang = ""
    fence_placeholder_emitted = False

    for raw_line in (content or "").split("\n"):
        fence_match = _FENCE_RE.match(raw_line.strip())
        if fence_match:
            if not in_fence:
                in_fence = True
                fence_lang = fence_match.group(1).lower()
                fence_placeholder_emitted = False
            else:
                in_fence = False
                fence_lang = ""
            continue

        if in_fence:
            if fence_lang == "mermaid" and not fence_placeholder_emitted:
                lines.append("[Diagram — view in the Presentation tab]")
                fence_placeholder_emitted = True
            # Non-mermaid fenced content (e.g. ```json, ```python) is
            # skipped outright rather than dumped as raw code -- same
            # "not meaningful as slideshow prose" reasoning.
            continue

        line = raw_line.rstrip()
        if not line.strip():
            lines.append("")
            continue

        if _TABLE_SEP_RE.match(line) and "-" in line and "|" in line:
            continue  # the '|---|---|' divider row itself carries no content
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            lines.append(_table_row_to_line(line))
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            lines.append(_strip_inline_markdown(heading_match.group(1)))
            continue

        quote_match = _BLOCKQUOTE_RE.match(line)
        if quote_match:
            line = quote_match.group(1)

        ordered_match = _ORDERED_LIST_RE.match(line)
        if ordered_match:
            indent, num, rest = ordered_match.groups()
            lines.append(f"{indent}{num}. {_strip_inline_markdown(rest)}")
            continue

        list_match = _LIST_MARKER_RE.match(line)
        if list_match:
            indent, rest = list_match.groups()
            lines.append(f"{indent}•  {_strip_inline_markdown(rest)}")
            continue

        lines.append(_strip_inline_markdown(line))

    return lines


# ---------------------------------------------------------------------------
# Visual polish: per-slide accent theme, soft gradient background, and an
# accent shape/bar instead of the previous flat white frame with plain
# black text. Pure Pillow (Image/ImageDraw), no new dependency -- gradients
# and rounded shapes are drawn with primitives already available in the
# Pillow version this module already required.
# ---------------------------------------------------------------------------

# Six (accent, ink) pairs cycled by slide index so consecutive frames read
# as a deliberate, varied deck rather than one repeated color. `ink` is the
# dark text color chosen for contrast against that accent's tint.
_THEMES = [
    ((91, 141, 239), (17, 24, 39)),    # blue
    ((236, 122, 94), (35, 20, 15)),    # coral
    ((84, 179, 153), (12, 30, 26)),    # teal
    ((197, 130, 232), (30, 17, 38)),   # violet
    ((240, 180, 60), (38, 27, 6)),     # amber
    ((99, 179, 237), (14, 28, 40)),    # sky
]

BG_TOP = (250, 250, 252)
BG_BOTTOM = (233, 235, 240)


def _load_fonts():
    """Falls back to Pillow's built-in bitmap font if no TrueType font is
    on the host -- a missing system font degrades legibility, it
    shouldn't crash the whole build."""
    candidates = [
        ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
        ("Arial Bold.ttf", "Arial.ttf"),
        ("LiberationSans-Bold.ttf", "LiberationSans-Regular.ttf"),
    ]
    for bold_name, regular_name in candidates:
        try:
            return (ImageFont.truetype(bold_name, 46),
                    ImageFont.truetype(regular_name, 30),
                    ImageFont.truetype(regular_name, 20))
        except OSError:
            continue
    default = ImageFont.load_default()
    return default, default, default


def _vertical_gradient(size, top_rgb, bottom_rgb):
    """Cheap top-to-bottom gradient background -- one Draw.rectangle call
    per row of a small vertical strip, then resized up, rather than a
    full-resolution per-pixel loop."""
    width, height = size
    strip_h = 256
    strip = Image.new("RGB", (1, strip_h))
    draw = ImageDraw.Draw(strip)
    for y in range(strip_h):
        t = y / (strip_h - 1)
        rgb = tuple(int(top_rgb[i] + (bottom_rgb[i] - top_rgb[i]) * t) for i in range(3))
        draw.point((0, y), fill=rgb)
    return strip.resize(size)


def _draw_accent_motif(img: Image.Image, accent: tuple) -> None:
    """A large, soft-opacity accent circle bleeding off the top-right
    corner -- a cheap way to give each themed slide a distinct, non-flat
    background without needing any image asset on disk."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    w, h = img.size
    r = int(h * 0.55)
    cx, cy = w + int(r * 0.15), -int(r * 0.25)
    odraw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*accent, 60))
    r2 = int(h * 0.22)
    odraw.ellipse((-int(r2 * 0.4), h - int(r2 * 0.5), r2 * 1.6, h + int(r2 * 1.5)), fill=(*accent, 40))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))


def _render_slide(heading: str, content: str, out_path: str, index: int = 0, total: int = 1) -> None:
    """One section -> one themed PNG frame. Still deliberately simple
    (left-aligned title, wrapped body text) -- this is the narrated-
    slideshow fallback, not a design tool. agents/exporter.py's real
    pptx export is still what a user downloads to actually edit slides."""
    title_font, body_font, small_font = _load_fonts()
    accent, ink = _THEMES[index % len(_THEMES)]
    body_color = tuple(min(255, c + 40) for c in ink)  # slightly lighter than the title for hierarchy

    img = _vertical_gradient(FRAME_SIZE, BG_TOP, BG_BOTTOM)
    _draw_accent_motif(img, accent)
    draw = ImageDraw.Draw(img)

    # Accent bar to the left of the title, plus a thin underline beneath
    # it -- the recurring visual anchor that makes each frame read as
    # part of the same themed deck rather than a plain text dump.
    bar_x = MARGIN - 26
    draw.rounded_rectangle((bar_x, MARGIN, bar_x + 10, MARGIN + 56), radius=5, fill=accent)
    draw.text((MARGIN, MARGIN), heading or "", font=title_font, fill=ink)
    underline_y = MARGIN + 70
    draw.line((MARGIN, underline_y, MARGIN + 180, underline_y), fill=accent, width=4)

    plain_lines = markdown_to_plain_lines(content)
    wrapped_lines = []
    for raw_line in plain_lines:
        if raw_line.strip():
            wrapped_lines.extend(textwrap.wrap(raw_line, width=62) or [""])
        else:
            wrapped_lines.append("")

    y = MARGIN + 110
    line_height = 42
    max_y = FRAME_SIZE[1] - MARGIN
    for i, line in enumerate(wrapped_lines):
        if y + line_height > max_y:
            if i < len(wrapped_lines):
                draw.text((MARGIN, y), "…", font=body_font, fill=accent)
            break
        draw.text((MARGIN, y), line, font=body_font, fill=body_color)
        y += line_height

    # Slide counter, bottom-right -- small continuity cue for a multi-
    # section deck, styled in the slide's own accent color.
    counter = f"{index + 1} / {total}"
    draw.text((FRAME_SIZE[0] - MARGIN - 60, FRAME_SIZE[1] - MARGIN + 20), counter, font=small_font, fill=accent)

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
        total = len(sections)
        for i, section in enumerate(sections):
            frame_path = os.path.join(tmp_dir, f"slide_{i:03d}.png")
            _render_slide(section.get("heading", ""), section.get("content", ""), frame_path, index=i, total=total)
            frame_clips.append(ImageClip(frame_path).with_duration(per_slide_seconds))

        video = concatenate_videoclips(frame_clips, method="compose")
        video = video.with_audio(audio_clip)
        video.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac", logger=None)

    audio_clip.close()
    return out_path