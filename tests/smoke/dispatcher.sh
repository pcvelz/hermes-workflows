#!/usr/bin/env bash
# =============================================================================
# tests/smoke/dispatcher.sh — run the dispatcher pure-function unit tests.
#
# Thin wrapper around tests/smoke/test_dispatcher.py so it participates in the
# [PASS]/[FAIL]/[SKIP] tally with the other smoke checks. The Python tests
# exercise only the I/O-free logic in hooks/per-profile-dispatcher/handler.py.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: dispatcher pure-function unit tests"

PY="$(pick_python || true)"
SUITE="$_self_dir/test_dispatcher.py"

if [ -z "$PY" ]; then
  skip "dispatcher unit tests — no python interpreter"
elif [ ! -f "$SUITE" ]; then
  fail "dispatcher unit tests — suite missing: $SUITE"
else
  if "$PY" "$SUITE" >/tmp/_disp.$$ 2>&1; then
    _n=$(grep -Eo 'Ran [0-9]+ tests?' /tmp/_disp.$$ | head -1)
    pass "dispatcher unit tests passed ($_n)"
  else
    fail "dispatcher unit tests FAILED — $(tail -20 /tmp/_disp.$$ | tr '\n' ' ')"
  fi
  rm -f /tmp/_disp.$$
fi

section "dispatcher summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
