# @user-gated
#!/usr/bin/env bash
# =============================================================================
# tests/smoke/harness-contract.sh — the harness contract (docs/harness-contract.md).
#
# Thin wrapper around tests/smoke/test_harness_contract.py: every rule of the
# contract as a test, each asserting D11's three things -- the agent received
# the refusal, the board did not move, the refusal is on disk. Runs against the
# real hermes-agent kanban_db in a scratch HOME and HERMES_HOME.
#
# Only the user changes this file or the suite it runs. Nothing in it is ever
# skipped, xfailed or narrowed.
#
#   HERMES_AGENT_VENV  venv to use          (default ~/.hermes/hermes-agent/venv)
#   HERMES_AGENT_SRC   agent source checkout (default ~/.hermes/hermes-agent)
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: harness contract (a hard gate blocks; it does not report)"

_hc_py="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}/bin/python"
_hc_out="$(mktemp)"

if [ ! -x "$_hc_py" ]; then
  skip "harness contract — hermes-agent venv python not found: $_hc_py (set HERMES_AGENT_VENV)"
else
  "$_hc_py" "$_self_dir/test_harness_contract.py" >"$_hc_out" 2>&1
  _hc_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_hc_out" | head -1)
  if [ "$_hc_rc" -eq 0 ]; then
    pass "harness contract passed ($_n)"
  elif [ "$_hc_rc" -eq 77 ]; then
    skip "harness contract — $(grep -m1 '^SKIP' "$_hc_out")"
  else
    fail "harness contract FAILED — $(tail -20 "$_hc_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_hc_out"

section "harness contract summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
