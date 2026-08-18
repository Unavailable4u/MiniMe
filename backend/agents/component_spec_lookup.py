"""
agents/component_spec_lookup.py — Real part dimension/datasheet lookup.

Part 1 (DigiKey only): same shape as agents/academic_search.py /
agents/part_price_finder.py -- plain HTTP, no LLM call. get_real_spec()
does DigiKey's OAuth2 client-credentials flow (token cached in memory,
refreshed on expiry) and calls the Product Information V4 API for the
exact part number passed in, parsing out dimensions_mm: {w, h, d} and
datasheet_url from the response.

Part 2 (Mouser fallback): adds _lookup_mouser(), tried only when
DigiKey returns None (not found, or DIGIKEY_CLIENT_ID/SECRET unset).
Mouser's Search API v1 is free and needs only a single API key (no
OAuth handshake, unlike DigiKey) -- signup is Log in/create a My
Mouser Account -> fill out the online Search API Request Form -> they
email the key (see MOUSER_API_KEY in .env.example). It's JSON-native,
up to 50 results/call, 30 calls/min, 1000 calls/day. The "Search by
Part Number" method (SearchByPartRequest) is used here since
get_real_spec() is always called with an exact part number already,
same as the DigiKey side. Response shape differs from DigiKey's
(ProductAttributes/AttributeName/AttributeValue vs.
Parameters/ParameterText/ValueText, DataSheetUrl vs DatasheetUrl) but
_parse_dimensions_mm below is shared -- only the two small "pull a
size_text out of this vendor's shape" helpers differ.

Part 3 (caching): get_real_spec() checks eo/spec_cache.py first (a
long-TTL cache, since physical dimensions don't move the way
part_price_finder.py's prices do) and writes to it after a real
DigiKey/Mouser hit, so repeated project generations that reuse common
parts don't re-spend API quota on the same part number every time.

Same "skip cleanly if key_env not set" pattern as the rest of the
codebase (see relay/emitter.py's _get_client() docstring): if
DIGIKEY_CLIENT_ID/DIGIKEY_CLIENT_SECRET or MOUSER_API_KEY aren't set,
the corresponding lookup returns None rather than raising.
"""
import os
import sys
import time
import unicodedata

import requests
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.spec_cache import get_cached_spec, set_cached_spec
from eo.datasheet_cache import get_cached_datasheet, set_cached_datasheet

# Permanent fix (2026-08-13): load_dotenv() at MODULE level, not just
# inside __main__. The __main__-only version only covered
# `python agents/component_spec_lookup.py` -- it silently did nothing
# for `python -c "from agents.component_spec_lookup import ..."` or
# any other import path, since those never execute the __main__ block.
# That's exactly what bit MOUSER_API_KEY: it was correctly set in
# .env, but a one-liner import saw it as unset because nothing had
# loaded .env into the process yet on that path.
#
# Safe to call unconditionally here too: python-dotenv's load_dotenv()
# defaults to override=False, so if api/server.py already loaded
# backend/.env earlier in a full app run, this is a harmless no-op --
# same reasoning agents/report_writer.py and agents/fixer_pool.py
# already rely on for their own module-level load_dotenv() calls.
load_dotenv()

REQUEST_TIMEOUT = 15

# F3 Part 5: a full datasheet PDF download is bigger and slower than
# any single DigiKey/Mouser JSON round-trip above, so it gets its own,
# longer timeout rather than sharing REQUEST_TIMEOUT.
DATASHEET_REQUEST_TIMEOUT = 30

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
# Permanent fix (real-bug follow-up, 2026-08-13): the old
# `/products/v4/search/{part_number}/productdetails` endpoint is a
# single-SKU EXACT resolver -- it only succeeds when part_number maps
# to exactly one DigiKey catalog product. Every part_number this
# module is actually called with (see hardware_speccer.py's prompt:
# "ESP32-WROOM-32", "DS18B20", etc.) is a manufacturer/family
# designator that legitimately matches several catalog variants, so
# productdetails 404s on essentially all real input -- confirmed live
# (ESP32-WROOM-32, XL6009, DHT22, SSD1306 all 404'd in the same run).
# Switched to the Keyword Search endpoint, which is DigiKey's
# many-candidates search (same shape Product Information V4 exposes
# for keyword queries) instead of an exact-match lookup -- see
# _lookup_digikey() below for how candidates are scanned, same
# "prefer the first hit with real dimensions, else first with a
# datasheet" pattern _lookup_mouser() already used.
DIGIKEY_KEYWORD_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

# Part 2 -- Mouser Search API v1. No OAuth: a single API key issued via
# the Search API Request Form (see module docstring), passed as a query
# param on every call rather than a header.
MOUSER_SEARCH_PART_NUMBER_URL = "https://api.mouser.com/api/v1/search/partnumber"

# DigiKey locale headers -- required on every Product Information V4
# call, not just the token request. Fixed to en/US/USD for now; revisit
# if this ever needs to serve non-US pricing/availability.
DIGIKEY_LOCALE_HEADERS = {
    "X-DIGIKEY-Locale-Site": "US",
    "X-DIGIKEY-Locale-Language": "en",
    "X-DIGIKEY-Locale-Currency": "USD",
}

# In-memory token cache. Module-level singleton, same "lazy, sticky"
# shape as relay/emitter.py's _get_client() -- one token shared across
# every get_real_spec() call in this process rather than fetching a
# fresh one per part. DigiKey client-credentials tokens are
# short-lived (typically ~10 min / 599s), so this is refreshed on
# expiry, not cached permanently like eo/spec_cache.py (Part 3) will
# cache the actual dimension data.
_digikey_token = None
_digikey_token_expires_at = 0.0  # epoch seconds


def _get_digikey_token() -> str | None:
    """Returns a live DigiKey access token, fetching/refreshing it via
    the client-credentials grant as needed. Returns None (without
    raising) if DIGIKEY_CLIENT_ID/DIGIKEY_CLIENT_SECRET aren't set, or
    if the token request itself fails -- callers treat None the same
    as "no spec found" and move on.
    """
    global _digikey_token, _digikey_token_expires_at

    client_id = os.getenv("DIGIKEY_CLIENT_ID")
    client_secret = os.getenv("DIGIKEY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    # 30s safety margin so we don't hand out a token that expires
    # mid-request.
    if _digikey_token and time.time() < (_digikey_token_expires_at - 30):
        return _digikey_token

    try:
        resp = requests.post(
            DIGIKEY_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [component_spec_lookup] DigiKey token request failed: {e}")
        return None

    token = payload.get("access_token")
    if not token:
        print("  [component_spec_lookup] DigiKey token response had no access_token")
        return None

    _digikey_token = token
    _digikey_token_expires_at = time.time() + payload.get("expires_in", 599)
    return _digikey_token


def _parse_dimensions_mm_from_text(size_text: str | None) -> dict | None:
    """Shared parser (Part 2): both DigiKey and Mouser expose size as a
    free-text attribute value (e.g. '10.00mm x 10.00mm',
    '0.394" L x 0.394" W') rather than a dedicated numeric w/h/d field
    -- only the surrounding vendor shape differs (see
    _digikey_size_text()/_mouser_size_text() below), so this text-level
    parse is factored out and shared instead of duplicated per vendor.
    Best-effort: pull the first two (or three) numeric mm-ish tokens out
    of the string. Returns None if it can't find/parse one, rather than
    guessing.
    """
    import re

    if not size_text:
        return None

    # Grab numeric tokens; convert inch tokens (marked with ") to mm.
    numbers = re.findall(r'(\d+(?:\.\d+)?)\s*(mm|")', size_text)
    if len(numbers) < 2:
        return None

    def _to_mm(value: str, unit: str) -> float:
        v = float(value)
        return round(v * 25.4, 3) if unit == '"' else round(v, 3)

    values_mm = [_to_mm(v, u) for v, u in numbers[:3]]
    dims = {"w": values_mm[0], "h": values_mm[1]}
    if len(values_mm) >= 3:
        dims["d"] = values_mm[2]
    return dims


def _digikey_size_text(product: dict) -> str | None:
    """DigiKey's Product Information V4 exposes size on the Parameters
    list, keyed by a ParameterText like "Size / Dimension" or "Size".
    """
    params = product.get("Parameters") or []
    for p in params:
        label = (p.get("ParameterText") or "").lower()
        if "size" in label or "dimension" in label:
            value = p.get("ValueText")
            if value:
                return value
    return None


def _mouser_size_text(product: dict) -> str | None:
    """Mouser's Search API exposes the same kind of free-text size
    value DigiKey does, on ProductAttributes (AttributeName/
    AttributeValue) -- IF the category/part carries one at all.
    Confirmed via live testing: for many parts this list only has
    shipping/packaging metadata (Reel/Tube/Cut Tape, pack qty), no
    size entry -- so this commonly returns None even on a successful,
    fully-populated response. That's expected, not a bug; see the
    comment in _lookup_mouser() above.
    """
    attrs = product.get("ProductAttributes") or []
    for a in attrs:
        label = (a.get("AttributeName") or "").lower()
        if "size" in label or "dimension" in label:
            value = a.get("AttributeValue")
            if value:
                return value
    return None


def _lookup_digikey(part_number: str) -> dict | None:
    """Keyword Search (not the old single-SKU productdetails exact
    lookup -- see DIGIKEY_KEYWORD_SEARCH_URL's comment for why that
    was the actual bug). Returns a ranked list of candidate products
    for a free-text/part-number query, so a manufacturer/family
    designator that matches several DigiKey catalog variants (the
    normal case for everything this module is actually called with)
    gets real candidates instead of an automatic 404.

    Same "don't trust the first candidate blindly" scan _lookup_mouser()
    already uses below: prefer the first candidate that yields parsed
    dimensions_mm, else fall back to the first with at least a
    datasheet_url.
    """
    token = _get_digikey_token()
    client_id = os.getenv("DIGIKEY_CLIENT_ID")
    if not token or not client_id:
        return None

    try:
        resp = requests.post(
            DIGIKEY_KEYWORD_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                **DIGIKEY_LOCALE_HEADERS,
            },
            json={
                "Keywords": part_number,
                "Limit": 10,
                "Offset": 0,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            # A genuine "nothing on DigiKey matches this at all" --
            # still a plausible, expected outcome for an obscure or
            # misspelled part, kept distinguishable from creds-unset/
            # network-fine-nothing-there the same way the old code did.
            print(f"  [component_spec_lookup] DigiKey: no matches for "
                  f"'{part_number}' (404)")
            return None
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [component_spec_lookup] DigiKey product lookup failed for "
              f"'{part_number}': {e}")
        return None

    products = payload.get("Products") or []
    if not products:
        print(f"  [component_spec_lookup] DigiKey: no matches for "
              f"'{part_number}' (empty result set)")
        return None

    first_with_datasheet = None
    for product in products:
        dimensions_mm = _parse_dimensions_mm_from_text(_digikey_size_text(product))
        datasheet_url = product.get("DatasheetUrl")

        if dimensions_mm:
            return {
                "dimensions_mm": dimensions_mm,
                "datasheet_url": datasheet_url,
                "source": "digikey",
                # Real distributor data -- same "verified" vocabulary
                # G1a's curated table uses (Master Guide, G1 section).
                # Only set alongside an actual resolved dimensions_mm;
                # a datasheet-only hit below hasn't verified a size.
                "confidence": "verified",
            }
        if first_with_datasheet is None and datasheet_url:
            first_with_datasheet = {
                "dimensions_mm": None,
                "datasheet_url": datasheet_url,
                "source": "digikey",
                "confidence": None,
            }

    return first_with_datasheet


def _lookup_mouser(part_number: str) -> dict | None:
    """Part 2. Mouser Search API v1, "search by part number" method --
    no OAuth, just an API key on the query string. Tried only when
    DigiKey (above) returns None.

    Permanent fix (real-bug follow-up, 2026-08-13): this used to send
    partSearchOptions: "Exact" unconditionally. That's the same
    exact-match assumption that was breaking DigiKey -- for a
    manufacturer/family designator (the normal input here, e.g.
    "ESP32-WROOM-32"), an exact-only search commonly returns zero
    results even though Mouser carries several matching products,
    which is exactly why this fallback wasn't rescuing any of
    DigiKey's misses in practice. Now tries Exact first (fast path,
    keeps existing exact-catalog-number callers unchanged), and only
    if that comes back empty, retries once with a plain keyword search
    (no partSearchOptions) so family-style queries actually get
    candidates instead of silently falling through to LLM estimation.
    """
    api_key = os.getenv("MOUSER_API_KEY")
    if not api_key:
        return None

    result = _lookup_mouser_with_options(part_number, api_key, exact=True)
    if result is not None:
        return result
    return _lookup_mouser_with_options(part_number, api_key, exact=False)


def _lookup_mouser_with_options(part_number: str, api_key: str, exact: bool) -> dict | None:
    request_body = {
        "SearchByPartRequest": {
            "mouserPartNumber": part_number,
        }
    }
    if exact:
        request_body["SearchByPartRequest"]["partSearchOptions"] = "Exact"

    try:
        resp = requests.post(
            MOUSER_SEARCH_PART_NUMBER_URL,
            params={"apiKey": api_key},
            json=request_body,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [component_spec_lookup] Mouser product lookup failed for "
              f"'{part_number}' (exact={exact}): {e}")
        return None

    # Mouser returns 200 with an "Errors" array on the payload itself
    # (e.g. bad/expired key, rate limit) rather than an HTTP error
    # status for most failure modes -- check that before looking for
    # SearchResults.
    errors = payload.get("Errors") or []
    if errors:
        print(f"  [component_spec_lookup] Mouser API error for "
              f"'{part_number}': {errors}")
        return None

    parts = (payload.get("SearchResults") or {}).get("Parts") or []
    if not parts:
        return None

    # Confirmed via live debug dump (2026-08-13, part "NA555"): Mouser's
    # ProductAttributes on this endpoint is populated inconsistently
    # per category, and for many parts (this 555 timer among them) it
    # only carries shipping/packaging metadata -- "Packaging":
    # "Reel"/"Tube"/"Cut Tape", "Standard Pack Qty" -- never a
    # size/dimension entry, even though a real DataSheetUrl is present.
    # This lines up with Mouser's own Search API docs page, which lists
    # "Packaging" and "Data Sheet URL" among the available fields but
    # never "Dimensions" -- so a Mouser hit with datasheet_url set and
    # dimensions_mm staying None is the expected, common case here, not
    # a parsing bug. (Whether other part categories, e.g. connectors or
    # mechanical hardware, ever DO carry a size-shaped attribute is
    # untested -- _mouser_size_text()/_parse_dimensions_mm_from_text()
    # are kept in place for that case rather than removed.)
    #
    # Also confirmed live: a bare part number can match several
    # manufacturers (7 results for "NA555" here -- Diodes Incorporated
    # plus six Texas Instruments package variants), and the first match
    # isn't guaranteed to be the best one -- Diodes' entry even carried
    # a "RestrictionMessage" (not sold in this region) despite still
    # having a usable datasheet. Rather than trust parts[0] blindly,
    # scan every candidate: prefer the first one that actually yields
    # parsed dimensions_mm, and only fall back to the first with at
    # least a datasheet if none do.
    first_with_datasheet = None
    for product in parts:
        dimensions_mm = _parse_dimensions_mm_from_text(_mouser_size_text(product))
        datasheet_url = product.get("DataSheetUrl")

        if dimensions_mm:
            return {
                "dimensions_mm": dimensions_mm,
                "datasheet_url": datasheet_url,
                "source": "mouser",
                # Same "verified" vocabulary as the DigiKey branch above
                # and G1a's curated table -- see that branch's comment.
                "confidence": "verified",
            }
        if first_with_datasheet is None and datasheet_url:
            first_with_datasheet = {
                "dimensions_mm": None,
                "datasheet_url": datasheet_url,
                "source": "mouser",
                "confidence": None,
            }

    return first_with_datasheet


def _normalize_part_number(s: str) -> str:
    """Bug 6 fix: fold Unicode dash variants an LLM-generated part
    number can contain (most commonly U+2011 NON-BREAKING HYPHEN, also
    U+2010 HYPHEN and U+2013 EN DASH) down to plain ASCII '-' before the
    part number ever reaches DigiKey/Mouser. Neither vendor's search
    matches across dash variants, so an unnormalized part number (e.g.
    "R9\u2011MM") silently 404s exactly like a real not-found part,
    with nothing in the response distinguishing the two cases.

    NFKC normalization first so other compatibility-equivalent forms
    (fullwidth digits/letters, etc.) are collapsed the same way, then
    an explicit dash-variant fold since NFKC alone does not map
    U+2011/U+2010/U+2013 to ASCII '-', then a final strip() for
    incidental leading/trailing whitespace.
    """
    if not s:
        return s

    normalized = unicodedata.normalize("NFKC", s)
    for dash in ("\u2011", "\u2013", "\u2010"):
        normalized = normalized.replace(dash, "-")
    return normalized.strip()


def get_real_spec(part_number: str) -> dict | None:
    """Returns {"dimensions_mm": {"w","h","d"}, "datasheet_url", "source",
    "confidence"} for the given exact part_number, or None if neither
    vendor has it (creds unset, part not found, or response has
    neither a parseable size nor a datasheet link on either side).

    Tries DigiKey first, only falls back to Mouser if that returns
    None -- the return shape (dimensions_mm/datasheet_url/source) is
    standardized across both, "source" being the only field that
    tells a caller which vendor actually answered. This None is the
    signal hardware_speccer.py (Part 4) uses to fall back to LLM
    estimation for that part.

    "confidence" is "verified" whenever dimensions_mm was actually
    resolved (a real distributor hit, same vocabulary G1a's curated
    table uses -- see the Master Guide's G1 section: "A DigiKey/Mouser
    hit is real distributor data, so it's tagged confidence: verified
    too"), or None for a datasheet-only hit that never resolved a
    size. hardware_speccer.py's _populate_dimensions() merges this
    onto the part as "dimension_confidence", same field name G1a's
    curated-table merge already uses, so downstream code (a future
    G3/G4 confidence-aware mech_validator) reads one consistent field
    regardless of which of the two sub-steps resolved it.

    Part 3: checks eo/spec_cache.py first (180-day TTL -- physical
    dimensions don't move the way price_cache.py's 5-day-cached prices
    do), and writes to it after a real DigiKey/Mouser hit, so repeated
    project generations that reuse common parts (ESP32, common
    sensors, etc.) don't re-spend DigiKey/Mouser quota on the same
    part number every time. A miss (part not found on either vendor)
    is NOT cached -- see spec_cache.py's own docstring for why.
    """
    if not part_number:
        return None

    part_number = _normalize_part_number(part_number)

    cached = get_cached_spec(part_number)
    if cached is not None:
        # Strip _cached_at (spec_cache.py's own bookkeeping field)
        # before returning -- callers, notably hardware_speccer.py's
        # Part 4 _populate_dimensions(), merge this return value's
        # keys directly onto a part dict and only expect
        # dimensions_mm/datasheet_url/source, not cache internals.
        return {
            "dimensions_mm": cached.get("dimensions_mm"),
            "datasheet_url": cached.get("datasheet_url"),
            "source": cached.get("source"),
            "confidence": cached.get("confidence"),
        }

    result = _lookup_digikey(part_number)
    if result is None:
        result = _lookup_mouser(part_number)
    if result is not None:
        set_cached_spec(part_number, result)
    return result


def get_datasheet_detail(datasheet_url: str) -> dict | None:
    """F3 Part 5 (optional stretch): downloads the PDF at datasheet_url
    and runs it through agents/pdf_ingestor.py's existing ingest_pdf()
    pipeline -- the same deterministic, no-LLM-call PDF parser already
    used elsewhere -- to pull finer detail (mounting-hole positions,
    pinout tables, anything else in the datasheet's running text)
    beyond the top-level dimensions_mm get_real_spec() already exposes.
    Callers store the result keyed to a part id (see
    hardware_speccer.py's Part 5 wiring) for a later mech-primitive step
    (G3) to read, if that gets built -- this function itself only
    fetches and parses, it doesn't interpret the content.

    Deliberately the most expensive per-part call in this module (a
    full PDF download + parse, vs. one JSON API round-trip for
    get_real_spec()) and the least likely to block anything downstream
    if skipped -- a None return here means "no deep-dive available for
    this part," never a reason to fail the part itself.

    Checks eo/datasheet_cache.py first (180-day TTL, see that module's
    own docstring for why) and writes to it after a real download+parse
    succeeds, so the same datasheet_url met again across projects (a
    common ESP32/DS18B20/etc. datasheet gets pulled once, not once per
    project generation) doesn't re-download/re-parse for free.

    Returns {"title", "content", "page_count"} on success -- "content"
    is ingest_pdf()'s single joined section (see that module's
    docstring for why a PDF always comes back as exactly one section),
    the full extracted text a later step could search/parse further.
    Returns None (never raises) for: no datasheet_url, a download that
    fails or times out, a response that doesn't look like a PDF
    (Content-Type and file extension both checked, since some vendors
    put an HTML product page behind a "datasheet" link), or a parse
    failure inside ingest_pdf() itself -- same "skip cleanly" pattern
    as the rest of this module.
    """
    if not datasheet_url:
        return None

    cached = get_cached_datasheet(datasheet_url)
    if cached is not None:
        return {
            "title": cached.get("title"),
            "content": cached.get("content"),
            "page_count": cached.get("page_count"),
        }

    import tempfile
    from agents.pdf_ingestor import ingest_pdf

    tmp_path = None
    try:
        resp = requests.get(datasheet_url, timeout=DATASHEET_REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "pdf" not in content_type and not datasheet_url.lower().endswith(".pdf"):
            # Not actually a PDF (e.g. an HTML product page some vendors
            # return under a "datasheet" link) -- ingest_pdf() would just
            # raise on this, so skip the download/parse attempt entirely.
            print(f"  [component_spec_lookup] datasheet_url doesn't look "
                  f"like a PDF (Content-Type={content_type!r}): {datasheet_url}")
            return None

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            for chunk in resp.iter_content(chunk_size=65536):
                tmp.write(chunk)
            tmp_path = tmp.name

        artifact = ingest_pdf(tmp_path)
    except Exception as e:
        print(f"  [component_spec_lookup] datasheet deep-dive failed for "
              f"'{datasheet_url}': {e}")
        return None
    finally:
        # Always clean up the downloaded temp file, success or failure --
        # this module has no standing reason to keep a copy of the raw
        # PDF around once ingest_pdf() has extracted its text.
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    result = {
        "title": artifact.get("title"),
        "content": (artifact.get("sections") or [{}])[0].get("content", ""),
        "page_count": (artifact.get("metadata") or {}).get("page_count"),
    }
    set_cached_datasheet(datasheet_url, result)
    return result


if __name__ == "__main__":
    # Manual smoke test — same "prove one real lookup works standalone"
    # step as part_price_finder.py's own __main__ block.
    #
    # load_dotenv() is no longer called here -- it now runs once at
    # module level (see top of file) so it's covered on every import
    # path, not just this direct-script-run one.
    import json

    # NA555 (TI's manufacturer part number) instead of a DigiKey
    # catalog SKU like "296-6501-1-ND": Part 4's real callers only ever
    # have a manufacturer part number to search with (the LLM fills in
    # things like "ESP32-WROOM-32", "DS18B20"), and a DigiKey-specific
    # "-ND" catalog number isn't something Mouser (or any other vendor)
    # would ever recognize -- testing both vendors against the same
    # real-world input they'll actually get is the point of this
    # smoke test.
    TEST_PART = "NA555"

    result = get_real_spec(TEST_PART)
    print(json.dumps(result, indent=2))

    # Part 5 smoke test: if the lookup above turned up a datasheet_url,
    # prove the deep-dive works standalone too -- same "one real call,
    # not just unit-tested plumbing" spirit as the DigiKey/Mouser tests
    # above.
    if result and result.get("datasheet_url"):
        print("--- datasheet deep-dive ---")
        detail = get_datasheet_detail(result["datasheet_url"])
        if detail:
            print(f"  title: {detail.get('title')!r}")
            print(f"  page_count: {detail.get('page_count')}")
            print(f"  content (first 300 chars): {(detail.get('content') or '')[:300]!r}")
        else:
            print("  no detail (download/parse failed or wasn't a PDF)")

    # Part 3 smoke test: call get_real_spec() again for the SAME part.
    # If caching is wired correctly this returns instantly from
    # eo/spec_cache.py rather than re-hitting DigiKey/Mouser -- no
    # "[component_spec_lookup] DigiKey: no exact match..." or Mouser
    # network calls should print between this line and the previous
    # result, unlike the first call.
    print("--- second call (should be cache-served, no network calls above this line) ---")
    cached_result = get_real_spec(TEST_PART)
    print(json.dumps(cached_result, indent=2))

    if os.getenv("MOUSER_API_KEY"):
        # Part 2 smoke test: force the Mouser path directly (bypassing
        # DigiKey) so it can be verified standalone even when
        # DIGIKEY_CLIENT_ID/SECRET are also set above.
        mouser_result = _lookup_mouser(TEST_PART)
        print(json.dumps(mouser_result, indent=2))

    if os.getenv("DEBUG_DIGIKEY_PARAMS"):
        # Temporary debug aid: dump every Parameters entry DigiKey
        # actually returned for this part, so _digikey_size_text can
        # be pointed at whatever label the API really uses (e.g.
        # "Package / Case") instead of guessing.
        #
        # Permanent fix (real-bug follow-up, 2026-08-13): this still
        # called the old single-SKU productdetails endpoint via the
        # since-removed DIGIKEY_PRODUCT_DETAILS_URL constant (undefined
        # name -- Ruff F821 / Pylance reportUndefinedVariable), left
        # over from before _lookup_digikey() switched to
        # DIGIKEY_KEYWORD_SEARCH_URL. Updated to issue the same POST
        # keyword-search call the real lookup path uses, and to read
        # Parameters off the first entry in the returned "Products"
        # list rather than a single top-level "Product" dict, since
        # keyword search returns candidates plural, not one exact hit.
        token = _get_digikey_token()
        client_id = os.getenv("DIGIKEY_CLIENT_ID")
        resp = requests.post(
            DIGIKEY_KEYWORD_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                **DIGIKEY_LOCALE_HEADERS,
            },
            json={
                "Keywords": TEST_PART,
                "Limit": 10,
                "Offset": 0,
            },
            timeout=REQUEST_TIMEOUT,
        )
        products = resp.json().get("Products") or []
        if not products:
            print(f"  [DEBUG_DIGIKEY_PARAMS] no candidates for {TEST_PART!r}")
        else:
            product = products[0]
            print(f"  [DEBUG_DIGIKEY_PARAMS] showing Parameters for first of "
                  f"{len(products)} candidate(s): "
                  f"{product.get('ManufacturerProductNumber')!r}")
            for p in product.get("Parameters", []):
                print(f"  {p.get('ParameterText')!r}: {p.get('ValueText')!r}")