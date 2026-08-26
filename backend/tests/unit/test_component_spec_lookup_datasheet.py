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

eo/datasheet_cache.py's get_cached_datasheet/set_cached_datasheet are
faked at the module level (bound names inside component_spec_lookup),
same approach test_agent_academic_search.py uses for requests.get --
this file never touches the real cache/network.
"""
import pytest

from agents import component_spec_lookup as csl


class _FakeResponse:
    def __init__(self, content_type="text/html", status=200, raise_exc=None):
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def iter_content(self, chunk_size=65536):
        yield b"%PDF-1.4 fake pdf bytes"


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """Every test in this file starts with a clean, always-miss cache
    so retry/not-found logic is exercised deterministically."""
    monkeypatch.setattr(csl, "get_cached_datasheet", lambda url: None)
    monkeypatch.setattr(csl, "set_cached_datasheet", lambda url, result: None)


def test_confirmed_non_pdf_with_no_retry_hit_returns_sentinel(monkeypatch):
    """Non-PDF Content-Type on the primary URL, search() finds nothing
    usable -> DATASHEET_NOT_FOUND, not a plain None."""
    monkeypatch.setattr(
        csl.requests, "get",
        lambda url, timeout, stream: _FakeResponse(content_type="text/html"),
    )
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

    def _fake_get(url, timeout, stream):
        call_count["n"] += 1
        return _FakeResponse(content_type="text/html")

    monkeypatch.setattr(csl.requests, "get", _fake_get)

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

    def _fake_get(url, timeout, stream):
        urls_hit.append(url)
        if url == "https://vendor.example.com/products/qtr-8a":
            return _FakeResponse(content_type="text/html")
        return _FakeResponse(content_type="application/pdf")

    monkeypatch.setattr(csl.requests, "get", _fake_get)
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
