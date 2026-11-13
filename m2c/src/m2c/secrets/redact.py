"""
m2c/src/m2c/secrets/redact.py -- Patch C15: find secrets in text and replace them, one-shot and as a stream.

Why this exists
---------------
Everything the model reads about this machine travels in ``tool_result.output`` and ``tool_stream.chunk``, and the
server is the untrusted side of the link (protocol/README.md: "Output is already redacted ... by the client").  A tool
that reads ``.env``, a command that prints ``env``, a ``git remote -v`` with a token in the URL: each would hand a live
credential to a third party.  So the runner (C24) passes every outgoing output through this module BEFORE it reaches
the link.  What the person sees in their own terminal is not touched: this is a filter on the outbound copy only.

The API
-------
``redact(text) -> Redaction``
    ``Redaction.text`` is the text with every secret replaced by ``[REDACTED:<kind>]`` (the kind only, never any part
    of the secret); ``Redaction.findings`` is a tuple of ``Finding(kind, count)``; ``Redaction.failed`` is True when
    something inside broke.  It NEVER raises.  On any internal error (and for anything that is not a ``str``) it fails
    CLOSED: ``text`` is the fixed ``FAIL_CLOSED_TEXT`` and nothing of the input is returned.  A security check that
    cannot run must not wave things through (build plan: security checks fail closed; metering and telemetry fail open).

``StreamRedactor``
    The same detectors for output that arrives in pieces.  It is LINE ORIENTED: ``feed(chunk)`` emits the complete
    lines it has (``\\n`` or ``\\r`` ends a line, so progress bars still move) and HOLDS the unfinished tail, because a
    token split across two chunks (``ghp_ab`` + ``cdef...``) cannot be recognised until it is whole.  ``flush()`` emits
    what is left; call it when the stream ends.  Three details make "holding the tail" safe and bounded:

    * a line longer than ``max_hold`` characters is cut, but only at whitespace and never inside a span the detectors
      can see, always keeping the last ~1 KB back; the previous ``STREAM_CONTEXT`` raw characters are carried over so
      ``password = `` and the value that follows are still seen together;
    * a private key block (BEGIN line seen, END line not yet) is held whole until its END line arrives; if it grows
      past ``max_block`` characters the redactor emits ONE ``[REDACTED:private_key]`` marker and swallows input up to
      the END line, so memory stays bounded and none of the body is ever emitted;
    * on any internal error the stream emits ``FAIL_CLOSED_TEXT`` once and then nothing more.

    The concatenated output of a stream equals ``redact(all the input).text`` for ordinary input (the tests pin it).

``scan_lines(text) -> [(line, kind)]``
    The STRICT profile used by C12's memory-write check: it never returns text, only where something secret-looking is
    (it refuses more: ``password: required`` is a finding there, and so is any ``NAME=value`` line).  Fail closed: if
    the scan itself breaks, the result is one finding of kind ``detector_error``.

What is detected (kinds)
------------------------
Cloud keys: ``aws_access_key``, ``google_api_key``, ``gcp_oauth_token``, ``gcp_client_secret``, ``azure_key``,
``azure_sas_signature``.  Tokens with well-known prefixes: ``github_token``, ``gitlab_token``, ``slack_token``,
``slack_webhook``, ``stripe_key``, ``api_key`` (``sk-...``: OpenAI/Anthropic style), ``npm_token``, ``pypi_token``,
``sendgrid_key``, ``huggingface_token``, ``digitalocean_token``, ``docker_token``, ``shopify_token``,
``databricks_token``, ``telegram_bot_token``, ``age_secret_key``.  ``jwt``.  ``private_key`` (the whole block).
``bearer_token`` and ``authorization_header`` (the value, not the word ``Bearer``).  ``password_assignment`` and
``secret_assignment`` (``name=value``, ``name: value``, JSON, ``.env``, ``--password value``).  ``url_credentials``
(``scheme://user:password@host``: only the user and password are replaced) and ``basic_auth`` (``curl -u user:pw``).
``high_entropy``: a 20-200 character string with both letters and digits and at least 3.5 bits per character, on a line
where a credential word (key, secret, token, password, credential, bearer ...) appears in the 80 characters before it.

What is deliberately NOT a secret
---------------------------------
Git commit hashes and every hex string of a digest length (32, 40, 56, 64, 96, 128), UUIDs, base64 ``data:`` URIs,
``sha512-...`` integrity strings, file paths, URLs, long identifiers (camelCase and snake_case are recognised by their
dictionary-like runs), ``${VAR}``/``$VAR``/``<placeholder>`` references, type annotations and calls
(``password: Optional[str]``, ``password = get_password()``).  The false-positive budget and the corpus that enforces it
are in tests/secret_handling/test_secrets_corpus.py.  A UUID or hash that is the VALUE of a name that is itself a
credential word (``api_key = <uuid>``) is still redacted: there the name says it is one.

Limits, stated plainly
----------------------
Patterns cannot see a secret that has been transformed (base64 of a token, split over two commands, spelled out in
words); this is hygiene against accident, not a defence against a model that is trying to exfiltrate.  A secret the
shell tool's own head/tail truncation cuts in half may survive as two harmless halves.  Unicode tricks are folded
(NFKC, zero-width and other format characters removed) and the replacement covers the original characters.

Logging: this module logs nothing.  The callers log kinds and counts, never text.

Standard library only.

Place this file at: m2c/src/m2c/secrets/redact.py
"""
from __future__ import annotations

import bisect
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = [
    "FAIL_CLOSED_TEXT",
    "Finding",
    "KIND_TEXT",
    "Redaction",
    "StreamRedactor",
    "contains_secret",
    "marker_for",
    "redact",
    "redaction_note",
    "scan_lines",
]

FAIL_CLOSED_TEXT = "[output withheld: redaction error]"
MAX_HOLD = 16_384            # a stream line longer than this is cut (at whitespace)
MAX_BLOCK = 65_536           # a private key block held longer than this is swallowed instead
STREAM_CONTEXT = 256         # raw characters of already-emitted text kept as context for the next piece
_STREAM_TAIL = 1_024         # a forced cut always keeps this much back
_STREAM_HARD = 1_048_576     # no whitespace anywhere in this much text: cut anyway
_END_KEEP = 128

KIND_TEXT = {
    "private_key": "a private key block",
    "github_token": "a GitHub access token",
    "gitlab_token": "a GitLab access token",
    "api_key": "an API key",
    "stripe_key": "a Stripe key",
    "aws_access_key": "an AWS access key ID",
    "slack_token": "a Slack token",
    "slack_webhook": "a Slack webhook URL",
    "google_api_key": "a Google API key",
    "gcp_oauth_token": "a Google OAuth access token",
    "gcp_client_secret": "a Google OAuth client secret",
    "azure_key": "an Azure access key",
    "azure_sas_signature": "an Azure SAS signature",
    "npm_token": "an npm token",
    "pypi_token": "a PyPI token",
    "sendgrid_key": "a SendGrid key",
    "huggingface_token": "a Hugging Face token",
    "digitalocean_token": "a DigitalOcean token",
    "docker_token": "a Docker access token",
    "shopify_token": "a Shopify token",
    "databricks_token": "a Databricks token",
    "telegram_bot_token": "a Telegram bot token",
    "age_secret_key": "an age secret key",
    "jwt": "a JSON Web Token",
    "bearer_token": "a Bearer token",
    "authorization_header": "an Authorization header value",
    "password_assignment": "a password assignment",
    "secret_assignment": "a secret, token or API key assignment",
    "env_line": "a .env style NAME=value line",
    "url_credentials": "a URL with a user name and password",
    "basic_auth": "a user name and password for basic authentication",
    "high_entropy": "a high-entropy string next to a key, secret, token or password",
    "detector_error": "text that could not be checked for secrets",
}


def marker_for(kind: str) -> str:
    return f"[REDACTED:{kind}]"


@dataclass(frozen=True)
class Finding:
    """What kind of secret and how many.  Never the secret, never a position inside the text."""

    kind: str
    count: int


@dataclass(frozen=True)
class Redaction:
    text: str
    findings: tuple[Finding, ...] = ()
    failed: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.findings)


def redaction_note(findings: Iterable[Finding]) -> str:
    """The one fixed line added to a result that changed: kinds and counts only."""
    parts = [f"{f.kind} x{f.count}" for f in findings]
    return "[Secrets were redacted on this device before this output was sent: " + ", ".join(parts) + ".]"


# ---------------------------------------------------------------------------
# Unicode folding with an offset map (so the replacement covers the ORIGINAL characters)
# ---------------------------------------------------------------------------
def _fold(text: str) -> tuple[str, list[int] | None]:
    if text.isascii():
        return text, None
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isascii():
            out.append(ch)
            index.append(i)
            continue
        for c in unicodedata.normalize("NFKC", ch):
            if unicodedata.category(c) != "Cf":
                out.append(c)
                index.append(i)
    return "".join(out), index


# ---------------------------------------------------------------------------
# Small predicates the detectors share
# ---------------------------------------------------------------------------
_L = r"(?<![A-Za-z0-9])"          # not in the middle of a longer alphanumeric run (an underscore before is fine)
_PLACEHOLDER_RE = re.compile(
    r"^(?:<.*|\$.*|\{\{.*|%\(.*|%[sd]|[*x.•\-]{3,}|.*\[REDACTED:.*|your[_-]?\w*|change[_-]?me|replace[_-]?me"
    r"|example|dummy|placeholder|redacted|secret|password|passwd|pass|pwd|token)$", re.IGNORECASE)
_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z_]{3,}$")
_KEYWORD_LITERALS = frozenset({
    "none", "null", "nil", "true", "false", "undefined", "required", "optional", "str", "string", "int", "integer",
    "bool", "boolean", "bytes", "any", "object", "secretstr", "raise", "return", "pass", "continue", "break", "print",
    "throw", "self", "cls", "lambda", "await", "yield", "assert", "del", "import", "from", "if", "else", "not"})
_ATTR_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CALLISH_RE = re.compile(r"[\[\](){}]")
_SNAKE_RE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
_SLUG_RE = re.compile(r"^[a-z]+(?:[-_.](?:[a-z]+|[0-9]{1,2}))*$")
_WORDS_RE = re.compile(r"^[a-z]+(?:[-_.][a-z]+)*$")
_LOWER_PATH_RE = re.compile(r"^(?:~|\.{1,2})?/(?:[a-z0-9_.-]+/)*[a-z0-9_.-]*$")
_LABEL_RE = re.compile(
    r"^(?:(?:enter|confirm|new|old|current|repeat|your|forgot|reset|change)\s+)*(?:password|passwd|pwd|passphrase)\??:?$",
    re.IGNORECASE)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_HASH_LENGTHS = frozenset({32, 40, 56, 64, 96, 128})
_PASSWORD_WORDS = frozenset({"password", "pass", "passwd", "pwd", "secret", "changeme", "xxxx", "xxxxxx"})


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'`")
    return not v or bool(_PLACEHOLDER_RE.match(v)) or bool(_ALLCAPS_RE.match(v))


def _has_digit_and_letter(value: str) -> bool:
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def _password_value_is_secret(value: str, quoted: bool, strict: bool, next_char: str) -> bool:
    inner = value[1:-1] if quoted else value
    if not inner.strip() or _is_placeholder(inner):
        return False
    if strict:
        return True
    if quoted:
        return not _LABEL_RE.match(inner.strip())
    if inner.lower() in _KEYWORD_LITERALS:
        return False
    if _CALLISH_RE.search(inner) or _ATTR_PATH_RE.match(inner) or _SNAKE_RE.match(inner):
        return False
    if next_char in ")," and _IDENT_RE.match(inner) and not any(c.isdigit() for c in inner):
        return False                                    # password=hashed) : a variable passed to a call
    return True


def _secret_value_is_secret(value: str, quoted: bool) -> bool:
    inner = value[1:-1] if quoted else value
    if len(inner) < 8 or _is_placeholder(inner) or not _has_digit_and_letter(inner):
        return False
    if "://" in inner or _LOWER_PATH_RE.match(inner) or _SLUG_RE.match(inner) or _ATTR_PATH_RE.match(inner):
        return False
    if not quoted and _CALLISH_RE.search(inner):
        return False
    return True


def _credential_value_ok(value: str, strict: bool) -> bool:
    if _is_placeholder(value):
        return False
    return strict or not _WORDS_RE.match(value)


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


# ---------------------------------------------------------------------------
# Token rules: a well-known shape is enough, no entropy needed
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    group: str | int = 0
    check: Callable[[str, bool], bool] | None = None


def _rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_PRIVATE_KEY_RE = _rx(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL)
_BEGIN_RE = _rx(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_END_RE = _rx(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")

_RULES: tuple[_Rule, ...] = (
    _Rule("private_key", _PRIVATE_KEY_RE),
    _Rule("aws_access_key", _rx(_L + r"(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Z0-9])")),
    _Rule("github_token", _rx(_L + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    _Rule("gitlab_token", _rx(_L + r"gl(?:pat|ptt|rt|dt|imt|agent|oas|soat|ft|cbt)-[A-Za-z0-9_-]{20,}")),
    _Rule("api_key", _rx(_L + r"sk-[A-Za-z0-9_-]{20,}"), 0, lambda v, s: any(c.isdigit() for c in v)),
    _Rule("stripe_key", _rx(_L + r"(?:[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{20,})")),
    _Rule("slack_token", _rx(_L + r"(?:xox[abposr]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,})")),
    _Rule("slack_webhook", _rx(r"https://hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9/_-]{20,}")),
    _Rule("google_api_key", _rx(_L + r"AIza[0-9A-Za-z_-]{35}")),
    _Rule("gcp_oauth_token", _rx(_L + r"ya29\.[A-Za-z0-9_-]{20,}")),
    _Rule("gcp_client_secret", _rx(_L + r"GOCSPX-[A-Za-z0-9_-]{20,}")),
    _Rule("azure_key", _rx(_L + r"(?:AccountKey|SharedAccessKey|PrimaryKey|SecondaryKey)\s*=\s*(?P<v>[A-Za-z0-9+/]{20,}={0,2})",
                          re.IGNORECASE), "v"),
    _Rule("azure_sas_signature", _rx(r"[?&]sig=(?P<v>[A-Za-z0-9%+/=_-]{20,})", re.IGNORECASE), "v"),
    _Rule("npm_token", _rx(_L + r"npm_[A-Za-z0-9]{36}")),
    _Rule("pypi_token", _rx(_L + r"pypi-[A-Za-z0-9_-]{50,}")),
    _Rule("sendgrid_key", _rx(_L + r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    _Rule("huggingface_token", _rx(_L + r"hf_[A-Za-z0-9]{30,}")),
    _Rule("digitalocean_token", _rx(_L + r"dop_v1_[a-f0-9]{64}")),
    _Rule("docker_token", _rx(_L + r"dckr_pat_[A-Za-z0-9_-]{20,}")),
    _Rule("shopify_token", _rx(_L + r"shp(?:at|ca|pa|ss)_[a-f0-9]{32}")),
    _Rule("databricks_token", _rx(_L + r"dapi[a-f0-9]{32}")),
    _Rule("telegram_bot_token", _rx(_L + r"[0-9]{8,10}:AA[A-Za-z0-9_-]{33}(?![A-Za-z0-9_-])")),
    _Rule("age_secret_key", _rx(_L + r"AGE-SECRET-KEY-1[A-Z0-9]{50,}")),
    _Rule("jwt", _rx(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)?")),
    _Rule("bearer_token", _rx(_L + r"Bearer[ \t]+(?P<v>[A-Za-z0-9._~+/=-]{16,})", re.IGNORECASE), "v",
          lambda v, s: _credential_value_ok(v, s)),
    _Rule("authorization_header", _rx(
        _L + r"Authorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:Bearer|Basic|Token|Negotiate)[ \t]+(?P<v>[A-Za-z0-9._~+/=-]{8,})",
        re.IGNORECASE), "v", lambda v, s: _credential_value_ok(v, s)),
    _Rule("authorization_header", _rx(
        _L + r"Authorization[\"']?[ \t]*[:=][ \t]*[\"']?(?!(?:Bearer|Basic|Token|Negotiate|Digest)\b)(?P<v>[A-Za-z0-9._~+/=-]{16,})",
        re.IGNORECASE), "v", lambda v, s: _credential_value_ok(v, s)),
)

# ---------------------------------------------------------------------------
# Assignments: name = value, in any of the usual spellings
# ---------------------------------------------------------------------------
_FAMILY_PASS = r"passw(?:or)?d|pwd|passphrase"
_FAMILY_SECRET = (r"secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth[_-]?key|credential|signing[_-]?key"
                  r"|encryption[_-]?key|master[_-]?key|session[_-]?key")
_VALUE = r"(?P<value>\"[^\"\r\n]{0,4096}\"|'[^'\r\n]{0,4096}'|[^\s\"'`,;)}\]]{1,4096})"
_ASSIGN_RE = _rx(
    r"(?P<pass>" + _FAMILY_PASS + r")|(?P<sec>" + _FAMILY_SECRET + r")", re.IGNORECASE)
_ASSIGN_TAIL_RE = _rx(
    r"s?[A-Za-z0-9_.-]{0,64}+[\"'`]?\]?[ \t]*(?:=>|:=|[:=])(?![=])[ \t]*" + _VALUE, re.IGNORECASE)
_CLI_FLAG_RE = _rx(
    r"(?<![A-Za-z0-9-])--(?P<flag>passw(?:or)?d|pwd|passphrase|(?:[a-z]+-)?(?:token|secret|api-?key)"
    r"|(?:access|auth|client)-(?:token|secret|key))[ \t]+(?P<value>\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s\"'`;|&<>]{1,4096})",
    re.IGNORECASE)
_CURL_USER_RE = _rx(
    r"(?<![A-Za-z0-9])(?:curl|wget|http|https|xh)\b[^\r\n]{0,500}?[ \t](?:-u|--user|--http-user|--ftp-user)[ \t]+"
    r"(?P<value>\"[^\"\r\n]{1,512}\"|'[^'\r\n]{1,512}'|[^\s\"'`]{1,512})")
_URL_CRED_RE = _rx(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{1,30}://(?P<v>(?P<user>[^\s/@:'\"<>]{1,256}):(?P<pw>[^\s/@'\"<>]{1,256}))@")
_ENV_LINE_RE = _rx(r"^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]+=\S", re.MULTILINE)

_KW_RE = _rx(r"(?i:secret|token|passw|pwd|passphrase|credential|bearer|apikey|api[_-]key|auth[_-](?:key|token)"
             r"|\bkey\b|_key\b|-key\b)|[a-z]Key\b")
_CANDIDATE_RE = _rx(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{20,200}+={0,2}(?![A-Za-z0-9+/_=-])")
_WORD_RUNS_RE = _rx(r"[a-z]{4,}|[A-Z][a-z]{3,}")
_PATH_SEGMENT_RE = _rx(r"[a-z][a-z0-9_-]{2,}")
_KW_WINDOW = 80

Span = tuple[int, int, str]


def _quoted_inner(value: str, start: int) -> tuple[int, int, bool]:
    """(start, end, quoted) of the part of a matched value to replace: inside the quotes when there are some."""
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return start + 1, start + len(value) - 1, True
    return start, start + len(value), False


def _scan_assignments(text: str, strict: bool) -> list[Span]:
    spans: list[Span] = []
    for m in _ASSIGN_RE.finditer(text):
        tail = _ASSIGN_TAIL_RE.match(text, m.end())
        if tail is None:
            continue
        value, vstart = tail.group("value"), tail.start("value")
        s, e, quoted = _quoted_inner(value, vstart)
        nxt = text[tail.end("value"):tail.end("value") + 1]
        if m.group("pass"):
            if _password_value_is_secret(value, quoted, strict, nxt):
                spans.append((s, e, "password_assignment"))
        elif _secret_value_is_secret(value, quoted):
            spans.append((s, e, "secret_assignment"))
    for m in _CLI_FLAG_RE.finditer(text):
        value = m.group("value")
        s, e, quoted = _quoted_inner(value, m.start("value"))
        is_pass = bool(re.match(r"passw|pwd|passphrase", m.group("flag"), re.IGNORECASE))
        if is_pass and _password_value_is_secret(value, quoted, strict, " "):
            spans.append((s, e, "password_assignment"))
        elif not is_pass and _secret_value_is_secret(value, quoted):
            spans.append((s, e, "secret_assignment"))
    for m in _CURL_USER_RE.finditer(text):
        value = m.group("value")
        s, e, quoted = _quoted_inner(value, m.start("value"))
        inner = value[1:-1] if quoted else value
        user, sep, pw = inner.partition(":")
        if sep and user and pw and not _is_placeholder(pw) and not pw.startswith("$"):
            spans.append((s, e, "basic_auth"))
    return spans


def _scan_urls(text: str, strict: bool) -> list[Span]:
    spans: list[Span] = []
    for m in _URL_CRED_RE.finditer(text):
        user, pw = m.group("user"), m.group("pw")
        if _is_placeholder(pw) or _is_placeholder(user) or "[REDACTED" in m.group("v"):
            continue
        if not strict and pw.lower() in _PASSWORD_WORDS:
            continue
        spans.append((m.start("v"), m.end("v"), "url_credentials"))
    return spans


def _identifier_like(candidate: str) -> bool:
    runs = sum(len(r) for r in _WORD_RUNS_RE.findall(candidate))
    return runs / len(candidate) >= 0.6


def _pathlike(candidate: str) -> bool:
    return "/" in candidate and sum(1 for s in candidate.split("/") if _PATH_SEGMENT_RE.fullmatch(s)) >= 2


def _scan_entropy(text: str) -> list[Span]:
    spans: list[Span] = []
    if not _KW_RE.search(text):
        return spans
    for m in _CANDIDATE_RE.finditer(text):
        cand = m.group(0)
        if not _has_digit_and_letter(cand):
            continue
        start = m.start()
        line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
        if not _KW_RE.search(text[max(line_start, start - _KW_WINDOW):start]):
            continue
        if text[max(0, start - 7):start] == "base64,":
            continue
        body = cand.rstrip("=")
        if _UUID_RE.match(body) or (_HEX_RE.match(body) and len(body) in _HASH_LENGTHS):
            continue
        if re.match(r"sha(?:1|256|384|512)-", body) or _pathlike(body) or _identifier_like(body):
            continue
        if _entropy(body) < 3.5:
            continue
        spans.append((start, m.end(), "high_entropy"))
    return spans


def _scan_rules(text: str, strict: bool) -> list[Span]:
    spans: list[Span] = []
    for rule in _RULES:
        for m in rule.pattern.finditer(text):
            group = rule.group
            value = m.group(group)
            if value is None or (rule.check is not None and not rule.check(value, strict)):
                continue
            spans.append((m.start(group), m.end(group), rule.kind))
    return spans


def _scan(text: str, strict: bool = False) -> list[Span]:
    """Every detector over ``text``; spans in the coordinates of the ORIGINAL text, overlapping, in priority order."""
    folded, index = _fold(text)
    spans = _scan_rules(folded, strict)
    spans += _scan_urls(folded, strict)
    spans += _scan_assignments(folded, strict)
    spans += _scan_entropy(folded)
    if strict:
        spans += [(m.start(), m.end(), "env_line") for m in _ENV_LINE_RE.finditer(folded)]
    if index is None:
        return spans
    return [(index[s], index[e - 1] + 1, k) for s, e, k in spans if e > s]


def _merge(spans: list[Span]) -> list[Span]:
    order = {id(s): i for i, s in enumerate(spans)}
    merged: list[list] = []
    for s, e, k in sorted(spans, key=lambda x: (x[0], -x[1], order[id(x)])):
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, k])
    return [(s, e, k) for s, e, k in merged]


def _findings(kinds: Iterable[str]) -> tuple[Finding, ...]:
    counts = Counter(kinds)
    return tuple(Finding(k, counts[k]) for k in sorted(counts))


def _apply(text: str, merged: list[Span], skip_before: int = 0) -> tuple[str, list[str]]:
    """``text[skip_before:]`` with the merged spans replaced; spans that end inside the skipped part are not touched."""
    out: list[str] = []
    kinds: list[str] = []
    pos = skip_before
    for s, e, k in merged:
        if e <= skip_before:
            continue
        s = max(s, skip_before)
        out.append(text[pos:s])
        out.append(marker_for(k))
        kinds.append(k)
        pos = e
    out.append(text[pos:])
    return "".join(out), kinds


def redact(text: str) -> Redaction:
    """Replace every secret in ``text``.  Never raises; fails closed (see the module docstring)."""
    try:
        if not isinstance(text, str):
            raise TypeError("not text")
        out, kinds = _apply(text, _merge(_scan(text)))
        return Redaction(out, _findings(kinds))
    except BaseException as exc:  # noqa: BLE001 - a redactor that cannot run must not pass the text through
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return Redaction(FAIL_CLOSED_TEXT, (), True)


def contains_secret(text: str) -> bool:
    """True when the (non-strict) detectors find anything in ``text``.  Raises on an internal error: callers decide."""
    return bool(_scan(text))


def scan_lines(text: str) -> list[tuple[int, str]]:
    """STRICT profile for the memory-write check: ``[(line, kind)]``, sorted, each pair once; never any text.

    Fails closed: if the scan itself breaks, the answer is one ``detector_error`` finding on line 1."""
    try:
        breaks = [m.end() for m in re.finditer(r"\r\n|\n|\r", text)]
        found = {(1 + bisect.bisect_right(breaks, s), k) for s, e, k in _scan(text, strict=True)}
        return sorted(found)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return [(1, "detector_error")]


# ---------------------------------------------------------------------------
# The stream redactor
# ---------------------------------------------------------------------------
class StreamRedactor:
    """Line-oriented, stateful redaction of output that arrives in pieces.  See the module docstring."""

    def __init__(self, *, max_hold: int = MAX_HOLD, max_block: int = MAX_BLOCK, context_chars: int = STREAM_CONTEXT) -> None:
        self._max_hold, self._max_block, self._context_chars = max_hold, max_block, context_chars
        self._pending = ""
        self._context = ""
        self._swallow = False
        self._failed = False
        self._kinds: Counter[str] = Counter()

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(Finding(k, self._kinds[k]) for k in sorted(self._kinds))

    def feed(self, chunk: str) -> str:
        """Text that is safe to send now (possibly empty)."""
        if self._failed:
            return ""
        try:
            if not isinstance(chunk, str):
                raise TypeError("not text")
            return self._feed(chunk)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return self._fail()

    def flush(self) -> str:
        """What is still held, redacted.  Call once when the stream ends."""
        if self._failed:
            return ""
        try:
            if self._swallow:
                self._pending = ""
                return ""
            out = self._emit(self._pending)
            self._pending = ""
            return out
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return self._fail()

    # -- internals ----------------------------------------------------------------------------------------------
    def _fail(self) -> str:
        self._failed = True
        self._pending = self._context = ""
        return FAIL_CLOSED_TEXT

    def _emit(self, segment: str) -> str:
        if not segment:
            return ""
        combined = self._context + segment
        out, kinds = _apply(combined, _merge(_scan(combined)), skip_before=len(self._context))
        self._kinds.update(kinds)
        self._context = combined[-self._context_chars:]
        return out

    def _open_block_start(self, text: str) -> int | None:
        for m in _BEGIN_RE.finditer(text):
            if _END_RE.search(text, m.end()) is None:
                return m.start()
        return None

    def _feed(self, chunk: str) -> str:
        if self._swallow:
            self._pending += chunk
            m = _END_RE.search(self._pending)
            if m is None:
                self._pending = self._pending[-_END_KEEP:]
                return ""
            self._pending = self._pending[m.end():]
            self._swallow = False
        else:
            self._pending += chunk
        return self._drain()

    def _drain(self) -> str:
        text = self._pending
        cut = max(text.rfind("\n"), text.rfind("\r")) + 1
        opened = self._open_block_start(text[:cut])
        if opened is not None:
            # a key block whose END line has not been completed (and ended) yet: hold it whole, from its first line
            line_start = max(text.rfind("\n", 0, opened), text.rfind("\r", 0, opened)) + 1
            out = self._emit(text[:line_start])
            held = text[line_start:]
            if len(held) > self._max_block:
                self._kinds["private_key"] += 1
                self._swallow = True
                self._pending = held[-_END_KEEP:]
                return out + marker_for("private_key")
            self._pending = held
            return out
        if len(text) - cut > self._max_hold:
            cut += self._forced_cut(text[cut:])
        self._pending = text[cut:]
        return self._emit(text[:cut])

    def _forced_cut(self, rest: str) -> int:
        """How many characters of ``rest`` (an over-long unfinished line) can be emitted now; 0 to keep holding."""
        limit = len(rest) - _STREAM_TAIL
        if limit <= 0:
            return 0
        window = rest[:limit]
        cut = max(window.rfind(" "), window.rfind("\t")) + 1
        if cut <= 0:
            if len(rest) <= _STREAM_HARD:
                return 0
            cut = limit
        combined = self._context + rest
        base = len(self._context)
        for s, e, _ in _merge(_scan(combined)):
            if s < base + cut < e:
                cut = max(s - base, 0)
        return cut
