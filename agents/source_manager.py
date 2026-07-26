"""
agents/source_manager.py — Data Layer architecture §1/§3. Source
Manager, promoted out of "just a Notebooks helper" into a system-wide
role: the one place any upload, from any tab, funnels through.

2a laid down the skill-dispatcher shell only: process_upload() picks
the matching existing ingestion skill (agents/pdf_ingestor.py,
agents/importer.py, agents/voice_ingestor.py, agents/video_ingestor.py,
agents/web_clipper.py) and writes the result as a Primary Source node
via agents/source_ingestor.py's write_ingested_source() -- the exact
two-step shape every upload endpoint in api/server.py already does by
hand today. Zero internal edits to any of the five ingestors: they stay
unchanged, deterministic, verbatim, same as the architecture doc calls
for. 2b wired every existing api/server.py upload endpoint to call
process_upload() instead of its own ingestor + write_ingested_source()
calls.

This patch (2c) added §3's Mode A topic extraction pass: right after a fresh
upload lands as Primary Source node(s), a single generic_worker call
(role "source_manager") reads that source's own sections back and
proposes a topic tree for it, written into Secondary Data
(eo/secondary_data.py) via apply_patch()'s "add" ops. Deliberately
scoped to THIS source alone, not the whole workspace graph -- fitting
one new source's topics into the EXISTING workspace tree (parent
matches across sources, cross-source connections) is Backlink
Detector's incremental-patch job (§4, a later step), not this one.

This patch (2d) adds parallel fan-out for large uploads: a source with
more than MODE_A_CHUNK_SIZE sections (a long PDF, a big import) splits
into contiguous chunks, each run as its own worker via
eo/worker_pool.py's shared _select_workers("source_manager", ...) --
same quota-aware, fairness-ranked selection agents/code_writers.py and
agents/content_adapter_pool.py already use for their own pools, not a
second copy. Each chunk resolves its own topics' "parent" references
locally (same single-source scoping §2c already keeps -- a chunk is
still a slice of ONE source, not a cross-source merge); the only
"merge logic" this needs is concatenating every chunk's already-
resolved ops into one apply_patch() call, so the whole source's Mode A
write stays atomic regardless of how many chunks it took.

A source at or under MODE_A_CHUNK_SIZE sections is completely
unaffected -- same single generic_worker call §2c already added.

This patch (3a) wires process_upload() to call agents/backlink_detector.py's
new run_after_source_manager() the moment Mode A's topic extraction pass
finishes -- trigger boundary only. Backlink Detector's own incremental-
patch logic (matching this source's new topic_ids against the rest of
the workspace's Secondary Data tree and emitting the connection ops)
is still a no-op stub pending §3b, a later, separate step.

Still NOT yet doing (later patches):
  - Backlink Detector's actual incremental-patch generation (§3b) and
    deletion cleanup (§3c)
  - accounts' natural_roles tagging for "source_manager" in
    AGENT_CAPABILITIES (§4c). Unlike §2c's sequential path (which falls
    through to the full account pool via eo/panel.py's _best_match()
    when no natural-roles match exists), THIS patch's parallel path
    calls eo/worker_pool.py's _select_workers(), which -- same as every
    existing caller of it (agents/code_writers.py, agents/
    content_adapter_pool.py) -- raises RuntimeError on an empty tagged
    pool rather than falling through. That's consistent with this
    codebase's existing parallel-pool behavior, not a new gap this
    patch introduces, but it does mean the >MODE_A_CHUNK_SIZE-sections
    path stays inert (raises, caught, logged, degrades to no topics for
    that source) until §4c's tagging lands. Every existing precedent
    (content_adapter_pool.py's "content_writer" tag) lands its
    AGENT_CAPABILITIES tag in the SAME patch as the pool itself; this
    plan's own step ordering defers it instead, so this one path is
    genuinely dead code until §4c -- flagging this explicitly rather
    than quietly working around the plan's stated ordering.

Place this file at: agents/source_manager.py
"""

import os
import sys
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_ingestor import write_ingested_source
from agents.pdf_ingestor import ingest_pdf
from agents.importer import import_artifact
from agents.voice_ingestor import ingest_voice
from agents.video_ingestor import ingest_video
from agents.web_clipper import clip_url
from agents.backlink_detector import run_after_source_manager
from eo.registry import get_role_prompt, add_role_prompt
from eo.secondary_data import apply_patch, CONTENT_HINTS
from eo.worker_pool import _select_workers
from relay.emitter import emit_event
from utils.llm_client import generate_text

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Same per-source cap agents/concept_linker.py uses, same reasoning: keep
# one long section from crowding the rest of this call's context window.
MODE_A_MAX_CONTENT_CHARS = 6000

# §2d: sections-per-chunk threshold that decides sequential (§2c, one
# generic_worker call) vs. parallel (§2d, one worker per chunk) below.
# Chosen so a typical chunk's combined content stays comfortably inside
# one call's context window even at MODE_A_MAX_CONTENT_CHARS per
# section (8 * 6000 = 48,000 chars worst case, well under every
# provider's context limit here) while still being small enough that a
# genuinely huge upload (50+ page PDF) actually fans out into several
# parallel workers instead of one or two.
MODE_A_CHUNK_SIZE = 8

# Hard cap on concurrent chunk workers for one upload, same "resource-
# considerate ceiling" role code_writers.py's/content_adapter_pool.py's
# own `expanded` cap plays (8 there too) -- process_upload() has no
# per-call expanded flag to plumb through, so this is just the fixed
# ceiling rather than a 5-vs-8 choice.
MODE_A_MAX_PARALLEL_WORKERS = 8

# Falls back to "conceptual" for a missing/invalid content_hint rather
# than dropping the topic -- an imperfect hint is still more useful
# downstream (§6's retrieval agents) than silently losing the topic.
_DEFAULT_CONTENT_HINT = "conceptual"

SOURCE_MANAGER_TOPIC_BRIEF = (
    "You are extracting a topic tree from part of ONE freshly-ingested "
    "source, given below as excerpts, each labeled with a bracketed id "
    "like [a1b2c3d4e5f6]. Identify the distinct topics this excerpt "
    "set actually covers -- a longer or more varied excerpt set may "
    "have several topics forming a shallow hierarchy (a topic and its "
    "subtopics); a short or single-idea one usually has just one. For "
    "each topic, write:\n"
    "- \"name\": a short topic name (a few words)\n"
    "- \"summary\": 1-2 sentences on what this topic actually covers\n"
    "- \"parent\": the exact \"name\" of ANOTHER topic in this same list "
    "that this one is a subtopic of, or null if it's top-level. Don't "
    "invent hierarchy that isn't really there -- most sources are fine "
    "with every topic top-level (parent: null).\n"
    "- \"source_section_ids\": a JSON array of the bracketed ids (as "
    "plain strings, no brackets) whose content this topic actually "
    "draws from\n"
    "- \"content_hint\": exactly one of \"procedural\", \"conceptual\", "
    "\"data-heavy\", or \"narrative\" -- whichever best fits this "
    "topic's content shape\n\n"
    "Output a single fenced ```json code block containing an object "
    "with exactly one key, \"topics\", a JSON array of these topic "
    "objects -- nothing else outside that code block. Never force an "
    "artificial hierarchy or a topic split the excerpts don't actually "
    "support. Judge only what's actually in the excerpts below -- you "
    "may be seeing one piece of a larger source, not the whole thing."
)

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


def _ensure_role_registered() -> None:
    # Defensive, same reasoning agents/concept_linker.py's own
    # _ensure_role_registered() gives: ROLE_PROMPTS_SEED only bootstraps
    # a brand-new deployment's memory bus, so an already-running
    # deployment that predates this patch still needs this role's brief
    # written the first time it's actually hired.
    if not get_role_prompt("source_manager"):
        add_role_prompt("source_manager", SOURCE_MANAGER_TOPIC_BRIEF, source="source_manager_seed")


def _zipped_sections(artifact: dict, node_ids: list[str]) -> list[tuple[str, dict]]:
    """Rebuilds the same non-empty-content section filter
    write_ingested_source() applies internally, then zips it against
    the node_ids that function returned, so each excerpt can be tagged
    with the real node_id that now holds it. Shared by both the
    sequential (§2c) and chunked/parallel (§2d) paths below -- exactly
    one place holds this alignment logic.

    Best-effort, not exact: write_ingested_source() only appends a
    node_id when write_node() actually succeeds, so a write failure
    partway through would shift this zip out of alignment for
    everything after it. That's a rare, already-degraded case (a
    section that failed to write isn't in the graph to reference
    anyway) -- zip() below just means this pass sees a prefix of the
    real sections rather than misattributing content to the wrong
    node_id.

    Returns a list of (node_id, section_dict) pairs, in document order.
    """
    sections = [s for s in artifact.get("sections", []) if (s.get("content") or "").strip()]
    return [(node_id, s) for s, node_id in zip(sections, node_ids) if node_id]


def _build_context(pairs: list[tuple[str, dict]], title: str) -> tuple[str, dict]:
    """Turns a list of (node_id, section) pairs -- the whole source
    (§2c) or one chunk of it (§2d) -- into one bracketed-id-tagged
    context string plus the {node_id: section} map that validates the
    LLM's own source_section_ids answer against. Returns ("", {}) for
    an empty `pairs`.
    """
    parts = []
    id_map = {}
    for node_id, section_data in pairs:
        heading = section_data.get("heading") or title
        content = section_data["content"].strip()[:MODE_A_MAX_CONTENT_CHARS]
        id_map[node_id] = section_data
        parts.append(f"--- [{node_id}] {heading} ---\n{content}")
    return "\n\n".join(parts), id_map


def _chunk_pairs(pairs: list[tuple[str, dict]], chunk_size: int) -> list[list[tuple[str, dict]]]:
    """Splits `pairs` into contiguous, document-order chunks of at most
    `chunk_size` each -- contiguous rather than any other grouping so a
    topic that spans a few adjacent sections (the common case: a
    document's ideas run in reading order) has a real chance of landing
    in the same chunk instead of being split across two workers that
    never see each other's excerpts."""
    return [pairs[i:i + chunk_size] for i in range(0, len(pairs), chunk_size)]


def _parse_mode_a_topics(raw: str, valid_section_ids: set) -> list[dict]:
    """Parses the fenced ```json block generic_worker's (or, for §2d's
    parallel path, a chunk worker's) output should contain into a
    validated list of topic dicts ready to write. Anything malformed
    (missing name, section ids outside this call's own id_map, an
    invalid content_hint) is dropped or repaired rather than raising --
    same "degrade, don't break ingestion" posture process_upload()
    already keeps for a failed write_node() call.
    """
    match = _JSON_BLOCK_RE.search(raw or "")
    if not match:
        return []
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    raw_topics = parsed.get("topics")
    if not isinstance(raw_topics, list):
        return []

    topics = []
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue  # no usable identity for this topic, or for anything naming it as a parent
        summary = (item.get("summary") or "").strip()
        content_hint = item.get("content_hint")
        if content_hint not in CONTENT_HINTS:
            content_hint = _DEFAULT_CONTENT_HINT
        section_ids = [
            sid for sid in (item.get("source_section_ids") or [])
            if isinstance(sid, str) and sid in valid_section_ids
        ]
        parent_name = item.get("parent")
        parent_name = parent_name.strip() if isinstance(parent_name, str) and parent_name.strip() else None
        topics.append({
            "name": name, "summary": summary, "parent_name": parent_name,
            "source_section_ids": section_ids, "content_hint": content_hint,
        })
    return topics


def _topics_to_ops(topics: list[dict]) -> tuple[list[dict], list[str]]:
    """Turns one call's parsed topics into ready-to-apply Secondary
    Data "add" ops plus the new topic_ids, resolving each topic's
    "parent_name" against every OTHER topic in this SAME `topics` list
    only -- never an existing Secondary Data topic from an earlier
    source or an earlier chunk, per this module's single-source (§2c)
    / single-chunk (§2d) scoping. Two passes: ids first (a parent
    reference needs every topic's real id to already exist before any
    topic's own "add" op is built), then resolve.

    A parent name that doesn't match another topic in this same batch
    (the LLM referenced something that isn't there) falls back to
    top-level rather than dropping the topic.
    """
    name_to_id = {t["name"].strip().lower(): str(uuid.uuid4()) for t in topics}
    ops = []
    topic_ids = []
    for t in topics:
        topic_id = name_to_id[t["name"].strip().lower()]
        parent_id = name_to_id.get(t["parent_name"].strip().lower()) if t["parent_name"] else None
        ops.append({
            "op": "add", "path": f"/topics/{topic_id}",
            "value": {
                "name": t["name"], "summary": t["summary"], "parent": parent_id,
                "source_section_ids": t["source_section_ids"],
                "content_hint": t["content_hint"],
            },
        })
        topic_ids.append(topic_id)
    return ops, topic_ids


def _run_sequential_pass(context: str, id_map: dict, session_id: str = None) -> tuple[list[dict], list[str]]:
    """§2c's original path, unchanged in behavior: one generic_worker
    call (role "source_manager") over the WHOLE source's context.
    Raises on failure -- the caller (_run_mode_a_topic_extraction)
    catches it, same as before this refactor.
    """
    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred -- same
                                                          # circular-import
                                                          # reason as
                                                          # agents/concept_linker.py

    task_text = (
        "Extract this newly-ingested source's topic tree, per your "
        "instructions.\n\n" + context
    )
    result = run_role(
        role="source_manager", task_text=task_text, input_keys=[],
        session_id=session_id, include_conversation_context=False,
        domain="notes",
    )
    topics = _parse_mode_a_topics(result.get("text") or "", set(id_map.keys()))
    return _topics_to_ops(topics)


def _run_chunk_worker(context: str, id_map: dict, key_env: str, worker_id: int,
                       session_id: str = None, domain: str = None) -> tuple[list[dict], list[str]]:
    """§2d: one chunk's worker. Runs on its own thread with one fixed,
    pre-selected account -- direct generate_text(), not
    generic_worker.run(), same reasoning agents/content_adapter_pool.py
    and agents/extraction_table_builder.py's own per-worker functions
    give: generic_worker.run()'s own multi-step fallback-chain building
    is for the sequential default path, not a pool where each worker
    already has ITS OWN specific account assigned by
    eo/worker_pool.py's fairness ranking.

    Never raises outward: a transient failure on this one chunk's
    account (RuntimeError from generate_text, a malformed response)
    degrades to ([], []) for this chunk only, same "don't let one
    slice's hiccup break the whole upload" posture as everywhere else
    in this module -- the other chunks' workers are unaffected.
    """
    from agents.generic_worker import _chain_step_for   # deferred -- same
                                                          # circular-import
                                                          # reason as above

    agent_name = f"source_manager_chunk_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name,
               payload={"label": f"Source Manager Chunk {worker_id} — topic extraction"})
    started = time.monotonic()

    def _done(ops: list[dict], topic_ids: list[str]) -> tuple[list[dict], list[str]]:
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name,
                   payload={"summary": f"{len(topic_ids)} topic(s)", "duration_ms": duration_ms})
        return ops, topic_ids

    try:
        raw = generate_text(
            SOURCE_MANAGER_TOPIC_BRIEF, context, [_chain_step_for(key_env)],
            agent_name=agent_name, session_id=session_id, domain=domain,
        )
    except RuntimeError as exc:
        print(f"  [Source Manager] Mode A chunk {worker_id} skipped: {exc}")
        return _done([], [])

    topics = _parse_mode_a_topics(raw, set(id_map.keys()))
    ops, topic_ids = _topics_to_ops(topics)
    return _done(ops, topic_ids)


def _run_parallel_passes(chunks: list[list[tuple[str, dict]]], title: str,
                          session_id: str = None, key_override=None) -> tuple[list[dict], list[str]]:
    """§2d: fans `chunks` out across eo/worker_pool.py-selected workers
    via ThreadPoolExecutor, same shape agents/code_writers.py and
    agents/content_adapter_pool.py already use for their own pools --
    round-robins chunks over the selected accounts when there are more
    chunks than workers, same as those two.

    Raises RuntimeError if _select_workers() can't fill the pool at all
    (see this module's own docstring on why that's the correct,
    consistent-with-precedent behavior rather than a silent fallback)
    -- the caller (_run_mode_a_topic_extraction) catches it exactly the
    same way it catches _run_sequential_pass()'s failures.
    """
    worker_count = min(len(chunks), MODE_A_MAX_PARALLEL_WORKERS)
    key_envs = _select_workers("source_manager", worker_count, key_override)

    contexts = [_build_context(chunk, title) for chunk in chunks]

    all_ops = []
    all_topic_ids = []
    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _run_chunk_worker, context, id_map, key_envs[i % len(key_envs)],
                (i % len(key_envs)) + 1, session_id=session_id, domain="notes",
            ): i
            for i, (context, id_map) in enumerate(contexts) if context
        }
        for future in as_completed(futures):
            ops, topic_ids = future.result()
            all_ops.extend(ops)
            all_topic_ids.extend(topic_ids)
    return all_ops, all_topic_ids


def _run_mode_a_topic_extraction(artifact: dict, node_ids: list[str],
                                  workspace_id: str, session_id: str = None,
                                  key_override=None) -> list[str]:
    """§3's Mode A pass: proposes a topic tree for the source that was
    JUST written (`node_ids`, from write_ingested_source() a moment
    earlier in process_upload()), scoped to that source alone -- see
    this module's docstring on why fitting it into the rest of the
    workspace's tree is Backlink Detector's job, not this one.

    §2c's single generic_worker call for a source at or under
    MODE_A_CHUNK_SIZE sections; §2d's parallel per-chunk fan-out above
    that. Either way, the resulting ops are written in exactly ONE
    apply_patch() call, so a whole source's Mode A write is atomic
    regardless of how many chunks or workers it took.

    Never raises: any failure along the way (no usable context, a bad
    or missing LLM response, every account exhausted) is caught and
    logged, and this returns [] -- an ingestion succeeding shouldn't
    fail, or even error-log confusingly, just because the topic
    extraction pass on top of it couldn't run. Returns the list of new
    topic_ids actually written to Secondary Data (may be empty even on
    a technical "success" if the source had no extractable topics).
    """
    pairs = _zipped_sections(artifact, node_ids)
    if not pairs:
        return []
    title = artifact.get("title", "Untitled")

    try:
        if len(pairs) <= MODE_A_CHUNK_SIZE:
            context, id_map = _build_context(pairs, title)
            ops, topic_ids = _run_sequential_pass(context, id_map, session_id=session_id)
        else:
            chunks = _chunk_pairs(pairs, MODE_A_CHUNK_SIZE)
            ops, topic_ids = _run_parallel_passes(
                chunks, title, session_id=session_id, key_override=key_override,
            )
    except Exception as exc:
        print(f"  [Source Manager] Mode A topic extraction skipped: {exc}")
        return []

    if not ops:
        return []

    try:
        apply_patch(workspace_id, ops)
    except ValueError as exc:
        print(f"  [Source Manager] Mode A Secondary Data write failed: {exc}")
        return []
    return topic_ids


def process_upload(kind: str, payload: str, workspace_id: str,
                    session_id: str = None, created_by: str = "user",
                    section: str = "notes", mode_a_key_override=None,
                    **ingest_kwargs) -> dict:
    """The one entry point every upload -- from any tab, not just
    Notebooks -- is meant to funnel through (§1, §9). Picks the
    matching ingestion skill by `kind`, runs it on `payload` (a local
    file path or a url, per PAYLOAD_KIND above), writes the result as a
    Primary Source node exactly the way every existing upload endpoint
    already does by hand, then runs §3's Mode A topic extraction pass
    on that same source (§2c sequential, or §2d's parallel chunk fan-out
    for a large source).

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
    inspect or validate it, just passes it along. Also forwarded to the
    Mode A pass as this call's session context.

    mode_a_key_override (§2d): forwarded to eo/worker_pool.py's
    _select_workers() ONLY when the source is large enough to take the
    parallel path -- same three-shape contract as agents/
    code_writers.py's own key_override (None -> fairness-ranked
    self-selection, a single key_env string -> use only that account,
    a list -> use exactly those accounts as the pool). No effect on a
    source small enough to stay on §2c's sequential path, which picks
    its own account the normal generic_worker way.

    Returns {"node_ids": [...], "title": str, "kind": str,
    "topic_ids": [...]} -- the same {"node_ids", "title"} shape every
    existing upload endpoint already returns today, plus `kind` (since
    this one function now serves all five where each endpoint used to
    only need to report its own) and `topic_ids` (§3's new topics, may
    be empty -- see _run_mode_a_topic_extraction()'s docstring on why
    that's never itself a failure of this call).
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
    topic_ids = _run_mode_a_topic_extraction(
        artifact, node_ids, workspace_id, session_id=session_id,
        key_override=mode_a_key_override,
    )
    # §3a: Backlink Detector's trigger boundary -- fires right after this
    # source's own Mode A pass, whether or not it found anything (the
    # empty-topic_ids no-op lives in run_after_source_manager() itself,
    # not here, so process_upload() doesn't need its own duplicate
    # guard). Never allowed to fail this call -- see
    # run_after_source_manager()'s own "never raises" docstring note.
    run_after_source_manager(workspace_id, topic_ids, session_id=session_id)

    # NEW — Data Layer architecture §9a: notify() boundary, fired once
    # this whole call (ingest + Mode A + the Backlink Detector trigger
    # above) is actually done -- the point where a chat session's
    # upload affordance (§4b) would have something new to show.
    from eo.notify import notify  # deferred, same reasoning
                                    # relay/emitter.py's own import
                                    # gets elsewhere in this module --
                                    # no hard dependency for callers
                                    # that never pass a session_id
    notify(session_id, "upload_processed", {
        "workspace_id": workspace_id, "node_ids": node_ids,
        "title": artifact.get("title", "Untitled"), "topic_ids": topic_ids,
    })

    return {
        "node_ids": node_ids, "title": artifact.get("title", "Untitled"),
        "kind": kind, "topic_ids": topic_ids,
    }
