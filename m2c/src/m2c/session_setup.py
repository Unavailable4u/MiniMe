"""
m2c/src/m2c/session_setup.py -- Patch C10b: what a session needs to use tools, built once for the TUI AND for ``m2c -p``.

Before this patch the TUI built these pieces inline in ``tui/session.py``: the C05 ``ToolContext`` for the project folder
(checked by C05's path guard), the C05 shell configuration, the C06 rule layers, the C12 run context, and the C06 policy
context.  Headless mode needs exactly the same pieces, and the same policy: two copies would drift, and a difference
between them is a hole in a security check.  So the construction lives here, and BOTH callers use it:

    ``open_project``            the folder check, the ToolContext + ShellConfig, the layers and the run context
    ``project_policy_context``  the C06 ``Context`` for a mode (what ``TuiSession.policy_context`` returns)

What only headless mode uses (the TUI never calls these):

    ``parse_cli_rules``         ``--allow`` / ``--deny`` text -> C06 rules, validated with C06's parser
    ``with_session_rules``      those rules as a session-only addition to the user layer
    ``denial_hint``             the fixed words that say WHICH rule a refused call needed
    ``headless_tool_setup``     the ``tool_setup`` argument of ``headless.run_headless``: validates, then (``open``) builds
                                the C24 ``ToolRunner`` and wraps it in a ``ToolHost``

How headless.py stays free of tool and policy imports (C10's rule): it never imports this module.  The CLI imports this
module and passes ``headless_tool_setup(...)`` in; headless.py calls it and uses the duck-typed result (``root``, ``notes``,
``run_start_fields``, ``open``; and for the host ``decide``, ``execute``, ``bind_run``, ``cancel``, ``aclose``, ``summary``).
This module imports ``ToolSetupError`` from headless.py, which is only an exception class.  It never imports the TUI.

The rules (all of them were already C06's; none is new here):

* The policy is C06's ``decide``, with the mode from ``--mode``.  Nothing in headless mode can ask, so the runner's headless
  adapter turns every would-ask into a refusal; this module only adds the sentence that says what was refused and why.
* ``--allow`` / ``--deny`` rules live for this one run.  C06 has no "session" layer (only managed, user and project), so the
  rules are appended to the USER layer in memory (never written to a file).  A deny still beats an allow, an ask rule still
  beats an allow, and a user file that failed to load still makes the whole layer untrustworthy (``invalid`` is kept).
* The project layer is never loaded (``project_trusted=False``: C06 does not even open the file); a project's settings file
  is reported as ignored, once.  Trust for a project is C26's business, not this patch's.
* The messages never contain what the model sent (the command, a path, the rule text): they are fixed words plus the name
  of the tool, and the name is only repeated when it is one of C06's catalog.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .context import RunContext, assemble_run_context
from .headless import ToolSetupError
from .logging_setup import log_event
from .paths import ConfigDirError, config_dir
from .policy import (
    Context,
    Request,
    RuleLayers,
    RuleSet,
    RuleSyntaxError,
    TOOLS,
    load_layers,
    parse_rule,
)
from .policy.decide import decide as policy_decide
from .runner import CHECKPOINTED_TOOLS, ToolRunner
from .tools import ToolContext
from .tools.paths import PathGuardError
from .tools.shell import ShellConfig, configure_shell

logger = logging.getLogger("m2c.session_setup")

SANDBOX_MODES = ("auto", "required", "off")
PROJECT_SETTINGS_IGNORED_NOTE = "project settings (.m2c/settings.toml) are ignored in this version"
_MODE_STRICTNESS = {"plan": 0, "ask": 1, "accept-edits": 2}


# ---------------------------------------------------------------------------------------------------------------------
# What the TUI and headless mode share
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProjectTools:
    """Everything built for one project folder.

    ``tool_context``           C05's context (the validated root, the denylist, the read-before-write record), with the
                               shell configuration attached
    ``root``                   the resolved project folder, as text
    ``config_home``            the M2C home (settings and memory live there)
    ``layers``                 C06's rule layers: managed none, user from ``config_home``, project NEVER loaded
    ``run_context``            C12's context fields for ``run.start``
    ``project_settings_present``  a ``.m2c/settings.toml`` exists in the project (the caller says it is ignored)
    ``settings_invalid``       the user (or managed) settings could not be trusted: C06 then never allows by rule
    """

    tool_context: ToolContext
    root: str
    config_home: Path
    layers: RuleLayers
    run_context: RunContext
    project_settings_present: bool
    settings_invalid: bool


def open_project(
    project_path: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    warn: Callable[[str], None] | None = None,
    sandbox: str = "auto",
) -> ProjectTools:
    """Build the project's tool pieces.  Raises ``PathGuardError`` when the folder cannot be a project (C05's check: it must
    exist, and must not be the filesystem root or the home directory, among others) and ``ConfigDirError`` for a bad
    ``M2C_HOME``; the caller words the error for its own surface.  This is the code that ``TuiSession.__init__`` ran inline
    before C10b, moved without change of behavior (the TUI passes ``sandbox="auto"``, the default, and its own ``warn``).

    ``warn`` receives the shell tool's "no sandbox is active" sentence once per session.  ``sandbox`` is C11's mode for
    ``ShellConfig`` (auto, required, off)."""
    tool_context = ToolContext(project_path)
    root = str(tool_context.root)
    configure_shell(tool_context, ShellConfig(warn=warn, sandbox=sandbox))
    env = os.environ if environ is None else environ
    config_home = Path(home) if home is not None else config_dir(env)
    layers = load_layers(managed=None, user=str(config_home / "settings.toml"), project=None, project_trusted=False)
    project_settings = Path(root) / ".m2c" / "settings.toml"
    run_context = assemble_run_context(root, root, config_home)
    return ProjectTools(
        tool_context=tool_context,
        root=root,
        config_home=config_home,
        layers=layers,
        run_context=run_context,
        project_settings_present=project_settings.exists(),
        settings_invalid=bool(layers.user.invalid or layers.managed.invalid),
    )


def project_policy_context(mode: str, layers: RuleLayers, root: str, *, project_trusted: bool = False) -> Context:
    """The C06 context for ``mode`` in the project at ``root``: nothing is tainted, ``~`` is the real home.  The project is
    not trusted unless the caller says so: headless mode never does (a trust decision needs a person, and C26's store is not
    this module's business); ``TuiSession.policy_context`` passes C26's answer, with the CURRENT mode, on every call.
    ``non_interactive`` is left
    off on purpose: the runner's headless adapter is what refuses a would-ask, and C06 can then still say ``ask`` (which is
    how the refusal can name "needs approval" rather than a bare deny)."""
    return Context(mode=mode, layers=layers, project_root=root, project_trusted=project_trusted, tainted=False,
                   home=str(Path.home()), cwd=root)


# ---------------------------------------------------------------------------------------------------------------------
# --allow / --deny
# ---------------------------------------------------------------------------------------------------------------------
def parse_cli_rules(allow: Sequence[str] = (), deny: Sequence[str] = ()) -> tuple[Any, ...]:
    """``--allow`` and ``--deny`` texts as C06 rules of the user layer (allows first, then denies; each flag's order kept).

    The syntax is C06's ``parse_rule`` and nothing else.  C06's parser checks only the SHAPE (``name(pattern)``), so a typo
    in a tool name would parse and then silently match nothing: for ``--allow`` that is merely a rule that does nothing,
    but for ``--deny`` it would be a protection the person thinks they have.  So the name is also checked against C06's
    tool catalog.  A bad rule raises ``ToolSetupError`` (exit 2) that names the flag and NEVER echoes the rule: it may be a
    command line with a secret in it."""
    rules: list[Any] = []
    for flag, action, texts in (("--allow", "allow", allow), ("--deny", "deny", deny)):
        for text in texts:
            try:
                rule = parse_rule(text, action, "user")
            except RuleSyntaxError:
                raise ToolSetupError(f"{flag}: not a valid rule; write it as tool(pattern), for example bash(git status).") from None
            if rule.tool not in TOOLS:
                raise ToolSetupError(f"{flag}: the rule names a tool this version does not have.") from None
            rules.append(rule)
    return tuple(rules)


def with_session_rules(layers: RuleLayers, rules: Sequence[Any]) -> RuleLayers:
    """``layers`` with ``rules`` added to the user layer, for this run only.  The managed and project layers are the same
    objects; ``invalid`` and ``errors`` of the user layer are kept, so a user file that failed to load still blocks every
    allow (fail closed), even one given on the command line."""
    user = layers.user
    merged = RuleSet(rules=tuple(user.rules) + tuple(rules), invalid=user.invalid, errors=user.errors)
    return dataclasses.replace(layers, user=merged)


# ---------------------------------------------------------------------------------------------------------------------
# The sentence that says which rule a refused call needed (fixed words; never the call's content)
# ---------------------------------------------------------------------------------------------------------------------
_PATH_TOOLS = frozenset({"read_file", "list_dir", "edit_file", "write_file", "delete_path"})


def denial_hint(tool: Any, action: str, matched_rule: str | None, mode: str, *, settings_invalid: bool = False) -> str:
    """One line of fixed words for a refused call: what rule would have been needed (``ask``), or why no rule helps
    (``deny``).  ``tool`` is repeated only when it is one of C06's catalog tools; ``matched_rule`` is used only for its
    ACTION prefix (``deny:...`` / ``ask:...``), its text is never repeated; the command, paths and patterns of the call are
    not inputs at all."""
    name = tool if isinstance(tool, str) and tool in TOOLS else None
    label = name or "the tool"
    kind = matched_rule.split(":", 1)[0] if isinstance(matched_rule, str) else ""
    if action == "deny":
        if kind == "deny":
            return "A deny rule forbids this call; remove that rule from your settings or --deny flags to allow it."
        if mode == "plan":
            return "In plan mode the policy refuses this call; use --mode ask or --mode accept-edits."
        return "The policy refuses this call outright (for example it touches a protected path); no rule can allow it."
    if kind == "ask":
        return "An ask rule covers this call, and headless mode cannot override it; remove that rule or run m2c interactively."
    if settings_invalid:
        return "Your settings file could not be read, so no allow rule is trusted; fix it, then run again."
    if name == "plan_exit":
        return "Leaving plan mode needs a person to approve it; start the run in another --mode instead."
    if name == "bash":
        return "Allow it with --allow 'bash(<command pattern>)'."
    if name in ("write_file", "edit_file") and mode == "ask":
        return f"Run with --mode accept-edits, or allow it with --allow '{name}(<path pattern>)'."
    if name in _PATH_TOOLS:
        return f"Allow it with --allow '{name}(<path pattern>)'."
    if name is not None:
        return f"Allow it with --allow '{name}(<pattern>)'."
    return f"Allow it with an --allow rule for {label}."


# ---------------------------------------------------------------------------------------------------------------------
# The tool host: the C24 runner, as headless.py uses it
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class HostDecision:
    """What ``ToolHost.decide`` answers: ``decision`` is ``allow`` / ``ask`` / ``deny``; ``hint`` the fixed words for the
    refusal message (empty for an allow)."""

    decision: str
    hint: str = ""


def _stricter_mode(a: str, b: Any) -> str:
    """The stricter of two modes (plan < ask < accept-edits), as the runner picks it; an unknown mode is kept for C06 to deny."""
    if a not in _MODE_STRICTNESS:
        return a
    if b not in _MODE_STRICTNESS:
        return a
    return a if _MODE_STRICTNESS[a] <= _MODE_STRICTNESS[b] else b


class ToolHost:
    """The C24 ``ToolRunner`` (no link, no ``ask``) plus the bookkeeping headless mode reports: what the refusal says, which
    files were changed, and the calls by outcome.  Built by ``HeadlessToolSetup.open`` on the loop thread that owns ``store``.

    ``decide`` and ``execute`` are the two hooks headless.py installs.  The runner does the real work (policy, checkpoint
    before an edit, the sandbox, redaction, one-run-per-call-id, serialization of mutating calls); this class only counts
    and words.  Everything here that is bookkeeping fails open: a failure to count never changes what a call did."""

    def __init__(self, runner: ToolRunner, store: sqlite3.Connection, layers: RuleLayers, root: str, mode: str,
                 settings_invalid: bool) -> None:
        self._runner = runner
        self._store = store
        self._layers = layers
        self._root = root
        self._mode = mode
        self._settings_invalid = settings_invalid
        self._session_id: str | None = None
        self._cancelling = False
        self._counts = {"ok": 0, "failed": 0, "denied": 0, "cancelled": 0}
        self._changed: set[str] = set()

    # -- the run ----------------------------------------------------------------------------------------------------
    def bind_run(self, run_id: str) -> None:
        """The run (and its local session, which has the same id) this host serves; checkpoints are refused until then."""
        self._session_id = run_id
        self._runner.bind_run(run_id)

    def cancel(self) -> None:
        """Kill every running command and drop what is queued.  Synchronous: it is called first, before anything awaits, so
        a command cannot outlive the run.  Nothing is sent to the server for a cancelled call."""
        self._cancelling = True
        self._runner.cancel_run()

    async def aclose(self) -> None:
        """Wait for the runner's tasks (a killed command takes up to the shell's grace period)."""
        await self._runner.aclose()

    # -- the two hooks ----------------------------------------------------------------------------------------------
    async def decide(self, proposal: Any) -> HostDecision:
        verdict = await self._runner.decide(proposal)
        if verdict == "allow":
            return HostDecision("allow")
        self._counts["denied"] += 1
        return HostDecision(verdict, self._hint(proposal, verdict))

    async def execute(self, proposal: Any) -> Any:
        tool = getattr(proposal, "tool", None)
        changes = tool in CHECKPOINTED_TOOLS
        before = self._last_step() if changes else 0
        try:
            outcome = await self._runner.execute(proposal)
        except asyncio.CancelledError:
            self._counts["cancelled"] += 1
            raise
        if self._cancelling:
            # cancel() came while it ran: the run is ending and the result must not be sent (the server closes the call)
            self._counts["cancelled"] += 1
            raise asyncio.CancelledError
        if getattr(outcome, "ok", False):
            self._counts["ok"] += 1
            if changes:
                self._record_changes(before)
        else:
            self._counts["failed"] += 1
        return outcome

    def summary(self) -> dict[str, Any]:
        """``files_changed`` (project-relative, sorted; only calls that succeeded after their checkpoint, so a ``bash``
        command's own changes are not listed) and ``tool_calls`` by outcome."""
        return {"files_changed": sorted(self._changed), "tool_calls": dict(self._counts)}

    # -- internals ---------------------------------------------------------------------------------------------------
    def _hint(self, proposal: Any, verdict: str) -> str:
        """The fixed-words hint for a refusal, from a second PURE C06 decision (it has no side effect).  Fails open to ''."""
        try:
            mode = _stricter_mode(self._mode, getattr(proposal, "mode", self._mode))
            context = project_policy_context(mode, self._layers, self._root)
            decision = policy_decide(Request(getattr(proposal, "tool", None), getattr(proposal, "input", None),
                                             getattr(proposal, "risk", None)), context)
            return denial_hint(getattr(proposal, "tool", None), verdict, decision.matched_rule, mode,
                               settings_invalid=self._settings_invalid)
        except Exception:  # noqa: BLE001 - the sentence is a courtesy; the refusal stands without it
            log_event(logger, logging.WARNING, "session_setup.hint_failed")
            return ""

    def _last_step(self) -> int:
        try:
            row = self._store.execute("SELECT COALESCE(MAX(step), 0) FROM checkpoints WHERE session_id = ?",
                                      (self._session_id,)).fetchone()
            return int(row[0])
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    def _record_changes(self, before: int) -> None:
        try:
            rows = self._store.execute("SELECT DISTINCT path FROM checkpoints WHERE session_id = ? AND step > ?",
                                       (self._session_id, before)).fetchall()
        except sqlite3.Error:
            return
        for (path,) in rows:
            if isinstance(path, str) and path:
                self._changed.add(path)


# ---------------------------------------------------------------------------------------------------------------------
# headless_tool_setup
# ---------------------------------------------------------------------------------------------------------------------
class HeadlessToolSetup:
    """What ``headless_tool_setup`` returns (the checked result of the first stage; ``open`` is the second).

    ``root``              the project folder (headless mode uses it as the run's project path)
    ``notes``             lines for stderr, fixed text (the project settings are ignored)
    ``run_start_fields``  the C12 fields for ``run.start`` (``instructions``, ``rules``, ``memory_index``)
    ``project``           the ``ProjectTools``"""

    def __init__(self, project: ProjectTools, layers: RuleLayers, notes: tuple[str, ...]) -> None:
        self.project = project
        self.root = project.root
        self.notes = notes
        self.run_start_fields = dict(project.run_context.run_start_fields())
        self._layers = layers
        self._notify: Callable[[str], None] | None = None

    def relay_warning(self, text: str) -> None:
        """The shell tool's sandbox warning, forwarded to whatever ``open`` was given (before that: dropped)."""
        notify = self._notify
        if notify is not None:
            notify(text)

    def open(self, store: sqlite3.Connection, notify: Callable[[str], None], *, mode: str) -> ToolHost:
        """Build the runner for this run: no link (the headless adapter sends nothing), no ``ask`` (nobody can answer), the
        policy context for ``mode``.  ``store`` is the connection of the loop that will call the host."""
        self._notify = notify
        layers = self._layers
        root = self.root
        runner = ToolRunner(None, run_id=None, tool_context=self.project.tool_context,
                            policy_context=lambda: project_policy_context(mode, layers, root), store=store, ask=None)
        return ToolHost(runner, store, layers, root, mode, self.project.settings_invalid)


def headless_tool_setup(
    *,
    cwd: str | os.PathLike[str] | None = None,
    allow: Sequence[str] = (),
    deny: Sequence[str] = (),
    sandbox: str = "auto",
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> HeadlessToolSetup:
    """Validate and prepare everything ``m2c -p`` needs to run tools; raises ``ToolSetupError`` (a usage error, exit 2) for a
    bad ``--allow`` / ``--deny`` / ``--sandbox`` / ``--cwd``.  Opens no connection and writes nothing: a failure here is
    reported before the run starts.  ``cwd`` None means the current directory, which is checked the same way (a headless run
    started from ``/`` or the home directory refuses, as the TUI does)."""
    rules = parse_cli_rules(allow, deny)
    if sandbox not in SANDBOX_MODES:
        raise ToolSetupError(f"--sandbox must be one of: {', '.join(SANDBOX_MODES)}.")
    holder: list[HeadlessToolSetup] = []

    def warn(text: str) -> None:
        if holder:
            holder[0].relay_warning(text)

    try:
        project = open_project(os.getcwd() if cwd is None else cwd, environ=environ, home=home, warn=warn,
                               sandbox=sandbox)
    except PathGuardError:
        raise ToolSetupError("--cwd: this folder cannot be used as a project; it must exist and must not be the "
                             "filesystem root or your home directory.") from None
    except ConfigDirError:
        raise ToolSetupError("The M2C config directory cannot be used; check M2C_HOME.", exit_code=1,
                             code="config_error") from None
    except OSError:
        raise ToolSetupError("--cwd: this folder could not be read.") from None
    notes = ("project settings (.m2c/settings.toml) are ignored in headless mode",) if project.project_settings_present else ()
    setup = HeadlessToolSetup(project, with_session_rules(project.layers, rules), notes)
    holder.append(setup)
    return setup
