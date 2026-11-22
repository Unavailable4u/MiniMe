"""
agents/web_clipper.py — Part 4 §4.2. Deterministic, no-LLM-call web
page ingestion into the same common artifact shape agents/pdf_ingestor.py
and agents/importer.py already produce ({title, sections, metadata}).
Uses trafilatura for boilerplate stripping (nav/ads/footers), rather
than raw BeautifulSoup, per the notes doc's own reasoning.

Fetching is the one real-world-flaky part of this module (a dead link,
a paywall, a site that blocks non-browser requests) — every failure
mode collapses to a single ValueError so the caller (the /api/notes/clip
endpoint) has one exception type to translate into a 400, rather than
needing to know the fetcher's internals.

Patch SX-06: the page is downloaded with utils.url_guard.safe_get, not
with trafilatura's own downloader. The URL is user input, so the fetch
must refuse private / loopback / cloud-metadata addresses (including
after a redirect), verify certificates and cap the size, and that
downloader does none of that. trafilatura is only used to extract text.

Place this file at: agents/web_clipper.py
"""

import json

import trafilatura

from utils.url_guard import UnsafeURL, safe_get

# Largest page a clip will download. PLACEHOLDER until the size limits are
# decided (decision D-3 in the SX patch guide).
CLIP_MAX_BYTES = 5_000_000
CLIP_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


def clip_url(url: str) -> dict:
    """Fetches and extracts `url` into the common artifact shape. Always
    a single section — a web page has no reliable multi-heading
    structure the way a PDF has pages, so unlike pdf_ingestor.py there's
    nothing principled to split on here.
    """
    try:
        _final_url, _status, _headers, downloaded = safe_get(
            url, max_bytes=CLIP_MAX_BYTES, allowed_content_types=CLIP_CONTENT_TYPES,
        )
    except UnsafeURL as exc:
        # The URL was refused. Do not repeat it back: it may hold control
        # characters or an internal name, and the route sends this message
        # to the client as-is. The guard's own text is fixed and safe.
        raise ValueError(f"could not fetch: {exc}") from exc
    except ValueError as exc:
        # FetchError, ResponseTooLarge and FetchTimeout: all ValueErrors with
        # fixed text that never names a resolved address.
        raise ValueError(f"could not fetch {url}: {exc}") from exc
    if not downloaded:
        raise ValueError(f"could not fetch {url}")

    extracted = trafilatura.extract(
        downloaded, output_format="json", with_metadata=True,
        include_comments=False, include_tables=True,
    )
    if not extracted:
        raise ValueError(f"no extractable content at {url}")

    data = json.loads(extracted)
    content = (data.get("text") or "").strip()
    if not content:
        raise ValueError(f"no extractable content at {url}")

    return {
        "title": data.get("title") or url,
        "sections": [{"heading": "", "content": content, "node_refs": []}],
        "metadata": {
            "source_format": "web",
            "source_url": url,
            "author": data.get("author"),
            "date": data.get("date"),
        },
    }


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        artifact = clip_url(u)
        print(f"--- {u} ---")
        print(json.dumps(artifact, indent=2)[:500])