"""
m2c/tests/secret_handling/test_secrets_detectors.py -- Patch C15: every detector, positive and negative.

Positive: each kind of secret (built at run time, see secret_fakes.py) is replaced by a marker that names only the
kind, the text around it is untouched, and the findings carry a kind and a count and nothing else.
Negative: near-misses and ordinary code are left alone.  Also: Unicode evasions, idempotence, the fail-closed path.
"""
from __future__ import annotations

import dataclasses
import re
import time
import uuid

import pytest
from secret_fakes import BUILDERS, _cat, leaked, make

import m2c.secrets.redact as redact_mod
from m2c.secrets.redact import FAIL_CLOSED_TEXT, Finding, redact

MARKER_RE = re.compile(r"\[REDACTED:([a-z0-9_]+)\]")
SEEDS = range(5)


# ---- positive cases -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_each_kind_of_secret_is_replaced_by_a_marker_naming_only_the_kind(name, seed):
    fake = make(name, seed)
    result = redact(fake.text)
    assert not result.failed
    assert not leaked(result.text, fake.secret), f"{name}: part of the secret survived"
    kinds_in_text = set(MARKER_RE.findall(result.text))
    assert kinds_in_text & set(fake.kinds), f"{name}: expected one of {fake.kinds}, got {kinds_in_text}"
    assert {f.kind for f in result.findings} >= kinds_in_text
    assert all(isinstance(f, Finding) and f.count >= 1 for f in result.findings)


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_findings_hold_a_kind_and_a_count_and_never_the_secret(name):
    fake = make(name)
    result = redact(fake.text)
    for finding in result.findings:
        assert {f.name for f in dataclasses.fields(finding)} == {"kind", "count"}
    assert fake.secret not in repr(result) and fake.secret[4:20] not in repr(result)


def test_text_around_a_secret_is_untouched_and_the_marker_sits_where_it_was():
    fake = make("github_token")
    before, after = fake.text.split(fake.secret)
    assert redact(fake.text).text == f"{before}[REDACTED:github_token]{after}"


def test_authorization_values_are_replaced_but_the_scheme_stays_readable():
    fake = make("bearer_header")
    out = redact(fake.text).text
    assert "Authorization: Bearer [REDACTED:" in out and "https://api.example.test/v1/items" in out


def test_connection_strings_keep_their_host_but_lose_the_credentials():
    fake = make("url_credentials")
    out = redact(fake.text).text
    assert "@db.internal:5432/app" in out and "postgres://" in out and "svc:" not in out


def test_a_private_key_block_is_replaced_whole_header_to_footer():
    fake = make("private_key_block")
    out = redact(fake.text).text
    assert out == "before the key\n[REDACTED:private_key]\nafter the key"


def test_a_private_key_block_with_no_footer_is_redacted_to_the_end():
    fake = make("private_key_block")
    cut = fake.text.split("-----END")[0]
    out = redact(cut).text
    assert out.startswith("before the key\n[REDACTED:private_key]") and not leaked(out, fake.secret)


def test_several_secrets_are_all_replaced_and_counted_by_kind():
    parts = [make("github_token", 1).text, make("github_token", 2).text, make("jwt", 3).text, "plain line"]
    result = redact("\n".join(parts))
    counts = {f.kind: f.count for f in result.findings}
    assert counts["github_token"] == 2 and counts["jwt"] == 1
    assert result.text.endswith("plain line")


def test_redaction_is_idempotent_and_a_marker_is_never_a_finding():
    text = "\n".join(make(n, 1).text for n in sorted(BUILDERS))
    once = redact(text)
    twice = redact(once.text)
    assert twice.text == once.text and twice.findings == ()


def test_windows_line_endings_and_tabs_survive():
    fake = make("password_yaml")
    out = redact("a\r\n" + fake.text + "\r\n\tb\r\n").text
    assert out.startswith("a\r\n") and out.endswith("\r\n\tb\r\n") and not leaked(out, fake.secret)


def test_a_secret_hidden_with_zero_width_characters_is_still_redacted():
    token = make("github_token").secret
    sneaky = token[:3] + "​" + token[3:12] + "⁠" + token[12:]
    out = redact(f"x {sneaky} y").text
    assert out == "x [REDACTED:github_token] y"


def test_a_secret_in_fullwidth_letters_is_still_redacted():
    key = make("openai_style_key").secret
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in key)
    out = redact("k=" + fullwidth + " tail").text
    assert "tail" in out and "[REDACTED:" in out and fullwidth[3:20] not in out


def test_non_ascii_text_around_a_secret_keeps_its_offsets():
    fake = make("aws_access_key")
    text = "héllo wörld — 日本語 " + fake.text + " ✓ done"
    out = redact(text).text
    assert out.startswith("héllo wörld — 日本語 ") and out.endswith(" ✓ done") and not leaked(out, fake.secret)


# ---- negative cases: each detector, the near miss ----------------------------------------------------------------
NEGATIVES = [
    ("aws id with the wrong length", _cat("AK", "IA", "ABCDEFGH2345")),
    ("short github-looking prefix", _cat("gh", "p_", "short")),
    ("sk- prefix too short", _cat("s", "k-", "learn-style")),
    ("sk- inside a word", "a disk-encryption-key-management-guide-with-many-words"),
    ("a certificate block is public", "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"),
    ("a public key block", "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----"),
    ("a jwt with one segment", _cat("ey", "J", "abcdefghijkl"),),
    ("bearer placeholder", "Authorization: Bearer <token>"),
    ("bearer env reference", 'curl -H "Authorization: Bearer ${API_TOKEN}"'),
    ("bearer prose", "Bearer tokens are validated in the middleware."),
    ("password from a function", "password = get_password()"),
    ("password type annotation", "password: Optional[str] = None"),
    ("password attribute", "user.password = form.password"),
    ("password placeholder", "password: <your password>"),
    ("password masked", "password = ********"),
    ("token annotation", "def f(token: str) -> None: ..."),
    ("secret from the environment", "secret_key = os.environ['SECRET_KEY']"),
    ("token ttl number", "token_ttl_seconds = 3600"),
    ("token url", "token_url = 'https://auth.example.test/oauth2/token'"),
    ("url with only a user", "git clone https://user@github.com/example/project.git"),
    ("url with placeholders", "postgres://<user>:<password>@localhost/app"),
    ("url with env references", "postgres://${DB_USER}:${DB_PASS}@db/app"),
    ("uuid near a keyword", "token request id " + str(uuid.UUID(int=0x1234_5678_9ABC_DEF0_1234_5678_9ABC_DEF0))),
    ("git hash near a keyword", _cat("the key commit is ", "a" * 20, "b" * 20)),
    ("long identifier near a keyword", "the secret helper authenticateWithTheIdentityProviderUsingConfiguredClient2 runs"),
    ("empty", ""),
    ("plain prose", "The quick brown fox jumps over the lazy dog."),
]


@pytest.mark.parametrize("label, text", NEGATIVES, ids=[n for n, _ in NEGATIVES])
def test_near_misses_and_ordinary_code_are_left_alone(label, text):
    result = redact(text)
    assert result.text == text and result.findings == ()


def test_a_git_hash_or_uuid_is_never_redacted_even_under_a_credential_name_in_prose():
    import hashlib
    import uuid

    for i in range(20):
        sha = hashlib.sha1(str(i).encode()).hexdigest()
        sha256 = hashlib.sha256(str(i).encode()).hexdigest()
        ident = str(uuid.UUID(int=i * 7919 + 13))
        for line in (f"commit {sha}", f"token request {ident}", f"secret rotation tracked in {sha256}",
                     f"credential lookup {sha} {ident}", f"merge key {sha}"):
            assert redact(line).text == line, line


# ---- the fail-closed path ------------------------------------------------------------------------------------------
def test_redact_never_raises_and_a_non_string_fails_closed():
    for bad in (None, 5, b"bytes", ["x"], object()):
        result = redact(bad)  # type: ignore[arg-type]
        assert result.failed and result.text == FAIL_CLOSED_TEXT


def test_an_internal_error_withholds_the_whole_output(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(redact_mod, "_scan", boom)
    result = redact("an ordinary line that the broken detector never got to look at")
    assert result.failed and result.text == FAIL_CLOSED_TEXT and FAIL_CLOSED_TEXT == "[output withheld: redaction error]"
    assert result.findings == ()


def test_a_memory_error_is_also_fail_closed(monkeypatch):
    def boom(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(redact_mod, "_scan", boom)
    assert redact("x").text == FAIL_CLOSED_TEXT


# ---- robustness ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("hostile", [
    "a" * 400_000,
    "password" * 50_000,
    "password=" * 40_000,
    "password=\"" * 40_000,
    _cat("-----BEGIN ", "PRIVATE", " KEY-----") * 10_000,
    "eyJ" + "a" * 100_000,
    "Authorization: " * 30_000,
    "token " + "A1" * 60_000,
    "key " + ("Zz9" * 70 + " ") * 2_000,
    "​" * 100_000 + "x",
])
def test_hostile_inputs_finish_quickly(hostile):
    started = time.monotonic()
    result = redact(hostile)
    assert time.monotonic() - started < 8.0, "redaction of a hostile input took far too long"
    assert isinstance(result.text, str)


def test_a_large_ordinary_output_is_fast():
    text = "\n".join(f"line {i}: processed item {i * 7} in {i % 13}ms" for i in range(20_000))
    started = time.monotonic()
    assert redact(text).text == text
    assert time.monotonic() - started < 5.0
