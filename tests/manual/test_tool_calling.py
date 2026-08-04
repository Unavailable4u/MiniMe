"""
tests/manual/test_tool_calling.py — moved from scripts/test_tool_calling.py
(B1 manual-tier migration; originally Notebooks Chat-First refinement,
Phase 2 step 2.3).

Isolated test harness: sends a handful of canned user messages, plus the
`tools` array built from the Phase 1 capability manifest (via
utils/capability_tools.manifest_to_tools), to the real Groq API and
checks the raw tool-call response for consistency across repeated runs.
Hits a live LLM, so this lives in tests/manual/ and is never run in CI
-- see pytest.ini's `manual` marker.

Deliberately does NOT touch the real send path (WorkspaceChatPanel.jsx /
sendTask()) -- that's step 2.5/2.6. FIXTURE_MANIFEST below is a
deliberately kept-separate fixture, not the real manifest (contrast
with test_capability_coverage.py, which uses a hand-synced copy of the
real one) -- this file's job is tuning tool-call classification in
isolation, not validating against production shape.

Records/replays via a vcrpy cassette (tests/manual/cassettes/ -- see the
README.md there for the full recording workflow) instead of re-hitting
the live API and re-spending REPEATS x len(TEST_CASES) calls every run.
First run needs a real GROQ_API_KEY to record; every run after that
replays the cassette and needs no key and no network call at all.

Usage:
    # first time (or to refresh a stale cassette):
    GROQ_API_KEY=your_key_here pytest tests/manual/test_tool_calling.py -v -s
    # every run after that -- no key needed, replays the cassette:
    pytest tests/manual/test_tool_calling.py -v -s
    (add TOOL_TEST_REPEATS=N to change repeats per message, default 3)
"""
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import vcr
from openai import OpenAI, APIStatusError

from utils.capability_tools import manifest_to_tools

pytestmark = pytest.mark.manual

CASSETTE_DIR = Path(__file__).parent / "cassettes"
CASSETTE_NAME = "test_tool_calling_classification_coverage.yaml"

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
# TEMP fixture manifest -- replace with the real manifest once you can
# import it directly (e.g. from a shared module, or by hitting your local
# GET /api/capabilities and pasting the JSON here). This is just enough
# shape to exercise manifest_to_tools() end to end.
# --------------------------------------------------------------------------
FIXTURE_MANIFEST = [
    {
        "key": "flashcards",
        "description": "Generate a set of flashcards from the user's sources.",
        "scopeAllowed": "sources",
    },
    {
        "key": "quiz",
        "description": "Generate a quiz to test understanding of the sources.",
        "scopeAllowed": "sources",
    },
    {
        "key": "study_guide",
        "description": "Generate a written study guide summarizing the sources.",
        "scopeAllowed": "sources",
    },
    {
        "key": "mindmap",
        "description": "Build a mind map of the topics across all sources.",
        "scopeAllowed": "whole",
    },
    {
        "key": "clusters",
        "description": "Cluster the sources into related topic groups.",
        "scopeAllowed": "whole",
    },
    {
        "key": "workflow",
        "description": (
            "Build an ordered, step-by-step study plan or learning "
            "sequence for one specific topic -- the steps a learner "
            "should work through to master it, shown as a checklist "
            "and flowchart. Use this for 'study plan', 'where do I "
            "start', 'learning path', or 'how should I approach this "
            "topic' style requests about a single named topic. This is "
            "different from the study guide tool, which produces a "
            "written prose summary rather than an ordered sequence of "
            "steps."
        ),
        "scopeAllowed": "topic",
    },
    {
        "key": "podcast",
        "description": "Generate a podcast-style audio overview (not yet available).",
        "scopeAllowed": "whole",
        "disabled": True,  # Phase 5 stub -- must NOT appear in the tools array
    },
]

# --------------------------------------------------------------------------
# Canned test messages -- mix clear-intent, ambiguous, and no-match
# phrasings on purpose.
#
# `expected` is the tool name we WANT to see (None means "no tool call is
# correct"). Used only to flag mismatches in the summary below -- it's not
# fed to the model.
# --------------------------------------------------------------------------
TEST_CASES = [
    ("Can you make me some flashcards for this chapter?", "generate_flashcards"),
    ("Quiz me on what I just read.", "generate_quiz"),
    ("Give me a summary I can study from.", "generate_study_guide"),
    ("Show me how all these topics connect.", "generate_mindmap"),
    # clusters coverage -- nothing in the original set targeted this tool
    ("Can you group my sources into related topic clusters?", "generate_clusters"),
    ("Sort these sources into buckets by topic.", "generate_clusters"),
    # workflow coverage -- also the only "topic" scope, required topic_id
    # arg was never exercised before. Naming a topic explicitly so we can
    # check whether the model actually fills topic_id.
    ("Build me a step-by-step study workflow for photosynthesis.", "generate_workflow"),
    ("What's a good study plan for the chapter on cellular respiration?", "generate_workflow"),
    ("What should I do next?", None),              # ambiguous -- should clarify, not guess
    ("Can you make me a podcast about this?", None),  # disabled, should NOT match
    ("What's the weather like today?", None),       # should NOT match anything
]

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2  # 2, 4, 8, 16...

MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = (
    "You are the assistant for a study workspace app. You have tools "
    "that generate study materials (flashcards, quizzes, study guides, "
    "mind maps, topic clusters, workflows) from the sources currently "
    "in the user's workspace.\n\n"
    "Only call a tool when the user is clearly asking for one of these "
    "specific study materials to be generated. If the request doesn't "
    "match any tool -- including requests for things that sound similar "
    "but aren't offered (e.g. a podcast/audio overview), small talk, "
    "or anything unrelated to the workspace (e.g. the weather) -- do "
    "NOT call a tool. Just reply normally in plain text: say what you "
    "can help with instead, or ask a clarifying question.\n\n"
    "Call at most one tool per turn. If a request could reasonably map "
    "to more than one tool (e.g. 'what should I do next'), don't call "
    "any of them -- ask the user which one they want instead."
)


def _is_malformed_tool_call_error(err: APIStatusError) -> bool:
    return err.status_code == 400 and "tool_use_failed" in str(getattr(err, "body", "") or err)


def _call_with_retries(client: OpenAI, **kwargs) -> Any:
    """
    Wraps client.chat.completions.create(...) so a single flaky call
    doesn't crash the whole run. Two known failure modes to survive:

    1. Transient Groq-side 5xx ("over capacity") -- retry with backoff,
       this reliably clears within a few seconds/minutes.
    2. Llama-3.3-via-Groq sometimes emits its own native
       "<function=name>(...)</function>" text syntax instead of a
       structured tool call, and Groq's API rejects that with a 400
       "tool_use_failed" before it ever reaches our code. Seen so far
       specifically on generate_mindmap/generate_clusters -- the same
       empty-parameters ("whole" scope) tools already flagged for the
       null-args quirk, so this looks like the same root cause (model
       struggles to format a call with nothing to fill in), just a
       more severe failure mode. This is worth retrying a couple times
       (it doesn't reproduce every run), but if it keeps happening
       that's real signal, not noise -- so we raise a distinct
       exception rather than silently swallowing it forever.
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except APIStatusError as err:
            last_err = err
            is_malformed_tool_call = (
                err.status_code == 400
                and "tool_use_failed" in str(getattr(err, "body", "") or err)
            )
            if err.status_code in (429, 500, 502, 503, 504) or is_malformed_tool_call:
                if attempt == MAX_RETRIES:
                    break
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                kind = "malformed tool-call format" if is_malformed_tool_call else f"HTTP {err.status_code}"
                print(f"    (retry {attempt}/{MAX_RETRIES} after {kind}, waiting {wait}s...)")
                time.sleep(wait)
                continue
            raise  # non-retryable error, let it propagate
    raise last_err


@pytest.mark.skipif(
    not (os.getenv("GROQ_API_KEY") or _cassette_exists()),
    reason="GROQ_API_KEY not set and no recorded cassette found -- see tests/manual/cassettes/README.md",
)
@my_vcr.use_cassette(CASSETTE_NAME)
def test_tool_calling_classification_coverage(monkeypatch):
    # A real key is only required to RECORD (cassette missing); once a
    # cassette exists, replay needs a syntactically-valid key but never
    # actually sends it anywhere -- vcrpy intercepts the request first.
    if not os.getenv("GROQ_API_KEY"):
        monkeypatch.setenv("GROQ_API_KEY", "sk-cassette-replay-placeholder")
    api_key = os.environ["GROQ_API_KEY"]
    repeats = int(os.environ.get("TOOL_TEST_REPEATS", "3"))
    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")

    tools = manifest_to_tools(FIXTURE_MANIFEST)
    enabled_names = [t["function"]["name"] for t in tools]
    print(f"Built {len(tools)} tools from manifest: {enabled_names}")
    print(f"Repeats per message: {repeats} (set TOOL_TEST_REPEATS to change)\n")

    # message -> list of per-run outcome dicts, for the summary table
    results: Dict[str, List[Dict[str, Any]]] = {}

    for message, expected in TEST_CASES:
        results[message] = []
        print("=" * 70)
        print(f"USER: {message}  (expected: {expected or 'no tool call'})")

        for run in range(1, repeats + 1):
            try:
                response = _call_with_retries(
                    client,
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": message},
                    ],
                    tools=tools,
                    tool_choice="auto",
                )
            except APIStatusError as err:
                if _is_malformed_tool_call_error(err):
                    print(f"  [run {run}] -> MALFORMED TOOL CALL (model emitted native "
                          f"<function=...> text instead of a structured call; Groq "
                          f"rejected it): {err}")
                    results[message].append({
                        "names": [], "null_args": False, "missing_required": False,
                        "malformed": True,
                    })
                    continue
                # Genuinely unrecoverable (retries exhausted, or a
                # non-retryable status) -- still don't crash the whole
                # test run over one message; log and move on.
                print(f"  [run {run}] -> API ERROR (giving up after retries): {err}")
                results[message].append({
                    "names": [], "null_args": False, "missing_required": False,
                    "malformed": False, "api_error": True,
                })
                continue

            choice = response.choices[0].message
            tool_calls = choice.tool_calls or []

            if not tool_calls:
                print(f"  [run {run}] -> NO TOOL CALL. Model said: {choice.content!r}")
                results[message].append({"names": [], "null_args": False, "missing_required": False})
                continue

            if len(tool_calls) > 1:
                # Multiple simultaneous calls = low-confidence classification.
                # Step 2.5/2.6 dispatch should treat this the same as "no
                # tool call" and fall through to sendTask(), not execute all
                # of them.
                names_preview = [c.function.name for c in tool_calls]
                print(f"  [run {run}] -> AMBIGUOUS: {len(tool_calls)} calls at once: {names_preview}")
                print("            (treat as low-confidence; fall through to sendTask())")

            run_names = []
            run_null_args = False
            run_missing_required = False

            for call in tool_calls:
                run_names.append(call.function.name)
                raw_args = call.function.arguments
                try:
                    # Empty-properties schemas ("whole" scope) can come back
                    # as the literal string "null" rather than "{}" --
                    # normalize so downstream args.get(...) doesn't blow up.
                    args = json.loads(raw_args) if raw_args and raw_args != "null" else {}
                    if args is None:
                        args = {}
                    if raw_args in (None, "", "null") or args == {}:
                        # Only meaningful as a "null-ish" flag for tools
                        # that actually take no required args ("whole"
                        # scope); for "topic"/"sources" scope this is
                        # covered by the missing_required check below.
                        run_null_args = raw_args in (None, "", "null")
                    print(f"  [run {run}] -> TOOL CALL: {call.function.name}  args: {json.dumps(args)}")

                    if call.function.name == "generate_workflow" and "topic_id" not in args:
                        run_missing_required = True
                        print("            !! missing required topic_id")
                except json.JSONDecodeError:
                    print(f"  [run {run}] -> TOOL CALL: {call.function.name}  args (unparsed): {raw_args!r}")

            results[message].append({
                "names": run_names,
                "null_args": run_null_args,
                "missing_required": run_missing_required,
            })

    # ----------------------------------------------------------------
    # Summary: per message, how consistent was the model across repeats
    # runs, and did it ever diverge from the expected tool?
    # ----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    failing_messages = []
    for message, expected in TEST_CASES:
        runs = results[message]
        clean_runs = [r for r in runs if not r.get("malformed") and not r.get("api_error")]
        outcomes = [tuple(r["names"]) or ("<none>",) for r in clean_runs]
        consistent = len(set(outcomes)) <= 1  # vacuously consistent if all runs failed
        got = outcomes[0][0] if outcomes and outcomes[0] != ("<none>",) else None
        matches_expected = (got == expected) if expected else (got is None) if outcomes else True
        null_args_seen = any(r["null_args"] for r in clean_runs)
        missing_required_seen = any(r["missing_required"] for r in clean_runs)
        malformed_count = sum(1 for r in runs if r.get("malformed"))
        api_error_count = sum(1 for r in runs if r.get("api_error"))

        flags = []
        if not consistent:
            flags.append("INCONSISTENT ACROSS RUNS")
        if not matches_expected:
            flags.append(f"MISMATCH (expected {expected or 'no tool call'}, got {got or 'no tool call'})")
        if null_args_seen:
            flags.append("null-args seen")
        if missing_required_seen:
            flags.append("missing required arg seen")
        if malformed_count:
            flags.append(f"MALFORMED TOOL CALL x{malformed_count}/{repeats} "
                          "(Llama emitted native <function=...> text; Groq rejected it)")
        if api_error_count:
            flags.append(f"API ERROR (unretried) x{api_error_count}/{repeats}")

        status = "OK" if not flags else " / ".join(flags)
        # null-args alone is a known, already-handled quirk (see the
        # normalization shim note below) -- not itself a failure signal.
        if flags and any(k in status for k in
                          ("INCONSISTENT", "MISMATCH", "missing required",
                           "MALFORMED", "API ERROR")):
            failing_messages.append(f"{message!r}: {status}")

        print(f"- {message!r}")
        print(f"    runs: {outcomes}")
        print(f"    status: {status}")

    print("=" * 70)
    assert not failing_messages, (
        "Tool-call classification coverage gap(s) found "
        "(null-args alone, if seen above, still just needs the "
        "normalization shim in the real dispatcher and isn't counted "
        "as a failure here):\n" + "\n".join(failing_messages)
    )
