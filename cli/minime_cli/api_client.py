"""
minime_cli/api_client.py

A thin wrapper over the same HTTP endpoints
frontend/app/context/SessionContext.jsx already calls -- POST
/api/chats, POST /api/chats/{id}/messages, POST /api/task, GET
/api/chats. No new backend routes, no new protocol; this is "the
frontend's fetch() calls, in Python, for a terminal instead of a
browser" (Patch A6's own stated goal).

Deliberately excluded from this first cut (out of scope for A6 per the
implementation guide -- Patch A8 is the introspection-commands patch):
/api/task/preview, /api/task/confirm (Part 2 §2.5's hire-review flow),
approval_roles / /api/resume (Part 2 §2.4's pause/approve flow), and
workflow templates. `minime ask` / `minime chat` cover the default
one-click dispatch path (mode="auto", no reviewBeforeDispatch) --
exactly today's default web-UI behavior with nothing turned on.
"""
from __future__ import annotations

import requests

from . import auth
from .config import Config

_TIMEOUT_CHAT = 15          # plain CRUD calls
_TIMEOUT_TASK = 600         # POST /api/task blocks synchronously until the
                             # run finishes (see api/routes/tasks.py's own
                             # docstring on stream_answer() for why) -- a
                             # real tier-3 multi-role run can legitimately
                             # take minutes, so this is generous on purpose.


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg

    def _headers(self, *, json_body: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {auth.get_access_token(self._cfg)}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._cfg.api_url}{path}"

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        try:
            detail = resp.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ApiError(f"{resp.status_code} {resp.reason}: {detail or resp.text[:300]}")

    # --- chats -------------------------------------------------------

    def list_chats(self) -> list:
        resp = requests.get(self._url("/api/chats"), headers=self._headers(), timeout=_TIMEOUT_CHAT)
        self._raise_for_status(resp)
        return resp.json()

    def create_chat(self, title: str = "New Chat") -> dict:
        resp = requests.post(
            self._url("/api/chats"), headers=self._headers(json_body=True),
            json={"title": title}, timeout=_TIMEOUT_CHAT,
        )
        self._raise_for_status(resp)
        return resp.json()

    def get_chat(self, chat_id: str, limit: int | None = None) -> dict:
        params = {"limit": limit} if limit else {}
        resp = requests.get(
            self._url(f"/api/chats/{chat_id}"), headers=self._headers(),
            params=params, timeout=_TIMEOUT_CHAT,
        )
        self._raise_for_status(resp)
        return resp.json()

    def persist_message(self, chat_id: str, message: dict) -> None:
        """Best-effort, same as SessionContext.jsx's persistMessage(): a
        failed save here means the chat transcript is missing a turn,
        not that the task itself failed, so this only ever raises for
        the caller to log -- never to abort an already-finished task."""
        resp = requests.post(
            self._url(f"/api/chats/{chat_id}/messages"), headers=self._headers(json_body=True),
            json={"message": message}, timeout=_TIMEOUT_CHAT,
        )
        self._raise_for_status(resp)

    # --- workspaces ------------------------------------------------------
    # NEW -- Patch A7. Not part of A6's original cut (chats/tasks only);
    # added so `minime attach` can offer a real pick-list instead of
    # asking a person to copy a workspace_id out of the web app's URL
    # bar. Same GET /api/workspaces the frontend's workspace switcher
    # already reads -- no new backend route.

    def list_workspaces(self) -> list:
        resp = requests.get(self._url("/api/workspaces"), headers=self._headers(), timeout=_TIMEOUT_CHAT)
        self._raise_for_status(resp)
        return resp.json()

    # --- skills / mcp ----------------------------------------------------
    # NEW -- Patch A8. Thin wrappers over the same GET /api/skills,
    # /api/skills/{id} routes A5 already added (api/routes/tasks.py) and
    # the GET /api/mcp/servers, /api/mcp/servers/{name}/status routes A8
    # itself adds (api/routes/mcp.py) as the missing HTTP surface over
    # A2's mcp_registry read functions. No new protocol -- same
    # requests-based GET pattern list_chats()/list_workspaces() already
    # use above.

    def list_skills(self) -> list:
        resp = requests.get(self._url("/api/skills"), headers=self._headers(), timeout=_TIMEOUT_CHAT)
        self._raise_for_status(resp)
        return resp.json()

    def get_skill(self, skill_id: str) -> dict:
        resp = requests.get(
            self._url(f"/api/skills/{skill_id}"), headers=self._headers(), timeout=_TIMEOUT_CHAT,
        )
        self._raise_for_status(resp)
        return resp.json()

    def list_mcp_servers(self) -> list:
        resp = requests.get(self._url("/api/mcp/servers"), headers=self._headers(), timeout=_TIMEOUT_CHAT)
        self._raise_for_status(resp)
        return resp.json()

    def mcp_server_status(self, server_name: str) -> dict:
        resp = requests.get(
            self._url(f"/api/mcp/servers/{server_name}/status"), headers=self._headers(),
            timeout=_TIMEOUT_CHAT,
        )
        self._raise_for_status(resp)
        return resp.json()

    # --- tasks ---------------------------------------------------------

    def send_task(self, task_text: str, *, session_id: str | None, mode: str = "auto",
                  app_slug: str | None = None, directed_task_type: str | None = None) -> dict:
        """POST /api/task. Note this call is synchronous and blocking on
        the server side (see api/routes/tasks.py's post_task()) --
        there is no polling loop here because there is nothing to poll;
        the HTTP response IS the finished (or paused/errored) result."""
        resp = requests.post(
            self._url("/api/task"), headers=self._headers(json_body=True),
            json={
                "task_text": task_text,
                "session_id": session_id,
                "mode": mode,
                **({"app_slug": app_slug} if app_slug else {}),
                **({"directed_task_type": directed_task_type} if directed_task_type else {}),
            },
            timeout=_TIMEOUT_TASK,
        )
        self._raise_for_status(resp)
        return resp.json()
