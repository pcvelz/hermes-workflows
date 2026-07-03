#!/usr/bin/env bash
# =============================================================================
# run-loop-proof.sh — run the PROVEN autonomous kanban loop, end to end.
#
# Reproduces the verified board → worker → verdict cycle:
#   seed config → init board → add the loop-proof task → dispatch (spawns the
#   worker) → watch ready→running→done → read the worker's verdict → clean up.
#
# Everything runs in an ISOLATED throwaway HERMES_HOME created with `mktemp -d`
# and removed on exit. Your live install at ~/.hermes is NEVER touched.
#
# The trivial task: "Create loop-proof.txt containing exactly LOOP_OK, then mark
# this task done." A spawned worker claims it, writes the file, self-verifies,
# writes a verdict back to the board, and marks itself done — no human in loop.
#
# Requirements (honest):
#   - A backend must be UP (Anthropic-Messages or OpenAI-compatible).
#     Set HERMES_LOOP_BASE_URL and HERMES_LOOP_MODEL for your backend.
#   - The `hermes` CLI on PATH, or set HERMES_BIN to an absolute venv binary.
#
# Environment overrides (REQUIRED before first run):
#   HERMES_BIN            Path to the hermes binary (default: `hermes` on PATH).
#   HERMES_LOOP_MODEL     DOTLESS model id your backend exposes (REQUIRED — no default;
#                         placeholder 'local-model' will 404). Use a dotless alias:
#                         hermes-agent normalises '.' → '-' in model ids, so dotted
#                         names (e.g. Foo3.6-Bar) become Foo3-6-Bar on the wire and
#                         404 on proxies that don't alias them. See docs/backend.md.
#   HERMES_LOOP_BASE_URL  Backend base URL (default: http://127.0.0.1:8001).
#   HERMES_LOOP_KEEP=1    Do NOT delete the throwaway HERMES_HOME (for inspection).
#   HERMES_LOOP_TIMEOUT   Seconds to wait for the worker to finish (default: 180).
#
# Usage:
#   bash examples/autonomous-loop/run-loop-proof.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_BIN="${HERMES_BIN:-hermes}"
MODEL="${HERMES_LOOP_MODEL:-local-model}"
BASE_URL="${HERMES_LOOP_BASE_URL:-http://127.0.0.1:8001}"
WAIT_TIMEOUT="${HERMES_LOOP_TIMEOUT:-180}"

echo "=================================================================="
echo "  Autonomous kanban loop — proof run (hermes-workflows)"
echo "=================================================================="
echo

# ---------------------------------------------------------------------------
# Preflight: backend configured?
# ---------------------------------------------------------------------------
# The model defaults to the generic placeholder `local-model`. If the caller
# hasn't overridden it (or set HERMES_LOOP_BASE_URL), print a friendly guide
# and exit — a bare run with the placeholder will 404 on any real backend.
if [ "$MODEL" = "local-model" ] || [ "$BASE_URL" = "http://127.0.0.1:8001" ] && [ "${HERMES_LOOP_BASE_URL:-}" = "" ]; then
  if [ "$MODEL" = "local-model" ]; then
    echo "ERROR: HERMES_LOOP_MODEL is not set (default placeholder 'local-model' will 404)." >&2
    echo >&2
    echo "  Set it to the DOTLESS alias your backend exposes, e.g.:" >&2
    echo "    HERMES_LOOP_MODEL=my-model HERMES_LOOP_BASE_URL=http://127.0.0.1:8001 \\" >&2
    echo "      bash examples/autonomous-loop/run-loop-proof.sh" >&2
    echo >&2
    echo "  IMPORTANT — use a DOTLESS model id." >&2
    echo "  hermes-agent normalises '.' → '-' in model ids before sending the request." >&2
    echo "  A dotted name like 'Foo3.6-Bar' becomes 'Foo3-6-Bar' on the wire, which" >&2
    echo "  404s on proxies (llama-swap etc.) that don't alias the normalised form." >&2
    echo "  See docs/backend.md for details." >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# Preflight: hermes CLI present?
# ---------------------------------------------------------------------------
if ! command -v "$HERMES_BIN" >/dev/null 2>&1 && [ ! -x "$HERMES_BIN" ]; then
  echo "ERROR: hermes CLI not found ('$HERMES_BIN')." >&2
  echo "  Install it (see docs/getting-started.md) or set HERMES_BIN to an" >&2
  echo "  absolute venv binary, e.g.:" >&2
  echo "    HERMES_BIN=/path/to/venv/bin/hermes bash examples/autonomous-loop/run-loop-proof.sh" >&2
  exit 2
fi
H="$HERMES_BIN"

# ---------------------------------------------------------------------------
# 0. Isolated, safe HERMES_HOME (never touches ~/.hermes)
# ---------------------------------------------------------------------------
TMP_HOME="$(mktemp -d "${TMPDIR:-/tmp}/hermes-loop.XXXXXX")"
export HERMES_HOME="$TMP_HOME"
echo "[0] Isolated HERMES_HOME: $TMP_HOME"

cleanup() {
  if [ "${HERMES_LOOP_KEEP:-0}" = "1" ]; then
    echo
    echo "[cleanup] HERMES_LOOP_KEEP=1 — leaving $TMP_HOME for inspection."
    return
  fi
  # Pattern-guarded removal: only ever delete a hermes-loop.* temp dir.
  case "$TMP_HOME" in
    */hermes-loop.*) rm -rf "$TMP_HOME"; echo; echo "[cleanup] removed $TMP_HOME" ;;
    *) echo "[cleanup] refusing to rm unexpected path: $TMP_HOME" >&2 ;;
  esac
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Seed the minimal config (local backend, approvals:never, manual dispatch).
#    Substitute model/base_url so overrides take effect without editing the file.
# ---------------------------------------------------------------------------
SEED="$SCRIPT_DIR/config.yaml.example"
if [ ! -f "$SEED" ]; then
  echo "ERROR: seed config not found: $SEED" >&2
  exit 1
fi
# Rewrite default model + base_url lines to honor env overrides.
sed \
  -e "s|^  default: .*|  default: ${MODEL}|" \
  -e "s|^  base_url: .*|  base_url: ${BASE_URL}|" \
  "$SEED" > "$TMP_HOME/config.yaml"
echo "[1] Seeded config: model=$MODEL base_url=$BASE_URL"

# ---------------------------------------------------------------------------
# 2. Sanity one-shot — proves model + isolated home work. Should print OK.
#    --ignore-user-config avoids the Claude Code OAuth latch on the one-shot.
# ---------------------------------------------------------------------------
echo
echo "[2] Backend sanity one-shot ..."
if ! "$H" -m "$MODEL" --ignore-user-config -z "say OK"; then
  echo "ERROR: backend sanity one-shot failed." >&2
  echo "  Is your backend up at $BASE_URL with model '$MODEL' available?" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Initialize the board
# ---------------------------------------------------------------------------
echo
echo "[3] Initializing kanban board ..."
"$H" kanban init

# ---------------------------------------------------------------------------
# 4. Add the task (assigned tasks land directly in `ready`)
# ---------------------------------------------------------------------------
WS="$TMP_HOME/proof-workspace"; mkdir -p "$WS"
echo
echo "[4] Creating the loop-proof task (workspace dir:$WS) ..."
"$H" kanban create "Create loop-proof file" \
  --body "Create a file named loop-proof.txt in your current working directory containing exactly the text LOOP_OK and nothing else. Then mark this task done." \
  --assignee default \
  --workspace "dir:$WS"

# Resolve the task id (newest task on the board).
TASK_ID="$("$H" kanban list 2>/dev/null | grep -oE 't_[0-9a-f]+' | head -1 || true)"
if [ -n "$TASK_ID" ]; then
  echo "    task id: $TASK_ID"
  # If it landed in `todo` instead of `ready`, promote it.
  if "$H" kanban list --status todo 2>/dev/null | grep -q "$TASK_ID"; then
    echo "    (task in todo — promoting to ready)"
    "$H" kanban promote "$TASK_ID" || true
  fi
fi

# ---------------------------------------------------------------------------
# 5. Run the dispatcher — ONE pass: reclaim stale, promote ready, SPAWN worker.
#    This is the autonomous tick. It detaches the worker and returns.
# ---------------------------------------------------------------------------
echo
echo "[5] Dispatching (one tick — spawns the worker) ..."
"$H" kanban dispatch

# ---------------------------------------------------------------------------
# 6. Watch the transition + read the verdict
# ---------------------------------------------------------------------------
echo
echo "[6] Waiting for the worker to finish (timeout ${WAIT_TIMEOUT}s) ..."
elapsed=0
done_flag=0
while [ "$elapsed" -lt "$WAIT_TIMEOUT" ]; do
  if "$H" kanban list --status done 2>/dev/null | grep -q "${TASK_ID:-Create loop-proof}"; then
    done_flag=1
    break
  fi
  sleep 5
  elapsed=$((elapsed + 5))
  printf '    ... %ss\n' "$elapsed"
done

echo
echo "------------------------- board state -------------------------"
"$H" kanban list || true
echo
if [ -n "${TASK_ID:-}" ]; then
  echo "------------------------- task detail -------------------------"
  "$H" kanban show "$TASK_ID" || true
  echo
fi

# ---------------------------------------------------------------------------
# Verify the artifact (proves a worker did real file I/O)
# ---------------------------------------------------------------------------
ARTIFACT="$WS/loop-proof.txt"
echo "------------------------- the artifact -------------------------"
if [ -f "$ARTIFACT" ]; then
  echo "  $ARTIFACT:"
  od -c "$ARTIFACT"
  CONTENT="$(cat "$ARTIFACT")"
  if [ "$CONTENT" = "LOOP_OK" ]; then
    echo "  VERDICT: PASS — artifact contains exactly LOOP_OK."
  else
    echo "  VERDICT: artifact present but content unexpected: '$CONTENT'"
  fi
else
  echo "  Artifact not found yet at $ARTIFACT."
  if [ "$done_flag" -eq 0 ]; then
    echo "  (Worker may still be running — cold start on a local model is ~65s+;"
    echo "   re-run with a larger HERMES_LOOP_TIMEOUT, or HERMES_LOOP_KEEP=1 to inspect.)"
  fi
fi

echo
echo "=================================================================="
echo "  Loop proof complete. The full agentic trace is in:"
echo "    $H kanban log ${TASK_ID:-<task_id>}"
echo "  (set HERMES_LOOP_KEEP=1 to inspect $TMP_HOME after exit)"
echo "=================================================================="
