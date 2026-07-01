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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from api.task_runner import run_task

app = FastAPI(title="AI Loop v5 — EO layer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["POST"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    task_text: str
    tier_override: Optional[int] = None
    directed_task_type: Optional[str] = None
    app_slug: Optional[str] = None
    run_tests: bool = False
    session_id: Optional[str] = None


class TaskResponse(BaseModel):
    decision: dict
    tier: int
    session_id: Optional[str] = None
    status: str
    result: Optional[dict] = None
    message: Optional[str] = None


@app.post("/api/task", response_model=TaskResponse)
def post_task(req: TaskRequest):
    try:
        return run_task(
            task_text=req.task_text,
            tier_override=req.tier_override,
            directed_task_type_override=req.directed_task_type,
            app_slug=req.app_slug,
            run_tests=req.run_tests,
            session_id=req.session_id,
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