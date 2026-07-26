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
pull. `node_type` is accepted for call-site compatibility but no longer
filters anything -- Secondary Data's topics are already derived
exclusively from ingested sources, so the one production value this
ever carried ("source") described a filter that's now the topic tree's
default population, not an opt-in.

Place this file at: agents/note_table_builder.py
"""
import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan
from eo.registry import AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot
from utils.llm_client import generate_text

# Two-model fallback per worker, same shorter-than-code_writers.py
# reasoning agents/extraction_table_builder.py already gives: extraction
# is a small, cheap completion, not a whole module.
MODELS = [
    "llama-3.3-70b-versatile",
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


def _extract_one_topic(tid: str, topic: dict, key_env: str, field_names: list[str],
                        session_id: str = None) -> tuple[str, dict]:
    """Runs on one worker thread with one fixed Groq key -- mirrors
    agents/extraction_table_builder.py's _extract_one_paper() shape
    exactly, just reading a topic's name/excerpts instead of a paper's
    title/abstract. `content` is the topic's Mode B excerpts if
    source_planner_lean flagged it, otherwise its own summary -- same
    fallback agents/fact_detector.py's retrofitted _context_for() uses.
    """
    title = topic.get("name") or "Untitled"
    content = topic.get("excerpts") or topic.get("summary") or ""
    chain = [{"provider": "groq", "model": m, "key_env": key_env} for m in MODELS]
    user_content = json.dumps({"title": title, "content": content[:4000]})

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

    return tid, fields


def build_table(workspace_id: str, field_names: list[str], node_type: str = None,
                 expanded: bool = False, session_id: str = None) -> dict:
    """Reads every topic in `workspace_id`'s Secondary Data (Mode B/C,
    via source_planner_lean.plan()) and extracts `field_names` from
    each, one worker per topic, merged into one row per topic in the
    packet's own topic order -- not as_completed() order, so the table
    reads the same regardless of which worker happened to finish first,
    exactly agents/extraction_table_builder.py's own ordering choice.

    `node_type` is accepted but unused -- see module docstring's
    CHANGED note.

    Raises ValueError (not the paper module's MissingDependencyError --
    there's no upstream role for eo/executor.py to self-heal by
    inserting here, this is a plain "nothing to extract from yet")
    if field_names is empty or the workspace has no topics yet.
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
    if not topics:
        raise ValueError(f"No topics found in workspace {workspace_id!r} yet.")

    worker_count = min(len(topics), 8 if expanded else 5)
    key_envs = _select_workers(worker_count)

    rows_by_id = {}
    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _extract_one_topic, tid, topic, key_envs[i % len(key_envs)], field_names,
                session_id=session_id,
            ): tid
            for i, (tid, topic) in enumerate(topics.items())
        }
        for future in as_completed(futures):
            tid = futures[future]
            _, fields = future.result()
            rows_by_id[tid] = {
                "topic_id": tid,
                "title": topics[tid].get("name"),
                **fields,
            }
            print(f"    [Note Table Builder] extracted: {topics[tid].get('name')}")

    rows = [rows_by_id[tid] for tid in topics]
    return {
        "rows": rows,
        "field_names": field_names,
        "summary": f"Extracted {', '.join(field_names)} for {len(rows)} topic(s).",
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python note_table_builder.py <workspace_id> <field1,field2,...>")
    else:
        result = build_table(sys.argv[1], sys.argv[2].split(","))
        print(json.dumps(result, indent=2)[:1000])