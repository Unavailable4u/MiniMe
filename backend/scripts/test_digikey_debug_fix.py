#!/usr/bin/env python3
"""
test_digikey_debug_fix.py — verifies the fix to component_spec_lookup.py's
DEBUG_DIGIKEY_PARAMS block (undefined DIGIKEY_PRODUCT_DETAILS_URL / Ruff F821
/ Pylance reportUndefinedVariable, the exact errors from the Problems panel).

Usage:
    python3 test_digikey_debug_fix.py /path/to/backend/agents/component_spec_lookup.py

Two independent checks, run in order:

  1. STATIC  — runs `ruff check --select F821` on the file, the same rule
     that flagged `DIGIKEY_PRODUCT_DETAILS_URL` before. Fails loudly if any
     undefined-name error remains.

  2. FUNCTIONAL — actually executes the file's __main__ block with
     DEBUG_DIGIKEY_PARAMS=1, but with `requests`, `dotenv`, `upstash_redis`,
     and `upstash_vector` replaced by in-memory fakes (no real network, no
     real API keys, no real Redis needed). This proves the debug block
     doesn't just lint clean — it actually runs, hits the keyword-search
     endpoint (not the old productdetails one), and correctly reads
     Parameters off Products[0].

Exit code 0 = both checks passed. Non-zero = something's still broken.

You can also point this at the OLD/unpatched file to confirm the test
itself is meaningful (it should FAIL loudly with a NameError on that one).
"""
import sys
import os
import io
import types
import runpy
import subprocess
import contextlib


def check_static(target: str) -> bool:
    print("=" * 70)
    print("CHECK 1/2 — static lint (ruff, rule F821: undefined name)")
    print("=" * 70)
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821", target],
        capture_output=True, text=True,
    )
    print(result.stdout.strip() or "(no output)")
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode == 0:
        print("PASS: no undefined-name errors.\n")
        return True
    else:
        print("FAIL: undefined-name error(s) still present.\n")
        return False


# ---- fakes used by the functional check -----------------------------------

class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


# Set once so the functional check can confirm which URL shape was hit
# (POST /search/keyword, not GET/format-string .../productdetails).
_calls = []


def _fake_post(url, headers=None, json=None, params=None, timeout=None, **kw):
    _calls.append(("POST", url, json))
    if url.endswith("/oauth2/token"):
        return _FakeResponse({"access_token": "FAKE_TOKEN", "expires_in": 599})
    if url.endswith("/search/keyword"):
        # Sanity-check the request shape the fixed code is supposed to send.
        assert json is not None and "Keywords" in json, (
            "expected a JSON body with 'Keywords' for the keyword-search POST"
        )
        return _FakeResponse({
            "Products": [
                {
                    "ManufacturerProductNumber": "NA555P",
                    "DatasheetUrl": None,
                    "Parameters": [
                        {"ParameterText": "Size / Dimension", "ValueText": "9.00mm x 5.00mm"},
                        {"ParameterText": "Package / Case", "ValueText": "DIP-8"},
                    ],
                },
                {
                    "ManufacturerProductNumber": "NA555D",
                    "DatasheetUrl": None,
                    "Parameters": [
                        {"ParameterText": "Size / Dimension", "ValueText": "4.90mm x 3.90mm"},
                    ],
                },
            ]
        })
    if "mouser.com" in url:
        return _FakeResponse({"Errors": [], "SearchResults": {"Parts": []}})
    raise AssertionError(f"unexpected POST to {url}")


def _fake_get(url, headers=None, timeout=None, stream=None, **kw):
    _calls.append(("GET", url, None))
    # The old buggy code did requests.get(DIGIKEY_PRODUCT_DETAILS_URL...).
    # The fixed code should never call requests.get at all in this block.
    raise AssertionError(f"unexpected GET to {url} — fixed code should only POST")


def _install_fakes():
    fake_requests = types.ModuleType("requests")
    fake_requests.post = _fake_post
    fake_requests.get = _fake_get
    sys.modules["requests"] = fake_requests

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **kw: None
    sys.modules["dotenv"] = fake_dotenv

    fake_upstash_redis = types.ModuleType("upstash_redis")

    class _FakeRedis:
        def __init__(self, *a, **kw):
            self._store = {}

        def get(self, key):
            return self._store.get(key)

        def set(self, key, value, **kw):
            self._store[key] = value

    fake_upstash_redis.Redis = _FakeRedis
    sys.modules["upstash_redis"] = fake_upstash_redis

    fake_upstash_vector = types.ModuleType("upstash_vector")

    class _FakeIndex:
        def __init__(self, *a, **kw):
            pass

    fake_upstash_vector.Index = _FakeIndex
    sys.modules["upstash_vector"] = fake_upstash_vector


def check_functional(target: str) -> bool:
    print("=" * 70)
    print("CHECK 2/2 — functional (actually run DEBUG_DIGIKEY_PARAMS block)")
    print("=" * 70)
    print("Mocking: requests, dotenv, upstash_redis, upstash_vector")
    print("Real network/API keys/Redis: NOT required for this check.\n")

    _install_fakes()

    os.environ["DIGIKEY_CLIENT_ID"] = "fake-id"
    os.environ["DIGIKEY_CLIENT_SECRET"] = "fake-secret"
    os.environ.pop("MOUSER_API_KEY", None)
    os.environ["DEBUG_DIGIKEY_PARAMS"] = "1"

    buf = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(buf):
            runpy.run_path(target, run_name="__main__")
    except Exception as e:  # noqa: BLE001 — we want to see anything, incl. NameError
        error = e

    output = buf.getvalue()
    print("--- captured stdout ---")
    print(output)
    print("--- end captured stdout ---\n")

    if error is not None:
        print(f"FAIL: script raised {type(error).__name__}: {error}\n")
        return False

    keyword_hits = [c for c in _calls if c[1].endswith("/search/keyword")]
    if not keyword_hits:
        print("FAIL: debug block never called the keyword-search endpoint.\n")
        return False

    expect_snippets = [
        "showing Parameters for first of 2 candidate(s): 'NA555P'",
        "'Size / Dimension': '9.00mm x 5.00mm'",
        "'Package / Case': 'DIP-8'",
    ]
    missing = [s for s in expect_snippets if s not in output]
    if missing:
        print(f"FAIL: expected output missing: {missing}\n")
        return False

    print("PASS: debug block ran end-to-end, hit the keyword-search endpoint,")
    print("      and printed Parameters from the first candidate correctly.\n")
    return True


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} /path/to/component_spec_lookup.py")
        sys.exit(2)

    target = os.path.abspath(sys.argv[1])
    if not os.path.isfile(target):
        print(f"no such file: {target}")
        sys.exit(2)

    ok_static = check_static(target)
    ok_functional = check_functional(target)

    print("=" * 70)
    if ok_static and ok_functional:
        print("RESULT: PASS — the fix works as intended.")
        sys.exit(0)
    else:
        print("RESULT: FAIL — see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
