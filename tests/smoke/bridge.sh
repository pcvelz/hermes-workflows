#!/usr/bin/env bash
# =============================================================================
# tests/smoke/bridge.sh — hermes-bridge.py /health liveness.
#
# Proves the host control bridge boots and answers its no-auth liveness probe:
#   1. Pick a free ephemeral 127.0.0.1 port.
#   2. Start scripts/bridge/hermes-bridge.py --bind 127.0.0.1 --port <ephemeral>
#      with an ISOLATED HERMES_HOME under $TMPDIR (so it never writes ~/.hermes).
#   3. Poll GET /health until HTTP 200 (or timeout).
#   4. Assert the JSON body reports status=ok and service=hermes-bridge.
#   5. ALWAYS stop the bridge process and clean up the temp HERMES_HOME.
#
# This NEVER touches the live install: HERMES_HOME is overridden to a temp dir,
# and the bridge only binds loopback on an unprivileged ephemeral port.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "SMOKE: hermes-bridge /health"

PY="$(pick_python || true)"
BRIDGE="$REPO_ROOT/scripts/bridge/hermes-bridge.py"

if [ -z "$PY" ]; then
  skip "bridge — no python interpreter"; info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
  [ "$FAIL_COUNT" -eq 0 ]; exit $?
fi
if ! have curl; then
  skip "bridge — curl not installed"; info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
  [ "$FAIL_COUNT" -eq 0 ]; exit $?
fi
if [ ! -f "$BRIDGE" ]; then
  fail "bridge — script missing: $BRIDGE"; info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
  [ "$FAIL_COUNT" -eq 0 ]; exit $?
fi

# --- Pick a free ephemeral loopback port via python --------------------------
PORT="$("$PY" - <<'PYEOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF
)"
if ! [ "$PORT" -gt 0 ] 2>/dev/null; then
  fail "bridge — could not allocate ephemeral port"; info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
  [ "$FAIL_COUNT" -eq 0 ]; exit $?
fi

# --- Isolated HERMES_HOME so the bridge never logs into ~/.hermes ------------
TMP_HOME="$(mktemp -d "${TMPDIR:-/tmp}/hermes-bridge-smoke.XXXXXX")"

BRIDGE_PID=""
cleanup() {
  if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill "$BRIDGE_PID" 2>/dev/null || true
    # Give it a moment, then hard-kill if still alive.
    for _ in 1 2 3 4 5; do kill -0 "$BRIDGE_PID" 2>/dev/null || break; sleep 0.2; done
    kill -9 "$BRIDGE_PID" 2>/dev/null || true
  fi
  # Safety: only remove temp dirs under TMPDIR/tmp.
  case "$TMP_HOME" in
    "${TMPDIR:-/tmp}"/hermes-bridge-smoke.*|/tmp/hermes-bridge-smoke.*|/var/folders/*/hermes-bridge-smoke.*)
      rm -rf "$TMP_HOME" ;;
  esac
}
trap cleanup EXIT INT TERM

info "Starting bridge on 127.0.0.1:$PORT (HERMES_HOME=$TMP_HOME)"
HERMES_HOME="$TMP_HOME" "$PY" "$BRIDGE" --bind 127.0.0.1 --port "$PORT" \
  >"$TMP_HOME/bridge.out" 2>&1 &
BRIDGE_PID=$!

# --- Poll /health until 200 or timeout (~10s) --------------------------------
HEALTH_URL="http://127.0.0.1:$PORT/health"
_ok=0; _code=""; _body=""
for _ in $(seq 1 50); do
  if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then break; fi   # process died early
  _code=$(curl -s -o "$TMP_HOME/health.body" -w '%{http_code}' --max-time 3 "$HEALTH_URL" 2>/dev/null || echo "000")
  if [ "$_code" = "200" ]; then _ok=1; break; fi
  sleep 0.2
done
[ -f "$TMP_HOME/health.body" ] && _body="$(cat "$TMP_HOME/health.body")"

if [ "$_ok" -ne 1 ]; then
  fail "bridge /health did not return 200 (last code=$_code). Bridge stdout: $(tr '\n' ' ' <"$TMP_HOME/bridge.out" | head -c 300)"
else
  pass "bridge /health returned HTTP 200 on port $PORT"
  if printf '%s' "$_body" | grep -q '"status"[: ]*"ok"' && \
     printf '%s' "$_body" | grep -q '"service"[: ]*"hermes-bridge"'; then
    pass "bridge /health body reports status=ok service=hermes-bridge"
  else
    fail "bridge /health body unexpected: ${_body:0:200}"
  fi
fi

# cleanup() runs on EXIT (stops bridge, removes temp home).
section "bridge summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
