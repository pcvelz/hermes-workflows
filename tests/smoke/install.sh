#!/usr/bin/env bash
# =============================================================================
# tests/smoke/install.sh — scripts/install.py: one command to a running loop.
#
# Thin wrapper around tests/smoke/test_install.py, which drives the installer
# against a scratch HERMES_HOME, a scratch agents dir and a fake launchctl.
# Touches nothing on the real machine.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: installer"

_in_py="$(pick_python || true)"
_in_out="$(mktemp)"
if [ -z "$_in_py" ]; then
  skip "installer — no python interpreter found (set \$PYTHON)"
else
  "$_in_py" "$_self_dir/test_install.py" >"$_in_out" 2>&1
  _in_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_in_out" | head -1)
  if [ "$_in_rc" -eq 0 ]; then
    pass "installer tests passed ($_n)"
  else
    fail "installer tests FAILED — $(tail -20 "$_in_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_in_out"

section "installer summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
