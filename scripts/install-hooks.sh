#!/usr/bin/env bash
# =============================================================================
# scripts/install-hooks.sh — enable the tracked pre-commit hook.
#
# Git does not activate hooks from `.githooks/` (or any tracked dir) on its
# own — `core.hooksPath` must be set explicitly per clone. This is a one-line
# convenience wrapper around that config command.
#
# Usage:
#   bash scripts/install-hooks.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"
git config core.hooksPath .githooks

echo "core.hooksPath set to .githooks — the pre-commit hook (static checks) is now active."
echo "Bypass in an emergency with: git commit --no-verify"
