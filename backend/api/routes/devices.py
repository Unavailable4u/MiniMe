"""
api/routes/devices.py -- S03: the HTTP surface for M2C device pairing.

Two audiences, one router (mounted with a single include_router line in
api/server.py):

  The terminal (`m2c login`, no account session yet) -- /v1/code/devices/*
    POST /v1/code/devices/pair/start   {device_name, os, client_version}
         -> {device_code, user_code, verification_url, interval, expires_in}
    POST /v1/code/devices/pair/poll    {device_code}
         -> {status: pending | approved | denied | expired}; `approved` also
            carries {device_credential, device_id}, exactly once.
            An unknown or already-collected device_code is a 400.
    POST /v1/code/devices/token        {device_credential}
         -> {jwt, expires_in}   (a ~15 minute device JWT; 401 if the
            credential is unknown or the device was revoked)

  The signed-in web app (require_auth, the usual Supabase bearer) -- /api/devices*
    POST /api/devices/approve          {user_code} -> binds the terminal to the caller
    POST /api/devices/deny             {user_code} -> refuses it ("not my terminal")
    GET  /api/devices[?include_revoked=true]
                                       -> {devices: [...]}, the caller's own, active only by default
    POST /api/devices/{id}/revoke      -> {id, revoked_at}; 404 for anyone else's device

All the decisions live in eo/devices.py (read its docstring first); this
file only turns its outcomes into status codes. Status mapping:

    429  too many attempts: pair/start limiter, or the account is locked
         after repeated wrong user codes (Retry-After set)
    503  not configured (no DEVICE_JWT_SECRET) or the rate-limit store is
         down -- fail closed, nothing is issued or approved
    404  approve/deny: the code is wrong, expired or used (one message for
         all of them); revoke: not your device (or no such device)
    400  poll: unknown, malformed or already-collected device code
    401  token: unknown, malformed or revoked credential (one message)

THE SECRETS NEVER LEAVE THE REQUEST BODY. device_code, user_code and the
device credential are POST bodies, never paths or query strings, so
uvicorn/proxy access logs cannot capture them. The request models hold them
as pydantic SecretStr, whose repr is masked, so a model that lands in a
traceback, a debug log or an error-tracker's captured locals does not print
them. Responses that carry a secret set `Cache-Control: no-store`. Error
messages are fixed strings; none echoes input.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator

from api.deps import require_auth
from eo import devices

router = APIRouter()


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------

class PairStartRequest(BaseModel):
    """What the terminal says about itself. Three short labels, nothing else:
    the registry is content-free (build plan section O1), so anything beyond
    these fields in the body is ignored, and the labels are length-capped and
    must be plain printable text."""
    device_name: str = Field(max_length=64)
    os: str = Field(max_length=64)
    client_version: str = Field(max_length=32)

    @field_validator("device_name", "os", "client_version")
    @classmethod
    def _plain_label(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("must be non-empty, printable text")
        return value


class PairStartResponse(BaseModel):
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


class PairPollRequest(BaseModel):
    device_code: SecretStr


class PairPollResponse(BaseModel):
    status: Literal["pending", "approved", "denied", "expired"]
    device_credential: str | None = None
    device_id: str | None = None


class TokenRequest(BaseModel):
    device_credential: SecretStr


class TokenResponse(BaseModel):
    jwt: str
    expires_in: int


class UserCodeRequest(BaseModel):
    user_code: SecretStr


class ApproveResponse(BaseModel):
    status: Literal["approved"]
    device_name: str
    os: str
    client_version: str


class DenyResponse(BaseModel):
    status: Literal["denied"]


class DeviceOut(BaseModel):
    id: str
    device_name: str
    os: str
    client_version: str
    created_at: str
    last_seen_at: str | None = None
    revoked_at: str | None = None


class DeviceListResponse(BaseModel):
    devices: list[DeviceOut]


class RevokeResponse(BaseModel):
    id: str
    revoked_at: str


# ---------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------

def _http_error(exc: devices.DeviceError) -> HTTPException:
    """One fixed message per outcome; never anything from the request."""
    if isinstance(exc, devices.RateLimited):
        return HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        )
    if isinstance(exc, devices.NotConfigured):
        return HTTPException(status_code=503, detail="Device pairing is not configured on this server.")
    if isinstance(exc, devices.StoreUnavailable):
        return HTTPException(
            status_code=503,
            detail="Device pairing is temporarily unavailable. Try again shortly.",
        )
    if isinstance(exc, devices.InvalidCode):
        return HTTPException(status_code=404, detail="That code is invalid or has expired.")
    if isinstance(exc, devices.InvalidDeviceCode):
        return HTTPException(status_code=400, detail="Invalid or already-used device code.")
    if isinstance(exc, devices.InvalidCredential):
        return HTTPException(status_code=401, detail="Invalid or revoked device credential.")
    if isinstance(exc, devices.DeviceNotFound):
        return HTTPException(status_code=404, detail="Device not found.")
    return HTTPException(status_code=500, detail="Internal server error")


def _no_store(response: Response) -> None:
    # A response carrying a secret must not sit in any shared cache.
    response.headers["Cache-Control"] = "no-store"


# ---------------------------------------------------------------------
# Terminal side (no account session)
# ---------------------------------------------------------------------

@router.post("/v1/code/devices/pair/start", response_model=PairStartResponse)
def pair_start(req: PairStartRequest, request: Request, response: Response):
    _no_store(response)
    try:
        return devices.start_pairing(
            device_name=req.device_name,
            os_name=req.os,
            client_version=req.client_version,
            client_ip=request.client.host if request.client else None,
        )
    except devices.DeviceError as exc:
        raise _http_error(exc) from None


@router.post("/v1/code/devices/pair/poll", response_model=PairPollResponse,
             response_model_exclude_none=True)
def pair_poll(req: PairPollRequest, response: Response):
    _no_store(response)
    try:
        return devices.poll_pairing(req.device_code.get_secret_value())
    except devices.DeviceError as exc:
        raise _http_error(exc) from None


@router.post("/v1/code/devices/token", response_model=TokenResponse)
def device_token(req: TokenRequest, response: Response):
    _no_store(response)
    try:
        return devices.issue_device_token(req.device_credential.get_secret_value())
    except devices.DeviceError as exc:
        raise _http_error(exc) from None


# ---------------------------------------------------------------------
# Web side (signed-in account)
# ---------------------------------------------------------------------

@router.post("/api/devices/approve", response_model=ApproveResponse)
def approve_device(req: UserCodeRequest, owner_id: str = Depends(require_auth)):
    try:
        return devices.approve_pairing(owner_id, req.user_code.get_secret_value())
    except devices.DeviceError as exc:
        raise _http_error(exc) from None


@router.post("/api/devices/deny", response_model=DenyResponse)
def deny_device(req: UserCodeRequest, owner_id: str = Depends(require_auth)):
    try:
        return devices.deny_pairing(owner_id, req.user_code.get_secret_value())
    except devices.DeviceError as exc:
        raise _http_error(exc) from None


@router.get("/api/devices", response_model=DeviceListResponse)
def list_my_devices(include_revoked: bool = False, owner_id: str = Depends(require_auth)):
    return {"devices": devices.list_devices(owner_id, include_revoked=include_revoked)}


@router.post("/api/devices/{device_id}/revoke", response_model=RevokeResponse)
def revoke_my_device(device_id: str, owner_id: str = Depends(require_auth)):
    try:
        return devices.revoke_device(owner_id, device_id)
    except devices.DeviceError as exc:
        raise _http_error(exc) from None
