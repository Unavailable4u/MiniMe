"""
Notebooks Chat-First refinement, Phase 2 step 2.2.

Converts the Phase 1 capability manifest (the same shape returned by
GET /api/capabilities -- key, label, icon, subTab, keywords, description,
scopeAllowed, endpoint) into an OpenAI-style `tools` array suitable for
client.chat.completions.create(..., tools=...).

Per the Phase 2 step 2.1 findings (see utils/llm_client.py), all six
configured providers -- Groq, Cerebras, Mistral, Gemini (OpenAI-compat),
HuggingFace router, and Cloudflare Workers AI -- accept tools in this
exact {type: "function", function: {name, description, parameters}}
shape, including Cloudflare's raw REST payload. So this one function
is enough for every provider; no per-provider branching needed here.

What this step deliberately does NOT do yet (later Phase 2 steps):
  - Does not call the LLM. See step 2.3 for the isolated test harness.
  - Does not get wired into _call_step()/_call_cloudflare_step()'s actual
    request payload -- both still ignore any "tools" kwarg today (this is
    the gap the 2.1 findings flagged). That wiring is step 2.5/2.6, once
    the classification pass is proven out against test messages (2.4).
  - Does not add the non-generation tools (e.g. "check my progress",
    "what's related to X") the guide mentions for Phase 2 step 1 --
    those aren't generation targets, so they don't come from this
    manifest. They get added as their own hand-written entries once
    Phase 6/3 give them something real to call.
"""

from typing import Any


# Manifest entries that aren't real yet (Phase 1.5's disabled/hidden
# stubs for podcast/video_overview/workflow, ahead of Phase 5's backend
# work). Never offer the model a tool with no working endpoint behind it.
#
# Step 2.5 fix: the FIXTURE_MANIFEST used by scripts/test_tool_calling.py
# marks stubs with `disabled: True`, but the *real* manifest
# (api/server.py's CAPABILITIES_MANIFEST) instead uses `enabled: False`
# (with `endpoint: None`) for the same podcast/video_overview/workflow
# stubs. This function only ever checked the fixture's convention, so
# wiring the real manifest into manifest_to_tools() (step 2.5) would have
# silently offered the model all three disabled stubs as callable tools —
# exactly what the Phase 2.4 findings say must never happen. Checking
# `enabled` too (defaulting missing/absent to True, so the fixture keeps
# behaving exactly as before) covers both conventions.
def _is_enabled(capability: dict[str, Any]) -> bool:
    return (
        capability.get("enabled", True)
        and not capability.get("disabled")
        and not capability.get("hidden")
    )


def _parameters_for_scope(capability: dict[str, Any]) -> dict[str, Any]:
    """
    Build the JSON-schema `parameters` block for one capability, based on
    its scopeAllowed value ("whole" | "sources" | "topic") plus, NEW —
    Chat wiring patch (step 4), any `pastableTextFields` the manifest
    entry declares.

    - "topic": the action is meaningless without knowing which topic
      (e.g. running a workflow off a Mind Map node), so topic_id is
      required.
    - "sources": the action can be scoped to a subset of ingested
      sources, but doesn't have to be -- omitting source_ids means "use
      everything currently attached to the workspace."
    - "whole": operates over the whole notebook; no extra arguments.

    pastableTextFields: NEW — Chat wiring patch (step 4). An optional
    list on a manifest entry (currently just "podcast" -> ["script_text"],
    "slide_deck" -> ["slide_text"], "video_overview" ->
    ["script_text", "slide_text"] -- see api/routes/notebooks.py's
    CAPABILITIES_MANIFEST) naming which of api/routes/notebooks.py's
    `scope["script_text"]`/`scope["slide_text"]` reuse keys (added by the
    Video Overview reuse patch, step 2's _generate_video_overview()
    rewrite, and read the same way by _generate_podcast()/
    _generate_slide_deck() once their own callers pass them through) this
    particular capability can accept. Each named field becomes its own
    optional string parameter, independent of the scope branch above --
    a capability can be both "sources"-scoped AND accept pasted text in
    the same call (e.g. "make a video overview of the intro chapter using
    the script I just pasted"). Deliberately never required: omitting
    these is what tells the backend to generate that half instead of
    reusing pasted text, exactly as much a normal, expected case as
    omitting source_ids is for scope.
    """
    scope = capability.get("scopeAllowed")
    properties: dict[str, Any] = {}
    required: list[str] = []

    if scope == "topic":
        properties["topic_id"] = {
            "type": "string",
            "description": (
                "The id of the topic this action applies to "
                "(from the workspace's Mind Map)."
            ),
        }
        required.append("topic_id")
    elif scope == "sources":
        properties["source_ids"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Which source ids to scope this to. Omit to use every "
                "source currently attached to the workspace."
            ),
        }
    elif scope != "whole":
        # Unknown/missing scopeAllowed -- fail loud rather than silently
        # emitting a tool with no parameters block, which would look
        # valid but misrepresent what the endpoint actually needs.
        raise ValueError(
            f"capability {capability.get('key')!r} has unrecognized "
            f"scopeAllowed={scope!r}"
        )

    _PASTABLE_TEXT_DESCRIPTIONS = {
        "script_text": (
            "The exact podcast/narration script text the user already "
            "wrote or pasted in this conversation, if any -- pass it "
            "through verbatim. Only set this when the user's own message "
            "actually contains or clearly points at a script they "
            "already have; omit it entirely to have this generate a new "
            "script from the workspace's sources instead."
        ),
        "slide_text": (
            "The exact slide deck / presentation outline text the user "
            "already wrote or pasted in this conversation, if any -- "
            "pass it through verbatim. Only set this when the user's own "
            "message actually contains or clearly points at slides they "
            "already have; omit it entirely to have this generate a new "
            "outline from the workspace's sources instead."
        ),
    }
    for field in capability.get("pastableTextFields") or []:
        properties[field] = {
            "type": "string",
            "description": _PASTABLE_TEXT_DESCRIPTIONS.get(
                field, f"Pre-existing {field} the user provided, if any."
            ),
        }

    return {"type": "object", "properties": properties, "required": required}


# NEW — Phase 6 step 6.8. The first of the "hand-written entries" this
# module's header comment said would show up "once Phase 6/3 give them
# something real to call" -- study_progress.py (steps 6.1-6.7) now
# backs a real "mark this topic done" action (PUT
# /api/workspaces/{ws_id}/progress?status=done), so this is no longer
# a generation target and doesn't belong in manifest_to_tools()/
# CAPABILITIES_MANIFEST. Kept as its own small builder (rather than a
# fake manifest entry with a made-up `endpoint`) so a future
# non-generation tool (e.g. Phase 3's "what's related to X") has an
# obvious place to sit next to this one instead of being smuggled into
# the generation manifest.
#
# Reuses _parameters_for_scope's "topic" branch verbatim -- marking a
# topic done is meaningless without knowing which topic, exactly like
# the "workflow" capability's own scopeAllowed: "topic" -- so the same
# {"topic_id": {...}} required-argument shape applies here too.
def study_progress_tools() -> list[dict[str, Any]]:
    """
    Hand-written (not manifest-derived) tools for eo/study_progress.py
    actions. Currently just "mark_topic_done" (step 6.8); append here,
    not to CAPABILITIES_MANIFEST, if/when more non-generation actions
    get wired into chat.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "mark_topic_done",
                "description": (
                    "Mark a topic as done/complete in the study progress "
                    "board. Use this when the user says something like "
                    "'mark X as done', 'I finished X', or 'I'm done with "
                    "X', where X is a topic from the workspace's Mind Map. "
                    "This does not generate anything -- it only updates "
                    "that topic's status."
                ),
                "parameters": _parameters_for_scope({"scopeAllowed": "topic"}),
            },
        }
    ]


def manifest_to_tools(
    manifest: list[dict[str, Any]],
    *,
    name_prefix: str = "generate_",
) -> list[dict[str, Any]]:
    """
    Convert a Phase 1 capability manifest into an OpenAI-style tools array.

    Each enabled manifest entry becomes one callable tool named
    f"{name_prefix}{key}" (e.g. "generate_flashcards", "generate_mindmap",
    "generate_workflow"), using the entry's `description` verbatim as the
    tool description -- this is exactly why Phase 1 step 1.2 required a
    human-readable description on every entry, not just a label.
    """
    tools = []
    for capability in manifest:
        if not _is_enabled(capability):
            continue
        if "key" not in capability or "description" not in capability:
            raise ValueError(
                "manifest entry missing required 'key' or 'description' "
                f"field: {capability!r}"
            )

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"{name_prefix}{capability['key']}",
                    "description": capability["description"],
                    "parameters": _parameters_for_scope(capability),
                },
            }
        )
    return tools
