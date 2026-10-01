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
"""
import contextlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Generic on purpose — see the module docstring.
INTERNAL_ERROR_DETAIL = "Internal server error"


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


def add_cors_and_error_middleware(app: FastAPI, allowed_origins: list[str]) -> None:
    """Installs UnhandledErrorMiddleware and CORSMiddleware in the one
    order that works: error middleware first (inner), CORS last
    (outer), so the error middleware's 500 passes through CORS."""
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
