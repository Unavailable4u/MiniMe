"""
minime_cli/render.py

The CLI only ever sees the JSON a TaskResponse serializes to (see
api/routes/tasks.py) -- it has no access to the Python dict
api/task_runner.py's own _extract_answer_text() closes over, and it
would be wrong for a terminal client to import backend internals
directly (this process doesn't share the backend's venv, its Redis
connection, or any of its config -- it only speaks HTTP to it, same as
the frontend). So this is a deliberate, small, client-side mirror of
that function's field-precedence logic, kept in sync by hand:
cache/SGA/tier-0 responses use `answer`, tier-1 uses `code`, tier-2/3
use `output`; a paused run has neither.

If a future patch changes what shape /api/task's `result` can take,
this needs the same update _extract_answer_text() gets -- there is
only one field-precedence rule in the system, it just has two
implementations (one server-side for the persisted transcript, one
here for the terminal).
"""
from __future__ import annotations


def extract_answer_text(response: dict) -> str:
    if response.get("status") == "paused":
        role = (response.get("result") or {}).get("paused_at_role")
        return f"[paused for approval at role '{role}' -- resume from the web UI, or use `minime resume` once A7/A8-era tooling adds it]"
    result = response.get("result") or {}
    if "answer" in result:
        return str(result["answer"])
    if "code" in result:
        return str(result["code"])
    if "output" in result:
        # Tier 3 multi-role output is the full role-keyed results dict,
        # not flat text (see task_runner.py's _write_plan_panels
        # docstring) -- there's no single "the answer" string to print
        # in that shape, so fall through to whatever `message` says
        # rather than dumping a raw dict at the terminal.
        output = result["output"]
        if isinstance(output, str):
            return output
        return str(response.get("message") or "(multi-role run finished -- open it in the web UI to see each role's output)")
    return str(response.get("message") or "(no answer text in response)")


def format_status_line(response: dict) -> str | None:
    """A short, non-fatal heads-up for statuses that aren't a plain
    finished answer -- printed to stderr-equivalent (click.echo(...,
    err=True)) alongside the answer text, never instead of it."""
    status = response.get("status")
    if status in (None, "ok"):
        return None
    if status == "error":
        return f"error: {response.get('message') or 'unknown error'}"
    if status == "needs_app":
        return "This task needs an app_slug (--app) to continue."
    if status == "needs_directed_task_type":
        return "This task needs --task-type to continue."
    if status == "needs_beast_mode_confirmation":
        return "This task needs beast-mode confirmation -- not yet supported from the CLI; use the web UI."
    if status == "needs_beast_mode_choice":
        return "This task needs a beast-mode choice -- not yet supported from the CLI; use the web UI."
    if status == "not_wired_yet":
        return "This task type isn't wired up yet on the backend."
    return f"status: {status}"
