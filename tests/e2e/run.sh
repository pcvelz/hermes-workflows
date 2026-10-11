#!/usr/bin/env bash
# =============================================================================
# tests/e2e/run.sh — GATED isolated end-to-end against the real hermes-agent.
#
# This is the ONLY layer that runs the actual hermes-agent binary. It is
# DOUBLE-GATED and HARD-ISOLATED so it can NEVER disturb the live install:
#
#   GATE      : runs ONLY when HERMES_WF_E2E=1. Otherwise SKIP and exit 0.
#   ISOLATION : a fresh temp HERMES_HOME under $TMPDIR. HARD-REFUSES (non-zero
#               exit) if that path would ever resolve to the real ~/.hermes.
#   VENV      : reuses the real hermes-agent venv READ-ONLY (override with
#               HERMES_AGENT_VENV; default ~/.hermes/hermes-agent/venv). It only
#               EXECUTES the interpreter; it never writes into the venv.
#   CONFIG    : copies a profile config template into the temp HERMES_HOME,
#               renamed from .example, with the backend base_url and model id
#               substituted in from env vars.
#   ONE-SHOT  : runs exactly one non-interactive query telling the model to
#               reply with the token E2E_OK; asserts the output contains it.
#   CLEANUP   : the temp HERMES_HOME is ALWAYS removed (trap EXIT).
#   TIMEOUT   : the whole one-shot is wrapped in a hard ceiling (default 180s).
#
# It does NOT use --replace, does NOT start a gateway, does NOT touch launchd,
# and never reads or writes the live kanban.db / state.db / config.yaml.
#
# Env:
#   HERMES_WF_E2E         must be "1" to run (the gate).
#   HERMES_AGENT_VENV     venv to reuse read-only (default ~/.hermes/hermes-agent/venv)
#   LLM_MODEL             model id to use (default <your-model-alias> — a placeholder stub).
#                         Override to your local model alias for a dog-food run
#                         (e.g. LLM_MODEL=my-local-alias LLM_BASE_URL_HOST=http://127.0.0.1:8001)
#   LLM_BASE_URL_HOST     backend base without /v1 suffix (default http://127.0.0.1:<PORT>)
#   HERMES_WF_E2E_TIMEOUT timeout seconds for the one-shot (default 180)
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "E2E: isolated hermes-agent one-shot"

# --- GATE -------------------------------------------------------------------
if [ "${HERMES_WF_E2E:-0}" != "1" ]; then
  skip "e2e gated off — set HERMES_WF_E2E=1 to run the isolated end-to-end test."
  info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
  exit 0
fi

MODEL="${LLM_MODEL:-<your-model-alias>}"
BASE_HOST="${LLM_BASE_URL_HOST:-http://127.0.0.1:<PORT>}"
E2E_TIMEOUT="${HERMES_WF_E2E_TIMEOUT:-180}"
VENV="${HERMES_AGENT_VENV:-$HOME/.hermes/hermes-agent/venv}"
VENV_PY="$VENV/bin/python"

# Template profile config to seed the isolated home from.
PROFILE_TEMPLATE="$REPO_ROOT/config/profiles/coding/config.yaml.example"

# --- Pre-flight -------------------------------------------------------------
if [ ! -x "$VENV_PY" ]; then
  fail "e2e — hermes-agent venv python not found/executable: $VENV_PY (set HERMES_AGENT_VENV)"
  info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"; exit 1
fi
if [ ! -f "$PROFILE_TEMPLATE" ]; then
  fail "e2e — profile template missing: $PROFILE_TEMPLATE"
  info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"; exit 1
fi

# --- Build & HARD-VALIDATE the isolated HERMES_HOME --------------------------
TMP_HOME="$(mktemp -d "${TMPDIR:-/tmp}/hermes-wf-e2e.XXXXXX")"

# Resolve both the temp home and the real ~/.hermes to absolute, symlink-free
# paths and HARD-REFUSE if they collide. This is the core safety invariant.
_abspath() { (cd "$1" 2>/dev/null && pwd -P) || echo ""; }
REAL_HERMES="$(_abspath "$HOME/.hermes")"
TMP_HOME_ABS="$(_abspath "$TMP_HOME")"

abort() {  # abort <msg> — clean up and exit non-zero.
  printf '%s[FATAL]%s %s\n' "$_C_RED" "$_C_RST" "$*" >&2
  [ -n "${TMP_HOME:-}" ] && rm -rf "$TMP_HOME" 2>/dev/null || true
  exit 3
}

[ -z "$TMP_HOME_ABS" ] && abort "could not resolve temp HERMES_HOME ($TMP_HOME)"
case "$TMP_HOME_ABS" in
  "$REAL_HERMES"|"$REAL_HERMES"/*)
    abort "REFUSING: temp HERMES_HOME ($TMP_HOME_ABS) resolves inside real ~/.hermes ($REAL_HERMES)." ;;
esac
if [ -n "$REAL_HERMES" ] && [ "$TMP_HOME_ABS" = "$REAL_HERMES" ]; then
  abort "REFUSING: temp HERMES_HOME equals real ~/.hermes."
fi
# Also require the temp home to live under a recognized temp root.
case "$TMP_HOME_ABS" in
  /tmp/*|/var/folders/*|"${TMPDIR%/}"/*|/private/var/folders/*) : ;;
  *) abort "REFUSING: temp HERMES_HOME ($TMP_HOME_ABS) is not under a temp root." ;;
esac

# --- Cleanup trap (ALWAYS removes the temp home) -----------------------------
cleanup() {
  case "$TMP_HOME_ABS" in
    /tmp/hermes-wf-e2e.*|/var/folders/*/hermes-wf-e2e.*|/private/var/folders/*/hermes-wf-e2e.*|"${TMPDIR%/}"/hermes-wf-e2e.*)
      rm -rf "$TMP_HOME" 2>/dev/null || true ;;
    *) : ;;  # never rm anything outside the expected pattern
  esac
}
trap cleanup EXIT INT TERM

info "Isolated HERMES_HOME = $TMP_HOME_ABS"
info "Reusing venv (read-only) = $VENV"
info "Model = $MODEL   base = $BASE_HOST   timeout = ${E2E_TIMEOUT}s"

# --- Seed config.yaml from the profile template (placeholders -> real) -------
mkdir -p "$TMP_HOME/logs"
CONFIG_DST="$TMP_HOME/config.yaml"
# Substitute the model placeholder and force the local base_url. Using a
# python rewrite keeps it robust regardless of sed dialect differences.
"$VENV_PY" - "$PROFILE_TEMPLATE" "$CONFIG_DST" "$MODEL" "$BASE_HOST" <<'PYEOF'
import sys
src, dst, model, base = sys.argv[1:5]
text = open(src).read()
text = text.replace("<YOUR_MODEL_NAME>", model)
# Rewrite all base_url lines in the config to the configured backend host.
out_lines = []
for line in text.splitlines():
    stripped = line.lstrip()
    if stripped.startswith("base_url:"):
        indent = line[: len(line) - len(stripped)]
        out_lines.append(f"{indent}base_url: {base}")  # override to configured backend
    else:
        out_lines.append(line)
open(dst, "w").write("\n".join(out_lines) + "\n")
PYEOF
info "Seeded $CONFIG_DST (renamed from .example)"

# --- Run ONE one-shot, hard-wrapped in a timeout -----------------------------
PROMPT='Output exactly the token E2E_OK and nothing else. Do not call any tools.'
OUT_FILE="$TMP_HOME/oneshot.out"

# Cross-platform hard timeout: prefer GNU/coreutils timeout, else a watchdog.
_run_with_timeout() {  # _run_with_timeout <secs> <cmd...>
  local secs="$1"; shift
  if have timeout; then
    timeout -k 10 "$secs" "$@"; return $?
  elif have gtimeout; then
    gtimeout -k 10 "$secs" "$@"; return $?
  fi
  # Fallback watchdog.
  "$@" &
  local cmd_pid=$!
  ( sleep "$secs"; kill -TERM "$cmd_pid" 2>/dev/null; sleep 5; kill -KILL "$cmd_pid" 2>/dev/null ) &
  local wd_pid=$!
  wait "$cmd_pid"; local rc=$?
  kill "$wd_pid" 2>/dev/null || true
  return $rc
}

info "Running one-shot..."
_rc=0
HERMES_HOME="$TMP_HOME" \
  _run_with_timeout "$E2E_TIMEOUT" \
  "$VENV_PY" -m hermes_cli.main chat \
    -q "$PROMPT" \
    -Q \
    -m "$MODEL" \
    --ignore-rules \
    >"$OUT_FILE" 2>&1 || _rc=$?

# --- Assert E2E_OK in the output --------------------------------------------
if [ "$_rc" -eq 124 ] || [ "$_rc" -eq 137 ]; then
  fail "e2e one-shot timed out after ${E2E_TIMEOUT}s (rc=$_rc). Tail: $(tail -5 "$OUT_FILE" | tr '\n' ' ')"
elif grep -q "E2E_OK" "$OUT_FILE"; then
  pass "e2e one-shot returned E2E_OK (rc=$_rc)"
else
  fail "e2e one-shot did NOT return E2E_OK (rc=$_rc). Output tail: $(tail -10 "$OUT_FILE" | tr '\n' ' ')"
fi

section "e2e summary"
info "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
