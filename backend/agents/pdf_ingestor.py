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

Whole-document-as-one-section (Notebooks integration guide §3): a PDF
used to come back as one section PER PAGE, which meant
source_ingestor.write_ingested_source() (its len(sections) > 1 branch)
wrote one graph node per page and chained them with same_source edges —
"the file I uploaded" fragmented into N nodes nobody asked for. Page
boundaries are still real and still worth keeping (search/embedding
quality, and anything downstream that wants to jump to a specific page),
so they're preserved as `metadata.page_breaks` — character offsets into
the single joined `content` string — rather than as separate sections.
source_ingestor's existing len(sections) <= 1 branch (already there,
already writes exactly one node) now does the right thing for PDFs with
no changes needed on that side at all.

Place this file at: agents/pdf_ingestor.py
"""

import os

import pdfplumber


SUPPORTED_EXTENSIONS = (".pdf",)
PAGE_JOIN = "\n\n"


def _guess_title(pdf, path: str) -> str:
    """PDF metadata title, if present and non-empty, wins. Otherwise the
    first non-empty line of page 1 (most PDFs put a title-sized line
    first even without setting the metadata field). Falls back to the
    filename if the document has no extractable text at all (e.g. a
    scanned, non-OCR'd PDF — see _extract_page_text's own note on that
    case).
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


def _extract_page_text(page) -> str:
    """Raw text for one page. No layout-aware heading detection —
    pdfplumber gives text, not style information, so unlike docx there's
    no reliable signal to split a page into sub-sections. A page with no
    extractable text (a scanned image with no OCR layer) comes back as
    an empty string rather than being skipped, so it still occupies its
    real position in `page_breaks` instead of silently shifting every
    later page's offset.
    """
    return (page.extract_text() or "").strip()


def _join_pages(page_texts: list[str]) -> tuple[str, list[dict]]:
    """Joins every page's text into one document string, recording where
    each page starts (`start_offset`) and how long it ran (`char_count`)
    so a caller that wants page-level granularity later (jump-to-page,
    finer citations, a future re-introduction of splitting) can recover
    it from `content` + `page_breaks` without this module needing to
    hand back separate sections again.
    """
    content_parts = []
    page_breaks = []
    offset = 0
    for i, text in enumerate(page_texts):
        page_breaks.append({"page": i + 1, "start_offset": offset, "char_count": len(text)})
        content_parts.append(text)
        offset += len(text) + len(PAGE_JOIN)
    return PAGE_JOIN.join(content_parts), page_breaks


def ingest_pdf(path: str) -> dict:
    """Parses a PDF at `path` into the common artifact shape. Raises
    FileNotFoundError if the path doesn't exist, matching
    agents/importer.py's import_artifact() contract so callers can
    handle both the same way.

    Always returns exactly one section (the whole document) — see the
    module docstring for why sections no longer track pages.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with pdfplumber.open(path) as pdf:
        title = _guess_title(pdf, path)
        page_texts = [_extract_page_text(page) for page in pdf.pages]

    content, page_breaks = _join_pages(page_texts)

    return {
        "title": title,
        "sections": [{"heading": title, "content": content, "node_refs": []}],
        "metadata": {
            "source_format": "pdf",
            "source_path": path,
            "page_count": len(page_texts),
            "page_breaks": page_breaks,
        },
    }


if __name__ == "__main__":
    import sys
    import json
    for p in sys.argv[1:]:
        artifact = ingest_pdf(p)
        print(f"--- {p} ---")
        print(json.dumps(artifact, indent=2)[:500])