"""
agents/pdf_ingestor.py — Part 4 §4.2. Deterministic, no-LLM-call PDF
parsing into the same common artifact shape agents/exporter.py and
agents/importer.py already use ({title, sections: [{heading, content,
node_refs}], metadata}). Not agents/importer.py's job on purpose — see
that module's own docstring: PDF is heavier (no reliable style/heading
metadata to key off, unlike docx/pptx) and belongs to Notes-domain
ingestion instead.

This module only parses. Turning the returned shape into real Part 0
knowledge-graph nodes is the separate shared "write ingested source as
node(s)" step every Capture ingestor feeds into (Part 4 §4.2) — not
duplicated per-ingestor here.

Place this file at: agents/pdf_ingestor.py
"""

import os

import pdfplumber


SUPPORTED_EXTENSIONS = (".pdf",)


def _guess_title(pdf, path: str) -> str:
    """PDF metadata title, if present and non-empty, wins. Otherwise the
    first non-empty line of page 1 (most PDFs put a title-sized line
    first even without setting the metadata field). Falls back to the
    filename if the document has no extractable text at all (e.g. a
    scanned, non-OCR'd PDF — see _read_page's own note on that case).
    """
    meta_title = (pdf.metadata or {}).get("Title")
    if meta_title and meta_title.strip():
        return meta_title.strip()
    if pdf.pages:
        first_page_text = pdf.pages[0].extract_text() or ""
        for line in first_page_text.split("\n"):
            line = line.strip()
            if line:
                return line
    return os.path.splitext(os.path.basename(path))[0]


def _read_page(page, page_number: int) -> dict:
    """One section per page. No layout-aware heading detection —
    pdfplumber gives text, not style information, so unlike docx there's
    no reliable signal to split a page into sub-sections. A page with no
    extractable text (a scanned image with no OCR layer) still becomes a
    real, present section rather than being silently dropped, so the
    caller can see exactly which pages came back empty instead of
    getting a shorter document than the source had.
    """
    text = (page.extract_text() or "").strip()
    return {"heading": f"Page {page_number}", "content": text, "node_refs": []}


def ingest_pdf(path: str) -> dict:
    """Parses a PDF at `path` into the common artifact shape. Raises
    FileNotFoundError if the path doesn't exist, matching
    agents/importer.py's import_artifact() contract so callers can
    handle both the same way.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with pdfplumber.open(path) as pdf:
        title = _guess_title(pdf, path)
        sections = [_read_page(page, i + 1) for i, page in enumerate(pdf.pages)]

    return {
        "title": title,
        "sections": sections,
        "metadata": {"source_format": "pdf", "source_path": path},
    }


if __name__ == "__main__":
    import sys
    import json
    for p in sys.argv[1:]:
        artifact = ingest_pdf(p)
        print(f"--- {p} ---")
        print(json.dumps(artifact, indent=2)[:500])