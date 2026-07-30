"""
Notebooks Chat-First refinement, Phase 2 step 2.3.

Isolated test harness: sends a handful of canned user messages, plus the
`tools` array built from the Phase 1 capability manifest (via
utils/capability_tools.manifest_to_tools), to the LLM and logs the raw
tool-call response.

Deliberately does NOT touch the real send path (WorkspaceChatPanel.jsx /
sendTask()) -- that's step 2.5/2.6. This script is scratch-only, meant to
be iterated on for step 2.4 (tune descriptions/prompt against ~10 test
messages until classification looks reliable).

Usage (PowerShell):
    $env:GROQ_API_KEY = "your_key_here"
    python scripts/test_tool_calling.py

Usage (bash):
    export GROQ_API_KEY=your_key_here
    python scripts/test_tool_calling.py

Requires: pip install openai
(Groq exposes an OpenAI-compatible endpoint, so the standard `openai`
client works against it -- just point base_url at Groq.)
"""

import json
import os
import sys
from pathlib import Path

# Running this file directly (python scripts/test_tool_calling.py) puts
# only scripts/ on sys.path, not the project root -- so `utils` wouldn't
# be importable otherwise. Add the repo root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from openai import OpenAI
except ImportError:
    print(
        "Missing dependency. Run: pip install openai\n"
        "(or: pip install openai --break-system-packages, if your "
        "environment requires it)",
        file=sys.stderr,
    )
    sys.exit(1)

from utils.capability_tools import manifest_to_tools


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
        "description": "Build a step-by-step study workflow for one topic.",
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
# Canned test messages -- start here for 2.3, expand toward ~10 for 2.4.
# Mix clear-intent, ambiguous, and no-match phrasings on purpose.
# --------------------------------------------------------------------------
TEST_MESSAGES = [
    "Can you make me some flashcards for this chapter?",
    "Quiz me on what I just read.",
    "Give me a summary I can study from.",
    "Show me how all these topics connect.",
    "What should I do next?",              # ambiguous
    "Can you make me a podcast about this?",  # should NOT match (disabled)
    "What's the weather like today?",      # should NOT match anything
]

MODEL = "llama-3.3-70b-versatile"


def main() -> None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print(
            "GROQ_API_KEY is not set in this shell.\n"
            "PowerShell:  $env:GROQ_API_KEY = \"your_key_here\"\n"
            "bash:        export GROQ_API_KEY=your_key_here",
            file=sys.stderr,
        )
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")

    tools = manifest_to_tools(FIXTURE_MANIFEST)
    enabled_names = [t["function"]["name"] for t in tools]
    print(f"Built {len(tools)} tools from manifest: {enabled_names}\n")

    for message in TEST_MESSAGES:
        print("=" * 70)
        print(f"USER: {message}")

        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": message}],
            tools=tools,
            tool_choice="auto",
        )

        choice = response.choices[0].message
        tool_calls = choice.tool_calls or []

        if not tool_calls:
            print(f"-> NO TOOL CALL. Model said: {choice.content!r}")
            continue

        for call in tool_calls:
            print(f"-> TOOL CALL: {call.function.name}")
            try:
                args = json.loads(call.function.arguments)
                print(f"   args: {json.dumps(args, indent=2)}")
            except json.JSONDecodeError:
                print(f"   args (raw, failed to parse): {call.function.arguments!r}")

    print("=" * 70)
    print("Done. Review misfires above before moving to step 2.4.")


if __name__ == "__main__":
    main()