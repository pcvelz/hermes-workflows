# =============================================================================
# tests/lib/common.sh — shared helpers for the hermes-workflows test harness.
#
# Sourced by every tests/<layer>/*.sh script. Provides:
#   - REPO_ROOT resolution (portable, no /Users/* hard-coding)
#   - [PASS]/[FAIL]/[SKIP] reporting with running counters
#   - Colorized output that degrades to plain text when not a TTY
#   - Tool-presence detection that maps "missing optional tool" -> SKIP
#
# Counters are exported into the environment so a parent runner can aggregate
# them across multiple sourced scripts in a single shell. Each script also
# prints its own per-file tally.
# =============================================================================

# --- REPO_ROOT: two levels up from this file (tests/lib/ -> repo root) -------
if [ -z "${REPO_ROOT:-}" ]; then
  _common_self="${BASH_SOURCE[0]}"
  _common_dir="$(cd "$(dirname "$_common_self")" && pwd)"
  REPO_ROOT="$(cd "$_common_dir/../.." && pwd)"
  export REPO_ROOT
fi

# --- Counters (aggregate across sourced scripts) ----------------------------
: "${PASS_COUNT:=0}"
: "${FAIL_COUNT:=0}"
: "${SKIP_COUNT:=0}"
export PASS_COUNT FAIL_COUNT SKIP_COUNT

# Accumulated failure/skip descriptions (newline-separated) for the summary.
: "${FAILURES:=}"
: "${SKIPS:=}"
export FAILURES SKIPS

# --- Color (TTY only) -------------------------------------------------------
if [ -t 1 ]; then
  _C_GREEN=$'\033[0;32m'; _C_RED=$'\033[0;31m'; _C_YEL=$'\033[0;33m'
  _C_CYAN=$'\033[0;36m';  _C_BOLD=$'\033[1m';   _C_RST=$'\033[0m'
else
  _C_GREEN=''; _C_RED=''; _C_YEL=''; _C_CYAN=''; _C_BOLD=''; _C_RST=''
fi

pass() {
  PASS_COUNT=$((PASS_COUNT + 1)); export PASS_COUNT
  printf '%s[PASS]%s %s\n' "$_C_GREEN" "$_C_RST" "$*"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1)); export FAIL_COUNT
  FAILURES="${FAILURES}${*}"$'\n'; export FAILURES
  printf '%s[FAIL]%s %s\n' "$_C_RED" "$_C_RST" "$*"
}

skip() {
  SKIP_COUNT=$((SKIP_COUNT + 1)); export SKIP_COUNT
  SKIPS="${SKIPS}${*}"$'\n'; export SKIPS
  printf '%s[SKIP]%s %s\n' "$_C_YEL" "$_C_RST" "$*"
}

info() { printf '%s[INFO]%s %s\n' "$_C_CYAN" "$_C_RST" "$*"; }

section() { printf '\n%s== %s ==%s\n' "$_C_BOLD" "$*" "$_C_RST"; }

# have <cmd> — true if command exists on PATH.
have() { command -v "$1" >/dev/null 2>&1; }

# os_is_linux / os_is_macos — platform detection for plutil etc.
os_is_macos() { [ "$(uname -s)" = "Darwin" ]; }
os_is_linux() { [ "$(uname -s)" = "Linux" ]; }

# A python interpreter to use for py_compile / yaml / json checks.
# Honors $PYTHON override; else python3; else python.
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return 0; fi
  if have python3; then echo python3; return 0; fi
  if have python;  then echo python;  return 0; fi
  echo ""; return 1
}
