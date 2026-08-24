"""
tests/unit/test_agent_video_overview_builder.py — Patch 7f-6-2.

Covers agents/video_overview_builder.py — the deterministic, no-LLM-call
assembly of a "narrated slideshow" mp4: one Pillow-drawn PNG frame per
{title, sections} section, stitched via moviepy against an
already-synthesized podcast mp3 (agents/tts_synthesizer.py's output),
with each slide getting an equal share of the audio's total duration,
floored at MIN_SLIDE_SECONDS.

_render_slide() is exercised against real Pillow (no network, no
external process, and font-fallback is itself part of what's under
test) -- only the moviepy classes (AudioFileClip, ImageClip,
concatenate_videoclips) are faked, since those are what shell out to a
real ffmpeg binary and are exactly the boundary this module's own
docstring flags as non-local.
"""
import os

from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageFont

import agents.video_overview_builder as vob


# ---------------------------------------------------------------------------
# 1. _load_fonts(): TrueType success vs bitmap fallback
# ---------------------------------------------------------------------------
class TestLoadFonts:
    def test_returns_truetype_fonts_when_available(self):
        title_font, body_font = vob._load_fonts()
        # Either a real ImageFont.FreeTypeFont (host has DejaVu) or the
        # bitmap ImageFont.ImageFont fallback -- both are valid
        # PIL font objects usable by ImageDraw.text.
        assert title_font is not None
        assert body_font is not None

    def test_falls_back_to_bitmap_font_on_os_error(self, monkeypatch):
        # ImageFont.load_default() itself calls truetype() internally on
        # this Pillow version, so the fake must only reject the specific
        # DejaVuSans lookups _load_fonts() makes -- a blanket raise would
        # also break the fallback path it's supposed to exercise.
        real_truetype = ImageFont.truetype

        def _raise_for_dejavu(font, *args, **kwargs):
            if isinstance(font, str) and "DejaVu" in font:
                raise OSError("no such font on this host")
            return real_truetype(font, *args, **kwargs)

        monkeypatch.setattr(ImageFont, "truetype", _raise_for_dejavu)
        title_font, body_font = vob._load_fonts()
        # Pillow's built-in bitmap default font -- a missing system font
        # degrades legibility, it must not crash the whole build.
        default_font = ImageFont.load_default()
        assert type(title_font) is type(default_font)
        assert type(body_font) is type(default_font)


# ---------------------------------------------------------------------------
# 2. _render_slide(): one section -> one PNG frame, real Pillow rendering
# ---------------------------------------------------------------------------
class TestRenderSlide:
    def test_writes_a_readable_png_file(self, tmp_path):
        out_path = str(tmp_path / "slide.png")
        vob._render_slide("A Heading", "Some body content.", out_path)
        assert os.path.exists(out_path)
        with Image.open(out_path) as img:
            assert img.size == vob.FRAME_SIZE

    def test_handles_empty_heading_and_content_without_crashing(self, tmp_path):
        out_path = str(tmp_path / "slide.png")
        vob._render_slide("", "", out_path)
        assert os.path.exists(out_path)

    def test_handles_none_heading_and_content_without_crashing(self, tmp_path):
        out_path = str(tmp_path / "slide.png")
        vob._render_slide(None, None, out_path)
        assert os.path.exists(out_path)

    def test_long_content_wraps_across_multiple_lines(self, tmp_path):
        # Not directly inspectable from the saved PNG, but a long,
        # unbroken line must not raise or silently truncate -- exercise
        # the textwrap.wrap() path with content well past the 60-char
        # wrap width.
        out_path = str(tmp_path / "slide.png")
        long_content = "word " * 100
        vob._render_slide("Heading", long_content, out_path)
        assert os.path.exists(out_path)

    def test_multiline_content_preserves_blank_lines(self, tmp_path):
        out_path = str(tmp_path / "slide.png")
        content = "first paragraph\n\nsecond paragraph"
        vob._render_slide("Heading", content, out_path)
        assert os.path.exists(out_path)


# ---------------------------------------------------------------------------
# 3. build_video_overview(): validation before any moviepy work happens
# ---------------------------------------------------------------------------
class TestValidation:
    def test_no_sections_raises_value_error(self, tmp_path):
        audio_path = str(tmp_path / "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(b"fake mp3 bytes")
        with pytest.raises(ValueError, match="no sections"):
            vob.build_video_overview({"title": "T", "sections": []}, audio_path, str(tmp_path / "out.mp4"))

    def test_missing_sections_key_raises_value_error(self, tmp_path):
        audio_path = str(tmp_path / "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(b"fake mp3 bytes")
        with pytest.raises(ValueError, match="no sections"):
            vob.build_video_overview({"title": "T"}, audio_path, str(tmp_path / "out.mp4"))

    def test_missing_audio_file_raises_file_not_found_error(self, tmp_path):
        slide_artifact = {"title": "T", "sections": [{"heading": "H", "content": "C"}]}
        missing_audio = str(tmp_path / "does_not_exist.mp3")
        with pytest.raises(FileNotFoundError):
            vob.build_video_overview(slide_artifact, missing_audio, str(tmp_path / "out.mp4"))

    def test_missing_audio_error_message_is_the_path(self, tmp_path):
        slide_artifact = {"title": "T", "sections": [{"heading": "H", "content": "C"}]}
        missing_audio = str(tmp_path / "does_not_exist.mp3")
        with pytest.raises(FileNotFoundError, match=r"does_not_exist\.mp3"):
            vob.build_video_overview(slide_artifact, missing_audio, str(tmp_path / "out.mp4"))


# ---------------------------------------------------------------------------
# 4. build_video_overview(): moviepy assembly, mocked at the class level
# ---------------------------------------------------------------------------
class FakeAudioClip:
    def __init__(self, filename, duration):
        self.filename = filename
        self.duration = duration
        self.closed = False

    def close(self):
        self.closed = True


class FakeImageClip:
    def __init__(self, filename):
        self.filename = filename
        self.duration = None

    def with_duration(self, seconds):
        self.duration = seconds
        return self


class FakeVideo:
    def __init__(self, clips, method):
        self.clips = clips
        self.method = method
        self.audio = None
        self.write_calls = []

    def with_audio(self, audio_clip):
        self.audio = audio_clip
        return self

    def write_videofile(self, out_path, **kwargs):
        self.write_calls.append((out_path, kwargs))
        with open(out_path, "wb") as f:
            f.write(b"fake mp4 bytes")


@pytest.fixture
def fake_moviepy(monkeypatch):
    state = {"audio_duration": 30.0, "video": None}

    def _fake_audio_file_clip(filename):
        return FakeAudioClip(filename, state["audio_duration"])

    def _fake_concatenate(clips, method="compose"):
        video = FakeVideo(clips, method)
        state["video"] = video
        return video

    monkeypatch.setattr(vob, "AudioFileClip", _fake_audio_file_clip)
    monkeypatch.setattr(vob, "ImageClip", FakeImageClip)
    monkeypatch.setattr(vob, "concatenate_videoclips", _fake_concatenate)
    return state


class TestBuildVideoOverview:
    def _slide_artifact(self, n_sections=2):
        return {
            "title": "T",
            "sections": [
                {"heading": f"H{i}", "content": f"C{i}"} for i in range(n_sections)
            ],
        }

    def _audio_path(self, tmp_path):
        audio_path = str(tmp_path / "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(b"fake mp3 bytes")
        return audio_path

    def test_returns_out_path(self, fake_moviepy, tmp_path):
        out_path = str(tmp_path / "out.mp4")
        result = vob.build_video_overview(self._slide_artifact(), self._audio_path(tmp_path), out_path)
        assert result == out_path

    def test_writes_video_to_out_path(self, fake_moviepy, tmp_path):
        out_path = str(tmp_path / "out.mp4")
        vob.build_video_overview(self._slide_artifact(), self._audio_path(tmp_path), out_path)
        assert os.path.exists(out_path)

    def test_one_image_clip_created_per_section(self, fake_moviepy, tmp_path):
        vob.build_video_overview(self._slide_artifact(n_sections=4), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        assert len(fake_moviepy["video"].clips) == 4

    def test_per_slide_duration_is_audio_duration_divided_by_section_count(self, fake_moviepy, tmp_path):
        fake_moviepy["audio_duration"] = 40.0
        vob.build_video_overview(self._slide_artifact(n_sections=4), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        for clip in fake_moviepy["video"].clips:
            assert clip.duration == pytest.approx(10.0)

    def test_per_slide_duration_floored_at_min_slide_seconds(self, fake_moviepy, tmp_path):
        # 2 sections, 1 second of audio total -> 0.5s/slide, which must be
        # floored up to MIN_SLIDE_SECONDS rather than flashing by.
        fake_moviepy["audio_duration"] = 1.0
        vob.build_video_overview(self._slide_artifact(n_sections=2), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        for clip in fake_moviepy["video"].clips:
            assert clip.duration == vob.MIN_SLIDE_SECONDS

    def test_video_duration_never_shorter_than_audio(self, fake_moviepy, tmp_path):
        fake_moviepy["audio_duration"] = 1.0
        n = 2
        vob.build_video_overview(self._slide_artifact(n_sections=n), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        total_video_seconds = sum(c.duration for c in fake_moviepy["video"].clips)
        assert total_video_seconds >= fake_moviepy["audio_duration"]

    def test_video_audio_is_the_loaded_audio_clip(self, fake_moviepy, tmp_path):
        vob.build_video_overview(self._slide_artifact(), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        video = fake_moviepy["video"]
        assert video.audio.duration == fake_moviepy["audio_duration"]

    def test_write_videofile_called_with_expected_codec_args(self, fake_moviepy, tmp_path):
        out_path = str(tmp_path / "out.mp4")
        vob.build_video_overview(self._slide_artifact(), self._audio_path(tmp_path), out_path)
        video = fake_moviepy["video"]
        assert len(video.write_calls) == 1
        called_path, kwargs = video.write_calls[0]
        assert called_path == out_path
        assert kwargs["fps"] == 24
        assert kwargs["codec"] == "libx264"
        assert kwargs["audio_codec"] == "aac"

    def test_concatenate_called_with_compose_method(self, fake_moviepy, tmp_path):
        vob.build_video_overview(self._slide_artifact(), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        assert fake_moviepy["video"].method == "compose"

    def test_audio_clip_closed_after_write(self, monkeypatch, tmp_path):
        closed = []

        class TrackedAudioClip(FakeAudioClip):
            def close(self):
                closed.append(True)
                super().close()

        monkeypatch.setattr(vob, "AudioFileClip", lambda filename: TrackedAudioClip(filename, 20.0))
        monkeypatch.setattr(vob, "ImageClip", FakeImageClip)
        monkeypatch.setattr(vob, "concatenate_videoclips", lambda clips, method="compose": FakeVideo(clips, method))

        vob.build_video_overview(self._slide_artifact(), self._audio_path(tmp_path), str(tmp_path / "out.mp4"))
        assert closed == [True]
