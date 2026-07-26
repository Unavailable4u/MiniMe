"""
agents/source_manager.py — Data Layer architecture §1/§3. Source
Manager, promoted out of "just a Notebooks helper" into a system-wide
role: the one place any upload, from any tab, funnels through.

This patch is the skill-dispatcher shell only: process_upload() picks
the matching existing ingestion skill (agents/pdf_ingestor.py,
agents/importer.py, agents/voice_ingestor.py, agents/video_ingestor.py,
agents/web_clipper.py) and writes the result as a Primary Source node
via agents/source_ingestor.py's write_ingested_source() -- the exact
two-step shape every upload endpoint in api/server.py already does by
hand today. Zero internal edits to any of the five ingestors: they stay
unchanged, deterministic, verbatim, same as the architecture doc calls
for.

Deliberately NOT yet doing (later patches in this same build step):
  - Mode A topic extraction into Secondary Data (§3, next patch)
  - content_hint computation (same patch as topic extraction)
  - parallel fan-out for large uploads via eo/worker_pool.py (after that)
  - wiring the six existing api/server.py endpoints to call this
    instead of their own ingestor + write_ingested_source() calls
    (a separate patch -- this module has no callers yet)

Place this file at: agents/source_manager.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_ingestor import write_ingested_source
from agents.pdf_ingestor import ingest_pdf
from agents.importer import import_artifact
from agents.voice_ingestor import ingest_voice
from agents.video_ingestor import ingest_video
from agents.web_clipper import clip_url

# One entry per ingestion skill, not per file extension -- "import"
# covers every agents/importer.py format (docx/pptx/xlsx/csv/md/json)
# through one kind, since importer.py already dispatches on its own
# `fmt` param internally. Keeping the kind vocabulary this small (one
# per ingestor module) means a new importer.py format needs zero
# changes here.
#
# Each dispatch function takes (payload, **kwargs) so the different
# ingestors' different argument shapes (a local path vs a url, an
# optional fmt/default_title) are absorbed here, not leaked to callers
# as five different calling conventions.
_INGEST_DISPATCH = {
    "pdf": lambda payload, **kw: ingest_pdf(payload),
    "import": lambda payload, **kw: import_artifact(
        payload, fmt=kw.get("fmt"), default_title=kw.get("default_title"),
    ),
    "voice": lambda payload, **kw: ingest_voice(payload),
    "video": lambda payload, **kw: ingest_video(payload),
    "web_clip": lambda payload, **kw: clip_url(payload),
}

# kind -> what `payload` means for that kind, kept here so a caller (or
# a future endpoint wiring patch) doesn't have to go read each
# ingestor's own docstring just to know whether to pass a path or a
# url.
PAYLOAD_KIND = {
    "pdf": "path",       # local file path, same as agents/pdf_ingestor.py:ingest_pdf()
    "import": "path",    # local file path, same as agents/importer.py:import_artifact()
    "voice": "path",     # local file path, same as agents/voice_ingestor.py:ingest_voice()
    "video": "url",       # same as agents/video_ingestor.py:ingest_video()
    "web_clip": "url",   # same as agents/web_clipper.py:clip_url()
}


def process_upload(kind: str, payload: str, workspace_id: str,
                    session_id: str = None, created_by: str = "user",
                    section: str = "notes", **ingest_kwargs) -> dict:
    """The one entry point every upload -- from any tab, not just
    Notebooks -- is meant to funnel through (§1, §9). Picks the
    matching ingestion skill by `kind`, runs it on `payload` (a local
    file path or a url, per PAYLOAD_KIND above), then writes the result
    as a Primary Source node exactly the way every existing upload
    endpoint already does by hand.

    `kind` must be one of _INGEST_DISPATCH's keys ("pdf", "import",
    "voice", "video", "web_clip"). Anything else raises ValueError --
    same "let the caller translate this into a 400" convention every
    ingestor here already uses for its own bad-input cases, so this
    dispatcher doesn't need a second error-handling convention.

    `ingest_kwargs` passes through to the underlying ingestor for the
    one skill that needs extra arguments -- "import"'s optional `fmt`
    and `default_title`. Every other kind ignores unrecognized kwargs.

    session_id is forwarded to write_ingested_source() unchanged (new
    as of the previous build step, §0/§10) -- this function doesn't
    inspect or validate it, just passes it along.

    Returns {"node_ids": [...], "title": str, "kind": str} -- the same
    {"node_ids", "title"} shape every existing upload endpoint already
    returns today, plus `kind` since this one function now serves all
    five where each endpoint used to only need to report its own.
    """
    if kind not in _INGEST_DISPATCH:
        raise ValueError(
            f"Unknown upload kind {kind!r}; expected one of "
            f"{sorted(_INGEST_DISPATCH)}"
        )

    artifact = _INGEST_DISPATCH[kind](payload, **ingest_kwargs)
    node_ids = write_ingested_source(
        artifact, workspace_id, created_by=created_by,
        section=section, session_id=session_id,
    )
    return {"node_ids": node_ids, "title": artifact.get("title", "Untitled"), "kind": kind}
