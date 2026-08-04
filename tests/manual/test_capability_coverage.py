"""
tests/manual/test_capability_coverage.py — moved from
scripts/test_capability_coverage.py (B1 manual-tier migration;
originally Notebooks Chat-First refinement, Phase 2 step 2.8:
"Test 5-10 phrasings per capability; log misfires.").

Hits a live LLM via utils.llm_client.classify_tool_intent(), so this
lives in tests/manual/ and is never run in CI -- see pytest.ini's
`manual` marker.

FIXED as a followup to the B1 migration: TEST_CASES had most of its
content commented out in the original scripts/ version -- only
"study_guide" (5 phrasings) plus the 2 generate_workflow
regression-check cases were active; the other 7 capability categories
(clusters, facts, suggested_notes, study_flashcards, study_quiz,
mindmap, podcast, video_overview) had ZERO live coverage, just
commented-out placeholders, against this file's own "5-10 phrasings per
capability" goal. All of it is now uncommented and active -- 40 phrasings
across the 8 enabled categories (5 each) plus the 2 regression-check
cases, 42 total. The no-match/edge-case pair ("What should I do next?",
"What's the weather like today?") that was also commented out is
restored too.

Two things distinguish this from test_tool_calling.py (steps 2.3/2.4):

  1. REAL_MANIFEST below is a hand-kept-in-sync copy of api/server.py's
     CAPABILITIES_MANIFEST -- not test_tool_calling.py's FIXTURE_MANIFEST.
     The two have drifted apart: the fixture used "flashcards"/"quiz" as
     keys and included "workflow" as a live (enabled) tool, but the real
     manifest uses "study_flashcards"/"study_quiz", adds "facts" and
     "suggested_notes" (never covered by the original ~10 test messages),
     and (as of Phase 5 step 5.7) "podcast"/"video_overview" are now
     live/enabled too, matching the fixture's naming for those two --
     "workflow" is the one that's still disabled (endpoint: None) here,
     not a real tool at all yet. Testing against the fixture would
     validate a tool list that doesn't match what actually ships.

  2. This calls utils.llm_client.classify_tool_intent() directly -- the
     exact function api/server.py's POST .../notebooks/classify-intent
     (step 2.5) and WorkspaceChatPanel.jsx's tryHandleClassifiedToolCall()
     (step 2.6) run in production -- rather than a separate hand-rolled
     OpenAI client call. 2.3/2.4 deliberately bypassed the real code (see
     that script's own header comment, "isolate classification behavior
     from chain-switching behavior"); by step 2.8 that real code exists
     and is what needs coverage, not a stand-in for it.

Does NOT import api/server.py directly to get CAPABILITIES_MANIFEST --
that module pulls in FastAPI, Supabase, Pusher, and starts wiring up
lifespan/CORS/etc. at import time, none of which this file needs or
wants side effects from. REAL_MANIFEST is hand-copied instead, same
"deliberately hand-kept in sync, nothing enforces it automatically"
tradeoff api/server.py's own CAPABILITIES_MANIFEST comment already
accepts for its relationship to notebookCapabilities.js.

Records/replays via a vcrpy cassette (tests/manual/cassettes/ -- see the
README.md there for the full recording workflow) instead of re-hitting
the live API and re-spending REPEATS x len(TEST_CASES) calls every run.
First run needs a real GROQ_API_KEY to record; every run after that
replays the cassette and needs no key and no network call at all.

Usage:
    # first time (or to refresh a stale cassette):
    GROQ_API_KEY=your_key_here pytest tests/manual/test_capability_coverage.py -v -s
    # every run after that -- no key needed, replays the cassette:
    pytest tests/manual/test_capability_coverage.py -v -s
    (add TOOL_TEST_REPEATS=N to change repeats per message, default 3)
"""
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import vcr

from utils.capability_tools import manifest_to_tools
from utils.llm_client import classify_tool_intent

pytestmark = pytest.mark.manual

CASSETTE_DIR = Path(__file__).parent / "cassettes"
CASSETTE_NAME = "test_capability_coverage_classification.yaml"

my_vcr = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    record_mode="once",
    match_on=["method", "uri", "body"],
    filter_headers=["authorization"],
    # See cassettes/README.md -- lets every repeat of an identical
    # message reuse the same recorded interaction instead of requiring
    # one distinct recording per repeat.
    allow_playback_repeats=True,
)


def _cassette_exists() -> bool:
    return (CASSETTE_DIR / CASSETTE_NAME).exists()


# --------------------------------------------------------------------------
# Hand-copied from api/server.py's CAPABILITIES_MANIFEST as of Phase 2 step
# 2.6. Keep in sync by hand if that manifest changes -- see module
# docstring above for why this isn't imported directly.
#
# Re-synced as of the Phase 2 step 2.4 revisit (Phase 5 step 5.8
# finding): suggested_notes/study_quiz/study_guide/mindmap/video_overview
# descriptions were tightened to name the specific phrasings that
# misfired in a real 5.8 test run and to disambiguate against their
# confusable neighbors (see api/server.py's own per-entry comments for
# the reasoning behind each change).
# --------------------------------------------------------------------------
REAL_MANIFEST = [
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
        "key": "suggested_notes", "label": "Suggested notes", "subTab": "insights",
        "description": "Scan the sources for note-worthy passages and propose draft notes the user can accept or discard. Use this for requests like 'suggest some notes', 'what should I take notes on', or 'find things worth noting' -- not for pulling out standalone facts (see the facts tool) or grouping sources by topic (see the clusters tool).",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "study_flashcards", "label": "Flashcards", "subTab": "study",
        "description": "Generate a set of question/answer flashcards for studying the selected scope.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "study_quiz", "label": "Quiz", "subTab": "study",
        "description": "Generate a graded quiz covering the selected scope, which the user can take and submit for scoring. Use this whenever the user wants to be quizzed or tested on the material -- e.g. 'quiz me', 'test my understanding', 'test me on this' -- even if they don't use the word 'quiz'.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "study_guide", "label": "Study guide", "subTab": "study",
        "description": "Produce a structured written study guide summarizing and organizing the selected scope for review. Use this for requests for a prose summary or write-up to study from -- e.g. 'give me a summary I can study from', 'write me a summary', 'summarize this for review' -- as opposed to a visual mind map (see the mindmap tool) or a list of standalone facts (see the facts tool). Do NOT use this for 'a step-by-step study workflow' or 'a study plan' requests -- those ask for an ordered sequence of steps, not a written summary, and no tool for that exists yet, so don't call anything for them.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        "key": "mindmap", "label": "Mind map", "subTab": "diagrams",
        "description": "Build a visual mind map of the concepts in the selected scope and how they relate to each other. Use this for requests to see or map out how topics/concepts connect or relate -- e.g. 'map out the connections between these topics', 'show me how these relate' -- as opposed to grouping sources into topic buckets (see the clusters tool).",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/generate",
        "enabled": True,
    },
    {
        # CHANGED — Phase 5 step 5.7: kept in sync with
        # api/server.py's CAPABILITIES_MANIFEST flip -- endpoint now
        # points at the dedicated route, enabled: True.
        "key": "podcast", "label": "Podcast", "subTab": "insights",
        "description": "Generate a two-host audio podcast episode discussing the selected scope.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/podcast",
        "enabled": True,
    },
    {
        # CHANGED — Phase 5 step 5.7: same sync as "podcast" above.
        "key": "video_overview", "label": "Video overview", "subTab": "insights",
        "description": "Generate a narrated video overview summarizing the selected scope -- a short explainer/walkthrough video. Use this for requests like 'video overview', 'video summary', 'explainer video', or 'video walkthrough'.",
        "scopeAllowed": "whole", "endpoint": "POST /api/workspaces/{ws_id}/notebooks/video_overview",
        "enabled": True,
    },
    {
        "key": "workflow", "label": "Workflow", "subTab": "diagrams",
        "description": "Build a step-by-step study workflow for a single topic.",
        "scopeAllowed": "topic", "endpoint": None,
        "enabled": False,
    },
]

# --------------------------------------------------------------------------
# 5-10 phrasings per ENABLED capability (the guide's step 2.8 ask), plus a
# no-match/edge-case block at the end. `expected` is the tool name we WANT
# ("generate_<key>") or None for "no tool call is correct" -- not fed to
# the model, only used to flag mismatches below.
#
# Restored to full coverage as a B1 followup -- see module docstring.
# --------------------------------------------------------------------------
TEST_CASES: List[tuple] = [

    # --- clusters ---
    ("Can you group my sources into related topic clusters?", "generate_clusters"),
    ("Sort these sources into buckets by topic.", "generate_clusters"),
    ("Organize my notes by theme.", "generate_clusters"),
    ("Cluster everything I've uploaded by subject.", "generate_clusters"),
    ("Group similar sources together.", "generate_clusters"),

    # --- facts ---
    ("Pull out the key facts from these sources.", "generate_facts"),
    ("Give me a list of standalone facts I can cite.", "generate_facts"),
    ("What are the citable facts in my sources?", "generate_facts"),
    ("Extract factual statements from this material.", "generate_facts"),
    ("List out concrete facts from what I've uploaded.", "generate_facts"),

    # --- suggested_notes ---
    ("Scan my sources for anything worth taking notes on.", "generate_suggested_notes"),
    ("Suggest some notes based on what's in my sources.", "generate_suggested_notes"),
    ("Find passages that are worth noting down.", "generate_suggested_notes"),
    ("Propose some draft notes from my sources.", "generate_suggested_notes"),
    ("What should I be taking notes on here?", "generate_suggested_notes"),

    # --- study_flashcards ---
    ("Can you make me some flashcards for this chapter?", "generate_study_flashcards"),
    ("I need flashcards to study from.", "generate_study_flashcards"),
    ("Turn my notes into flashcards.", "generate_study_flashcards"),
    ("Make Q&A cards out of this material.", "generate_study_flashcards"),
    ("Give me flashcards covering these sources.", "generate_study_flashcards"),

    # --- study_quiz ---
    ("Quiz me on what I just read.", "generate_study_quiz"),
    ("Give me a quiz to test my understanding.", "generate_study_quiz"),
    ("Can you test me on this material?", "generate_study_quiz"),
    ("Make a graded quiz from my sources.", "generate_study_quiz"),
    ("I want to quiz myself on this chapter.", "generate_study_quiz"),

    # --- study_guide ---
    ("Give me a summary I can study from.", "generate_study_guide"),
    ("Write me a study guide for this material.", "generate_study_guide"),
    ("Summarize everything into something I can review.", "generate_study_guide"),
    ("Can you put together a study guide?", "generate_study_guide"),
    ("I need a written summary to study from.", "generate_study_guide"),

    # --- mindmap ---
    ("Show me how all these topics connect.", "generate_mindmap"),
    ("Build a mind map of these concepts.", "generate_mindmap"),
    ("Visualize how these ideas relate to each other.", "generate_mindmap"),
    ("Map out the connections between these topics.", "generate_mindmap"),
    ("Give me a concept map of this material.", "generate_mindmap"),

    # --- podcast (Phase 5 step 5.7 enabled this tool; step 5.8 is this
    # coverage block) ---
    ("Can you make me a podcast about this?", "generate_podcast"),
    ("Turn my sources into a podcast episode.", "generate_podcast"),
    ("I'd rather listen than read -- make an audio podcast of this.", "generate_podcast"),
    ("Generate a two-host discussion of this material.", "generate_podcast"),
    ("Make a podcast episode covering these sources.", "generate_podcast"),

    # --- video_overview (Phase 5 step 5.7/5.8, same as podcast) ---
    ("Can you make a video overview of this?", "generate_video_overview"),
    ("I want a narrated video summarizing these sources.", "generate_video_overview"),
    ("Turn this into a short explainer video.", "generate_video_overview"),
    ("Give me a video walkthrough of this material.", "generate_video_overview"),
    ("Generate a narrated video summary of my notes.", "generate_video_overview"),

    # --- no-match / edge cases ---
    ("What should I do next?", None),                         # ambiguous -- should clarify, not guess
    ("What's the weather like today?", None),                  # unrelated to the app
    # REGRESSION CHECK, step 2.6 fallout: these two phrasings were
    # generate_workflow's coverage cases back in the 2.3/2.4 fixture,
    # where workflow was a live tool. In the REAL manifest workflow is
    # disabled (endpoint: None) -- there is currently no "study plan"
    # capability at all. The risk isn't "does it call generate_workflow"
    # (that tool isn't even offered), it's whether the model instead
    # misfires onto study_guide or mindmap for a "study plan" request
    # that doesn't actually match either one's description.
    ("Build me a step-by-step study workflow for photosynthesis.", None),
    ("What's a good study plan for the chapter on cellular respiration?", None),
]

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2


def _classify_with_retries(message: str, tools: list) -> Dict[str, Any]:
    """
    classify_tool_intent() itself is deliberately single-shot / fail-open
    (see its docstring in utils/llm_client.py) -- a real chat send should
    never retry-and-block on this. A coverage *test* run, on the other
    hand, wants to tell "the model is genuinely unreliable here" apart
    from "one request had a transient hiccup" -- so retry HERE, at the
    harness level, on a returned `error`, without changing the production
    function's own no-retry behavior at all.
    """
    last_result = None
    for attempt in range(1, MAX_RETRIES + 1):
        result = classify_tool_intent(message, tools)
        if not result.get("error"):
            return result
        last_result = result
        if attempt == MAX_RETRIES:
            break
        wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        print(f"    (retry {attempt}/{MAX_RETRIES} after error {result['error']!r}, waiting {wait}s...)")
        time.sleep(wait)
    return last_result


@pytest.mark.skipif(
    not (os.getenv("GROQ_API_KEY") or _cassette_exists()),
    reason="GROQ_API_KEY not set and no recorded cassette found -- see tests/manual/cassettes/README.md",
)
@my_vcr.use_cassette(CASSETTE_NAME)
def test_capability_coverage_classification(monkeypatch):
    # classify_tool_intent() -> _get_groq() reads GROQ_API_KEY directly
    # and bails out early with an "error" result if it's unset, before
    # ever reaching the (vcrpy-intercepted) HTTP call -- so replay needs
    # a syntactically-valid key present even though it's never actually
    # sent anywhere once vcrpy intercepts the request.
    if not os.getenv("GROQ_API_KEY"):
        monkeypatch.setenv("GROQ_API_KEY", "sk-cassette-replay-placeholder")
    repeats = int(os.environ.get("TOOL_TEST_REPEATS", "3"))

    tools = manifest_to_tools(REAL_MANIFEST)
    enabled_names = [t["function"]["name"] for t in tools]
    print(f"Built {len(tools)} tools from REAL_MANIFEST: {enabled_names}")
    # "generate_podcast"/"generate_video_overview" are enabled, real
    # tools as of Phase 5 step 5.7, so their presence here is expected,
    # not a leak. "generate_workflow" is the one still-disabled stub
    # left to guard against (endpoint: None in REAL_MANIFEST above).
    assert "generate_workflow" not in enabled_names, (
        "disabled capability leaked into the tools array -- see "
        "utils/capability_tools.py's _is_enabled()"
    )
    print(f"Repeats per message: {repeats} (set TOOL_TEST_REPEATS to change)\n")

    results: Dict[str, List[Dict[str, Any]]] = {}

    for message, expected in TEST_CASES:
        results[message] = []
        print("=" * 70)
        print(f"USER: {message}  (expected: {expected or 'no tool call'})")

        for run in range(1, repeats + 1):
            result = _classify_with_retries(message, tools)

            if result.get("error"):
                print(f"  [run {run}] -> ERROR (unretried): {result['error']}")
                results[message].append({"name": None, "ambiguous": False, "error": True})
                continue

            if result["ambiguous"]:
                print(f"  [run {run}] -> AMBIGUOUS: multiple simultaneous calls "
                      "(treated as no-match, falls through to sendTask())")
                results[message].append({"name": None, "ambiguous": True, "error": False})
                continue

            if not result["tool_calls"]:
                print(f"  [run {run}] -> NO TOOL CALL. Model said: {result['content']!r}")
                results[message].append({"name": None, "ambiguous": False, "error": False})
                continue

            call = result["tool_calls"][0]
            print(f"  [run {run}] -> TOOL CALL: {call['name']}  args: {call['arguments']}")
            results[message].append({"name": call["name"], "ambiguous": False, "error": False})

    # ----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    failing_messages = []
    for message, expected in TEST_CASES:
        runs = results[message]
        clean_runs = [r for r in runs if not r["error"]]
        outcomes = [r["name"] for r in clean_runs]
        consistent = len(set(outcomes)) <= 1
        got = outcomes[0] if outcomes else None
        matches_expected = (got == expected) if clean_runs else True
        error_count = sum(1 for r in runs if r["error"])
        ambiguous_count = sum(1 for r in runs if r["ambiguous"])

        flags = []
        if not consistent:
            flags.append("INCONSISTENT ACROSS RUNS")
        if not matches_expected:
            flags.append(f"MISFIRE (expected {expected or 'no tool call'}, got {got or 'no tool call'})")
        if ambiguous_count:
            flags.append(f"ambiguous x{ambiguous_count}/{repeats}")
        if error_count:
            flags.append(f"ERROR x{error_count}/{repeats} (after retries)")

        status = "OK" if not flags else " / ".join(flags)
        if any(k in status for k in ("INCONSISTENT", "MISFIRE", "ERROR")):
            failing_messages.append(f"{message!r}: {status}")

        print(f"- {message!r}")
        print(f"    runs: {outcomes}")
        print(f"    status: {status}")

    print("=" * 70)
    assert not failing_messages, (
        "Misfire(s) or instability found:\n" + "\n".join(failing_messages)
    )
