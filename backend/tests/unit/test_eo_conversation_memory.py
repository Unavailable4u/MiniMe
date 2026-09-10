"""
tests/unit/test_eo_conversation_memory.py — Patch 7e (content/knowledge
group).

eo/conversation_memory.py had zero test coverage before this. Per its
own module docstring it's deliberately paired with (and distinct from)
eo/chat_store.py: this module is the agents' short-term, capped,
Redis-backed working memory (keyed by session_id, capped at
MAX_STORED_TURNS), while chat_store.py is the UI's durable record.
Coverage here focuses on:

  1. append_turn()'s cap enforcement (MAX_STORED_TURNS=20) and its
     fail-quiet no-op contract on a falsy session_id/text, since a
     silent behavior change here either lets Redis storage grow
     unbounded or drops real turns.
  2. get_full_context() / get_light_context()'s three-part assembly
     order — workspace facts, then linked-chat context, then the
     session's own recent turns — and their independent truncation
     limits (FULL_TURN_CHAR_LIMIT=1500 vs LIGHT_TURN_CHAR_LIMIT=120),
     since the module's own docstring calls out that the light path
     exists specifically to avoid "flooding the classifier's prompt"
     and "corrupting exact-match caching" — a regression that quietly
     widened the light path back toward full-size text would defeat
     both of those without any obviously broken behavior.
  3. append_turn()'s best-effort note-taker dispatch, which the
     module's own contract says must never propagate a failure back to
     the caller.

Isolation: this module does `from memory.bus import read, write` (names
bound directly into its own namespace), so the autouse `fake_bus`
fixture (tests/conftest.py) already covers it with zero per-test setup
-- read()/write() transparently hit an in-memory FakeRedis. `from eo
import chat_store / chat_workspace / workspace_facts` are module
imports, so those are patched as `conversation_memory.chat_store.X` /
`conversation_memory.chat_workspace.X` / `conversation_memory.
workspace_facts.X` for locality with the rest of this file, same
convention as test_eo_chat_store.py's resolve_chat_access tests.
"""
from unittest.mock import MagicMock

import pytest

from eo import conversation_memory

# Captured before any fixture can monkeypatch conversation_memory over
# it (see _default_ambiguous_classifier_stores below) -- the direct
# _classify_ambiguous_turn() tests further down restore this real
# implementation for their own duration so they can exercise it
# instead of the autouse stub every other test in this file relies on.
_REAL_CLASSIFY_AMBIGUOUS_TURN = conversation_memory._classify_ambiguous_turn


@pytest.fixture(autouse=True)
def _no_workspace_context(monkeypatch):
    """Most tests below aren't exercising the workspace-facts/linked-
    chat integration -- default both to their real "nothing to add"
    shape so append_turn/get_*_context tests can focus on the
    session's own transcript without needing to also stub every
    collaborator module. Tests that DO care override these
    explicitly."""
    monkeypatch.setattr(conversation_memory.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: None)
    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text",
                         lambda *a, **kw: "")


@pytest.fixture(autouse=True)
def _no_note_taker_dispatch(monkeypatch):
    """append_turn()'s note-taker dispatch does a lazy `from
    agents.note_taker import note_from_latest_turn_async` inside the
    function body -- patch the real module attribute so tests don't
    depend on (or trigger) the real note-taking pipeline."""
    from agents import note_taker
    monkeypatch.setattr(note_taker, "note_from_latest_turn_async", MagicMock())


@pytest.fixture(autouse=True)
def _default_ambiguous_classifier_stores(monkeypatch):
    """Autouse: _should_store()'s ambiguous tier (perf audit follow-up
    #3) does a lazy `from eo.sga import SGA_CHAINS` / `from
    utils.llm_client import generate_text` inside
    _classify_ambiguous_turn() -- stub that function directly so
    ordinary unit tests (most of which append short strings like
    "hello"/"hi"/"one") never attempt a real provider call. The
    default, (True, False) i.e. "store, via fallback," matches the
    store-if-unsure bias _classify_ambiguous_turn() is built around, so
    every pre-existing test in this file that expects a short turn to
    end up stored keeps working unchanged. Tests below that exercise
    the classifier itself, or a genuine SKIP verdict, override this
    explicitly."""
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn",
                         lambda *a, **kw: (True, False))


# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_is_session_namespaced():
    assert conversation_memory._key("sess_1") == "conversation:sess_1"


# ---------------------------------------------------------------------
# append_turn
# ---------------------------------------------------------------------

def test_append_turn_is_a_noop_when_session_id_is_falsy(fake_bus):
    conversation_memory.append_turn("", "user", "hello")
    assert fake_bus._dump() == {}


def test_append_turn_is_a_noop_when_text_is_falsy(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "")
    assert conversation_memory.read(conversation_memory._key("sess_1"), default=None) is None


def test_append_turn_stores_role_and_text(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "hello there")
    turns = conversation_memory.read(conversation_memory._key("sess_1"), default=[])
    assert turns == [{"role": "user", "text": "hello there"}]


def test_append_turn_accumulates_across_calls(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "one")
    conversation_memory.append_turn("sess_1", "assistant", "two")
    turns = conversation_memory.read(conversation_memory._key("sess_1"), default=[])
    assert [t["text"] for t in turns] == ["one", "two"]


def test_append_turn_caps_at_max_stored_turns_keeping_the_most_recent(fake_bus):
    for i in range(conversation_memory.MAX_STORED_TURNS + 5):
        conversation_memory.append_turn("sess_1", "user", f"turn {i}")

    turns = conversation_memory.read(conversation_memory._key("sess_1"), default=[])
    assert len(turns) == conversation_memory.MAX_STORED_TURNS
    # oldest 5 turns (0..4) dropped, most recent MAX_STORED_TURNS kept
    assert turns[0]["text"] == "turn 5"
    assert turns[-1]["text"] == f"turn {conversation_memory.MAX_STORED_TURNS + 4}"


def test_append_turn_dispatches_note_taker_only_for_assistant_turns(fake_bus):
    from agents import note_taker

    conversation_memory.append_turn("sess_1", "user", "question")
    note_taker.note_from_latest_turn_async.assert_not_called()

    conversation_memory.append_turn("sess_1", "assistant", "answer", owner_id="owner_1")
    note_taker.note_from_latest_turn_async.assert_called_once_with(
        "sess_1", "owner_1", "question", "answer")


def test_append_turn_note_taker_dispatch_failure_never_propagates(fake_bus, monkeypatch):
    from agents import note_taker
    monkeypatch.setattr(note_taker, "note_from_latest_turn_async",
                         MagicMock(side_effect=RuntimeError("dispatch failed")))

    # must not raise
    conversation_memory.append_turn("sess_1", "assistant", "answer", owner_id="owner_1")

    turns = conversation_memory.read(conversation_memory._key("sess_1"), default=[])
    assert turns[-1]["text"] == "answer"  # the turn is still stored


# ---------------------------------------------------------------------
# _should_store / append_turn's "worth storing" pre-filter
# (perf audit follow-up #3)
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "ok", "Ok!", "okay.", "  thanks  ", "Thanks so much!", "thx", "ty",
    "sounds good.", "sounds good!", "no", "nope", "cool", "great", "np",
    "lol", "haha", "+1", "👍", "K", "kk",
])
def test_should_store_skips_bare_acknowledgements(text):
    assert conversation_memory._should_store(text) is False


@pytest.mark.parametrize("text", [
    "please write a haiku about the sea",
    "make it shorter",
    "the login bug is in session refresh",
    "use TypeScript for this project",
])
def test_should_store_stores_text_at_or_above_the_definite_length_floor(text):
    assert len(text) >= conversation_memory._DEFINITELY_STORE_MIN_CHARS
    assert conversation_memory._should_store(text) is True


def test_should_store_escalates_short_non_filler_text_to_the_ambiguous_classifier(monkeypatch):
    classifier = MagicMock(return_value=(True, True))
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", classifier)

    result = conversation_memory._should_store("what time?")

    assert result is True
    classifier.assert_called_once()
    assert classifier.call_args.args[0] == "what time?"


def test_should_store_is_a_full_passthrough_when_the_filter_is_disabled(monkeypatch):
    monkeypatch.setattr(conversation_memory, "CONVERSATION_MEMORY_FILTER_ENABLED", False)
    classifier = MagicMock()
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", classifier)

    assert conversation_memory._should_store("ok") is True  # would otherwise be skipped
    classifier.assert_not_called()  # disabled filter shouldn't even run the deterministic tiers


def test_append_turn_does_not_store_a_bare_acknowledgement(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "thanks!")
    assert conversation_memory.read(conversation_memory._key("sess_1"), default=None) is None


def test_append_turn_stores_a_bare_acknowledgement_when_filter_disabled(fake_bus, monkeypatch):
    monkeypatch.setattr(conversation_memory, "CONVERSATION_MEMORY_FILTER_ENABLED", False)
    conversation_memory.append_turn("sess_1", "user", "thanks!")
    turns = conversation_memory.read(conversation_memory._key("sess_1"), default=[])
    assert turns == [{"role": "user", "text": "thanks!"}]


def test_append_turn_does_not_dispatch_note_taker_for_a_skipped_turn(fake_bus):
    from agents import note_taker
    conversation_memory.append_turn("sess_1", "user", "question here, a real one")
    conversation_memory.append_turn("sess_1", "assistant", "ok")  # bare ack, skipped
    note_taker.note_from_latest_turn_async.assert_not_called()


# ---------------------------------------------------------------------
# _classify_ambiguous_turn
# ---------------------------------------------------------------------

def test_classify_ambiguous_turn_returns_skip_on_a_skip_verdict(monkeypatch):
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", _REAL_CLASSIFY_AMBIGUOUS_TURN)
    import utils.llm_client as llm_client
    monkeypatch.setattr(llm_client, "generate_text", lambda **kw: "SKIP")

    store, used_llm = conversation_memory._classify_ambiguous_turn("np")

    assert (store, used_llm) == (False, True)


def test_classify_ambiguous_turn_returns_store_on_a_store_verdict(monkeypatch):
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", _REAL_CLASSIFY_AMBIGUOUS_TURN)
    import utils.llm_client as llm_client
    monkeypatch.setattr(llm_client, "generate_text", lambda **kw: "STORE")

    store, used_llm = conversation_memory._classify_ambiguous_turn("my flight is at 6")

    assert (store, used_llm) == (True, True)


def test_classify_ambiguous_turn_defaults_to_store_on_an_unparseable_answer(monkeypatch):
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", _REAL_CLASSIFY_AMBIGUOUS_TURN)
    import utils.llm_client as llm_client
    monkeypatch.setattr(llm_client, "generate_text", lambda **kw: "uh, sure I guess")

    store, used_llm = conversation_memory._classify_ambiguous_turn("hmm")

    assert (store, used_llm) == (True, False)


def test_classify_ambiguous_turn_defaults_to_store_on_a_provider_error(monkeypatch):
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", _REAL_CLASSIFY_AMBIGUOUS_TURN)
    import utils.llm_client as llm_client

    def boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_client, "generate_text", boom)

    store, used_llm = conversation_memory._classify_ambiguous_turn("hmm")

    assert (store, used_llm) == (True, False)


def test_classify_ambiguous_turn_skips_the_llm_call_entirely_when_disabled(monkeypatch):
    monkeypatch.setattr(conversation_memory, "_classify_ambiguous_turn", _REAL_CLASSIFY_AMBIGUOUS_TURN)
    import utils.llm_client as llm_client
    called = MagicMock()
    monkeypatch.setattr(llm_client, "generate_text", called)
    monkeypatch.setattr(conversation_memory, "CONVERSATION_MEMORY_AMBIGUOUS_LLM_ENABLED", False)

    store, used_llm = conversation_memory._classify_ambiguous_turn("hmm")

    assert (store, used_llm) == (True, False)
    called.assert_not_called()


# ---------------------------------------------------------------------
# get_conversation_memory_store_stats
# ---------------------------------------------------------------------

def test_get_conversation_memory_store_stats_starts_cold_not_zero(fake_bus):
    stats = conversation_memory.get_conversation_memory_store_stats()
    assert stats["total_stored"] == 0
    assert stats["total_skipped"] == 0
    assert stats["skip_rate"] is None


def test_get_conversation_memory_store_stats_counts_stored_and_skipped_turns(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "the login bug is in session refresh")  # definite store
    conversation_memory.append_turn("sess_1", "user", "thanks!")  # ack, skipped
    conversation_memory.append_turn("sess_1", "user", "thanks!")  # ack, skipped

    stats = conversation_memory.get_conversation_memory_store_stats()

    assert stats["stored_definite"] == 1
    assert stats["skipped_ack"] == 2
    assert stats["total_stored"] == 1
    assert stats["total_skipped"] == 2
    assert stats["skip_rate"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------
# _workspace_facts_text
# ---------------------------------------------------------------------

def test_workspace_facts_text_empty_without_session_id_or_owner_id():
    assert conversation_memory._workspace_facts_text("", "owner_1") == ""
    assert conversation_memory._workspace_facts_text("sess_1", None) == ""


def test_workspace_facts_text_empty_when_session_has_no_workspace(monkeypatch):
    monkeypatch.setattr(conversation_memory.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: None)
    assert conversation_memory._workspace_facts_text("sess_1", "owner_1") == ""


def test_workspace_facts_text_passes_owner_id_through_to_workspace_lookup(monkeypatch):
    calls = []

    def fake_workspace_for_chat(session_id, owner_id):
        calls.append((session_id, owner_id))
        return {"id": "ws_1"}

    monkeypatch.setattr(conversation_memory.chat_workspace, "workspace_for_chat",
                         fake_workspace_for_chat)
    monkeypatch.setattr(conversation_memory.workspace_facts, "format_facts_for_prompt",
                         lambda ws_id: f"facts for {ws_id}")

    result = conversation_memory._workspace_facts_text("sess_1", "owner_1")

    assert calls == [("sess_1", "owner_1")]
    assert result == "facts for ws_1"


# ---------------------------------------------------------------------
# get_full_context
# ---------------------------------------------------------------------

def test_get_full_context_empty_without_session_id():
    assert conversation_memory.get_full_context("") == ""


def test_get_full_context_returns_own_turns_when_nothing_linked(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "hello")
    conversation_memory.append_turn("sess_1", "assistant", "hi there")

    text = conversation_memory.get_full_context("sess_1")

    assert text == "[user]: hello\n\n[assistant]: hi there"


def test_get_full_context_only_uses_the_most_recent_max_turns(fake_bus):
    for i in range(10):
        conversation_memory.append_turn("sess_1", "user", f"turn {i}")

    text = conversation_memory.get_full_context(
        "sess_1", max_turns=conversation_memory.FULL_CONTEXT_TURNS)

    lines = text.split("\n\n")
    assert len(lines) == conversation_memory.FULL_CONTEXT_TURNS
    assert lines[0] == f"[user]: turn {10 - conversation_memory.FULL_CONTEXT_TURNS}"
    assert lines[-1] == "[user]: turn 9"


def test_get_full_context_truncates_each_turn_to_full_char_limit(fake_bus):
    long_text = "x" * (conversation_memory.FULL_TURN_CHAR_LIMIT + 50)
    conversation_memory.append_turn("sess_1", "user", long_text)

    text = conversation_memory.get_full_context("sess_1")

    assert text == f"[user]: {'x' * conversation_memory.FULL_TURN_CHAR_LIMIT}..."


def test_get_full_context_does_not_fetch_linked_context_without_owner_id(fake_bus, monkeypatch):
    linked_fn = MagicMock(return_value="should not be called")
    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text", linked_fn)
    conversation_memory.append_turn("sess_1", "user", "hi")

    conversation_memory.get_full_context("sess_1", owner_id=None)

    linked_fn.assert_not_called()


def test_get_full_context_prepends_linked_context_before_own_transcript(fake_bus, monkeypatch):
    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text",
                         lambda *a, **kw: "[Shared memory from chat \"Other\"]\n- user: hey")
    conversation_memory.append_turn("sess_1", "user", "hello")

    text = conversation_memory.get_full_context("sess_1", owner_id="owner_1")

    assert text.startswith('[Shared memory from chat "Other"]')
    assert "--- current conversation ---" in text
    assert text.endswith("[user]: hello")


def test_get_full_context_uses_only_linked_text_when_session_has_no_turns_yet(fake_bus, monkeypatch):
    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text",
                         lambda *a, **kw: "linked only")

    text = conversation_memory.get_full_context("sess_1", owner_id="owner_1")

    assert text == "linked only"
    assert "--- current conversation ---" not in text


def test_get_full_context_prepends_workspace_facts_first_when_present(fake_bus, monkeypatch):
    monkeypatch.setattr(conversation_memory.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: {"id": "ws_1"})
    monkeypatch.setattr(conversation_memory.workspace_facts, "format_facts_for_prompt",
                         lambda ws_id: "known facts")
    conversation_memory.append_turn("sess_1", "user", "hello")

    text = conversation_memory.get_full_context("sess_1", owner_id="owner_1")

    assert text.startswith("known facts\n\n")
    assert text.endswith("[user]: hello")


def test_get_full_context_returns_only_facts_when_nothing_else_present(fake_bus, monkeypatch):
    monkeypatch.setattr(conversation_memory.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: {"id": "ws_1"})
    monkeypatch.setattr(conversation_memory.workspace_facts, "format_facts_for_prompt",
                         lambda ws_id: "known facts")

    text = conversation_memory.get_full_context("sess_1", owner_id="owner_1")

    assert text == "known facts"


# ---------------------------------------------------------------------
# get_light_context
# ---------------------------------------------------------------------

def test_get_light_context_empty_without_session_id():
    assert conversation_memory.get_light_context("") == ""


def test_get_light_context_formats_as_dashed_one_liners(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "hello")
    conversation_memory.append_turn("sess_1", "assistant", "hi there")

    text = conversation_memory.get_light_context("sess_1")

    assert text == "- user: hello\n- assistant: hi there"


def test_get_light_context_truncates_each_turn_to_light_char_limit_not_full(fake_bus):
    long_text = "x" * (conversation_memory.LIGHT_TURN_CHAR_LIMIT + 50)
    conversation_memory.append_turn("sess_1", "user", long_text)

    text = conversation_memory.get_light_context("sess_1")

    assert text == f"- user: {'x' * conversation_memory.LIGHT_TURN_CHAR_LIMIT}..."
    # regression guard: the light path must stay far smaller than the
    # full path's own limit, per the module's own "don't flood the
    # classifier" contract.
    assert conversation_memory.LIGHT_TURN_CHAR_LIMIT < conversation_memory.FULL_TURN_CHAR_LIMIT


def test_get_light_context_strips_newlines_from_turn_text(fake_bus):
    conversation_memory.append_turn("sess_1", "user", "line one\nline two")

    text = conversation_memory.get_light_context("sess_1")

    assert text == "- user: line one line two"


def test_get_light_context_passes_its_own_smaller_linked_context_budget(fake_bus, monkeypatch):
    captured = {}

    def fake_linked(session_id, owner_id, max_turns_per_chat=None, char_limit=None):
        captured["max_turns_per_chat"] = max_turns_per_chat
        captured["char_limit"] = char_limit
        return ""

    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text", fake_linked)
    conversation_memory.append_turn("sess_1", "user", "hi")

    conversation_memory.get_light_context("sess_1", owner_id="owner_1")

    assert captured == {"max_turns_per_chat": 3, "char_limit": 150}


def test_get_full_context_passes_its_own_larger_linked_context_budget(fake_bus, monkeypatch):
    captured = {}

    def fake_linked(session_id, owner_id, max_turns_per_chat=None, char_limit=None):
        captured["max_turns_per_chat"] = max_turns_per_chat
        captured["char_limit"] = char_limit
        return ""

    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text", fake_linked)
    conversation_memory.append_turn("sess_1", "user", "hi")

    conversation_memory.get_full_context("sess_1", owner_id="owner_1")

    assert captured == {"max_turns_per_chat": 6, "char_limit": 400}
