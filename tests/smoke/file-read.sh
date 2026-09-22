#!/usr/bin/env bash
# =============================================================================
# tests/smoke/file-read.sh — the file_read toolset: reading without writing.
#
# Thin wrapper around tests/smoke/test_file_read.py, which runs against the REAL
# hermes-agent in a scratch HERMES_HOME: a profile granted file_read opens, lists
# and searches; every write, create, move and delete is refused; scripts stay
# closed; a profile with no grant reads nothing; the planner holds no writer.
#
#   HERMES_AGENT_VENV  venv to use          (default ~/.hermes/hermes-agent/venv)
#   HERMES_AGENT_SRC   agent source checkout (default ~/.hermes/hermes-agent)
# Missing agent/venv -> SKIP (environment condition, not a repo defect).
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: file_read toolset"

_fr_venv="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}"
_fr_py="$_fr_venv/bin/python"
_fr_suite="$_self_dir/test_file_read.py"
_fr_out="$(mktemp)"

if [ ! -x "$_fr_py" ]; then
  skip "file_read — hermes-agent venv python not found: $_fr_py (set HERMES_AGENT_VENV)"
else
  "$_fr_py" "$_fr_suite" >"$_fr_out" 2>&1
  _fr_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_fr_out" | head -1)
  if [ "$_fr_rc" -eq 0 ]; then
    pass "file_read tests passed ($_n)"
  elif [ "$_fr_rc" -eq 77 ]; then
    skip "file_read — $(grep -m1 '^SKIP' "$_fr_out")"
  else
    fail "file_read tests FAILED — $(tail -20 "$_fr_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_fr_out"

section "file_read summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
