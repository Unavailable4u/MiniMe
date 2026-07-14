"""
agents/calendar_agent.py — Part 8.5: Google Calendar connector.

REAL_ACTION_ROLES tool agent, same shape as agents/academic_search.py:
zero LLM calls, plain HTTP requests to one external API, structured data
in and structured data out. The one thing genuinely new versus
academic_search.py: this API call is made ON BEHALF OF a specific user,
using a token from eo/integrations.py rather than a free, no-key public
endpoint — every function below takes user_id first, for exactly that
reason.

This is the template for Gmail / Slack / Jira-Asana-Linear: each of those
connectors is this same shape (resolve a live token via
eo.integrations.refresh_if_needed, make one HTTP call, return structured
data) against a different base URL and payload shape.

Not connected: if refresh_if_needed() returns None, every function here
raises IntegrationNotConnectedError rather than silently returning empty
data — a task that needs the user's calendar and can't reach it should
surface that clearly, not look like "you have zero events today."
"""
import os
import sys
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo import integrations

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
REQUEST_TIMEOUT = 15
PROVIDER = "google_calendar"


class IntegrationNotConnectedError(Exception):
    """Raised when the requesting user has no live Google Calendar
    credential (never connected, or a refresh failed and needs
    reconnecting). api/server.py maps this to a 409, same convention as
    eo/errors.py's MissingDependencyError elsewhere in this codebase —
    a real, anticipated "needs a prerequisite step first" condition, not
    an unexpected failure."""
    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"No live Google Calendar connection for user {user_id!r}")


def _auth_headers(user_id: str) -> dict:
    token = integrations.refresh_if_needed(user_id, PROVIDER)
    if not token:
        raise IntegrationNotConnectedError(user_id)
    return {"Authorization": f"Bearer {token}"}


def list_events(user_id: str, time_min: str, time_max: str, calendar_id: str = "primary",
                 max_results: int = 25) -> dict:
    """time_min/time_max: RFC3339 timestamps (e.g. '2026-07-14T00:00:00Z').

    Result shape:
    {"events": [{"id", "summary", "start", "end", "location", "html_link"}], "count": <int>}
    """
    resp = requests.get(
        f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events",
        headers=_auth_headers(user_id),
        params={
            "timeMin": time_min, "timeMax": time_max,
            "maxResults": max_results, "singleEvents": "true", "orderBy": "startTime",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    events = [
        {
            "id": it.get("id"),
            "summary": it.get("summary", "(no title)"),
            "start": (it.get("start") or {}).get("dateTime") or (it.get("start") or {}).get("date"),
            "end": (it.get("end") or {}).get("dateTime") or (it.get("end") or {}).get("date"),
            "location": it.get("location"),
            "html_link": it.get("htmlLink"),
        }
        for it in items
    ]
    return {"events": events, "count": len(events)}


def create_event(user_id: str, summary: str, start: str, end: str,
                  description: str = "", location: str = "", calendar_id: str = "primary") -> dict:
    """start/end: RFC3339 timestamps. Returns the created event's id and
    html_link — same "structured data out" convention as
    academic_search.py's node_id-bearing results, so a caller (or a
    downstream agent) has something concrete to reference afterward."""
    resp = requests.post(
        f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events",
        headers=_auth_headers(user_id),
        json={
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    created = resp.json()
    return {
        "id": created.get("id"),
        "summary": created.get("summary"),
        "html_link": created.get("htmlLink"),
        "start": start,
        "end": end,
    }


def delete_event(user_id: str, event_id: str, calendar_id: str = "primary") -> dict:
    resp = requests.delete(
        f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}",
        headers=_auth_headers(user_id),
        timeout=REQUEST_TIMEOUT,
    )
    # Google returns 204 on success, 410 if it was already deleted --
    # both count as "gone," which is what the caller actually wants to know.
    if resp.status_code not in (204, 410):
        resp.raise_for_status()
    return {"id": event_id, "deleted": True}