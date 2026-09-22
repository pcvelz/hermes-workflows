#!/usr/bin/env bash
# =============================================================================
# tests/static/check-origin-headers.sh — every .example names where it came from.
#
# A template gets copied out of this repo into a runtime home or into another
# project, edited there, and then nobody remembers which file it was — so the
# copy drifts from the version that keeps getting fixed here. Every `.example`
# therefore carries, near its top, ONE line pointing at its own file on main:
#
#     Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/<own path>
#
# An agent that finds a copied template follows that link to check it is not
# stale, and edits the upstream version rather than forking a private one.
# The path must be the file's OWN path — a header copied from another file is
# worse than none, because it points at the wrong original.
#
# Covers both forms: `name.ext.example` and `name.example.ext`.
#
# Skipped, on purpose:
#   * JSON — JSON has no comments, and a "_source" key would change the config
#     a loader actually reads (docs/board-design.md D7).
#
# The header must appear within the first 8 lines: after a shebang (which must
# stay on line 1), an <?xml ...?> / <!DOCTYPE> prologue, or a title banner.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

cd "$REPO_ROOT"

section "origin headers — every .example links its source of truth on main"

_URL_BASE="https://github.com/pcvelz/hermes-workflows/blob/main/"
_checked=0
_missing=0

while IFS= read -r f; do
  [ -z "$f" ] && continue
  rel="${f#./}"
  case "$rel" in
    *.json.example|*.example.json) continue ;;   # D7: JSON cannot carry a comment
  esac
  _checked=$((_checked + 1))
  head_text="$(head -n 8 "$f")"
  if ! printf '%s\n' "$head_text" | grep -qF "Source of truth: ${_URL_BASE}"; then
    fail "no one-line 'Source of truth: ${_URL_BASE}…' in the first 8 lines: $rel"
    _missing=$((_missing + 1))
    continue
  fi
  if ! printf '%s\n' "$head_text" | grep -qF "Source of truth: ${_URL_BASE}${rel}"; then
    fail "origin header links another file ($rel): $(printf '%s\n' "$head_text" | grep -o 'blob/main/[^ ]*' | head -1)"
    _missing=$((_missing + 1))
    continue
  fi
  case "$rel" in
    *.sh.example|*.example.sh)
      if [ "$(head -c 2 "$f")" != "#!" ]; then
        fail "shebang is no longer on line 1: $rel"
        _missing=$((_missing + 1))
        continue
      fi ;;
  esac
done < <(find . \( -name .git -o -name __pycache__ -o -name node_modules \) -prune -o \
              -type f \( -name '*.example' -o -name '*.example.*' \) -print | sort)

if [ "$_missing" -eq 0 ]; then
  pass "origin headers — all $_checked non-JSON .example files link their own source on main"
fi

info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
