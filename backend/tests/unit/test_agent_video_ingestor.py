"""
tests/unit/test_agent_video_ingestor.py — Patch 7f-6-2.

Covers agents/video_ingestor.py's ingest_video(): the deterministic,
no-LLM-call YouTube ingestor that resolves a video id + title via
yt-dlp (extract_info, download=False -- no video stream ever touches
disk), pulls the transcript via youtube_transcript_api, and chunks it
into SECTION_LENGTH_SECONDS-wide sections in the same common artifact
shape ({title, sections, metadata}) agents/web_clipper.py and
agents/pdf_ingestor.py already produce. Both real network-touching
calls (yt_dlp.YoutubeDL and YouTubeTranscriptApi.get_transcript) are
faked at the module level -- this module never makes a real network
call under test.

FINDING (flagged, not fixed here -- out of scope for a test-only
patch): requirements.txt pins youtube-transcript-api==1.2.4, whose
YouTubeTranscriptApi class has no get_transcript classmethod at all
(dir() on that version shows only instance methods fetch()/list() --
get_transcript was the pre-1.0 API). agents/video_ingestor.py's own
YouTubeTranscriptApi.get_transcript(video_id) call would therefore
raise AttributeError against the pinned dependency version at real
runtime, not just under test. The fake_transcript_api fixture below
patches get_transcript onto the class with raising=False specifically
because the attribute doesn't exist pre-patch on this pinned version --
that's this finding made concrete, not a workaround for a test-only
quirk.

Patch SX-07 adds the host checks: the URL must be a YouTube URL before
yt-dlp is touched, and the webpage_url yt-dlp reports back must be on a
YouTube host. Sections 6 and 7 cover those; no DNS lookup is ever made.
"""
import socket

import pytest
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

from agents import video_ingestor


class FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL's context-manager surface --
    only extract_info(url, download=False) is ever called by this
    module."""

    last_opts = None
    instances = 0
    extract_calls = []

    def __init__(self, opts):
        FakeYDL.last_opts = opts
        FakeYDL.instances += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.extract_calls.append(url)
        return FakeYDL.info


GOOD_PAGE = "https://www.youtube.com/watch?v=vid123"


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """The YouTube check needs no DNS (yt-dlp does its own networking, and
    it is faked here). A lookup is recorded as well as refused."""
    lookups = []

    def getaddrinfo(host, *args, **kwargs):
        lookups.append(host)
        raise AssertionError(f"unexpected DNS lookup for {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return lookups


def _entry(text, start):
    return {"text": text, "start": start, "duration": 2.0}


@pytest.fixture
def fake_yt_dlp(monkeypatch):
    FakeYDL.info = {"id": "vid123", "title": "A Real Video Title", "webpage_url": GOOD_PAGE}
    FakeYDL.instances = 0
    FakeYDL.extract_calls = []
    monkeypatch.setattr(video_ingestor.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


@pytest.fixture
def fake_transcript_api(monkeypatch):
    fake = type("FakeTranscriptApi", (), {})()
    fake.transcript = [_entry("hello world", 0.0)]
    fake.calls = []

    def get_transcript(video_id):
        fake.calls.append(video_id)
        if isinstance(fake.transcript, Exception):
            raise fake.transcript
        return fake.transcript

    monkeypatch.setattr(video_ingestor.YouTubeTranscriptApi, "get_transcript", staticmethod(get_transcript), raising=False)
    return fake


# ---------------------------------------------------------------------------
# 1. _video_id(): resolution success/failure
# ---------------------------------------------------------------------------
class TestVideoId:
    def test_returns_id_and_title_from_extract_info(self, fake_yt_dlp):
        video_id, title = video_ingestor._video_id("https://youtu.be/vid123")
        assert video_id == "vid123"
        assert title == "A Real Video Title"

    def test_missing_title_falls_back_to_url(self, fake_yt_dlp):
        FakeYDL.info = {"id": "vid123", "webpage_url": GOOD_PAGE}
        video_id, title = video_ingestor._video_id("https://youtu.be/vid123")
        assert title == "https://youtu.be/vid123"

    def test_empty_string_title_falls_back_to_url(self, fake_yt_dlp):
        FakeYDL.info = {"id": "vid123", "title": "", "webpage_url": GOOD_PAGE}
        video_id, title = video_ingestor._video_id("https://youtu.be/vid123")
        assert title == "https://youtu.be/vid123"

    def test_extract_info_returning_none_raises_value_error(self, fake_yt_dlp):
        FakeYDL.info = None
        with pytest.raises(ValueError, match="could not resolve a video id"):
            video_ingestor._video_id("https://youtu.be/bad")

    def test_extract_info_missing_id_key_raises_value_error(self, fake_yt_dlp):
        FakeYDL.info = {"title": "No id here"}
        with pytest.raises(ValueError, match="could not resolve a video id"):
            video_ingestor._video_id("https://youtu.be/bad")

    def test_error_message_includes_the_url(self, fake_yt_dlp):
        FakeYDL.info = None
        with pytest.raises(ValueError, match=r"https://youtu\.be/bad-link"):
            video_ingestor._video_id("https://youtu.be/bad-link")

    def test_ydl_opts_request_quiet_and_skip_download(self, fake_yt_dlp):
        video_ingestor._video_id("https://youtu.be/vid123")
        assert FakeYDL.last_opts.get("quiet") is True
        assert FakeYDL.last_opts.get("skip_download") is True


# ---------------------------------------------------------------------------
# 2. _format_timestamp(): mm:ss vs hh:mm:ss
# ---------------------------------------------------------------------------
class TestFormatTimestamp:
    def test_under_an_hour_formats_as_mm_ss(self):
        assert video_ingestor._format_timestamp(65) == "01:05"

    def test_zero_seconds_formats_as_00_00(self):
        assert video_ingestor._format_timestamp(0) == "00:00"

    def test_over_an_hour_formats_as_hh_mm_ss(self):
        assert video_ingestor._format_timestamp(3725) == "01:02:05"

    def test_fractional_seconds_are_truncated(self):
        assert video_ingestor._format_timestamp(65.9) == "01:05"


# ---------------------------------------------------------------------------
# 3. ingest_video(): transcript-unavailable failure modes
# ---------------------------------------------------------------------------
class TestTranscriptUnavailable:
    def test_transcripts_disabled_raises_value_error(self, fake_yt_dlp, fake_transcript_api):
        fake_transcript_api.transcript = TranscriptsDisabled("vid123")
        with pytest.raises(ValueError, match="no transcript available"):
            video_ingestor.ingest_video("https://youtu.be/vid123")

    def test_no_transcript_found_raises_value_error(self, fake_yt_dlp, fake_transcript_api):
        fake_transcript_api.transcript = NoTranscriptFound("vid123", ["en"], None)
        with pytest.raises(ValueError, match="no transcript available"):
            video_ingestor.ingest_video("https://youtu.be/vid123")

    def test_empty_transcript_list_raises_value_error(self, fake_yt_dlp, fake_transcript_api):
        fake_transcript_api.transcript = []
        with pytest.raises(ValueError, match="empty transcript"):
            video_ingestor.ingest_video("https://youtu.be/vid123")

    def test_error_message_includes_the_url(self, fake_yt_dlp, fake_transcript_api):
        fake_transcript_api.transcript = TranscriptsDisabled("vid123")
        with pytest.raises(ValueError, match=r"https://youtu\.be/vid123"):
            video_ingestor.ingest_video("https://youtu.be/vid123")

    def test_bad_video_id_resolution_raises_before_transcript_call(self, fake_yt_dlp, fake_transcript_api):
        FakeYDL.info = None
        with pytest.raises(ValueError, match="could not resolve a video id"):
            video_ingestor.ingest_video("https://youtu.be/bad")
        assert fake_transcript_api.calls == []


# ---------------------------------------------------------------------------
# 4. ingest_video(): chunking into SECTION_LENGTH_SECONDS-wide sections
# ---------------------------------------------------------------------------
class TestChunking:
    def test_short_transcript_becomes_a_single_section(self, fake_yt_dlp, fake_transcript_api):
        fake_transcript_api.transcript = [
            _entry("hello", 0.0),
            _entry("world", 5.0),
        ]
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        assert len(artifact["sections"]) == 1
        assert artifact["sections"][0]["content"] == "hello world"
        assert artifact["sections"][0]["heading"] == "00:00"

    def test_transcript_spanning_two_chunks_produces_two_sections(self, fake_yt_dlp, fake_transcript_api):
        limit = video_ingestor.SECTION_LENGTH_SECONDS
        fake_transcript_api.transcript = [
            _entry("early line", 0.0),
            _entry("late line", limit + 1.0),
        ]
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        assert len(artifact["sections"]) == 2
        assert artifact["sections"][0]["content"] == "early line"
        assert artifact["sections"][1]["content"] == "late line"

    def test_section_heading_is_formatted_start_timestamp(self, fake_yt_dlp, fake_transcript_api):
        limit = video_ingestor.SECTION_LENGTH_SECONDS
        fake_transcript_api.transcript = [
            _entry("early line", 0.0),
            _entry("late line", limit + 5.0),
        ]
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        assert artifact["sections"][1]["heading"] == video_ingestor._format_timestamp(limit + 5.0)

    def test_every_section_has_empty_node_refs(self, fake_yt_dlp, fake_transcript_api):
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        for section in artifact["sections"]:
            assert section["node_refs"] == []

    def test_content_lines_are_joined_and_stripped(self, fake_yt_dlp, fake_transcript_api):
        fake_transcript_api.transcript = [
            _entry("  leading space", 0.0),
            _entry("trailing space  ", 1.0),
        ]
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        content = artifact["sections"][0]["content"]
        assert content == "leading space trailing space"


# ---------------------------------------------------------------------------
# 5. ingest_video(): top-level artifact shape
# ---------------------------------------------------------------------------
class TestArtifactShape:
    def test_returns_expected_top_level_keys(self, fake_yt_dlp, fake_transcript_api):
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        assert set(artifact.keys()) == {"title", "sections", "metadata"}

    def test_title_taken_from_resolved_video_title(self, fake_yt_dlp, fake_transcript_api):
        FakeYDL.info = {"id": "vid123", "title": "My Video", "webpage_url": GOOD_PAGE}
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        assert artifact["title"] == "My Video"

    def test_metadata_has_expected_fields(self, fake_yt_dlp, fake_transcript_api):
        artifact = video_ingestor.ingest_video("https://youtu.be/vid123")
        assert artifact["metadata"] == {
            "source_format": "video",
            "source_url": "https://youtu.be/vid123",
            "video_id": "vid123",
        }


# ---------------------------------------------------------------------------
# 6. SX-07: the URL is checked BEFORE yt-dlp (no yt-dlp, no transcript, no DNS)
# ---------------------------------------------------------------------------
REFUSED_URLS = [
    "https://youtube.com.evil.com/",
    "https://evil.com/?u=youtube.com",
    "https://youtu.be@evil.com/",
    "https://www.youtube.com@evil.com/watch?v=abc",
    "https://www.youtube.com\\@evil.com/",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost/",
    "file:///etc/passwd",
    "gopher://127.0.0.1/",
    "javascript:alert(1)",
    "https://www.youtube.com:8443/watch?v=abc",
    "https://www.youtube.com/watch?v=abc\r\nHost: evil",
    "",
    None,
    12345,
]


class TestUrlRefusedBeforeYtDlp:
    @pytest.mark.parametrize("url", REFUSED_URLS)
    def test_ingest_video_refuses_without_touching_yt_dlp(
            self, fake_yt_dlp, fake_transcript_api, no_dns, url):
        with pytest.raises(ValueError) as caught:
            video_ingestor.ingest_video(url)
        assert type(caught.value) is ValueError
        assert str(caught.value).startswith("not a supported video URL: ")
        if isinstance(url, str) and url:
            assert url not in str(caught.value)      # a refused URL is not echoed
        assert FakeYDL.instances == 0
        assert FakeYDL.extract_calls == []
        assert fake_transcript_api.calls == []
        assert no_dns == []

    @pytest.mark.parametrize("url", REFUSED_URLS)
    def test_video_id_refuses_without_touching_yt_dlp(self, fake_yt_dlp, url):
        with pytest.raises(ValueError, match="not a supported video URL"):
            video_ingestor._video_id(url)
        assert FakeYDL.instances == 0

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://m.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
        "https://youtube.com/watch?v=abc",
        "https://www.youtube-nocookie.com/embed/abc",
        "http://www.youtube.com/watch?v=abc",
    ])
    def test_youtube_urls_are_accepted(self, fake_yt_dlp, fake_transcript_api, no_dns, url):
        artifact = video_ingestor.ingest_video(url)
        assert FakeYDL.extract_calls == [url]
        assert artifact["metadata"]["source_url"] == url
        assert no_dns == []


# ---------------------------------------------------------------------------
# 7. SX-07: the page yt-dlp reports back must be on a YouTube host
# ---------------------------------------------------------------------------
class TestResolvedPageMustBeOnYouTube:
    @pytest.mark.parametrize("page", [
        "https://evil.com/watch?v=vid123",
        "https://youtube.com.evil.com/watch?v=vid123",
        "https://www.youtube.com@evil.com/watch?v=vid123",
        "https://evil.com@www.youtube.com/watch?v=vid123",
        "https://vimeo.com/12345",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "",
        None,
        42,
    ])
    def test_other_host_or_odd_webpage_url_is_refused(self, fake_yt_dlp, fake_transcript_api, page):
        FakeYDL.info = {"id": "vid123", "title": "T", "webpage_url": page}
        with pytest.raises(ValueError, match="the page is not on YouTube") as caught:
            video_ingestor.ingest_video("https://youtu.be/vid123")
        assert type(caught.value) is ValueError
        assert fake_transcript_api.calls == []          # no transcript is fetched for it

    def test_missing_webpage_url_is_refused(self, fake_yt_dlp, fake_transcript_api):
        FakeYDL.info = {"id": "vid123", "title": "T"}
        with pytest.raises(ValueError, match="the page is not on YouTube"):
            video_ingestor.ingest_video("https://youtu.be/vid123")
        assert fake_transcript_api.calls == []

    @pytest.mark.parametrize("host", sorted(video_ingestor.YOUTUBE_HOSTS))
    def test_every_youtube_host_is_accepted(self, fake_yt_dlp, fake_transcript_api, host):
        FakeYDL.info = {"id": "vid123", "title": "T", "webpage_url": f"https://{host}/watch?v=vid123"}
        assert video_ingestor.ingest_video("https://youtu.be/vid123")["metadata"]["video_id"] == "vid123"

    def test_id_failure_message_still_comes_first(self, fake_yt_dlp):
        FakeYDL.info = {"title": "No id", "webpage_url": "https://evil.com/"}
        with pytest.raises(ValueError, match="could not resolve a video id"):
            video_ingestor._video_id("https://youtu.be/bad")
