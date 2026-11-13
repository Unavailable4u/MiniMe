"""
m2c/src/m2c/secrets/env.py -- Patch C15: the environment a shell command gets.

Why
---
A command the model runs inherits this process's environment.  ``env`` or ``echo $X`` would print every credential the
person's shell holds, and the output goes to the server.  C07 (tools/shell.py) already removed the obvious names; this
module is the stronger, single place those rules live now, and the function C07's shell tool calls
(``tools.shell.sanitized_env`` delegates to ``sanitize_environment`` here).

The layers, in order (the first that applies decides)
-----------------------------------------------------
1. ``PATH`` and ``HOME`` are always kept (C07).  Then the fixed terminal settings are applied (C07): ``TERM=dumb``,
   ``NO_COLOR=1``, ``PAGER=cat``, ``GIT_PAGER=cat``, ``GIT_TERMINAL_PROMPT=0``.
2. HARD NEVER (cannot be granted): this device's own ``M2C_*`` variables, exported shell functions (``BASH_FUNC_*``)
   and the code-injecting ``BASH_ENV`` and ``ENV``.
3. The person's ``extra_strip_names`` (``ShellConfig``): exact, or ``PREFIX*``; case-insensitive.  A grant does not beat it.
4. THE C07 FLOOR: names containing KEY, TOKEN, SECRET, PASSWORD, PASSWD or CREDENTIAL (so also ``KEYBOARD``, on purpose),
   prefixes ``AWS_ AZURE_ GOOGLE_ GCP_ GCLOUD_``, and ``SSH_AUTH_SOCK KUBECONFIG DATABASE_URL``.  Nothing C07 stripped
   comes back, except by a named grant (below).  The allow list below does not override the floor.
5. NAMED GRANTS: a name the person listed for THIS project in their own user settings is kept (past the floor and the
   deny patterns, never past layers 2 and 3).
6. THE ALLOW LIST: names a shell genuinely needs (``LANG``, ``USER``, ``TZ``, ``GIT_AUTHOR_*``, ``DOCKER_HOST`` ...) are
   exempt from layers 7 and 8.
7. DENY PATTERNS (new in C15): more cloud, CI, database, package-registry and service variables (prefixes such as
   ``GITHUB_``, ``NPM_``, ``PIP_``, ``DB_``, ``REDIS_``, ``SENTRY_``, ``VAULT_``, ``TF_``; substrings such as ``AUTH``,
   ``COOKIE``, ``PRIVATE``, ``PASSPHRASE``, ``DSN``, ``WEBHOOK``, ``CONNECTION_STRING``).  Over-matching is the safe
   direction: a name that is stripped by mistake is one grant away.
8. THE VALUE SCAN: a variable whose VALUE holds a credential (a token, a JWT, ``user:password@`` in a URL ...) is
   stripped whatever it is called.  If the scan itself fails, the variable is stripped (fail closed).
Everything else is kept, as before.

Named per-project grants
------------------------
The person can keep a named variable for one project (say ``OPENAI_API_KEY`` for the project that needs it) by writing,
in THEIR OWN user settings file ``<M2C_HOME>/settings.toml``::

    [environment.projects."/home/me/work/app"]
    keep = ["OPENAI_API_KEY"]

and only there.  Project files (``.m2c/settings.toml``, ``M2C.md``, ``.env``, anything in the repository) are never
read for grants, so a repository cannot grant itself its own secrets; neither does any environment variable choose the
file.  Names are exact (no patterns), checked as safe identifiers; a hard-never name in the list is ignored; a file that
cannot be parsed, or a table of the wrong shape, grants nothing (fail closed).  The project path is compared in its
resolved form, so a symlink to the project matches.  The environment is rebuilt for every command, so a new grant takes
effect on the next command.  A command that sets or reads the grant does not see the settings file's content.  (A screen
for making grants is not part of this patch; the file is the interface.)

Logging: one line when grants were applied, with a count only; never a name or a value.

Standard library only.

Place this file at: m2c/src/m2c/secrets/env.py
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..logging_setup import log_event
from ..paths import ConfigDirError, config_dir
from .redact import contains_secret as scan_text

logger = logging.getLogger("m2c.secrets")

__all__ = ["ALLOWED_NAMES", "FORCED", "granted_names", "sanitize_environment", "settings_path"]

# -- layer 1 / C07 ------------------------------------------------------------------------------------------------
KEEP_NAMES = frozenset({"PATH", "HOME"})
FORCED = {"TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"}
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

# -- layer 2 ------------------------------------------------------------------------------------------------------
_HARD_PREFIXES = ("M2C_", "BASH_FUNC_")
_HARD_NAMES = frozenset({"BASH_ENV", "ENV"})

# -- layer 4: the C07 floor, unchanged ----------------------------------------------------------------------------
_FLOOR_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_FLOOR_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GCLOUD_")
_FLOOR_NAMES = frozenset({"SSH_AUTH_SOCK", "KUBECONFIG", "DATABASE_URL"})

# -- layer 6 ------------------------------------------------------------------------------------------------------
ALLOWED_NAMES = frozenset({
    "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "PWD", "OLDPWD", "SHLVL", "HOSTNAME",
    "EDITOR", "VISUAL", "COLORTERM", "DISPLAY", "CI", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYTHONPATH",
    "PYTHONHOME", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "NODE_PATH", "NODE_ENV", "JAVA_HOME", "GOPATH", "GOROOT",
    "GOFLAGS", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR", "PNPM_HOME", "DOCKER_HOST", "DOCKER_BUILDKIT",
    "DOCKER_DEFAULT_PLATFORM", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE", "PIP_DISABLE_PIP_VERSION_CHECK", "PIP_REQUIRE_VIRTUALENV",
})
_ALLOWED_PREFIXES = ("LC_", "XDG_")

# -- layer 7 ------------------------------------------------------------------------------------------------------
_DENY_PREFIXES = (
    "ALIBABA_", "ALICLOUD_", "ARM_", "CLOUDSDK_", "DIGITALOCEAN_", "DOCKER_", "GH_", "GITHUB_", "GITLAB_", "CI_",
    "JENKINS_", "BUILDKITE_", "CIRCLE_", "TRAVIS_", "TEAMCITY_", "BITBUCKET_", "NPM_", "YARN_", "PIP_", "POETRY_",
    "TWINE_", "PYPI_", "CARGO_REGISTR", "NUGET_", "MAVEN_", "GRADLE_", "ORG_GRADLE_", "HF_", "HUGGINGFACE_", "OPENAI_",
    "ANTHROPIC_", "SLACK_", "STRIPE_", "SENTRY_", "DATADOG_", "DD_", "NEWRELIC_", "NEW_RELIC_", "VAULT_", "CONSUL_",
    "NOMAD_", "TF_", "TERRAFORM_", "HEROKU_", "NETLIFY_", "VERCEL_", "FIREBASE_", "SUPABASE_", "MYSQL_", "POSTGRES_",
    "PGHOST", "PGUSER", "PGDATABASE", "REDIS_", "MONGO", "DB_", "DATABASE_", "AMQP_", "RABBITMQ_", "KAFKA_",
    "ELASTIC_", "SMTP_", "MAILGUN_", "SENDGRID_", "TWILIO_", "GPG_", "GNUPG", "SSH_",
)
_DENY_SUBSTRINGS = ("AUTH", "COOKIE", "PRIVATE", "PASSPHRASE", "PASSFILE", "SIGNING", "DSN", "WEBHOOK",
                    "CONNECTION_STRING", "CONN_STR")

_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _upper_names(names: Iterable[str]) -> tuple[set[str], list[str]]:
    exact: set[str] = set()
    prefixes: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip("*"):
            continue
        upper = name.upper()
        if upper.endswith("*"):
            prefixes.append(upper.rstrip("*"))
        else:
            exact.add(upper)
    return exact, prefixes


def _hard_never(upper: str) -> bool:
    return upper in _HARD_NAMES or upper.startswith(_HARD_PREFIXES)


def _floor(upper: str) -> bool:
    return upper in _FLOOR_NAMES or upper.startswith(_FLOOR_PREFIXES) or any(s in upper for s in _FLOOR_SUBSTRINGS)


def _allowed(upper: str) -> bool:
    return upper in ALLOWED_NAMES or upper.startswith(_ALLOWED_PREFIXES)


def _denied_by_pattern(upper: str) -> bool:
    return upper.startswith(_DENY_PREFIXES) or any(s in upper for s in _DENY_SUBSTRINGS)


def _value_has_secret(value: str) -> bool:
    return scan_text(value)


def sanitize_environment(environ: Mapping[str, str], extra: Any = (), *, keep: Iterable[str] = ()) -> dict[str, str]:
    """The environment a command gets (see the module docstring for the layers).

    ``extra``: names to strip (exact, or ending in ``*`` for a prefix), case-insensitive.  ``keep``: the project's named
    grants (``granted_names``).  Never raises for a variable it cannot judge: it strips that variable."""
    extra_exact, extra_prefixes = _upper_names(tuple(extra))
    granted = {n.upper() for n in keep if isinstance(n, str) and _SAFE_NAME_RE.match(n) and not _hard_never(n.upper())}
    out: dict[str, str] = {}
    applied = 0
    for name, value in environ.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        upper = name.upper()
        if upper in KEEP_NAMES:
            out[name] = value
            continue
        if _hard_never(upper) or upper in extra_exact or (extra_prefixes and upper.startswith(tuple(extra_prefixes))):
            continue
        if upper in granted:
            if _floor(upper) or _denied_by_pattern(upper):
                applied += 1
            out[name] = value
            continue
        if _floor(upper):
            continue
        if not _allowed(upper):
            if _denied_by_pattern(upper):
                continue
            try:
                if _value_has_secret(value):
                    continue
            except Exception:  # noqa: BLE001 - a value that cannot be judged is not passed on
                continue
        out[name] = value
    out.update(FORCED)
    if not out.get("PATH"):
        out["PATH"] = DEFAULT_PATH
    if applied:
        log_event(logger, logging.INFO, "secrets.env_grants_applied", count=applied)
    return out


# ---------------------------------------------------------------------------
# Named per-project grants (user settings only)
# ---------------------------------------------------------------------------
def settings_path() -> Path | None:
    """``<M2C_HOME>/settings.toml`` (the person's own file), or None when the home cannot be located."""
    try:
        return config_dir() / "settings.toml"
    except ConfigDirError:
        return None


def _same_project(key: str, project: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(key)) == project
    except (OSError, ValueError):
        return False


def granted_names(project_root: "str | os.PathLike[str]") -> frozenset[str]:
    """The variable names the person kept for this project.  Reads ONLY the user settings file; never raises."""
    try:
        path = settings_path()
        if path is None or not path.is_file():
            return frozenset()
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
        projects = doc.get("environment", {}).get("projects", {})
        if not isinstance(projects, dict):
            return frozenset()
        project = os.path.normcase(os.path.realpath(os.fspath(project_root)))
        names: set[str] = set()
        for key, table in projects.items():
            if not isinstance(table, dict) or not _same_project(key, project):
                continue
            listed = table.get("keep", [])
            if not isinstance(listed, list) or not all(isinstance(n, str) for n in listed):
                continue
            names.update(n.upper() for n in listed if _SAFE_NAME_RE.match(n) and not _hard_never(n.upper()))
        return frozenset(names)
    except Exception:  # noqa: BLE001 - an unreadable or odd file grants nothing
        return frozenset()
