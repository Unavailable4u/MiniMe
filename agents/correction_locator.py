"""
agents/correction_locator.py — Data Layer architecture §8b: turning one
plain-language correction (§8a's Corrections tab capture — a scope
plus free text) into a located edit for §8c's Patch Review tab to
render as a before/after and let the user accept or reject.

Two-pass posture, same "cheap read first, LLM fallback only when
actually needed" shape agents/source_planner_lean.py's Mode B already
established for a different judgment call:

  1. Search Secondary Data first — eo/source_index.py:get_packet()'s
     Mode C topic skeleton (name/summary/content_hint, no LLM to get
     that far), one lean role call over it judging which single topic
     the correction is about, and whether that topic's own summary
     already says enough to write a confident edit straight from it.
  2. Fall back to Mode B on Primary Source when it doesn't — pull that
     ONE located topic's real excerpts (same covers-walk +
     eo/knowledge_graph.py:get_node() reads agents/source_planner_lean.py's
     own _attach_excerpts() uses, not reused directly since duplicating
     ~10 lines here keeps this module's only cross-agent dependency to
     "same conventions," not "same private function" — same design
     agents/mind_mapper.py and agents/backlink_detector.py already
     keep from each other) and ask a second, better-grounded call to
     write the edit off the actual source text. Only fires when pass 1
     says the summary alone isn't enough to trust a correction against
     — most corrections about something the summary already states
     (a name, a date, a term) resolve in pass 1 alone.

Never touches Primary Source itself — agents/source_ingestor.py's
write_ingested_source() output stays verbatim, same architecture-wide
invariant eo/secondary_data.py's own docstring states. A located edit
here is always a proposed JSON Patch "replace" op against Secondary
Data (a topic's derived name/summary/content_hint), never a rewrite of
the ingested content those fields were derived from. This module only
PROPOSES that op — applying it via eo/secondary_data.py:apply_patch()
is §8c's job, once a person has actually reviewed it.

Needs its own AGENT_CAPABILITIES tag ("correction_locator") on the same
notes-domain Groq pool §5b's own patch tagged for "source_planner_lean"
— ships in this same patch, same "the precedent's established, no
reason to defer it again" reasoning that patch gave for its own tag.

Place this file at: agents/correction_locator.py
"""
import os
import sys
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import get_role_prompt, add_role_prompt
from eo.source_index import get_packet
from eo.secondary_data import get_secondary_data
from eo.knowledge_graph import get_node

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Same per-node truncation reasoning as agents/source_planner_lean.py's
# own MAX_EXCERPT_CHARS_PER_NODE.
MAX_EXCERPT_CHARS_PER_NODE = 6000

# The only fields a located edit is ever allowed to touch — the same
# three Secondary Data actually keys a topic's displayed identity by.
# "parent" and "source_section_ids" are structural (Backlink Detector's
# and Source Manager's job to set), never something a plain-language
# correction should be able to silently move.
_EDITABLE_FIELDS = ("name", "summary", "content_hint")

CORRECTION_LOCATOR_BRIEF = (
    "A user is correcting something in their notebook. You're given a "
    "plain-language description of what's wrong, plus a list of topics "
    "from their topic tree (each labeled with a bracketed id, name, "
    "content hint, and summary) — and, for at most one topic, its real "
    "source excerpt if that's included below.\n\n"
    "First decide which single topic the correction is actually about, "
    "if any — most corrections are about exactly one. Then decide "
    "whether you have enough to write the corrected text confidently: "
    "if that topic's real excerpt is included below, always decide "
    "from the excerpt (ground truth) over the summary. If no excerpt "
    "is included, judge whether the summary alone already states what "
    "the correction is about; if the correction needs something the "
    "summary doesn't mention, say you need the excerpt rather than "
    "guessing.\n\n"
    "Output a single fenced ```json code block containing an object "
    "with exactly these keys:\n"
    "- \"topic_id\": the bracketed id of the matching topic, or null if "
    "none match\n"
    "- \"needs_source\": true if you need that topic's real excerpt and "
    "don't have it yet, false otherwise\n"
    "- \"edit\": null when needs_source is true or topic_id is null; "
    "otherwise an object with just the field(s) that should change — "
    "\"name\", \"summary\", and/or \"content_hint\" — set to the "
    "corrected text. Never invent an edit an excerpt doesn't actually "
    "support — if given the excerpt and it contradicts the user's "
    "correction, set \"edit\" to null instead of writing something "
    "unsupported.\n"
    "Nothing else outside that code block."
)


def _ensure_role_registered() -> None:
    # Same defensive bootstrap agents/source_manager.py's,
    # agents/backlink_detector.py's, and agents/source_planner_lean.py's
    # own _ensure_role_registered() functions give.
    if not get_role_prompt("correction_locator"):
        add_role_prompt("correction_locator", CORRECTION_LOCATOR_BRIEF,
                         source="correction_locator_seed")


def _candidate_topics(workspace_id: str, scope_node_ids: set | None) -> dict:
    """§8b's own "search Secondary Data" step: Mode C's topic skeleton
    (no LLM), optionally narrowed to just the topics touching
    `scope_node_ids` — the Corrections tab's file-scope picker (§8a)
    resolved down to a set of Primary Source node ids by its caller.
    `scope_node_ids=None` means "All files" — every topic in scope.
    """
    topics = get_packet(workspace_id, scope="project")["topics"]
    if scope_node_ids is None:
        return topics
    return {
        tid: t for tid, t in topics.items()
        if scope_node_ids & set(t.get("covers") or [])
    }


def _skeleton_context(topics: dict, correction_text: str) -> str:
    lines = [f"CORRECTION: {correction_text}\n", "TOPICS:"]
    for tid, topic in topics.items():
        lines.append(
            f"[{tid}] {topic.get('name')} "
            f"({topic.get('content_hint')}): {topic.get('summary')}"
        )
    return "\n".join(lines)


def _excerpt_for(workspace_id: str, topic: dict) -> str:
    """Best-effort concatenation of one topic's covered source
    sections — same "one bad node shouldn't sink the whole call"
    posture agents/source_planner_lean.py's own _attach_excerpts()
    already takes, just for a single topic instead of a flagged batch.
    """
    parts = []
    for node_id in topic.get("covers") or []:
        node = get_node(workspace_id, node_id)
        if not node:
            continue
        content = (node.get("content") or "").strip()[:MAX_EXCERPT_CHARS_PER_NODE]
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def _parse_decision(raw: str, valid_topic_ids) -> dict:
    """Same degrade-don't-break posture as
    agents/source_planner_lean.py:_parse_decision() — malformed or
    missing JSON just means "no located edit," never a raised error.
    An out-of-scope topic_id, or an edit field outside
    _EDITABLE_FIELDS, is dropped rather than trusted.
    """
    default = {"topic_id": None, "needs_source": False, "edit": None}
    match = _JSON_BLOCK_RE.search(raw or "")
    if not match:
        return default
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return default
    if not isinstance(parsed, dict):
        return default

    topic_id = parsed.get("topic_id")
    if topic_id not in valid_topic_ids:
        topic_id = None

    needs_source = bool(parsed.get("needs_source")) and topic_id is not None

    edit = parsed.get("edit")
    if topic_id is None or needs_source or not isinstance(edit, dict):
        edit = None
    else:
        edit = {k: v for k, v in edit.items() if k in _EDITABLE_FIELDS and isinstance(v, str)}
        if not edit:
            edit = None

    return {"topic_id": topic_id, "needs_source": needs_source, "edit": edit}


def _call_role(task_text: str, session_id: str = None, domain: str = None) -> str:
    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason as
                                                          # agents/source_manager.py,
                                                          # agents/backlink_detector.py,
                                                          # agents/source_planner_lean.py
    result = run_role(
        role="correction_locator",
        task_text=task_text,
        input_keys=[], session_id=session_id,
        include_conversation_context=False, domain=domain or "notes",
    )
    return result.get("text") or ""


def _build_op(workspace_id: str, topic_id: str, edit: dict) -> dict | None:
    """A located edit becomes one "replace" op against the topic's OWN
    full stored entry (eo/secondary_data.py's own path shape treats a
    topic as one opaque value, same as agents/backlink_detector.py's
    own _build_ops() already does for a reparent) — only the fields
    `edit` actually named are changed, everything else (parent,
    source_section_ids) passes through untouched. Returns None if the
    topic vanished between the search pass and here (a concurrent
    delete) rather than proposing an op against a topic that's gone.
    """
    current = get_secondary_data(workspace_id)["topics"].get(topic_id)
    if current is None:
        return None
    updated = dict(current)
    updated.update(edit)
    return {"op": "replace", "path": f"/topics/{topic_id}", "value": updated}


def locate_correction(workspace_id: str, correction_text: str,
                       scope_node_ids: set | None = None,
                       session_id: str = None, domain: str = None) -> dict:
    """§8b's full entry point: plain-language correction in, located
    edit (or a clear reason there isn't one) out.

    `scope_node_ids`: the Corrections tab's file-scope picker (§8a)
    resolved down to a set of Primary Source node ids by its caller
    (None for "All files" — every topic in scope, same convention
    get_packet()'s own scope argument uses for "nothing to narrow by").

    Returns:
        {
          "workspace_id": str,
          "topic_id": str | None,      # the located topic, if any
          "op": {"op": "replace", "path": "/topics/<id>",
                 "value": {...}} | None,  # ready for §8c's Patch
                                           # Review to render + for
                                           # eo/secondary_data.py:
                                           # apply_patch() to consume
                                           # once accepted
          "reason": str | None,        # set whenever op is None,
                                        # short and user-facing
        }

    No LLM call at all when there are no candidate topics in scope —
    same "nothing to decide, don't spend a call finding that out"
    short-circuit agents/source_planner_lean.py:plan() and
    agents/backlink_detector.py:run_after_source_manager() both already
    take for their own empty-input cases.
    """
    result = {"workspace_id": workspace_id, "topic_id": None, "op": None, "reason": None}

    topics = _candidate_topics(workspace_id, scope_node_ids)
    if not topics:
        result["reason"] = "no topics in scope to search"
        return result

    raw = _call_role(_skeleton_context(topics, correction_text), session_id, domain)
    decision = _parse_decision(raw, topics.keys())

    if decision["topic_id"] is None:
        result["reason"] = "couldn't find a matching topic for this correction"
        return result
    result["topic_id"] = decision["topic_id"]

    if not decision["needs_source"] and decision["edit"]:
        op = _build_op(workspace_id, decision["topic_id"], decision["edit"])
        if op is None:
            result["reason"] = "topic no longer exists"
            return result
        result["op"] = op
        return result

    # Pass 1 either asked for the source, or came back with no usable
    # edit — either way, give it one more try with the real excerpt in
    # hand before giving up (§8b's own Mode B fallback).
    topic = topics[decision["topic_id"]]
    excerpt = _excerpt_for(workspace_id, topic)
    if not excerpt:
        result["reason"] = "no source excerpt available to verify this correction against"
        return result

    context = _skeleton_context({decision["topic_id"]: topic}, correction_text)
    context += f"\n\n--- Source excerpt for [{decision['topic_id']}] ---\n{excerpt}"
    raw2 = _call_role(context, session_id, domain)
    decision2 = _parse_decision(raw2, [decision["topic_id"]])

    if not decision2["edit"]:
        result["reason"] = "the source excerpt didn't support this correction"
        return result

    op = _build_op(workspace_id, decision["topic_id"], decision2["edit"])
    if op is None:
        result["reason"] = "topic no longer exists"
        return result
    result["op"] = op
    return result


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 3:
        print("usage: python -m agents.correction_locator <workspace_id> <correction text>")
    else:
        print(locate_correction(_sys.argv[1], " ".join(_sys.argv[2:])))
