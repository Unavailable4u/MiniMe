"""
tests/integration/test_staff_task_e2e.py — end-to-end coverage for
eo/panel.py's staff_task(): a genuinely new role gets a real brief
written and persisted, and a fresh call for the same role is a
registry hit with zero extra LLM calls.

Moved from tests/test_staff_task_e2e.py (B1 audit) and rewritten: the
original file was a bare top-level script (asserts at import time, no
`test_*` functions at all), so pytest collection never actually ran
its checks as tests — and it called eo/registry.py's
read(ROLE_PROMPTS_KEY)/write(ROLE_PROMPTS_KEY, ...) directly against
whatever Redis memory.bus.redis pointed at, which is REAL Upstash
outside pytest (tests/conftest.py's autouse fake_bus fixture didn't
exist yet when this file was written) — a stray run against production
config could have deleted a real role-prompt registry entry via the
`del existing[NOVEL_ROLE]` cleanup step.

Now two proper pytest functions using the fake_bus/mock_llm fixtures
from tests/conftest.py: fake_bus makes every memory.bus call hit an
isolated in-memory store (nothing production-adjacent to accidentally
wipe), and mock_llm patches eo.panel's own generate_text so brief
writing needs zero real network/keys.
"""
from eo import panel  # noqa: F401  (ensures mock_llm patches this module)
from eo.panel import staff_task
from eo.registry import get_role_prompt

NOVEL_ROLE = "changelog_summarizer_test_role"  # not implementer/verifier/researcher/writer/fact_checker

FAKE_CLASSIFICATION = {
    "tier": 2,
    "directed_task_type": "write_docs",
    "confidence": 0.8,
    "suggested_agents": [NOVEL_ROLE],
    "reasoning": "task needs a role we've never seen before",
}

TASK_TEXT = "Summarize the last 10 commits into a punchy one-paragraph changelog entry."


def test_novel_role_gets_a_real_brief_written_and_persisted(fake_bus, mock_llm):
    # Confirmed not in the (fake, isolated) registry yet.
    assert get_role_prompt(NOVEL_ROLE) is None

    mock_llm.set_response(
        "Write a concise, punchy summary of recent changes suitable for a changelog entry."
    )

    hires = staff_task(FAKE_CLASSIFICATION, task_text=TASK_TEXT, session_id=None)

    assert len(hires) == 1, f"expected exactly 1 hire, got {len(hires)}"
    assert hires[0]["role"] == NOVEL_ROLE
    assert mock_llm.mock.called, "generate_text was never called — brief-writing did not reach the LLM step"

    stored_brief = get_role_prompt(NOVEL_ROLE)
    assert stored_brief == hires[0]["brief"]


def test_second_call_for_same_role_is_a_registry_hit_with_no_extra_llm_call(fake_bus, mock_llm):
    mock_llm.set_response("Write a concise, punchy summary of recent changes.")
    staff_task(FAKE_CLASSIFICATION, task_text=TASK_TEXT, session_id=None)
    assert mock_llm.mock.call_count == 1

    # A second staff_task() call for the exact same role must hit the
    # now-persisted registry entry (eo/panel.py's _get_or_write_role_prompt()
    # fast path) rather than writing (and paying for) a brand-new brief.
    hires_again = staff_task(FAKE_CLASSIFICATION, task_text=TASK_TEXT, session_id=None)
    assert mock_llm.mock.call_count == 1, "second call for an already-known role must not call the LLM again"
    assert hires_again[0]["brief"] == get_role_prompt(NOVEL_ROLE)
