"""
agents/fact_detector.py — Notebooks integration guide §6.2: the missing
"detect step" for the Facts subtab's Generate wiring.

eo/workspace_facts.py already has a complete agent-proposed-fact review
flow — propose_fact() / list_candidates() / accept_candidate() /
reject_candidate() — same accept/reject discipline as
agents/note_clusterer.py's cluster candidates and agents/note_taker.py's
note candidates. What was actually missing (confirmed by grepping the
whole repo: propose_fact() has zero real callers, only docstring
mentions) is anything that reads a workspace's sources and calls it in
the first place. The guide's original "Facts is real, but disconnected"
note assumed that detect step already existed somewhere; it doesn't —
this file is that step.

Unlike Clusters (KMeans over existing embeddings) or Backlinks (plain
substring match), "is this a durable, workspace-level fact worth
remembering" is a judgment call over source content, not something a
deterministic pass can produce. So this follows agents/note_taker.py's
shape instead of agents/note_clusterer.py's: a single-hire
generic_worker role call, not a staffed Panel run (Notebooks
integration guide §2's recommendation for exactly this kind of
one-shot reasoning job — reads context, produces structured output, no
role handoffs).

CHANGED — Data Layer architecture §6b: was reading every in-scope
node's raw content straight off eo/knowledge_graph.py's list_nodes().
Now reads agents/source_planner_lean.py:plan() instead -- Mode B/C
(§5's distinction): the lean role first judges, from the topic
skeleton alone, which topics are thin enough that a fact worth
"remembering" might be hiding in the raw text, and only THOSE topics'
excerpts get pulled in; every other topic's name/summary stands on its
own. This module's own fact_detector role never sees raw content for a
topic Mode B didn't flag -- same "don't needlessly pull excerpts"
posture plan()'s own docstring describes.

Place this file at: agents/fact_detector.py
"""
import os
import sys
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan
from eo import workspace_facts
from eo.registry import get_role_prompt, add_role_prompt

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Per-source cap, not a total-context cap: with §3's PDF fix a single
# source is now the whole document, so without a per-source limit one
# long source could crowd out every other source in scope. Long enough
# for the model to get real signal, short enough to leave room for
# several sources in one call.
MAX_CONTENT_CHARS_PER_SOURCE = 6000

# Mirrors eo/registry.py's ROLE_PROMPTS_SEED entry for this role (added
# alongside this file) so a fresh install bootstraps it immediately, but
# also registered defensively here: _load_prompts() in eo/registry.py
# only bootstraps from ROLE_PROMPTS_SEED when the role-prompt store is
# completely empty, so an already-running deployment's store (seeded
# long before "fact_detector" existed) would never pick up a
# seed-only addition. Calling add_role_prompt() here the first time
# this role is actually used covers that case without needing a manual
# migration step.
FACT_DETECTOR_BRIEF = (
    "You read one or more source excerpts from a project's notebook and "
    "propose any durable, workspace-level facts worth remembering about "
    "the project as a whole — decisions, defining characteristics, "
    "constraints, or context that would still matter in a future, "
    "unrelated conversation about this project. Skip anything that's "
    "just restating the source's own content rather than a fact ABOUT "
    "the project, and skip anything trivial or already obvious. If "
    "nothing in the excerpts is worth remembering, output exactly the "
    "single word NONE and nothing else. Otherwise output a single "
    "fenced ```json code block containing a JSON array of objects, each "
    "with \"key\" (a short slug-like label for this fact, e.g. "
    "\"target_platform\") and \"value\" (the fact itself, written so "
    "it's self-contained and understandable without the source it came "
    "from) — nothing else outside that code block. Never invent a fact "
    "the excerpts didn't actually support, and prefer a short or empty "
    "list over padding it with marginal facts."
)


def _ensure_role_registered() -> None:
    if not get_role_prompt("fact_detector"):
        add_role_prompt("fact_detector", FACT_DETECTOR_BRIEF, source="fact_detector_seed")


def _context_for(topics: dict) -> str:
    """One section per topic: its Mode B excerpts if source_planner_lean
    flagged it as needing them, otherwise its name/summary/content_hint
    as-is. Same per-topic truncation reasoning as before -- plan()'s own
    MAX_EXCERPT_CHARS_PER_NODE already caps any pulled excerpt, this
    just guards the skeleton-only fallback the same way.
    """
    parts = []
    for topic in topics.values():
        title = topic.get("name") or "Untitled topic"
        body = topic.get("excerpts")
        if not body:
            body = topic.get("summary") or topic.get("content_hint") or ""
        body = body.strip()[:MAX_CONTENT_CHARS_PER_SOURCE]
        if not body:
            continue
        parts.append(f"--- {title} ---\n{body}")
    return "\n\n".join(parts)


def detect_facts(workspace_id: str, source_node_ids: list[str] | None = None) -> list[dict]:
    """Reads the given sources (or every source in the workspace when
    `source_node_ids` is falsy — "blank scope = whole notebook," the
    same convention Notebooks integration guide §4.2 uses for every
    other scoped target) and asks the `fact_detector` role to propose
    durable, workspace-level facts. Each proposal is written through
    workspace_facts.propose_fact() into the same pending-candidate list
    the Facts subtab already reviews — this never writes directly into
    live facts.

    CHANGED — Data Layer architecture §6b: `source_node_ids` scoping
    used to mean "only these Primary Source nodes"; read the same way
    §6a's retrofits read it -- "only topics whose `covers` list touches
    one of these node ids."

    Returns just the candidates this call added. workspace_facts.
    propose_fact() only ever hands back the *whole* pending list (same
    as agents/note_clusterer.py's propose_clusters() replacing its own
    whole list), so this diffs against the list from before the call
    rather than assuming the tail is this call's.
    """
    packet = plan(
        workspace_id,
        task_text=(
            "Identify any durable, workspace-level fact worth remembering "
            "about this project as a whole -- a decision, defining "
            "characteristic, constraint, or precise detail (not something "
            "a 1-2 sentence summary would already capture)."
        ),
        scope="project",
    )
    topics = packet["topics"]
    if source_node_ids:
        wanted = set(source_node_ids)
        topics = {tid: t for tid, t in topics.items()
                  if wanted & set(t.get("covers") or [])}

    context = _context_for(topics)
    if not context:
        return []

    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason
                                                          # agents/note_taker.py's
                                                          # own generic_worker
                                                          # call defers this

    task_text = (
        "Read the source excerpts below and propose any durable, "
        "workspace-level facts worth remembering about this project.\n\n"
        + context
    )
    result = run_role(
        role="fact_detector",
        task_text=task_text,
        input_keys=[],
        session_id=None,
        # These excerpts already ARE this call's context -- no chat
        # history to fold in, same reasoning as note_taker's own
        # include_conversation_context=False for _propose_from_context().
        include_conversation_context=False,
        domain="notes",
    )
    raw = (result.get("text") or "").strip()
    if raw.upper() == "NONE":
        return []

    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []

    before = len(workspace_facts.list_candidates(workspace_id))
    for item in parsed:
        if not isinstance(item, dict):
            continue
        key = (item.get("key") or item.get("title") or "").strip()
        value = (item.get("value") or item.get("summary") or "").strip()
        if not key or not value:
            continue
        workspace_facts.propose_fact(workspace_id, key=key, value=value, proposed_by="fact_detector")

    return workspace_facts.list_candidates(workspace_id)[before:]


if __name__ == "__main__":
    import sys as _sys
    for ws in _sys.argv[1:]:
        found = detect_facts(ws)
        print(f"--- {ws}: {len(found)} new candidate fact(s) ---")
        print(json.dumps(found, indent=2)[:1000])
