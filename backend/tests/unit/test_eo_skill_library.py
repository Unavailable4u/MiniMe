"""
tests/unit/test_eo_skill_library.py — Patch 7e-S6.

eo/skill_library.py had zero test coverage before this. Two contracts
the module docstring calls out as load-bearing, not incidental:

  - get_relevant_skill()'s "" return is the actual signal a later
    self-improvement loop treats as "unfamiliar task type, worth
    researching" -- so a real miss (below-threshold match, empty
    query, or a Vector hiccup) must reliably come back as "", never a
    stale/wrong doc.
  - write_skill()'s Redis half always happens; the Vector embed/upsert
    half is best-effort and must never raise even if HF/Vector fails
    -- "the skill is saved but not yet retrievable," a degraded state,
    not a crashed write.

ensure_skill_for_task() gets its own section: the module docstring is
explicit that it "NEVER raises," end to end, across a research miss, a
condensation call returning "NONE"/empty, or any outright exception.

Isolation: skill_library.py does `from utils.embedding import
embed_text` and `from memory.bus import read, write, vector_index`
(bound names) -- tests patch `embed_text` and `vector_index` directly
on the skill_library module object, same gotcha
test_eo_routing_memory.py's own module docstring already documents for
this exact import shape. `read`/`write` route through fake_bus
(conftest, autouse) transparently since they ultimately call the same
module-level `redis` object fake_bus swaps out, so no separate patch
is needed for the Redis half.
"""
from unittest.mock import MagicMock

import pytest

import eo.skill_library as skill_library


class FakeMatch:
    def __init__(self, score, metadata=None):
        self.score = score
        self.metadata = metadata or {}


class FakeIndex:
    def __init__(self):
        self.upserted = []
        self.query_result = []
        self.raise_on_upsert = False
        self.raise_on_query = False

    def upsert(self, vectors):
        if self.raise_on_upsert:
            raise RuntimeError("simulated upsert failure")
        self.upserted.append(vectors)

    def query(self, vector, top_k, include_metadata, filter):
        if self.raise_on_query:
            raise RuntimeError("simulated query failure")
        return self.query_result


@pytest.fixture(autouse=True)
def _reset_process_seed_flag():
    """_ensure_seed_embedded() only runs its embed/upsert loop once per
    process (module-level _seed_embedded_this_process flag) -- reset it
    before every test so each test's own fake_index actually observes
    the seed-embedding calls instead of the flag from a prior test
    silently skipping them."""
    skill_library._seed_embedded_this_process = False
    yield
    skill_library._seed_embedded_this_process = False


@pytest.fixture
def fake_index(monkeypatch):
    index = FakeIndex()
    monkeypatch.setattr(skill_library, "vector_index", lambda: index)
    monkeypatch.setattr(skill_library, "embed_text", lambda text: [0.1, 0.2, 0.3])
    return index


# ---------------------------------------------------------------------
# _load_skills / list_skills — seed bootstrap
# ---------------------------------------------------------------------

def test_first_call_bootstraps_the_hand_written_seed():
    skills = skill_library.list_skills()
    assert set(skills.keys()) == set(skill_library.SKILL_SEED.keys())
    for entry in skills.values():
        assert entry["updated_at"] is None
        assert entry["times_matched"] == 0


def test_second_call_reads_the_live_store_not_the_seed_again():
    first = skill_library.list_skills()
    # Mutate the live store directly -- a second call must reflect this,
    # proving it's reading through, not re-seeding from SKILL_SEED.
    skill_library.write(skill_library.SKILL_LIBRARY_KEY, {"custom": {"title": "x"}})
    second = skill_library.list_skills()
    assert second == {"custom": {"title": "x"}}
    assert second != first


# ---------------------------------------------------------------------
# write_skill
# ---------------------------------------------------------------------

def test_write_skill_persists_to_redis_and_returns_a_slug_id(fake_index):
    skill_id = skill_library.write_skill("My New Skill!", "Do the thing carefully.")
    assert skill_id == "my_new_skill"
    stored = skill_library.list_skills()[skill_id]
    assert stored["title"] == "My New Skill!"
    assert stored["doc"] == "Do the thing carefully."
    assert stored["source"] == "hand_written"
    assert stored["updated_at"] is not None


def test_write_skill_also_embeds_and_upserts_into_vector(fake_index):
    skill_library.write_skill("My New Skill!", "Do the thing carefully.")
    assert len(fake_index.upserted) == 1
    vectors = fake_index.upserted[0]
    assert vectors[0][0] == f"{skill_library.ID_PREFIX}:my_new_skill"


def test_write_skill_with_same_title_updates_in_place_not_duplicated(fake_index):
    first_id = skill_library.write_skill("Same Title", "v1 doc")
    second_id = skill_library.write_skill("Same Title", "v2 doc")
    assert first_id == second_id
    skills = skill_library.list_skills()
    assert len(skills) == len(skill_library.SKILL_SEED) + 1
    assert skills[first_id]["doc"] == "v2 doc"


def test_write_skill_preserves_existing_times_matched_on_update(fake_index):
    skill_id = skill_library.write_skill("Same Title", "v1 doc")
    skills = skill_library.list_skills()
    skills[skill_id]["times_matched"] = 5
    skill_library.write(skill_library.SKILL_LIBRARY_KEY, skills)

    skill_library.write_skill("Same Title", "v2 doc")
    assert skill_library.list_skills()[skill_id]["times_matched"] == 5


def test_write_skill_still_saves_to_redis_even_if_vector_upsert_fails(fake_index):
    fake_index.raise_on_upsert = True
    skill_id = skill_library.write_skill("Broken Vector", "doc text")  # must not raise
    assert skill_library.list_skills()[skill_id]["doc"] == "doc text"


def test_write_skill_still_saves_to_redis_even_if_embed_fails(monkeypatch, fake_index):
    monkeypatch.setattr(skill_library, "embed_text",
                         MagicMock(side_effect=RuntimeError("HF down")))
    skill_id = skill_library.write_skill("Broken Embed", "doc text")  # must not raise
    assert skill_library.list_skills()[skill_id]["doc"] == "doc text"


# ---------------------------------------------------------------------
# get_relevant_skill
# ---------------------------------------------------------------------

def test_empty_task_text_returns_empty_string_without_querying(fake_index):
    assert skill_library.get_relevant_skill("") == ""
    assert skill_library.get_relevant_skill("   ") == ""
    assert fake_index.upserted == []  # never even reached _ensure_seed_embedded's calls... 


def test_match_above_threshold_returns_the_docs_text(fake_index):
    skill_id = skill_library.write_skill("Existing Skill", "the actual doc text")
    fake_index.query_result = [FakeMatch(0.95, {"skill_id": skill_id})]

    result = skill_library.get_relevant_skill("some task")
    assert result == "the actual doc text"


def test_match_below_threshold_returns_empty_string(fake_index):
    skill_id = skill_library.write_skill("Existing Skill", "the actual doc text")
    fake_index.query_result = [FakeMatch(0.10, {"skill_id": skill_id})]

    assert skill_library.get_relevant_skill("some task") == ""


def test_no_matches_at_all_returns_empty_string(fake_index):
    fake_index.query_result = []
    assert skill_library.get_relevant_skill("some task") == ""


def test_match_whose_skill_id_is_missing_from_redis_is_treated_as_a_miss(fake_index):
    fake_index.query_result = [FakeMatch(0.99, {"skill_id": "does_not_exist_in_redis"})]
    assert skill_library.get_relevant_skill("some task") == ""


def test_query_failure_degrades_to_empty_string_not_raise(fake_index):
    fake_index.raise_on_query = True
    assert skill_library.get_relevant_skill("some task") == ""  # must not raise


def test_embed_failure_degrades_to_empty_string_not_raise(monkeypatch, fake_index):
    monkeypatch.setattr(skill_library, "embed_text",
                         MagicMock(side_effect=RuntimeError("HF down")))
    assert skill_library.get_relevant_skill("some task") == ""  # must not raise


def test_successful_match_bumps_times_matched(fake_index):
    skill_id = skill_library.write_skill("Existing Skill", "doc text")
    fake_index.query_result = [FakeMatch(0.95, {"skill_id": skill_id})]

    skill_library.get_relevant_skill("some task")

    assert skill_library.list_skills()[skill_id]["times_matched"] == 1


# ---------------------------------------------------------------------
# ensure_skill_for_task — self-improvement loop, never raises
# ---------------------------------------------------------------------

def test_ensure_skill_returns_empty_for_blank_task_text(fake_index):
    assert skill_library.ensure_skill_for_task("") == ""


def test_ensure_skill_is_a_noop_when_a_matching_skill_already_exists(monkeypatch, fake_index):
    skill_id = skill_library.write_skill("Existing Skill", "doc text")
    fake_index.query_result = [FakeMatch(0.95, {"skill_id": skill_id})]

    web_researcher_mock = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "agents.web_researcher", web_researcher_mock)

    result = skill_library.ensure_skill_for_task("some task")
    assert result == ""
    web_researcher_mock.run.assert_not_called()


def test_ensure_skill_returns_empty_when_research_finds_no_sources(monkeypatch, fake_index):
    fake_index.query_result = []  # miss -> proceeds to research
    from agents import web_researcher
    monkeypatch.setattr(web_researcher, "run", MagicMock(return_value={"sources": []}))

    assert skill_library.ensure_skill_for_task("a totally novel task") == ""


def test_ensure_skill_returns_empty_when_condensation_says_none(monkeypatch, fake_index):
    fake_index.query_result = []
    from agents import web_researcher
    monkeypatch.setattr(web_researcher, "run", MagicMock(
        return_value={"sources": [{"title": "t", "snippet": "s"}]}))
    monkeypatch.setattr(skill_library, "generate_text", MagicMock(return_value="NONE"))

    assert skill_library.ensure_skill_for_task("a totally novel task") == ""


def test_ensure_skill_writes_a_new_skill_on_a_successful_condensation(monkeypatch, fake_index):
    fake_index.query_result = []
    from agents import web_researcher
    monkeypatch.setattr(web_researcher, "run", MagicMock(
        return_value={"sources": [{"title": "t", "snippet": "s"}]}))
    monkeypatch.setattr(skill_library, "generate_text",
                         MagicMock(return_value="Do the novel task by doing X then Y."))

    skill_id = skill_library.ensure_skill_for_task("a totally novel task")

    assert skill_id != ""
    stored = skill_library.list_skills()[skill_id]
    assert stored["source"] == "self_improvement_loop"
    assert stored["doc"] == "Do the novel task by doing X then Y."


def test_ensure_skill_never_raises_on_an_outright_exception(monkeypatch, fake_index):
    fake_index.query_result = []
    from agents import web_researcher
    monkeypatch.setattr(web_researcher, "run", MagicMock(side_effect=RuntimeError("network down")))

    result = skill_library.ensure_skill_for_task("a totally novel task")  # must not raise
    assert result == ""
