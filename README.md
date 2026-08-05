# MiniMe

## Setup

After cloning, run the one-time setup script:

```bash
./scripts/setup.sh
```

This wires `.githooks/` as this clone's git hooks path
(`core.hooksPath`) so the local pre-commit secret scan (Gitleaks) is
active — see `.githooks/pre-commit` and `.gitleaks.toml`. This step is
required per clone: `core.hooksPath` is a local git config value, not
something the repository can set for you automatically.

CI runs the full secret + static-analysis scan (Gitleaks + Semgrep,
uploaded as SARIF to GitHub's Security tab) on every push regardless —
see `.github/workflows/security-scan.yml` — but the local pre-commit hook
catches a leaked secret before it ever reaches a commit, which is worth
having too.
