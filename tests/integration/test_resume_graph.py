"""
tests/integration/test_resume_graph.py — Part 2 §2.4's human-in-the-loop
pause/resume checkpoint: eo/executor.py's execute_graph()/_run_loop()
pausing at an approval_roles role, and resume_graph() applying a human's
approve/edit/reject_redo decision and continuing the same run.

New file (not a move) — this checkpoint had no dedicated test coverage
before this pass. Uses role_names of "writer"/"editor" throughout: both
are plain reasoning roles with no dedicated module (see
eo/registry.py's resolve_role() — anything not in REAL_ACTION_ROLES
resolves to the literal "generic_worker"), so every step here actually
runs agents.generic_worker.run() for real, with only generate_text and
the two registry/conversation-memory lookups it needs mocked -- the
same "mock the LLM boundary, run everything else for real" posture
test_generic_worker_legacy_roles.py already established for this
module.

Finding: writing this file's first pause->resume round trip caught a
real, previously-uncaught bug — resume_graph() fires
emit_event("execution_resumed", session_id=session_id, ...) right after
applying the human's decision (eo/executor.py), but "execution_resumed"
was never added to relay/emitter.py's VALID_EVENT_TYPES. Since
resume_graph() always has a real session_id (a paused run can't exist
without one — it's the dict key), every real resume call hit
emit_event()'s `if event_type not in VALID_EVENT_TYPES: raise
ValueError(...)` immediately, regardless of whether Pusher was even
configured. Fixed directly in relay/emitter.py rather than masked here
(see that file's own comment on the fix) — this suite exercises the
real code path, unpatched, specifically so a regression here would be
caught again the same way.
"""
import pytest

import agents.generic_worker as generic_worker  # noqa: F401  (ensures mock_llm patches this module)
from eo.executor import execute_graph, resume_graph
from eo.registry import resolve_role
from memory.bus import read as bus_read


@pytest.fixture(autouse=True)
def _stub_role_lookups(monkeypatch):
    """Every role here is a brand-new, never-registered name — give
    generic_worker.run() a brief without depending on real Redis-stored
    role prompts, and skip the conversation-memory prepend so each
    step's prompt is just this test's own fixture data."""
    monkeypatch.setattr(generic_worker, "get_role_prompt", lambda role, user_id=None: f"You are the {role}.")
    monkeypatch.setattr(generic_worker.conversation_memory, "get_full_context", lambda session_id: "")


def _build_plan(roles):
    role_names = list(roles)
    agent_names = [resolve_role(r) for r in role_names]
    assert agent_names == ["generic_worker"] * len(role_names), (
        "test fixture roles must all resolve to generic_worker — pick different role names if this fails"
    )
    return agent_names, role_names


def test_pause_writes_snapshot_and_approve_resumes_to_completion(mock_llm, fake_bus):
    session_id = "resume-session-approve"
    agent_names, role_names = _build_plan(["writer", "editor"])
    mock_llm.set_sequence(["a first draft", "a polished final version"])

    paused = execute_graph(
        agent_names, role_names=role_names, task_text="write something",
        session_id=session_id, path="adaptive", approval_roles={"writer"},
    )

    assert paused == {"status": "paused", "paused_at_role": "writer"}
    snapshot = bus_read(f"paused_execution:{session_id}", default=None)
    assert snapshot is not None
    assert snapshot["idx"] == 0
    assert snapshot["results"]["writer"]["text"] == "a first draft"

    result = resume_graph(session_id, {"action": "approve"})

    assert result["writer"]["text"] == "a first draft"
    assert result["editor"]["text"] == "a polished final version"
    # Snapshot consumed — a finished run must not leave a stale pause
    # record a later resume_graph() call could mistakenly pick up.
    assert bus_read(f"paused_execution:{session_id}", default=None) is None


def test_edit_action_overwrites_text_and_persists_to_bus(mock_llm, fake_bus):
    session_id = "resume-session-edit"
    agent_names, role_names = _build_plan(["writer", "editor"])
    mock_llm.set_sequence(["a rough draft", "final, built on the edited draft"])

    paused = execute_graph(
        agent_names, role_names=role_names, task_text="write something",
        session_id=session_id, path="adaptive", approval_roles={"writer"},
    )
    assert paused["paused_at_role"] == "writer"

    result = resume_graph(session_id, {"action": "edit", "text": "a human-edited draft"})

    assert result["writer"]["text"] == "a human-edited draft"
    # The edit must land on the bus too, not just the in-memory results
    # dict -- any later generic_worker step reading this role's output
    # via input_keys needs to see the edited version. resume_graph()
    # edits in place onto the paused role's own stored result dict
    # (role/next_destination preserved, only "text" overwritten), not a
    # bare {"text": ...} -- see eo/executor.py's resume_graph() docstring.
    bus_value = bus_read(f"stage_output:{session_id}:writer", default=None)
    assert bus_value["text"] == "a human-edited draft"
    assert bus_value["role"] == "writer"
    assert result["editor"]["text"] == "final, built on the edited draft"


def test_reject_redo_reruns_the_role_and_respects_max_revisits(mock_llm, fake_bus):
    session_id = "resume-session-reject"
    agent_names, role_names = _build_plan(["writer"])
    mock_llm.set_sequence(["draft attempt 1", "draft attempt 2", "draft attempt 3"])

    paused = execute_graph(
        agent_names, role_names=role_names, task_text="write something",
        session_id=session_id, path="adaptive", approval_roles={"writer"},
    )
    assert paused["paused_at_role"] == "writer"

    # reject_redo #1 (visits 0 -> 1): re-runs "writer" from scratch, and
    # since "writer" is still in approval_roles, pauses again immediately.
    redo_1 = resume_graph(session_id, {"action": "reject_redo"})
    assert redo_1 == {"status": "paused", "paused_at_role": "writer"}

    # reject_redo #2 (visits 1 -> 2): same shape, still under the cap.
    redo_2 = resume_graph(session_id, {"action": "reject_redo"})
    assert redo_2 == {"status": "paused", "paused_at_role": "writer"}

    # reject_redo #3: visits is now 2, which is >= MAX_STAGE_REVISITS (2)
    # -- must raise rather than loop forever, and must not leave a
    # snapshot behind for a caller to mistakenly resume again.
    with pytest.raises(RuntimeError):
        resume_graph(session_id, {"action": "reject_redo"})
    assert bus_read(f"paused_execution:{session_id}", default=None) is None
    # All three generate_text calls for "writer" were actually made
    # (initial run + 2 successful redos before the cap raised).
    assert mock_llm.mock.call_count == 3


def test_multiple_approval_roles_chain_through_separate_pauses(mock_llm, fake_bus):
    session_id = "resume-session-multi-pause"
    agent_names, role_names = _build_plan(["writer", "editor", "publisher"])
    mock_llm.set_sequence(["draft", "edited draft", "published output"])

    paused_at = []
    result = execute_graph(
        agent_names, role_names=role_names, task_text="write something",
        session_id=session_id, path="adaptive", approval_roles={"writer", "editor"},
    )
    paused_at.append(result["paused_at_role"])

    result = resume_graph(session_id, {"action": "approve"})
    assert result["status"] == "paused"
    paused_at.append(result["paused_at_role"])

    result = resume_graph(session_id, {"action": "approve"})

    assert paused_at == ["writer", "editor"]
    assert result["writer"]["text"] == "draft"
    assert result["editor"]["text"] == "edited draft"
    assert result["publisher"]["text"] == "published output"


def test_resume_with_no_paused_run_raises_keyerror(fake_bus):
    with pytest.raises(KeyError):
        resume_graph("a-session-that-never-paused", {"action": "approve"})


def test_unknown_resume_action_raises_valueerror(mock_llm, fake_bus):
    session_id = "resume-session-bad-action"
    agent_names, role_names = _build_plan(["writer"])
    mock_llm.set_response("draft")

    execute_graph(
        agent_names, role_names=role_names, task_text="write something",
        session_id=session_id, path="adaptive", approval_roles={"writer"},
    )

    with pytest.raises(ValueError):
        resume_graph(session_id, {"action": "not_a_real_action"})
