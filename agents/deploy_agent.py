"""
agents/deploy_agent.py — Part 7 §7.4, step 2 of the propose/apply split.
Executes the plan agents/deploy_config_writer.py proposed. No LLM call
here, same reasoning as agents/file_manager.py: the only code allowed to
touch the real filesystem (or, for the live-deploy step below, an
external host) stays deterministic and auditable.

Deliberately NOT a REAL_ACTION_ROLES / STRUCTURE_TEMPLATES entry, and not
dispatched through eo/executor.py at all -- unlike deploy_config_writer.py
(which the Panel can hire like any other role, see eo/registry.py), this
module is only ever invoked from the "Deploy" button described in Part 7
§7.6, via a dedicated API endpoint. It has no Role Library presence and
needs no role icon, same as file_manager.py itself.

Two genuinely different-risk actions, kept as two separate functions on
purpose (Part 7 §7.4):
  - write_deploy_config(): writes the proposed config file into the
    project. Reversible, low-stakes -- same risk class as any other
    file_manager.py write, so no confirmation gate.
  - trigger_live_deploy(): the actual "push this live" action. Uses real
    (if free) hosting quota and can make the project publicly reachable
    -- genuinely not reversible the way a file write is -- so this is
    gated behind an explicit y/N confirmation every time, regardless of
    whether the target is MiniMe's own generated apps/ or a registered
    external project.

    This deliberately does NOT call file_manager.py's own
    _confirm_destructive() directly: that function always returns True
    when project_unique_name is None (MiniMe's own apps/ writes are never
    gated) -- correct for an ordinary file write, but wrong here, since
    §7.4 is explicit that the live-deploy step must be confirmed
    regardless of target. _confirm_deploy() below reuses the exact same
    interactive y/N mechanism, not the exact same bypass semantics.

    No real per-host CLI/API client exists yet in this codebase (no
    Render/Fly/GitHub Pages/Vercel HTTP integration has been built) --
    trigger_live_deploy() is honest about that rather than silently
    pretending to push something live. It performs and gates the real,
    meaningful part (irreversible confirmation before anything would go
    out) and returns a clearly-labeled "not yet wired to a real host"
    result past that gate, the same honesty discipline
    feasibility_estimator's seed brief already uses for its own
    heuristic-not-a-real-estimate labeling.

Part 7 §7.5 addition -- UptimeRobot registration. Genuinely different
from everything above: it's the first real *external* HTTP call this
module (or arguably this whole Part 7 gap-list) makes, as opposed to an
LLM call or a local file write. Two things worth being explicit about,
since neither is silently assumed:
  - trigger_live_deploy() above never produces a real URL (no host
    client exists yet -- see its own docstring). So
    register_uptimerobot_monitor() takes an explicit `url` argument
    (manual entry) rather than reading one off the last deploy result.
    Once a real per-host client exists, wiring trigger_live_deploy()'s
    real output into this call is the natural next step, not done here.
  - The API key is stored via eo/workspace_facts.py's `custom` dict
    (update_custom_fact()), the exact precedent that module's own
    docstring gives for "an agent stashing a fact it cares about." That
    store is plain, unencrypted memory-bus storage -- same discipline as
    every other bus key in this system, no dedicated secrets handling
    exists anywhere here. Flagging this rather than treating an API key
    as equivalent to a brand-voice string by default.

Place this file at: agents/deploy_agent.py
"""

import os
import sys
import json
import requests  # NEW -- Part 7 §7.5, same lib + timeout/error-handling
                 # convention utils/llm_client.py's Cloudflare path uses

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, get_current_app_slug
from eo.project_registry import resolve_project_root
from eo.errors import MissingDependencyError
from relay.emitter import emit_event
# Reuse, don't reimplement -- same root-confinement/path-safety functions
# file_manager.py already enforces for every other write in this system.
# A deploy config file is still a file write, same risk class as any
# other file_manager.py operation.
from agents.file_manager import _safe_relpath, _confine_to_root, APPS_ROOT

from agents.deploy_config_writer import DEPLOY_CONFIG_PLAN_KEY

# NEW -- Part 7 §7.5: session_id -> workspace_id resolution, identical
# call shape to eo/conversation_memory.py's own _workspace_facts_text().
from eo import chat_workspace
from eo import workspace_facts

LAST_DEPLOY_CONFIG_SUMMARY_KEY = "last_deploy_config_summary"
LAST_UPTIMEROBOT_REGISTRATION_KEY = "last_uptimerobot_registration"
UPTIMEROBOT_API_KEY_FACT = "uptimerobot_api_key"
UPTIMEROBOT_NEW_MONITOR_URL = "https://api.uptimerobot.com/v2/newMonitor"


def _confirm_deploy(action_desc: str) -> bool:
    """Same interactive y/N pattern as file_manager.py's
    _confirm_destructive() (and loop_v4.py's own tier-3 cost-ceiling
    confirmation before it) -- but deliberately its own function, not a
    call to _confirm_destructive() itself. See module docstring: that
    function's project_unique_name-is-None bypass is specifically wrong
    for a live deploy, which §7.4 requires to be confirmed every time."""
    confirm = input(
        f"[Deploy Agent] About to {action_desc}. This uses real (if free) "
        f"hosting quota and may make the project publicly reachable. "
        f"Proceed? [y/N]: "
    ).strip().lower()
    return confirm == "y"


def write_deploy_config(project_unique_name: str = None, session_id: str = None) -> dict:
    """Writes deploy_config_writer.py's proposed config file to disk.
    Reversible, low-stakes -- no confirmation gate, same as any other
    ordinary file_manager.py write."""
    plan = read(DEPLOY_CONFIG_PLAN_KEY)
    if not plan:
        raise MissingDependencyError(
            "deploy_config_writer",
            "No deploy_config_plan found in memory. Run deploy_config_writer.py first.",
        )

    if project_unique_name:
        app_slug = project_unique_name
        app_dir = _confine_to_root(resolve_project_root(project_unique_name), project_unique_name)
    else:
        app_slug = get_current_app_slug()
        if not app_slug:
            raise ValueError("No app_slug in memory -- write_deploy_config() must run "
                              "after a build cycle has scoped one.")
        app_dir = _confine_to_root(os.path.join(APPS_ROOT, app_slug), project_unique_name)

    config_filename = plan.get("config_filename")
    config_content = plan.get("config_content", "")
    if not config_filename:
        raise ValueError("deploy_config_plan has no config_filename to write.")

    full = _safe_relpath(app_dir, config_filename)
    os.makedirs(os.path.dirname(full) or app_dir, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(config_content)

    summary = {
        "app_dir": app_dir,
        "app_slug": app_slug,
        "platform": plan.get("platform"),
        "config_filename": config_filename,
        "written": [config_filename],
    }
    write(LAST_DEPLOY_CONFIG_SUMMARY_KEY, summary)
    emit_event("deploy_config_written", session_id, agent="deploy_agent", payload=summary)
    return summary


def trigger_live_deploy(project_unique_name: str = None, session_id: str = None) -> dict:
    """The actual "push this live" step. Gated behind _confirm_deploy()
    every time -- see module docstring for why this can't reuse
    file_manager.py's _confirm_destructive() bypass semantics.

    Returns a clearly-labeled stub result if confirmed (no real per-host
    API client exists yet -- see module docstring), or a declined result
    if the user says no. Never silently proceeds without asking."""
    summary = read(LAST_DEPLOY_CONFIG_SUMMARY_KEY, default=None)
    if not summary:
        raise MissingDependencyError(
            "deploy_agent",
            "No deploy config has been written to disk yet. Call "
            "write_deploy_config() first.",
        )

    platform = summary.get("platform", "the proposed host")
    app_slug = summary.get("app_slug")

    if not _confirm_deploy(f"deploy '{app_slug}' live to {platform}"):
        result = {"status": "declined", "platform": platform, "app_slug": app_slug}
        emit_event("deploy_declined", session_id, agent="deploy_agent", payload=result)
        return result

    # Honest stub past the confirmation gate -- see module docstring.
    # The meaningful, safety-relevant part (the confirmation itself) is
    # real; this codebase has no live Render/Fly/GitHub Pages/Vercel
    # client to actually call yet.
    result = {
        "status": "confirmed_not_yet_integrated",
        "platform": platform,
        "app_slug": app_slug,
        "message": (
            f"Confirmed -- but no real {platform} API/CLI client is wired "
            f"up in this system yet, so nothing was actually pushed live. "
            f"The config file itself is already on disk from "
            f"write_deploy_config() and ready for a manual deploy."
        ),
    }
    write("last_deploy_trigger_result", result)
    emit_event("deploy_confirmed", session_id, agent="deploy_agent", payload=result)
    return result


def _resolve_workspace_id(session_id: str) -> str:
    """Identical shape to eo/conversation_memory.py's own
    _workspace_facts_text() helper -- session_id and chat_id are the
    same string everywhere in this system, so a session's workspace is
    "whichever workspace this chat_id is a member of." Returns None
    (not "") for a session with no workspace, since callers here need to
    tell "no workspace" apart from "workspace has an empty custom dict"
    rather than treating both as an empty-string no-op the way a prompt
    prepend would."""
    if not session_id:
        return None
    ws = chat_workspace.workspace_for_chat(session_id)
    return ws["id"] if ws else None


def set_uptimerobot_api_key(session_id: str, api_key: str) -> dict:
    """Part 7 §7.5. Stores the user's UptimeRobot free-tier API key via
    eo/workspace_facts.py's update_custom_fact() -- the exact precedent
    that module's own docstring gives. Plain, unencrypted storage, same
    as every other workspace fact; see module docstring for why this
    isn't silently treated as more protected than a brand-voice string
    by default.

    Raises if this session has no workspace -- there is nowhere durable
    to put a per-workspace fact for an ad-hoc chat that isn't a member
    of one, and silently no-op'ing here (unlike a context-prepend
    read) would make the user think their key was saved when it wasn't."""
    workspace_id = _resolve_workspace_id(session_id)
    if not workspace_id:
        raise ValueError(
            "This session isn't part of a workspace, so there's nowhere "
            "durable to store an UptimeRobot API key. Add this chat to "
            "a workspace first."
        )
    return workspace_facts.update_custom_fact(workspace_id, UPTIMEROBOT_API_KEY_FACT, api_key)


def get_uptimerobot_api_key(session_id: str) -> str:
    workspace_id = _resolve_workspace_id(session_id)
    if not workspace_id:
        return None
    facts = workspace_facts.get_facts(workspace_id)
    return (facts.get("custom") or {}).get(UPTIMEROBOT_API_KEY_FACT)


def register_uptimerobot_monitor(url: str, session_id: str = None,
                                  friendly_name: str = None) -> dict:
    """Part 7 §7.5. Registers `url` as a new HTTP(s) monitor on
    UptimeRobot's free-tier API. Deliberately takes `url` as an explicit
    argument rather than reading one off trigger_live_deploy()'s result
    -- see module docstring for why (no real host client exists yet, so
    there is no real deployed URL to read automatically today).

    Uses the same requests + timeout + narrow exception handling
    convention as utils/llm_client.py's Cloudflare HTTP path (the only
    other place in this codebase making a raw external HTTP call), for
    consistency rather than inventing a second style.

    Unlike trigger_live_deploy(), this is a REAL external call with real
    (if free-tier) effects the moment it succeeds -- it does not need
    _confirm_deploy()'s y/N gate the way a live deploy does, because
    registering a monitor is reversible (delete it on UptimeRobot) and
    not itself an act of making anything publicly reachable; the URL is
    already public by the time this runs."""
    if not url:
        raise ValueError("register_uptimerobot_monitor() requires a url.")

    api_key = get_uptimerobot_api_key(session_id)
    if not api_key:
        raise MissingDependencyError(
            "deploy_agent",
            "No UptimeRobot API key stored for this workspace. Call "
            "set_uptimerobot_api_key() first.",
        )

    app_slug = get_current_app_slug()
    payload = {
        "api_key": api_key,
        "format": "json",
        "type": 1,  # HTTP(s) monitor
        "url": url,
        "friendly_name": friendly_name or app_slug or url,
    }

    try:
        response = requests.post(
            UPTIMEROBOT_NEW_MONITOR_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        result = {"status": "error", "url": url, "message": f"UptimeRobot request failed: {exc}"}
        emit_event("uptimerobot_registration_failed", session_id, agent="deploy_agent", payload=result)
        return result
    except requests.exceptions.HTTPError as exc:
        result = {"status": "error", "url": url, "message": f"UptimeRobot HTTP error: {exc}"}
        emit_event("uptimerobot_registration_failed", session_id, agent="deploy_agent", payload=result)
        return result

    # UptimeRobot's v2 API returns HTTP 200 even on a logical failure
    # (bad key, duplicate URL, etc.) -- the real result is in "stat".
    if data.get("stat") != "ok":
        error_msg = (data.get("error") or {}).get("message", "unknown error")
        result = {"status": "error", "url": url, "message": f"UptimeRobot rejected the request: {error_msg}"}
        emit_event("uptimerobot_registration_failed", session_id, agent="deploy_agent", payload=result)
        return result

    monitor = data.get("monitor", {})
    result = {
        "status": "registered",
        "url": url,
        "monitor_id": monitor.get("id"),
        "friendly_name": payload["friendly_name"],
    }
    write(LAST_UPTIMEROBOT_REGISTRATION_KEY, result)
    emit_event("uptimerobot_registered", session_id, agent="deploy_agent", payload=result)
    return result


if __name__ == "__main__":
    print(json.dumps(write_deploy_config(), indent=2))