"""
agents/content_adapter_pool.py — Multi-Platform Content Fan-Out Pool
(Part 6 §6.2 of the v6 migration).

Mirrors agents/code_writers.py's shape directly: one core input (there,
module_specs; here, a content_brief) fans out into N independent
generate_text() calls run genuinely in parallel via ThreadPoolExecutor,
one worker per selected account, through the same
usage-logged/agent_start/agent_done path code_writers.py already uses.

Why this is a dedicated module instead of the default sequential
generic_worker path (Part 1 §1.4's standing default for new domains):
platform variants of one core message are independent outputs from one
spec — no step needs to see another step's output first — which is
exactly the shape code_writers.py already solved. Unlike Part 1's
personas (which DO need to run sequentially so simulation_synthesizer can
read all of them together), content fan-out has no such ordering
constraint, so it earns skipping straight to the dedicated pool.

Worker selection reuses eo/worker_pool.py's shared, role_tag-parameterized
helper (Part 6 §6.2's extraction) with role_tag="content_writer" — the
exact same quota-aware, fairness-ranked account selection code_writers.py
uses for role_tag="implementer", not a second copy that could drift.
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
from eo.worker_pool import _select_workers as _select_workers_for_role
from eo import conversation_memory

load_dotenv()

ROLE_TAG = "content_writer"

# Base instruction every platform worker gets, ahead of its own
# platform-specific addition below. Deliberately generic — the actual
# voice/tone comes from workspace_facts (Part 0 §0.3), prepended into
# user_content the same way generic_worker.py's context_parts already
# does for every reasoning role in this build order (see
# _write_one_variant() below); this system prompt only sets the
# structural/format rules for the platform itself.
BASE_SYSTEM_PROMPT = """You are a skilled content adapter. Given a core \
message, rewrite it for the specific platform described below. Preserve \
the core message's meaning and key facts exactly — do not invent claims \
the core message doesn't make. Output ONLY the adapted content, no \
explanation, no markdown code fences, no surrounding quotation marks."""

# Per-platform structural rules (tone, length ceiling, format conventions).
# Not an exhaustive list of every platform a task could name — an
# unrecognized platform string falls back to DEFAULT_PLATFORM_PROMPT
# rather than failing, so a brief naming e.g. "reddit_post" still gets a
# reasonable, if generic, adaptation.
PLATFORM_PROMPTS = {
    "twitter": """
Platform: X/Twitter. Hard constraint: 280 characters maximum, including \
spaces and punctuation. No hashtags unless the core message explicitly \
mentions a campaign tag. Punchy, direct, one idea.""",
    "x": """
Platform: X/Twitter. Hard constraint: 280 characters maximum, including \
spaces and punctuation. No hashtags unless the core message explicitly \
mentions a campaign tag. Punchy, direct, one idea.""",
    "linkedin": """
Platform: LinkedIn. Professional tone, 3-6 short paragraphs, no more than \
1300 characters. Open with a hook line. End with a soft call to action. \
Avoid emoji-heavy or overly casual phrasing.""",
    "instagram_caption": """
Platform: Instagram caption. Warm, conversational tone, 1-3 short \
paragraphs, up to 2200 characters. Light emoji use is acceptable if it \
fits the brand voice. Put a call to action on its own final line.""",
    "press_release": """
Platform: Press release. Use a structured dateline format: a headline \
line, a dateline (CITY, Month Day, Year —), then formal third-person body \
paragraphs in inverted-pyramid order (most important fact first). Close \
with a boilerplate-style final paragraph if the core message supports \
one. No first-person voice, no emoji.""",
    "facebook": """
Platform: Facebook post. Conversational, 2-4 sentences, plain language, \
no more than 500 characters. Fine to ask a light question to invite \
comments.""",
    "blog_intro": """
Platform: Blog post introduction. 2-4 paragraphs, engaging opening \
sentence, set up why the reader should keep reading. No heading, just the \
opening paragraphs.""",
}

DEFAULT_PLATFORM_PROMPT = """
Platform: {platform}. No specific format rules are on file for this \
platform — use clear, well-structured prose appropriate to a general \
public audience, and keep it to a reasonable single-post length."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1].strip()
    return text


def _write_one_variant(platform: str, core_message: str, key_env: str, worker_id: int,
                        session_id: str = None, path: str = None,
                        domain: str = None) -> tuple[str, str]:
    """Runs on one worker thread with one fixed account. Returns
    (platform, content). Mirrors code_writers.py's _write_one_module()
    almost exactly — same generate_text()/agent_start/agent_done shape —
    with one addition: prepends this session's workspace-facts/
    conversation context (Part 0 §0.3, via eo/conversation_memory.py's
    get_full_context()) ahead of the core message, the same way
    generic_worker.py's context_parts already does for every reasoning
    role. This is the actual mechanism that lets a stored brand voice
    reach generation — code_writers.py's plain json.dumps(module_spec)
    user_content has no equivalent need, since code has no "voice"."""
    agent_name = f"content_adapter_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Content Adapter {worker_id} — {platform}"})
    started = time.monotonic()

    def _done(content: str) -> tuple[str, str]:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = content if len(content) <= 300 else content[:300] + "..."
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": summary, "duration_ms": duration_ms})
        return platform, content

    system_prompt = BASE_SYSTEM_PROMPT + PLATFORM_PROMPTS.get(
        platform, DEFAULT_PLATFORM_PROMPT.format(platform=platform))

    context_parts = [f"CORE MESSAGE:\n{core_message}"]
    conv_context = conversation_memory.get_full_context(session_id)
    if conv_context:
        context_parts.insert(0, f"--- Brand voice & recent context ---\n{conv_context}")
    user_content = "\n\n".join(context_parts)

    chain = [{"provider": "cerebras", "model": "gpt-oss-120b", "key_env": key_env}]

    try:
        raw = generate_text(
            system_prompt,
            user_content,
            chain,
            agent_name=agent_name,
            session_id=session_id,
            path=path,
            domain=domain,
        )
        content = _strip_fences(raw)
        if not content:
            content = f"CONTENT ADAPTER FAILED: model returned empty content for platform '{platform}'."
    except RuntimeError as exc:
        content = f"CONTENT ADAPTER FAILED: {exc}"

    return _done(content)


def _derive_brief_from_task_text(task_text: str, session_id: str = None,
                                  domain: str = None) -> dict:
    """Fallback brief synthesis for when this pool is hired without an
    upstream generic_worker role having already written
    KEYS["content_targets"] — the same gap code_writers.py's
    _derive_specs_from_task_text() fixes for module_specs, and the same
    reasoning: don't crash on `brief["platforms"]` when nothing wrote a
    brief yet, ask the same kind of single-shot question seeded from the
    raw task text instead."""
    brief_prompt = """You are a content strategist. Given a task description, \
extract the core message and the target platforms it should be adapted \
for. If the task doesn't name specific platforms, choose 2-4 reasonable \
ones for the described launch/announcement.

Output ONLY a JSON object with:
- "core_message": a concise statement of the message to adapt (string)
- "platforms": a list of platform identifiers, using these exact strings \
where they apply: "twitter", "linkedin", "instagram_caption", \
"press_release", "facebook", "blog_intro" — or another short lowercase \
identifier if none of those fit.

Respond with ONLY valid JSON, no markdown, no explanation."""
    chain = [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
        {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
    ]
    try:
        raw_text = generate_text(
            brief_prompt, f"Task: {task_text}", chain,
            agent_name="Content Adapter Pool (brief fallback)", session_id=session_id,
            domain=domain,
        ).strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()
        brief = json.loads(raw_text)
        if not brief.get("platforms"):
            raise ValueError("empty platforms list")
    except Exception:
        # Last-resort single-platform brief so this never hard-crashes
        # even if the fallback LLM call itself fails.
        brief = {
            "core_message": task_text or "Announce the update.",
            "platforms": ["twitter"],
        }
    write(KEYS["content_targets"], brief)
    return brief


def run(session_id: str = None, path: str = None, expanded: bool = False,
        key_override=None, task_text: str = None, domain: str = None):
    """
    key_override: same three-shape contract as code_writers.py's run() —
    None (pick own workers via _select_workers), a single key_env string
    (use only that account for every platform), or a list (use exactly
    those accounts as the parallel worker pool).

    task_text: fallback seed for _derive_brief_from_task_text() when
    KEYS["content_targets"] hasn't been written yet by an upstream
    generic_worker role. Optional, same as code_writers.py's task_text.
    """
    brief = read(KEYS["content_targets"])
    if not brief or not brief.get("platforms"):
        brief = _derive_brief_from_task_text(task_text, session_id=session_id, domain=domain)

    core_message = brief.get("core_message", task_text or "")
    platforms = brief["platforms"]
    results = {}

    # Fixed pool size, same as code_writers.py's run() — NOT scaled to
    # len(platforms). If there are more platforms than workers, keys are
    # reused round-robin below (the same worker/key doing a second
    # platform), matching code_writers.py's own documented behavior for
    # more-than-5 modules.
    worker_count = 8 if expanded else 5
    key_envs = _select_workers_for_role(ROLE_TAG, worker_count, key_override)

    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _write_one_variant, platform, core_message, key_envs[i % len(key_envs)],
                (i % len(key_envs)) + 1, session_id=session_id, path=path,
                domain=domain,
            ): platform
            for i, platform in enumerate(platforms)
        }
        for future in as_completed(futures):
            platform, content = future.result()
            results[platform] = content
            print(f"    [Content Adapter Pool] wrote variant: {platform} ({len(content)} chars)")

    write(KEYS["platform_content"], results)
    return results


if __name__ == "__main__":
    results = run()
    for platform, content in results.items():
        print(f"\n=== {platform} ===")
        print(content[:300] + ("..." if len(content) > 300 else ""))