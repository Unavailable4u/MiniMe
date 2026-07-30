"""
agents/workflow_suggester.py — bug audit #7 (new feature): "Suggested
workflow / process diagrams."

Genuinely different job from agents/mind_mapper.py (one topic-overview
diagram of the whole notebook) and agents/concept_linker.py (relationships
BETWEEN sources/topics) — this one looks for procedures/sequences
DESCRIBED WITHIN the material ("starting sequence for a DC motor,"
"construction steps of an alternator's stator") and renders each as its
own small flowchart, for use as a study aid. Same shape as those two:
single generic_worker role call, no handoffs, self-registers its role
brief the first time it's used (concept_linker.py's pattern, not
mind_mapper.py's ROLE_PROMPTS_SEED entry — no strong reason to hand-edit
the shared seed dict for a role this scoped).

Detection is part of the same call, not a separate step: the model finds
0-4 distinct procedures in one pass. Zero is a valid, expected result for
source material that's purely conceptual/descriptive (a history chapter,
a policy doc) — the brief explicitly says not to force one into
existence, and the caller (api/server.py's _generate_workflows) treats an
empty list as a normal "done" result, not an error or a retry trigger.

Output contract: one fenced ```json block, same pattern
concept_linker.py's CONCEPT_LINKER_BRIEF already uses --

    {
      "workflows": [
        {
          "title": "Starting a DC Shunt Motor",
          "description": "Sequence for safely energizing and starting the motor.",
          "steps": [
            {"id": "S1", "label": "Close field circuit", "type": "step"},
            {"id": "S2", "label": "Set starter to first position", "type": "step"},
            {"id": "S3", "label": "Fuse blown?", "type": "decision"}
          ],
          "mermaid": "flowchart TD\\n  S1[Close field circuit] --> S2[Set starter to first position] --> S3{Fuse blown?}"
        }
      ]
    }

Per bug #6's discipline (never hand a caller "raw text pretending to be a
diagram"), a workflow whose `mermaid` field doesn't parse into a fenced
block at all is dropped from the result entirely at this layer -- see
_parse_workflow()'s per-item validation. A workflow that fences correctly
but has a genuine Mermaid *syntax* error inside it (a typo) is still
returned; that's MermaidDiagram.jsx's `hideSourceOnFail` last-resort case
to handle at render time, same as Mind Map.

Stable per-step ids matter here in a way they didn't for Mind Map: two
steps in a real procedure can share wording (e.g. "Check connections"
at both the start and end of a workflow). This role is explicitly told
to emit stable S1/S2/... ids as the Mermaid node ids themselves, so a
future checklist UI can key off the rendered SVG's node id
(`flowchart-S1-3` -> `S1`) instead of scraping label text -- see the
guide's refinement #1 for why that's worth doing from the start rather
than retrofitting later.

CHANGED — Data Layer architecture §6c: was reading every in-scope
node's raw content straight off eo/knowledge_graph.py's list_nodes().
Now reads agents/source_planner_lean.py:plan() instead -- Mode B/C
(§5's distinction): finding a real step-by-step procedure needs actual
prose describing it, not a one-line summary, so most topics end up
flagged here in practice -- but the lean role still makes that call
per-topic rather than this module assuming it, same posture every
other §6b/§6c retrofit takes.

Place this file at: agents/workflow_suggester.py
"""
import os
import sys
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan
from eo.registry import get_role_prompt, add_role_prompt
from utils.mermaid_lint import looks_valid_mermaid

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*(.*?)\s*```", re.DOTALL)

# Same per-source truncation reasoning as agents/mind_mapper.py and
# agents/concept_linker.py.
MAX_CONTENT_CHARS_PER_SOURCE = 6000

MAX_WORKFLOWS = 4
VALID_STEP_TYPES = {"step", "decision"}

WORKFLOW_SUGGESTER_BRIEF = (
    "You read source material and look for distinct procedures, "
    "sequences, or processes it describes — concrete step-by-step "
    "things a person would DO, not just concepts it explains (a "
    "starting sequence, an assembly procedure, a troubleshooting "
    "flow, a setup checklist). Find between 0 and 4 of these. Zero is "
    "a completely valid answer for material that's purely conceptual "
    "or descriptive — never invent a procedure just to have something "
    "to show; only report ones the source material actually spells "
    "out. For each procedure found, write a short title, a one-line "
    "description, an ordered list of steps, and the same steps "
    "rendered as a Mermaid flowchart. Each step needs a stable short "
    "id (S1, S2, S3, ...), a concise plain-text label (a few words, "
    "not a full sentence), and a type of either \"step\" (something "
    "done) or \"decision\" (a branch point / question). The Mermaid "
    "flowchart must use those exact S1/S2/... strings as its own node "
    "ids (e.g. S1[Close field circuit] --> S2[Set starter to first "
    "position] --> S3{Fuse blown?}), written top-to-bottom as "
    "'flowchart TD'. Output a single fenced ```json code block "
    "containing an object with exactly one key, \"workflows\", a JSON "
    "array where each item has \"title\", \"description\", \"steps\" "
    "(array of {\"id\", \"label\", \"type\"}), and \"mermaid\" (the "
    "fenced-flowchart source as a plain string, no nested code fence). "
    "Nothing outside that one code block. An empty \"workflows\" array "
    "is a completely normal, expected response — do not pad it out "
    "with a procedure the source material doesn't actually describe."
)


def _ensure_role_registered() -> None:
    if not get_role_prompt("workflow_suggester"):
        add_role_prompt("workflow_suggester", WORKFLOW_SUGGESTER_BRIEF, source="workflow_suggester_seed")


# --- Topic Workflow Builder -------------------------------------------------
#
# Distinct job from the detection-style role above: no "0-4, zero is
# fine" judgment call here. A Mind Map node click always wants exactly
# one mastery sequence back for the topic the user picked, synthesized
# rather than merely detected -- the source material grounds it when
# it's substantive, but thin/absent material is never a reason to come
# back empty. Reuses the same {title, description, steps, mermaid}
# shape (and this module's own _parse_workflow()/_parse_steps()
# validation) so WorkflowCard needs no changes to render either kind
# of result.

TOPIC_WORKFLOW_BUILDER_BRIEF = (
    "You are given one specific topic and whatever source material is "
    "available about it. Unlike a detection task, you do not decide "
    "whether a procedure exists in the material -- you always "
    "synthesize exactly one mastery sequence: an ordered set of 4-8 "
    "steps a learner should work through to actually understand this "
    "topic. Ground it in the source material where that material is "
    "substantive, and use your own general knowledge of the subject to "
    "fill in a sensible sequence where the material is thin or absent. "
    "Never refuse and never return zero steps -- there is always a "
    "reasonable generic study sequence for any named topic. Write a "
    "short title, a one-line description, the ordered steps, and the "
    "same steps rendered as a Mermaid flowchart. Each step needs a "
    "stable short id (S1, S2, S3, ...), a concise plain-text label (a "
    "few words, not a full sentence), and a type of either \"step\" "
    "(something done) or \"decision\" (a branch point / question). The "
    "Mermaid flowchart must use those exact S1/S2/... strings as its "
    "own node ids (e.g. S1[Skim the overview] --> S2[Identify key "
    "terms] --> S3{Confident yet?}), written top-to-bottom as "
    "'flowchart TD'. Output a single fenced ```json code block "
    "containing an object with exactly one key, \"workflow\", whose "
    "value is an object with \"title\", \"description\", \"steps\" "
    "(array of {\"id\", \"label\", \"type\"}), and \"mermaid\" (the "
    "flowchart source as a plain string, no nested code fence). "
    "Nothing outside that one code block."
)


def _ensure_topic_role_registered() -> None:
    if not get_role_prompt("topic_workflow_builder"):
        add_role_prompt("topic_workflow_builder", TOPIC_WORKFLOW_BUILDER_BRIEF,
                         source="topic_workflow_builder_seed")


def _find_topic(topics: dict, topic_label: str, source_node_ids: list[str] | None):
    """Case-insensitive exact match of `topic_label` against each
    topic's own `name`, optionally restricted to topics whose `covers`
    list touches `source_node_ids` -- same scoping convention
    suggest_workflows() already reads that parameter with. Returns
    (topic_id, topic) or (None, None); a miss is expected (the Mind
    Map's topic tree can drift from a stale node label) and is handled
    by the generic fallback below, not treated as an error.
    """
    wanted = set(source_node_ids) if source_node_ids else None
    needle = (topic_label or "").strip().lower()
    for tid, topic in topics.items():
        if wanted and not (wanted & set(topic.get("covers") or [])):
            continue
        name = str(topic.get("name") or "").strip().lower()
        if name == needle:
            return tid, topic
    return None, None


def _context_for_topic(topic: dict | None, topic_label: str) -> str:
    """Single-topic analog of _context_for() above -- that one grounds
    a whole-notebook detection pass across every topic; this one only
    ever needs the one topic the user actually clicked. Falls back to
    the bare label itself when there's no topic match or no excerpt/
    summary/content_hint content to show, so the caller always has
    *something* to hand the model rather than an empty string.
    """
    if not topic:
        return topic_label
    body = topic.get("excerpts")
    if not body:
        body = topic.get("summary") or topic.get("content_hint") or ""
    body = body.strip()[:MAX_CONTENT_CHARS_PER_SOURCE]
    name = topic.get("name") or topic_label
    return f"--- {name} ---\n{body}" if body else name


def _generic_fallback_workflow(topic_label: str) -> dict:
    """A topic click can never come back empty-handed. If plan() fails
    outright, `topic_label` can't be matched against the workspace's
    own topic tree, or the model's response doesn't parse into a valid
    workflow, this hardcoded generic mastery sequence stands in --
    grounded in nothing but the clicked label itself, so it's
    intentionally generic rather than wrong. A vague-but-usable 6-step
    study path beats an error or a blank card.
    """
    label = (topic_label or "this topic").strip() or "this topic"
    safe_label = label.replace('"', "")
    steps = [
        {"id": "S1", "label": f"Skim {label} overview", "type": "step"},
        {"id": "S2", "label": "Identify key terms", "type": "step"},
        {"id": "S3", "label": "Work through a core example", "type": "step"},
        {"id": "S4", "label": "Summarize in your own words", "type": "step"},
        {"id": "S5", "label": "Confident yet?", "type": "decision"},
        {"id": "S6", "label": "Test with practice questions", "type": "step"},
    ]
    mermaid = (
        "flowchart TD\n"
        f"  S1[Skim {safe_label} overview] --> S2[Identify key terms]\n"
        "  S2 --> S3[Work through a core example]\n"
        "  S3 --> S4[Summarize in your own words]\n"
        "  S4 --> S5{Confident yet?}\n"
        "  S5 --> S6[Test with practice questions]\n"
        "  S5 --> S3"
    )
    return {
        "title": f"{label} — Mastery Path",
        "description": (
            f"A generic study sequence for {label}; source material "
            "was too thin to ground a more specific one."
        ),
        "steps": steps,
        "mermaid": mermaid,
    }


def build_topic_workflow(workspace_id: str, topic_label: str,
                          source_node_ids: list[str] | None = None) -> dict:
    """Returns a single {"title", "description", "steps", "mermaid"}
    dict -- same per-item shape suggest_workflows() already emits, so
    WorkflowCard needs no changes -- synthesized for exactly the
    clicked Mind Map topic.

    Unlike suggest_workflows()'s whole-notebook detection pass (0-4
    entries, empty is a valid result), a topic click can never come
    back empty or raise: every failure mode below -- plan() itself
    failing, `topic_label` not matching any topic in scope, the
    model's own response not parsing into a valid workflow -- falls
    through to _generic_fallback_workflow() instead of propagating.

    `source_node_ids` scopes which topics are eligible to match
    `topic_label` against, same "only topics whose `covers` touches
    one of these ids" convention suggest_workflows() already reads it
    as; falsy means "search the whole notebook's topics."
    """
    topic_label = (topic_label or "").strip() or "This topic"

    topic = None
    try:
        packet = plan(
            workspace_id,
            task_text=(
                f"Build a 4-8 step mastery sequence for the topic "
                f"'{topic_label}' -- concrete things a learner should "
                "do to understand it, grounded in whatever source "
                "material actually describes it."
            ),
            scope="project",
        )
        _tid, topic = _find_topic(packet["topics"], topic_label, source_node_ids)
    except Exception:
        # plan() failing (e.g. an empty/unreadable workspace) is not
        # this function's problem to surface -- fall through to the
        # generic sequence below same as a label that just doesn't
        # match anything.
        topic = None

    context = _context_for_topic(topic, topic_label)

    _ensure_topic_role_registered()
    workflow = None
    try:
        from agents.generic_worker import run as run_role   # deferred,
                                                              # same
                                                              # circular-
                                                              # import
                                                              # reason as
                                                              # suggest_workflows()
                                                              # above
        result = run_role(
            role="topic_workflow_builder",
            task_text=f"Build a mastery sequence for this topic:\n\n{context}",
            input_keys=[],
            session_id=None,
            include_conversation_context=False,
            domain="notes",
        )
        raw = (result.get("text") or "").strip()
        match = _JSON_BLOCK_RE.search(raw)
        if match:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                workflow = _parse_workflow(parsed.get("workflow"))
    except Exception:
        # Never let a call/parse failure surface as an error to a
        # topic click -- fall back to the generic sequence instead.
        workflow = None

    return workflow or _generic_fallback_workflow(topic_label)


def _context_for(topics: dict) -> str:
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


def _parse_steps(raw_steps) -> list[dict]:
    steps = []
    seen_ids = set()
    for item in raw_steps or []:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        step_type = str(item.get("type") or "step").strip()
        if not step_id or not label or step_id in seen_ids:
            continue   # duplicate/missing id would break the checklist's
                        # id-based lookup later -- drop rather than guess
        if step_type not in VALID_STEP_TYPES:
            step_type = "step"
        seen_ids.add(step_id)
        steps.append({"id": step_id, "label": label, "type": step_type})
    return steps


def _parse_workflow(item: dict) -> dict | None:
    """Returns a validated workflow dict, or None if it's missing the
    one thing that actually matters (a fenced, non-empty Mermaid
    flowchart) -- per bug #6's rule, this module never hands back
    something pretending to be a diagram when it isn't one. `steps`
    can still be usable (rendered as a plain checklist) even when the
    diagram itself later fails to *render* correctly at the frontend
    layer — that's a different, milder failure this function doesn't
    try to catch, since a real Mermaid syntax error inside an
    otherwise well-formed block isn't something regex validation can
    catch anyway.
    """
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    description = str(item.get("description") or "").strip()
    steps = _parse_steps(item.get("steps"))

    mermaid_raw = str(item.get("mermaid") or "").strip()
    match = _MERMAID_BLOCK_RE.search(mermaid_raw)
    mermaid = match.group(1).strip() if match else mermaid_raw
    if not mermaid or not steps:
        return None

    # BUGFIX (rendering audit): a real Mermaid syntax typo genuinely can't be
    # caught by regex (see this function's own docstring above), but
    # looks_valid_mermaid()'s cheaper heuristic checks -- empty content, no
    # recognized diagram-type header, unbalanced brackets/quotes, a
    # flowchart with zero edges -- catch a meaningful chunk of the common
    # cases where the model's "mermaid" field was closer to prose than a
    # diagram. There's no per-workflow retry loop here (one role call
    # proposes 0-4 workflows together), so rather than dropping the whole
    # workflow -- losing a perfectly good title/description/checklist over
    # a diagram that was never going to render -- this keeps everything else
    # and just omits the diagram. WorkflowCard/MermaidDiagram.jsx already
    # render an empty diagram slot as nothing (mermaidText falsy -> no
    # render attempted) rather than an error, so this fails silently
    # instead of showing the "couldn't render" fallback for something that
    # was never going to succeed anyway.
    if not looks_valid_mermaid(mermaid):
        mermaid = None

    return {"title": title, "description": description, "steps": steps, "mermaid": mermaid}


def suggest_workflows(workspace_id: str, source_node_ids: list[str] | None = None) -> dict:
    """Returns {"workflows": [...]}, 0-4 entries, built from the given
    sources (or every source in the workspace when `source_node_ids`
    is falsy — same "blank scope = whole notebook" convention every
    other Notebooks target uses). An empty list is a normal result,
    not an error.

    CHANGED — Data Layer architecture §6c: `source_node_ids` scoping
    used to mean "only these Primary Source nodes"; read the same way
    every other §6 retrofit reads it -- "only topics whose `covers`
    list touches one of these node ids."

    Raises LookupError if the resolved scope has zero readable topic
    content, same contract agents/mind_mapper.py's generate_mindmap()
    uses, so the caller can turn that into a clear per-branch error
    instead of silently saving an empty result.
    """
    packet = plan(
        workspace_id,
        task_text=(
            "Find 0-4 distinct step-by-step procedures described in the "
            "source material -- concrete sequences a person would DO, "
            "not just concepts explained. A topic's own summary usually "
            "can't show a real procedure's actual steps, so pull in "
            "excerpts for any topic that might describe one."
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
        raise LookupError("no readable topic content in scope")

    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason as
                                                          # agents/mind_mapper.py,
                                                          # agents/concept_linker.py

    task_text = (
        "Find 0-4 distinct step-by-step procedures described in the "
        "source material below.\n\n" + context
    )
    result = run_role(
        role="workflow_suggester",
        task_text=task_text,
        input_keys=[],
        session_id=None,
        include_conversation_context=False,
        domain="notes",
    )
    raw = (result.get("text") or "").strip()
    match = _JSON_BLOCK_RE.search(raw)
    parsed = {}
    if match:
        try:
            parsed = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    workflows = []
    for item in (parsed.get("workflows") or [])[:MAX_WORKFLOWS]:
        parsed_item = _parse_workflow(item)
        if parsed_item:
            workflows.append(parsed_item)

    return {"workflows": workflows}


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.workflow_suggester <workspace_id>")
    else:
        out = suggest_workflows(_sys.argv[1])
        print(f"{len(out['workflows'])} workflow(s) found")
        print(json.dumps(out, indent=2)[:2000])
