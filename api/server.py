"""
api/server.py — Stage 6, step 2 (Part 10).

The thin HTTP layer in front of api/task_runner.py. This is intentionally
the smallest possible FastAPI app: one endpoint, one job — take a task,
run it through the EO layer synchronously, return the result as JSON.

No streaming, no Pusher, no live panels here — that's Stage 6 step 1
(relay) and steps 3-6 (live UI), layered on top of this later. Step 2's
job is just proving task-in/result-out works end to end.

Run locally:
    pip install fastapi uvicorn
    uvicorn api.server:app --reload --port 8000

CORS is open to the Next.js dev server origin (localhost:3000) only —
tighten this before deploying anywhere real.
"""
import os
import sys
import traceback


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, Depends
from eo.project_registry import list_projects, generate_control_unit, register_project
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Union

from api.task_runner import run_task
from eo.quota_sentinel import get_quota_snapshot
from eo.project_registry import list_projects

app = FastAPI(title="MiniMe v6 — EO layer API")

API_AUTH_MODE = os.getenv("API_AUTH_MODE", "api_key")
API_AUTH_SECRET = os.getenv("API_AUTH_SECRET")


def require_auth(request: Request):
    if not API_AUTH_SECRET:
        return  # auth disabled if no secret configured — same
                # fail-open-to-local-dev behavior the CORS default has
    provided = request.headers.get("x-api-key")
    if provided != API_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

class RegisterProjectRequest(BaseModel):
    path: str
    display_name: str


@app.post("/api/projects", dependencies=[Depends(require_auth)])
def register_project_endpoint(req: RegisterProjectRequest):
    unit = generate_control_unit(req.display_name)
    register_project(unit["unique_name"], req.path)
    return {"unique_name": unit["unique_name"], "root_path": req.path}

class TaskRequest(BaseModel):
    task_text: str
    tier_override: Optional[int] = None
    directed_task_type: Optional[str] = None
    app_slug: Optional[str] = None
    run_tests: bool = False
    session_id: Optional[str] = None
    mode: Optional[str] = "auto"
    project_unique_name: Optional[str] = None   # NEW


class TaskResponse(BaseModel):
    # tier is int for tiers 0-3, or the literal string "sga" when the
    # Starter General Agents resolved the task before classification
    # (Part 2) — loosened from `int` to fix a latent validation bug that
    # would have 500'd on every real SGA-resolved HTTP request.
    decision: dict
    tier: Union[int, str]
    session_id: Optional[str] = None
    # status values: "ok" | "error" | "needs_app" | "needs_directed_task_type"
    # | "not_wired_yet" | "needs_beast_mode_confirmation" (Part 3)
    # | "needs_beast_mode_choice" (Part 3)
    status: str
    result: Optional[dict] = None
    message: Optional[str] = None


@app.post("/api/task", response_model=TaskResponse, dependencies=[Depends(require_auth)])
def post_task(req: TaskRequest):
    try:
        return run_task(
            task_text=req.task_text,
            tier_override=req.tier_override,
            directed_task_type_override=req.directed_task_type,
            app_slug=req.app_slug,
            run_tests=req.run_tests,
            session_id=req.session_id,
            mode=req.mode,
            project_unique_name=req.project_unique_name,   # NEW
        )
    except Exception as exc:
        # Step 2 has no relay yet, so a stack trace on the server console
        # is the only debugging signal you get — keep it, but also return
        # a clean JSON error instead of a raw 500 with no body.
        traceback.print_exc()
        return TaskResponse(
            decision={},
            tier=-1,
            status="error",
            result=None,
            message=f"{exc.__class__.__name__}: {exc}",
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/quota", dependencies=[Depends(require_auth)])
def quota():
    return get_quota_snapshot()


@app.get("/api/projects", dependencies=[Depends(require_auth)])
def projects():
    return list_projects()