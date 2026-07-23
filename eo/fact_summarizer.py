"""
eo/fact_summarizer.py — Part 3 of the Data-bubble content work.

Called once per task from api/task_runner.py's _maybe_extract_content_fact(),
already gated to tier 2/3 responses only (see that function's docstring for
why cache/SGA/tier-0/tier-1 never reach here at all). Does relevance
filtering ("is this worth keeping") and summarization in a single model
call rather than two — the relevance question is folded into the same
structured output as the summary itself, so a "not memorable" result costs
exactly the same one call as a "memorable" one, not a separate filter step.

Not registered in eo/router.py or eo/panel.py's staffable-role tables like
a normal agent — this isn't something the Inspector/Panel ever hires; it's
a fixed post-processing step task_runner.py calls directly, closer in
shape to eo/sga.py's relay than to a Panel-staffed role.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from eo.workspace_facts import CATEGORY_TO_SECTION

# Generic fallback chain. Deliberately NOT eo/sga.py's dedicated
# per-account SGA_GROQ_1/2/3 keys (SGA is latency-sensitive; this call
# runs once per tier-2/3 task, well after SGA is done, so no reason to
# compete with it) and NOT GROQ_API_KEY / CEREBRAS_API_KEY_9 either —
# per env(example).txt, GROQ_API_KEY is already shared by five
# sequential production agents, and CEREBRAS_API_KEY_9 is the Structure
# Architect's deliberately *isolated* key (kept off the shared pool
# specifically to avoid queuing behind other agents' bursts — reusing
# it here would undo that isolation). GROQ_RESERVE_1 / CEREBRAS_RESERVE_1
# are the unfilled "reserved for Part 3" slots already sitting in the
# env file for exactly this situation: a new agent that shouldn't
# borrow another agent's dedicated quota. GITHUB_MODELS_PAT is kept as
# the third-tier fallback since it's already the documented shared
# fallback for the whole 19-agent roster and this call is low-volume,
# third-tier-only traffic.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_RESERVE_1"},
    {"provider": "cerebras", "model": "llama-3.3-70b", "key_env": "CEREBRAS_RESERVE_1"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

# No provider in this chain supports response_format/json_mode on the
# groq/cerebras/github (OpenAI-SDK-shaped) path in utils/llm_client.py
# today — only the Cloudflare step supports json_mode, and this chain
# doesn't use Cloudflare. So JSON is enforced by instruction only, and
# _parse() below strips a ```json fence the same way other structured-
# output callers in this codebase already do, rather than assuming a
# bare JSON body.
SYSTEM_PROMPT = """You extract durable, workspace-level facts from a completed task and its \
answer, for a project's long-term memory. You will be given the task and its answer, and must \
decide whether anything in it is worth remembering for future tasks in the same project.

Respond with ONLY a JSON object and nothing else — no preamble, no markdown fences — matching \
exactly this shape:

{"worth_remembering": true or false, "category": "decision" | "preference" | "idea" | "context", "title": "...", "summary": "..."}

worth_remembering is true only for durable, reusable information — a stated preference \
("always use TypeScript"), a real decision made ("target this for students"), a concrete idea \
worth revisiting later, or important standing context (a fact about the project, a constraint, \
a correction). It is false for anything that's just the output of the task itself with nothing \
durable in it — a one-off code snippet, a direct factual answer with no lasting relevance, small \
talk, or routing/mechanical detail about how the task was resolved.

category must be exactly one of: "decision", "preference", "idea", "context". Pick the closest \
fit; never invent another value.

title is a short label (under 10 words). Reuse the same wording as a prior similar fact if this \
restates or updates it, so repeated statements of the same fact merge instead of piling up as \
separate entries.

summary is one or two plain sentences stating the fact itself — not the mechanics of how the \
task was resolved.

If worth_remembering is false, set category/title/summary to empty strings."""


def _parse(raw: str) -> dict:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def extract_fact(task_text: str, answer_text: str, session_id: str = None) -> dict:
    """Returns the parsed {"worth_remembering", "category", "title",
    "summary"} dict when the model judged the task worth remembering, or
    None in every other case — escalate/error, malformed JSON, an
    unrecognized category, a missing title/summary, or
    worth_remembering: false. Callers should treat None as "skip the
    write, don't block the task response" (fail-open, same discipline
    eo/workspace_facts.py's _invalidate_facts_cache() already uses) —
    this function never raises.

    task_text/answer_text are passed as-is; truncation or context
    trimming, if ever needed for very long tier-3 outputs, belongs to
    the caller, not here, since only the caller knows how much of the
    original text is safe to drop without losing the fact worth
    capturing.
    """
    user_content = f"Task:\n{task_text}\n\nAnswer:\n{answer_text}"
    try:
        raw = generate_text(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
            chain=CHAIN,
            agent_name="fact_summarizer",
            session_id=session_id,
        )
        parsed = _parse(raw)
    except Exception as exc:
        print(f"  [fact_summarizer] extraction call/parse failed, skipped (fail-open): {exc}")
        return None

    if not isinstance(parsed, dict) or not parsed.get("worth_remembering"):
        return None
    if parsed.get("category") not in CATEGORY_TO_SECTION:
        print(f"  [fact_summarizer] unrecognized category {parsed.get('category')!r}, skipped")
        return None
    if not parsed.get("title") or not parsed.get("summary"):
        return None
    return parsed
