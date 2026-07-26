"""
agents/source_planner_lean.py — Data Layer architecture §5b: Source
Planner (lean), Mode B's one judgment call.

Mode C (eo/source_index.py:get_packet(), §5a) is pure topic skeleton --
name/summary/parent/content_hint/covers, no LLM, no raw excerpt content.
That's enough for a purely structural consumer (§6a: Mind Mapper,
Concept Linker just draw the tree). It is NOT enough for a consumer
that needs to say something substantive ABOUT a topic's actual content
(§6b/§6c: Fact Detector, Note Table Builder, Study Generator, Workflow
Suggester) -- a one-line summary can't support pulling out a precise
fact or a workflow's real steps. Mode B is the middle ground: one small,
cheap LLM call looks at the skeleton ALONE (never the real source text)
and judges which topics are thin enough on their own that a consumer
would need the underlying excerpts pulled in too, and which are
detailed enough to answer from the skeleton as-is. Judgment only --
this module never generates the consumer's actual output, that's still
each retrofitted agent's own job in §6.

WHY "lean" here does NOT mean Part 2.4's tier-1 lean pipeline: that
pipeline (agents/prompt_writer_lean.py, code_writer_lean.py,
reviewer_fixer_lean.py) is a separate, fixed, memory-bus-KEYS-wired
sequence for tier-1's small-app coding path, with its own hardcoded
CHAIN that deliberately bypasses eo/registry.py's AGENT_CAPABILITIES
tagging. This role has nothing to do with that pipeline or that
product surface -- it's Notebooks' own domain, same as
agents/source_manager.py and agents/backlink_detector.py, and it hires
through the exact same generic_worker.run(role=...) path those two
already use (single-hire, AGENT_CAPABILITIES-tag-aware via
eo/panel.py:_best_match(), domain="notes"). "Lean" here just means what
the notebook step calls it: one small, fast judgment call, not a full
generation pass -- the same sense agents/idea_planner.py's or
agents/reviewer_fixer_lean.py's own single-pass judgment calls use the
word, not a reference to that other pipeline's infrastructure.

Needs its own AGENT_CAPABILITIES tag ("source_planner_lean") on the same
notes-domain Groq pool §4c already tagged for "source_manager" and
"backlink_detector" -- ships in the same patch as this module, unlike
§4c's own deferred tagging of "source_manager" (see that module's own
docstring), since there's no reason to repeat that gap a second time
now that the precedent's established.

Place this file at: agents/source_planner_lean.py
"""
import os
import sys
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import get_role_prompt, add_role_prompt
from eo.source_index import get_packet
from eo.knowledge_graph import get_node

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Same per-node truncation reasoning as agents/source_manager.py's
# MODE_A_MAX_CONTENT_CHARS -- keeps one long covered section from
# crowding out the rest of a flagged topic's excerpt bundle.
MAX_EXCERPT_CHARS_PER_NODE = 6000

SOURCE_PLANNER_LEAN_BRIEF = (
    "You are deciding which topics in a workspace's topic tree need "
    "their full source excerpts pulled in for a downstream task, versus "
    "which are already covered well enough by their own name/summary "
    "alone. You will NOT see the actual source excerpts -- judge only "
    "from each topic's name, summary, and content_hint below, plus the "
    "task this decision is for. A topic needs its excerpts when the "
    "task requires something precise or detailed a 1-2 sentence summary "
    "can't reliably supply (an exact fact, figure, quote, or procedural "
    "step) -- err toward NOT flagging a topic whose summary already "
    "seems to answer the task; needlessly pulling excerpts is wasted "
    "context, not free.\n\n"
    "Output a single fenced ```json code block containing an object "
    "with exactly one key, \"needs_excerpts\", a JSON array of the "
    "bracketed topic ids (as plain strings, no brackets) that need "
    "their excerpts pulled in -- an empty array if none do. Nothing "
    "else outside that code block."
)


def _ensure_role_registered() -> None:
    # Same defensive bootstrap agents/source_manager.py's and
    # agents/backlink_detector.py's own _ensure_role_registered()
    # functions give.
    if not get_role_prompt("source_planner_lean"):
        add_role_prompt("source_planner_lean", SOURCE_PLANNER_LEAN_BRIEF,
                         source="source_planner_lean_seed")


def _skeleton_context(topics: dict, task_text: str) -> str:
    """One bracketed-id-tagged line per topic -- name, summary,
    content_hint -- ahead of the task description. Deliberately omits
    `covers` (the raw node ids): this role judges from the topic's OWN
    description, not from how many sections back it, and a count or id
    list here would just invite the model to guess at coverage instead
    of judging content sufficiency.
    """
    lines = [f"TASK: {task_text}\n", "TOPICS:"]
    for tid, topic in topics.items():
        lines.append(
            f"[{tid}] {topic.get('name')} "
            f"({topic.get('content_hint')}): {topic.get('summary')}"
        )
    return "\n".join(lines)


def _parse_decision(raw: str, valid_topic_ids) -> list[str]:
    """Same degrade-don't-break posture as
    agents/source_manager.py:_parse_mode_a_topics() -- malformed or
    missing JSON just means "flag nothing," never a raised error, and
    any flagged id outside this call's own topic set is dropped rather
    than trusted.
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
    flagged = parsed.get("needs_excerpts")
    if not isinstance(flagged, list):
        return []
    valid = set(valid_topic_ids)
    return [tid for tid in flagged if isinstance(tid, str) and tid in valid]


def _attach_excerpts(workspace_id: str, topics: dict, flagged: list[str]) -> None:
    """Mutates `topics` in place: every flagged topic id gets an
    "excerpts" key, built by walking that topic's own `covers` node ids
    (§5a's covers-edge walk output) through
    eo/knowledge_graph.py:get_node() and concatenating their content.
    Never raises on a missing/unfetchable node -- best-effort, same
    "one bad node shouldn't sink the whole decision" posture
    agents/mind_mapper.py's own _context_for() already takes silently
    by just working off whatever list_nodes() returned.

    Un-flagged topics are left exactly as get_packet() returned them --
    no "excerpts": None padding -- so a consumer can tell "skeleton is
    all you get" apart from "we looked and there was nothing" with a
    plain `"excerpts" in topic` check.
    """
    for tid in flagged:
        topic = topics.get(tid)
        if not topic:
            continue
        parts = []
        for node_id in topic.get("covers") or []:
            node = get_node(workspace_id, node_id)
            if not node:
                continue
            content = (node.get("content") or "").strip()[:MAX_EXCERPT_CHARS_PER_NODE]
            if content:
                parts.append(content)
        topic["excerpts"] = "\n\n".join(parts)


def plan(workspace_id: str, task_text: str, scope: str = "project",
         session_id: str = None, domain: str = None) -> dict:
    """Mode B's full entry point: get_packet()'s Mode C skeleton, plus
    this role's one judgment call, plus (for whatever it flagged) the
    real excerpts pulled in -- a caller gets back a single, directly
    consumable packet, same "one call, fully usable result" shape §5a's
    get_packet() already is for Mode C.

    No LLM call at all when there are no topics to judge (an empty or
    brand-new workspace) -- same "nothing to decide, don't spend a call
    finding that out" short-circuit agents/backlink_detector.py's own
    run_after_source_manager() takes for an empty topic_ids batch.

    Returns get_packet()'s own shape plus one added top-level key:

        {
          "workspace_id": str, "scope": str,
          "topics": {"<topic_id>": {..._SKELETON_FIELDS, "covers": [...],
                                     "excerpts": str}, ...},  # excerpts
                                                               # present
                                                               # only on
                                                               # flagged
                                                               # topics
          "connections": [...],
          "needs_excerpts": ["<topic_id>", ...],
        }
    """
    packet = get_packet(workspace_id, scope=scope, session_id=session_id)
    if not packet["topics"]:
        packet["needs_excerpts"] = []
        return packet

    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred -- same
                                                          # circular-import
                                                          # reason as
                                                          # agents/source_manager.py

    result = run_role(
        role="source_planner_lean",
        task_text=_skeleton_context(packet["topics"], task_text),
        input_keys=[], session_id=session_id,
        include_conversation_context=False, domain=domain or "notes",
    )
    flagged = _parse_decision(result.get("text") or "", packet["topics"].keys())
    _attach_excerpts(workspace_id, packet["topics"], flagged)
    packet["needs_excerpts"] = flagged
    return packet
