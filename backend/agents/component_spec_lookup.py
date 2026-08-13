"""
agents/component_spec_lookup.py — Real part dimension/datasheet lookup.

Part 1 (DigiKey only): same shape as agents/academic_search.py /
agents/part_price_finder.py -- plain HTTP, no LLM call. get_real_spec()
does DigiKey's OAuth2 client-credentials flow (token cached in memory,
refreshed on expiry) and calls the Product Information V4 API for the
exact part number passed in, parsing out dimensions_mm: {w, h, d} and
datasheet_url from the response.

No Mouser fallback yet (Part 2), no eo/spec_cache.py caching yet
(Part 3) -- this just proves one real DigiKey lookup works standalone.

Same "skip cleanly if key_env not set" pattern as the rest of the
codebase (see relay/emitter.py's _get_client() docstring): if
DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET aren't set, get_real_spec()
returns None rather than raising.
"""
import os
import sys
import time

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUEST_TIMEOUT = 15

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_PRODUCT_DETAILS_URL = "https://api.digikey.com/products/v4/search/{part_number}/productdetails"

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


def _parse_dimensions_mm(product: dict) -> dict | None:
    """DigiKey's Product Information V4 exposes size on the
    Parameters list as a free-text ValueText (e.g. '10.00mm x 10.00mm',
    '0.394" L x 0.394" W') keyed by a ParameterText like "Size /
    Dimension" or "Size" -- there's no dedicated numeric w/h/d field.
    Best-effort parse: pull the first two (or three) numeric mm-ish
    tokens out of that string. Returns None if no size-shaped
    parameter is present or it can't be parsed, rather than guessing.
    """
    import re

    params = product.get("Parameters") or []
    size_text = None
    for p in params:
        label = (p.get("ParameterText") or "").lower()
        if "size" in label or "dimension" in label:
            size_text = p.get("ValueText")
            if size_text:
                break
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

    dimensions_mm = _parse_dimensions_mm(product)
    datasheet_url = product.get("DatasheetUrl")

    if not dimensions_mm and not datasheet_url:
        return None

    return {
        "dimensions_mm": dimensions_mm,
        "datasheet_url": datasheet_url,
        "source": "digikey",
    }


def get_real_spec(part_number: str) -> dict | None:
    """Returns {"dimensions_mm": {"w","h","d"}, "datasheet_url", "source"}
    for the given exact part_number, or None if DigiKey creds aren't
    configured, the part isn't found, or the response has neither a
    parseable size nor a datasheet link.

    Part 1 scope: DigiKey only. Part 2 adds a Mouser fallback here
    (try DigiKey first, only fall back to Mouser if this returns None)
    and standardizes the return shape across both sources -- the shape
    above is written to already match what Part 2 will need.
    """
    if not part_number:
        return None
    return _lookup_digikey(part_number)


if __name__ == "__main__":
    # Manual smoke test — same "prove one real lookup works standalone"
    # step as part_price_finder.py's own __main__ block.
    #
    # load_dotenv() here (not at module level): when this module is
    # imported by the running app, api/server.py has already loaded
    # backend/.env into the process env by then. But run directly as a
    # script (`python agents/component_spec_lookup.py`), nothing else
    # has loaded it yet, so os.getenv() below would silently see
    # DIGIKEY_CLIENT_ID/SECRET as unset even with real values sitting in
    # .env -- same fix tests/manual/test_groq.py applies for the same
    # reason.
    from dotenv import load_dotenv
    load_dotenv()

    import json
    result = get_real_spec("296-6501-1-ND")
    print(json.dumps(result, indent=2))

    if os.getenv("DEBUG_DIGIKEY_PARAMS"):
        # Temporary debug aid: dump every Parameters entry DigiKey
        # actually returned for this part, so _parse_dimensions_mm can
        # be pointed at whatever label the API really uses (e.g.
        # "Package / Case") instead of guessing.
        token = _get_digikey_token()
        client_id = os.getenv("DIGIKEY_CLIENT_ID")
        resp = requests.get(
            DIGIKEY_PRODUCT_DETAILS_URL.format(part_number="296-6501-1-ND"),
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