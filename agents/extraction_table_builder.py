"""
agents/extraction_table_builder.py — Structured multi-paper extraction
(Part 3 §3.5).

REAL_ACTION_ROLES tool agent, modeled directly on agents/code_writers.py's
shape: N papers, N parallel workers via ThreadPoolExecutor, one worker per
paper (round-robin over keys if there are more papers than workers, exactly
like code_writers.py already handles more than 5 modules). Each worker
extracts a fixed set of structured fields (sample size, methodology,
population, outcome, effect size) from one paper's title/abstract into
JSON. Cheaper than code generation, so this runs on Groq's fast pool
rather than needing Cerebras's code-tuned models -- see the
"extraction_table_builder" tag added to the existing Reviewer Pool
accounts in eo/registry.py's AGENT_CAPABILITIES (base 3 + reserve 2,
same accounts, same fairness-rotation selection code_writers.py uses).

The merge step is a REAL, deliberate deviation from a agents/
review_aggregator.py-style port (Part 3 §3.5's own reasoning): each
paper's row is a distinct fact about a distinct source, not a duplicate
of another paper's row -- there is no fuzzy-similarity collapsing here.
The only dedup this module needs is EXACT-ID (same DOI/title queued
twice), applied once, up front, before any worker call is spent on it.
Everything after that is a simple keyed union: one row per paper,
assembled into one table.

Input: KEYS["academic_search_report"] (agents/academic_search.py's
output, Part 3 §3.3). If that key is empty/missing, this raises
MissingDependencyError so eo/executor.py's adaptive-path self-heal can
splice "academic_search" in ahead of this step automatically, the same
mechanism other tool agents that depend on upstream data already rely
on.

Output written to KEYS["extraction_table"]:
{
  "papers": [{"paper_id", "node_id", "title", "authors", "year", "doi",
              "sample_size", "methodology", "population", "outcome",
              "effect_size"}],
  "field_names": ["sample_size", "methodology", "population", "outcome", "effect_size"],
  "summary": "...",
}
"""
import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from utils.llm_client import generate_text
from eo.registry import AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot
from eo.errors import MissingDependencyError

load_dotenv()

# Two-model fallback per worker, same key throughout -- shorter than
# code_writers.py's 3-model Cerebras rotation because extraction is a
# much smaller, cheaper completion (a handful of short fields, not a
# whole module), not because reliability matters less.
MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

FIELD_NAMES = ["sample_size", "methodology", "population", "outcome", "effect_size"]

# Matches the "extraction_table_builder" tag added to the Reviewer Pool
# accounts (GROQ_API_KEY_6/7/8 + GROQ_RESERVE_1/2) in eo/registry.py's
# AGENT_CAPABILITIES -- reusing that pool rather than inventing a new
# one, same reasoning code_writers.py's ROLE_TAG follows for "implementer".
ROLE_TAG = "extraction_table_builder"

SYSTEM_PROMPT = """You are a careful research-extraction assistant. Given one paper's \
title and abstract, extract exactly these five fields, only using what the \
text actually states or clearly implies:
- "sample_size": the study's sample size (e.g. "n=240", "1,200 participants"), or null if not stated.
- "methodology": the study design/method in a few words (e.g. "randomized controlled trial", "systematic review"), or null if not stated.
- "population": who or what was studied (e.g. "adults with type 2 diabetes"), or null if not stated.
- "outcome": the primary result or finding, in one sentence, or null if not stated.
- "effect_size": the reported effect size or magnitude (e.g. "OR 1.8", "d=0.42"), or null if not stated.

Do not infer, estimate, or guess a field that isn't actually in the text -- \
use null rather than making one up. Output ONLY a JSON object with exactly \
these five keys, no markdown, no explanation."""


def _eligible_pool() -> list:
    """Every account tagged for this role -- base AND reserve accounts
    alike, same shape as code_writers.py's _eligible_pool()."""
    return [key for key, info in AGENT_CAPABILITIES.items() if ROLE_TAG in info.get("natural_roles", [])]


def _select_workers(worker_count: int, key_override=None) -> list:
    """Panel-driven hires win outright; otherwise rank the full eligible
    pool by today's live usage and take the `worker_count` least-used
    accounts -- identical fairness rotation to code_writers.py's
    _select_workers()."""
    if key_override:
        return key_override if isinstance(key_override, list) else [key_override]
    pool = _eligible_pool()
    if not pool:
        raise RuntimeError(
            "extraction_table_builder: no accounts tagged 'extraction_table_builder' "
            "in AGENT_CAPABILITIES."
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


def _paper_key(paper: dict) -> str:
    """Exact-ID dedup only -- see module docstring on why this isn't
    fuzzy matching. DOI wins when present (most specific), title is the
    fallback (matches academic_search.py's own _dedup_key() logic)."""
    if paper.get("doi"):
        return f"doi:{paper['doi'].lower()}"
    return f"title:{(paper.get('title') or '').strip().lower()}"


def _extract_one_paper(paper: dict, key_env: str, worker_id: int,
                        session_id: str = None, path: str = None,
                        domain: str = None) -> tuple[str, dict]:
    """Runs on one worker thread with one fixed Groq key. Returns
    (paper_key, fields_dict). worker_id is this worker's key-slot number
    for labeling only -- not unique per paper once keys get reused
    round-robin for more papers than workers, same as code_writers.py."""
    paper_key = _paper_key(paper)
    title = paper.get("title") or "Untitled"
    agent_name = f"extraction_worker_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Extraction Worker {worker_id} — {title}"})
    started = time.monotonic()

    def _done(fields: dict) -> tuple[str, dict]:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = json.dumps(fields)
        summary = summary if len(summary) <= 300 else summary[:300] + "..."
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": summary, "duration_ms": duration_ms})
        return paper_key, fields

    chain = [{"provider": "groq", "model": m, "key_env": key_env} for m in MODELS]
    user_content = json.dumps({"title": title, "abstract": paper.get("abstract") or ""})

    fallback = {name: None for name in FIELD_NAMES}
    try:
        raw = generate_text(
            SYSTEM_PROMPT, user_content, chain,
            agent_name=agent_name, session_id=session_id, path=path, domain=domain,
        )
        parsed = json.loads(_strip_fences(raw))
        fields = {name: parsed.get(name) for name in FIELD_NAMES}
    except (RuntimeError, json.JSONDecodeError, AttributeError):
        fields = dict(fallback)
        fields["extraction_error"] = True

    return _done(fields)


def run(session_id: str = None, path: str = None, expanded: bool = False,
        key_override=None, task_text: str = None, domain: str = None) -> dict:
    """
    task_text is accepted but unused -- kept for signature parity with
    code_writers.py so eo/executor.py's dispatch can pass the same
    kwargs to either without a special case, even though this module's
    real input is KEYS["academic_search_report"], not task_text.

    key_override: same three shapes code_writers.py documents (None ->
    self-selected fairness rotation, a single key_env string -> use only
    that account, a list -> use exactly those accounts as the pool).
    """
    report = read(KEYS["academic_search_report"])
    papers = (report or {}).get("papers") or []
    if not papers:
        raise MissingDependencyError(required_role="academic_search")

    # Exact-ID dedup up front (Part 3 §3.5) -- collapses a paper that
    # genuinely got queued twice before any worker call is spent on it.
    # Not fuzzy matching: two DIFFERENT papers are never collapsed here.
    seen = set()
    deduped = []
    for paper in papers:
        key = _paper_key(paper)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(paper)

    worker_count = 8 if expanded else 5
    key_envs = _select_workers(worker_count, key_override)

    rows_by_key = {}
    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _extract_one_paper, paper, key_envs[i % len(key_envs)],
                (i % len(key_envs)) + 1, session_id=session_id, path=path,
                domain=domain,
            ): paper
            for i, paper in enumerate(deduped)
        }
        for future in as_completed(futures):
            paper = futures[future]
            paper_key, fields = future.result()
            row = {
                "paper_id": paper.get("paper_id") or paper_key,
                "node_id": paper.get("node_id"),
                "title": paper.get("title"),
                "authors": paper.get("authors", []),
                "year": paper.get("year"),
                "doi": paper.get("doi"),
                **fields,
            }
            rows_by_key[paper_key] = row
            print(f"    [Extraction Table Builder] extracted: {paper.get('title')}")

    # Keyed union, in the deduped input's original order -- not
    # insertion order from as_completed(), so the table reads the same
    # regardless of which worker happened to finish first.
    rows = [rows_by_key[_paper_key(paper)] for paper in deduped]

    table = {
        "papers": rows,
        "field_names": FIELD_NAMES,
        "summary": f"Extracted structured fields for {len(rows)} paper(s).",
    }
    write(KEYS["extraction_table"], table)
    return table


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))