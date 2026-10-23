"""
api/middleware.py — keeps unhandled server errors readable by the browser.

THE PROBLEM THIS EXISTS TO SOLVE
--------------------------------
Starlette builds the middleware stack as

    ServerErrorMiddleware -> [user middleware, e.g. CORSMiddleware] -> ExceptionMiddleware -> routes

`HTTPException`, `RequestValidationError` and every exception that has
its own `@app.exception_handler` (DatabaseUnavailable, AgentPoolSaturated,
AgentTaskTimeout in api/server.py) are answered by ExceptionMiddleware,
which sits INSIDE CORSMiddleware, so those responses get their
`Access-Control-Allow-Origin` header. An exception nobody handles (a
psycopg error, a plain bug) instead escapes all the way up to
ServerErrorMiddleware, the OUTERMOST layer, which writes its bare
"Internal Server Error" 500 from outside CORSMiddleware: no CORS
headers. The browser then refuses to expose that response to the page
and `fetch()` rejects with an opaque "TypeError: Failed to fetch" — so
every backend bug (e.g. the Build tab's move/rename hitting a database
permission error) looked like a connectivity problem, and the real
status code and message were invisible to the frontend.

(`@app.exception_handler(Exception)` does NOT help: Starlette routes
that one specifically to ServerErrorMiddleware too, so it has the same
problem.)

THE FIX
-------
UnhandledErrorMiddleware sits just INSIDE CORSMiddleware. It catches
whatever the routes/ExceptionMiddleware didn't handle and answers with a
JSON 500 in the same `{"detail": ...}` shape HTTPException produces
(what the frontend's error parsing already reads). That response then
travels out through CORSMiddleware's `send`, which stamps the CORS
headers on it like any other response.

It then RE-RAISES the original exception. That is deliberate and is
Starlette's own convention (ServerErrorMiddleware "always continues to
raise the exception"): the response is already sent, so the re-raise
changes nothing for the client, but it keeps the existing behavior of
uvicorn logging the full traceback and Sentry's integration capturing
the error. Swallowing it here would silence both.

The client only ever sees a generic message; the exception text can
contain SQL, table names or file paths and stays in the server log.

Use add_cors_and_error_middleware() rather than adding the two
middlewares by hand: the ORDER is the whole point (this one must be
added BEFORE CORSMiddleware, since the last middleware added is the
outermost), and having one function own it lets tests pin the order.

CORS ORIGINS (Patch B07)
------------------------
Which origins may call the API is configured by two env vars read in
api/server.py and parsed here, so the parsing is unit-testable without
booting the server:

  ALLOWED_ORIGINS       comma-separated explicit origins. Unset or blank
                        means DEFAULT_ALLOWED_ORIGINS, which names both
                        http://localhost:3000 and http://127.0.0.1:3000
                        (a browser treats them as different origins, and
                        `next dev` is reachable at either). A non-blank
                        value REPLACES the defaults: production sets its
                        real origin(s) explicitly and gets nothing else.
  ALLOWED_ORIGIN_REGEX  optional, DEV ONLY, for testing from another
                        device on the LAN (http://192.168.x.y:3000). A
                        regex is far easier to get wrong than a list, so
                        production leaves it unset.

Credentials are never enabled. The middleware does not offer an
allow_credentials option and passes allow_credentials=False explicitly,
so "*" (or a regex such as ".*") can never be combined with credentials:
Starlette answers a credentialed request to a wildcard by reflecting the
caller's Origin, which would let any website make authenticated calls.
The API authenticates with an Authorization header, which needs no
credentials mode. If cookie auth is ever added, it must also stop
accepting "*" before allow_credentials is switched on.
"""
import contextlib
import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Generic on purpose — see the module docstring.
INTERNAL_ERROR_DETAIL = "Internal server error"

# Patch B07: what ALLOWED_ORIGINS means when it is unset or blank. Both
# spellings of loopback, because the browser matches the Origin header
# literally and developers open the app at either one.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def parse_allowed_origins(raw: str | None) -> list[str]:
    """Turns the ALLOWED_ORIGINS env string into the origin list.

    Items are comma-separated; whitespace around an item is stripped and
    empty items are ignored (so "a, b,," and a trailing comma are fine).
    If nothing is left (the variable is unset, blank, or only commas)
    the dev defaults are used. A blank value never means "allow
    everything" and never means "allow nothing"; a typo'd empty line in
    a .env file should not lock the developer out of their own app.

    Always returns a new list, so a caller cannot mutate the defaults.
    """
    items = [item.strip() for item in (raw or "").split(",")]
    items = [item for item in items if item]
    return items or list(DEFAULT_ALLOWED_ORIGINS)


def parse_allowed_origin_regex(raw: str | None) -> str | None:
    """Turns the ALLOWED_ORIGIN_REGEX env string into the regex, or None.

    DEV ONLY (LAN testing). Blank or unset means "no regex". The pattern
    is matched against the WHOLE Origin header (Starlette uses
    fullmatch), so anchors are not needed, but it should still be as
    narrow as possible, for example
    ``http://192\\.168\\.\\d{1,3}\\.\\d{1,3}:3000``.
    """
    value = (raw or "").strip()
    return value or None


class UnhandledErrorMiddleware:
    """Pure-ASGI (not BaseHTTPMiddleware) so it adds no buffering or
    task-group overhead to streaming responses such as the task SSE
    stream. Only HTTP scopes are touched; websocket/lifespan pass
    straight through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            # Once headers have gone out (e.g. a streaming response that
            # failed midway) there is no way to change the status, so
            # only answer if nothing has been sent yet.
            if not response_started:
                # Best effort: if the client already disconnected, the
                # send fails — that must not mask the original error.
                with contextlib.suppress(Exception):
                    response = JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)
                    await response(scope, receive, send)
            # Always re-raise: uvicorn logs the traceback, Sentry captures it.
            raise


def add_cors_and_error_middleware(
    app: FastAPI,
    allowed_origins: list[str],
    *,
    allowed_origin_regex: str | None = None,
) -> None:
    """Installs UnhandledErrorMiddleware and CORSMiddleware in the one
    order that works: error middleware first (inner), CORS last
    (outer), so the error middleware's 500 passes through CORS.

    `allowed_origin_regex` (Patch B07, keyword-only, default None so
    existing callers are unaffected) is a DEV-ONLY extra: an origin is
    allowed if it is in `allowed_origins` OR fully matches the regex.
    An invalid pattern raises ValueError here, at boot, instead of
    failing on the first request when Starlette would compile it.

    Credentials are deliberately not configurable: see "CORS ORIGINS" in
    the module docstring.
    """
    if allowed_origin_regex is not None:
        try:
            re.compile(allowed_origin_regex)
        except re.error as exc:
            raise ValueError(
                f"ALLOWED_ORIGIN_REGEX is not a valid regular expression: {exc}"
            ) from exc

    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_origin_regex=allowed_origin_regex,
        # Explicit, not just the Starlette default: never combine a
        # wildcard (or a catch-all regex) with credentials.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
