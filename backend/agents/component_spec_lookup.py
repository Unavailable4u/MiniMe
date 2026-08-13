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

import requests
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.spec_cache import get_cached_spec, set_cached_spec

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

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_PRODUCT_DETAILS_URL = "https://api.digikey.com/products/v4/search/{part_number}/productdetails"

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
    token = _get_digikey_token()
    client_id = os.getenv("DIGIKEY_CLIENT_ID")
    if not token or not client_id:
        return None

    try:
        resp = requests.get(
            DIGIKEY_PRODUCT_DETAILS_URL.format(part_number=part_number),
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                **DIGIKEY_LOCALE_HEADERS,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            # Was previously a silent `return None` -- same shape as
            # every other found/not-found path in this file (see
            # Mouser's "Errors" print, or its own "no parts found"
            # fallthrough) except this one, which left a genuine miss
            # indistinguishable from "creds unset" or "network fine,
            # just nothing there" with zero output. DigiKey's
            # productdetails endpoint expects an exact, unambiguous
            # catalog lookup (a specific DigiKey SKU like
            # "296-6501-1-ND", or a manufacturer part number DigiKey
            # can resolve to exactly one product) -- a manufacturer
            # part number that maps to several vendors' variants (e.g.
            # "NA555": Diodes Inc. + 6 different TI package options,
            # confirmed via the Mouser side of this same lookup) is a
            # plausible, expected 404 here, not a bug -- but it should
            # say so rather than look identical to every other reason
            # for returning None.
            print(f"  [component_spec_lookup] DigiKey: no exact match for "
                  f"'{part_number}' (404)")
            return None
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [component_spec_lookup] DigiKey product lookup failed for "
              f"'{part_number}': {e}")
        return None

    product = payload.get("Product")
    if not product:
        return None

    dimensions_mm = _parse_dimensions_mm_from_text(_digikey_size_text(product))
    datasheet_url = product.get("DatasheetUrl")

    if not dimensions_mm and not datasheet_url:
        return None

    return {
        "dimensions_mm": dimensions_mm,
        "datasheet_url": datasheet_url,
        "source": "digikey",
    }


def _lookup_mouser(part_number: str) -> dict | None:
    """Part 2. Mouser Search API v1, "search by part number" method --
    no OAuth, just an API key on the query string. Tried only when
    DigiKey (above) returns None.
    """
    api_key = os.getenv("MOUSER_API_KEY")
    if not api_key:
        return None

    try:
        resp = requests.post(
            MOUSER_SEARCH_PART_NUMBER_URL,
            params={"apiKey": api_key},
            json={
                "SearchByPartRequest": {
                    "mouserPartNumber": part_number,
                    "partSearchOptions": "Exact",
                }
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [component_spec_lookup] Mouser product lookup failed for "
              f"'{part_number}': {e}")
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
            }
        if first_with_datasheet is None and datasheet_url:
            first_with_datasheet = {
                "dimensions_mm": None,
                "datasheet_url": datasheet_url,
                "source": "mouser",
            }

    return first_with_datasheet


def get_real_spec(part_number: str) -> dict | None:
    """Returns {"dimensions_mm": {"w","h","d"}, "datasheet_url", "source"}
    for the given exact part_number, or None if neither vendor has it
    (creds unset, part not found, or response has neither a parseable
    size nor a datasheet link on either side).

    Tries DigiKey first, only falls back to Mouser if that returns
    None -- the return shape (dimensions_mm/datasheet_url/source) is
    standardized across both, "source" being the only field that
    tells a caller which vendor actually answered. This None is the
    signal hardware_speccer.py (Part 4) uses to fall back to LLM
    estimation for that part.

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
        }

    result = _lookup_digikey(part_number)
    if result is None:
        result = _lookup_mouser(part_number)
    if result is not None:
        set_cached_spec(part_number, result)
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
        token = _get_digikey_token()
        client_id = os.getenv("DIGIKEY_CLIENT_ID")
        resp = requests.get(
            DIGIKEY_PRODUCT_DETAILS_URL.format(part_number=TEST_PART),
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                **DIGIKEY_LOCALE_HEADERS,
            },
            timeout=REQUEST_TIMEOUT,
        )
        product = resp.json().get("Product", {})
        for p in product.get("Parameters", []):
            print(f"  {p.get('ParameterText')!r}: {p.get('ValueText')!r}")