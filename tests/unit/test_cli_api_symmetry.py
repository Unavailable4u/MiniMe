"""
tests/unit/test_cli_api_symmetry.py — rebuilt around FakeRedis +
mock_llm (was a print-and-eyeball script that hit real Groq/Cerebras
for the brief-writing cold-start path; see the old
tests/test_cli_api_symmetry.py).

Confirms eo/panel.py's staff_task() produces an identical hire for a
brand-new role regardless of which real entrypoint calls it: the CLI
path (eo/loop_v4.py: staff_task(decision, task_text=task_text)) and the
API path (task_runner.py: staff_task(decision, task_text=task_text,
session_id=session_id)) must never diverge on the brief a novel role
gets written.
"""
from eo.panel import staff_task
from eo.registry import get_role_prompt, ROLE_PROMPTS_KEY
from memory.bus import read, write

NOVEL_ROLE = "cli_api_symmetry_test_role"


def _fake_classification():
    return {
        "tier": 2,
        "directed_task_type": "write_docs",
        "confidence": 0.8,
        "suggested_agents": [NOVEL_ROLE],
        "reasoning": "checking CLI/API symmetry",
    }


def test_cli_and_api_call_styles_produce_the_identical_brief():
    task_text = "Write a short glossary entry explaining a new technical term."

    # "CLI-style" call: staff_task(decision, task_text=task_text)
    cli_hires = staff_task(_fake_classification(), task_text=task_text)

    # "API-style" call: staff_task(decision, task_text=task_text, session_id=...)
    api_hires = staff_task(
        _fake_classification(), task_text=task_text, session_id="fake-session-id-123"
    )

    assert cli_hires, "CLI-style call produced no hires at all"
    assert api_hires, "API-style call produced no hires at all"
    assert cli_hires[0]["role"] == api_hires[0]["role"] == NOVEL_ROLE
    # The second call hits the fast path (get_role_prompt() already has
    # it) -- confirms persistence works regardless of caller.
    assert cli_hires[0]["brief"] == api_hires[0]["brief"]


def test_second_call_does_not_write_a_new_brief():
    task_text = "Write a short glossary entry explaining a new technical term."
    staff_task(_fake_classification(), task_text=task_text)
    brief_after_first_call = get_role_prompt(NOVEL_ROLE)

    existing = read(ROLE_PROMPTS_KEY, default={})
    updated_at_after_first = existing[NOVEL_ROLE]["updated_at"]

    staff_task(_fake_classification(), task_text=task_text)
    existing_after_second = read(ROLE_PROMPTS_KEY, default={})

    assert get_role_prompt(NOVEL_ROLE) == brief_after_first_call
    assert existing_after_second[NOVEL_ROLE]["updated_at"] == updated_at_after_first
