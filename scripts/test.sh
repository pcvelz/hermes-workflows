#!/usr/bin/env bash
# =============================================================================
# scripts/test.sh — hermes-workflows test runner.
#
# Three layers, each progressively closer to the live system:
#   static  — syntax/validity/link checks; no network, no model, no live install.
#   smoke   — LLM backend reachability, hermes-bridge /health, dispatcher unit tests.
#   e2e     — GATED (HERMES_WF_E2E=1) isolated one-shot against real hermes-agent.
#
# Usage:
#   bash scripts/test.sh static | smoke | e2e | all
#
# Exit status: non-zero if ANY layer reports a [FAIL]. Missing OPTIONAL tools
# (docker, plutil, jq) are reported as [SKIP], never [FAIL].
#
# Portable: macOS + Linux. No /Users/* hard-coding. All paths derived from the
# repo root (this script's location), all endpoints/venv overridable via env.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export REPO_ROOT
# shellcheck source=../tests/lib/common.sh
. "$REPO_ROOT/tests/lib/common.sh"

usage() {
  cat <<EOF
Usage: bash scripts/test.sh <subcommand>

Subcommands:
  static   Syntax/validity/link checks (offline, safe).
  smoke    LLM backend reachability + bridge /health + dispatcher unit tests.
  e2e      Isolated one-shot against the real hermes-agent (GATED:
           requires HERMES_WF_E2E=1, otherwise skipped).
  all      Run static, then smoke, then e2e (e2e self-gates).

Exit non-zero if any check FAILs. Missing optional tools are SKIPs.
EOF
}

# Run a sub-script in the CURRENT shell (source) so the PASS/FAIL/SKIP counters
# accumulate into one grand total. Each sub-script returns non-zero on FAIL,
# which we capture but do not let abort the whole run (we want all layers).
_overall_fail=0

# A gate that stops early must never report green. Every layer is counted when
# it starts and when it finishes; a sourced layer that calls `exit` never
# reaches "finished", and this trap -- which runs on ANY exit -- turns that into
# a failure. (Before this, `exit 0` inside llm.sh ended `test.sh smoke` green
# after one check; any smoke green claimed from before that fix is void.)
_layers_started=0
_layers_finished=0
_gate_check() {
  local rc=$?
  if [ "$_layers_started" -ne "$_layers_finished" ]; then
    printf '\n[FAIL] gate incomplete: %d layer(s) started, %d did not finish -- a layer ended the run early. This gate is NOT green.\n' \
      "$_layers_started" "$(( _layers_started - _layers_finished ))"
    exit 1
  fi
  exit "$rc"
}
trap _gate_check EXIT
run_layer() {  # run_layer <label> <script-path> [args...]
  local label="$1"; shift
  local script="$1"; shift
  printf '\n%s########## %s ##########%s\n' "$_C_BOLD" "$label" "$_C_RST"
  if [ ! -f "$script" ]; then
    fail "$label — runner missing: $script"
    _overall_fail=1
    return
  fi
  # Source so counters aggregate; tolerate its non-zero return.
  _layers_started=$(( _layers_started + 1 ))
  # shellcheck disable=SC1090
  . "$script" || _overall_fail=1
  _layers_finished=$(( _layers_finished + 1 ))
}

run_layer_isolated() {  # run_layer_isolated <label> <script-path>
  # For a layer that may `exit` (llm.sh does, when the backend is unreachable).
  # Sourced, that exit ended the whole gate with status 0 and every later layer
  # was silently skipped -- a green gate that had run one check. In its own
  # process it can only end itself.
  local label="$1" script="$2"
  printf '\n%s########## %s ##########%s\n' "$_C_BOLD" "$label" "$_C_RST"
  if [ ! -f "$script" ]; then
    fail "$label — runner missing: $script"; _overall_fail=1; return
  fi
  bash "$script" || { fail "$label — layer failed"; _overall_fail=1; }
}

CMD="${1:-}"
case "$CMD" in
  static)
    run_layer "STATIC" "$REPO_ROOT/tests/static/run.sh"
    run_layer "STATIC — backends" "$REPO_ROOT/tests/static/check-backends.sh"
    ;;
  smoke)
    run_layer_isolated "SMOKE — LLM" "$REPO_ROOT/tests/smoke/llm.sh"
    run_layer "SMOKE — bridge"     "$REPO_ROOT/tests/smoke/bridge.sh"
    run_layer "SMOKE — dispatcher" "$REPO_ROOT/tests/smoke/dispatcher.sh"
    run_layer "SMOKE — kanban harness" "$REPO_ROOT/tests/smoke/kanban-harness.sh"
    run_layer "SMOKE — resilience" "$REPO_ROOT/tests/smoke/resilience.sh"
    run_layer "SMOKE — board" "$REPO_ROOT/tests/smoke/board.sh"
    run_layer "SMOKE — harness contract" "$REPO_ROOT/tests/smoke/harness-contract.sh"
    run_layer "SMOKE — claude-code-bridge" "$REPO_ROOT/tests/smoke/claude-code-bridge.sh"
    ;;
  e2e)
    run_layer "E2E" "$REPO_ROOT/tests/e2e/run.sh"
    ;;
  all)
    run_layer "STATIC" "$REPO_ROOT/tests/static/run.sh"
    run_layer "STATIC — backends" "$REPO_ROOT/tests/static/check-backends.sh"
    run_layer_isolated "SMOKE — LLM" "$REPO_ROOT/tests/smoke/llm.sh"
    run_layer "SMOKE — bridge"     "$REPO_ROOT/tests/smoke/bridge.sh"
    run_layer "SMOKE — dispatcher" "$REPO_ROOT/tests/smoke/dispatcher.sh"
    run_layer "SMOKE — kanban harness" "$REPO_ROOT/tests/smoke/kanban-harness.sh"
    run_layer "SMOKE — resilience" "$REPO_ROOT/tests/smoke/resilience.sh"
    run_layer "SMOKE — board" "$REPO_ROOT/tests/smoke/board.sh"
    run_layer "SMOKE — harness contract" "$REPO_ROOT/tests/smoke/harness-contract.sh"
    run_layer "SMOKE — claude-code-bridge" "$REPO_ROOT/tests/smoke/claude-code-bridge.sh"
    run_layer "E2E" "$REPO_ROOT/tests/e2e/run.sh"
    ;;
  -h|--help|help|"")
    usage
    [ -z "$CMD" ] && exit 2 || exit 0
    ;;
  *)
    printf 'Unknown subcommand: %s\n\n' "$CMD" >&2
    usage
    exit 2
    ;;
esac

# ---------------------------------------------------------------------------
# Grand summary
# ---------------------------------------------------------------------------
printf '\n%s================ SUMMARY ================%s\n' "$_C_BOLD" "$_C_RST"
printf '%sPASS=%d%s  %sFAIL=%d%s  %sSKIP=%d%s\n' \
  "$_C_GREEN" "$PASS_COUNT" "$_C_RST" \
  "$_C_RED"   "$FAIL_COUNT" "$_C_RST" \
  "$_C_YEL"   "$SKIP_COUNT" "$_C_RST"

if [ -n "${FAILURES//[$'\n']/}" ]; then
  printf '%sFailures:%s\n' "$_C_RED" "$_C_RST"
  printf '%s' "$FAILURES" | sed '/^$/d; s/^/  - /'
fi

if [ "$FAIL_COUNT" -gt 0 ] || [ "$_overall_fail" -ne 0 ]; then
  exit 1
fi
exit 0
