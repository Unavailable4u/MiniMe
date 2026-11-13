"""
m2c/tests/secret_handling/secret_fakes.py -- Patch C15: fake secrets and ordinary developer text, built at run time.

WHY EVERYTHING HERE IS ASSEMBLED WHEN A TEST RUNS
-------------------------------------------------
This repository's pre-commit hook and CI run gitleaks, and m2c/tests/ is not on its allowlist.  So no string in this
directory may look like a real credential as a literal: every fake below is joined from short pieces, or drawn from a
seeded random generator, when it is CALLED.  ``_cat`` is a function on purpose: ``"gh" + "p_" + ...`` in a function
body is folded by the Python compiler into one contiguous constant (and lands in the .pyc file, where gitleaks looks
too), a call to ``_cat`` is not.

The fakes are random, not hand-picked, so the tests exercise the detectors on values nobody chose.  Every random body
is given at least one digit and one letter (``_mixed``): a real 32-character key lacks a digit with a chance below one
in a thousand, and the detectors that need a digit say so in their documentation.

``Fake.secret`` is the part that must never survive redaction; ``Fake.text`` is a realistic line around it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import uuid
from dataclasses import dataclass
from typing import Callable

UPPER = string.ascii_uppercase
LOWER = string.ascii_lowercase
DIGITS = string.digits
B62 = string.ascii_letters + string.digits
B64URL = B62 + "-_"
B64STD = B62 + "+/"
HEX = "0123456789abcdef"


def _cat(*parts: str) -> str:
    return "".join(parts)


def _rnd(rng: random.Random, n: int, alphabet: str = B62) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _mixed(rng: random.Random, n: int, alphabet: str = B62) -> str:
    """Random, with a digit, an upper-case and a lower-case letter guaranteed (when the alphabet has them)."""
    chars = [rng.choice(alphabet) for _ in range(n)]
    slots = rng.sample(range(n), 3)
    for slot, pool in zip(slots, (DIGITS, UPPER, LOWER)):
        if any(c in alphabet for c in pool):
            chars[slot] = rng.choice([c for c in pool if c in alphabet])
    return "".join(chars)


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


@dataclass(frozen=True)
class Fake:
    text: str          # a realistic line (or block) containing the secret
    secret: str        # what must not survive
    kinds: tuple[str, ...]   # the kinds a detector may legitimately name for it


def _fake(text: str, secret: str, *kinds: str) -> Fake:
    assert secret in text
    return Fake(text, secret, tuple(kinds))


# ---------------------------------------------------------------------------
# One builder per kind of secret.  Each takes a ``random.Random`` and returns a Fake.
# ---------------------------------------------------------------------------
def aws_access_key(rng):
    key = _cat("AK", "IA", _rnd(rng, 16, UPPER + DIGITS))
    return _fake(f"found {key} in the build log", key, "aws_access_key")


def aws_secret_key(rng):
    value = _mixed(rng, 40, B64STD)
    return _fake(_cat("aws_secret_", "access_key = ", value), value, "secret_assignment", "aws_secret_key")


def google_api_key(rng):
    key = _cat("AI", "za", _rnd(rng, 35, B64URL))
    return _fake(f"maps key: {key}", key, "google_api_key")


def gcp_oauth_token(rng):
    token = _cat("ya", "29.", _rnd(rng, 60, B64URL))
    return _fake(f"got {token} back", token, "gcp_oauth_token")


def github_token(rng):
    token = _cat("gh", "p_", _rnd(rng, 36))
    return _fake(f"remote: {token} was used", token, "github_token")


def github_fine_grained(rng):
    token = _cat("github", "_pat_", _rnd(rng, 22), "_", _rnd(rng, 59))
    return _fake(f"using {token} for the clone", token, "github_token")


def gitlab_token(rng):
    token = _cat("gl", "pat-", _rnd(rng, 20, B64URL))
    return _fake(f"CI used {token}", token, "gitlab_token")


def openai_style_key(rng):
    key = _cat("s", "k-", _mixed(rng, 48))
    return _fake(f"the client sent {key} upstream", key, "api_key")


def anthropic_style_key(rng):
    key = _cat("s", "k-", "ant-", "api03-", _mixed(rng, 60, B64URL))
    return _fake(f"the client sent {key} upstream", key, "api_key")


def stripe_key(rng):
    key = _cat("s", "k_live_", _rnd(rng, 24))
    return _fake(f"billing={key}", key, "stripe_key")


def slack_token(rng):
    token = _cat("xo", "xb-", _rnd(rng, 12, DIGITS), "-", _rnd(rng, 12, DIGITS), "-", _rnd(rng, 24))
    return _fake(f"slack says {token}", token, "slack_token")


def slack_webhook(rng):
    url = _cat("https://hooks.", "slack.com/services/T", _rnd(rng, 8, UPPER + DIGITS), "/B", _rnd(rng, 8, UPPER + DIGITS),
               "/", _rnd(rng, 24))
    return _fake(f"notify -> {url}", url, "slack_webhook")


def npm_token(rng):
    token = _cat("np", "m_", _rnd(rng, 36))
    return _fake(f"registry auth {token}", token, "npm_token")


def pypi_token(rng):
    token = _cat("py", "pi-", _rnd(rng, 80, B64URL))
    return _fake(f"upload with {token}", token, "pypi_token")


def sendgrid_key(rng):
    key = _cat("S", "G.", _rnd(rng, 22, B64URL), ".", _rnd(rng, 43, B64URL))
    return _fake(f"mail via {key}", key, "sendgrid_key")


def huggingface_token(rng):
    token = _cat("h", "f_", _rnd(rng, 34))
    return _fake(f"hub login {token}", token, "huggingface_token")


def jwt(rng):
    token = ".".join([_b64url({"alg": "HS256", "typ": "JWT"}), _b64url({"sub": _rnd(rng, 8), "n": rng.randint(1, 99)}),
                      _rnd(rng, 43, B64URL)])
    return _fake(f"cookie session={token}; path=/", token, "jwt")


def private_key_block(rng):
    head = _cat("-----BEGIN ", "OPENSSH PRIVATE", " KEY-----")
    foot = _cat("-----END ", "OPENSSH PRIVATE", " KEY-----")
    lines = [_rnd(rng, 64, B64STD) for _ in range(3)]
    return _fake("\n".join(["before the key", head, *lines, foot, "after the key"]), lines[1], "private_key")


def bearer_header(rng):
    value = _mixed(rng, 40, B64URL)
    return _fake(_cat("curl -H 'Authorization: ", "Bearer ", value, "' https://api.example.test/v1/items"), value,
                 "bearer_token", "authorization_header")


def basic_header(rng):
    value = base64.b64encode(_cat("svc:", _rnd(rng, 14)).encode()).decode()
    return _fake(_cat("Authorization: ", "Basic ", value), value, "authorization_header")


def password_env(rng):
    value = _mixed(rng, 14)
    return _fake(_cat("DB_PASS", "WORD=", value), value, "password_assignment")


def password_yaml(rng):
    value = _mixed(rng, 12)
    return _fake(_cat("  pass", "word: '", value, "'"), value, "password_assignment")


def password_json(rng):
    value = _mixed(rng, 12)
    return _fake(_cat('{"user": "admin", "pass', 'word": "', value, '"}'), value, "password_assignment")


def password_code(rng):
    value = _mixed(rng, 12)
    return _fake(_cat("conn = connect(host, user, pass", 'word="', value, '")'), value, "password_assignment")


def secret_assignment_env(rng):
    value = _mixed(rng, 32, B64URL)
    return _fake(_cat("export API_TO", "KEN=", value), value, "secret_assignment")


def secret_assignment_ini(rng):
    value = _mixed(rng, 24, HEX + "g")
    return _fake(_cat("client_sec", "ret = ", value), value, "secret_assignment")


def secret_assignment_cli(rng):
    value = _mixed(rng, 30)
    return _fake(_cat("tool login --pass", "word ", value, " --host example.test"), value, "password_assignment")


def url_credentials(rng):
    password = _mixed(rng, 16)
    return _fake(_cat("DATABASE_", "URL=postgres://svc:", password, "@db.internal:5432/app"), password,
                 "url_credentials", "secret_assignment")


def azure_account_key(rng):
    value = _mixed(rng, 64, B64STD)
    return _fake(_cat("DefaultEndpointsProtocol=https;AccountName=acct;Account", "Key=", value, "==;EndpointSuffix=x"),
                 value, "azure_key")


def high_entropy_near_keyword(rng):
    value = _mixed(rng, 36, B64URL)
    return _fake(f"the internal signing key is now {value} (rotated)", value, "high_entropy")


BUILDERS: dict[str, Callable[[random.Random], Fake]] = {
    f.__name__: f for f in (
        aws_access_key, aws_secret_key, google_api_key, gcp_oauth_token, github_token, github_fine_grained,
        gitlab_token, openai_style_key, anthropic_style_key, stripe_key, slack_token, slack_webhook, npm_token,
        pypi_token, sendgrid_key, huggingface_token, jwt, private_key_block, bearer_header, basic_header, password_env,
        password_yaml, password_json, password_code, secret_assignment_env, secret_assignment_ini,
        secret_assignment_cli, url_credentials, azure_account_key, high_entropy_near_keyword)
}


def make(name: str, seed: int = 0) -> Fake:
    return BUILDERS[name](random.Random(f"{name}:{seed}"))


def leaked(output: str, secret: str, window: int = 8) -> bool:
    """True when the secret, or any ``window`` consecutive characters of it, appears in ``output``."""
    if secret in output:
        return True
    if len(secret) <= window:
        return False
    return any(secret[i:i + window] in output for i in range(len(secret) - window + 1))


# ---------------------------------------------------------------------------
# Ordinary developer text for the false-positive budget
# ---------------------------------------------------------------------------
def ordinary_corpus(seed: int = 7) -> dict[str, list[str]]:
    """Categories of text that must pass through (almost) untouched.  Deterministic for a seed."""
    rng = random.Random(seed)
    corpus: dict[str, list[str]] = {}

    corpus["git_hashes"] = [hashlib.sha1(str(i).encode()).hexdigest() for i in range(60)]
    corpus["git_log"] = [f"commit {hashlib.sha1(str(i).encode()).hexdigest()}\nAuthor: A Dev <dev@example.test>\n"
                         f"Date:   Mon Jan {1 + i % 28} 10:00:00 2026 +0000\n\n    fix the token refresh test\n"
                         for i in range(20)]
    corpus["sha256_checksums"] = [f"{hashlib.sha256(str(i).encode()).hexdigest()}  dist/pkg-{i}.tar.gz" for i in range(30)]
    corpus["uuids"] = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(40)]
    corpus["uuid_near_keywords"] = [f"request id for the auth token call: {uuid.UUID(int=rng.getrandbits(128), version=4)}"
                                    for _ in range(10)]
    corpus["hash_near_keywords"] = [f"checked key {hashlib.sha256(str(i).encode()).hexdigest()} against the index"
                                    for i in range(10)]
    corpus["base64_images"] = [
        '<img alt="logo" src="data:image/png;base64,' + base64.b64encode(rng.randbytes(600)).decode() + '">'
        for _ in range(6)]
    corpus["base64_images_near_keywords"] = [
        '<img alt="token icon" src="data:image/png;base64,' + base64.b64encode(rng.randbytes(300)).decode() + '">'
        for _ in range(4)]
    corpus["lock_files"] = (
        [f'      "integrity": "sha512-{base64.b64encode(hashlib.sha512(str(i).encode()).digest()).decode()}",'
         for i in range(25)]
        + [f'checksum = "{hashlib.sha256(str(i).encode()).hexdigest()}"' for i in range(25)]
        + [f'hash = "sha256:{hashlib.sha256(str(i).encode()).hexdigest()}"' for i in range(25)])
    words = ["alpha", "beta", "gamma", "delta", "value", "item", "node", "list", "map", "call", "next", "head"]
    minified = []
    for i in range(6):
        minified.append("".join(
            f"function {rng.choice(words)}{i}(a,b){{return a.{rng.choice(words)}(b)||{rng.randint(0, 99)}}}" for _ in range(60)))
    corpus["minified_code"] = minified
    corpus["long_identifiers"] = [
        "someVeryLongCamelCaseIdentifierNameThatKeepsGoingAndGoing",
        "get_the_user_account_settings_from_the_remote_configuration_service",
        "ThisIsAnExtremelyLongClassNameForAFactoryOfFactoriesBuilder",
        "com.example.application.service.internal.AuthenticationTokenRefreshSchedulerImpl",
        "SOME_VERY_LONG_CONSTANT_NAME_FOR_THE_MAXIMUM_NUMBER_OF_RETRIES_ALLOWED",
        "tokenizer_vocabulary_merges_file_for_the_byte_pair_encoder_v2",
        "authenticateWithTheIdentityProviderUsingTheConfiguredClientCredentials",
        "test_that_the_password_reset_email_is_sent_to_the_right_user_after_confirmation",
    ]
    corpus["source_code"] = [
        "def authenticate(token: str, password: str) -> bool:",
        "    token = request.headers.get('Authorization')",
        "    password = getpass.getpass('Password: ')",
        "    password = get_password()",
        "    secret_key = os.environ['SECRET_KEY']",
        "    api_key = settings.API_KEY",
        "    api_key = config.get('api_key')",
        "    self.token = token",
        "    return hmac.compare_digest(token, expected_token)",
        "const apiKey = process.env.API_KEY;",
        "const token = localStorage.getItem('token');",
        "export const TOKEN_TTL_SECONDS = 3600;",
        "max_tokens = 4096",
        "token_type = 'bearer'",
        "password: str = Field(..., min_length=8)",
        "password: Optional[str] = None",
        "password: required",
        "password_hash = bcrypt.hashpw(raw, salt)",
        "if not password: raise ValueError('password required')",
        "user = User(name=name, email=email, password=hashed)",
        "headers = {'Authorization': f'Bearer {token}'}",
        "curl -H \"Authorization: Bearer $TOKEN\" https://api.example.test/items",
        "curl -H \"Authorization: Bearer ${API_TOKEN}\" https://api.example.test/items",
        "export GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}",
        "docker login --username me --password-stdin",
        "echo $PASSWORD | docker login --password-stdin",
        "ssh-keygen -t ed25519 -C 'dev@example.test'",
        "git clone https://github.com/example/project.git",
        "git clone https://user@github.com/example/project.git",
        "url = 'postgres://<user>:<password>@localhost:5432/app'",
        "DATABASE_URL=postgres://${DB_USER}:${DB_PASS}@db:5432/app",
        "token_url = 'https://auth.example.test/oauth2/token'",
        "secret_name: prod-db-secret-2",
        "password: <your password here>",
        "password = ********",
        "# TODO: rotate the api key before release",
        "The API key is stored in the secrets manager.",
        "keyboard layout: us; keymap: default",
        "monkey patch the donkey-kong module",
        "SELECT id, name FROM users WHERE token IS NULL;",
        "assert response.json()['access_token'] != ''",
        "tokens = tokenizer.encode(text)  # 1024 tokens max",
    ]
    corpus["logs_and_traces"] = [
        f"2026-01-{1 + i % 28:02d}T10:{i % 60:02d}:00Z INFO worker-{i % 4} processed {rng.randint(1, 500)} jobs in {rng.randint(1, 900)}ms"
        for i in range(60)
    ] + [
        '  File "/usr/lib/python3.11/site-packages/requests/adapters.py", line 486, in send',
        "Traceback (most recent call last):",
        "KeyError: 'token'",
        "ModuleNotFoundError: No module named 'auth.session_store'",
        "npm WARN deprecated request@2.88.2: request has been deprecated",
        "added 312 packages, and audited 313 packages in 4s",
        "tests/test_auth.py::test_token_refresh PASSED                      [ 42%]",
        "/home/dev/projects/service-with-a-long-name/src/service/handlers/authentication_handler.py:128",
        "C:\\Users\\dev\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
    ]
    corpus["config_files"] = [
        "[tool.pytest.ini_options]", "testpaths = [\"tests\"]", "line-length = 100", "port: 8080", "workers: 4",
        "image: registry.example.test/team/app:1.4.2", "timeout_seconds = 30", "log_level = \"info\"",
        "key_bindings: vim", "sort_keys: true", "primary_key: id", "foreign_key = 'user_id'",
    ]
    return corpus
