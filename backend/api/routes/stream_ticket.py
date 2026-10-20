"""
api/routes/stream_ticket.py

POST /api/stream-ticket -- mints the short-lived, single-use ticket that
the /ws/{session_id} WebSocket and the /api/task/{session_id}/stream SSE
route accept in place of the user's full access token. See
api/stream_tickets.py for why this exists and how redemption works.

This is the one place the browser presents its real credential for these
transports, and it does so the normal way: an Authorization header, which
require_auth() verifies like on every other route.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from api.deps import _resolve_chat_or_404, require_auth
from api.stream_tickets import (
    KIND_SSE,
    TICKET_TTL_SECONDS,
    TicketStoreUnavailable,
    issue_ticket,
)

router = APIRouter()


class StreamTicketRequest(BaseModel):
    kind: Literal["ws", "sse"]
    session_id: str = Field(min_length=1, max_length=200)


class StreamTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@router.post("/api/stream-ticket", response_model=StreamTicketResponse)
def create_stream_ticket(
    req: StreamTicketRequest,
    response: Response,
    owner_id: str = Depends(require_auth),
):
    # An SSE ticket is only useful for GET /api/task/{id}/stream, which
    # already requires read access to the chat. Check that here too, so
    # we never mint one for a chat the caller can't read and the client
    # gets its 404 up front instead of after opening a stream.
    #
    # Deliberately NOT done for "ws": /ws/{session_id} has never checked
    # session ownership, so adding it here would change who can connect
    # rather than just how they authenticate. The ticket still binds to
    # the session_id, so it can't be replayed against a different one.
    if req.kind == KIND_SSE:
        _resolve_chat_or_404(req.session_id, owner_id, require_edit=False)

    try:
        ticket = issue_ticket(owner_id, req.kind, req.session_id)
    except TicketStoreUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Stream tickets are temporarily unavailable. Try again shortly.",
        )

    # A ticket is a credential; keep it out of any shared cache.
    response.headers["Cache-Control"] = "no-store"
    return StreamTicketResponse(ticket=ticket, expires_in=TICKET_TTL_SECONDS)
