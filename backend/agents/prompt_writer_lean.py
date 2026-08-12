"""
agents/prompt_writer_lean.py — Prompt/Spec Writer (lean), Part 2.4's
tier-1 pipeline, first step.

Reads tier1_task_text (the raw task, written by whatever drove this run —
normally eo/loop_v4.py), writes a single small module spec to
tier1_module_spec.

Part 2.4's table lists this as sharing a key with the production Prompt
Writer, with the concurrency caveat flagged in Part 12.1 — that's honored
here literally: GROQ_API_KEY is the same env var agents/prompt_writer.py
reads. Unlike the production version (which calls Groq directly with no
fallback), this one uses the full CHAIN/fallback pattern per Part 2.4's
table: originally Groq llama-3.3-70b-versatile -> GitHub Models
gpt-4.1-mini.

2026-08-12 fix: GitHub Models retired (see the §4 note below), which had
quietly dropped this agent back down to a single Groq step with zero real
fallback — the same "shares its one key with production, dies when it
cools down" bug already found and fixed in prompt_writer.py, test_writer.py,
schema_diagrammer.py, and dependency_mapper.py. Those four run through
eo/registry.py's build_fallback_chain(), but this is the tier-1 lean
pipeline, which deliberately bypasses the registry for speed (see
eo/registry.py:110) — build_fallback_chain() has nothing to resolve under
a "prompt_writer_lean" role. So the fix here is the same one
reviewer_fixer_lean.py already uses: a real, hardcoded second-provider
step, Cerebras on its own key/account, so a Groq-side outage or cooldown
on GROQ_API_KEY (shared with prompt_writer.py) doesn't take this agent
down with it.

Deliberately produces ONE module, not 2-3 like the production Prompt
Writer — Part 2.1 defines tier 1 as "a small, self-contained script or
single-file program," so there's nothing to split.

Part 23: prepends this session's full conversation-memory context
(eo/conversation_memory.py's get_full_context()) ahead of the task text
sent to the LLM, so a follow-up task has real prior content to build on.
Only the text actually sent to the model gets this prefix -- the stored
tier1_task_text stays the raw task text, unmodified, so anything else
that reads it later still gets a clean value.
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from eo import conversation_memory   # NEW — Part 23
from utils.llm_client import generate_text

# Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
# its fallback step is removed here, not replaced.
#
# 2026-08-12 fix: that left a single Groq step with no real fallback,
# sharing GROQ_API_KEY with prompt_writer.py -- one account cooldown and
# both agents go down together. Lean-pipeline agents bypass
# eo/registry.py's build_fallback_chain() by design (see eo/registry.py:
# 110), so unlike prompt_writer.py's own fix this can't just call into
# the registry -- it needs a real, hardcoded second-provider step
# instead, same approach as reviewer_fixer_lean.py's Groq -> Cerebras
# CHAIN.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_1"},
]

SYSTEM_PROMPT = """You are a technical spec writer for a lean, single-file build \
task. Given a task description, output ONLY a JSON object shaped like:
{
  "name": "short_module_name",
  "description": "what it does",
  "language": "the programming language requested in the task, e.g. python, c, javascript, bash — default to python if the task does not specify one",
  "inputs": "expected inputs",
  "outputs": "expected outputs",
  "edge_cases": ["list", "of", "edge", "cases", "to", "handle"],
  "constraints": ["any other explicit preferences from the task text, e.g. 'keep it short', 'no external libraries', 'use recursion', 'add comments' — empty list if none given"]
}
This must describe ONE self-contained module — do not split the task into \
multiple modules or files. Respond with ONLY valid JSON, no markdown, no \
explanation."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run(task_text: str = None, session_id: str = None, path: str = None,
        domain: str = None) -> dict:
    if task_text:
        write(KEYS["tier1_task_text"], task_text)
    else:
        task_text = read(KEYS["tier1_task_text"])
        if not task_text:
            raise ValueError(
                "No tier1_task_text found in memory and none passed in. "
                "This must be the first step of the tier-1 pipeline."
            )

    conv_context = conversation_memory.get_full_context(session_id)   # NEW — Part 23
    user_content = f"Task: {task_text}"
    if conv_context:
        user_content = f"Recent conversation:\n{conv_context}\n\n{user_content}"   # NEW — Part 23

    raw = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,   # CHANGED — Part 23, was f"Task: {task_text}"
        chain=CHAIN,
        agent_name="Prompt Writer (lean)",
        session_id=session_id,
        path=path,   # Migration Part 27 §1: generate_text() now accepts `path` for real
        domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
    )
    spec = json.loads(_strip_fences(raw))
    write(KEYS["tier1_module_spec"], spec)
    return spec


if __name__ == "__main__":
    result = run(" ".join(__import__("sys").argv[1:]) or "write a script that reverses a string from stdin")
    print(json.dumps(result, indent=2))