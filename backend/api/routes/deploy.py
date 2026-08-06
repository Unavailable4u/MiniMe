"""
api/routes/deploy.py

B6, piece 8 (the last piece) -- Part 7 §7.4/§7.5's deploy action button
and monitoring hooks. Five routes total:

- POST /api/deploy/{session_id}/propose
- POST /api/deploy/{session_id}/write
- POST /api/deploy/{session_id}/go-live
- POST /api/monitoring/{session_id}/uptimerobot-key
- POST /api/monitoring/{session_id}/uptimerobot-register

Deliberately three separate deploy endpoints for three separate-risk
actions, same split as agents/deploy_config_writer.py /
agents/deploy_agent.py themselves: propose (LLM call, no filesystem
write), write (filesystem write, reversible, no confirmation), go-live
(irreversible, gated behind _confirm_deploy()'s interactive y/N prompt
every time). These call the agent modules directly rather than going
through eo.registry.resolve()/eo/executor.py -- same "import an agent
module, call it straight from a route" convention api/server.py used
for agents.backlink_detector / agents.note_table_builder, appropriate
here since this is a one-off UI-button action, not a Panel-hired
pipeline step (see agents/deploy_agent.py's own docstring).

Sentry needs no endpoint at all -- it's an ordinary module_specs/
submitted_code entry now (agents/prompt_writer.py's
_maybe_add_monitoring_module()), same pool as everything else
code_writers.py generates. UptimeRobot is the one piece that needs
real endpoints, since it needs a user-supplied API key and an explicit
URL (agents/deploy_agent.py's trigger_live_deploy() has no real
deployed URL to read automatically yet -- see that module's
docstring).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from memory.bus import set_app_slug
from eo.errors import MissingDependencyError
from agents import deploy_config_writer as deploy_config_writer_agent
from agents import deploy_agent as deploy_agent_module

router = APIRouter()


class DeployActionRequest(BaseModel):
    project_unique_name: Optional[str] = None


class UptimeRobotKeyRequest(BaseModel):
    api_key: str


class UptimeRobotRegisterRequest(BaseModel):
    url: str
    friendly_name: Optional[str] = None


@router.post("/api/deploy/{session_id}/propose", dependencies=[Depends(require_auth)])
def deploy_propose(session_id: str, req: DeployActionRequest = DeployActionRequest()):
    """Runs deploy_config_writer.py -- proposes a platform + config file
    content, does NOT write anything to disk yet. Safe to call more than
    once; each call overwrites the prior proposal."""
    set_app_slug(session_id)
    return deploy_config_writer_agent.run_deploy_config_writer(session_id=session_id)


@router.post("/api/deploy/{session_id}/write", dependencies=[Depends(require_auth)])
def deploy_write(session_id: str, req: DeployActionRequest = DeployActionRequest()):
    """Writes the proposed config file to disk. Reversible, low-stakes --
    no confirmation gate, matching file_manager.py's own treatment of an
    ordinary file write."""
    set_app_slug(session_id)
    try:
        return deploy_agent_module.write_deploy_config(
            project_unique_name=req.project_unique_name, session_id=session_id
        )
    except MissingDependencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/deploy/{session_id}/go-live", dependencies=[Depends(require_auth)])
def deploy_go_live(session_id: str, req: DeployActionRequest = DeployActionRequest()):
    """The actual "push this live" trigger -- blocks on an interactive
    y/N confirmation (agents/deploy_agent.py's _confirm_deploy()) every
    single call, regardless of target, before returning. See that
    module's docstring for why nothing is silently pushed live past this
    point yet (no real per-host API client exists in this codebase)."""
    set_app_slug(session_id)
    try:
        return deploy_agent_module.trigger_live_deploy(
            project_unique_name=req.project_unique_name, session_id=session_id
        )
    except MissingDependencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/monitoring/{session_id}/uptimerobot-key", dependencies=[Depends(require_auth)])
def set_uptimerobot_key(session_id: str, req: UptimeRobotKeyRequest):
    """Stores the user's UptimeRobot API key against this session's
    workspace (eo/workspace_facts.py's `custom` dict, via
    agents/deploy_agent.py's set_uptimerobot_api_key()). 409 if this
    session isn't part of a workspace -- there's nowhere durable to put
    the key for an ad-hoc chat."""
    try:
        deploy_agent_module.set_uptimerobot_api_key(session_id, req.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "saved"}


@router.post("/api/monitoring/{session_id}/uptimerobot-register", dependencies=[Depends(require_auth)])
def register_uptimerobot(session_id: str, req: UptimeRobotRegisterRequest):
    """Registers req.url as a new UptimeRobot HTTP(s) monitor -- a real
    external call, made immediately with no confirmation gate. Different
    risk class than the live-deploy trigger on purpose: see
    agents/deploy_agent.py's register_uptimerobot_monitor() docstring
    for why (reversible, and the URL is already public by the time this
    runs, unlike a live-deploy trigger which is the act of making
    something public)."""
    set_app_slug(session_id)
    try:
        return deploy_agent_module.register_uptimerobot_monitor(
            req.url, session_id=session_id, friendly_name=req.friendly_name
        )
    except MissingDependencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
