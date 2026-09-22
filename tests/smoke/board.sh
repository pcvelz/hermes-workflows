#!/usr/bin/env bash
# =============================================================================
# tests/smoke/board.sh — config/board.yaml: loader, auto_advance, auto-archive.
#
# Thin wrapper around tests/smoke/test_board.py. The Python suite loads the
# REAL config/board.yaml and drives the REAL hermes-agent kanban_db in a
# scratch HERMES_HOME (it refuses the real ~/.hermes):
#
#   * the specification loads, maps each column to its status, and reports
#     exactly the contradictions docs/board-design.md lists;
#   * a waiting card leaves by itself when what it waits on reaches review or
#     done, and the move carries its explanation — including when upstream's
#     own promoter wins the race;
#   * a finished card is archived on the clock, and nothing else ever is;
#   * none of it runs unless a board specification is passed explicitly.
#
#   HERMES_AGENT_VENV  venv to use          (default ~/.hermes/hermes-agent/venv)
#   HERMES_AGENT_SRC   agent source checkout (default ~/.hermes/hermes-agent)
# Missing agent/venv -> SKIP (environment condition, not a repo defect).
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: board (loader, auto_advance, auto-archive)"

_bd_venv="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}"
_bd_py="$_bd_venv/bin/python"
_bd_suite="$_self_dir/test_board.py"
_bd_out="$(mktemp)"

if [ ! -x "$_bd_py" ]; then
  skip "board — hermes-agent venv python not found: $_bd_py (set HERMES_AGENT_VENV)"
else
  "$_bd_py" "$_bd_suite" >"$_bd_out" 2>&1
  _bd_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_bd_out" | head -1)
  if [ "$_bd_rc" -eq 0 ]; then
    pass "board tests passed ($_n)"
  elif [ "$_bd_rc" -eq 77 ]; then
    skip "board — $(grep -m1 '^SKIP' "$_bd_out")"
  else
    fail "board tests FAILED — $(tail -20 "$_bd_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_bd_out"

section "board summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
