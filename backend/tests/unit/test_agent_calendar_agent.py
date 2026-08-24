"""
tests/unit/test_agent_calendar_agent.py — Patch 7f-5.

Covers agents/calendar_agent.py: the Google Calendar REAL_ACTION_ROLES
connector (list_events/create_event/delete_event), all built on the
same _auth_headers() gate.

  1. _auth_headers() / IntegrationNotConnectedError — every public
     function raises this (never returns empty data) when
     eo.integrations.refresh_if_needed() has no live token for the user.
  2. list_events() — request params (timeMin/timeMax/maxResults/
     singleEvents/orderBy), the raw-item -> {id, summary, start, end,
     location, html_link} mapping (including the all-day "date"-only
     fallback and the "(no title)" default), and that a non-2xx status
     propagates via raise_for_status().
  3. create_event() — the request payload shape and that the returned
     dict echoes back the start/end the caller passed (not whatever
     Google's response body happens to contain).
  4. delete_event() — 204/410 both count as "deleted" without calling
     raise_for_status(); any other status does call it (and propagates
     whatever it raises).

`requests` is faked at the module level (calendar_agent.requests), same
posture test_agent_academic_search.py/test_agent_pagespeed_agent.py
already take for their own requests-calling modules — no real network
call under test. eo.integrations.refresh_if_needed is faked via the
module object calendar_agent imports (`from eo import integrations`),
same pattern test_eo_integrations.py's own docstring documents for
patch points on a module-import (not bound-name) dependency.
"""
from unittest.mock import MagicMock

import pytest
import requests as real_requests

from agents import calendar_agent


class _FakeResponse:
    def __init__(self, json_data=None, status=200, raise_exc=None):
        self._json_data = json_data
        self.status_code = status
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json_data


@pytest.fixture
def fake_requests(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(calendar_agent, "requests", fake)
    return fake


@pytest.fixture
def connected(monkeypatch):
    """Live-token happy path: refresh_if_needed() returns a usable token."""
    monkeypatch.setattr(calendar_agent.integrations, "refresh_if_needed",
                         lambda user_id, provider: "live-token-123")


@pytest.fixture
def not_connected(monkeypatch):
    monkeypatch.setattr(calendar_agent.integrations, "refresh_if_needed",
                         lambda user_id, provider: None)


# ---------------------------------------------------------------------------
# 1. _auth_headers() / IntegrationNotConnectedError
# ---------------------------------------------------------------------------

class TestAuthHeaders:
    def test_returns_bearer_header_when_connected(self, connected):
        headers = calendar_agent._auth_headers("user-1")
        assert headers == {"Authorization": "Bearer live-token-123"}

    def test_raises_when_not_connected(self, not_connected):
        with pytest.raises(calendar_agent.IntegrationNotConnectedError) as exc_info:
            calendar_agent._auth_headers("user-1")
        assert exc_info.value.user_id == "user-1"
        assert "user-1" in str(exc_info.value)

    @pytest.mark.parametrize("fn,kwargs", [
        (calendar_agent.list_events, {"time_min": "2026-01-01T00:00:00Z", "time_max": "2026-01-02T00:00:00Z"}),
        (calendar_agent.create_event, {"summary": "Meeting", "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T10:00:00Z"}),
        (calendar_agent.delete_event, {"event_id": "evt-1"}),
    ])
    def test_every_public_function_raises_when_not_connected(self, not_connected, fake_requests, fn, kwargs):
        with pytest.raises(calendar_agent.IntegrationNotConnectedError):
            fn("user-1", **kwargs)
        fake_requests.get.assert_not_called()
        fake_requests.post.assert_not_called()
        fake_requests.delete.assert_not_called()


# ---------------------------------------------------------------------------
# 2. list_events()
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("connected")
class TestListEvents:
    def test_request_params_and_default_calendar(self, fake_requests):
        fake_requests.get.return_value = _FakeResponse(json_data={"items": []})

        calendar_agent.list_events("user-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")

        args, kwargs = fake_requests.get.call_args
        assert args[0] == f"{calendar_agent.CALENDAR_API_BASE}/calendars/primary/events"
        assert kwargs["headers"] == {"Authorization": "Bearer live-token-123"}
        assert kwargs["params"] == {
            "timeMin": "2026-01-01T00:00:00Z", "timeMax": "2026-01-02T00:00:00Z",
            "maxResults": 25, "singleEvents": "true", "orderBy": "startTime",
        }
        assert kwargs["timeout"] == calendar_agent.REQUEST_TIMEOUT

    def test_custom_calendar_id_and_max_results(self, fake_requests):
        fake_requests.get.return_value = _FakeResponse(json_data={"items": []})
        calendar_agent.list_events("user-1", "t0", "t1", calendar_id="work", max_results=5)
        args, kwargs = fake_requests.get.call_args
        assert args[0] == f"{calendar_agent.CALENDAR_API_BASE}/calendars/work/events"
        assert kwargs["params"]["maxResults"] == 5

    def test_maps_timed_event_fields(self, fake_requests):
        fake_requests.get.return_value = _FakeResponse(json_data={"items": [
            {
                "id": "evt-1", "summary": "Standup",
                "start": {"dateTime": "2026-01-01T09:00:00Z"},
                "end": {"dateTime": "2026-01-01T09:15:00Z"},
                "location": "Room 4", "htmlLink": "https://cal/evt-1",
            },
        ]})
        result = calendar_agent.list_events("user-1", "t0", "t1")
        assert result == {
            "events": [{
                "id": "evt-1", "summary": "Standup",
                "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T09:15:00Z",
                "location": "Room 4", "html_link": "https://cal/evt-1",
            }],
            "count": 1,
        }

    def test_all_day_event_falls_back_to_date_field(self, fake_requests):
        fake_requests.get.return_value = _FakeResponse(json_data={"items": [
            {"id": "evt-2", "summary": "Holiday",
             "start": {"date": "2026-07-04"}, "end": {"date": "2026-07-05"}},
        ]})
        result = calendar_agent.list_events("user-1", "t0", "t1")
        assert result["events"][0]["start"] == "2026-07-04"
        assert result["events"][0]["end"] == "2026-07-05"

    def test_missing_summary_and_location_default(self, fake_requests):
        fake_requests.get.return_value = _FakeResponse(json_data={"items": [
            {"id": "evt-3", "start": {"date": "2026-07-04"}, "end": {"date": "2026-07-05"}},
        ]})
        result = calendar_agent.list_events("user-1", "t0", "t1")
        assert result["events"][0]["summary"] == "(no title)"
        assert result["events"][0]["location"] is None

    def test_count_matches_number_of_events(self, fake_requests):
        fake_requests.get.return_value = _FakeResponse(json_data={"items": [
            {"id": "a", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-01"}},
            {"id": "b", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-02"}},
        ]})
        result = calendar_agent.list_events("user-1", "t0", "t1")
        assert result["count"] == 2

    def test_bad_status_propagates_via_raise_for_status(self, fake_requests):
        error = real_requests.exceptions.HTTPError("500 Server Error")
        fake_requests.get.return_value = _FakeResponse(status=500, raise_exc=error)
        with pytest.raises(real_requests.exceptions.HTTPError):
            calendar_agent.list_events("user-1", "t0", "t1")


# ---------------------------------------------------------------------------
# 3. create_event()
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("connected")
class TestCreateEvent:
    def test_request_payload_shape(self, fake_requests):
        fake_requests.post.return_value = _FakeResponse(json_data={
            "id": "evt-9", "summary": "Kickoff", "htmlLink": "https://cal/evt-9",
        })
        calendar_agent.create_event(
            "user-1", summary="Kickoff", start="2026-01-01T09:00:00Z", end="2026-01-01T10:00:00Z",
            description="Project kickoff", location="HQ",
        )
        args, kwargs = fake_requests.post.call_args
        assert args[0] == f"{calendar_agent.CALENDAR_API_BASE}/calendars/primary/events"
        assert kwargs["json"] == {
            "summary": "Kickoff", "description": "Project kickoff", "location": "HQ",
            "start": {"dateTime": "2026-01-01T09:00:00Z"},
            "end": {"dateTime": "2026-01-01T10:00:00Z"},
        }

    def test_description_and_location_default_to_empty_string(self, fake_requests):
        fake_requests.post.return_value = _FakeResponse(json_data={"id": "evt-9"})
        calendar_agent.create_event("user-1", summary="Kickoff", start="t0", end="t1")
        _, kwargs = fake_requests.post.call_args
        assert kwargs["json"]["description"] == ""
        assert kwargs["json"]["location"] == ""

    def test_result_echoes_caller_supplied_start_and_end(self, fake_requests):
        # Google's response body is deliberately missing start/end here --
        # the result must still carry back exactly what the caller passed,
        # not anything read from the response.
        fake_requests.post.return_value = _FakeResponse(json_data={
            "id": "evt-9", "summary": "Kickoff", "htmlLink": "https://cal/evt-9",
        })
        result = calendar_agent.create_event(
            "user-1", summary="Kickoff", start="2026-01-01T09:00:00Z", end="2026-01-01T10:00:00Z",
        )
        assert result == {
            "id": "evt-9", "summary": "Kickoff", "html_link": "https://cal/evt-9",
            "start": "2026-01-01T09:00:00Z", "end": "2026-01-01T10:00:00Z",
        }

    def test_bad_status_propagates(self, fake_requests):
        error = real_requests.exceptions.HTTPError("400 Bad Request")
        fake_requests.post.return_value = _FakeResponse(status=400, raise_exc=error)
        with pytest.raises(real_requests.exceptions.HTTPError):
            calendar_agent.create_event("user-1", summary="x", start="t0", end="t1")


# ---------------------------------------------------------------------------
# 4. delete_event()
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("connected")
class TestDeleteEvent:
    def test_204_counts_as_deleted_without_calling_raise_for_status(self, fake_requests):
        resp = _FakeResponse(status=204, raise_exc=AssertionError("should not be called"))
        fake_requests.delete.return_value = resp
        result = calendar_agent.delete_event("user-1", "evt-1")
        assert result == {"id": "evt-1", "deleted": True}

    def test_410_counts_as_deleted_without_calling_raise_for_status(self, fake_requests):
        resp = _FakeResponse(status=410, raise_exc=AssertionError("should not be called"))
        fake_requests.delete.return_value = resp
        result = calendar_agent.delete_event("user-1", "evt-1")
        assert result == {"id": "evt-1", "deleted": True}

    def test_other_status_calls_raise_for_status_and_propagates(self, fake_requests):
        error = real_requests.exceptions.HTTPError("403 Forbidden")
        fake_requests.delete.return_value = _FakeResponse(status=403, raise_exc=error)
        with pytest.raises(real_requests.exceptions.HTTPError):
            calendar_agent.delete_event("user-1", "evt-1")

    def test_custom_calendar_id_used_in_url(self, fake_requests):
        fake_requests.delete.return_value = _FakeResponse(status=204)
        calendar_agent.delete_event("user-1", "evt-1", calendar_id="work")
        args, kwargs = fake_requests.delete.call_args
        assert args[0] == f"{calendar_agent.CALENDAR_API_BASE}/calendars/work/events/evt-1"
