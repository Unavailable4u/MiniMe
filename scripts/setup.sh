#!/usr/bin/env bash
# scripts/setup.sh — one-time per-clone setup.
#
# Master Guide B2 note: `core.hooksPath` is a local git config value, not
# something git tracks or enforces from the repo itself — a fresh clone
# has no idea .githooks/ exists until this is run once. This script is
# the fix for that gap: run it right after cloning.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Wiring .githooks/ as this clone's git hooks path..."
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit

if command -v gitleaks &> /dev/null; then
  echo "gitleaks found on PATH — pre-commit secret scanning is active."
else
  echo "NOTE: gitleaks isn't installed yet. Install it so the pre-commit"
  echo "hook can actually run:"
  echo "  macOS:   brew install gitleaks"
  echo "  Windows: winget install --id Gitleaks.Gitleaks -e"
  echo "  Linux:   see https://github.com/gitleaks/gitleaks#installing"
  echo "(CI will still catch secrets via .github/workflows/security-scan.yml"
  echo "even if you skip this, but the local pre-commit gate needs it.)"
fi

echo "Setup complete."
