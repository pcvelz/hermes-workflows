#!/usr/bin/env bash
# check-no-inline-secrets.sh
#
# Fail if any config/env file carries an INLINE secret VALUE that should have
# been externalized. Catches the class a naive `api_key` grep misses:
#   - hyphenated header keys (x-api-key) and secret-shaped keys followed by an
#     opaque value >=16 chars (real API keys / tokens);
#   - known live-token prefixes (sk- / ghp_ / xox*- / ops_ / AKIA).
#
# ALLOWED (never flagged): env-var references (`name_env: MY_VAR`), empty values
# (`key: ''`), ${VAR} interpolation, and *.example templates.
#
# Usage:  bash check-no-inline-secrets.sh [PATH]           # default: repo root
#   Also run against an external tree before committing it, e.g. the private
#   deploy overlay:  bash check-no-inline-secrets.sh /path/to/hermes/deploy
#
# Exit non-zero on any finding. Intended for CI, pre-commit, and the release gate.
# Output withholds the secret value (prints file:line only).
set -uo pipefail

target="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fail=0

# (1) secret-shaped key + opaque >=16-char value. Excludes *.example and
#     *.example.<ext> (e.g. seckit-defaults.example.json), .git, and comment
#     lines; env-refs (`..._env: NAME`) never match because the value run
#     breaks at the ':' after "_env".
key_hits="$(grep -rniE \
  --include='*.yaml' --include='*.yml' --include='*.json' --include='*.env' \
  --exclude='*.example' --exclude='*.example.*' --exclude-dir='.git' \
  '((x-)?api[_-]?key|authorization|(access|refresh|auth)[_-]?token|password|client[_-]?secret|bearer)[^A-Za-z0-9]{1,4}[A-Za-z0-9+/._-]{16,}' \
  "$target" 2>/dev/null | grep -vE ':[0-9]+:[[:space:]]*#' || true)"
if [ -n "$key_hits" ]; then
  while IFS= read -r h; do
    printf '  INLINE-SECRET  %s  (value withheld)\n' "$(printf '%s' "$h" | cut -d: -f1-2)" >&2
  done <<< "$key_hits"
  fail=1
fi

# (2) known live-token prefixes anywhere
pfx_hits="$(grep -rnoE --exclude='*.example' --exclude='*.example.*' --exclude-dir='.git' \
  '(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|xox[bpas]-[A-Za-z0-9-]{10,}|ops_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})' \
  "$target" 2>/dev/null || true)"
if [ -n "$pfx_hits" ]; then
  while IFS= read -r h; do
    printf '  TOKEN-PREFIX   %s  (looks like a live token)\n' "$(printf '%s' "$h" | cut -d: -f1-2)" >&2
  done <<< "$pfx_hits"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  printf '\nFAIL: inline secret(s) found. Externalize with `name_env: MY_VAR` (value in .env / a vault) or ${MY_VAR}.\n' >&2
  exit 1
fi
printf 'PASS: no inline secrets (env-refs, empty values, ${VAR}, and *.example templates are allowed).\n'
