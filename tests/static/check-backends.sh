#!/usr/bin/env bash
# =============================================================================
# tests/static/check-backends.sh — STATIC layer for scripts/check-backends.py.
#
# Offline. Builds fixture backends.yaml + profile configs in a temp dir and
# checks the checker's verdicts (match / mismatch / invalid). Then, only when a
# real config/backends.yaml exists (CI has just the .example), checks the real
# files. Sourced by scripts/test.sh, so it must not call `exit` or set a trap.
# =============================================================================
set -uo pipefail

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$_self_dir/../lib/common.sh"

section "check-backends — backends.yaml vs profile configs"

CHECKER="$REPO_ROOT/scripts/check-backends.py"
PY="$(pick_python || true)"

if [ -z "$PY" ]; then
  skip "check-backends — no python interpreter"
elif ! "$PY" -c "import yaml" 2>/dev/null; then
  skip "check-backends — PyYAML not importable (pip install pyyaml)"
elif [ ! -f "$CHECKER" ]; then
  fail "check-backends — checker missing: $CHECKER"
else
  T="$(mktemp -d)"

  # --- fixtures -------------------------------------------------------------
  cat > "$T/backends.yaml" <<'EOF'
tiers:
  priority:   http://127.0.0.1:8002
  default:    http://127.0.0.1:8001
  background: http://127.0.0.1:8003
profiles:
  alpha:
    model: model-a
    tier: background
  beta:
    model: model-b
    remote: https://api.example.com
  gamma:
    model: model-g
    tier: priority
    delegation: { model: model-d, tier: default }
  delta:
    model: model-x
    tier: default
EOF
  mkdir -p "$T/match/alpha" "$T/match/beta" "$T/match/gamma"
  cat > "$T/match/alpha/config.yaml" <<'EOF'
model:
  default: model-a
  base_url: http://127.0.0.1:8003/
EOF
  cat > "$T/match/beta/config.yaml" <<'EOF'
model:
  default: model-b
  base_url: https://api.example.com/v1
EOF
  cat > "$T/match/gamma/config.yaml" <<'EOF'
model:
  default: model-g
  base_url: http://127.0.0.1:8002/v1
delegation:
  model: model-d
  base_url: http://127.0.0.1:8001/v1
EOF
  # delta has no config.yaml on purpose: must be warned and skipped, not failed.

  # A copy of the good tree with one wrong base_url and one wrong delegation URL.
  cp -R "$T/match" "$T/bad"
  cat > "$T/bad/alpha/config.yaml" <<'EOF'
model:
  default: model-a
  base_url: http://127.0.0.1:8001
EOF
  cat > "$T/bad/gamma/config.yaml" <<'EOF'
model:
  default: model-g
  base_url: http://127.0.0.1:8002/v1
delegation:
  model: model-d
  base_url: http://127.0.0.1:8003/v1
EOF

  # Invalid backends: unknown tier.
  cat > "$T/invalid.yaml" <<'EOF'
tiers:
  default: http://127.0.0.1:8001
profiles:
  alpha:
    model: model-a
    tier: nosuchtier
EOF

  # run_case <expected-exit> <label> <must-contain-or-empty> <checker args...>
  run_case() {
    local want="$1" label="$2" needle="$3"; shift 3
    local out rc
    out="$("$PY" "$CHECKER" "$@" 2>&1)"; rc=$?
    if [ "$rc" -ne "$want" ]; then
      fail "$label — exit $rc, wanted $want :: $out"
      return
    fi
    if [ -n "$needle" ] && ! printf '%s' "$out" | grep -q -- "$needle"; then
      fail "$label — output lacks '$needle' :: $out"
      return
    fi
    pass "$label (exit $rc)"
  }

  run_case 0 "matching fixture passes (trailing slash, /v1, remote host, missing profile skipped)" "" \
    "$T/backends.yaml" --profiles-dir "$T/match"
  run_case 1 "mismatched fixture fails with a diff" "model.base_url" \
    "$T/backends.yaml" --profiles-dir "$T/bad"
  run_case 1 "mismatched delegation.base_url is reported" "delegation.base_url" \
    "$T/backends.yaml" --profiles-dir "$T/bad"
  run_case 2 "invalid backends (unknown tier) exits 2" "unknown tier" \
    "$T/invalid.yaml" --profiles-dir "$T/match"

  rm -rf "$T"

  # --- real files (only when the operator has linked config/backends.yaml) ---
  if [ -f "$REPO_ROOT/config/backends.yaml" ]; then
    run_case 0 "real config/backends.yaml matches config/profiles" "" \
      "$REPO_ROOT/config/backends.yaml" --profiles-dir "$REPO_ROOT/config/profiles"
  else
    skip "check-backends real files — config/backends.yaml absent (CI has only the .example)"
  fi
fi

section "check-backends summary"
[ "$FAIL_COUNT" -eq 0 ]
