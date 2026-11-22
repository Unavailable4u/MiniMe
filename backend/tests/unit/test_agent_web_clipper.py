"""
tests/unit/test_agent_web_clipper.py — Patch 7f-4b-3, updated by SX-06.

Covers agents/web_clipper.py's clip_url(): the deterministic,
no-LLM-call web-page ingestor that fetches + extracts a URL into the
common artifact shape ({title, sections, metadata}), collapsing every
real-world-flaky failure mode (dead link, paywall, refused address, no
extractable text) into a single ValueError for the caller.

Since SX-06 the page is fetched with utils.url_guard.safe_get, not
trafilatura's own downloader. safe_get / trafilatura.extract are faked
at the module level (web_clipper.safe_get, web_clipper.trafilatura),
same "patch the bound import" shape used across this suite (see
tests/conftest.py's generate_text sweep). A module-wide autouse fixture
also blocks real DNS and real socket connects, so nothing here can
reach the network; the tests that run the REAL safe_get (the refused
addresses) prove it refuses before any DNS lookup or connection.
"""
import json
import socket

import pytest

from agents import web_clipper
from utils.url_guard import (
    FetchError,
    FetchTimeout,
    ResponseTooLarge,
    UnsafeURL,
)


def _extracted_json(text="Some real article body.", title="Article Title",
                     author="Jane Doe", date="2024-01-01"):
    return json.dumps({"text": text, "title": title, "author": author, "date": date})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any DNS lookup or socket connect in this module is a bug. The calls
    are recorded as well as refused, because url_guard turns an exception
    from its resolver into 'could not be resolved' and would hide it."""
    calls = type("NetworkCalls", (), {})()
    calls.dns, calls.connects = [], []

    def getaddrinfo(host, *args, **kwargs):
        calls.dns.append(host)
        raise AssertionError(f"unexpected DNS lookup for {host!r}")

    def connect(self, address):
        calls.connects.append(address)
        raise AssertionError(f"unexpected socket connect to {address!r}")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    return calls


@pytest.fixture
def fake_clip(monkeypatch):
    """Defaults to a full successful fetch+extract. Tests override
    downloaded / extracted / fetch_error individually. trafilatura's own
    downloader must never be used any more, so it fails the test if called."""
    fake = type("FakeClip", (), {})()
    fake.safe_get_calls = []
    fake.extract_calls = []

    def safe_get(url, **kwargs):
        fake.safe_get_calls.append((url, kwargs))
        if fake.fetch_error is not None:
            raise fake.fetch_error
        return url, 200, {"Content-Type": "text/html"}, fake.downloaded

    def extract(downloaded, **kwargs):
        fake.extract_calls.append((downloaded, kwargs))
        return fake.extracted

    def fetch_url(*args, **kwargs):
        raise AssertionError("trafilatura.fetch_url must not be used (SX-06)")

    fake.downloaded = b"<html>raw page</html>"
    fake.extracted = _extracted_json()
    fake.fetch_error = None
    monkeypatch.setattr(web_clipper, "safe_get", safe_get)
    monkeypatch.setattr(web_clipper.trafilatura, "fetch_url", fetch_url)
    monkeypatch.setattr(web_clipper.trafilatura, "extract", extract)
    return fake


# ---------------------------------------------------------------------------
# 1. Fetch failure -> ValueError, extract never called
# ---------------------------------------------------------------------------
class TestFetchFailure:
    def test_empty_body_raises_value_error(self, fake_clip):
        fake_clip.downloaded = b""
        with pytest.raises(ValueError, match="could not fetch"):
            web_clipper.clip_url("https://example.com/a")
        assert fake_clip.extract_calls == []

    def test_error_message_includes_the_url(self, fake_clip):
        fake_clip.downloaded = b""
        with pytest.raises(ValueError, match=r"https://example\.com/dead-link"):
            web_clipper.clip_url("https://example.com/dead-link")

    def test_safe_get_called_with_the_given_url(self, fake_clip):
        web_clipper.clip_url("https://example.com/page")
        assert [url for url, _ in fake_clip.safe_get_calls] == ["https://example.com/page"]

    def test_safe_get_called_with_the_size_cap_and_html_content_types(self, fake_clip):
        web_clipper.clip_url("https://example.com/page")
        _, kwargs = fake_clip.safe_get_calls[0]
        assert kwargs["max_bytes"] == web_clipper.CLIP_MAX_BYTES == 5_000_000
        assert tuple(kwargs["allowed_content_types"]) == ("text/html", "application/xhtml+xml")

    def test_downloaded_bytes_go_to_extract_unchanged(self, fake_clip):
        fake_clip.downloaded = b"<html>exact bytes</html>"
        web_clipper.clip_url("https://example.com/a")
        assert fake_clip.extract_calls[0][0] == b"<html>exact bytes</html>"


# ---------------------------------------------------------------------------
# 1b. A failure inside safe_get -> exactly one ValueError, extract never called
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("error", [
    FetchError("The server did not return the page"),
    ResponseTooLarge("The response is too large"),
    FetchTimeout("The server did not answer in time"),
])
class TestSafeGetFailure:
    def test_becomes_a_plain_value_error_naming_url_and_reason(self, fake_clip, error):
        fake_clip.fetch_error = error
        with pytest.raises(ValueError) as caught:
            web_clipper.clip_url("https://example.com/slow")
        assert type(caught.value) is ValueError          # one exception type for the route
        message = str(caught.value)
        assert message.startswith("could not fetch https://example.com/slow")
        assert str(error) in message
        assert fake_clip.extract_calls == []


class TestRefusedByTheGuard:
    def test_unsafe_url_becomes_a_plain_value_error_without_the_url(self, fake_clip):
        fake_clip.fetch_error = UnsafeURL("URL destination is not allowed")
        with pytest.raises(ValueError) as caught:
            web_clipper.clip_url("http://internal.example/secret")
        assert type(caught.value) is ValueError
        assert str(caught.value) == "could not fetch: URL destination is not allowed"
        assert "internal.example" not in str(caught.value)
        assert fake_clip.extract_calls == []


# ---------------------------------------------------------------------------
# 1c. The REAL guard: a private address is refused before any fetch.
#     safe_get is NOT faked here; no_network records any DNS or connect.
# ---------------------------------------------------------------------------
class TestPrivateAddressRefusedBeforeAnyFetch:
    @pytest.fixture(autouse=True)
    def only_extract_is_faked(self, monkeypatch):
        self.extract_calls = []
        monkeypatch.setattr(web_clipper.trafilatura, "extract",
                            lambda *a, **k: self.extract_calls.append(a))

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://2130706433/",
        "file:///etc/passwd",
        "http://user@example.com/",
    ])
    def test_refused_with_no_dns_and_no_connection(self, no_network, url):
        with pytest.raises(ValueError) as caught:
            web_clipper.clip_url(url)
        assert type(caught.value) is ValueError
        assert str(caught.value).startswith("could not fetch: ")
        assert url not in str(caught.value)
        assert no_network.dns == []
        assert no_network.connects == []
        assert self.extract_calls == []

    def test_name_that_resolves_to_a_private_address_is_refused_before_connecting(
            self, monkeypatch, no_network):
        lookups = []

        def getaddrinfo(host, port, *args, **kwargs):
            lookups.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        with pytest.raises(ValueError, match="could not fetch: URL destination is not allowed"):
            web_clipper.clip_url("https://intranet.example/page")
        assert lookups                                   # the name WAS looked up ...
        assert no_network.connects == []                 # ... and nothing was connected to
        assert self.extract_calls == []


# ---------------------------------------------------------------------------
# 2. Extract failure -> ValueError
# ---------------------------------------------------------------------------
class TestExtractFailure:
    def test_extract_returning_none_raises_value_error(self, fake_clip):
        fake_clip.extracted = None
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_extract_returning_empty_string_raises_value_error(self, fake_clip):
        fake_clip.extracted = ""
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_extract_called_with_downloaded_content_and_expected_kwargs(self, fake_clip):
        web_clipper.clip_url("https://example.com/a")
        downloaded, kwargs = fake_clip.extract_calls[0]
        assert downloaded == fake_clip.downloaded
        assert kwargs["output_format"] == "json"
        assert kwargs["with_metadata"] is True
        assert kwargs["include_comments"] is False
        assert kwargs["include_tables"] is True

    def test_error_message_includes_the_url(self, fake_clip):
        fake_clip.extracted = None
        with pytest.raises(ValueError, match=r"https://example\.com/paywalled"):
            web_clipper.clip_url("https://example.com/paywalled")


# ---------------------------------------------------------------------------
# 3. Extracted JSON with no usable text -> ValueError
# ---------------------------------------------------------------------------
class TestEmptyExtractedText:
    def test_missing_text_key_raises_value_error(self, fake_clip):
        fake_clip.extracted = json.dumps({"title": "T"})
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_whitespace_only_text_raises_value_error(self, fake_clip):
        fake_clip.extracted = json.dumps({"text": "   \n  "})
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_empty_string_text_raises_value_error(self, fake_clip):
        fake_clip.extracted = json.dumps({"text": ""})
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")


# ---------------------------------------------------------------------------
# 4. Successful extraction -> artifact shape
# ---------------------------------------------------------------------------
class TestSuccessfulClip:
    def test_returns_expected_top_level_shape(self, fake_clip):
        artifact = web_clipper.clip_url("https://example.com/a")
        assert set(artifact.keys()) == {"title", "sections", "metadata"}

    def test_content_is_stripped_of_surrounding_whitespace(self, fake_clip):
        fake_clip.extracted = _extracted_json(text="  \n  body text here  \n  ")
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["sections"][0]["content"] == "body text here"

    def test_single_section_with_empty_heading_and_no_node_refs(self, fake_clip):
        artifact = web_clipper.clip_url("https://example.com/a")
        assert len(artifact["sections"]) == 1
        section = artifact["sections"][0]
        assert section["heading"] == ""
        assert section["node_refs"] == []
        assert section["content"] == "Some real article body."

    def test_title_taken_from_extracted_data(self, fake_clip):
        fake_clip.extracted = _extracted_json(title="Real Title")
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["title"] == "Real Title"

    def test_missing_title_falls_back_to_url(self, fake_clip):
        fake_clip.extracted = json.dumps({"text": "body", "author": None, "date": None})
        artifact = web_clipper.clip_url("https://example.com/no-title")
        assert artifact["title"] == "https://example.com/no-title"

    def test_empty_string_title_falls_back_to_url(self, fake_clip):
        fake_clip.extracted = json.dumps(
            {"text": "body", "title": "", "author": None, "date": None}
        )
        artifact = web_clipper.clip_url("https://example.com/blank-title")
        assert artifact["title"] == "https://example.com/blank-title"

    def test_metadata_fields_populated_from_extracted_data(self, fake_clip):
        fake_clip.extracted = _extracted_json(author="A. Writer", date="2023-06-01")
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["metadata"] == {
            "source_format": "web",
            "source_url": "https://example.com/a",
            "author": "A. Writer",
            "date": "2023-06-01",
        }

    def test_metadata_author_and_date_default_to_none_when_absent(self, fake_clip):
        fake_clip.extracted = json.dumps({"text": "body", "title": "T"})
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["metadata"]["author"] is None
        assert artifact["metadata"]["date"] is None

    def test_source_url_matches_the_requested_url_exactly(self, fake_clip):
        artifact = web_clipper.clip_url("https://example.com/exact-path?x=1")
        assert artifact["metadata"]["source_url"] == "https://example.com/exact-path?x=1"


# ---------------------------------------------------------------------------
# 5. A normal page still produces text (REAL trafilatura.extract, fetched
#    bytes from a faked safe_get)
# ---------------------------------------------------------------------------
_ARTICLE_HTML = b"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Why Bridges Sing</title>
<meta name="author" content="Jane Doe"></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article>
<h1>Why Bridges Sing</h1>
<p>Long suspension bridges hum in the wind because the cables shed small vortices at a steady rate. When that rate
matches a natural frequency of a cable, the cable starts to vibrate, and the sound can carry a long way.</p>
<p>Engineers damp these vibrations with small dampers near the anchorages, with helical strakes on the cables, and
with careful shaping of the deck. None of this is new: the same effect is why telephone wires whistle in a gale.</p>
<p>Measuring the effect is simple. A microphone near a cable and an accelerometer clamped to it are enough to show
the same peak in both signals, which is how the cause was first confirmed on a real bridge.</p>
</article>
<footer>Copyright example.com</footer>
</body></html>"""


class TestRealExtraction:
    def test_normal_page_produces_text_from_the_fetched_bytes(self, monkeypatch):
        monkeypatch.setattr(
            web_clipper, "safe_get",
            lambda url, **kw: (url, 200, {"Content-Type": "text/html; charset=utf-8"}, _ARTICLE_HTML),
        )
        artifact = web_clipper.clip_url("https://example.com/bridges")
        content = artifact["sections"][0]["content"]
        assert "vortices" in content
        assert "helical strakes" in content
        assert "Copyright example.com" not in content        # boilerplate stripped
        assert artifact["metadata"]["source_format"] == "web"
        assert artifact["metadata"]["source_url"] == "https://example.com/bridges"
