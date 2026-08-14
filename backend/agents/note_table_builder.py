"""
agents/note_table_builder.py — Part 4 §4.4. Data tables from scattered
facts.

A real correction to the upgrade plan's own claim, worth being precise
about — same category of correction as this build order's report_writer
one. The plan says this "reuses Part 3 §3.5's extraction-table pattern
wholesale ... just extracting user-specified fields from notebook
sources instead of paper metadata." On inspection, agents/
extraction_table_builder.py's run() reads one fixed, hardcoded input
(KEYS["academic_search_report"]) and extracts one fixed, hardcoded set
of five paper-specific fields (sample_size, methodology, population,
outcome, effect_size) — neither the input source nor the field list can
actually be swapped without editing that module's code. "Reuse
wholesale" doesn't hold as a literal statement.

What's actually true, and simpler to state precisely: the SAME shape
is reused — ThreadPoolExecutor, one worker per source, a fixed
extraction system prompt built per-call, deterministic keyed-union
merge, never a fuzzy-similarity collapse — rewired for Notes' real
input (a workspace's own ingested nodes, via eo/knowledge_graph.py's
list_nodes(), Part 4 §4.3) and a real user-specified field list instead
of a fixed one.

Also NOT hired through eo/registry.py's REAL_ACTION_ROLES / the Panel
pipeline, unlike agents/extraction_table_builder.py — eo/executor.py's
dispatch has no workspace_id parameter threaded through any of its
call-site branches, and adding one is a larger, cross-cutting change
outside this module's scope. Called directly from its own API endpoint
instead, the same shape agents/backlink_detector.py and agents/
note_clusterer.py already established for Notes-domain deterministic
tool agents in Part 4 §4.3.

CHANGED — Data Layer architecture §6b: was reading every node's raw
content straight off eo/knowledge_graph.py's list_nodes() and running
one extraction worker per NODE. Now reads
agents/source_planner_lean.py:plan() instead -- Mode B/C (§5's
distinction) -- and runs one worker per TOPIC: for exact-field
extraction (the whole point of this module) source_planner_lean's own
judgment usually flags most topics as needing their excerpts, but the
decision is still made per-topic rather than assumed, so a topic whose
summary already states a field plainly doesn't cost a wasted excerpt
pull.

FIX — the endpoint this module backs (POST /api/workspaces/{id}/table)
is shared by BOTH the Notebooks tab and the Research tab's Extraction
Panel (same buildExtractionTable() call, same component). plan() only
ever returns topics for a workspace that's gone through Notebooks'
Secondary Data clustering (IngestionDropzone -> topic clustering); a
Research project's academic_search-written `source` nodes never go
through that pipeline, so packet["topics"] comes back correctly empty
for a Research workspace, and build_table() used to treat that as
"nothing to extract" unconditionally. It wasn't -- there was ingested
content, just not in topic-tree shape.

build_table() now falls back to eo/knowledge_graph.py's list_nodes()
-- the very call this module used to make before the CHANGED note
above -- ONLY when plan() returns no topics at all, and runs one
worker per raw node instead of per topic. This is exactly the old
node-level extraction path, kept alive as a fallback rather than
removed, so a Research (or any non-Notebooks) workspace's ingested
nodes are still reachable through this one endpoint without a second
route or a second frontend component. `node_type` is unused on the
topic path (Secondary Data's topics are already derived exclusively
from ingested sources, so there's nothing left to filter there) but is
a real, active filter on this fallback path -- ResearchTab.jsx's
"Sources only" toggle now does something again.

Place this file at: agents/note_table_builder.py
"""
import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan
from eo.knowledge_graph import list_nodes
from eo.registry import AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot
from utils.llm_client import generate_text

# Two-model fallback per worker, same shorter-than-code_writers.py
# reasoning agents/extraction_table_builder.py already gives: extraction
# is a small, cheap completion, not a whole module.
MODELS = [
    # llama-3.3-70b-versatile decommissioned by Groq; migrated to the two
    # models Groq's decommission notice suggested in its place.
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "llama-3.1-8b-instant",
]

# Reuses the exact same Reviewer Pool accounts agents/
# extraction_table_builder.py already tagged for this shape of work
# (see eo/registry.py's AGENT_CAPABILITIES) — same pool, one more tag,
# not a new account allocation.
ROLE_TAG = "note_table_builder"


def _eligible_pool() -> list:
    return [key for key, info in AGENT_CAPABILITIES.items() if ROLE_TAG in info.get("natural_roles", [])]


def _select_workers(worker_count: int) -> list:
    pool = _eligible_pool()
    if not pool:
        raise RuntimeError(
            "note_table_builder: no accounts tagged 'note_table_builder' in AGENT_CAPABILITIES."
        )
    snapshot = get_quota_snapshot()
    ranked = sorted(pool, key=lambda k: (snapshot.get(k) or {}).get("pct") or 0.0)
    return ranked[:worker_count]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _system_prompt(field_names: list[str]) -> str:
    fields_desc = "\n".join(f'- "{f}"' for f in field_names)
    return (
        "You are a careful extraction assistant. Given one source's title "
        "and content, extract exactly these fields, only using what the "
        "text actually states or clearly implies:\n"
        f"{fields_desc}\n"
        "Use null for any field not actually stated in the text — do not "
        "infer, estimate, or guess a value. Output ONLY a JSON object with "
        "exactly these keys, no markdown, no explanation."
    )


def _extract_one_item(iid: str, title: str, content: str, key_env: str,
                       field_names: list[str], session_id: str = None) -> tuple[str, dict]:
    """Runs on one worker thread with one fixed Groq key -- mirrors
    agents/extraction_table_builder.py's _extract_one_paper() shape
    exactly, just reading a plain (title, content) pair instead of a
    paper's title/abstract. Generalized over BOTH callers below: the
    topic path passes a topic's name + Mode B excerpts (or its own
    summary, same fallback agents/fact_detector.py's retrofitted
    _context_for() uses) and the raw-node fallback path passes a node's
    title + content straight off eo/knowledge_graph.py -- this function
    itself doesn't need to know which.
    """
    chain = [{"provider": "groq", "model": m, "key_env": key_env} for m in MODELS]
    user_content = json.dumps({"title": title or "Untitled", "content": (content or "")[:4000]})

    fallback = {name: None for name in field_names}
    try:
        raw = generate_text(
            _system_prompt(field_names), user_content, chain,
            agent_name="note_table_worker", session_id=session_id,
        )
        parsed = json.loads(_strip_fences(raw))
        fields = {name: parsed.get(name) for name in field_names}
    except (RuntimeError, json.JSONDecodeError, AttributeError):
        fields = dict(fallback)
        fields["extraction_error"] = True

    return iid, fields


def _run_extraction(items: dict, field_names: list[str], expanded: bool,
                     session_id: str = None) -> dict:
    """Shared worker-pool shape for both the topic path and the raw-node
    fallback in build_table() below -- one worker per item, merged back
    in the caller's own item order rather than as_completed() order, so
    the table reads the same regardless of which worker finished first
    (same ordering choice agents/extraction_table_builder.py makes).

    `items` maps an id (topic_id or node_id) to a (title, content) pair.
    Returns {id: fields_dict}, one entry per item in `items`.
    """
    worker_count = min(len(items), 8 if expanded else 5)
    key_envs = _select_workers(worker_count)

    results = {}
    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _extract_one_item, iid, title, content, key_envs[i % len(key_envs)],
                field_names, session_id=session_id,
            ): (iid, title)
            for i, (iid, (title, content)) in enumerate(items.items())
        }
        for future in as_completed(futures):
            iid, title = futures[future]
            _, fields = future.result()
            results[iid] = fields
            print(f"    [Note Table Builder] extracted: {title}")
    return results


def build_table(workspace_id: str, field_names: list[str], node_type: str = None,
                 expanded: bool = False, session_id: str = None) -> dict:
    """Reads every topic in `workspace_id`'s Secondary Data (Mode B/C,
    via source_planner_lean.plan()) and extracts `field_names` from
    each, one worker per topic, merged into one row per topic in the
    packet's own topic order -- not as_completed() order, so the table
    reads the same regardless of which worker happened to finish first,
    exactly agents/extraction_table_builder.py's own ordering choice.

    FIX: when the workspace has gone through Notebooks' topic clustering
    (packet["topics"] non-empty), that stays the whole story -- `node_type`
    is still accepted-but-unused there, per the module docstring's CHANGED
    note. But when plan() comes back with NO topics at all, that no longer
    means "nothing to extract" -- it means this workspace was never
    clustered into topics (e.g. a Research project, whose sources are
    academic_search-written `source` nodes that never touch Secondary
    Data). In that case this falls back to eo/knowledge_graph.py's
    list_nodes() and extracts one row per raw node instead, with
    `node_type` now acting as a real filter (e.g. "source" for
    ResearchTab.jsx's "Sources only" toggle, None/"" for "All content").

    Raises ValueError (not the paper module's MissingDependencyError --
    there's no upstream role for eo/executor.py to self-heal by
    inserting here, this is a plain "nothing to extract from yet") if
    field_names is empty, or if the workspace has neither topics nor any
    ingested nodes yet.
    """
    if not field_names:
        raise ValueError("field_names is required — there's nothing to extract otherwise.")

    packet = plan(
        workspace_id,
        task_text=(
            "Extract these exact fields from each topic, using only what "
            "is actually stated or clearly implied: " + ", ".join(field_names)
        ),
        scope="project",
        session_id=session_id,
    )
    topics = packet["topics"]

    if topics:
        items = {
            tid: (topic.get("name"), topic.get("excerpts") or topic.get("summary") or "")
            for tid, topic in topics.items()
        }
        extracted = _run_extraction(items, field_names, expanded, session_id=session_id)
        rows = [
            {"topic_id": tid, "title": topics[tid].get("name"), **extracted[tid]}
            for tid in topics
        ]
        return {
            "rows": rows,
            "field_names": field_names,
            "summary": f"Extracted {', '.join(field_names)} for {len(rows)} topic(s).",
        }

    # FIX -- no topic tree for this workspace (Research, or any other
    # domain that never runs Notebooks' clustering). Fall back to the
    # workspace's own ingested nodes -- the same eo/knowledge_graph.py
    # scan this module made before the CHANGED note above, restored as a
    # fallback rather than removed. `node_type` is a real filter here
    # (None/"" -> every node type in the workspace, matching "All
    # content"; "source" -> just source nodes, matching "Sources only").
    nodes = list_nodes(workspace_id, node_type=node_type or None)
    if not nodes:
        raise ValueError(
            f"No topics or ingested sources found in workspace {workspace_id!r} yet."
        )

    items = {n["node_id"]: (n.get("title"), n.get("content") or "") for n in nodes}
    extracted = _run_extraction(items, field_names, expanded, session_id=session_id)
    nodes_by_id = {n["node_id"]: n for n in nodes}
    rows = [
        {
            "node_id": nid,
            "title": nodes_by_id[nid].get("title"),
            "tags": nodes_by_id[nid].get("tags") or [],
            **extracted[nid],
        }
        for nid in items
    ]
    return {
        "rows": rows,
        "field_names": field_names,
        "summary": f"Extracted {', '.join(field_names)} for {len(rows)} source(s).",
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python note_table_builder.py <workspace_id> <field1,field2,...>")
    else:
        result = build_table(sys.argv[1], sys.argv[2].split(","))
        print(json.dumps(result, indent=2)[:1000])