"""
eo/fact_summarizer.py — Part 3 of the Data-bubble content work, extended
by Patch B2 to also emit per-account profile signals.

Called once per task from api/task_runner.py's _maybe_extract_content_fact(),
already gated to tier 2/3 responses only (see that function's docstring for
why cache/SGA/tier-0/tier-1 never reach here at all). Does relevance
filtering ("is this worth keeping") and summarization in a single model
call rather than two — the relevance question is folded into the same
structured output as the summary itself, so a "not memorable" result costs
exactly the same one call as a "memorable" one, not a separate filter step.

Patch B2 folds a second, independent question into that same call and
that same structured output, rather than paying for a second LLM call:
"does anything here say something about the *person*, not just the
project" — expertise level, a stated like/dislike, a recurring mistake
pattern, or an output-format preference. That's `profile_signals` below,
a (possibly empty) list living alongside the original
worth_remembering/category/title/summary fields. The two questions are
independent — a task can carry a profile signal with nothing
workspace-worthy in it (an aside about the user's own preference on an
otherwise one-off/trivial task), or a workspace fact with no profile
signal, or both, or neither. See extract_fact()'s docstring for exactly
how the two halves of the response combine into what gets returned.

profile_signals entries are routed by api/task_runner.py to
eo/user_profile.py's apply_profile_signal() (Patch B1), keyed by
owner_id — NOT written here. This module stays a pure extraction step
with no storage side effects of its own, same posture it already has for
the workspace-fact half (workspace_facts.record_section_entry() is also
only ever called from task_runner.py, never from here).

Not registered in eo/router.py or eo/panel.py's staffable-role tables like
a normal agent — this isn't something the Inspector/Panel ever hires; it's
a fixed post-processing step task_runner.py calls directly, closer in
shape to eo/sga.py's relay than to a Panel-staffed role.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.user_profile import PROFILE_SIGNAL_TYPES
from eo.workspace_facts import CATEGORY_TO_SECTION
from utils.llm_client import generate_text

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
# borrow another agent's dedicated quota. The former third-tier fallback
# (GITHUB_MODELS_PAT) is removed, not replaced, per quota-reality fix §4
# (2026-07-30, full GitHub Models retirement) -- Groq -> Cerebras is the
# full chain now.
CHAIN = [
    # llama-3.3-70b-versatile decommissioned by Groq; migrated to the two
    # models Groq's decommission notice suggested in its place.
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_RESERVE_1"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_RESERVE_1"},
    # OR-3f: Cerebras -> OpenRouter, same reserve slot (was
    # CEREBRAS_RESERVE_1). "openrouter/free" is OpenRouter's own
    # auto-router, not a pinned model slug (see utils/llm_client.py's
    # OR-1 notes).
    {"provider": "openrouter", "model": "openrouter/free", "key_env": "OPENROUTER_RESERVE_1"},
    # Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
    # its fallback step is removed here, not replaced.
]

# No provider in this chain supports response_format/json_mode on the
# groq/cerebras/github (OpenAI-SDK-shaped) path in utils/llm_client.py
# today — only the Cloudflare step supports json_mode, and this chain
# doesn't use Cloudflare. So JSON is enforced by instruction only, and
# _parse() below strips a ```json fence the same way other structured-
# output callers in this codebase already do, rather than assuming a
# bare JSON body.
SYSTEM_PROMPT = """You extract durable, workspace-level facts AND per-person profile signals \
from a completed task and its answer, for long-term memory. You will be given the task and its \
answer, and must decide (1) whether anything in it is worth remembering for future tasks in the \
same project, and (2) whether anything in it reveals something about the PERSON themselves \
(their skill level, a like/dislike, a recurring mistake, a format preference) rather than the \
project. These are two independent judgments — either, both, or neither can apply to the same \
task.

Respond with ONLY a JSON object and nothing else — no preamble, no markdown fences — matching \
exactly this shape:

{"worth_remembering": true or false, "category": "decision" | "preference" | "idea" | "context", "title": "...", "summary": "...", "profile_signals": [...]}

--- Part 1: worth_remembering / category / title / summary (workspace-level) ---

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

If worth_remembering is false, set category/title/summary to empty strings.

--- Part 2: profile_signals (person-level, independent of Part 1) ---

profile_signals is a list, usually empty, of objects shaped:

{"type": "expertise_signal" | "format_preference" | "error_pattern" | "like" | "dislike", "key": "...", "value": "...", "explicit": true or false}

Only include an entry when the task or answer actually shows one of these:
- "expertise_signal": the person's skill level at a topic became evident (e.g. key="React", \
value="intermediate" — inferred from how they asked, or explicit if they stated it outright).
- "format_preference": they showed or stated a preference for how answers should be delivered \
(key="default_format", value one of "text" | "markdown" | "artifact" | "diagram" | "code").
- "error_pattern": they made the same kind of mistake again (key names the pattern, e.g. \
"off-by-one in loop bounds", value is a short description).
- "like" / "dislike": a clear positive or negative reaction to a topic, tool, or approach \
(key is the topic, value is a short description of the reaction).

explicit is true only when the person stated the thing about themselves directly in their own \
words ("I'm still learning React", "I prefer diagrams", "I always mess this up"). explicit is \
false when you are inferring it from behavior or context rather than a direct statement. When \
in doubt, prefer leaving profile_signals empty over guessing — a false signal is worse than a \
missed one, since low-confidence inferred signals still need repeated corroboration before they \
matter, but a wrong explicit signal overwrites the record immediately.

Every entry needs a non-empty type from the list above, a non-empty value, and (except for \
format_preference, where key is fixed to "default_format") a non-empty key. If nothing in the \
task reveals anything about the person, return an empty list — do not manufacture a signal just \
to fill the field."""


def _parse(raw: str) -> dict:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def _validate_profile_signals(raw) -> list:
    """Patch B2. Defensive validation of the `profile_signals` half of
    the model's response, same fail-open posture as the rest of this
    module — a malformed or partially-malformed list degrades to
    dropping the bad entries, never to raising. Mirrors the shape
    `apply_profile_signal()` (eo/user_profile.py) expects, so
    task_runner.py can hand each validated entry straight through
    without re-checking it.

    Never raises: any unexpected shape (not a list, a non-dict item,
    missing/blank fields) is silently skipped rather than surfaced,
    consistent with extract_fact()'s "callers treat this whole
    function as fail-open" contract."""
    if not isinstance(raw, list):
        return []

    validated = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        signal_type = item.get("type")
        if signal_type not in PROFILE_SIGNAL_TYPES:
            continue
        value = item.get("value")
        if not value:
            continue
        is_format_preference = signal_type == "format_preference"
        key = "default_format" if is_format_preference else item.get("key")
        if not key:
            continue
        validated.append({
            "type": signal_type,
            "key": key,
            "value": value,
            "explicit": bool(item.get("explicit")),
        })
    return validated


def extract_fact(task_text: str, answer_text: str, session_id: str = None) -> dict:
    """Returns {"worth_remembering", "category", "title", "summary",
    "profile_signals"} — Patch B2 adds `profile_signals` (a possibly-
    empty list; see _validate_profile_signals()) alongside the
    original four fields.

    Returns None only when there is genuinely nothing to do with this
    task: the LLM call/parse itself failed, worth_remembering came
    back false AND profile_signals is empty, or the workspace-fact
    half was well-formed-but-invalid (unrecognized category, or a
    missing title/summary) AND profile_signals is empty. In every
    other case a dict is returned, even if only one of the two halves
    has anything in it — a profile signal on an otherwise
    not-worth-remembering task (an aside about the user's own
    preference on a one-off question) still needs to reach the
    caller, so it can't be treated as a full miss just because Part 1
    of the response was empty.

    When returned, the workspace-fact half of the dict is internally
    consistent: worth_remembering is only ever true when category/
    title/summary are all present and category is a recognized value;
    any workspace-fact validation failure resets worth_remembering to
    False and category/title/summary to "" rather than propagating a
    half-valid fact to record_section_entry()'s caller.

    Callers should treat None as "skip both writes, don't block the
    task response" (fail-open, same discipline
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

    if not isinstance(parsed, dict):
        return None

    profile_signals = _validate_profile_signals(parsed.get("profile_signals"))

    worth_remembering = bool(parsed.get("worth_remembering"))
    category, title, summary = "", "", ""
    if worth_remembering:
        if parsed.get("category") not in CATEGORY_TO_SECTION:
            print(f"  [fact_summarizer] unrecognized category {parsed.get('category')!r}, skipped")
            worth_remembering = False
        elif not parsed.get("title") or not parsed.get("summary"):
            worth_remembering = False
        else:
            category, title, summary = parsed["category"], parsed["title"], parsed["summary"]

    if not worth_remembering and not profile_signals:
        return None

    return {
        "worth_remembering": worth_remembering,
        "category": category,
        "title": title,
        "summary": summary,
        "profile_signals": profile_signals,
    }
