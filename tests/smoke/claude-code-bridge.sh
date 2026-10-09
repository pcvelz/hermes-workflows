#!/usr/bin/env bash
# =============================================================================
# tests/smoke/claude-code-bridge.sh — claude-code-bridge plugin: core, worker, session.
#
# Thin wrapper around the three unittest suites under tests/smoke/:
#   test_claude_code_bridge_core.py     config, spool and dispatch helpers
#   test_claude_code_bridge_worker.py   worker tick, reply posting and retry
#   test_claude_code_bridge_session.py  tmux session handling against a fake claude
# No suite starts a real claude or touches the network. Session tests need tmux.
#   PYTHON  interpreter to use (default: python3)
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: claude-code-bridge (core, worker, session)"

_cb_py="$(pick_python || true)"

if [ -z "$_cb_py" ]; then
  skip "claude-code-bridge — no python interpreter on PATH"
else
  for _cb_suite in test_claude_code_bridge_core.py test_claude_code_bridge_worker.py test_claude_code_bridge_session.py test_claude_code_bridge_mcp.py; do
    _cb_out="$(mktemp)"
    PYTHONDONTWRITEBYTECODE=1 "$_cb_py" "$_self_dir/$_cb_suite" >"$_cb_out" 2>&1
    _cb_rc=$?
    _n=$(grep -Eo 'Ran [0-9]+ tests?' "$_cb_out" | head -1)
    if [ "$_cb_rc" -eq 0 ]; then
      pass "claude-code-bridge $_cb_suite passed ($_n)"
    else
      fail "claude-code-bridge $_cb_suite FAILED — $(tail -20 "$_cb_out" | tr '\n' ' ')"
    fi
    rm -f "$_cb_out"
  done
fi

section "claude-code-bridge summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
