"""
tests/unit/test_component_spec_lookup_datasheet.py — Patch L.3/L.4.

Covers agents/component_spec_lookup.py's get_datasheet_detail(): when a
resolved datasheet_url turns out not to be a PDF (Content-Type/extension
check fails), this now makes ONE retry against a reformulated search
query (utils/web_search.py's search()) before giving up, and returns
the DATASHEET_NOT_FOUND sentinel -- rather than a plain None -- when
that retry also fails to turn up a usable PDF, so a caller can tell
"confirmed nothing here" apart from any other kind of lookup failure.

Also covers hardware_speccer.py's _populate_datasheet_details(): the
caller that actually nulls out a part's datasheet_url field when it
gets DATASHEET_NOT_FOUND back.

Patch SX-08: the datasheet download now goes through
utils.url_guard.safe_stream, which is what these tests fake (as
csl.safe_stream) instead of requests.get. DNS and socket connects are
blocked for the whole module, so nothing here can reach the network; the
tests that run the REAL safe_stream (private addresses) prove it refuses
before any lookup or connection.

eo/datasheet_cache.py's get_cached_datasheet/set_cached_datasheet are
faked at the module level (bound names inside component_spec_lookup),
same approach test_agent_academic_search.py uses for requests.get --
this file never touches the real cache/network.
"""
import contextlib
import os
import socket
import tempfile

import pytest
import requests

from agents import component_spec_lookup as csl
from utils.url_guard import FetchError, FetchTimeout, ResponseTooLarge


class _FakeResponse:
    """What safe_stream yields: .headers, .status, .url and iter_bytes()."""

    def __init__(self, content_type="text/html", chunks=(b"%PDF-1.4 fake pdf bytes",), fail_after=None):
        self.headers = {"Content-Type": content_type}
        self.status = 200
        self.url = ""
        self._chunks = chunks
        self._fail_after = fail_after      # an exception raised after the first chunk

    def iter_bytes(self, max_bytes=None):
        for i, chunk in enumerate(self._chunks):
            yield chunk
            if i == 0 and self._fail_after is not None:
                raise self._fail_after


def _fake_safe_stream(monkeypatch, handler):
    """Replace csl.safe_stream. handler(url, **kwargs) returns a _FakeResponse
    or raises (a raise stands for the guard refusing / the fetch failing).
    Returns the list of (url, kwargs) calls."""
    calls = []

    @contextlib.contextmanager
    def fake(url, **kwargs):
        calls.append((url, kwargs))
        yield handler(url, **kwargs)

    monkeypatch.setattr(csl, "safe_stream", fake)
    return calls


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any DNS lookup or socket connect in this module is a bug. Recorded as
    well as refused, because url_guard turns a resolver error into 'could not
    be resolved' and would hide it."""
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


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """Every test in this file starts with a clean, always-miss cache
    so retry/not-found logic is exercised deterministically."""
    monkeypatch.setattr(csl, "get_cached_datasheet", lambda url: None)
    monkeypatch.setattr(csl, "set_cached_datasheet", lambda url, result: None)


def test_confirmed_non_pdf_with_no_retry_hit_returns_sentinel(monkeypatch):
    """Non-PDF Content-Type on the primary URL, search() finds nothing
    usable -> DATASHEET_NOT_FOUND, not a plain None."""
    _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse(content_type="text/html"))
    # search() is imported lazily inside _search_for_pdf_datasheet(), so
    # patch it at its real home instead of on the csl module object.
    import utils.web_search as web_search
    monkeypatch.setattr(web_search, "search", lambda *a, **k: [])

    result = csl.get_datasheet_detail(
        "https://vendor.example.com/products/qtr-8a", part_number="QTR-8A"
    )
    assert result is csl.DATASHEET_NOT_FOUND


def test_confirmed_non_pdf_with_no_part_number_skips_retry_returns_sentinel(monkeypatch):
    """No part_number given at all -> _search_for_pdf_datasheet() is
    never even worth calling out to; still DATASHEET_NOT_FOUND, not a
    lookup that silently returns the bad URL's non-existent content."""
    call_count = {"n": 0}

    def _fake_get(url, **kw):
        call_count["n"] += 1
        return _FakeResponse(content_type="text/html")

    _fake_safe_stream(monkeypatch, _fake_get)

    result = csl.get_datasheet_detail("https://vendor.example.com/products/qtr-8a")
    assert result is csl.DATASHEET_NOT_FOUND
    # Only the one primary-URL request -- no retry attempted without a
    # part_number to build a reformulated query from.
    assert call_count["n"] == 1


def test_non_pdf_retry_finds_a_real_pdf(monkeypatch):
    """Primary URL is HTML; the reformulated-query retry turns up a
    second URL that IS a real PDF -- exactly one retry, and the parsed
    detail from the retry URL comes back normally (not the sentinel)."""
    urls_hit = []

    def _fake_get(url, **kw):
        urls_hit.append(url)
        if url == "https://vendor.example.com/products/qtr-8a":
            return _FakeResponse(content_type="text/html")
        return _FakeResponse(content_type="application/pdf")

    _fake_safe_stream(monkeypatch, _fake_get)
    monkeypatch.setattr(
        csl, "_fetch_and_parse_pdf",
        lambda url: (
            (_ for _ in ()).throw(csl._NotAPdfError(url))
            if url == "https://vendor.example.com/products/qtr-8a"
            else {"title": "QTR-8A Datasheet", "content": "reflectance sensor array...", "page_count": 4}
        ),
    )

    import utils.web_search as web_search
    monkeypatch.setattr(
        web_search, "search",
        lambda query, max_results=3, agent_name="web_search": [
            {"url": "https://cdn.example.com/QTR-8A.pdf", "title": "QTR-8A", "snippet": "..."},
        ],
    )

    result = csl.get_datasheet_detail(
        "https://vendor.example.com/products/qtr-8a", part_number="QTR-8A"
    )
    assert result == {
        "title": "QTR-8A Datasheet",
        "content": "reflectance sensor array...",
        "page_count": 4,
    }


def test_retry_also_non_pdf_returns_sentinel(monkeypatch):
    """Both the primary URL AND the retry's top search result are
    confirmed non-PDF -> still DATASHEET_NOT_FOUND (capped at exactly
    one retry, never a second search)."""
    search_call_count = {"n": 0}

    def _fake_fetch(url):
        raise csl._NotAPdfError(url)

    monkeypatch.setattr(csl, "_fetch_and_parse_pdf", _fake_fetch)

    import utils.web_search as web_search

    def _fake_search(query, max_results=3, agent_name="web_search"):
        search_call_count["n"] += 1
        return [{"url": "https://vendor.example.com/also-html", "title": "x", "snippet": ""}]

    monkeypatch.setattr(web_search, "search", _fake_search)

    result = csl.get_datasheet_detail(
        "https://vendor.example.com/products/qtr-8a", part_number="QTR-8A"
    )
    assert result is csl.DATASHEET_NOT_FOUND
    assert search_call_count["n"] == 1


def test_no_part_number_and_no_url_still_returns_none_for_no_url():
    """Sanity check the pre-existing no-datasheet_url-at-all behavior is
    unchanged by this patch -- still a plain None, never the sentinel."""
    assert csl.get_datasheet_detail("") is None
    assert csl.get_datasheet_detail(None) is None


# ---------------------------------------------------------------------------
# SX-08: the download itself (safe_stream, chunks, cap, timeouts, temp file)
# ---------------------------------------------------------------------------
DATASHEET = "https://vendor.example.com/ds/qtr-8a.pdf"
PARSED = {"title": "T", "sections": [{"content": "body text"}], "metadata": {"page_count": 3}}


@pytest.fixture
def downloads(monkeypatch, tmp_path):
    """Temp files go to an empty directory we can inspect; ingest_pdf is
    faked and records what file it was given and what that file held."""
    folder = tmp_path / "dl"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    state = type("Downloads", (), {})()
    state.folder = folder
    state.ingested = []

    def ingest_pdf(path):
        with open(path, "rb") as fh:
            state.ingested.append((path, fh.read()))
        return PARSED

    import agents.pdf_ingestor as pdf_ingestor
    monkeypatch.setattr(pdf_ingestor, "ingest_pdf", ingest_pdf)
    return state


class TestDownload:
    def test_goes_through_safe_stream_with_cap_and_timeouts(self, monkeypatch, downloads):
        calls = _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse("application/pdf"))
        result = csl.get_datasheet_detail(DATASHEET, part_number="QTR-8A")
        assert result == {"title": "T", "content": "body text", "page_count": 3}
        (url, kwargs), = calls
        assert url == DATASHEET
        assert kwargs["max_bytes"] == csl.DATASHEET_MAX_BYTES == 25_000_000
        assert kwargs["timeout"] == csl.DATASHEET_REQUEST_TIMEOUT
        assert kwargs["total_timeout"] == 2 * csl.DATASHEET_REQUEST_TIMEOUT

    def test_body_is_written_chunk_by_chunk_and_temp_file_removed(self, monkeypatch, downloads):
        chunks = (b"%PDF-1.4 ", b"middle ", b"end")
        _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse("application/pdf", chunks=chunks))
        csl.get_datasheet_detail(DATASHEET)
        (path, content), = downloads.ingested
        assert content == b"%PDF-1.4 middle end"
        assert path.endswith(".pdf")
        assert os.listdir(downloads.folder) == []

    def test_pdf_extension_is_enough_even_with_a_generic_content_type(self, monkeypatch, downloads):
        _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse("application/octet-stream"))
        assert csl.get_datasheet_detail(DATASHEET)["title"] == "T"

    def test_pdf_content_type_is_enough_without_an_extension(self, monkeypatch, downloads):
        _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse("application/pdf; qs=0.9"))
        assert csl.get_datasheet_detail("https://vendor.example.com/download?id=7")["title"] == "T"

    def test_not_a_pdf_downloads_nothing(self, monkeypatch, downloads):
        _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse("text/html"))
        assert csl.get_datasheet_detail("https://vendor.example.com/page") is csl.DATASHEET_NOT_FOUND
        assert downloads.ingested == []
        assert os.listdir(downloads.folder) == []


class TestPrivateAddressesAreRefused:
    """The REAL safe_stream: the download returns the normal 'nothing here'
    result (None) and nothing is looked up, connected to or ingested."""

    @pytest.mark.parametrize("url", [
        "http://10.0.0.5/ds.pdf",
        "http://169.254.169.254/latest/meta-data/ds.pdf",
        "http://127.0.0.1/ds.pdf",
        "http://localhost/ds.pdf",
        "http://[::1]/ds.pdf",
        "http://2130706433/ds.pdf",
        "file:///etc/passwd",
        "https://user@vendor.example.com/ds.pdf",
    ])
    def test_literal_private_urls_return_none_without_dns_or_connect(self, downloads, no_network, url):
        assert csl.get_datasheet_detail(url, part_number="QTR-8A") is None
        assert no_network.dns == [] and no_network.connects == []
        assert downloads.ingested == []
        assert os.listdir(downloads.folder) == []

    def test_name_that_resolves_to_a_private_address_is_refused_before_connecting(
            self, monkeypatch, downloads, no_network):
        lookups = []

        def getaddrinfo(host, port, *args, **kwargs):
            lookups.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        assert csl.get_datasheet_detail("https://cdn.vendor.example/ds.pdf") is None
        assert lookups                                   # looked up ...
        assert no_network.connects == []                 # ... never connected to
        assert downloads.ingested == []

    def test_a_refused_url_is_not_treated_as_not_a_pdf(self, monkeypatch, downloads):
        """A refusal is a plain failure (None), so it does not trigger the
        reformulated-query search the way a confirmed non-PDF does."""
        import utils.web_search as web_search
        searches = []
        monkeypatch.setattr(web_search, "search", lambda *a, **k: searches.append(a) or [])
        assert csl.get_datasheet_detail("http://10.0.0.5/ds.pdf", part_number="QTR-8A") is None
        assert searches == []


class TestTooLarge:
    def test_declared_size_over_the_cap_returns_none(self, monkeypatch, downloads):
        def handler(url, **kw):
            raise ResponseTooLarge("The response is too large")
        _fake_safe_stream(monkeypatch, handler)
        assert csl.get_datasheet_detail(DATASHEET) is None
        assert downloads.ingested == []

    def test_body_that_outgrows_the_cap_mid_download_returns_none_and_leaves_no_file(
            self, monkeypatch, downloads):
        _fake_safe_stream(monkeypatch, lambda url, **kw: _FakeResponse(
            "application/pdf", chunks=(b"%PDF-first-chunk", b"never written"),
            fail_after=ResponseTooLarge("The response is too large")))
        assert csl.get_datasheet_detail(DATASHEET) is None
        assert downloads.ingested == []
        assert os.listdir(downloads.folder) == []        # the half-written file was removed


class TestFailuresAndTimeouts:
    def test_http_error_returns_none_without_a_retry(self, monkeypatch, downloads):
        def handler(url, **kw):
            raise FetchError("The server did not return the page")
        calls = _fake_safe_stream(monkeypatch, handler)
        assert csl.get_datasheet_detail(DATASHEET) is None
        assert len(calls) == 1

    def test_timeout_is_a_requests_timeout_so_the_retry_wrapper_works(self):
        assert isinstance(FetchTimeout(), requests.exceptions.Timeout)

    def test_timeout_triggers_one_retry_with_the_longer_budget(self, monkeypatch, downloads):
        attempts = []

        def handler(url, **kw):
            attempts.append(kw["timeout"])
            if len(attempts) == 1:
                raise FetchTimeout("The server did not answer in time")
            return _FakeResponse("application/pdf")

        _fake_safe_stream(monkeypatch, handler)
        result = csl.get_datasheet_detail(DATASHEET)
        assert result["title"] == "T"
        assert attempts == [csl.DATASHEET_REQUEST_TIMEOUT, csl.DATASHEET_REQUEST_TIMEOUT_RETRY]

    def test_timeout_in_the_middle_of_the_body_also_retries_and_leaves_no_file(
            self, monkeypatch, downloads):
        attempts = []

        def handler(url, **kw):
            attempts.append(kw["timeout"])
            if len(attempts) == 1:
                return _FakeResponse("application/pdf", chunks=(b"%PDF-start", b"x"),
                                     fail_after=FetchTimeout())
            return _FakeResponse("application/pdf")

        _fake_safe_stream(monkeypatch, handler)
        assert csl.get_datasheet_detail(DATASHEET)["title"] == "T"
        assert len(attempts) == 2
        assert os.listdir(downloads.folder) == []

    def test_two_timeouts_give_up_with_none(self, monkeypatch, downloads):
        def handler(url, **kw):
            raise FetchTimeout()
        calls = _fake_safe_stream(monkeypatch, handler)
        assert csl.get_datasheet_detail(DATASHEET) is None
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# hardware_speccer.py's _populate_datasheet_details(): the caller that
# nulls out part["datasheet_url"] on DATASHEET_NOT_FOUND
# ---------------------------------------------------------------------------

def test_populate_datasheet_details_nulls_out_confirmed_not_found(monkeypatch):
    import agents.hardware_speccer as hs

    def _fake_get_datasheet_detail(url, part_number=None):
        if part_number == "QTR-8A":
            return csl.DATASHEET_NOT_FOUND
        return {"title": "t", "content": "c", "page_count": 1}

    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        _fake_get_datasheet_detail,
    )

    parts = [
        {"id": "p1", "part_number": "QTR-8A", "datasheet_url": "https://vendor.example.com/qtr-8a"},
        {"id": "p2", "part_number": "ESP32-WROOM-32", "datasheet_url": "https://vendor.example.com/esp32.pdf"},
    ]

    details = hs._populate_datasheet_details(parts)

    assert parts[0]["datasheet_url"] is None
    assert parts[1]["datasheet_url"] == "https://vendor.example.com/esp32.pdf"
    assert "p1" not in details
    assert details["p2"] == {"title": "t", "content": "c", "page_count": 1}
