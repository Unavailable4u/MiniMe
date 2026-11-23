"""Patch SX-22 -- agents/pdf_ingestor.py::ingest_pdf refuses a PDF that has too many pages or
too much text, with a ValueError (which /api/notes/pdf already turns into HTTP 400).

PDFs are generated on the fly with reportlab (already a pinned dependency), the same way
tests/integration/test_pii_scrub_ingestors.py does. PII scrubbing is replaced by an identity
function so the tests do not load Presidio; the cap check happens before scrubbing anyway,
and a test below proves nothing is scrubbed once a cap is exceeded. Nothing here skips.
"""
import pytest
from reportlab.pdfgen import canvas

from agents import pdf_ingestor
from agents.pdf_ingestor import ingest_pdf


def _make_pdf(path, pages: int) -> str:
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 720, f"page {i + 1} of the test document")
        c.showPage()
    c.save()
    return str(path)


class _Counter:
    """Wraps a callable and counts calls."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.fn(*args, **kwargs)


@pytest.fixture
def scrub_calls(monkeypatch):
    counter = _Counter(lambda text, *a, **k: text)
    monkeypatch.setattr(pdf_ingestor, "scrub", counter)
    return counter


def test_the_placeholder_limits():
    assert pdf_ingestor.PDF_MAX_PAGES == 500
    assert pdf_ingestor.PDF_MAX_CHARS == 2_000_000


# ---- page cap ---------------------------------------------------------------------------------

def test_a_pdf_with_exactly_the_page_cap_is_ingested(tmp_path, monkeypatch, scrub_calls):
    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_PAGES", 3)
    artifact = ingest_pdf(_make_pdf(tmp_path / "ok.pdf", 3))
    assert artifact["metadata"]["page_count"] == 3
    assert len(artifact["metadata"]["page_breaks"]) == 3
    content = artifact["sections"][0]["content"]
    assert "page 1 of the test document" in content  # real text extraction is unchanged
    assert "page 3 of the test document" in content


def test_a_pdf_over_the_page_cap_is_refused_before_any_text_is_extracted(tmp_path, monkeypatch, scrub_calls):
    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_PAGES", 3)
    extract = _Counter(pdf_ingestor._extract_page_text)
    monkeypatch.setattr(pdf_ingestor, "_extract_page_text", extract)
    path = _make_pdf(tmp_path / "big.pdf", 4)
    with pytest.raises(ValueError) as exc:
        ingest_pdf(path)
    assert str(exc.value) == "This PDF has 4 pages; the limit is 3 pages."
    assert extract.calls == 0
    assert scrub_calls.calls == 0  # not even the title was scrubbed


def test_the_real_default_page_cap_refuses_501_pages(tmp_path, scrub_calls, monkeypatch):
    def never(page):
        raise AssertionError("text extraction ran on a PDF that is over the page cap")

    monkeypatch.setattr(pdf_ingestor, "_extract_page_text", never)
    path = _make_pdf(tmp_path / "501.pdf", 501)
    with pytest.raises(ValueError, match=r"501 pages; the limit is 500 pages"):
        ingest_pdf(path)


# ---- character cap ----------------------------------------------------------------------------

def _fake_page_texts(monkeypatch, texts):
    """Make _extract_page_text return the given strings in order; returns its call counter."""
    it = iter(texts)
    counter = _Counter(lambda page: next(it))
    monkeypatch.setattr(pdf_ingestor, "_extract_page_text", counter)
    return counter


def test_text_exactly_at_the_char_cap_is_ingested(tmp_path, monkeypatch, scrub_calls):
    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_CHARS", 50)
    _fake_page_texts(monkeypatch, ["a" * 25, "b" * 25])
    artifact = ingest_pdf(_make_pdf(tmp_path / "ok.pdf", 2))
    assert artifact["metadata"]["page_count"] == 2
    assert artifact["sections"][0]["content"] == "a" * 25 + pdf_ingestor.PAGE_JOIN + "b" * 25


def test_text_over_the_char_cap_stops_extraction_at_the_page_that_crosses_it(tmp_path, monkeypatch, scrub_calls):
    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_CHARS", 50)
    extract = _fake_page_texts(monkeypatch, ["a" * 30] * 5)
    path = _make_pdf(tmp_path / "wordy.pdf", 5)
    with pytest.raises(ValueError) as exc:
        ingest_pdf(path)
    assert str(exc.value) == "This PDF has too much text; the limit is 50 characters."
    assert extract.calls == 2  # page 2 pushed the total to 60; pages 3-5 were never read
    assert scrub_calls.calls == 2  # the title, and page 1; the page that crossed the cap is not scrubbed


def test_the_char_limit_is_shown_with_thousands_separators(tmp_path, monkeypatch, scrub_calls):
    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_CHARS", 1000)
    _fake_page_texts(monkeypatch, ["x" * 1001])
    with pytest.raises(ValueError, match=r"the limit is 1,000 characters"):
        ingest_pdf(_make_pdf(tmp_path / "w.pdf", 1))


# ---- what the user sees -----------------------------------------------------------------------

def test_refusal_messages_do_not_leak_the_server_path(tmp_path, monkeypatch, scrub_calls):
    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_PAGES", 1)
    path = _make_pdf(tmp_path / "secret-name.pdf", 2)
    with pytest.raises(ValueError) as exc:
        ingest_pdf(path)
    assert str(tmp_path) not in str(exc.value)
    assert "secret-name" not in str(exc.value)


def test_a_missing_file_is_still_file_not_found(tmp_path, scrub_calls):
    with pytest.raises(FileNotFoundError):
        ingest_pdf(str(tmp_path / "nope.pdf"))


# ---- through the real route (no fake ingestor) ------------------------------------------------

def test_the_pdf_route_answers_400_with_the_message_for_an_oversize_pdf(tmp_path, monkeypatch, scrub_calls):
    """/api/notes/pdf -> process_upload -> ingest_pdf, all real: the refusal is raised before
    anything is written to the knowledge graph, and the route turns the ValueError into a 400."""
    import tempfile

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    import api.routes.notebooks as nb
    from api.deps import require_auth

    monkeypatch.setattr(pdf_ingestor, "PDF_MAX_PAGES", 2)
    work = tmp_path / "tmp"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(work))  # where the route's temp file goes
    pdf_path = _make_pdf(tmp_path / "three.pdf", 3)

    app = FastAPI()
    app.include_router(nb.router)
    app.dependency_overrides[require_auth] = lambda: "test-owner-id"
    with open(pdf_path, "rb") as fh:
        resp = TestClient(app).post(
            "/api/notes/pdf",
            data={"workspace_id": "ws-1"},
            files={"file": ("three.pdf", fh.read(), "application/pdf")},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "This PDF has 3 pages; the limit is 2 pages."
    assert list(work.iterdir()) == []  # the route's temp copy was removed
