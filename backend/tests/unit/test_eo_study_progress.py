"""
tests/unit/test_eo_study_progress.py — Patch 7e-S5.

eo/study_progress.py had zero test coverage before this. Priorities:

  1. get_progress()'s sparse-storage default: an untouched topic must
     return _default_record()'s "not_started" shape, never a KeyError,
     and the whole-workspace form must return {} for an untouched
     workspace rather than raising.
  2. set_progress()'s merge semantics -- status and notes are
     independent optional fields; passing only one must leave the
     other untouched, not reset it. VALID_STATUSES gating.
  3. The status_changed_at vs. updated_at timestamp distinction:
     status_changed_at only moves when status actually changes value;
     a notes-only call or a same-value status re-set must leave it
     alone while still bumping updated_at.

Isolation follows test_eo_node_summaries.py's convention: a real JSON
file on disk (PROGRESS_PATH), monkeypatched to a tmp_path location.
"""
import eo.study_progress as study_progress


def _use_tmp_path(monkeypatch, tmp_path):
    monkeypatch.setattr(study_progress, "PROGRESS_PATH", str(tmp_path / "_study_progress.json"))


# ---------------------------------------------------------------------
# get_progress — sparse storage / defaults
# ---------------------------------------------------------------------

def test_get_progress_unknown_topic_returns_default_not_started_record(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    record = study_progress.get_progress("ws_1", "topic_never_touched")
    assert record["status"] == study_progress.STATUS_NOT_STARTED
    assert record["notes"] == ""


def test_get_progress_unknown_workspace_returns_empty_map_without_topic_id(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    assert study_progress.get_progress("ws_never_touched") == {}


def test_get_progress_without_topic_id_returns_the_whole_workspace_map(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    study_progress.set_progress("ws_1", "topic_b", status=study_progress.STATUS_DONE)
    result = study_progress.get_progress("ws_1")
    assert set(result.keys()) == {"topic_a", "topic_b"}


def test_get_progress_with_topic_id_returns_saved_record(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    record = study_progress.get_progress("ws_1", "topic_a")
    assert record["status"] == study_progress.STATUS_ONGOING


# ---------------------------------------------------------------------
# set_progress — validation
# ---------------------------------------------------------------------

def test_set_progress_rejects_invalid_status(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    try:
        study_progress.set_progress("ws_1", "topic_a", status="in_orbit")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_progress_none_status_does_not_raise(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    record = study_progress.set_progress("ws_1", "topic_a", notes="just notes")
    assert record["status"] == study_progress.STATUS_NOT_STARTED


# ---------------------------------------------------------------------
# set_progress — merge semantics
# ---------------------------------------------------------------------

def test_set_progress_status_only_leaves_notes_untouched(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", notes="original notes")
    record = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    assert record["notes"] == "original notes"
    assert record["status"] == study_progress.STATUS_ONGOING


def test_set_progress_notes_only_leaves_status_untouched(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_DONE)
    record = study_progress.set_progress("ws_1", "topic_a", notes="a late thought")
    assert record["status"] == study_progress.STATUS_DONE
    assert record["notes"] == "a late thought"


def test_set_progress_first_write_starts_from_default_record(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    record = study_progress.set_progress("ws_1", "topic_a", notes="first note")
    assert record["status"] == study_progress.STATUS_NOT_STARTED
    assert record["notes"] == "first note"


def test_set_progress_persists_across_calls(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    result = study_progress.get_progress("ws_1", "topic_a")
    assert result["status"] == study_progress.STATUS_ONGOING


def test_set_progress_does_not_affect_other_topics_in_same_workspace(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_DONE)
    study_progress.set_progress("ws_1", "topic_b", status=study_progress.STATUS_ONGOING)
    assert study_progress.get_progress("ws_1", "topic_a")["status"] == study_progress.STATUS_DONE


def test_set_progress_does_not_affect_other_workspaces(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_DONE)
    study_progress.set_progress("ws_2", "topic_a", status=study_progress.STATUS_ONGOING)
    assert study_progress.get_progress("ws_1", "topic_a")["status"] == study_progress.STATUS_DONE
    assert study_progress.get_progress("ws_2", "topic_a")["status"] == study_progress.STATUS_ONGOING


# ---------------------------------------------------------------------
# set_progress — timestamp semantics
# ---------------------------------------------------------------------

def test_set_progress_status_change_updates_status_changed_at(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    first = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    first_status_changed = first["timestamps"]["status_changed_at"]

    second = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_DONE)

    assert second["timestamps"]["status_changed_at"] != first_status_changed


def test_set_progress_notes_only_call_does_not_move_status_changed_at(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    first = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    first_status_changed = first["timestamps"]["status_changed_at"]

    second = study_progress.set_progress("ws_1", "topic_a", notes="just a note")

    assert second["timestamps"]["status_changed_at"] == first_status_changed


def test_set_progress_resetting_to_the_same_status_does_not_move_status_changed_at(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    first = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    first_status_changed = first["timestamps"]["status_changed_at"]

    second = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)

    assert second["timestamps"]["status_changed_at"] == first_status_changed


def test_set_progress_every_call_bumps_updated_at(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    first = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    first_updated = first["timestamps"]["updated_at"]

    second = study_progress.set_progress("ws_1", "topic_a", notes="anything")

    assert "updated_at" in second["timestamps"]
    # both calls stamp _now() -- not asserting inequality (could tie at
    # second resolution), just that the field is present and stamped
    # fresh on every write, per the module's own contract.
    assert second["timestamps"]["updated_at"] >= first_updated


def test_default_record_created_at_predates_or_equals_status_changed_at(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    record = study_progress.set_progress("ws_1", "topic_a", status=study_progress.STATUS_ONGOING)
    ts = record["timestamps"]
    assert ts["created_at"] <= ts["status_changed_at"]
