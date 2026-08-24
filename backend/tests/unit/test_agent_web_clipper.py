"""
tests/unit/test_agent_web_clipper.py — Patch 7f-4b-3.

Covers agents/web_clipper.py's clip_url(): the deterministic,
no-LLM-call web-page ingestor that fetches + extracts a URL into the
common artifact shape ({title, sections, metadata}) via trafilatura,
collapsing every real-world-flaky failure mode (dead link, paywall,
no extractable text) into a single ValueError for the caller.

trafilatura.fetch_url / trafilatura.extract are faked at the module
level (web_clipper.trafilatura), same "patch the bound import" shape
used across this suite (see tests/conftest.py's generate_text sweep)
-- this module never makes a real network call under test.
"""
import json

import pytest

from agents import web_clipper


def _extracted_json(text="Some real article body.", title="Article Title",
                     author="Jane Doe", date="2024-01-01"):
    return json.dumps({"text": text, "title": title, "author": author, "date": date})


@pytest.fixture
def fake_trafilatura(monkeypatch):
    """Defaults to a full successful fetch+extract. Tests override
    fetch_url/extract return values individually."""
    fake = type("FakeTrafilatura", (), {})()
    fake.fetch_url_calls = []
    fake.extract_calls = []

    def fetch_url(url, *args, **kwargs):
        fake.fetch_url_calls.append(url)
        return fake.downloaded

    def extract(downloaded, **kwargs):
        fake.extract_calls.append((downloaded, kwargs))
        return fake.extracted

    fake.downloaded = "<html>raw page</html>"
    fake.extracted = _extracted_json()
    monkeypatch.setattr(web_clipper.trafilatura, "fetch_url", fetch_url)
    monkeypatch.setattr(web_clipper.trafilatura, "extract", extract)
    return fake


# ---------------------------------------------------------------------------
# 1. Fetch failure -> ValueError, extract never called
# ---------------------------------------------------------------------------
class TestFetchFailure:
    def test_fetch_url_returning_none_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.downloaded = None
        with pytest.raises(ValueError, match="could not fetch"):
            web_clipper.clip_url("https://example.com/a")
        assert fake_trafilatura.extract_calls == []

    def test_fetch_url_returning_empty_string_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.downloaded = ""
        with pytest.raises(ValueError, match="could not fetch"):
            web_clipper.clip_url("https://example.com/a")
        assert fake_trafilatura.extract_calls == []

    def test_error_message_includes_the_url(self, fake_trafilatura):
        fake_trafilatura.downloaded = None
        with pytest.raises(ValueError, match=r"https://example\.com/dead-link"):
            web_clipper.clip_url("https://example.com/dead-link")

    def test_fetch_url_called_with_the_given_url(self, fake_trafilatura):
        web_clipper.clip_url("https://example.com/page")
        assert fake_trafilatura.fetch_url_calls == ["https://example.com/page"]


# ---------------------------------------------------------------------------
# 2. Extract failure -> ValueError
# ---------------------------------------------------------------------------
class TestExtractFailure:
    def test_extract_returning_none_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.extracted = None
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_extract_returning_empty_string_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.extracted = ""
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_extract_called_with_downloaded_content_and_expected_kwargs(self, fake_trafilatura):
        web_clipper.clip_url("https://example.com/a")
        downloaded, kwargs = fake_trafilatura.extract_calls[0]
        assert downloaded == fake_trafilatura.downloaded
        assert kwargs["output_format"] == "json"
        assert kwargs["with_metadata"] is True
        assert kwargs["include_comments"] is False
        assert kwargs["include_tables"] is True

    def test_error_message_includes_the_url(self, fake_trafilatura):
        fake_trafilatura.extracted = None
        with pytest.raises(ValueError, match=r"https://example\.com/paywalled"):
            web_clipper.clip_url("https://example.com/paywalled")


# ---------------------------------------------------------------------------
# 3. Extracted JSON with no usable text -> ValueError
# ---------------------------------------------------------------------------
class TestEmptyExtractedText:
    def test_missing_text_key_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.extracted = json.dumps({"title": "T"})
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_whitespace_only_text_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.extracted = json.dumps({"text": "   \n  "})
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")

    def test_empty_string_text_raises_value_error(self, fake_trafilatura):
        fake_trafilatura.extracted = json.dumps({"text": ""})
        with pytest.raises(ValueError, match="no extractable content"):
            web_clipper.clip_url("https://example.com/a")


# ---------------------------------------------------------------------------
# 4. Successful extraction -> artifact shape
# ---------------------------------------------------------------------------
class TestSuccessfulClip:
    def test_returns_expected_top_level_shape(self, fake_trafilatura):
        artifact = web_clipper.clip_url("https://example.com/a")
        assert set(artifact.keys()) == {"title", "sections", "metadata"}

    def test_content_is_stripped_of_surrounding_whitespace(self, fake_trafilatura):
        fake_trafilatura.extracted = _extracted_json(text="  \n  body text here  \n  ")
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["sections"][0]["content"] == "body text here"

    def test_single_section_with_empty_heading_and_no_node_refs(self, fake_trafilatura):
        artifact = web_clipper.clip_url("https://example.com/a")
        assert len(artifact["sections"]) == 1
        section = artifact["sections"][0]
        assert section["heading"] == ""
        assert section["node_refs"] == []
        assert section["content"] == "Some real article body."

    def test_title_taken_from_extracted_data(self, fake_trafilatura):
        fake_trafilatura.extracted = _extracted_json(title="Real Title")
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["title"] == "Real Title"

    def test_missing_title_falls_back_to_url(self, fake_trafilatura):
        fake_trafilatura.extracted = json.dumps({"text": "body", "author": None, "date": None})
        artifact = web_clipper.clip_url("https://example.com/no-title")
        assert artifact["title"] == "https://example.com/no-title"

    def test_empty_string_title_falls_back_to_url(self, fake_trafilatura):
        fake_trafilatura.extracted = json.dumps(
            {"text": "body", "title": "", "author": None, "date": None}
        )
        artifact = web_clipper.clip_url("https://example.com/blank-title")
        assert artifact["title"] == "https://example.com/blank-title"

    def test_metadata_fields_populated_from_extracted_data(self, fake_trafilatura):
        fake_trafilatura.extracted = _extracted_json(author="A. Writer", date="2023-06-01")
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["metadata"] == {
            "source_format": "web",
            "source_url": "https://example.com/a",
            "author": "A. Writer",
            "date": "2023-06-01",
        }

    def test_metadata_author_and_date_default_to_none_when_absent(self, fake_trafilatura):
        fake_trafilatura.extracted = json.dumps({"text": "body", "title": "T"})
        artifact = web_clipper.clip_url("https://example.com/a")
        assert artifact["metadata"]["author"] is None
        assert artifact["metadata"]["date"] is None

    def test_source_url_matches_the_requested_url_exactly(self, fake_trafilatura):
        artifact = web_clipper.clip_url("https://example.com/exact-path?x=1")
        assert artifact["metadata"]["source_url"] == "https://example.com/exact-path?x=1"
