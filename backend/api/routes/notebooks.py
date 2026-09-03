"""
api/routes/notebooks.py

B6, piece 7 — the Notebooks "Generate" command and its whole target
roster (clusters, facts, suggested/topic notes, the three study
targets, mind map, suggested route, backlinks, workflows, podcast,
video overview, presentation rehearsal), classify-intent, per-topic
workflow, data tables, the Test tab's "simulate" domain, the entire
/api/notes/* family (capture, generate, podcast synthesis, video
overview build, study/quiz tools), and /api/capabilities. Pulled out
of api/server.py verbatim (same functions, same error handling, same
docstrings) — nothing here changes behavior, this is a pure move.

This is by far the largest piece — NotebooksTab.jsx's own 3,162 lines
are the deepest tab in the app, and this file is the backend half of
that surface area.

NOTES_EXPORTS_DIR is redefined here rather than imported from
api/server.py, same "duplicated to avoid a circular import back into
server.py" reasoning api/routes/workspace_data.py's own module
docstring already gives for its copy — this is now the third copy of
that same one-line join() (server.py itself no longer needs its own,
since every route that read it lived in this file).

_parse_marketplace_reviews (used only by /api/workspaces/{ws_id}/
simulate below) moves here with it — api/server.py's own comment
about it staying behind was written when piece 7 hadn't moved yet;
now that /simulate has a new home, so does its one caller's helper.

Deliberately NOT included: everything piece 6 already took (graph
edges, node summaries, topics/graph, nodes, note candidates,
backlinks/detect, clusters propose/candidates/accept/reject) and
everything still in api/server.py (the websocket endpoint, and the
deploy/monitoring block reserved for piece 8).
"""
import json
import os
import re
import tempfile
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agents.concept_linker import link_concepts
from agents.exporter import SUPPORTED_FORMATS as EXPORTABLE_FORMATS
from agents.exporter import export_artifact
from agents.fact_detector import detect_facts
from agents.importer import SUPPORTED_FORMATS as IMPORTABLE_FORMATS
from agents.mind_mapper import generate_mindmap, generate_suggested_route
from agents.note_clusterer import propose_clusters
from agents.note_table_builder import build_table
from agents.podcast_scriptwriter import generate_podcast_script
from agents.rehearsal_scriptwriter import generate_rehearsal_script
from agents.slide_deck_planner import generate_slide_deck
from agents.source_manager import process_upload
from agents.study_generator import generate_study_content
from agents.tts_synthesizer import synthesize_podcast
from agents.video_overview_builder import build_video_overview
from agents.workflow_suggester import build_topic_workflow, suggest_workflows
from api.deps import _resolve_chat_or_404, require_auth
from eo import (
    chat_store,
    chat_workspace,
    panel_content,
    quiz_progress,
    study_progress,
    workspace_facts,
)
from eo.mcp_agent_tools import mcp_tools_for_agent  # NEW — Patch A3
from eo.structure import STRUCTURE_TEMPLATES
from graph.adapters import markdown_text_to_artifact
from memory.bus import read_many as bus_read_many
from memory.bus import set_app_slug
from utils.capability_tools import manifest_to_tools, study_progress_tools
from utils.llm_client import classify_tool_intent

router = APIRouter()

# Part 4 §4.4 -- where generated reports/decks/scripts land before being
# handed back as a download. Sibling to eo/graph_edges.py's data/graph/
# and eo/chat_workspace.py's data/chats/ -- same "small dedicated
# subfolder under data/" convention this codebase already uses throughout.
NOTES_EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "exports",
)

# Bug fix (audit follow-up, gap 3): _generate_video_overview() below (and
# _generate_slide_deck()) can source slide_text/script_text from a PASTED
# value or a PREVIOUSLY SAVED panel row, not just a fresh generic_worker
# run -- and generic_worker.run()'s own format validation/retry (see
# agents/generic_worker.py's DIALOGUE_LABEL_ROLES) only ever runs on a
# FRESH generation. A pasted value the user typed by hand, or a row saved
# before that validation existed (or saved from a run that predates this
# fix), reaches synthesize_podcast()/markdown_text_to_artifact() completely
# unchecked -- the exact same "no speaker-labeled dialogue lines" /
# "no '## ' headings" failure, just from a different entry point the
# generation-time fix can't see. These two checks close that gap at the
# one place both entry points (fresh generation and reuse) actually
# converge: right before the text is trusted.
def _is_usable_dialogue_script(text: str) -> bool:
    """True if `text` reuses at least one speaker label across two or
    more lines. Deferred import, same circular-import posture this
    module already uses elsewhere, and reuses the real downstream
    checker rather than a second hand-rolled regex -- see
    agents/generic_worker.py's near-identical helper.

    BUGFIX (rehearsal validation gap): this used to accept "at least one"
    recognized speaker line -- a plain Markdown report with even one
    bolded "**Word:** ..." line (e.g. "**VERDICT:** The material is
    excellent...") satisfies that trivially, which is exactly how a
    report generated for the Rehearsal tab slipped past this gate and
    got silently mis-synthesized as if it were a real script. Requiring a
    label to repeat across multiple lines is what actually distinguishes
    dialogue from a report; see tts_synthesizer.has_repeating_speaker_
    labels()'s own docstring for the full reasoning."""
    from agents.tts_synthesizer import has_repeating_speaker_labels
    return has_repeating_speaker_labels(text or "")


def _is_usable_rehearsal_script(text: str) -> bool:
    """Stricter than _is_usable_dialogue_script() above, for
    presentation_rehearsal specifically: also requires at least one
    '[PAUSE:N]' marker, matching rehearsal_scriptwriter's own brief
    ("never skip the pause-then-model-answer sequence") -- an ordinary
    report never happens to contain that bracket syntax, so this is a
    second, independent signal beyond the repeating-label check. Used as
    a defensive check right after generation (agents/generic_worker.py's
    STRUCTURAL_FORMAT_CHECKS already retries once at the source for this
    same failure mode, but that's a separate module reached through a
    different code path -- checking again here, right before the text is
    trusted and synthesized to audio, costs nothing and catches it even
    if that retry is ever bypassed or changed)."""
    from agents.tts_synthesizer import has_repeating_speaker_labels, has_pause_markers
    text = text or ""
    return has_repeating_speaker_labels(text) and has_pause_markers(text)


def _is_usable_slide_deck(text: str) -> bool:
    """True if `text` parses into at least one real '## '-headed slide
    section via the exact same parser markdown_text_to_artifact() (and
    therefore build_video_overview()) will use on it later -- not just a
    non-empty string. A pasted or saved value with no real headings at
    all (plain prose, a bare title, an empty paste) parses to zero
    sections and would otherwise reach video_overview_builder.py as a
    deck with nothing to render."""
    from agents.importer import parse_markdown_text
    if not (text or "").strip():
        return False
    return bool(parse_markdown_text(text, default_title="").get("sections"))


class ClipUrlRequest(BaseModel):
    url: str
    workspace_id: str
    # NEW — Data Layer architecture §9c: optional so this model's shape
    # doesn't break for any caller that predates this (there weren't
    # any -- both endpoints below are this model's only two users --
    # but Optional is also just correct: a clip/video ingest kicked off
    # from a context with no chat session open has nothing real to pass
    # here, and process_upload()'s own session_id already defaults to
    # None for exactly that case). Threaded straight through to
    # process_upload() below so eo/notify.py's "upload_processed" push
    # (§9a/§9b) actually has a session_id to fire on -- until this
    # patch, none of the six ingestion endpoints passed one, so §9b's
    # new /ws/{session_id} transport had nothing to deliver for uploads.
    session_id: str | None = None


class ExportArtifactRequest(BaseModel):
    text: str                    # a generator role's raw Markdown stage_output
    title: str = "Untitled"
    fmt: str                     # one of agents/exporter.py's SUPPORTED_FORMATS
    workspace_id: str | None = None
    tags: list[str] | None = None


class BuildTableRequest(BaseModel):
    field_names: list[str]
    node_type: str | None = None
    expanded: bool = False


class SimulateRequest(BaseModel):
    session_id: str


class SynthesizePodcastRequest(BaseModel):
    script_text: str             # podcast_scriptwriter's raw Markdown stage_output
    title: str = "podcast"

class RecordQuizAttemptRequest(BaseModel):
    workspace_id: str
    quiz_node_id: str            # vector_id of the exported/stored quiz node
    quiz_text: str                # quiz_writer's raw Markdown stage_output
    answers: list[int]            # one option-index per question, in question order
    # NEW — step 6.7: which topic (study_progress.py's topic_key, same
    # value build_topic_workflow() returns/keys "topic_workflows" by)
    # this attempt counts toward, so a passing score can bump that
    # topic's progress record. Optional and best-effort on purpose —
    # quiz_progress.py itself has no topic concept (a quiz_node_id
    # carries no topic linkage anywhere in the store), so this only
    # works once a caller (e.g. a topic-scoped quiz launched from the
    # workflow/study board) actually knows and sends it. An attempt
    # with no topic_id still grades and records exactly as before;
    # it just can't drive study_progress.
    topic_id: str | None = None


class GradeQuizRequest(BaseModel):
    quiz_text: str                # quiz_writer's raw Markdown stage_output
    answers: list[int]

class BuildVideoOverviewRequest(BaseModel):
    slide_text: str               # slide_planner's raw Markdown stage_output
    podcast_title: str            # the `title` used in a prior POST
                                   # /api/notes/podcast/synthesize call for
                                   # this notebook -- locates that mp3 on
                                   # disk rather than re-synthesizing it
    title: str = "video_overview"




# --- Notebooks "Generate" command (Notebooks integration guide §4, §6) ----
# Chat's free-text/picker flow parses down to one structured intent --
# {targets: [...], scope: {...}} -- and hits this single endpoint rather
# than each target having its own bespoke chat-trigger route. The response
# is a per-target result list, not one shared payload: guide §5 has the
# Working Panel show one branch per target, each with its own
# Generating/Done/Error state, so a multi-target command that partially
# fails still reports whichever targets succeeded instead of failing the
# whole request.
#
# All six of Phase 1's targets plus Phase 2's Mind Map, Phase 3's
# Backlinks concept graph, and bug audit §7's suggested Workflows are
# wired now: Clusters, Facts, Suggested Notes, Flashcards, Quiz, Study
# Guide, Mind Map, Backlinks, and Workflows.
# `scope` is accepted and shape-checked here so the request contract
# didn't have to change as targets were added one patch at a time --
# Facts, the three Study targets, Mind Map, Backlinks, and Workflows are
# the ones that actually read it (an optional `source_node_ids` list;
# blank/omitted means "whole notebook," same convention guide §4.2 uses
# for scope elsewhere). Backlinks additionally supports a `force` scope
# flag (see _generate_backlinks below) to bypass its regeneration-check
# skip, for testing/debugging -- not exposed in the picker UI; guide
# §6.6's skip-if-nothing-new behavior is meant to be the default,
# silent path.
#
# Each target function takes (ws_id, scope, owner_id). Clusters and Facts
# ignore owner_id -- they're workspace-scoped, same as
# detect_backlinks() above. Suggested Notes needs it: its underlying
# call, agents/note_taker.py's scan_conversation(), is scoped to one
# CHAT (session_id), not a workspace, so _most_recently_active_chat(ws_id,
# owner_id) below picks the workspace's most recently updated chat as
# "the" conversation a workspace-level Generate command means -- same as
# opening the notebook and continuing wherever you left off. A workspace
# with no chats yet returns a "done" branch with a null candidate rather
# than an error -- "nothing to scan" is a valid, unsurprising result of
# pressing Generate on a brand new notebook, not a failure. The three
# Study targets need owner_id too, but only to pass through to
# panel_content.set_content()'s updated_by field -- same "just the
# caller changes, not the store" reasoning as guide §6.5's Mind Map note.

def _most_recently_active_chat_id(ws_id: str, owner_id: str) -> str | None:
    ws = chat_workspace.get_workspace(ws_id, owner_id)
    chats = []
    for chat_id in ws.get("chat_ids") or []:
        try:
            chats.append(chat_store.get_chat(chat_id, owner_id))
        except Exception:
            continue   # a chat this user can no longer access -- skip it, don't fail the whole scan
    if not chats:
        return None
    chats.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return chats[0]["id"]


def _generate_clusters(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    return {"candidates": propose_clusters(ws_id)}


def _generate_facts(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    source_node_ids = (scope or {}).get("source_node_ids")
    return {"candidates": detect_facts(ws_id, source_node_ids)}


def _generate_suggested_notes(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    chat_id = _most_recently_active_chat_id(ws_id, owner_id)
    if not chat_id:
        return {"candidate": None, "note": "no chats in this workspace yet"}
    from agents.note_taker import scan_conversation  # deferred, same
                                                        # circular-import
                                                        # reason
                                                        # eo/conversation_memory.py's
                                                        # own note_taker
                                                        # import defers this
    return {"candidate": scan_conversation(chat_id, owner_id)}


def _generate_topic_notes(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """NEW — 2026-08-01 gap fix: "suggested_notes" above scans the chat
    transcript for decisions/action items and ignores `scope` entirely
    -- it was never actually a "write me a note on topic X" generator,
    despite its CAPABILITIES_MANIFEST description implying it was (see
    agents/topic_note_writer.py's module docstring for the full finding).
    This is the real thing: requires scope.topic_id, pulls that one
    topic's actual source content, and writes a single source-grounded
    note candidate through the same eo/note_candidates.py review queue.

    Raises ValueError (becomes a failed branch, same as every other
    target's validation error) if scope.topic_id is missing -- this
    target has no whole-notebook fallback, unlike facts/clusters/etc.,
    since "write a note about the whole notebook" isn't a coherent
    single note the way "write a note about this one topic" is.
    """
    topic_id = (scope or {}).get("topic_id")
    if not topic_id:
        raise ValueError("topic_notes requires scope.topic_id")
    from agents.topic_note_writer import generate_topic_note  # deferred,
                                                                  # same
                                                                  # circular-import
                                                                  # reason
                                                                  # every
                                                                  # other
                                                                  # target
                                                                  # function
                                                                  # here
                                                                  # already
                                                                  # gives
    try:
        candidate = generate_topic_note(ws_id, topic_id)
    except KeyError:
        raise ValueError(f"topic_id {topic_id!r} not found in this workspace")
    return {"candidate": candidate}


def _make_study_generate(panel_key: str):
    """Flashcards/Quiz/Study Guide are the one truly zero-new-storage
    case (guide §6.1): generate_study_content() returns raw Markdown,
    panel_content.set_content() saves it under a panel_key that's
    already allow-listed and already read by the Study subtab today --
    the exact same store a manual paste-and-Load writes to, just with
    the agent as the caller instead of the user's clipboard. One
    closure per panel_key rather than three near-identical functions.
    """
    def _run(ws_id: str, scope: dict | None, owner_id: str) -> dict:
        source_node_ids = (scope or {}).get("source_node_ids")
        content = generate_study_content(panel_key, ws_id, source_node_ids)
        # CHANGED — bug audit §2 real fix: record the scope this
        # generation actually used, so a later source delete can
        # invalidate just this panel instead of every panel in the
        # workspace. source_node_ids is already None for a whole-notebook
        # run, which is exactly the value GENERATED_PANEL_KEYS wants for
        # "invalidate on any delete" — no extra translation needed here.
        return panel_content.set_content(ws_id, panel_key, content, owner_id, source_node_ids=source_node_ids)
    return _run


def _generate_mindmap(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Phase 2 (guide §6.5). Same save target Mind Map's manual-paste
    path already wrote to (panel_key "mindmap") -- Regenerate is meant
    to be a last-write-wins overwrite, same as every other panel_content
    write, so no extra confirmation step belongs here; the guide's own
    §9 open question about warning-before-overwrite is a frontend
    concern for whenever the "Regenerate" button is built, not this
    endpoint's job.

    CHANGED — bug #6 fix: generate_mindmap() now returns a typed
    {"kind", "text"} result instead of always handing back something
    string-shaped. Only a "mermaid" result gets saved -- a "markdown"
    result (the model answered without a valid fence, even after
    mind_mapper.py's internal retry) raises instead of being written to
    panel_content, so a bad Regenerate attempt can't clobber a
    previously-good diagram with prose, and so this becomes a normal
    "error" branch that notebooks_generate already knows how to report
    (see that endpoint below) -- MindMapView's existing error handling
    (a plain message, no code dump) covers it with no frontend change
    needed here.
    """
    source_node_ids = (scope or {}).get("source_node_ids")
    result = generate_mindmap(ws_id, source_node_ids)
    if result["kind"] != "mermaid":
        raise RuntimeError("Couldn't generate a valid diagram from this notebook's sources — try Regenerate.")
    # CHANGED — bug audit §2 real fix: record scope, see _make_study_generate's comment above.
    return panel_content.set_content(ws_id, "mindmap", result["text"], owner_id, source_node_ids=source_node_ids)


def _generate_suggested_route(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Chat audit fix: agents/mind_mapper.py:generate_suggested_route()
    was fully implemented (Data Layer architecture §7c) but had no
    caller anywhere in the codebase — not this endpoint, not the
    frontend. This is that missing wiring.

    Deterministic, no LLM call: walks agents/backlink_detector.py's own
    "prerequisite-of" edges between topics and renders them as a
    flowchart TD showing which topic to study before which — the
    cross-notebook "what order do I work through this in" view, as
    opposed to "mindmap"'s single topic-overview diagram. Saved under
    its own panel_key ("suggested_route", added to
    eo/panel_content.py's VALID_PANEL_KEYS/GENERATED_PANEL_KEYS)
    so regenerating one never overwrites the other.

    No source_node_ids scoping — generate_suggested_route() only takes
    a project/chat scope (see its own docstring): a prerequisite route
    is inherently about ordering across topics, so narrowing to a
    handful of sources isn't a meaningful operation here the way it is
    for mindmap/workflows/study.
    """
    try:
        result = generate_suggested_route(ws_id, scope="project")
    except LookupError:
        raise RuntimeError(
            "No prerequisite relationships between topics yet — this needs "
            "Backlinks to have found at least one real \"study X before Y\" "
            "connection first. Try running Backlinks, then Regenerate here."
        )
    return panel_content.set_content(ws_id, "suggested_route", result["text"], owner_id, source_node_ids=None)


def _generate_backlinks(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Phase 3 (guide section 6.6). Unlike every other target here,
    "done" isn't the only successful outcome: link_concepts() returns
    status "up_to_date" when nothing's changed since the last run and
    skips the LLM pass entirely (guide's regeneration rule) -- that's
    still a "done" branch from the Working Panel's point of view (guide
    section 5), not an error, so this passes the whole result straight
    through rather than collapsing it to a bool.

    scope["force"] = true bypasses the skip check (see comment above
    NOTEBOOKS_GENERATE_TARGETS) -- not reachable from the picker, only
    useful for manually re-running the pass while testing.
    """
    scope = scope or {}
    source_node_ids = scope.get("source_node_ids")
    force = bool(scope.get("force"))
    return link_concepts(ws_id, source_node_ids, force=force)


def _generate_workflows(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Bug audit §7 (new feature): agents/workflow_suggester.py finds
    0-4 procedures described in the notebook's sources and diagrams
    each one. An empty `workflows` list is a normal "done" result, not
    an error -- purely conceptual/descriptive source material
    genuinely has no procedure to show, same as Backlinks' "up to
    date" branch above being a non-error outcome. Stored as a JSON
    string under panel_content's "suggested_workflows" key, same
    encode-on-write/decode-on-read shape the frontend already handles
    for every other structured (non-plain-Markdown) panel.
    """
    source_node_ids = (scope or {}).get("source_node_ids")
    result = suggest_workflows(ws_id, source_node_ids)
    # CHANGED — bug audit §2 real fix: record scope, see _make_study_generate's comment above.
    return panel_content.set_content(ws_id, "suggested_workflows", json.dumps(result), owner_id,
                                      source_node_ids=source_node_ids)


def _generate_slide_deck(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Video Overview reuse patch, step 1. Same (ws_id, scope, owner_id)
    -> result shape every other NOTEBOOKS_GENERATE_TARGETS entry here
    already has -- gives agents/slide_deck_planner.py's slide_planner
    role a standalone Generate target of its own, the same way
    _generate_podcast() below already gives podcast_scriptwriter one,
    instead of that role only ever running as a hidden first stage
    inside _generate_video_overview().

    Two things this unlocks: (1) "make me a presentation on X" now
    resolves to real content on its own, with nothing narrated or
    rendered to video; (2) a saved "slide_deck" panel row becomes
    something _generate_video_overview() (see below) can check for and
    reuse, instead of unconditionally regenerating a fresh slide outline
    on every single video build.

    Raises LookupError on failure (no readable topic content in scope),
    same contract _generate_podcast()'s own generate_podcast_script()
    call already gives -- notebooks_generate()'s surrounding try/except
    turns that into a normal "error" branch the same way any other
    target's raise already does.

    FIX: this previously ignored scope["slide_text"] entirely and always
    regenerated from workspace sources, even when the caller (the
    Presentation tab's optional paste box) pasted an outline to reformat.
    _generate_video_overview() already reads this exact scope key
    correctly via generate_slide_deck()'s raw_context parameter -- this
    target was just missed when the paste-box scope contract was added.
    """
    scope = scope or {}
    source_node_ids = scope.get("source_node_ids")
    pasted_slide = (scope.get("slide_text") or "").strip()
    # Bug fix (audit follow-up, gap 3): a paste-box value that isn't a
    # real '## '-headed deck (pasted plain prose, an accidental partial
    # paste, ...) used to go straight to generate_slide_deck()'s
    # raw_context as if it were trustworthy slide content, then reach
    # video_overview_builder.py as a deck with nothing to render. Falling
    # through to a fresh, whole-notebook generation instead is exactly
    # today's existing "nothing pasted" behavior -- so an unusable paste
    # degrades to the same well-tested path a blank paste box already
    # takes, rather than a new failure mode.
    if pasted_slide and not _is_usable_slide_deck(pasted_slide):
        print(
            "  [notebooks:slide_deck] pasted slide_text has no usable "
            "'## ' headings -- ignoring the paste and generating fresh."
        )
        pasted_slide = ""
    if pasted_slide:
        slide_text = generate_slide_deck(ws_id, raw_context=pasted_slide)
    else:
        slide_text = generate_slide_deck(ws_id, source_node_ids)

    # NEW: persisted under its own "slide_deck" panel_key (added to
    # eo/panel_content.py's VALID_PANEL_KEYS/GENERATED_PANEL_KEYS
    # alongside "podcast") -- same encode-on-write JSON-string shape
    # _generate_podcast() below uses for "podcast", just the one
    # {"slide_text"} field since there's no on-disk artifact (mp3/mp4)
    # for a plain text outline the way there is for podcast/video_overview.
    saved = panel_content.set_content(
        ws_id,
        "slide_deck",
        json.dumps({"slide_text": slide_text}),
        owner_id,
        source_node_ids=source_node_ids,
    )

    return {
        "panel_key": "slide_deck",
        "status": "done",
        "slide_text": slide_text,
        "updated_at": saved["updated_at"],
    }


def _generate_podcast(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Phase 5 step 5.6. Same (ws_id, scope, owner_id) -> result shape
    every other NOTEBOOKS_GENERATE_TARGETS entry above already has --
    extracted out of notebooks_podcast() below (step 5.1's dedicated
    route) so this same generation logic is reachable both from that
    route directly AND from notebooks_generate()'s {"targets": [...]}
    dispatch, without duplicating it. Raises LookupError/ValueError on
    failure rather than an HTTPException -- notebooks_generate()'s own
    try/except (see that function above) turns any raise into an "error"
    branch the same way every other target's raise already does;
    notebooks_podcast() itself still translates the same two exceptions
    into a 400 for its own single-target callers.
    """
    source_node_ids = (scope or {}).get("source_node_ids")
    script_text = generate_podcast_script(ws_id, source_node_ids)

    audio_filename = f"podcast_{ws_id}.mp3"
    out_path = os.path.join(NOTES_EXPORTS_DIR, audio_filename)
    synthesize_podcast(script_text, out_path)

    # NEW — step 5.4: persist script_text + the (NOTES_EXPORTS_DIR-relative)
    # audio_filename via panel_content, under the "podcast" panel_key.
    # Same encode-on-write JSON-string shape _generate_workflows above
    # already uses for "suggested_workflows".
    saved = panel_content.set_content(
        ws_id,
        "podcast",
        json.dumps({"script_text": script_text, "audio_path": audio_filename}),
        owner_id,
        source_node_ids=source_node_ids,
    )

    return {
        "panel_key": "podcast",
        "status": "done",
        "script_text": script_text,
        "audio_bytes": os.path.getsize(out_path),
        "updated_at": saved["updated_at"],
        "message": (
            "Script + audio generated and saved (Phase 5 steps 5.2/5.3/5.4), "
            "reachable via Generate as of step 5.6. A servable download URL "
            f"(step 5.7+) isn't wired yet -- the mp3 exists on disk at {out_path!r} for now."
        ),
    }


def _load_saved_text(ws_id: str, panel_key: str, field: str) -> str:
    """Video Overview reuse patch, step 2. Best-effort read of one text
    field out of a JSON-encoded panel_content row -- returns "" for a
    panel with no row saved yet, or content that doesn't parse or
    doesn't have the field, so every caller can treat "no usable saved
    text" as one single falsy case instead of handling get_content()'s
    empty-row shape and a possible json.JSONDecodeError separately at
    each call site. Used for the "slide_deck" panel below; "podcast"
    needs its own sibling (_load_saved_podcast) since it also has to
    hand back a reusable audio_path, not just script_text.
    """
    row = panel_content.get_content(ws_id, panel_key)
    if not row["content"]:
        return ""
    try:
        data = json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        return ""
    return (data.get(field) or "").strip()


def _load_saved_podcast(ws_id: str) -> tuple[str, str | None]:
    """Video Overview reuse patch, step 2. Returns (script_text,
    audio_filename) from a saved "podcast" panel row, or ("", None) if
    there's nothing usable saved. audio_filename comes back as None
    (even when the saved row names one) unless that exact file still
    exists on disk under NOTES_EXPORTS_DIR -- panel_content and disk
    state could in principle drift (e.g. someone manually cleared
    NOTES_EXPORTS_DIR), and promising a caller a reusable audio path
    that then 404s inside build_video_overview() would be a worse
    failure mode than just re-synthesizing it.
    """
    row = panel_content.get_content(ws_id, "podcast")
    if not row["content"]:
        return "", None
    try:
        data = json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        return "", None
    script_text = (data.get("script_text") or "").strip()
    if not script_text:
        return "", None
    audio_filename = data.get("audio_path")
    if audio_filename and os.path.exists(os.path.join(NOTES_EXPORTS_DIR, audio_filename)):
        return script_text, audio_filename
    return script_text, None


def _load_saved_video_path(ws_id: str) -> str | None:
    """Frontend paste-box patch (patch 4), step 1. Returns the
    NOTES_EXPORTS_DIR-relative video filename saved under the
    "video_overview" panel, or None if there's no saved row, the row's
    content doesn't parse, or the file it names no longer exists on
    disk. Same "don't promise a path that then 404s" posture
    _load_saved_podcast() above already takes for "podcast"'s
    audio_path -- this is that same check, just for video_overview's
    video_path, and pulled out as its own helper (rather than inlined
    into notebooks_video_overview_video() below) so the existence check
    isn't duplicated between this and any future caller.
    """
    row = panel_content.get_content(ws_id, "video_overview")
    if not row["content"]:
        return None
    try:
        data = json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    video_filename = data.get("video_path")
    if video_filename and os.path.exists(os.path.join(NOTES_EXPORTS_DIR, video_filename)):
        return video_filename
    return None


def _load_saved_rehearsal(ws_id: str) -> tuple[str, str | None]:
    """Patch 5 (Presentation rehearsal reachability patch), step 1. Same
    shape and same "don't promise a path that then 404s" posture as
    _load_saved_podcast() above, just reading the "presentation_rehearsal"
    panel_key instead of "podcast" -- that panel's content is a
    {"script_text", "audio_path", "mode", "difficulty"} JSON string
    (see _generate_presentation_rehearsal()'s own docstring), so only
    the two fields this helper needs are pulled out of it.
    """
    row = panel_content.get_content(ws_id, "presentation_rehearsal")
    if not row["content"]:
        return "", None
    try:
        data = json.loads(row["content"])
    except (json.JSONDecodeError, TypeError):
        return "", None
    script_text = (data.get("script_text") or "").strip()
    if not script_text:
        return "", None
    audio_filename = data.get("audio_path")
    if audio_filename and os.path.exists(os.path.join(NOTES_EXPORTS_DIR, audio_filename)):
        return script_text, audio_filename
    return script_text, None


def _generate_video_overview(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Phase 5 step 5.6, rewritten by the Video Overview reuse patch,
    step 2. Raises LookupError/ValueError on failure, same contract as
    every other target function here -- see _generate_podcast()'s own
    docstring for why.

    Before this patch, this function unconditionally ran BOTH
    slide_deck_planner and podcast_scriptwriter fresh over the whole
    notebook every single time, ignoring anything already saved and
    giving a caller no way to hand it a script or slide deck it already
    has (a pasted script, a pasted slide outline, or a previous
    standalone "slide_deck"/"podcast" Generate run). That's the actual
    gap this rewrite closes:

      scope["slide_text"] / scope["script_text"] -- NEW. Either or both
      can be passed directly (e.g. a script or slide outline the user
      just pasted into chat) to use as-is instead of generating it.

      Otherwise, a previously saved "slide_deck" / "podcast" panel for
      this workspace is reused as-is -- including the podcast's
      already-synthesized audio file, when it still exists on disk, so
      a video overview built right after a standalone Podcast/
      Presentation generation costs zero additional LLM calls and no
      re-synthesis.

      Otherwise, if exactly one half is available (pasted or saved) and
      the other isn't, the missing half is generated FROM the available
      one (agents/podcast_scriptwriter.py / agents/slide_deck_planner.py's
      new `raw_context` parameter) rather than independently from
      unrelated whole-notebook source material -- so e.g. handing this
      only a slide deck gets narration that actually matches those
      slides, not a generic notebook-wide script.

      Only when NEITHER half is available does this fall back to
      today's original behavior: both generated independently, fresh,
      from the workspace's own sources.

    Whichever text ends up freshly generated or freshly pasted (i.e.
    NOT reused unchanged from an existing panel) is backfilled into its
    own standalone "slide_deck"/"podcast" panel too, so a video overview
    build also leaves behind independently reusable Presentation/Podcast
    content, not just the merged video.
    """
    scope = scope or {}
    source_node_ids = scope.get("source_node_ids")

    pasted_slide = (scope.get("slide_text") or "").strip()
    pasted_script = (scope.get("script_text") or "").strip()
    saved_slide = "" if pasted_slide else _load_saved_text(ws_id, "slide_deck", "slide_text")
    saved_script, saved_audio_filename = ("", None) if pasted_script else _load_saved_podcast(ws_id)

    # Bug fix (audit follow-up, gap 3): all four of these can reach this
    # point WITHOUT ever having passed through generic_worker.run()'s own
    # format validation/retry -- pasted_slide/pasted_script are typed by
    # hand, and saved_slide/saved_script/saved_audio_filename are read
    # straight from a previously saved panel row that may have been
    # written before this fix existed, or by some other path entirely.
    # Trusting any of them unchecked is exactly how a stale or malformed
    # value reaches synthesize_podcast()/video_overview_builder.py here
    # even after generation-time validation is airtight. Treat an
    # unusable one exactly like an absent one -- same "fall through to
    # deriving or generating fresh" behavior every branch below already
    # has for a genuinely missing half, just reached from one more input
    # shape. A saved value being discarded here also means its audio
    # can't be trusted either, so saved_audio_filename is cleared with it.
    if pasted_slide and not _is_usable_slide_deck(pasted_slide):
        print("  [notebooks:video_overview] pasted slide_text has no usable "
              "'## ' headings -- treating as not provided.")
        pasted_slide = ""
    if saved_slide and not _is_usable_slide_deck(saved_slide):
        print("  [notebooks:video_overview] saved slide_deck panel has no "
              "usable '## ' headings -- treating as not provided.")
        saved_slide = ""
    if pasted_script and not _is_usable_dialogue_script(pasted_script):
        print("  [notebooks:video_overview] pasted script_text has no "
              "speaker-labeled dialogue lines -- treating as not provided.")
        pasted_script = ""
    if saved_script and not _is_usable_dialogue_script(saved_script):
        print("  [notebooks:video_overview] saved podcast panel has no "
              "speaker-labeled dialogue lines -- treating as not provided "
              "(its audio is discarded too, since it narrates the same "
              "unusable script).")
        saved_script, saved_audio_filename = "", None

    have_slide = pasted_slide or saved_slide
    have_script = pasted_script or saved_script

    # Resolve slides: prefer what's already available; otherwise derive
    # from the script if there is one (keeps both halves telling the
    # same story); only fall all the way back to a fresh, whole-notebook
    # slide_planner run when neither exists yet.
    if have_slide:
        slide_text = have_slide
        slide_origin = "pasted" if pasted_slide else "existing"
    elif have_script:
        slide_text = generate_slide_deck(ws_id, source_node_ids, raw_context=have_script)
        slide_origin = "derived"
    else:
        slide_text = generate_slide_deck(ws_id, source_node_ids)
        slide_origin = "generated"

    # Resolve narration: same priority, deriving from slide_text (always
    # resolved to *something* by this point) whenever there wasn't
    # already a script to reuse. reusable_audio_filename only ever comes
    # back non-None for the "existing" origin -- every other origin is
    # new or changed text with no matching audio yet.
    if have_script:
        script_text = have_script
        script_origin = "pasted" if pasted_script else "existing"
        reusable_audio_filename = saved_audio_filename if script_origin == "existing" else None
    elif slide_origin in ("pasted", "existing"):
        script_text = generate_podcast_script(ws_id, source_node_ids, raw_context=slide_text)
        script_origin = "derived"
        reusable_audio_filename = None
    else:
        script_text = generate_podcast_script(ws_id, source_node_ids)
        script_origin = "generated"
        reusable_audio_filename = None

    if reusable_audio_filename:
        audio_filename = reusable_audio_filename
    else:
        audio_filename = f"video_overview_narration_{ws_id}.mp3"
        synthesize_podcast(script_text, os.path.join(NOTES_EXPORTS_DIR, audio_filename))
    audio_path = os.path.join(NOTES_EXPORTS_DIR, audio_filename)

    slide_artifact = markdown_text_to_artifact(slide_text, title_fallback="video_overview")
    video_filename = f"video_overview_{ws_id}.mp4"
    video_path = os.path.join(NOTES_EXPORTS_DIR, video_filename)
    build_video_overview(slide_artifact, audio_path, video_path)

    # NEW — step 5.4/5.5: persist slide_text + script_text + the
    # (NOTES_EXPORTS_DIR-relative) video_filename via panel_content,
    # under the "video_overview" panel_key. Same shape _generate_podcast()
    # above uses for its own "podcast" panel_key.
    saved = panel_content.set_content(
        ws_id,
        "video_overview",
        json.dumps({
            "slide_text": slide_text,
            "script_text": script_text,
            "video_path": video_filename,
        }),
        owner_id,
        source_node_ids=source_node_ids,
    )

    # NEW — Video Overview reuse patch, step 2: backfill the two
    # standalone panels with whatever wasn't already sitting there
    # unchanged, so this build leaves behind real, independently
    # reusable Presentation/Podcast content too -- not just the video.
    # Deliberately skipped for "existing" origin: that content is
    # already saved verbatim, and rewriting it would just bump
    # updated_at for no actual change.
    if slide_origin != "existing":
        panel_content.set_content(ws_id, "slide_deck", json.dumps({"slide_text": slide_text}),
                                   owner_id, source_node_ids=source_node_ids)
    if script_origin != "existing":
        panel_content.set_content(
            ws_id, "podcast",
            json.dumps({"script_text": script_text, "audio_path": audio_filename}),
            owner_id, source_node_ids=source_node_ids,
        )

    return {
        "panel_key": "video_overview",
        "status": "done",
        "slide_text": slide_text,
        "script_text": script_text,
        "video_bytes": os.path.getsize(video_path),
        "slide_source": slide_origin,    # "pasted" | "existing" | "derived" | "generated"
        "script_source": script_origin,  # same four values
        "reused_audio": reusable_audio_filename is not None,
        "updated_at": saved["updated_at"],
        "message": (
            "Slide deck, narration, and video generated and saved (Phase 5 step 5.5), "
            "reachable via Generate as of step 5.6. A servable download URL "
            f"(step 5.7+) isn't wired yet -- the mp4 exists on disk at {video_path!r} for now."
        ),
    }


def _generate_presentation_rehearsal(ws_id: str, scope: dict | None, owner_id: str) -> dict:
    """Phase 5 step 5.10. Same (ws_id, scope, owner_id) -> result shape
    every other NOTEBOOKS_GENERATE_TARGETS entry above already has --
    deliberately NOT registered in that dict yet (that's step 5.11, same
    "define the target function first, wire it into dispatch/manifest as
    a separate step" split podcast/video_overview already went through
    across steps 5.1-5.6).

    Reuses the podcast pipeline exactly, just with a different script
    source: generate_rehearsal_script() (agents/rehearsal_scriptwriter.py,
    step 5.9) in place of generate_podcast_script(), then the exact same
    synthesize_podcast() call _generate_podcast() above already makes.
    This works with zero changes to synthesize_podcast() itself because
    step 5.9's tts_synthesizer generalization (generic "LABEL:" speaker
    detection + "[PAUSE]"/"[PAUSE:N]" handling) already covers
    "JUDGE:"/"HOST A:"/"HOST B:"/"ADVOCATE:"/"MODEL ANSWER:" — this
    function doesn't need to know or care which labels a given mode's
    script uses.

    scope carries two new, rehearsal-specific keys on top of the usual
    source_node_ids: "mode" (judge / two_host / devils_advocate) and
    "difficulty" (novice / expert). Both are optional -- omitted, they
    fall through to generate_rehearsal_script()'s own defaults (see that
    module's _DEFAULT_MODE / _DEFAULT_DIFFICULTY) -- same "blank scope
    still works" posture source_node_ids already has across every other
    target in this file. An invalid mode/difficulty raises ValueError
    from generate_rehearsal_script() itself, which notebooks_generate()'s
    surrounding try/except (see that function above) turns into a normal
    "error" branch, same as any other target's raise.

    Persisted under its own "presentation_rehearsal" panel_key (added to
    eo/panel_content.py's VALID_PANEL_KEYS/GENERATED_PANEL_KEYS alongside
    "podcast"/"video_overview") rather than overloading the "podcast"
    key -- a rehearsal script and a podcast script are different content
    a user would reasonably want to keep side by side, not last-write-
    wins overwrite each other. Content shape mirrors "podcast"'s own
    {"script_text", "audio_path"} JSON string, plus "mode"/"difficulty"
    so a reload can show which variant is saved without re-parsing the
    script text to guess.
    """
    scope = scope or {}
    source_node_ids = scope.get("source_node_ids")
    mode = scope.get("mode") or "judge"
    difficulty = scope.get("difficulty") or "expert"

    script_text = generate_rehearsal_script(ws_id, mode, difficulty, source_node_ids)

    # BUGFIX (rehearsal validation gap): generic_worker.py's generation-
    # time retry (STRUCTURAL_FORMAT_CHECKS) already guards against this
    # at the source, but this endpoint never re-checked its own result --
    # a report that happened to satisfy even the STRICTER retry check (or
    # reached this function through some future path that skips that
    # retry) would otherwise go straight to synthesize_podcast() and get
    # silently mis-synthesized/persisted as if it were a real script.
    # Same "check again right before the text is trusted" posture
    # _is_usable_dialogue_script()'s own docstring already explains for
    # the video-overview reuse path.
    if not _is_usable_rehearsal_script(script_text):
        preview = script_text.strip()[:200].replace("\n", " ")
        raise ValueError(
            "rehearsal_scriptwriter produced text that doesn't look like "
            "a real dialogue script (no repeated speaker label and/or no "
            f"'[PAUSE:N]' marker found) -- got: {preview!r}"
        )

    audio_filename = f"presentation_rehearsal_{ws_id}.mp3"
    out_path = os.path.join(NOTES_EXPORTS_DIR, audio_filename)
    synthesize_podcast(script_text, out_path)

    saved = panel_content.set_content(
        ws_id,
        "presentation_rehearsal",
        json.dumps({
            "script_text": script_text,
            "audio_path": audio_filename,
            "mode": mode,
            "difficulty": difficulty,
        }),
        owner_id,
        source_node_ids=source_node_ids,
    )

    return {
        "panel_key": "presentation_rehearsal",
        "status": "done",
        "script_text": script_text,
        "mode": mode,
        "difficulty": difficulty,
        "audio_bytes": os.path.getsize(out_path),
        "updated_at": saved["updated_at"],
        "message": (
            "Rehearsal script + audio generated and saved (Phase 5 step 5.10), "
            "reachable via Generate as of step 5.11, and servable via "
            "GET .../notebooks/presentation_rehearsal/audio as of patch 5."
        ),
    }


NOTEBOOKS_GENERATE_TARGETS = {
    "clusters": _generate_clusters,
    "facts": _generate_facts,
    "suggested_notes": _generate_suggested_notes,
    "topic_notes": _generate_topic_notes,  # NEW — 2026-08-01 gap fix, see
                                            # _generate_topic_notes()'s own
                                            # comment above.
    "study_flashcards": _make_study_generate("study_flashcards"),
    "study_quiz": _make_study_generate("study_quiz"),
    "study_guide": _make_study_generate("study_guide"),
    "mindmap": _generate_mindmap,
    "suggested_route": _generate_suggested_route,
    "slide_deck": _generate_slide_deck,  # NEW — Video Overview reuse patch, step 1.
    "podcast": _generate_podcast,  # NEW — Phase 5 step 5.6.
    "video_overview": _generate_video_overview,  # NEW — Phase 5 step 5.6.
    "presentation_rehearsal": _generate_presentation_rehearsal,  # NEW — Phase 5 step 5.11.
    # REMOVED — chat audit: "backlinks" unregistered as a Generate target.
    # _generate_backlinks/link_concepts() below are left defined (in case
    # something else needs them later) but no longer reachable from any
    # picker or endpoint — real connection detection
    # (agents/backlink_detector.py's run_after_source_manager()) is
    # fully automatic on upload, per explicit request: no manual
    # "generate backlinks" path anywhere.
    # REMOVED — step 3: "workflows" unregistered as a Generate target, same
    # defined-but-unreachable treatment as "backlinks" right above.
    # _generate_workflows()/suggest_workflows() are left in place (the
    # "suggested_workflows" panel_content key and its whole-notebook,
    # 0-4-procedures pass are still valid concepts, just not this
    # feature's path anymore) but nothing calls them once step 5 also
    # drops the picker's "workflows" chip. Per-topic workflows now go
    # exclusively through POST /api/workspaces/{ws_id}/topics/workflow
    # (step 2) and agents/workflow_suggester.py's build_topic_workflow().
}


# Notebooks — Chat-First Refinement, Phase 1 step 1.6.
#
# Server-side mirror of frontend/app/lib/notebookCapabilities.js's TARGETS
# array (steps 1.1-1.5). Deliberately hand-kept in sync rather than
# generated from the JS file — this is Python, that's JS, and there's no
# shared build step between them yet. Guide's own framing (Phase 1 §3):
# this manifest is "the single source of truth both the LLM system
# prompt and any future UI ... read from, so the picker's keyword list
# and the LLM's tool list can never drift apart" — the two files still
# have to be edited together by hand for now; nothing here enforces that
# automatically. Fields intentionally omit `icon` (frontend-only,
# meaningless to the LLM or to this endpoint's callers) and `keywords`
# (Phase 2 replaces keyword matching with real tool-calling, so this
# manifest's `description` is what feeds the model — `keywords` stays a
# frontend-only fallback/pre-fill concern in NotebooksGeneratePicker.jsx).
#
# "suggested_route" is deliberately left out, matching the JS TARGETS
# array: it's a Diagrams sub-view (NotebooksTab.jsx), never a picker
# chip or a Generate-target the user (or, later, the LLM) selects
# directly, so it isn't a "capability" in this manifest's sense.
CAPABILITIES_MANIFEST = [
    {
        "key": "clusters", "label": "Clusters", "subTab": "insights",
        "description": "Group the workspace's notes and sources into topic clusters, so related material is organized together instead of a flat list.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "facts", "label": "Facts", "subTab": "insights",
        "description": "Pull out standalone factual statements from the sources and list them as discrete, citable facts.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # CHANGED — Phase 2 step 2.4 revisit (5.8 finding): description
        # now names the actual phrasing that misfired in 5.8's coverage
        # run ("What should I be taking notes on here?" -> no tool call,
        # 3/3) and calls out the facts/clusters neighbors it gets
        # confused with, so the model has enough to commit instead of
        # hedging. See utils/llm_client.py's CLASSIFY_INTENT_SYSTEM_PROMPT
        # comment for the paired prompt-level fix.
        "key": "suggested_notes", "label": "Suggested notes", "subTab": "insights",
        # CHANGED — 2026-08-01 gap fix: description corrected to match
        # what this target actually does (_generate_suggested_notes
        # calls agents/note_taker.py's scan_conversation() -- a chat
        # transcript scan for decisions/action items, not a source
        # scan). The old description ("Scan the sources for note-worthy
        # passages") was aspirational, not accurate, and is exactly why
        # asking this tool to write a note on a specific topic reliably
        # produced nothing (see agents/topic_note_writer.py's docstring
        # for the confirmed test). "topic_notes" right below is the
        # actual source-scanning tool that description used to promise.
        "description": "Scan the recent chat conversation in this notebook for a decision, insight, or action item worth saving as a note, and propose a draft the user can accept or discard. This does NOT read the sources/topics themselves -- it only looks at what's been said in chat. Use this for requests like 'was there anything worth noting in our conversation' or 'save that as a note' -- NOT for 'write a note about <topic>' or 'summarize this topic as a note' (see the topic_notes tool for that), pulling out standalone facts (see the facts tool), or grouping sources by topic (see the clusters tool).",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # NEW — 2026-08-01 gap fix: the source-grounded, single-topic
        # note generator "suggested_notes" was mistakenly assumed to
        # already be. See agents/topic_note_writer.py's module
        # docstring for the full finding, and _generate_topic_notes()
        # above for why this has no whole-notebook fallback.
        "key": "topic_notes", "label": "Notes on this topic", "subTab": "insights",
        "description": "Write one draft note summarizing a SPECIFIC topic's actual source material, for the user to accept or discard. Requires a single topic in scope (e.g. after clicking a Mind Map node, or naming a topic by title) -- reads that topic's real source excerpts, not the chat conversation. Use this for requests like 'write a note on <topic>', 'summarize this topic as a note', or 'give me notes on <topic>'. NOT for a whole-notebook scan of the chat for things worth noting (see the suggested_notes tool for that).",
        "scopeAllowed": "topic", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "study_flashcards", "label": "Flashcards", "subTab": "study",
        "description": "Generate a set of question/answer flashcards for studying the selected scope.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # CHANGED — Phase 2 step 2.4 revisit (5.8 finding): "Quiz me on
        # what I just read." and "Can you test me on this material?"
        # both misfired to no-tool-call 3/3 in 5.8's run -- neither says
        # "quiz" but both are unambiguous requests to be tested.
        "key": "study_quiz", "label": "Quiz", "subTab": "study",
        "description": "Generate a graded quiz covering the selected scope, which the user can take and submit for scoring. Use this whenever the user wants to be quizzed or tested on the material -- e.g. 'quiz me', 'test my understanding', 'test me on this' -- even if they don't use the word 'quiz'.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # CHANGED — Phase 2 step 2.4 revisit (5.8 finding): "Give me a
        # summary I can study from." and "I need a written summary to
        # study from." both misfired to no-tool-call 3/3, with the model
        # asking whether they meant a study guide, a mind map, or
        # flashcards -- "summary" alone reads as ambiguous across those.
        # Naming the phrasing directly and contrasting against mindmap/
        # facts resolves it.
        # CHANGED — regression from the first tuning pass (spotted in
        # the follow-up test run): broadening this description to cover
        # "a summary I can study from" made it also swallow "step-by-
        # step study workflow"/"study plan" requests, which must stay
        # no-tool-call (no workflow-planning tool is live yet -- see
        # this same regression already called out in
        # scripts/test_capability_coverage.py's TEST_CASES comment).
        # Added an explicit negative example rather than narrowing the
        # positive examples back down, since those still need to work.
        "key": "study_guide", "label": "Study guide", "subTab": "study",
        "description": "Produce a structured written study guide summarizing and organizing the selected scope for review. Use this for requests for a prose summary or write-up to study from -- e.g. 'give me a summary I can study from', 'write me a summary', 'summarize this for review' -- as opposed to a visual mind map (see the mindmap tool) or a list of standalone facts (see the facts tool). Do NOT use this for 'a step-by-step study workflow' or 'a study plan' requests -- those ask for an ordered sequence of steps, not a written summary, and no tool for that exists yet, so don't call anything for them.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # CHANGED — Phase 2 step 2.4 revisit (5.8 finding): "Map out the
        # connections between these topics." misfired to no-tool-call
        # 3/3, with the model asking "mind map or clusters?" -- the old
        # description's "how they relate to each other" didn't clearly
        # separate this from clusters' "grouped together." Spelled out
        # the relate-vs-group distinction directly.
        "key": "mindmap", "label": "Mind map", "subTab": "diagrams",
        "description": "Build a visual mind map of the concepts in the selected scope and how they relate to each other. Use this for requests to see or map out how topics/concepts connect or relate -- e.g. 'map out the connections between these topics', 'show me how these relate' -- as opposed to grouping sources into topic buckets (see the clusters tool).",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # CHANGED — Phase 5 step 5.7: endpoint/enabled flipped from the
        # Phase 1.5 stub (endpoint: None, enabled: False) now that steps
        # 5.1-5.4 gave this a real, workspace-scoped route and step 5.6
        # registered it in NOTEBOOKS_GENERATE_TARGETS. Points at the
        # dedicated POST .../notebooks/podcast route (not the shared
        # .../notebooks/generate dispatch route every other entry above
        # uses) since that's the more specific, real endpoint behind this
        # key -- see notebooks_podcast()'s own step 5.6 comment for why
        # both remain reachable. `endpoint` is documentation only today
        # (nothing reads it for routing -- generateNotebooks() always
        # calls .../notebooks/generate regardless of this string), so
        # this doesn't change any runtime dispatch, only what a future
        # help menu / the LLM's tool metadata would report. `enabled:
        # True` is what actually matters -- utils/capability_tools.py's
        # manifest_to_tools() only skips entries where `enabled` is
        # False, so this is what makes "generate_podcast" appear in
        # Phase 2's tools array for the first time.
        "key": "podcast", "label": "Podcast", "subTab": "insights",
        # CHANGED — Chat wiring patch (step 4): description now mentions
        # both new capabilities this step actually unlocks --
        # source/topic scoping (scopeAllowed flipped to "sources" right
        # below) and reusing a script the user already pasted
        # (pastableTextFields, read by utils/capability_tools.py's
        # _parameters_for_scope()) -- so the classifier knows to fill in
        # source_ids/script_text instead of always calling this with an
        # empty scope. This is a real behavior change, not just a
        # description tweak: before this step, podcast was hardcoded
        # "whole" and had no path for a pasted script at all -- see
        # _generate_podcast()/generate_podcast_script()'s own raw_context
        # parameter (Video Overview reuse patch, step 2) for the half of
        # this it already supported that chat just couldn't reach yet.
        "description": "Generate a two-host audio podcast episode discussing the selected scope. Can be scoped to specific sources ('a podcast about the pricing chapter') instead of the whole notebook. If the user's message already includes or clearly refers to a script they wrote themselves, pass it as script_text to narrate that instead of writing a new one from the sources.",
        "scopeAllowed": "sources", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/podcast",
        "pastableTextFields": ["script_text"],
        "enabled": True,
    },
    {
        # NEW — Video Overview reuse patch, step 1: slide_planner now has
        # a standalone target (_generate_slide_deck(), registered in
        # NOTEBOOKS_GENERATE_TARGETS above) instead of only ever running
        # as video_overview's hidden first stage, so it needs its own
        # manifest entry the same way "podcast" got one in step 5.7 --
        # this is what makes "generate_slide_deck" appear in the chat
        # tool list at all. No dedicated route (unlike podcast/
        # video_overview) -- same "shared dispatch route is enough"
        # reasoning presentation_rehearsal's own entry below gives, since
        # nothing here needs a second, TTS/build-adjacent stage the way
        # podcast/video_overview's dedicated routes do.
        "key": "slide_deck", "label": "Presentation", "subTab": "insights",
        # CHANGED — Chat wiring patch (step 4): same scoping + pasted-text
        # flip "podcast" above got, for the slide half -- see that
        # entry's own comment.
        "description": "Generate a slide deck outline (title + slide-by-slide bullet points) summarizing the selected scope, with no narration or video attached. Use this for requests like 'make me a presentation', 'build a slide deck', or 'outline slides for this' -- as opposed to the video_overview tool, which additionally narrates and renders these slides into a video. Can be scoped to specific sources instead of the whole notebook. If the user's message already includes or clearly refers to a slide outline they wrote themselves, pass it as slide_text to reformat/reuse that instead of writing a new one from the sources.",
        "scopeAllowed": "sources", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "pastableTextFields": ["slide_text"],
        "enabled": True,
    },
    {
        # CHANGED — Phase 5 step 5.7: same flip as "podcast" above, now
        # that step 5.5 gave video_overview a real route and step 5.6
        # registered it in NOTEBOOKS_GENERATE_TARGETS.
        # CHANGED — Phase 5 step 5.8 finding: "Give me a video
        # walkthrough of this material." misfired to no-tool-call 3/3 --
        # the model correctly recognized "video overview" was the
        # closest tool but hedged because "walkthrough" wasn't in the
        # description. Added it (and "explainer") as explicit synonyms.
        "key": "video_overview", "label": "Video overview", "subTab": "insights",
        # CHANGED — Chat wiring patch (step 4): same scoping flip as
        # "podcast"/"slide_deck" above, plus BOTH pastableTextFields --
        # this is the tool the Video Overview reuse patch (step 2) was
        # actually written for: _generate_video_overview() already knows
        # how to take either or both of script_text/slide_text and skip
        # regenerating whichever half it's given (reusing a saved
        # "podcast"/"slide_deck" panel, or -- new as of this step -- text
        # the user just pasted this turn), deriving the other half from
        # it when only one is given, and falling all the way back to
        # generating both fresh only when neither is available. This
        # description spells out all three shapes (nothing pasted,
        # only a script, only slides) since a caller reading only "video
        # overview" wouldn't otherwise know pasted content changes what
        # gets generated.
        "description": "Generate a narrated video overview summarizing the selected scope -- a short explainer/walkthrough video. Use this for requests like 'video overview', 'video summary', 'explainer video', or 'video walkthrough'. Can be scoped to specific sources instead of the whole notebook. If the user's message already includes or clearly refers to a podcast script and/or a slide outline they wrote themselves, pass those as script_text/slide_text -- either one alone still works (the missing half gets generated to match whichever one you gave), and passing both skips generation for this call entirely and just renders the video.",
        "scopeAllowed": "sources", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/video_overview",
        "pastableTextFields": ["script_text", "slide_text"],
        "enabled": True,
    },
    {
        # NEW — Phase 5 step 5.11: flips this from undefined-in-the-manifest
        # (it had no Phase 1.5 stub -- rehearsal didn't exist as a concept
        # until step 5.9) straight to a real, enabled entry, now that step
        # 5.10 gave it a working _generate_presentation_rehearsal() target
        # and this step just registered that in NOTEBOOKS_GENERATE_TARGETS
        # above. No dedicated route exists for this one (unlike podcast/
        # video_overview) -- it only needs the shared dispatch route, same
        # as clusters/facts/etc., so `endpoint` points there rather than
        # inventing a POST .../notebooks/presentation_rehearsal route this
        # plan never asked for.
        #
        # `scope.mode`/`scope.difficulty` are called out explicitly in the
        # description (rather than left to a generic "selected scope"
        # phrase like the entries above) since Phase 2's tool-calling reads
        # this string to build the LLM's tool list, and mode/difficulty are
        # exactly the two things a caller might want to specify in the
        # same turn as the request itself (e.g. "quiz me like a skeptical
        # judge" or "give me an easy two-host run-through").
        "key": "presentation_rehearsal", "label": "Presentation rehearsal", "subTab": "insights",
        "description": "Generate an interactive audio rehearsal for defending or presenting the selected scope -- a mock Q&A or practice run, not a straight recap. Supports a 'judge' mode (a skeptical panelist grills you), 'two_host' mode (a friendly co-presenter walk-through), and 'devils_advocate' mode (a debate partner pushes back), each at 'novice' or 'expert' difficulty. Use this for requests like 'help me rehearse my presentation', 'quiz me like a thesis defense', 'practice defending this', or 'mock Q&A on this material'.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "workflow", "label": "Workflow", "subTab": "diagrams",
        "description": "Build a step-by-step study workflow for a single topic.",
        "scopeAllowed": "topic", "endpoint": None,
        "enabled": False,
    },
]

# NEW — Phase 4 step 4.4: capability key -> human-readable label, for the
# generation_started/generation_done/generation_error payloads' `label`
# field (see eo/notify.py's VALID_KINDS comment, step 4.3). Built off
# CAPABILITIES_MANIFEST rather than hand-duplicated, so the two can't
# drift. "suggested_route" is deliberately absent from the manifest (see
# comment above CAPABILITIES_MANIFEST) but IS still a real, reachable
# NOTEBOOKS_GENERATE_TARGETS entry -- _capability_label() below falls back
# to a title-cased version of the key for it (and for anything else that
# might reach notebooks_generate() without a manifest entry) rather than
# raising, since a slightly-rough fallback label is fine for a
# notification and this must never be what breaks a real generation.
_CAPABILITY_LABELS = {c["key"]: c["label"] for c in CAPABILITIES_MANIFEST}


def _capability_label(target: str) -> str:
    return _CAPABILITY_LABELS.get(target) or target.replace("_", " ").title()


@router.get("/api/capabilities")
def get_capabilities():
    """Phase 1 step 1.6.

    Returns the same manifest shape the frontend's TARGETS array carries,
    so step 1.7 can point the frontend here instead of the static import.
    No scope/auth filtering yet — every registered capability (including
    disabled stubs, so a future help UI or debugging call can still see
    what's coming) is returned as-is; that keeps this step a pure read
    with zero behavioral change, matching the rest of Phase 1. Left
    public (no Depends(require_auth)) since the manifest isn't
    user-scoped data, same as any other static config the frontend reads.
    """
    return {"capabilities": CAPABILITIES_MANIFEST}


class ClassifyIntentRequest(BaseModel):
    message: str


@router.post("/api/workspaces/{ws_id}/notebooks/classify-intent")
async def classify_intent(ws_id: str, req: ClassifyIntentRequest, owner_id: str = Depends(require_auth)):
    """Phase 2 step 2.5.

    Runs one chat message through the real tool-calling classification
    pass (utils.llm_client.classify_tool_intent(), validated against
    Groq in steps 2.3/2.4) and returns the raw result. Deliberately does
    NOT call generateNotebooks(...) or any other side-effecting target,
    and does not persist or act on anything -- WorkspaceChatPanel.jsx's
    send path (step 2.5) only logs this response, it doesn't branch on
    it yet. That branching is step 2.6.

    ws_id is accepted (and the workspace existence is confirmed, same as
    every other workspace-scoped route) for auth/scoping consistency and
    so a later step can pass workspace-specific context (e.g. which
    topics exist, for a workflow topic_id hint) without changing this
    route's shape -- nothing here uses ws_id yet beyond that check.

    classify_tool_intent() is written to never raise (see its own
    docstring) -- any Groq-side failure comes back as a normal
    {"error": "..."} field in the 200 response, not a 500, since a
    log-only classification pass failing should never look like a real
    error to the frontend or block the fallback to sendTask().

    NEW — Patch A3: the tools array this route classifies against now
    also includes every currently-connected MCP server's tools
    (eo.mcp_agent_tools.mcp_tools_for_agent()), normalized into the same
    OpenAI shape as the internal tools above -- so a message like "find
    open issues on our repo" can classify to a GitHub MCP tool exactly
    the way "quiz me on this" classifies to generate_study_quiz. This
    route is now `async def` (it wasn't before) purely because
    mcp_tools_for_agent() needs to await eo.mcp_client.list_tools() per
    connected server -- classify_tool_intent() itself is still a plain
    sync call, unchanged.
    """
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    # NEW — step 6.8: "mark X as done" isn't a generation target, so it
    # doesn't live in CAPABILITIES_MANIFEST -- it's appended from its own
    # hand-written builder (see utils/capability_tools.py's header
    # comment on why generation and non-generation tools stay separate).
    tools = (
        manifest_to_tools(CAPABILITIES_MANIFEST)
        + study_progress_tools()
        + await mcp_tools_for_agent()  # NEW — Patch A3
    )
    return classify_tool_intent(req.message, tools)


class NotebooksGenerateRequest(BaseModel):
    targets: list[str]
    scope: dict[str, Any] | None = None
    # NEW — Phase 4 step 4.4. Flagged as a gap in the step 4.1 transport
    # decision (decisions/step-4.1-notification-transport.md): this route
    # is keyed on ws_id + owner_id only, but emit_event() needs a
    # session_id to know which Pusher channel to publish on, and a
    # workspace can have more than one chat/session attached to it, so
    # the backend can't safely infer "the" session server-side. The
    # frontend passes its active chat's session_id (WorkspaceChatPanel.jsx
    # already has it as dock.state.sessionId) through here. Optional and
    # defaulting to None so existing callers that don't pass it yet keep
    # working exactly as before -- notify()/emit_event() both already
    # no-op cleanly on session_id=None (see eo/notify.py).
    session_id: str | None = None


@router.post("/api/workspaces/{ws_id}/notebooks/generate")
def notebooks_generate(ws_id: str, req: NotebooksGenerateRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    if not req.targets:
        raise HTTPException(status_code=422, detail="targets must be a non-empty list")

    # NEW — Phase 4 step 4.4: chat-native notifications for this generate
    # flow, on top of the branches list this endpoint already returns.
    # Deferred import, same reasoning every other notify() call site in
    # this codebase already gives (eo/chat_workspace.py's
    # chat_triggered_partial_promote(), agents/source_manager.py, etc.):
    # no hard dependency for callers that never pass a session_id.
    # req.session_id=None makes every notify() below a documented no-op
    # (see eo/notify.py), so this is safe to land even before any
    # frontend call site is updated to actually send one.
    from eo.notify import notify

    branches = []
    for target in req.targets:
        label = _capability_label(target)
        run_target = NOTEBOOKS_GENERATE_TARGETS.get(target)
        if run_target is None:
            # Not an unknown-route 404 -- the endpoint itself is valid,
            # this particular target just isn't wired yet (see comment
            # above). Reported as a failed branch, same as a target that
            # raises during its run, so a mixed request like
            # {"targets": ["clusters", "flashcards"]} still returns
            # Clusters' result instead of rejecting the whole call.
            error = f"'{target}' isn't wired to Generate yet"
            branches.append({"panel_key": target, "status": "error", "error": error})
            # Still worth a chat-native notification -- from the chat
            # thread's point of view this is exactly as much a "your
            # generation failed" moment as a run_target() raising below,
            # even though it never actually started.
            notify(req.session_id, "generation_error",
                   {"panel_key": target, "workspace_id": ws_id, "label": error})
            continue
        notify(req.session_id, "generation_started",
               {"panel_key": target, "workspace_id": ws_id, "label": label})
        try:
            result = run_target(ws_id, req.scope, owner_id)
            branches.append({"panel_key": target, "status": "done", "result": result})
            notify(req.session_id, "generation_done",
                   {"panel_key": target, "workspace_id": ws_id, "label": label})
        except Exception as exc:
            branches.append({"panel_key": target, "status": "error", "error": str(exc)})
            # label carries the user-facing message here, per
            # eo/notify.py's VALID_KINDS comment for generation_error --
            # more useful in a chat notification than the capability's
            # static display name would be.
            notify(req.session_id, "generation_error",
                   {"panel_key": target, "workspace_id": ws_id, "label": str(exc)})

    return {"branches": branches}


# --- Notebooks — Chat-First Refinement, Phase 5 step 5.1 / 5.2 / 5.3 -------
# Podcast, workspace-scoped route.
#
# CHANGED — step 5.6: this is no longer the ONLY way to reach podcast
# generation -- _generate_podcast() (defined above, right before
# NOTEBOOKS_GENERATE_TARGETS) is now also registered under the "podcast"
# key in that dict, so a chat-triggered {"targets": ["podcast"]} call to
# notebooks_generate() reaches the exact same generation logic this
# dedicated route calls. This route itself is kept -- a single-target
# call with a plain 400 on failure (rather than a one-item branches
# list) is a nicer shape for a direct "generate my podcast" caller, same
# reasoning the per-topic workflow endpoint below gives for staying
# outside NOTEBOOKS_GENERATE_TARGETS entirely, just one step less
# extreme here since this target IS also dispatch-reachable.
#
# The guide's own framing (§0's "what I found" bullet 4) is that
# podcast/video_overview live in a completely separate,
# non-workspace-scoped "notes" domain today
# (POST /api/notes/podcast/synthesize, requiring a script_text the
# caller already has in hand -- see synthesize_podcast_endpoint() above)
# -- this route is the start of pulling that into the notebook-scoped
# world instead: ws_id-addressed, same require_auth +
# chat_workspace.get_workspace() existence check every other
# /api/workspaces/{ws_id}/... route above already uses, same request
# shape (scope, session_id) as NotebooksGenerateRequest so step 5.4 can
# grow this handler in place without a breaking request-shape change
# once persistence lands. session_id is accepted (not used yet) for the
# same forward-compatibility reason NotebooksGenerateRequest's own
# session_id field gives -- step 5.4+ will want to emit Phase 4's
# generation_started/generation_done/generation_error notifications the
# same way notebooks_generate() already does, and threading it through
# from the start avoids a second request-shape change later.
#
# CHANGED — step 5.2: now actually runs agents/podcast_scriptwriter.py's
# generate_podcast_script() over the workspace's (optionally scoped)
# sources and returns the real two-host Markdown script.
# scope["source_node_ids"] is read the same "blank scope = whole
# notebook" way every other Generate target in this file already reads
# it (see _generate_facts, _make_study_generate above).
#
# CHANGED — step 5.3: now also feeds that script straight into
# agents/tts_synthesizer.py:synthesize_podcast() (already imported
# above for synthesize_podcast_endpoint's own use -- no new import
# needed) and writes a real mp3. Written to NOTES_EXPORTS_DIR under
# `podcast_{ws_id}.mp3` -- unlike synthesize_podcast_endpoint's
# free-text `req.title`-derived filename (which needs slugifying), ws_id
# is already an opaque, filesystem-safe id, same "use the id directly,
# no slugify needed" precedent /api/workspaces/{ws_id}/export/files's
# own `{ws_id}_export.zip` naming already sets a few hundred lines up.
# Deliberately a single fixed per-workspace filename, not one per call
# -- last-write-wins on regenerate, same overwrite semantics every other
# Generate target in this file already has (see _generate_mindmap's own
# "Regenerate is meant to be a last-write-wins overwrite" comment).
# synthesize_podcast()'s own ValueError ("no HOST X: dialogue lines
# found") is treated the same 400 way generate_podcast_script()'s
# LookupError already is just below -- a malformed script is a bad-
# input case, not a server error, whichever of the two stages produced
# it.
#
# CHANGED — step 5.4: now persists via panel_content.set_content()
# under panel_key "podcast" (added to eo/panel_content.py's
# VALID_PANEL_KEYS/GENERATED_PANEL_KEYS above), same encode-on-write
# JSON-string shape _generate_workflows already uses for
# "suggested_workflows" -- content here is
# json.dumps({"script_text", "audio_path"}). audio_path is stored
# relative to NOTES_EXPORTS_DIR (just the filename), not the absolute
# `out_path` on disk, so the row doesn't need rewriting if
# NOTES_EXPORTS_DIR's location ever changes. source_node_ids is
# recorded the same "blank scope = whole notebook" way
# _make_study_generate's own comment explains, so a source delete can
# invalidate a scoped podcast the same as any other generated panel.
# Status moves from "audio_synthesized" to "done" now that both stages
# and the persistence step have all succeeded -- still no GET route
# serves the mp3 back yet (that's step 5.6+, alongside registering this
# target in NOTEBOOKS_GENERATE_TARGETS and the Phase 1 manifest), so the
# response keeps reporting the on-disk byte count rather than the file
# itself.

class NotebooksPodcastRequest(BaseModel):
    scope: dict[str, Any] | None = None
    session_id: str | None = None


@router.post("/api/workspaces/{ws_id}/notebooks/podcast")
def notebooks_podcast(ws_id: str, req: NotebooksPodcastRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    try:
        # CHANGED — step 5.6: the actual generation logic now lives in
        # _generate_podcast() above (shared with NOTEBOOKS_GENERATE_TARGETS'
        # "podcast" entry) -- this route's only job is the
        # workspace-existence check above and translating that shared
        # function's raises into an HTTP error, same "clear 400, not a
        # 500" contract agents/study_generator.py's own LookupError
        # already gets from _make_study_generate's caller. There's no
        # branches list here since this isn't a multi-target dispatch,
        # so both LookupError (bad scope) and ValueError (bad script)
        # surface directly as a 400 rather than the "error" branch
        # notebooks_generate() reports for the same two exceptions.
        return _generate_podcast(ws_id, req.scope, owner_id)
    except (LookupError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- workspace-scoped Video Overview (Phase 5 step 5.5) --------------------
# Same "pull the notes-domain subsystem into the workspace-scoped world"
# move notebooks_podcast() above already made for podcast in steps
# 5.1-5.4 -- this is that same repeat for Video Overview per the plan's
# own "Repeat 5.1-5.4 for .../video_overview" instruction. Same request
# shape (scope, session_id) as NotebooksPodcastRequest for the same
# forward-compatibility reason that route's own header comment gives.
#
# Two source-grounded stages, not one: a video overview needs both a
# slide deck (agents/slide_deck_planner.py's slide_planner role, step
# 5.5's own new module) AND narration audio. Rather than inventing a
# single-narrator TTS path with no role/voice precedent anywhere in this
# codebase, this reuses the exact two-host podcast pipeline
# _generate_podcast() already runs (generate_podcast_script() +
# synthesize_podcast()) as the narration track -- agents/
# video_overview_builder.py's own docstring already describes the result
# as "static slide-style frames narrated by ... already-synthesized
# podcast audio," so a two-host narration is the pipeline's own designed
# use case, not a repurposing. Generation is fully self-contained (does
# NOT read back a previously-saved "podcast" panel) -- same
# no-cross-endpoint-dependency posture _generate_podcast() itself has,
# and the one the guide's own §0 finding on the old
# /api/notes/video-overview/build's by-title lookup flags as the thing
# worth fixing.
#
# CHANGED — step 5.6: the actual generation (both stages, the build,
# and the panel_content persistence) now lives in
# _generate_video_overview() above, registered under the
# "video_overview" key in NOTEBOOKS_GENERATE_TARGETS -- same extraction
# _generate_podcast() got, so this target is reachable both from the
# dedicated route below AND from a chat-triggered
# {"targets": ["video_overview"]} call to notebooks_generate(). This
# route's job is now just the workspace-existence check and translating
# _generate_video_overview()'s raise (LookupError from either generation
# stage, or ValueError from the narration or build stage -- not
# distinguished here, same single-400-either-way posture
# notebooks_podcast() already takes) into an HTTP error.
#
# Persists via panel_content.set_content() under panel_key
# "video_overview" (added to eo/panel_content.py's VALID_PANEL_KEYS/
# GENERATED_PANEL_KEYS alongside "podcast") -- content is
# json.dumps({"slide_text", "script_text", "video_path"}), same
# encode-on-write JSON-string shape as "podcast"'s own
# {"script_text", "audio_path"}. video_path is stored
# NOTES_EXPORTS_DIR-relative, same portability reasoning as podcast's
# audio_path. No GET route serves the mp4 back yet, and this isn't
# registered in the Phase 1 manifest yet either -- that's step 5.7.

class NotebooksVideoOverviewRequest(BaseModel):
    scope: dict[str, Any] | None = None
    session_id: str | None = None


@router.post("/api/workspaces/{ws_id}/notebooks/video_overview")
def notebooks_video_overview(ws_id: str, req: NotebooksVideoOverviewRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    try:
        # CHANGED — step 5.6: see notebooks_podcast()'s own comment above --
        # same extraction, same shared-with-NOTEBOOKS_GENERATE_TARGETS
        # reasoning, this time for _generate_video_overview()'s
        # LookupError (either generation stage) / ValueError
        # (narration or build stage).
        return _generate_video_overview(ws_id, req.scope, owner_id)
    except (LookupError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- serving the panel_content-backed podcast/video_overview media --------
# Frontend paste-box patch (patch 4), step 1: the gap flagged in
# notebooks_podcast()'s and _generate_video_overview()'s own comments --
# "no GET route serves the mp3/mp4 back yet" -- closed here. Deliberately
# two thin, workspace-scoped GET routes rather than a single
# "/media/{filename}" route: each one resolves its own filename off the
# matching panel_content row (via _load_saved_podcast()/
# _load_saved_video_path() above) rather than trusting a client-supplied
# filename, so there's no path-traversal surface and no way for one
# workspace's owner to probe another workspace's export filenames by
# guessing them. Same require_auth + get_workspace existence check every
# other route in this file already uses; a 404 here means "generate one
# first" (via chat, the dedicated POST route, or the Presentation/Podcast/
# Video Overview paste boxes), not a server error.
@router.get("/api/workspaces/{ws_id}/notebooks/podcast/audio")
def notebooks_podcast_audio(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    _, audio_filename = _load_saved_podcast(ws_id)
    if not audio_filename:
        raise HTTPException(
            status_code=404,
            detail="No podcast audio saved for this workspace yet -- generate one first.",
        )
    return FileResponse(
        os.path.join(NOTES_EXPORTS_DIR, audio_filename),
        media_type="audio/mpeg",
        filename=audio_filename,
    )


@router.get("/api/workspaces/{ws_id}/notebooks/video_overview/video")
def notebooks_video_overview_video(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    video_filename = _load_saved_video_path(ws_id)
    if not video_filename:
        raise HTTPException(
            status_code=404,
            detail="No video overview saved for this workspace yet -- generate one first.",
        )
    return FileResponse(
        os.path.join(NOTES_EXPORTS_DIR, video_filename),
        media_type="video/mp4",
        filename=video_filename,
    )


# Patch 5 (Presentation rehearsal reachability patch), step 1: closes the
# same "no GET route serves the mp3 back" gap the Frontend paste-box patch
# (patch 4) closed for "podcast" above -- presentation_rehearsal got a
# generation target (step 5.10) and a manifest/chat-tool entry (step 5.11)
# but was left with no way to fetch the audio it saves, and no frontend
# reachability at all. Same workspace-scoped, filename-resolved-server-side
# shape as notebooks_podcast_audio() above, just against the
# "presentation_rehearsal" panel via _load_saved_rehearsal().
@router.get("/api/workspaces/{ws_id}/notebooks/presentation_rehearsal/audio")
def notebooks_presentation_rehearsal_audio(ws_id: str, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    _, audio_filename = _load_saved_rehearsal(ws_id)
    if not audio_filename:
        raise HTTPException(
            status_code=404,
            detail="No presentation rehearsal audio saved for this workspace yet -- generate one first.",
        )
    return FileResponse(
        os.path.join(NOTES_EXPORTS_DIR, audio_filename),
        media_type="audio/mpeg",
        filename=audio_filename,
    )


# --- per-topic workflow, triggered by a Mind Map node click (step 2) -------
# Deliberately its own endpoint rather than a NOTEBOOKS_GENERATE_TARGETS
# entry: Generate targets are whole-notebook passes persisted to
# panel_content (see _generate_workflows above -- step 3 unregistered
# it from this dict, defined-but-unreachable, same as "backlinks"),
# each keyed by a fixed panel_key.
#
# CHANGED — step 7 persistence fix: a topic click used to be a genuine
# one-off (result held only in DiagramsView's React state, gone on tab
# switch/refresh -- the exact bug this step fixes). It's still addressed
# by whatever label the user clicked, but the result is now also
# persisted under panel_content's single "topic_workflows" key, a JSON
# dict of {topic_key: workflow} merged across every topic this
# workspace has ever generated a workflow for -- see
# eo/panel_content.py's VALID_PANEL_KEYS entry for why one merged blob
# was chosen over a dynamic workflow:<topic_id> key per topic.
#
# Read-modify-write race: two topics generated in quick succession
# (e.g. two rapid Mind Map clicks) both do get_content -> merge ->
# set_content, and the second write can clobber the first's if they
# interleave. Not worth a locking mechanism for what's a single-user,
# one-click-at-a-time interaction in practice -- flagged here rather
# than silently accepted.

class TopicWorkflowRequest(BaseModel):
    topic_label: str
    source_node_ids: list[str] | None = None


@router.post("/api/workspaces/{ws_id}/topics/workflow")
def topic_workflow_endpoint(ws_id: str, req: TopicWorkflowRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    if not (req.topic_label or "").strip():
        raise HTTPException(status_code=422, detail="topic_label must be non-empty")

    # build_topic_workflow() already swallows its own failures (plan()
    # errors, no topic match, unparseable model output) and falls back
    # to a generic sequence rather than raising -- see its docstring.
    # Nothing here should ever need the except branch, but a topic
    # click still shouldn't 500 the request if something unforeseen
    # slips through.
    try:
        workflow = build_topic_workflow(ws_id, req.topic_label, source_node_ids=req.source_node_ids)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # NEW — step 7: get-merge-set into the single "topic_workflows" blob.
    # Always written with source_node_ids=None (see GENERATED_PANEL_KEYS'
    # comment) -- this row's scope isn't "the sources for this one
    # topic," it's "every topic ever generated," so there's no single
    # source_node_ids value that would be correct to record per write.
    try:
        existing = panel_content.get_content(ws_id, "topic_workflows")
        merged = json.loads(existing["content"]) if existing.get("content") else {}
        if not isinstance(merged, dict):
            merged = {}
        # NEW — store the exact clicked label alongside the workflow (not
        # just its LLM-generated `title`, which can read differently, e.g.
        # "DC Motor" clicked vs. a "DC Motor — Mastery Path" title) so the
        # frontend can hydrate WorkflowsView entries keyed the same way a
        # live click keys them -- see NotebooksTab.jsx's DiagramsView.
        merged[workflow["topic_key"]] = {**workflow, "topic_label": req.topic_label}
        panel_content.set_content(ws_id, "topic_workflows", json.dumps(merged), owner_id,
                                   source_node_ids=None)
    except Exception:
        # Persistence failing shouldn't fail the click itself -- the
        # user still gets their workflow rendered this session, it just
        # won't survive a refresh this one time. Same "never let this
        # be the reason a topic click errors" posture build_topic_workflow()
        # already takes internally.
        pass

    # NEW — step 6.6: first-generation-only progress hook (see
    # eo/study_progress.py). Kept here at the caller rather than inside
    # build_topic_workflow() itself, same reasoning as the
    # topic_workflows persistence block above -- that function stays a
    # pure "compute a workflow" helper with no storage side effects.
    # Keyed by topic_key (never None) rather than topic_id (None on a
    # generic-fallback miss), matching how the persistence block above
    # keys "topic_workflows" too.
    # Only bumps not_started -> ongoing: a topic a user has already
    # marked "done" (or that's already "ongoing") shouldn't regress
    # just because they re-click/regenerate its workflow later.
    try:
        current = study_progress.get_progress(ws_id, workflow["topic_key"])
        if current["status"] == study_progress.STATUS_NOT_STARTED:
            study_progress.set_progress(ws_id, workflow["topic_key"],
                                         status=study_progress.STATUS_ONGOING)
    except Exception:
        # Best-effort, same "never let this be the reason a topic
        # click errors" posture as the persistence block above.
        pass

    return workflow


# --- data tables from scattered facts (see agents/note_table_builder.py,
# Part 4 §4.4) --------------------------------------------------------------
# Same directly-called, own-endpoint shape as backlinks/clustering above,
# not routed through the Panel/executor role-hiring pipeline -- see that
# module's docstring for why.

@router.post("/api/workspaces/{ws_id}/table")
def build_table_endpoint(ws_id: str, req: BuildTableRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")
    try:
        table = build_table(ws_id, req.field_names, node_type=req.node_type, expanded=req.expanded)
        workspace_facts.record_section_entries(
            ws_id,
            "extractions",
            [
                {
                    "key": row.get("node_id") or row.get("title") or f"row_{index}",
                    "title": row.get("title") or row.get("node_id") or f"Row {index + 1}",
                    "summary": ", ".join(
                        f"{field}={row.get(field)!r}"
                        for field in req.field_names
                        if row.get(field) not in (None, "")
                    ) or table.get("summary") or "Extraction row",
                    "text": row.get("title") or "",
                    "data": row,
                }
                for index, row in enumerate(table.get("rows", []))
            ],
            source="note_table_builder",
            source_ref="/api/workspaces/{ws_id}/table",
            event="extraction",
        )
        return table
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Test tab / "simulate" domain (see eo/structure.py's STRUCTURE_TEMPLATES
# ["simulate"], Part 1) -----------------------------------------------------
# Test tab design spec's Build Order step 1 originally called for wrapping
# agents/review_aggregator.py's aggregate_reviews() merge step here -- that
# doesn't actually fit: aggregate_reviews expects each member's output
# already shaped as {"issues": [...], "summary": ...} (Reviewer Pool's
# structured JSON), while every persona role's own ROLE_PROMPTS_SEED brief
# (persona_customer, persona_skeptic, critic_reviewer, usability_walkthrough,
# red_team, pricing_sensitivity, support_ticket_predictor, competitor_response)
# is a plain generic_worker role writing free-form in-character prose, not
# structured issues -- there'd be nothing for aggregate_reviews to parse.
# More importantly, simulation_synthesizer's own brief explicitly rejects
# review_aggregator-style merging ("Preserve real disagreement between
# personas explicitly -- do not average conflicting reactions into a single
# flattened conclusion"). The synthesis this tab needs already runs as part
# of the domain's own execution order -- simulation_synthesizer is
# deliberately hired last, after every persona (see STRUCTURE_TEMPLATES'
# own comment), so it can read their outputs via input_keys. So this
# endpoint's job is just reading back what already ran off the memory bus,
# same pattern GET /api/tasks/{session_id} already uses for
# integration_flagger's stage_output -- not a new merge step.
#
# marketplace_review_batch is read separately from the other persona roles
# since its own brief specifies a different, already-structured fenced-json
# shape (a bare array of {"rating","sentiment","text"}) rather than free
# prose -- each role's own brief decides its shape, this endpoint just
# reads it back either way.

@router.post("/api/workspaces/{ws_id}/simulate")
def get_simulation_results(ws_id: str, req: SimulateRequest, owner_id: str = Depends(require_auth)):
    try:
        chat_workspace.get_workspace(ws_id, owner_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown workspace_id")

    # SECURITY FIX (Bug 3 / IDOR): req.session_id was previously trusted
    # straight from the request body with no check that it belongs to
    # ws_id or to this user at all -- any authenticated user who owned
    # *some* workspace could read back another user's persona reactions
    # by pairing their own ws_id with a guessed/leaked session_id, since
    # the memory-bus keys below are built from session_id alone. Same
    # guard api/routes/tasks.py already uses on every session-scoped
    # endpoint, imported from api.deps for the same "stop just any
    # authenticated user who happens to know the session_id" reason.
    _resolve_chat_or_404(req.session_id, owner_id, require_edit=False)

    session_id = req.session_id
    persona_roles = [r for r in STRUCTURE_TEMPLATES["simulate"] if r != "simulation_synthesizer"]

    # Same read-side app_slug scoping GET /api/tasks/{session_id} uses for
    # stage_output:* keys -- without it, read_many() falls back to
    # whatever app_slug happens to be the persisted Redis global, the
    # exact cross-session collision Migration Part B fixed on the write
    # side.
    set_app_slug(session_id)
    keys = [f"stage_output:{session_id}:{role}" for role in persona_roles]
    synthesis_key = f"stage_output:{session_id}:simulation_synthesizer"
    data = bus_read_many(keys + [synthesis_key], default=None)

    personas = []
    for role in persona_roles:
        text = data[f"stage_output:{session_id}:{role}"]
        if not text:
            continue
        if role == "marketplace_review_batch":
            reviews = _parse_marketplace_reviews(text)
            if reviews:
                personas.append({"role": role, "reviews": reviews})
        else:
            personas.append({"role": role, "text": text})

    return {
        "session_id": session_id,
        "synthesis": data[synthesis_key],
        "personas": personas,
    }


# --- notes domain: capture (see agents/web_clipper.py, Part 4 §4.2) ------
# Driven by a small bookmarklet/extension that POSTs the current page's
# URL here. One new ingestion endpoint, not a new backend paradigm --
# same shape as every other write endpoint above, just backed by a
# deterministic tool agent instead of a memory-bus write. PDF/Office/
# video/voice ingestion land the same way once those ingestors exist;
# this is the first one wired end to end.

@router.post("/api/notes/clip", dependencies=[Depends(require_auth)])
def clip_url_endpoint(req: ClipUrlRequest):
    try:
        # session_id NEW — §9c, see ClipUrlRequest's own field comment
        result = process_upload(
            "web_clip", req.url, req.workspace_id,
            session_id=req.session_id, created_by="user",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"node_ids": result["node_ids"], "title": result["title"]}


@router.post("/api/notes/video", dependencies=[Depends(require_auth)])
def ingest_video_endpoint(req: ClipUrlRequest):
    # Reuses ClipUrlRequest -- identical {url, workspace_id, session_id}
    # shape, no reason for a separate model.
    try:
        # session_id NEW — §9c, see ClipUrlRequest's own field comment
        result = process_upload(
            "video", req.url, req.workspace_id,
            session_id=req.session_id, created_by="user",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"node_ids": result["node_ids"], "title": result["title"]}


@router.post("/api/notes/import", dependencies=[Depends(require_auth)])
async def import_file_endpoint(
    workspace_id: str = Form(...), file: UploadFile = File(...),
    session_id: str | None = Form(None),   # NEW — §9c, see ClipUrlRequest's field comment; Form(...) can't reuse a Pydantic model the way JSON endpoints do, so this is the multipart-endpoint version of the same optional field
):
    """Office/docx/pptx/xlsx/csv/md/json ingestion. No new parsing code —
    agents/importer.py (Part 0 §0.5) already reads every one of these
    formats back into the common artifact shape; this endpoint is just
    that plus process_upload("import", ...) (§2b: Source Manager, the
    one funnel every ingestion endpoint here now goes through instead
    of calling its own ingestor + write_ingested_source() by hand).
    PDF is deliberately absent from
    IMPORTABLE_FORMATS -- that's agents/pdf_ingestor.py's job, not
    agents/importer.py's (see that module's own docstring)."""
    ext = os.path.splitext(file.filename or "")[1].lstrip(".").lower()
    if ext not in IMPORTABLE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported import format '{ext}'. Supported: {', '.join(IMPORTABLE_FORMATS)}.",
        )
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        original_title = os.path.splitext(file.filename or "")[0] or None
        result = process_upload(
            "import", tmp_path, workspace_id, session_id=session_id,
            created_by="user", fmt=ext, default_title=original_title,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.remove(tmp_path)
    return {"node_ids": result["node_ids"], "title": result["title"]}


@router.post("/api/notes/pdf", dependencies=[Depends(require_auth)])
async def ingest_pdf_endpoint(
    workspace_id: str = Form(...), file: UploadFile = File(...),
    session_id: str | None = Form(None),   # NEW — §9c, see /api/notes/import's copy of this same field for why it's Form(...) here rather than a Pydantic field
):
    """PDF ingestion -- agents/pdf_ingestor.py (pdfplumber, page-by-page
    extraction) already exists and was fully implemented, it just had no
    endpoint calling it. PDF is deliberately absent from IMPORTABLE_FORMATS
    (see /api/notes/import above) -- this is that "other job", same
    temp-file-then-cleanup shape, funneled through process_upload()
    (§2b) like every other ingestion endpoint here."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        result = process_upload("pdf", tmp_path, workspace_id, session_id=session_id, created_by="user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.remove(tmp_path)
    return {"node_ids": result["node_ids"], "title": result["title"]}


@router.post("/api/notes/voice", dependencies=[Depends(require_auth)])
async def ingest_voice_endpoint(
    workspace_id: str = Form(...), file: UploadFile = File(...),
    session_id: str | None = Form(None),   # NEW — §9c, see /api/notes/import's copy of this same field for why it's Form(...) here rather than a Pydantic field
):
    """Voice notes / meeting recordings -- agents/voice_ingestor.py
    transcribes locally (faster-whisper, no API key), same temp-file-
    then-cleanup shape as /api/notes/import above. No format allowlist
    here: unlike Office import, faster-whisper/ffmpeg handles a broad
    range of audio containers, and an unsupported one already surfaces
    as ingest_voice()'s own ValueError -> 400 rather than needing a
    second check here."""
    suffix = os.path.splitext(file.filename or "")[1] or ".audio"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        result = process_upload("voice", tmp_path, workspace_id, session_id=session_id, created_by="user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.remove(tmp_path)
    return {"node_ids": result["node_ids"], "title": result["title"]}


# --- notes domain: generate (see agents/exporter.py, Part 0 §0.5 /
# graph/adapters.py, Part 4 §4.4) ------------------------------------------
# Turns a generator role's raw Markdown stage_output (mapper, report_writer,
# slide_planner, podcast_scriptwriter -- every one asks for headered
# Markdown via generic_worker.py's MARKDOWN_INSTRUCTION) into a real file.
# Takes the text straight from the client rather than re-reading it off
# the memory bus here: stage_output:* keys are app_slug-namespaced
# (memory/bus.py's _namespaced()), and the client already has the exact
# text it rendered to the user, so this sidesteps reconstructing that
# namespace server-side.

@router.post("/api/notes/export", dependencies=[Depends(require_auth)])
def export_artifact_endpoint(req: ExportArtifactRequest):
    fmt = req.fmt.lower().lstrip(".")
    if fmt not in EXPORTABLE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported export format '{fmt}'. Supported: {', '.join(EXPORTABLE_FORMATS)}.",
        )
    artifact = markdown_text_to_artifact(
        req.text, title_fallback=req.title,
        workspace_id=req.workspace_id, tags=req.tags,
    )
    path = export_artifact(artifact, fmt, NOTES_EXPORTS_DIR)
    return FileResponse(path, filename=os.path.basename(path))


# --- notes domain: podcast synthesis (see agents/tts_synthesizer.py,
# Part 4 §4.4) --------------------------------------------------------------
# Synthesis half of Audio Overview. Takes podcast_scriptwriter's raw
# Markdown stage_output straight from the client, same take-the-text-
# from-the-client reasoning export_artifact_endpoint above already uses —
# no re-read off the namespaced memory bus here either. No LLM call in
# this handler; synthesize_podcast() is pure edge-tts.

@router.post("/api/notes/podcast/synthesize", dependencies=[Depends(require_auth)])
def synthesize_podcast_endpoint(req: SynthesizePodcastRequest):
    safe_title = "".join(c for c in req.title if c.isalnum() or c in ("-", "_")) or "podcast"
    out_path = os.path.join(NOTES_EXPORTS_DIR, f"{safe_title}.mp3")
    try:
        synthesize_podcast(req.script_text, out_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(out_path, filename=os.path.basename(out_path))


# --- notes domain: Video Overview (see agents/video_overview_builder.py,
# Part 4 §4.4) ----------------------------------------------------------
# Labeled "narrated slideshow" in-product, not "video" -- see that
# module's docstring for why. Reuses slide_planner's own Markdown via the
# same markdown_text_to_artifact() adapter export_artifact_endpoint above
# already uses. The podcast audio is NOT re-synthesized here -- it's
# located on disk by `podcast_title`, the same safe-slugified filename
# synthesize_podcast_endpoint above already writes to NOTES_EXPORTS_DIR.
# This by-title lookup is a deliberate simplification (no session_id/
# workspace_id-keyed store for exports exists yet); call podcast
# synthesis first with a title, then pass that same title here.

@router.post("/api/notes/video-overview/build", dependencies=[Depends(require_auth)])
def build_video_overview_endpoint(req: BuildVideoOverviewRequest):
    safe_podcast_title = "".join(c for c in req.podcast_title if c.isalnum() or c in ("-", "_")) or "podcast"
    audio_path = os.path.join(NOTES_EXPORTS_DIR, f"{safe_podcast_title}.mp3")
    if not os.path.exists(audio_path):
        raise HTTPException(
            status_code=404,
            detail=(f"No synthesized podcast audio found for title {req.podcast_title!r}. "
                     "Call POST /api/notes/podcast/synthesize with this title first."),
        )
    slide_artifact = markdown_text_to_artifact(req.slide_text, title_fallback=req.title)
    safe_title = "".join(c for c in req.title if c.isalnum() or c in ("-", "_")) or "video_overview"
    out_path = os.path.join(NOTES_EXPORTS_DIR, f"{safe_title}.mp4")
    try:
        build_video_overview(slide_artifact, audio_path, out_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(out_path, filename=os.path.basename(out_path))

# --- notes domain: study tools (see eo/quiz_progress.py, Part 4 §4.5) -----
# flashcard_writer/study_guide_writer need no new endpoint -- both already
# use the '# Title' / '## Heading' grammar export_artifact_endpoint above
# already handles, same as report_writer. quiz_writer's output round-trips
# through that same endpoint too (its '- [ ]'/'- [x]' lines are just
# ordinary section content to markdown_text_to_artifact()) -- these
# endpoints only cover what export/import can't: grading a submission
# against quiz_writer's own Markdown and recording the result.

@router.post("/api/notes/study/quiz/grade", dependencies=[Depends(require_auth)])
def grade_quiz_endpoint(req: GradeQuizRequest):
    """Grades without persisting -- lets the frontend show results before
    committing an attempt (e.g. a "check my answers" button before final
    submit). POST .../attempts below does the same grading AND records
    it; this is the preview-only half."""
    return quiz_progress.grade_quiz(req.quiz_text, req.answers)


# NEW — step 6.7: what counts as "passed" for the purposes of marking a
# topic done. quiz_progress.grade_quiz()'s `percent` is 0-100 float;
# picked 70 as a conventional passing bar since nothing in the codebase
# already defines one (no PASSING/pass_threshold constant existed
# anywhere before this). Module-level so it's one place to tune later
# rather than a magic number inline in the endpoint below.
QUIZ_PASS_PERCENT = 70.0


@router.post("/api/notes/study/quiz/attempts", dependencies=[Depends(require_auth)])
def record_quiz_attempt_endpoint(req: RecordQuizAttemptRequest):
    attempt = quiz_progress.record_attempt(
        workspace_id=req.workspace_id,
        quiz_node_id=req.quiz_node_id,
        quiz_markdown=req.quiz_text,
        answers=req.answers,
        created_by="user",
    )

    # NEW — step 6.7: passing-quiz-attempt progress hook (see
    # eo/study_progress.py). Kept here at the caller, not inside
    # quiz_progress.record_attempt() itself, same reasoning as step
    # 6.6's hook -- that function stays a pure "grade and log the
    # attempt" helper with no study_progress side effects, and no
    # notification either (see step 6.5's PUT route docstring: the
    # Phase 4 push is reserved for the out-of-band hooks, this is one
    # of them, but wiring the actual emit is step 6.13, not this one).
    # req.topic_id is optional and best-effort (see its field comment
    # above) -- an attempt with no topic_id just can't drive this.
    # Only bumps toward "done": a topic already "done" doesn't get
    # touched again by a later re-attempt, matching 6.6's "don't
    # regress a further-along status" posture.
    if req.topic_id and attempt["percent"] >= QUIZ_PASS_PERCENT:
        try:
            current = study_progress.get_progress(req.workspace_id, req.topic_id)
            if current["status"] != study_progress.STATUS_DONE:
                study_progress.set_progress(req.workspace_id, req.topic_id,
                                             status=study_progress.STATUS_DONE)
        except Exception:
            # Best-effort, same "never let this be the reason the
            # attempt-recording call errors" posture as 6.6's hook.
            pass

    return attempt


@router.get("/api/notes/study/quiz/attempts", dependencies=[Depends(require_auth)])
def list_quiz_attempts_endpoint(workspace_id: str = Query(...),
                                 quiz_node_id: str | None = Query(None)):
    return quiz_progress.list_attempts(workspace_id, quiz_node_id)


@router.get("/api/notes/study/quiz/missed", dependencies=[Depends(require_auth)])
def missed_quiz_questions_endpoint(workspace_id: str = Query(...),
                                    quiz_node_id: str = Query(...)):
    return quiz_progress.get_missed_questions(workspace_id, quiz_node_id)


# B6 — /api/task*, /api/resume, /api/roles*, /api/workflow-templates*
# moved to api/routes/tasks.py; /api/health, /api/quota, /api/usage/history
# moved to api/routes/system.py. Both wired in via app.include_router()
# near the CORS setup above. See those files for the actual route code.
#
# B6 piece 6 cleanup — /api/tasks/{session_id} and /api/tasks/workspace/
# {ws_id} (plus the _parse_fenced_json and _sentry_status helpers they
# alone used) also moved to api/routes/tasks.py, closing out the gap
# left when piece 1 was originally split. _parse_marketplace_reviews
# below stays here -- it's only used by /api/workspaces/{ws_id}/simulate
# (piece 7, not yet moved) -- and is otherwise unchanged.


def _parse_marketplace_reviews(text):
    """marketplace_review_batch (Part 1 §1.4 track 2) is a generic_worker
    role whose own ROLE_PROMPTS_SEED brief instructs it to emit a single
    fenced ```json code block containing a bare array of
    {"rating", "sentiment", "text"} objects -- not the {"integrations":
    [...]} wrapper shape _parse_fenced_json (now in api/routes/tasks.py)
    expects, since that's what THIS role's own brief specifies. Same
    strip-the-fence-then-json.loads approach, same "[] on anything
    unparseable, never an error state" posture as _parse_fenced_json.
    """
    if not text:
        return []
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1) if match else text
    try:
        parsed = json.loads(raw.strip())
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, AttributeError):
        return []

