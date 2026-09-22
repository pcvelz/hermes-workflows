#!/usr/bin/env bash
# =============================================================================
# tests/smoke/resilience.sh — wait budget, counting policy, escalation.
#
# Thin wrapper around tests/smoke/test_resilience.py. The Python suite runs
# against the REAL hermes-agent kanban_db and the REAL error classifier in a
# scratch HERMES_HOME (it refuses the real ~/.hermes):
#
#   * a wait-class error is retried past the old count limit WITHOUT
#     incrementing the card's failure counter;
#   * budget exhaustion escalates to a human, with the resume command;
#   * a real failure still trips the circuit breaker;
#   * the escalation message renders with the sender STUBBED — no test ever
#     opens a socket or touches a real channel.
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

section "SMOKE: resilience (wait budget, counting policy, escalation)"

_rs_venv="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}"
_rs_py="$_rs_venv/bin/python"
_rs_suite="$_self_dir/test_resilience.py"
_rs_out="$(mktemp)"

if [ ! -x "$_rs_py" ]; then
  skip "resilience — hermes-agent venv python not found: $_rs_py (set HERMES_AGENT_VENV)"
else
  "$_rs_py" "$_rs_suite" >"$_rs_out" 2>&1
  _rs_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_rs_out" | head -1)
  if [ "$_rs_rc" -eq 0 ]; then
    pass "resilience tests passed ($_n)"
  elif [ "$_rs_rc" -eq 77 ]; then
    skip "resilience — $(grep -m1 '^SKIP' "$_rs_out")"
  else
    fail "resilience tests FAILED — $(tail -20 "$_rs_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_rs_out"

section "resilience summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
