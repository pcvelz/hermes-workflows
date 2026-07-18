#!/usr/bin/env bash
# check-example-completeness.sh
#
# Enforces the .example <-> real-twin invariant:
#   For every shipped *.example file, its real twin (with the .example marker
#   stripped) MUST be git-ignored — so a user's real config/secret can never be
#   committed, and every ignored path has a documenting template in the repo.
#
# Run from anywhere; intended for CI and as a pre-release gate. Exits non-zero
# on any unprotected twin.
set -euo pipefail

# Resolve repo root relative to this script (tests/static/<this>).
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

fail=0
count=0
while IFS= read -r ex; do
  count=$((count + 1))
  # Strip the first ".example" occurrence — handles both the suffix form
  # (config.yaml.example) and the infix form (llm-providers.example.yaml).
  twin="$(printf '%s' "$ex" | sed -E 's/\.example//')"
  if git check-ignore -q "$twin"; then
    printf 'ok    %s\n' "$twin"
  else
    printf 'LEAK  %s   (documented by %s) is NOT git-ignored\n' "$twin" "$ex" >&2
    fail=1
  fi
done < <(find . -name '*.example*' -not -path './.git/*' | sed 's|^\./||' | sort)

printf -- '---\nchecked %s templates\n' "$count"
if [ "$fail" -ne 0 ]; then
  printf 'FAIL: a real twin is not git-ignored — a real file could leak into git.\n' >&2
  exit 1
fi
printf 'PASS: every .example has its real twin git-ignored.\n'
