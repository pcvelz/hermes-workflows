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

# The live motor: upstream's dispatch_once, as the gateway ticks it. Own
# process, so its counters stay its own; its exit status is the verdict.
if bash "$_self_dir/chain-flows.sh" >/tmp/_chain.$$ 2>&1; then
  pass "chain flows: $(grep -m1 -E '^\[(PASS|SKIP)\] chain flows' /tmp/_chain.$$)"
else
  fail "chain flows FAILED — $(tail -20 /tmp/_chain.$$ | tr '\n' ' ')"
fi
rm -f /tmp/_chain.$$

# What starts the motor: scripts/install.py registers the gateway the
# dispatcher runs in. Own process, like the chain flows above.
if bash "$_self_dir/install.sh" >/tmp/_inst.$$ 2>&1; then
  pass "installer: $(grep -m1 -E '^\[(PASS|SKIP)\] installer' /tmp/_inst.$$)"
else
  fail "installer FAILED — $(tail -20 /tmp/_inst.$$ | tr '\n' ' ')"
fi
rm -f /tmp/_inst.$$

section "dispatcher summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
