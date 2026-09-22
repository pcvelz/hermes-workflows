#!/usr/bin/env bash
# =============================================================================
# tests/smoke/chain-flows.sh — a planner's card is claimed with nobody typing.
#
# Thin wrapper around tests/smoke/test_chain_flows.py, which drives the REAL
# kanban_db.dispatch_once (the gateway's embedded dispatcher tick) in a scratch
# HERMES_HOME with the live caps: claim, per-profile cap, gone-PID requeue,
# live-PID-with-silent-heartbeat kept.
#
#   HERMES_AGENT_VENV  venv to use          (default ~/.hermes/hermes-agent/venv)
#   HERMES_AGENT_SRC   agent source checkout (default ~/.hermes/hermes-agent)
# Missing agent/venv -> SKIP (environment condition, not a repo defect).
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: chain flows (dispatcher tick claims planner cards)"

_cf_venv="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}"
_cf_py="$_cf_venv/bin/python"
_cf_suite="$_self_dir/test_chain_flows.py"
_cf_out="$(mktemp)"

if [ ! -x "$_cf_py" ]; then
  skip "chain flows — hermes-agent venv python not found: $_cf_py (set HERMES_AGENT_VENV)"
else
  "$_cf_py" "$_cf_suite" >"$_cf_out" 2>&1
  _cf_rc=$?
  _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_cf_out" | head -1)
  if [ "$_cf_rc" -eq 0 ]; then
    pass "chain flows tests passed ($_n)"
  elif [ "$_cf_rc" -eq 77 ]; then
    skip "chain flows — $(grep -m1 '^SKIP' "$_cf_out")"
  else
    fail "chain flows tests FAILED — $(tail -20 "$_cf_out" | tr '\n' ' ')"
  fi
fi
rm -f "$_cf_out"

section "chain flows summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
