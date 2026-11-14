"""
m2c/tests/tools/test_tools_shell_network.py -- Patch C11b: how the shell tool uses the network proxy (``ShellConfig.network``).

No bubblewrap is needed.  As in test_tools_shell_sandbox.py, ``m2c.sandbox.get_status`` and ``wrap_command`` are fakes
(the wrapper records what it was given and runs the command through a plain ``sh``), but the proxy is REAL: it is started
by the shell tool exactly as in production, on its own thread.  So these tests prove the wiring:

* ``network="off"`` is exactly the C11a behavior: no proxy, no mount, nothing new passed to the wrapper;
* ``network="prompt"`` (the default) starts the proxy lazily, once per session, and gives the wrapper a read-only mount of
  its private directory plus the python that runs the bridge;
* the proxy variables are injected AFTER sanitizing: the host's own proxy settings (any letter case) are replaced,
  ``NO_PROXY`` is empty, and a user's ``extra_strip_names`` cannot remove ours;
* anything that goes wrong (no python3, the proxy cannot start) is "no network" with a fixed log code, never a refusal
  of the command and never a wider network; a command that is not sandboxed gets no proxy at all;
* settings reach the policy (pre-allowed domains, ports, prompt, record, cleartext), and changed settings rebuild the proxy.

Run: cd m2c && pytest tests/tools/test_tools_shell_network.py -v
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from m2c import sandbox
from m2c.sandbox import bridge, netproxy
from m2c.sandbox import bwrap as sandbox_bwrap
from m2c.sandbox.netproxy import NetworkError
from m2c.sandbox.profile import ProfileError
from m2c.tools import ToolContext, ToolError
from m2c.tools.shell import (
    AUTH_USER_APPROVED,
    NETWORK_MODES,
    ShellConfig,
    configure_shell,
    run_command,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the shell tool targets Linux and WSL2")


@pytest.fixture(autouse=True)
def _no_host_proxy_settings(monkeypatch):
    """Whatever proxy the machine running the tests uses must not leak into what these tests compare."""
    for name in list(os.environ):
        if name.upper() in bridge.PROXY_VARIABLES or name.upper() == "FTP_PROXY":
            monkeypatch.delenv(name, raising=False)

FAKE_PYTHON = "/usr/bin/python3"


class Rig:
    """A fake "working sandbox" whose wrapper records every call (including the network mount and the environment)."""

    def __init__(self, monkeypatch, tmp_path, *, available=True, python=FAKE_PYTHON):
        self.wrap_calls: list[dict] = []
        self.labels: list[str] = []
        self.dirs_seen: list[str | None] = []
        self.context: ToolContext | None = None
        self.wrap_error: ProfileError | None = None
        result = sandbox.SandboxStatus(available, "ok" if available else "bwrap_missing",
                                       "the sandbox works" if available else "bubblewrap (bwrap) is not installed", None)

        def get_status(**kw):
            return result

        def wrap(command, **kw):
            self.wrap_calls.append({"command": command, **kw})
            network = kw.get("network")
            if network is not None and self.context is not None:
                self.dirs_seen.append(network.directory)
                service = netproxy._SERVICES.get(self.context)
                self.labels.append(service._label if service is not None else "<no service>")
            if self.wrap_error is not None:
                raise self.wrap_error
            return ["/bin/sh", "-c", command]

        monkeypatch.setattr(sandbox, "get_status", get_status)
        monkeypatch.setattr(sandbox, "wrap_command", wrap)
        monkeypatch.setattr(sandbox_bwrap, "find_sandbox_python", lambda root="/": python)

    @property
    def network_calls(self):
        return [c for c in self.wrap_calls if "network" in c]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path)


@pytest.fixture
def make(proj, rig):
    made: list[ToolContext] = []

    def _make(**cfg):
        context = ToolContext(proj)
        rig.context = context
        configure_shell(context, ShellConfig(kill_grace_s=0.3, **cfg))
        made.append(context)
        return context

    yield _make
    for context in made:
        netproxy.close_network(context)


def run(context, command="echo hi", timeout_s=20, **kw):
    return run_command(context, command, timeout_s, authorization=AUTH_USER_APPROVED, **kw)


# ==========================================================================
# the setting
# ==========================================================================
def test_the_default_is_prompt_and_the_modes_are_prompt_and_off():
    cfg = ShellConfig()
    assert cfg.network == "prompt"
    assert NETWORK_MODES == ("prompt", "off")
    assert cfg.network_allow == () and cfg.network_ports == (443,) and cfg.network_prompt is None
    assert cfg.network_record is None and cfg.network_allow_cleartext is False


@pytest.mark.parametrize("bad", ["", "PROMPT", "on", "allow", "open", "all", None, True, 1, ["prompt"]])
def test_an_unknown_network_mode_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        ShellConfig(network=bad)


@pytest.mark.parametrize("kwargs", [
    {"network_allow": "pypi.org"}, {"network_allow": ("",)}, {"network_allow": (5,)}, {"network_allow": ("a\x00b",)},
    {"network_ports": ()}, {"network_ports": (0,)}, {"network_ports": (70000,)}, {"network_ports": (True,)},
    {"network_ports": ("443",)}, {"network_ports": 443}, {"network_ports": "443"},
    {"network_prompt": "yes"}, {"network_record": 5}, {"network_allow_cleartext": "yes"}, {"network_allow_cleartext": 1},
])
def test_bad_network_settings_are_refused_at_construction(kwargs):
    with pytest.raises(ValueError):
        ShellConfig(**kwargs)


def test_lists_are_copied_to_tuples():
    cfg = ShellConfig(network_allow=["a.example"], network_ports=[443, 8443])
    assert cfg.network_allow == ("a.example",) and cfg.network_ports == (443, 8443)


# ==========================================================================
# off is C11a
# ==========================================================================
def test_off_starts_no_proxy_and_passes_the_wrapper_nothing_new(rig, make):
    context = make(network="off")
    out = run(context, "echo hi")
    assert out.ok and "hi" in out.output
    assert netproxy._SERVICES.get(context) is None
    (call,) = rig.wrap_calls
    assert "network" not in call                                           # exactly the C11a call
    assert not any(name.upper() in bridge.PROXY_VARIABLES for name in call["env"])


def test_off_leaves_the_environment_as_c11a_made_it(rig, make, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://corp.example:3128")
    context = make(network="off")
    run(context)
    env = rig.wrap_calls[0]["env"]
    assert env.get("HTTP_PROXY") != bridge.proxy_url()


# ==========================================================================
# prompt: lazy, once per session, a mount for the wrapper
# ==========================================================================
def test_nothing_starts_until_the_first_sandboxed_command(rig, make):
    context = make()
    assert netproxy._SERVICES.get(context) is None


def test_the_first_command_starts_the_proxy_and_hands_the_wrapper_a_private_mount(rig, make):
    context = make()
    out = run(context, "echo hi")
    assert out.ok
    (call,) = rig.network_calls
    mount = call["network"]
    assert mount.python == FAKE_PYTHON
    mode = stat.S_IMODE(os.stat(mount.directory).st_mode)
    assert mode == 0o700 and os.stat(mount.directory).st_uid == os.geteuid()
    assert (Path(mount.directory) / bridge.SOCKET_NAME).exists() and (Path(mount.directory) / bridge.SCRIPT_NAME).exists()


def test_the_same_proxy_serves_the_whole_session(rig, make):
    context = make()
    for _ in range(3):
        run(context)
    assert len(set(rig.dirs_seen)) == 1 and len(rig.dirs_seen) == 3


def test_two_sessions_have_two_proxies(rig, make):
    a, b = make(), make()
    run(a)
    run(b)
    assert len(set(rig.dirs_seen)) == 2


def test_the_label_names_the_running_command_and_is_cleared_afterwards(rig, make):
    context = make()
    run(context, "pip install requests\necho second line")
    assert rig.labels == ["pip install requests"]
    assert netproxy._SERVICES[context]._label == ""                       # a prompt after the command names no command


def test_the_label_is_cleared_even_when_the_command_fails_to_wrap(rig, make):
    context = make()
    rig.wrap_error = ProfileError("no_shell")
    with pytest.raises(ToolError):
        run(context, "echo hi")
    assert netproxy._SERVICES[context]._label == ""


def test_the_label_never_carries_a_secret(rig, make):
    fake = "ghp_" + "x" * 36                                              # built at run time, never a literal
    context = make()
    run(context, f"echo {fake}")
    assert fake not in rig.labels[0]


def test_a_dead_proxy_is_replaced_by_the_next_command(rig, make):
    context = make()
    run(context)
    first = netproxy._SERVICES[context]
    first.close()
    run(context)
    second = netproxy._SERVICES[context]
    assert second is not first and second.alive
    assert not os.path.exists(rig.dirs_seen[0]) and os.path.exists(rig.dirs_seen[1])


def test_close_network_stops_the_proxy_and_removes_its_directory(rig, make):
    context = make()
    run(context)
    directory = rig.dirs_seen[0]
    netproxy.close_network(context)
    assert not os.path.exists(directory) and netproxy._SERVICES.get(context) is None
    netproxy.close_network(context)                                      # twice is harmless


# ==========================================================================
# the environment
# ==========================================================================
def test_the_proxy_variables_are_injected_after_sanitizing(rig, make, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    context = make()
    run(context)
    env = rig.network_calls[0]["env"]
    url = bridge.proxy_url()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[name] == url
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""
    assert "PATH" in env


@pytest.mark.parametrize("name", ["HTTP_PROXY", "http_proxy", "Https_Proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"])
def test_the_hosts_own_proxy_settings_never_reach_the_command(rig, make, monkeypatch, name):
    monkeypatch.setenv(name, "http://corp.example:3128" if "no" not in name.lower() else "*")
    context = make()
    run(context)
    env = rig.network_calls[0]["env"]
    assert "corp.example" not in " ".join(env.values())
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""
    assert {k for k in env if k.upper() in bridge.PROXY_VARIABLES} == set(bridge.proxy_environment())


def test_our_variables_survive_a_users_strip_list(rig, make, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://corp.example:3128")
    context = make(extra_strip_names=("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "PROXY*", "*_PROXY"))
    run(context)
    env = rig.network_calls[0]["env"]
    assert env["HTTP_PROXY"] == bridge.proxy_url() and env["https_proxy"] == bridge.proxy_url()
    assert env["NO_PROXY"] == ""


def test_credentials_are_still_stripped_with_the_proxy_on(rig, make, monkeypatch):
    fake = "x" * 40
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", fake)
    monkeypatch.setenv("GITHUB_TOKEN", fake)
    context = make()
    run(context)
    env = rig.network_calls[0]["env"]
    assert fake not in env.values()


# ==========================================================================
# no sandbox, no proxy; trouble means no network
# ==========================================================================
def test_without_a_working_sandbox_no_proxy_is_started(monkeypatch, tmp_path, proj):
    rig = Rig(monkeypatch, tmp_path, available=False)
    context = ToolContext(proj)
    configure_shell(context, ShellConfig(sandbox="auto", warn=lambda text: None))
    out = run(context)
    assert out.ok
    assert netproxy._SERVICES.get(context) is None
    assert rig.network_calls == [] and rig.wrap_calls == []


def test_sandbox_off_starts_no_proxy(rig, make):
    context = make(sandbox="off")
    run(context)
    assert netproxy._SERVICES.get(context) is None and rig.wrap_calls == []


def test_required_without_a_sandbox_still_refuses_before_any_proxy(monkeypatch, tmp_path, proj):
    Rig(monkeypatch, tmp_path, available=False)
    context = ToolContext(proj)
    configure_shell(context, ShellConfig(sandbox="required"))
    with pytest.raises(ToolError):
        run(context)
    assert netproxy._SERVICES.get(context) is None


def test_no_system_python_means_no_network_but_the_command_still_runs_sandboxed(monkeypatch, tmp_path, proj, caplog):
    rig = Rig(monkeypatch, tmp_path, python=None)
    context = ToolContext(proj)
    configure_shell(context, ShellConfig(kill_grace_s=0.3))
    with caplog.at_level(logging.INFO):
        out = run(context)
    assert out.ok and "hi" in out.output
    (call,) = rig.wrap_calls
    assert "network" not in call
    assert not any(name.upper() in bridge.PROXY_VARIABLES for name in call["env"])
    assert netproxy._SERVICES.get(context) is None
    assert "no_python" in caplog.text


def test_a_proxy_that_cannot_start_means_no_network_and_a_fixed_log_code(rig, make, monkeypatch, caplog):
    def broken(owner, key, build):
        raise NetworkError("start_failed")

    monkeypatch.setattr(netproxy, "service_for", broken)
    context = make()
    with caplog.at_level(logging.INFO):
        out = run(context)
    assert out.ok
    (call,) = rig.wrap_calls
    assert "network" not in call and not any(name.upper() in bridge.PROXY_VARIABLES for name in call["env"])
    assert "start_failed" in caplog.text


def test_an_unexpected_error_in_the_proxy_code_is_also_just_no_network(rig, make, monkeypatch, caplog):
    def broken(owner, key, build):
        raise RuntimeError("/home/someone/secret-path blew up")

    monkeypatch.setattr(netproxy, "service_for", broken)
    context = make()
    with caplog.at_level(logging.INFO):
        out = run(context)
    assert out.ok and "network" not in rig.wrap_calls[0]
    assert "secret-path" not in caplog.text and "proxy_unavailable" in caplog.text


def test_a_profile_that_cannot_be_built_is_still_a_refusal_with_the_proxy_on(rig, make):
    context = make()
    rig.wrap_error = ProfileError("git_not_directory")
    with pytest.raises(ToolError) as info:
        run(context)
    assert info.value.code == "refused"


def test_nothing_about_the_network_is_logged_with_hosts_paths_or_commands(rig, make, caplog):
    context = make()
    secret = "cmd-" + os.urandom(6).hex()
    with caplog.at_level(logging.DEBUG):
        run(context, f"echo {secret}")
    assert secret not in caplog.text
    for directory in rig.dirs_seen:
        assert directory not in caplog.text


# ==========================================================================
# the settings reach the policy
# ==========================================================================
def test_pre_allowed_domains_ports_and_cleartext_reach_the_proxy(rig, make):
    context = make(network_allow=("pre.example", "*.cdn.example"), network_ports=(443, 8443), network_allow_cleartext=True)
    run(context)
    service = netproxy._SERVICES[context]
    assert service.policy.check("pre.example", 443).action == "allow"
    assert service.policy.check("a.cdn.example", 8443).action == "allow"
    assert service.policy.check("pre.example", 80).action == "deny"
    assert service.policy.check("other.example", 443).action == "ask"
    assert service._server.config.allow_cleartext is True


def test_the_default_policy_is_443_only_no_cleartext_and_asks_about_everything(rig, make):
    context = make()
    run(context)
    service = netproxy._SERVICES[context]
    assert service.policy.ports == frozenset({443}) and service._server.config.allow_cleartext is False
    assert service.policy.check("anything.example", 443).action == "ask"


def test_the_prompt_and_the_recorder_are_installed(rig, make):
    async def prompt(host, port, label):
        return "deny"

    records: list[dict] = []
    context = make(network_prompt=prompt, network_record=records.append)
    run(context)
    service = netproxy._SERVICES[context]
    assert service.policy.has_prompt
    service.policy.note("x.example", "deny", "address")
    assert records == [{"host": "x.example", "decision": "deny", "scope": "address"}]


def test_without_a_prompt_an_unlisted_domain_is_denied_and_flagged_unprompted(rig, make):
    context = make()
    run(context)
    service = netproxy._SERVICES[context]
    assert not service.policy.has_prompt
    verdict = service.run(service.policy.decide("unlisted.example", 443), 5)
    assert verdict.action == "deny" and verdict.scope == "no_prompt"
    assert service.unprompted_denials == 1


def test_the_production_service_has_the_users_saved_list_and_never_a_project_file(rig, make, tmp_path):
    from m2c.sandbox.netpolicy import AllowFile

    AllowFile().add("saved.example")
    context = make()
    run(context)
    assert netproxy._SERVICES[context].policy.check("saved.example", 443).action == "allow"


def test_changed_settings_rebuild_the_proxy_and_remove_the_old_directory(rig, make):
    context = make(network_allow=("a.example",))
    run(context)
    first_dir = rig.dirs_seen[0]
    configure_shell(context, ShellConfig(kill_grace_s=0.3, network_allow=("b.example",)))
    run(context)
    second_dir = rig.dirs_seen[1]
    assert first_dir != second_dir and not os.path.exists(first_dir)
    policy = netproxy._SERVICES[context].policy
    assert policy.check("b.example", 443).action == "allow" and policy.check("a.example", 443).action == "ask"


def test_switching_to_off_mid_session_stops_using_the_proxy(rig, make):
    context = make()
    run(context)
    configure_shell(context, ShellConfig(kill_grace_s=0.3, network="off"))
    run(context)
    assert "network" in rig.wrap_calls[0] and "network" not in rig.wrap_calls[1]


def test_a_garbage_config_object_is_still_an_internal_error_not_a_network(rig, make):
    context = make()
    context.shell_config = object()
    with pytest.raises(ToolError) as info:
        run(context)
    assert info.value.code == "internal_error"
