"""
m2c/tests/context/context_helpers.py -- Patch C12: shared helpers for the
instructions / rules / memory tests.

Two jobs:

1. Build small repositories on disk (``put``, ``make_world``).
2. Build FAKE secrets at run time.  This repository's pre-commit hook and CI
   run gitleaks, and m2c/tests/ is not on its allowlist, so no string in this
   directory may look like a real token as a literal.  Every fake below is
   assembled from short, low-entropy pieces when a test runs, so the source
   text never contains the finished secret.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path


def put(path: Path, text: str = "", *, data: bytes | None = None) -> Path:
    """Write a file (creating parents) and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_bytes(data)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


@dataclass(frozen=True)
class World:
    """A repository root, a separate M2C home, and a directory outside both."""

    root: Path
    home: Path
    outside: Path


def make_world(tmp_path: Path) -> World:
    dirs = {}
    for name in ("repo", "m2chome", "outside"):
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        dirs[name] = d.resolve()
    return World(root=dirs["repo"], home=dirs["m2chome"], outside=dirs["outside"])


def note_codes(result) -> list[str]:
    return [n.code for n in result.notes]


# ---------------------------------------------------------------------------
# Fake secrets, assembled at run time
# ---------------------------------------------------------------------------
# ``_cat`` joins its pieces when it is CALLED.  Writing ``"gh" + "p_" + "ab" * 4``
# instead would be folded by the Python compiler into one contiguous literal in
# the .pyc file (gitleaks found exactly that in __pycache__), so the finished
# token must never be a constant expression.
def _cat(*parts: str) -> str:
    return "".join(parts)


def _rep(piece: str, times: int) -> str:
    return piece * times


def _b64url(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def fake_jwt() -> str:
    return ".".join([_b64url({"alg": "none", "typ": "JWT"}), _b64url({"sub": "tester", "n": 1}), _rep("s", 43)])


def fake_github_token() -> str:
    return _cat("gh", "p_", _rep("aB3dE5gH7j", 4))


def fake_github_fine_grained() -> str:
    return _cat("github", "_pat_", _rep("x1Y2z3", 12))


def fake_sk_key() -> str:
    return _cat("s", "k-", _rep("Ab12", 10))


def fake_aws_key_id() -> str:
    return _cat("AK", "IA", "ABCDEFGH", "23456723")


def fake_slack_token() -> str:
    return _cat("xo", "xb-", _rep("1234567890-", 2), "abcdEFGHijkl")


def fake_bearer_value() -> str:
    return _rep("t0k3n", 6)


def fake_private_key_block() -> str:
    head = _cat("-----BEGIN ", "OPENSSH PRIVATE", " KEY-----")
    tail = _cat("-----END ", "OPENSSH PRIVATE", " KEY-----")
    body = "\n".join([_rep("QUJD", 16)] * 3)
    return f"{head}\n{body}\n{tail}"


def fake_password_line() -> str:
    return _cat("pass", "word = ", "S3cr3t", "Value")


def fake_url_with_credentials() -> str:
    return _cat("https://deploy:", "pw", "123456", "@example.com/repo.git")


def fake_env_lines() -> str:
    return _cat("DATABASE_", "URL=postgres://db.internal/app\nDEBUG", "_LEVEL=3")


def fake_google_key() -> str:
    return _cat("AI", "za", "Sy", _rep("A1b2C3d4E5", 3), "F6g7H")


def fake_stripe_key() -> str:
    return _cat("sk", "_live_", _rep("a1B2c3D4", 4))


def fake_gitlab_token() -> str:
    return _cat("gl", "pat-", _rep("k9J8h7G6f5", 2))
