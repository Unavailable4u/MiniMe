"""
tests/unit/test_api_middleware.py — api/middleware.py.

The bug these pin: an UNHANDLED exception (a DB error, any bug) used to
be answered by Starlette's outermost ServerErrorMiddleware, i.e. from
outside CORSMiddleware, so the 500 carried no Access-Control-Allow-Origin
header and the browser surfaced it as an opaque "Failed to fetch"
instead of a readable 500 (this is what made the Build tab's move/rename
permission error look like a network failure).

These tests build a small app through add_cors_and_error_middleware() —
the exact function api/server.py calls — rather than importing
api.server (which boots the DB pool, Sentry, Laya, MCP...), so the real
middleware order is what's under test.

Patch B07 (M2C) adds the CORS-origin tests at the bottom of this file:
the dev-friendly default origins, the ALLOWED_ORIGINS override, the
optional dev-only ALLOWED_ORIGIN_REGEX, and the rule that a wildcard
can never be combined with credentials.
"""
import asyncio
import warnings

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.middleware import (
    DEFAULT_ALLOWED_ORIGINS,
    INTERNAL_ERROR_DETAIL,
    UnhandledErrorMiddleware,
    add_cors_and_error_middleware,
    parse_allowed_origin_regex,
    parse_allowed_origins,
)

warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)

ORIGIN = "http://localhost:3000"


class _Saturated(Exception):
    """Stands in for DatabaseUnavailable & co: has its own handler."""


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(_Saturated)
    async def _saturated(request: Request, exc: _Saturated):
        return JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": "2"})

    add_cors_and_error_middleware(app, [ORIGIN])

    @app.get("/boom")
    def boom():
        raise RuntimeError("select * from secret_table failed: permission denied")

    @app.get("/http-error")
    def http_error():
        raise HTTPException(status_code=400, detail="bad path")

    @app.get("/saturated")
    def saturated():
        raise _Saturated()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    return app


@pytest.fixture
def client():
    # raise_server_exceptions=False: look at the response a browser would get.
    return TestClient(_build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------
# The fix itself
# ---------------------------------------------------------------------

def test_unhandled_error_is_a_json_500_with_cors_headers(client):
    r = client.get("/boom", headers={"Origin": ORIGIN})
    assert r.status_code == 500
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.json() == {"detail": INTERNAL_ERROR_DETAIL}


def test_unhandled_error_does_not_leak_exception_text(client):
    r = client.get("/boom", headers={"Origin": ORIGIN})
    assert "secret_table" not in r.text
    assert "permission denied" not in r.text


def test_unhandled_error_is_still_raised_to_the_server():
    # The response is sent, but the exception must keep propagating so
    # uvicorn logs the traceback and Sentry's integration captures it.
    strict = TestClient(_build_app())  # raise_server_exceptions=True
    with pytest.raises(RuntimeError, match="secret_table"):
        strict.get("/boom", headers={"Origin": ORIGIN})


def test_cors_is_not_broadened_for_other_origins(client):
    r = client.get("/boom", headers={"Origin": "http://evil.example"})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------
# Nothing that already worked changes
# ---------------------------------------------------------------------

def test_http_exception_is_untouched(client):
    r = client.get("/http-error", headers={"Origin": ORIGIN})
    assert r.status_code == 400
    assert r.json() == {"detail": "bad path"}
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_registered_exception_handlers_still_win(client):
    r = client.get("/saturated", headers={"Origin": ORIGIN})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json() == {"detail": "busy"}


def test_successful_requests_are_untouched(client):
    r = client.get("/ok", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_preflight_still_answered(client):
    r = client.options("/boom", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200
    assert "POST" in r.headers["access-control-allow-methods"]


def test_premise_cors_alone_does_not_cover_unhandled_errors():
    """Documents WHY the middleware exists: with CORSMiddleware alone,
    Starlette's outermost layer answers an unhandled error without CORS
    headers. If a Starlette upgrade ever makes this fail, the middleware
    has become unnecessary and can be removed."""
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["GET"], allow_headers=["*"])

    @app.get("/boom")
    def boom():
        raise RuntimeError("x")

    r = TestClient(app, raise_server_exceptions=False).get("/boom", headers={"Origin": ORIGIN})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------
# Raw-ASGI edge cases (driven directly; TestClient can't express these)
# ---------------------------------------------------------------------

def _run(middleware, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def go():
        await middleware(scope, receive, send)

    return sent, go


def test_error_after_response_started_is_not_answered_twice():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("died mid-stream")

    sent, go = _run(UnhandledErrorMiddleware(app), {"type": "http"})
    with pytest.raises(RuntimeError, match="died mid-stream"):
        asyncio.run(go())

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1 and starts[0]["status"] == 200


def test_failure_to_send_the_500_does_not_mask_the_original_error():
    async def app(scope, receive, send):
        raise ValueError("original")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def disconnected_send(message):
        raise OSError("client went away")

    mw = UnhandledErrorMiddleware(app)
    with pytest.raises(ValueError, match="original"):
        asyncio.run(mw({"type": "http"}, receive, disconnected_send))


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
def test_non_http_scopes_pass_straight_through(scope_type):
    async def app(scope, receive, send):
        raise RuntimeError("ws failure")

    sent, go = _run(UnhandledErrorMiddleware(app), {"type": scope_type})
    with pytest.raises(RuntimeError, match="ws failure"):
        asyncio.run(go())
    assert sent == []  # no HTTP 500 invented for a non-HTTP scope

# ---------------------------------------------------------------------
# Patch B07 — dev-friendly CORS origins
#
# api/server.py cannot be imported in a unit test (it boots the DB pool,
# Sentry, Laya, MCP...), so the env-string parsing lives in
# api/middleware.py as pure functions and is tested here directly; the
# behavior tests go through add_cors_and_error_middleware(), the exact
# function api/server.py calls.
# ---------------------------------------------------------------------

LOCALHOST = "http://localhost:3000"
LOOPBACK_IP = "http://127.0.0.1:3000"
LAN_ORIGIN = "http://192.168.1.50:3000"
LAN_REGEX = r"http://192\.168\.\d{1,3}\.\d{1,3}:3000"


def _cors_client(origins, **kwargs) -> TestClient:
    app = FastAPI()
    add_cors_and_error_middleware(app, origins, **kwargs)

    @app.get("/ok")
    def ok():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def _cors_options(app: FastAPI) -> dict:
    """The kwargs CORSMiddleware was installed with."""
    (entry,) = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    return dict(entry.kwargs)


# ----- defaults -------------------------------------------------------

def test_default_origins_include_localhost_and_loopback_ip():
    assert parse_allowed_origins(None) == [LOCALHOST, LOOPBACK_IP]
    assert list(DEFAULT_ALLOWED_ORIGINS) == [LOCALHOST, LOOPBACK_IP]


@pytest.mark.parametrize("raw", ["", "   ", ",", " , ,, "])
def test_blank_or_separator_only_env_falls_back_to_the_defaults(raw):
    # `ALLOWED_ORIGINS=` left blank in a .env file must not silently
    # lock every browser out, and must never turn into a wildcard.
    assert parse_allowed_origins(raw) == [LOCALHOST, LOOPBACK_IP]


def test_default_list_is_a_fresh_copy_each_call():
    first = parse_allowed_origins(None)
    first.append("http://mutated.example")
    assert parse_allowed_origins(None) == [LOCALHOST, LOOPBACK_IP]


def test_loopback_ip_origin_is_allowed_by_default():
    client = _cors_client(parse_allowed_origins(None))
    for origin in (LOCALHOST, LOOPBACK_IP):
        r = client.get("/ok", headers={"Origin": origin})
        assert r.headers["access-control-allow-origin"] == origin


# ----- ALLOWED_ORIGINS override --------------------------------------

def test_env_value_overrides_the_defaults_entirely():
    got = parse_allowed_origins("https://app.example.com")
    assert got == ["https://app.example.com"]
    assert LOCALHOST not in got and LOOPBACK_IP not in got


def test_env_value_is_stripped_and_empty_items_are_ignored():
    raw = "  https://a.example.com , ,https://b.example.com,, \t"
    assert parse_allowed_origins(raw) == ["https://a.example.com", "https://b.example.com"]


def test_overridden_origins_are_what_the_middleware_enforces():
    client = _cors_client(parse_allowed_origins(" https://a.example.com ,"))
    ok = client.get("/ok", headers={"Origin": "https://a.example.com"})
    assert ok.headers["access-control-allow-origin"] == "https://a.example.com"
    # The old dev default is gone once production origins are explicit.
    dev = client.get("/ok", headers={"Origin": LOCALHOST})
    assert "access-control-allow-origin" not in dev.headers


# ----- ALLOWED_ORIGIN_REGEX (dev-only) -------------------------------

def test_regex_setting_is_optional_and_blank_means_none():
    assert parse_allowed_origin_regex(None) is None
    assert parse_allowed_origin_regex("") is None
    assert parse_allowed_origin_regex("   ") is None
    assert parse_allowed_origin_regex(f"  {LAN_REGEX}  ") == LAN_REGEX


def test_regex_origin_is_allowed_and_non_matching_origin_is_not():
    client = _cors_client([LOCALHOST], allowed_origin_regex=LAN_REGEX)
    lan = client.get("/ok", headers={"Origin": LAN_ORIGIN})
    assert lan.headers["access-control-allow-origin"] == LAN_ORIGIN
    # The explicit list keeps working next to the regex.
    explicit = client.get("/ok", headers={"Origin": LOCALHOST})
    assert explicit.headers["access-control-allow-origin"] == LOCALHOST
    other = client.get("/ok", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_regex_must_match_the_whole_origin_not_a_prefix():
    client = _cors_client([LOCALHOST], allowed_origin_regex=LAN_REGEX)
    sneaky = client.get("/ok", headers={"Origin": LAN_ORIGIN + ".evil.example"})
    assert "access-control-allow-origin" not in sneaky.headers


def test_regex_applies_to_preflight_too():
    client = _cors_client([LOCALHOST], allowed_origin_regex=LAN_REGEX)
    r = client.options(
        "/ok", headers={"Origin": LAN_ORIGIN, "Access-Control-Request-Method": "POST"}
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == LAN_ORIGIN


def test_regex_defaults_to_none_so_existing_callers_are_unaffected():
    app = FastAPI()
    add_cors_and_error_middleware(app, [LOCALHOST])  # old two-argument call
    assert _cors_options(app).get("allow_origin_regex") is None
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/nope", headers={"Origin": LAN_ORIGIN})
    assert "access-control-allow-origin" not in r.headers


def test_invalid_regex_fails_at_install_time_with_a_clear_message():
    # Starlette would only compile it lazily, when the first request
    # builds the middleware stack; a typo in ALLOWED_ORIGIN_REGEX must
    # instead stop the server at boot, naming the setting.
    with pytest.raises(ValueError, match="ALLOWED_ORIGIN_REGEX"):
        add_cors_and_error_middleware(FastAPI(), [LOCALHOST], allowed_origin_regex="(")


# ----- wildcard can never be combined with credentials ---------------

@pytest.mark.parametrize(
    "origins, regex",
    [
        (["*"], None),
        ([LOCALHOST, "*"], None),
        ([LOCALHOST], ".*"),
        (parse_allowed_origins("*"), None),
    ],
)
def test_wildcard_is_never_combined_with_credentials(origins, regex):
    app = FastAPI()
    add_cors_and_error_middleware(app, origins, allowed_origin_regex=regex)

    # 1. Configuration: credentials are never switched on.
    assert _cors_options(app).get("allow_credentials", False) is False

    # 2. Behavior: even a cookie-bearing request gets no credentials
    #    grant (Starlette would reflect the origin AND allow
    #    credentials if allow_credentials were True with a wildcard).
    @app.get("/ok")
    def ok():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Origin": "http://anything.example", "Cookie": "session=abc"}
    simple = client.get("/ok", headers=headers)
    assert "access-control-allow-credentials" not in simple.headers
    preflight = client.options(
        "/ok", headers={**headers, "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-credentials" not in preflight.headers


def test_credentials_are_off_for_normal_explicit_origins_too():
    app = FastAPI()
    add_cors_and_error_middleware(app, [LOCALHOST])
    assert _cors_options(app).get("allow_credentials") is False
