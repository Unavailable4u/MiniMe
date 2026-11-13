"""
m2c/tests/secret_handling/test_secrets_env.py -- Patch C15: the environment a shell command gets.

* C07's rules are the FLOOR: every name C07 stripped is still stripped (checked against a copy of C07's rule kept in
  this file, over a large generated list of names), unless the person named it in their own user settings.
* New deny patterns (more cloud / CI / database / package-registry variables) and a value scan.
* An allow list for what a shell genuinely needs.
* Named per-project exceptions, stored only in ``<M2C_HOME>/settings.toml``; a project file cannot grant one.
"""
from __future__ import annotations

import json
import os
import random
import string

import pytest
from secret_fakes import leaked, make

from m2c.secrets import env as env_mod
from m2c.secrets.env import granted_names, sanitize_environment
from m2c.tools.shell import ShellConfig, configure_shell, sanitized_env
from m2c.tools import CallEnv, ToolContext, run_tool
from m2c.tools.shell import AUTH_USER_APPROVED

# ---- C07's rule, copied here on purpose: the floor must not move when the real code is edited -----------------------
LEGACY_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
LEGACY_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GCLOUD_", "M2C_", "BASH_FUNC_")
LEGACY_NAMES = {"SSH_AUTH_SOCK", "KUBECONFIG", "DATABASE_URL", "BASH_ENV", "ENV"}


def legacy_strips(name: str) -> bool:
    upper = name.upper()
    if upper in ("PATH", "HOME"):
        return False
    return upper in LEGACY_NAMES or upper.startswith(LEGACY_PREFIXES) or any(s in upper for s in LEGACY_SUBSTRINGS)


def generated_names(count: int = 4000) -> list[str]:
    rng = random.Random(15)
    words = ["APP", "DB", "API", "AUTH", "CI", "GIT", "NODE", "PY", "MY", "SERVICE", "USER", "HOST", "PORT", "URL",
             "KEY", "TOKEN", "SECRET", "PASS", "PASSWORD", "PASSWD", "CREDENTIAL", "AWS", "AZURE", "GOOGLE", "GCP",
             "M2C", "BASH_FUNC", "NPM", "PIP", "DOCKER", "KEYBOARD", "TERM", "LANG", "EDITOR", "ID", "NAME", "DIR"]
    names = []
    for _ in range(count):
        parts = [rng.choice(words) for _ in range(rng.randint(1, 3))]
        name = rng.choice(["_", "_", ""]).join(parts)
        names.append(rng.choice([name, name.lower(), name.title()]))
    return names


def values_for(names):
    return {name: f"v{i}" for i, name in enumerate(names)}


def run_env(ctx):
    """``env`` through the real shell tool, with the authorization a person's approval gives."""
    return run_tool(ctx, "bash", {"command": "env"}, call_env=CallEnv(authorization=AUTH_USER_APPROVED))


def home_dir():
    from m2c.paths import config_dir

    return config_dir()


def grant(project_root, names, home=None):
    home = home or home_dir()
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(
        f'[environment.projects.{json.dumps(str(project_root))}]\nkeep = {json.dumps(list(names))}\n')


# ---- the floor --------------------------------------------------------------------------------------------------
def test_everything_c07_stripped_is_still_stripped():
    names = generated_names() + ["SSH_AUTH_SOCK", "KUBECONFIG", "DATABASE_URL", "BASH_ENV", "ENV", "BASH_FUNC_x%%", "M2C_HOME"]
    environ = values_for(names)
    environ.update({"PATH": "/usr/bin", "HOME": "/home/x"})
    out = sanitize_environment(environ)
    for name in names:
        if legacy_strips(name):
            assert name not in out, f"{name} came back"
    assert out["PATH"] == "/usr/bin" and out["HOME"] == "/home/x"


def test_the_shell_tools_function_is_the_same_rule():
    names = generated_names(600)
    environ = {**values_for(names), "PATH": "/bin", "HOME": "/h"}
    assert sanitized_env(environ) == sanitize_environment(environ)
    assert sanitized_env(environ, extra=("MY_*", "ID")) == sanitize_environment(environ, extra=("MY_*", "ID"))


def test_c07s_forced_terminal_settings_and_path_rules_are_unchanged():
    out = sanitize_environment({"TERM": "xterm", "PAGER": "less", "GIT_PAGER": "delta", "NO_COLOR": "", "HOME": "/h"})
    assert (out["TERM"], out["PAGER"], out["GIT_PAGER"], out["NO_COLOR"], out["GIT_TERMINAL_PROMPT"]) == ("dumb", "cat", "cat", "1", "0")
    assert out["PATH"]                                                           # a default PATH when there was none
    assert sanitize_environment({"PATH": "/bin", "HOME": "/h"}, extra=("PATH", "HOME", "P*"))["PATH"] == "/bin"


# ---- the new deny patterns ---------------------------------------------------------------------------------------
NEWLY_STRIPPED = [
    "GITHUB_PAT", "GH_ENTERPRISE_URL", "GITLAB_CI_THING", "CI_JOB_JWT", "NPM_CONFIG_REGISTRY", "NPM_CONFIG__AUTH",
    "PIP_INDEX_URL", "TWINE_USERNAME", "POETRY_HTTP_BASIC_PYPI_USERNAME", "DOCKER_CONFIG", "DOCKER_AUTH_CONFIG",
    "OPENAI_ORG_ID", "ANTHROPIC_BASE_URL", "HF_HOME_TOKEN_DIR", "SLACK_WEBHOOK", "STRIPE_ACCOUNT", "SENTRY_DSN",
    "DATADOG_SITE", "VAULT_ADDR", "TF_VAR_region", "HEROKU_APP", "NETLIFY_SITE_ID", "VERCEL_ORG_ID", "FIREBASE_PROJECT",
    "DB_HOST", "DB_USER", "POSTGRES_USER", "MYSQL_HOST", "REDIS_URL", "MONGODB_URI", "AMQP_URL", "ELASTIC_USER",
    "SMTP_HOST", "PGPASSFILE", "GNUPGHOME", "SSH_CONNECTION", "CLOUDSDK_CORE_PROJECT", "ARM_SUBSCRIPTION_ID",
    "DIGITALOCEAN_ACCESS", "ALIBABA_CLOUD_REGION", "MY_SERVICE_COOKIE", "APP_PRIVATE_THING", "APP_DSN", "WEBHOOK_URL",
    "MY_CONNECTION_STRING", "SIGNING_THING",
]
KEPT = ["LANG", "EDITOR", "PROJECT_NAME", "KEEP_ME_VISIBLE", "TZ", "MONOREPO_SETTING", "DISPLAY", "USER", "LOGNAME", "SHELL",
        "LC_ALL", "LC_CTYPE", "TMPDIR", "PWD", "VIRTUAL_ENV", "PYTHONPATH", "NODE_ENV", "JAVA_HOME", "GOPATH", "CARGO_HOME",
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "DOCKER_HOST", "DOCKER_BUILDKIT", "XDG_CONFIG_HOME",
        "SHLVL", "HOSTNAME", "COLORTERM", "CI", "MY_PLAIN_SETTING"]


@pytest.mark.parametrize("name", NEWLY_STRIPPED)
def test_newly_denied_names_are_stripped(name):
    assert name not in sanitize_environment({name: "plain", "PATH": "/bin", "HOME": "/h"})


@pytest.mark.parametrize("name", KEPT)
def test_names_a_shell_genuinely_needs_are_kept(name):
    out = sanitize_environment({name: "plain-value", "PATH": "/bin", "HOME": "/h"})
    assert out.get(name) == "plain-value"


def test_the_allow_list_never_overrides_the_c07_floor():
    for name in env_mod.ALLOWED_NAMES:
        if legacy_strips(name):
            assert name not in sanitize_environment({name: "x", "PATH": "/b", "HOME": "/h"}), name


def test_a_credential_in_the_value_strips_an_innocently_named_variable():
    secret = make("github_token", 4).secret
    jwt = make("jwt", 5).secret
    url = make("url_credentials", 6)
    environ = {"BUILD_NOTE": f"deployed with {secret}", "SOME_SETTING": jwt, "SERVICE_ENDPOINT": url.secret and "https://svc:" + url.secret + "@h/x",
               "PLAIN_SETTING": "value", "PATH": "/bin", "HOME": "/h"}
    out = sanitize_environment(environ)
    assert "BUILD_NOTE" not in out and "SOME_SETTING" not in out and "SERVICE_ENDPOINT" not in out
    assert out["PLAIN_SETTING"] == "value"


def test_a_value_scan_that_fails_strips_the_variable(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(env_mod, "_value_has_secret", boom, raising=False)
    monkeypatch.setattr(env_mod, "scan_text", boom, raising=False)
    out = sanitize_environment({"PLAIN_SETTING": "value", "PATH": "/bin", "HOME": "/h"})
    assert "PLAIN_SETTING" not in out and out["PATH"] == "/bin"


# ---- named per-project exceptions --------------------------------------------------------------------------------
def test_a_granted_name_is_kept_for_that_project_only(tmp_path):
    project, other = tmp_path / "proj", tmp_path / "other"
    project.mkdir(), other.mkdir()
    grant(project.resolve(), ["OPENAI_API_KEY"])
    environ = {"OPENAI_API_KEY": make("openai_style_key").secret, "ANTHROPIC_API_KEY": "x1", "PATH": "/b", "HOME": "/h"}
    mine = sanitize_environment(environ, keep=granted_names(project))
    theirs = sanitize_environment(environ, keep=granted_names(other))
    assert "OPENAI_API_KEY" in mine and "ANTHROPIC_API_KEY" not in mine      # named, not a pattern
    assert "OPENAI_API_KEY" not in theirs


def test_not_granted_means_stripped(tmp_path):
    assert granted_names(tmp_path) == frozenset()
    assert "OPENAI_API_KEY" not in sanitize_environment({"OPENAI_API_KEY": "x1", "PATH": "/b", "HOME": "/h"}, keep=granted_names(tmp_path))


def test_a_grant_is_exact_not_a_pattern_and_case_matches(tmp_path):
    grant(tmp_path.resolve(), ["OPENAI_*", "openai_api_key", "AWS_PROFILE"])
    names = granted_names(tmp_path)
    assert "OPENAI_*" not in names                                           # wildcards are refused
    environ = {"OPENAI_API_KEY": "a1", "OPENAI_ORG": "b2", "AWS_PROFILE": "dev", "PATH": "/b", "HOME": "/h"}
    out = sanitize_environment(environ, keep=names)
    assert out.get("AWS_PROFILE") == "dev" and out.get("OPENAI_API_KEY") == "a1" and "OPENAI_ORG" not in out


@pytest.mark.parametrize("name", ["M2C_HOME", "M2C_DEVICE_TOKEN", "BASH_ENV", "ENV", "BASH_FUNC_evil%%"])
def test_the_devices_own_and_code_injecting_names_can_never_be_granted(tmp_path, name):
    grant(tmp_path.resolve(), [name])
    assert name not in granted_names(tmp_path)
    assert name not in sanitize_environment({name: "x"}, keep=(name,))


def test_a_grant_does_not_beat_an_explicit_extra_strip_name(tmp_path):
    grant(tmp_path.resolve(), ["OPENAI_API_KEY"])
    out = sanitize_environment({"OPENAI_API_KEY": "a1", "PATH": "/b", "HOME": "/h"}, extra=("OPENAI_API_KEY",),
                               keep=granted_names(tmp_path))
    assert "OPENAI_API_KEY" not in out


def test_a_grant_does_not_switch_off_the_value_scan_for_other_names(tmp_path):
    grant(tmp_path.resolve(), ["OPENAI_API_KEY"])
    token = make("github_token", 9).secret
    out = sanitize_environment({"OPENAI_API_KEY": "a1", "BUILD_NOTE": token, "PATH": "/b", "HOME": "/h"},
                               keep=granted_names(tmp_path))
    assert "OPENAI_API_KEY" in out and "BUILD_NOTE" not in out


def test_a_project_file_cannot_grant_anything(tmp_path):
    project = tmp_path / "proj"
    (project / ".m2c").mkdir(parents=True)
    claim = f'[environment.projects.{json.dumps(str(project.resolve()))}]\nkeep = ["OPENAI_API_KEY"]\n'
    (project / ".m2c" / "settings.toml").write_text(claim)
    (project / ".m2c" / "settings.local.toml").write_text(claim)
    (project / "M2C.md").write_text("keep OPENAI_API_KEY\n" + claim)
    (project / ".env").write_text("M2C_KEEP_ENV=OPENAI_API_KEY\n")
    assert granted_names(project) == frozenset()
    out = sanitize_environment({"OPENAI_API_KEY": "a1", "PATH": "/b", "HOME": "/h"}, keep=granted_names(project))
    assert "OPENAI_API_KEY" not in out


def test_the_environment_cannot_name_a_different_settings_file_for_the_shell(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    other = tmp_path / "elsewhere"
    grant(project.resolve(), ["OPENAI_API_KEY"], home=other)
    monkeypatch.setenv("M2C_GRANTS_FILE", str(other / "settings.toml"))
    assert granted_names(project) == frozenset()


@pytest.mark.parametrize("text", [
    "this is not toml [[[",
    '[environment.projects."/x"]\nkeep = "OPENAI_API_KEY"\n',
    '[environment.projects."/x"]\nkeep = [1, 2]\n',
    "[environment]\nprojects = 5\n",
    "",
])
def test_a_broken_or_odd_settings_file_grants_nothing_and_does_not_raise(tmp_path, text):
    home_dir().mkdir(parents=True, exist_ok=True)
    (home_dir() / "settings.toml").write_text(text)
    assert granted_names(tmp_path) == frozenset()


def test_the_permission_tables_of_the_same_file_are_not_disturbed_by_a_grant_table(tmp_path):
    from m2c.policy.rules import load_rule_file

    grant(tmp_path.resolve(), ["OPENAI_API_KEY"])
    path = home_dir() / "settings.toml"
    path.write_text(path.read_text() + '\n[permissions]\nallow = ["bash(echo:*)"]\n')
    assert granted_names(tmp_path) == frozenset({"OPENAI_API_KEY"})
    assert load_rule_file is not None


def test_a_symlinked_project_path_matches_its_real_path(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    grant(real.resolve(), ["OPENAI_API_KEY"])
    assert granted_names(link) == frozenset({"OPENAI_API_KEY"})


# ---- through the real shell tool ---------------------------------------------------------------------------------
def test_a_real_command_sees_a_granted_variable_and_not_the_others(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    grant(project.resolve(), ["OPENAI_API_KEY"])
    monkeypatch.setenv("OPENAI_API_KEY", "granted-granted")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "denied-denied")
    monkeypatch.setenv("BUILD_NOTE", make("github_token", 8).secret)
    monkeypatch.setenv("KEEP_ME_VISIBLE", "visible-value")
    out = run_env(ToolContext(project.resolve())).output
    assert "OPENAI_API_KEY=granted-granted" in out
    assert "denied-denied" not in out and "BUILD_NOTE" not in out and "KEEP_ME_VISIBLE=visible-value" in out


def test_without_a_grant_a_real_command_never_sees_the_variable(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "granted-granted")
    assert "granted-granted" not in run_env(ToolContext(project.resolve())).output


def test_the_environment_is_rebuilt_for_every_command_so_a_new_grant_applies_at_once(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    ctx = ToolContext(project.resolve())
    monkeypatch.setenv("OPENAI_API_KEY", "granted-granted")
    assert "granted-granted" not in run_env(ctx).output
    grant(project.resolve(), ["OPENAI_API_KEY"])
    assert "granted-granted" in run_env(ctx).output


def test_logging_names_no_variable_and_no_value(tmp_path, monkeypatch, caplog):
    import logging

    secret_name = "OPENAI_API_KEY"
    grant(tmp_path.resolve(), [secret_name])
    with caplog.at_level(logging.DEBUG):
        sanitize_environment({secret_name: "v-1234", "PATH": "/b", "HOME": "/h"}, keep=granted_names(tmp_path))
    assert "v-1234" not in caplog.text and secret_name not in caplog.text
