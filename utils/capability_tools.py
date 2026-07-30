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

from typing import Any, Dict, List


# Manifest entries that aren't real yet (Phase 1.5's disabled/hidden
# stubs for podcast/video_overview/workflow, ahead of Phase 5's backend
# work). Never offer the model a tool with no working endpoint behind it.
def _is_enabled(capability: Dict[str, Any]) -> bool:
    return not capability.get("disabled") and not capability.get("hidden")


def _parameters_for_scope(capability: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the JSON-schema `parameters` block for one capability, based on
    its scopeAllowed value ("whole" | "sources" | "topic").

    - "topic": the action is meaningless without knowing which topic
      (e.g. running a workflow off a Mind Map node), so topic_id is
      required.
    - "sources": the action can be scoped to a subset of ingested
      sources, but doesn't have to be -- omitting source_ids means "use
      everything currently attached to the workspace."
    - "whole": operates over the whole notebook; no extra arguments.
    """
    scope = capability.get("scopeAllowed")
    properties: Dict[str, Any] = {}
    required: List[str] = []

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

    return {"type": "object", "properties": properties, "required": required}


def manifest_to_tools(
    manifest: List[Dict[str, Any]],
    *,
    name_prefix: str = "generate_",
) -> List[Dict[str, Any]]:
    """
    Convert a Phase 1 capability manifest into an OpenAI-style tools array.

    Each enabled manifest entry becomes one callable tool named
    f"{name_prefix}{key}" (e.g. "generate_flashcards", "generate_mindmap",
    "generate_workflow"), using the entry's `description` verbatim as the
