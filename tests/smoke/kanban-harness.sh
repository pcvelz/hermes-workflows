#!/usr/bin/env bash
# =============================================================================
# tests/smoke/kanban-harness.sh — kanban workflow harness, end to end.
#
# Thin wrapper around tests/smoke/test_kanban_harness.py. The Python suite runs
# against the REAL hermes-agent kanban_db in a scratch HERMES_HOME (it refuses
# the real ~/.hermes): every forbidden transition / side door must be refused,
# and the coder -> QA -> user -> done path must work.
#
# Needs a python that can import hermes-agent: the install's venv by default.
#   HERMES_AGENT_VENV  venv to use          (default ~/.hermes/hermes-agent/venv)
#   HERMES_AGENT_SRC   agent source checkout (default ~/.hermes/hermes-agent)
# Missing agent/venv -> SKIP (environment condition, not a repo defect).
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: kanban workflow harness"

_kh_venv="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}"
_kh_py="$_kh_venv/bin/python"
_kh_suite="$_self_dir/test_kanban_harness.py"
_kh_out="$(mktemp)"

if [ ! -x "$_kh_py" ]; then
  skip "kanban harness — hermes-agent venv python not found: $_kh_py (set HERMES_AGENT_VENV)"
else
  "$_kh_py" "$_kh_suite" >"$_kh_out" 2>&1
  _kh_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_kh_out" | head -1)
  if [ "$_kh_rc" -eq 0 ]; then
    pass "kanban harness tests passed ($_n)"
  elif [ "$_kh_rc" -eq 77 ]; then
    skip "kanban harness — $(grep -m1 '^SKIP' "$_kh_out")"
  else
    fail "kanban harness tests FAILED — $(tail -20 "$_kh_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_kh_out"

section "kanban harness summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
