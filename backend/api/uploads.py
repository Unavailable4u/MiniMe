"""Patch SX-20 -- save an uploaded file to a temp path without ever holding it all in memory,
and refuse it with HTTP 413 as soon as it passes a size cap.

Why: /api/notes/import, /api/notes/pdf and /api/notes/voice each did
`tmp.write(await file.read())`, which loads the whole upload into memory with no limit.
`save_upload_capped` reads in 1 MB chunks instead, so memory use stays at about one chunk
and an oversize upload is cut off at `max_bytes + 1` bytes. This patch adds the helper only;
SX-21 switches the three routes over to it.

The caps below are PLACEHOLDERS (decision D-3: no cost model exists yet). Each can be
overridden by an environment variable of the same name; an unset, empty, non-numeric or
non-positive value falls back to the placeholder.

What this does NOT do: the multipart parser in Starlette has already received the whole
request body (into a spooled temp file) before a route function runs, so this cap limits what
the route copies and processes, not what the server first receives. Limiting the request
body itself is a reverse-proxy job (the Caddy request limits listed as later work in the
patch guide).
"""
import logging
import os
import tempfile

from fastapi import HTTPException

logger = logging.getLogger(__name__)

CHUNK_BYTES = 1_048_576  # 1 MB per read


def _env_int(name: str, default: int) -> int:
    """A positive int from the environment variable `name`, else `default`."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning("ignoring invalid %s (need a positive integer); using the default", name)
        return default
    return value


# Placeholders until decision D-3 -- see the module docstring.
IMPORT_MAX_BYTES = _env_int("IMPORT_MAX_BYTES", 10_000_000)
PDF_MAX_BYTES = _env_int("PDF_MAX_BYTES", 25_000_000)
VOICE_MAX_BYTES = _env_int("VOICE_MAX_BYTES", 25_000_000)


async def save_upload_capped(file, suffix: str, max_bytes: int) -> str:
    """Copy `file` (a Starlette/FastAPI UploadFile) into a new temp file and return its path.

    Reads in chunks of at most CHUNK_BYTES and never asks for more than one byte past the
    cap. If the upload is larger than `max_bytes`, the temp file is deleted and
    HTTPException(413) is raised. If anything else goes wrong while copying (a read error,
    the request being cancelled) the temp file is deleted too and the error propagates. On
    success the CALLER owns the file and must delete it.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = tmp.name
    try:
        with tmp:
            total = 0
            while True:
                chunk = await file.read(min(CHUNK_BYTES, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload too large: the limit is {max_bytes:,} bytes.",
                    )
                tmp.write(chunk)
    except BaseException:
        # The `with` above has already closed the file, so it can be removed (also on Windows).
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        raise
    return path
