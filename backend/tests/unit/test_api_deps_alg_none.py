"""Patch SX-15 (scaffold) -- `alg: none` tokens must be a clean 401 in api/deps.py.

What this file is
-----------------
A scaffold for a known gap found while testing the SX-11 PyJWT bump. It changes no
production code. The tests state the CORRECT behaviour and the ones that fail today are
STRICT xfails, the same convention as tests/canary/test_no_content_at_rest.py: CI stays
green, the gap stays visible, and the day it is fixed the strict xfail turns into a failure
that says "remove this marker".

The gap (observed 2026-11-23 on api/deps.py::_verify_supabase_jwt)
-------------------------------------------------------------------
Only `alg == "HS256"` takes the shared-secret branch. EVERY other `alg` value, including
`none`, `NONE`, an empty string and a missing header, falls into the JWKS branch, which:

  * calls `_get_jwk_client()` -- an unauthenticated caller can make the server fetch the
    JWKS URL; and with SUPABASE_URL unset it answers HTTP 500 "Server misconfigured";
  * lets `PyJWKClientError` (JWKS unreachable, or no key for the token's `kid`) and
    `InvalidKeyError` (lowercase `none` decoded against a real key) escape, because the
    handlers only catch `jwt.InvalidTokenError` and those two derive from `PyJWTError`
    instead -> HTTP 500. (`NONE`, an empty `alg` and a missing `alg` are already turned into
    401 by PyJWT's `InvalidAlgorithmError`, which IS an `InvalidTokenError`; they are kept
    below as regression guards.)

No `alg: none` token is ever ACCEPTED (the control tests rely on that), so this is not an
authentication bypass. It is a wrong status code (500 instead of 401) plus an avoidable
network call before the token has been judged.

Not fixed here on purpose: this is a dependency patch, and a code change belongs in its own
patch. The smallest fix is two lines in `_verify_supabase_jwt`: answer 401 for an `alg` that
is not a real signature algorithm BEFORE touching the JWKS client, and catch
`jwt.PyJWTError` (not only `InvalidTokenError`) around the decode.
"""
import base64
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

import api.deps as deps

_SECRET = "unit-test-secret-not-real-0123456789abcdef"  # >= 32 bytes so PyJWT does not warn
_CLAIMS = {"sub": "user-1", "aud": "authenticated"}


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _unsigned(header: dict) -> str:
    """A token with the given header, valid claims and an EMPTY signature segment."""
    return f"{_b64(header)}.{_b64(_CLAIMS)}."


# Header variants an attacker can send. All have an empty signature, so none can be valid.
_NONE_TOKENS = {
    "none": lambda: jwt.encode(_CLAIMS, None, algorithm="none"),
    "none-with-kid": lambda: jwt.encode(_CLAIMS, None, algorithm="none", headers={"kid": "abc"}),
    "NONE-uppercase": lambda: _unsigned({"alg": "NONE", "typ": "JWT"}),
    "empty-alg": lambda: _unsigned({"alg": "", "typ": "JWT"}),
    "missing-alg": lambda: _unsigned({"typ": "JWT"}),
}

_XFAIL = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="SX-15 scaffold: api/deps.py::_verify_supabase_jwt sends alg 'none' (and any non-HS256 alg) "
           "to the JWKS client and lets PyJWKClientError/InvalidKeyError escape as HTTP 500. "
           "When fixed, delete this marker.",
)


class _ForbiddenJwkClient:
    """Stands in for PyJWKClient; any use means the token reached the network branch."""

    def get_signing_key_from_jwt(self, token):
        raise AssertionError("JWKS client was consulted for a token that must be refused first")


class _OfferedKey:
    """What PyJWKClient returns on a kid match: an object with a real public key in `.key`."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1()).public_key()


class _KeyOnOfferJwkClient:
    def get_signing_key_from_jwt(self, token):
        return _OfferedKey()


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch):
    monkeypatch.setattr(deps, "SUPABASE_URL", "https://project.example.test")
    monkeypatch.setattr(deps, "SUPABASE_JWT_SECRET", _SECRET)
    monkeypatch.setattr(deps, "_jwk_client", None)


# ---- controls: these PASS today and prove the harness can tell 401 from everything else --------

def test_control_valid_hs256_token_is_accepted():
    token = jwt.encode(_CLAIMS, _SECRET, algorithm="HS256")
    assert deps._verify_supabase_jwt(token) == "user-1"


def test_control_tampered_hs256_signature_is_401():
    token = jwt.encode(_CLAIMS, "some-other-secret-0123456789abcdef0123", algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        deps._verify_supabase_jwt(token)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("name", sorted(_NONE_TOKENS))
def test_control_alg_none_is_never_accepted(name, monkeypatch):
    """Whatever the status code, an unsigned token must never return a user id."""
    monkeypatch.setattr(deps, "_get_jwk_client", lambda: _KeyOnOfferJwkClient())
    try:
        result = deps._verify_supabase_jwt(_NONE_TOKENS[name]())
    except Exception:
        return  # refused one way or another: that is all this control asserts
    pytest.fail(f"unsigned token {name!r} was accepted as user {result!r}")


# ---- the gap: these state the correct behaviour and fail today (strict xfail) ------------------

@_XFAIL
@pytest.mark.parametrize("name", sorted(_NONE_TOKENS))
def test_alg_none_is_401_without_any_jwks_fetch(name, monkeypatch):
    monkeypatch.setattr(deps, "_get_jwk_client", lambda: _ForbiddenJwkClient())
    with pytest.raises(HTTPException) as exc:
        deps._verify_supabase_jwt(_NONE_TOKENS[name]())
    assert exc.value.status_code == 401


# With a key on offer PyJWT already refuses NONE / empty / missing alg with a 401 (guards that
# stay green); only the lowercase `none` forms escape as InvalidKeyError today.
_KEY_ON_OFFER_CASES = [
    pytest.param(name, marks=_XFAIL if name in ("none", "none-with-kid") else ())
    for name in sorted(_NONE_TOKENS)
]


@pytest.mark.parametrize("name", _KEY_ON_OFFER_CASES)
def test_alg_none_is_401_even_if_a_jwks_key_is_on_offer(name, monkeypatch):
    monkeypatch.setattr(deps, "_get_jwk_client", lambda: _KeyOnOfferJwkClient())
    try:
        deps._verify_supabase_jwt(_NONE_TOKENS[name]())
    except HTTPException as exc:
        assert exc.status_code == 401
    except Exception as exc:  # PyJWKClientError / InvalidKeyError escaping = today's HTTP 500
        raise AssertionError(f"{type(exc).__name__} escaped instead of HTTP 401") from exc
    else:
        raise AssertionError("token was not refused")
