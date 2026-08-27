"""
cli/tests/test_skills_cmds.py -- Patch A8.

Exercises `minime skills list` / `minime skills show <id>` through
Click's own CliRunner, with ApiClient monkeypatched at the
skills_cmds module boundary -- same "fake the client, test the
command's own flow" posture test_attach_cmds.py already documents for
`minime attach`.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from minime_cli.api_client import ApiError
from minime_cli.commands import skills_cmds
from minime_cli.config import ConfigError


class _FakeClient:
    def __init__(self, skills=None, detail=None, list_error=None, detail_error=None):
        self._skills = skills or []
        self._detail = detail
        self._list_error = list_error
        self._detail_error = detail_error

    def list_skills(self):
        if self._list_error:
            raise self._list_error
        return self._skills

    def get_skill(self, skill_id):
        if self._detail_error:
            raise self._detail_error
        return self._detail


@pytest.fixture
def runner():
    return CliRunner()


def test_list_prints_id_and_title_per_skill(monkeypatch, runner):
    fake = _FakeClient(skills=[
        {"skill_id": "diagram-basics", "title": "Diagram Basics"},
        {"skill_id": "api-design", "title": "API Design"},
    ])
    monkeypatch.setattr(skills_cmds, "_client", lambda: fake)

    result = runner.invoke(skills_cmds.list_skills_cmd)
    assert result.exit_code == 0
    assert "diagram-basics\tDiagram Basics" in result.output
    assert "api-design\tAPI Design" in result.output


def test_list_with_no_skills_prints_a_friendly_message(monkeypatch, runner):
    monkeypatch.setattr(skills_cmds, "_client", lambda: _FakeClient(skills=[]))
    result = runner.invoke(skills_cmds.list_skills_cmd)
    assert result.exit_code == 0
    assert "No skills" in result.output


def test_list_turns_an_api_error_into_a_clean_click_exception(monkeypatch, runner):
    monkeypatch.setattr(
        skills_cmds, "_client", lambda: _FakeClient(list_error=ApiError("401 Unauthorized: bad token")),
    )
    result = runner.invoke(skills_cmds.list_skills_cmd)
    assert result.exit_code != 0
    assert "401" in result.output


def test_list_turns_a_config_error_into_a_clean_click_exception(monkeypatch, runner):
    def _raise_config_error():
        raise ConfigError("Missing: MINIME_SUPABASE_URL")
    monkeypatch.setattr(skills_cmds, "_client", _raise_config_error)
    result = runner.invoke(skills_cmds.list_skills_cmd)
    assert result.exit_code != 0
    assert "MINIME_SUPABASE_URL" in result.output


def test_show_prints_the_full_record(monkeypatch, runner):
    fake = _FakeClient(detail={
        "skill_id": "diagram-basics",
        "title": "Diagram Basics",
        "source": "seed",
        "updated_at": "2026-01-01T00:00:00Z",
        "times_matched": 3,
        "doc": "How to draw a good diagram.",
    })
    monkeypatch.setattr(skills_cmds, "_client", lambda: fake)

    result = runner.invoke(skills_cmds.show_skill_cmd, ["diagram-basics"])
    assert result.exit_code == 0
    assert "id:            diagram-basics" in result.output
    assert "title:         Diagram Basics" in result.output
    assert "How to draw a good diagram." in result.output


def test_show_for_an_unknown_id_turns_the_404_into_a_clean_click_exception(monkeypatch, runner):
    monkeypatch.setattr(
        skills_cmds, "_client",
        lambda: _FakeClient(detail_error=ApiError("404 Not Found: No skill found with id 'nope'")),
    )
    result = runner.invoke(skills_cmds.show_skill_cmd, ["nope"])
    assert result.exit_code != 0
    assert "404" in result.output
