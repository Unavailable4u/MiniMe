"""
tests/unit/test_agent_deploy_agent.py — Patch 7f-5.

Covers agents/deploy_agent.py's four public actions:
  - write_deploy_config(): reads deploy_config_writer's plan, resolves
    app_dir (explicit project_unique_name -> resolve_project_root, else
    the current app_slug under APPS_ROOT), writes the config file to
    disk through file_manager.py's own _safe_relpath()/_confine_to_root()
    safety functions, and writes a summary to the bus.
  - trigger_live_deploy(): reads the last write_deploy_config() summary,
    gates on _confirm_deploy()'s y/N prompt every time, returns a
    declined result on "n" and a clearly-labeled
    "confirmed_not_yet_integrated" stub on "y" (see the module's own
    docstring for why -- no real per-host API client exists yet).
  - set_uptimerobot_api_key() / get_uptimerobot_api_key(): store/read a
    per-workspace UptimeRobot key via eo/workspace_facts.py.
  - register_uptimerobot_monitor(): a real external POST to UptimeRobot's
    v2 API, including the "HTTP 200 but stat != ok" logical-failure case
    that API is documented to return.

Bug fix included in this patch (found while writing these tests):
_resolve_workspace_id() called chat_workspace.workspace_for_chat(session_id)
with only one argument, but that function was migrated to require an
owner_id (eo/conversation_memory.py's _workspace_facts_text() already
documents and was updated for this same migration; this call site was
missed). Confirmed via a plain call: it raised
`TypeError: workspace_for_chat() missing 1 required positional argument:
'owner_id'` every time session_id was truthy -- meaning
set_uptimerobot_api_key()/get_uptimerobot_api_key()/
register_uptimerobot_monitor() crashed with an unhandled TypeError
instead of their intended "no workspace" ValueError/None outcomes,
whenever a caller passed a session_id (which api/routes/deploy.py always
does). Fixed by threading an optional owner_id parameter through
_resolve_workspace_id() / set_uptimerobot_api_key() /
get_uptimerobot_api_key() / register_uptimerobot_monitor(), failing
quiet (returning None) when it's absent -- same convention
eo/conversation_memory.py's _workspace_facts_text() already uses for the
identical migration. Full remediation (the API layer actually capturing
and threading the authenticated owner_id through, the way
api/routes/tasks.py's routes already do) is a separate, later change,
out of scope for this module -- flagging it here rather than silently
leaving it half-fixed.

file_manager.py's _confine_to_root()/_safe_relpath()/APPS_ROOT are the
real functions (not mocked) -- same "isolate the agent under test, not
its already-tested safety net" approach test_file_manager.py itself
takes, just imported one level up.
"""

import pytest
import requests

from agents import deploy_agent, file_manager
from eo.errors import MissingDependencyError
from memory.bus import read, set_app_slug, write


def _set_apps_root(monkeypatch, path):
    """deploy_agent.py does `from agents.file_manager import ... APPS_ROOT`,
    a bound-name copy -- deploy_agent.write_deploy_config() reads its own
    copy directly (os.path.join(APPS_ROOT, app_slug)), but
    _confine_to_root()'s no-project_unique_name branch reads
    file_manager's own module-level APPS_ROOT global, not the copy.
    Both must be patched for a tmp_path-scoped write to land inside the
    confined root instead of tripping _confine_to_root()'s real
    PermissionError against the repo's actual apps/ directory."""
    monkeypatch.setattr(deploy_agent, "APPS_ROOT", str(path))
    monkeypatch.setattr(file_manager, "APPS_ROOT", str(path))


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


@pytest.fixture(autouse=True)
def _fake_emit_event(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy_agent, "emit_event",
        lambda event, session_id, agent=None, payload=None: calls.append(
            (event, session_id, agent, payload)
        ),
    )
    return calls


VALID_PLAN = {
    "platform": "render", "config_filename": "render.yaml",
    "config_content": "services:\n  - type: web\n",
}


# ---------------------------------------------------------------------------
# 1. write_deploy_config()
# ---------------------------------------------------------------------------
class TestWriteDeployConfig:
    def test_no_plan_raises_missing_dependency(self, fake_bus):
        with pytest.raises(MissingDependencyError) as exc_info:
            deploy_agent.write_deploy_config()
        assert exc_info.value.required_role == "deploy_config_writer"

    def test_plan_without_config_filename_raises_value_error(self, fake_bus, tmp_path, monkeypatch):
        _set_apps_root(monkeypatch, tmp_path)
        set_app_slug("myapp")
        write(deploy_agent.DEPLOY_CONFIG_PLAN_KEY, {"platform": "render", "config_content": "x"})

        with pytest.raises(ValueError, match="config_filename"):
            deploy_agent.write_deploy_config()

    def test_no_app_slug_and_no_project_unique_name_raises(self, fake_bus, tmp_path, monkeypatch):
        _set_apps_root(monkeypatch, tmp_path)
        write(deploy_agent.DEPLOY_CONFIG_PLAN_KEY, VALID_PLAN)

        with pytest.raises(ValueError, match="No app_slug"):
            deploy_agent.write_deploy_config()

    def test_writes_config_file_to_disk_under_current_app_slug(self, fake_bus, tmp_path, monkeypatch):
        _set_apps_root(monkeypatch, tmp_path)
        set_app_slug("myapp")
        write(deploy_agent.DEPLOY_CONFIG_PLAN_KEY, VALID_PLAN)

        summary = deploy_agent.write_deploy_config()

        written_path = tmp_path / "myapp" / "render.yaml"
        assert written_path.read_text() == "services:\n  - type: web\n"
        assert summary["config_filename"] == "render.yaml"
        assert summary["platform"] == "render"
        assert summary["written"] == ["render.yaml"]

    def test_result_written_to_bus_and_event_emitted(self, fake_bus, tmp_path, monkeypatch, _fake_emit_event):
        _set_apps_root(monkeypatch, tmp_path)
        set_app_slug("myapp")
        write(deploy_agent.DEPLOY_CONFIG_PLAN_KEY, VALID_PLAN)

        summary = deploy_agent.write_deploy_config(session_id="sess-1")

        assert read(deploy_agent.LAST_DEPLOY_CONFIG_SUMMARY_KEY) == summary
        event, session_id, agent, payload = _fake_emit_event[0]
        assert event == deploy_agent.EventType.DEPLOY_CONFIG_WRITTEN
        assert session_id == "sess-1"
        assert agent == "deploy_agent"
        assert payload == summary

    def test_nested_config_filename_creates_intermediate_dirs(self, fake_bus, tmp_path, monkeypatch):
        _set_apps_root(monkeypatch, tmp_path)
        set_app_slug("myapp")
        plan = dict(VALID_PLAN, config_filename=".github/workflows/deploy.yml")
        write(deploy_agent.DEPLOY_CONFIG_PLAN_KEY, plan)

        deploy_agent.write_deploy_config()

        assert (tmp_path / "myapp" / ".github" / "workflows" / "deploy.yml").exists()

    def test_path_escaping_config_filename_is_rejected(self, fake_bus, tmp_path, monkeypatch):
        _set_apps_root(monkeypatch, tmp_path)
        set_app_slug("myapp")
        plan = dict(VALID_PLAN, config_filename="../../etc/passwd")
        write(deploy_agent.DEPLOY_CONFIG_PLAN_KEY, plan)

        with pytest.raises(ValueError, match="unsafe path"):
            deploy_agent.write_deploy_config()


# ---------------------------------------------------------------------------
# 2. trigger_live_deploy()
# ---------------------------------------------------------------------------
class TestTriggerLiveDeploy:
    def test_no_prior_summary_raises_missing_dependency(self, fake_bus):
        with pytest.raises(MissingDependencyError) as exc_info:
            deploy_agent.trigger_live_deploy()
        assert exc_info.value.required_role == "deploy_agent"

    def test_declined_confirmation_returns_declined_status(self, fake_bus, monkeypatch, _fake_emit_event):
        write(deploy_agent.LAST_DEPLOY_CONFIG_SUMMARY_KEY,
              {"platform": "render", "app_slug": "myapp"})
        monkeypatch.setattr(deploy_agent, "_confirm_deploy", lambda desc: False)

        result = deploy_agent.trigger_live_deploy()

        assert result["status"] == "declined"
        assert result["platform"] == "render"
        event, _, _, payload = _fake_emit_event[0]
        assert event == deploy_agent.EventType.DEPLOY_DECLINED
        assert payload == result

    def test_confirmed_deploy_returns_labeled_stub_and_writes_result(
        self, fake_bus, monkeypatch, _fake_emit_event
    ):
        write(deploy_agent.LAST_DEPLOY_CONFIG_SUMMARY_KEY,
              {"platform": "fly", "app_slug": "myapp"})
        monkeypatch.setattr(deploy_agent, "_confirm_deploy", lambda desc: True)

        result = deploy_agent.trigger_live_deploy()

        assert result["status"] == "confirmed_not_yet_integrated"
        assert result["platform"] == "fly"
        assert "no real fly" in result["message"] or "fly" in result["message"]
        assert read("last_deploy_trigger_result") == result
        event, _, _, payload = _fake_emit_event[0]
        assert event == deploy_agent.EventType.DEPLOY_CONFIRMED
        assert payload == result

    def test_confirm_deploy_prompt_mentions_platform_and_app_slug(self, fake_bus, monkeypatch):
        write(deploy_agent.LAST_DEPLOY_CONFIG_SUMMARY_KEY,
              {"platform": "vercel", "app_slug": "coolapp"})
        seen = {}

        def _fake_confirm(desc):
            seen["desc"] = desc
            return False

        monkeypatch.setattr(deploy_agent, "_confirm_deploy", _fake_confirm)
        deploy_agent.trigger_live_deploy()

        assert "coolapp" in seen["desc"]
        assert "vercel" in seen["desc"]


class TestConfirmDeploy:
    def test_accepts_lowercase_y(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert deploy_agent._confirm_deploy("do a thing") is True

    def test_accepts_uppercase_y_and_strips_whitespace(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "  Y  ")
        assert deploy_agent._confirm_deploy("do a thing") is True

    def test_anything_else_is_declined(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert deploy_agent._confirm_deploy("do a thing") is False

    def test_empty_input_is_declined(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert deploy_agent._confirm_deploy("do a thing") is False


# ---------------------------------------------------------------------------
# 3. _resolve_workspace_id(): the owner_id-scoping bug fix
# ---------------------------------------------------------------------------
class TestResolveWorkspaceId:
    def test_no_session_id_returns_none_without_calling_workspace_for_chat(self, monkeypatch):
        called = []
        monkeypatch.setattr(deploy_agent.chat_workspace, "workspace_for_chat",
                             lambda *a, **k: called.append(1))
        assert deploy_agent._resolve_workspace_id(None, owner_id="owner-1") is None
        assert called == []

    def test_session_id_without_owner_id_fails_quiet_not_typeerror(self, monkeypatch):
        """Regression guard for the bug this patch fixes: previously this
        called workspace_for_chat(session_id) with no owner_id and crashed
        with TypeError. It must now return None instead."""
        called = []
        monkeypatch.setattr(deploy_agent.chat_workspace, "workspace_for_chat",
                             lambda *a, **k: called.append(1))
        result = deploy_agent._resolve_workspace_id("sess-1")
        assert result is None
        assert called == []  # never even reaches workspace_for_chat without an owner_id

    def test_session_id_and_owner_id_resolves_workspace(self, monkeypatch):
        captured = {}

        def _fake(chat_id, owner_id):
            captured["args"] = (chat_id, owner_id)
            return {"id": "ws-42"}

        monkeypatch.setattr(deploy_agent.chat_workspace, "workspace_for_chat", _fake)
        result = deploy_agent._resolve_workspace_id("sess-1", owner_id="owner-1")

        assert result == "ws-42"
        assert captured["args"] == ("sess-1", "owner-1")

    def test_no_workspace_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(deploy_agent.chat_workspace, "workspace_for_chat", lambda *a, **k: None)
        assert deploy_agent._resolve_workspace_id("sess-1", owner_id="owner-1") is None


# ---------------------------------------------------------------------------
# 4. set_uptimerobot_api_key() / get_uptimerobot_api_key()
# ---------------------------------------------------------------------------
class TestUptimeRobotApiKeyStorage:
    def test_set_with_no_workspace_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(deploy_agent, "_resolve_workspace_id", lambda *a, **k: None)
        with pytest.raises(ValueError, match="isn't part of a workspace"):
            deploy_agent.set_uptimerobot_api_key("sess-1", "abc123")

    def test_set_with_no_owner_id_also_raises_value_error_not_typeerror(self, fake_bus):
        """End-to-end version of the bug-fix regression guard above: a
        real call with no owner_id (today's actual caller shape) must
        raise the intended user-facing ValueError, not TypeError."""
        with pytest.raises(ValueError, match="isn't part of a workspace"):
            deploy_agent.set_uptimerobot_api_key("sess-1", "abc123")

    def test_set_with_workspace_calls_update_custom_fact(self, monkeypatch):
        monkeypatch.setattr(deploy_agent, "_resolve_workspace_id", lambda *a, **k: "ws-1")
        captured = {}
        monkeypatch.setattr(
            deploy_agent.workspace_facts, "update_custom_fact",
            lambda ws_id, key, value: captured.update(ws_id=ws_id, key=key, value=value),
        )

        deploy_agent.set_uptimerobot_api_key("sess-1", "abc123", owner_id="owner-1")

        assert captured == {
            "ws_id": "ws-1", "key": deploy_agent.UPTIMEROBOT_API_KEY_FACT, "value": "abc123",
        }

    def test_get_with_no_workspace_returns_none(self, monkeypatch):
        monkeypatch.setattr(deploy_agent, "_resolve_workspace_id", lambda *a, **k: None)
        assert deploy_agent.get_uptimerobot_api_key("sess-1") is None

    def test_get_with_workspace_reads_from_custom_facts(self, monkeypatch):
        monkeypatch.setattr(deploy_agent, "_resolve_workspace_id", lambda *a, **k: "ws-1")
        monkeypatch.setattr(
            deploy_agent.workspace_facts, "get_facts",
            lambda ws_id: {"custom": {deploy_agent.UPTIMEROBOT_API_KEY_FACT: "stored-key"}},
        )
        assert deploy_agent.get_uptimerobot_api_key("sess-1", owner_id="owner-1") == "stored-key"


# ---------------------------------------------------------------------------
# 5. register_uptimerobot_monitor()
# ---------------------------------------------------------------------------
class TestRegisterUptimeRobotMonitor:
    def test_no_url_raises_value_error(self):
        with pytest.raises(ValueError, match="requires a url"):
            deploy_agent.register_uptimerobot_monitor("")

    def test_no_api_key_raises_missing_dependency(self, monkeypatch):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: None)
        with pytest.raises(MissingDependencyError) as exc_info:
            deploy_agent.register_uptimerobot_monitor("https://example.com")
        assert exc_info.value.required_role == "deploy_agent"

    def test_successful_registration_returns_monitor_id(self, fake_bus, monkeypatch, _fake_emit_event):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: "ur-key")
        monkeypatch.setattr(
            deploy_agent.requests, "post",
            lambda *a, **k: _FakeResponse({"stat": "ok", "monitor": {"id": 999}}),
        )

        result = deploy_agent.register_uptimerobot_monitor("https://example.com", friendly_name="My App")

        assert result["status"] == "registered"
        assert result["monitor_id"] == 999
        assert result["friendly_name"] == "My App"
        assert read(deploy_agent.LAST_UPTIMEROBOT_REGISTRATION_KEY) == result
        event, _, _, payload = _fake_emit_event[0]
        assert event == deploy_agent.EventType.UPTIMEROBOT_REGISTERED

    def test_friendly_name_defaults_to_app_slug_then_url(self, fake_bus, monkeypatch):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: "ur-key")
        set_app_slug("myapp")
        captured = {}

        def _fake_post(url, data=None, headers=None, timeout=None):
            captured["data"] = data
            return _FakeResponse({"stat": "ok", "monitor": {"id": 1}})

        monkeypatch.setattr(deploy_agent.requests, "post", _fake_post)
        deploy_agent.register_uptimerobot_monitor("https://example.com")

        assert captured["data"]["friendly_name"] == "myapp"

    def test_stat_not_ok_returns_error_result(self, fake_bus, monkeypatch, _fake_emit_event):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: "ur-key")
        monkeypatch.setattr(
            deploy_agent.requests, "post",
            lambda *a, **k: _FakeResponse({"stat": "fail", "error": {"message": "invalid key"}}),
        )

        result = deploy_agent.register_uptimerobot_monitor("https://example.com")

        assert result["status"] == "error"
        assert "invalid key" in result["message"]
        event, _, _, payload = _fake_emit_event[0]
        assert event == deploy_agent.EventType.UPTIMEROBOT_REGISTRATION_FAILED

    def test_timeout_returns_error_result(self, fake_bus, monkeypatch, _fake_emit_event):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: "ur-key")

        def _raise(*a, **k):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr(deploy_agent.requests, "post", _raise)
        result = deploy_agent.register_uptimerobot_monitor("https://example.com")

        assert result["status"] == "error"
        assert "timed out" in result["message"]
        event, _, _, _ = _fake_emit_event[0]
        assert event == deploy_agent.EventType.UPTIMEROBOT_REGISTRATION_FAILED

    def test_connection_error_returns_error_result(self, fake_bus, monkeypatch):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: "ur-key")

        def _raise(*a, **k):
            raise requests.exceptions.ConnectionError("no route")

        monkeypatch.setattr(deploy_agent.requests, "post", _raise)
        result = deploy_agent.register_uptimerobot_monitor("https://example.com")

        assert result["status"] == "error"

    def test_http_error_returns_error_result(self, fake_bus, monkeypatch):
        monkeypatch.setattr(deploy_agent, "get_uptimerobot_api_key", lambda *a, **k: "ur-key")
        monkeypatch.setattr(
            deploy_agent.requests, "post",
            lambda *a, **k: _FakeResponse(raise_exc=requests.exceptions.HTTPError("500")),
        )

        result = deploy_agent.register_uptimerobot_monitor("https://example.com")

        assert result["status"] == "error"
        assert "HTTP error" in result["message"]
