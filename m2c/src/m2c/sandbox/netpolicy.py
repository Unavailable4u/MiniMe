"""
m2c/src/m2c/sandbox/netpolicy.py -- Patch C11b: which domains a sandboxed command may reach (the decision logic).

THE RULES (pure, no I/O except the one saved file)
--------------------------------------------------
The allowlist holds EXACT hosts (``pypi.org``), explicit wildcards (``*.example.com``) and exact IP literals.

* A wildcard matches any host UNDER the name, at any depth, and never the bare name: ``*.example.com`` matches
  ``a.example.com`` and ``a.b.example.com`` but not ``example.com``.  A wildcard needs at least two labels after the
  star (``*.com`` is ignored), and nothing else with a star is valid.  The star cannot know public suffixes, so a
  person who writes ``*.co.uk`` has allowed a lot; that is theirs to decide.
* Names are lowercased, IDNA-normalized (what is shown and compared is the ASCII/punycode form, so a look-alike
  domain in another script can never be mistaken for the real one on screen) and have no trailing dot.
* IP literals are DENIED unless the exact address is listed (never asked about).  Odd spellings of an address
  (``2130706433``, ``0x7f.1``, ``127.1``) are denied outright.  ``localhost`` and ``*.localhost`` are denied too.
* Only the allowed destination ports pass (443 by default); any other port is denied before anything else.

DECISIONS: ``allow``, ``deny`` or ``ask``.  ``NetworkPolicy.check`` is pure and returns one of the three.  ``decide``
turns ``ask`` into allow or deny: it calls the injected async ``DomainPrompt(host, port, label)``, which answers
``allow_session``, ``allow_always`` or ``deny``.

* A timeout (60 s by default), an error in the prompt and a cancelled prompt all mean DENY.
* Several simultaneous requests for the same host share ONE prompt.  At most ``max_pending_prompts`` different hosts
  can be waiting for the person at once; more are denied without a prompt, so a command cannot bury the person in
  prompts.
* A denial (or timeout) is remembered for ``deny_cooldown_s`` seconds, so a command that retries in a loop does not
  raise the same question again and again right after the person said no.
* With NO prompt installed (headless), ``ask`` means deny, and ``unprompted_denials`` counts how often that
  happened, so headless mode can end with its policy-denial exit code (wiring that is a later one-line follow-up).
* ``allow_always`` is saved to ``~/.m2c/network.json`` (mode 0600, atomic write).  That file is read from the user's
  config directory ONLY: a project's files are untrusted and are never consulted.  It lives inside the directory the
  file tools, the policy engine and the sandbox already keep the model away from.  A file that is a symlink, owned
  by someone else, writable by others or too large is ignored (fail closed); malformed entries are ignored.

What is logged: nothing from here.  ``record`` (if given) receives ``{host, decision, scope}`` for each final
decision -- never a path, header, payload or command.  The command LABEL goes to the prompt only, so the person can
tell which command is asking; it is never recorded.

Standard library only (asyncio).

Place this file at: m2c/src/m2c/sandbox/netpolicy.py
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from ..logging_setup import log_event

__all__ = [
    "ALLOW", "ANSWER_ALLOW_ALWAYS", "ANSWER_ALLOW_SESSION", "ANSWER_DENY", "ASK", "DEFAULT_PORTS", "DENY",
    "Allowlist", "AllowFile", "Decision", "DomainPrompt", "Entry", "NetworkPolicy", "command_label",
    "normalize_host", "parse_entry", "prompt_on_loop",
]

logger = logging.getLogger("m2c.sandbox.netpolicy")

ALLOW, DENY, ASK = "allow", "deny", "ask"
ANSWER_ALLOW_SESSION, ANSWER_ALLOW_ALWAYS, ANSWER_DENY = "allow_session", "allow_always", "deny"
_ANSWERS = (ANSWER_ALLOW_SESSION, ANSWER_ALLOW_ALWAYS, ANSWER_DENY)
DEFAULT_PORTS: tuple[int, ...] = (443,)

DomainPrompt = Callable[[str, int, str], Awaitable[str]]

MAX_HOST_CHARS = 253
MAX_LABEL_CHARS = 60
MAX_FILE_BYTES = 64 * 1024
MAX_ENTRIES = 1000
MAX_SESSION_HOSTS = 1000
_LABEL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


# ==========================================================================
# hosts and entries
# ==========================================================================
def normalize_host(raw: object) -> str | None:
    """The canonical form of a host name or IP literal, or None when it is not acceptable.

    Lowercase, IDNA (punycode) for non-ASCII names, one trailing dot removed, letters/digits/hyphen/underscore labels
    of 1..63 characters, 253 characters at most.  An IPv6 literal may be written with brackets.  Anything with a
    port, path, user, star, space, control character or zone id is None."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_HOST_CHARS + 8:
        return None
    if "%" in text:                                   # a zone id (fe80::1%eth0) names an interface, never a destination
        return None
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
        try:
            addr = ipaddress.IPv6Address(text)
        except ValueError:
            return None
        return str(addr)
    if ":" in text:                                   # a bare IPv6 literal; anything else with a colon is not a host
        try:
            return str(ipaddress.IPv6Address(text))
        except ValueError:
            return None
    if text.endswith("."):
        text = text[:-1]
    if not text or text.endswith("."):
        return None
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        try:
            text = text.lower().encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
    text = text.lower()
    if len(text) > MAX_HOST_CHARS:
        return None
    labels = text.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-"):
            return None
        if not re.fullmatch(r"[a-z0-9_-]+", label):
            return None
    try:
        return str(ipaddress.IPv4Address(text))       # canonical dotted quad stays as it is
    except ValueError:
        pass
    return text


def _ip_literal(host: str) -> str | None:
    """The canonical address when ``host`` (already normalized) is an IP literal, else None."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _numeric_lookalike(host: str) -> bool:
    """True for a name that a resolver would read as an address in an odd spelling (``2130706433``, ``0x7f.1``,
    ``127.1``): a final label that is all digits or a hex number.  No real top-level domain looks like that."""
    last = host.rsplit(".", 1)[-1]
    return bool(re.fullmatch(r"[0-9]+|0x[0-9a-f]*", last))


@dataclass(frozen=True)
class Entry:
    kind: str       # "exact", "wildcard" or "ip"
    value: str      # the host, the name under the star, or the canonical address


def parse_entry(raw: object) -> Entry | None:
    """One allowlist entry, or None when it is malformed (callers ignore those)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("*."):
        base = normalize_host(text[2:])
        if base is None or _ip_literal(base) is not None or _numeric_lookalike(base) or base.count(".") < 1:
            return None
        return Entry("wildcard", base)
    if "*" in text:
        return None
    host = normalize_host(text)
    if host is None:
        return None
    ip = _ip_literal(host)
    if ip is not None:
        return Entry("ip", ip)
    if _numeric_lookalike(host):
        return None
    return Entry("exact", host)


class Allowlist:
    """A set of entries.  ``matches`` takes a normalized host."""

    def __init__(self, entries: Iterable[object] = ()) -> None:
        self._exact: set[str] = set()
        self._wild: set[str] = set()
        self._ips: set[str] = set()
        for raw in entries:
            self.add(raw)

    def add(self, raw: object) -> bool:
        entry = parse_entry(raw)
        if entry is None:
            return False
        target = {"exact": self._exact, "wildcard": self._wild, "ip": self._ips}[entry.kind]
        target.add(entry.value)
        return True

    def matches(self, host: str) -> bool:
        ip = _ip_literal(host)
        if ip is not None:
            return ip in self._ips
        if host in self._exact:
            return True
        parts = host.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[i:]) in self._wild:
                return True
        return False

    def __len__(self) -> int:
        return len(self._exact) + len(self._wild) + len(self._ips)

    def entries(self) -> list[str]:
        return sorted(self._exact) + sorted("*." + w for w in self._wild) + sorted(self._ips)


# ==========================================================================
# the saved file
# ==========================================================================
class AllowFile:
    """``~/.m2c/network.json``: ``{"version": 1, "allow": ["host", ...]}``.  User level only."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            from .. import paths

            path = paths.config_dir() / "network.json"
        self.path = Path(path)

    def load(self) -> list[str]:
        """The valid entries in the file; [] when it is missing, unsafe or unreadable.  Never raises."""
        try:
            st = os.lstat(self.path)
        except OSError:
            return []
        import stat as _stat

        if not _stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return []
        if hasattr(os, "geteuid") and (st.st_uid != os.geteuid() or st.st_mode & 0o022):
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("allow") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items[:MAX_ENTRIES]:
            entry = parse_entry(item)
            if entry is not None:
                out.append("*." + entry.value if entry.kind == "wildcard" else entry.value)
        return out

    def add(self, host: str) -> bool:
        """Add one exact host (re-reading the file first, so a second running m2c's additions are kept).  True when
        the file now holds it.  Atomic: the new content is written beside the file and renamed over it."""
        entry = parse_entry(host)
        if entry is None or entry.kind == "wildcard":
            return False
        value = entry.value
        current = self.load()
        if value in current:
            return True
        current.append(value)
        payload = json.dumps({"version": 1, "allow": current[:MAX_ENTRIES]}, indent=2) + "\n"
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(prefix=".network.", suffix=".tmp", dir=directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            log_event(logger, logging.WARNING, "net.policy", check="network_file", status="error", code="save_failed")
            return False
        return True


# ==========================================================================
# the policy
# ==========================================================================
@dataclass(frozen=True)
class Decision:
    """``action`` is allow, deny or ask.  ``scope`` says where the verdict came from: ``listed`` (the caller's list
    or the saved file), ``session`` / ``always`` (the person's answer), or a reason for a denial (``port``,
    ``ip_literal``, ``invalid_host``, ``local_name``, ``user``, ``timeout``, ``no_prompt``, ``cooldown``,
    ``too_many_prompts``, ``prompt_error``, ``not_listed`` for ask)."""

    action: str
    scope: str


def command_label(command: object) -> str:
    """A short label of a command for the prompt: its first line, secrets redacted, control characters removed, 60
    characters at most.  It is shown to the person only.  Anything that goes wrong gives a generic label."""
    try:
        text = str(command).splitlines()[0] if str(command).strip() else ""
        try:
            from ..secrets.redact import redact

            red = redact(text)
            if red.failed:
                return "a command"                    # redaction failed: show nothing of the command
            text = red.text
        except Exception:  # noqa: BLE001 - never show a command that could not be checked
            return "a command"
        text = _LABEL.sub("", text).strip()
        if len(text) > MAX_LABEL_CHARS:
            text = text[: MAX_LABEL_CHARS - 1] + "…"
        return text or "a command"
    except Exception:  # noqa: BLE001
        return "a command"


class NetworkPolicy:
    """The allowlist, the saved file, the session's answers and the prompt.  Use it on one event loop."""

    def __init__(
        self,
        *,
        allow: Iterable[object] = (),
        ports: Iterable[int] = DEFAULT_PORTS,
        store: AllowFile | None = None,
        prompt: DomainPrompt | None = None,
        prompt_timeout_s: float = 60.0,
        deny_cooldown_s: float = 15.0,
        max_pending_prompts: int = 3,
        record: Callable[[dict[str, str]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ports = frozenset(int(p) for p in ports if isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536)
        self._configured = Allowlist(allow)
        self._store = store
        self._saved = Allowlist(store.load() if store is not None else ())
        self._session: set[str] = set()
        self._prompt = prompt
        self.prompt_timeout_s = prompt_timeout_s
        self.deny_cooldown_s = deny_cooldown_s
        self.max_pending_prompts = max_pending_prompts
        self._record = record
        self._clock = clock
        self._denied_until: dict[str, float] = {}
        self._pending: dict[str, asyncio.Future[Decision]] = {}
        self.unprompted_denials = 0

    # -- configuration ---------------------------------------------------------------------------------------------
    def set_prompt(self, prompt: DomainPrompt | None) -> None:
        self._prompt = prompt

    @property
    def has_prompt(self) -> bool:
        return self._prompt is not None

    # -- the pure part ---------------------------------------------------------------------------------------------
    def check(self, host: object, port: object) -> Decision:
        """allow, deny or ask for (host, port).  No prompting, no I/O."""
        name = normalize_host(host)
        if name is None:
            return Decision(DENY, "invalid_host")
        if not isinstance(port, int) or isinstance(port, bool) or port not in self.ports:
            return Decision(DENY, "port")
        ip = _ip_literal(name)
        if ip is not None:
            if self._configured.matches(ip) or self._saved.matches(ip):
                return Decision(ALLOW, "listed")
            return Decision(DENY, "ip_literal")
        if _numeric_lookalike(name):
            return Decision(DENY, "ip_literal")
        if name == "localhost" or name.endswith(".localhost"):
            return Decision(DENY, "local_name")
        if self._configured.matches(name) or self._saved.matches(name):
            return Decision(ALLOW, "listed")
        if name in self._session:
            return Decision(ALLOW, "session")
        until = self._denied_until.get(name)
        if until is not None:
            if self._clock() < until:
                return Decision(DENY, "cooldown")
            del self._denied_until[name]
        return Decision(ASK, "not_listed")

    # -- the deciding part -----------------------------------------------------------------------------------------
    async def decide(self, host: object, port: object, label: str = "") -> Decision:
        """allow or deny for (host, port), asking the person when needed.  Never raises (except cancellation)."""
        name = normalize_host(host)
        verdict = self.check(host, port)
        if verdict.action == ASK:
            verdict = await self._ask(name or "", int(port), label)  # type: ignore[arg-type]
        self._emit(name, verdict)
        return verdict

    def note(self, host: object, action: str, scope: str) -> None:
        """Record a decision made elsewhere (the proxy refusing an allowed name's unsafe address)."""
        self._emit(normalize_host(host), Decision(action, scope))

    def _emit(self, host: str | None, verdict: Decision) -> None:
        if self._record is None or host is None:
            return
        try:
            self._record({"host": host, "decision": verdict.action, "scope": verdict.scope})
        except Exception:  # noqa: BLE001 - recording is telemetry: it fails open
            pass

    async def _ask(self, host: str, port: int, label: str) -> Decision:
        if self._prompt is None:
            self.unprompted_denials += 1
            return Decision(DENY, "no_prompt")
        pending = self._pending.get(host)
        if pending is None:
            if len(self._pending) >= self.max_pending_prompts:
                return Decision(DENY, "too_many_prompts")
            pending = asyncio.ensure_future(self._run_prompt(host, port, label))
            self._pending[host] = pending
        # shield: one waiter giving up must not cancel the question the others are waiting on
        return await asyncio.shield(pending)

    async def _run_prompt(self, host: str, port: int, label: str) -> Decision:
        prompt = self._prompt
        try:
            try:
                async with asyncio.timeout(self.prompt_timeout_s):
                    answer = await prompt(host, port, label)  # type: ignore[misc]
            except TimeoutError:
                return self._denied(host, "timeout")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken prompt is a denial
                log_event(logger, logging.WARNING, "net.policy", check="domain_prompt", status="error", code="prompt_error")
                return self._denied(host, "prompt_error")
            if answer == ANSWER_ALLOW_SESSION or answer == ANSWER_ALLOW_ALWAYS:
                if len(self._session) < MAX_SESSION_HOSTS:
                    self._session.add(host)
                if answer == ANSWER_ALLOW_ALWAYS:
                    if self._store is not None and self._store.add(host):
                        self._saved.add(host)
                    return Decision(ALLOW, "always")
                return Decision(ALLOW, "session")
            return self._denied(host, "user")
        finally:
            self._pending.pop(host, None)

    def _denied(self, host: str, scope: str) -> Decision:
        if self.deny_cooldown_s > 0:
            if len(self._denied_until) >= MAX_SESSION_HOSTS:
                self._denied_until.clear()
            self._denied_until[host] = self._clock() + self.deny_cooldown_s
        return Decision(DENY, scope)

    async def aclose(self) -> None:
        """Cancel any question still waiting (the person is no longer asked)."""
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()


# ==========================================================================
# crossing event loops
# ==========================================================================
def prompt_on_loop(prompt: Callable[[str, int, str], Awaitable[Any]], loop: asyncio.AbstractEventLoop) -> DomainPrompt:
    """A ``DomainPrompt`` that runs ``prompt`` on ``loop`` (the terminal's loop) while the proxy runs on its own
    thread's loop.  Cancelling the proxy's wait cancels the prompt, so a timed-out question disappears from the screen."""

    async def ask(host: str, port: int, label: str) -> str:
        future = asyncio.run_coroutine_threadsafe(prompt(host, port, label), loop)
        try:
            answer = await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        return answer if answer in _ANSWERS else ANSWER_DENY

    return ask
