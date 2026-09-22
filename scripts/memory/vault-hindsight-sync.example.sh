#!/usr/bin/env bash
# Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/scripts/memory/vault-hindsight-sync.example.sh
# vault-hindsight-sync.example.sh
#
# Purpose: Incrementally sync changed vault markdown files into Hindsight
#          semantic memory (pgvector) for recall-by-meaning.
#
# EXAMPLE / SKELETON — search for "TODO(binding)" before using in production.
# The active code path runs in dry-run mode (safe with no Hindsight instance).
# Wire the real ingest call by following the TODO(binding) markers below.
#
# Invocation (per project convention — run with bash, not ./):
#   bash scripts/memory/vault-hindsight-sync.example.sh
#
# Typical invocation via launchd (native) or cron (every 30 minutes):
#   */30 * * * * bash /path/to/scripts/memory/vault-hindsight-sync.example.sh >> ~/.hindsight-sync.log 2>&1
#
# Requirements: bash >= 4, jq, curl
#
# See also:
#   config/hindsight/hindsight.example.yaml  — sync configuration template
#   docs/memory.md                           — three-layer memory architecture

set -euo pipefail

# ── Configuration (all env-overridable, portable defaults) ───────────────────

VAULT_ROOT="${VAULT_ROOT:-$HOME/vault}"
HINDSIGHT_ENDPOINT="${HINDSIGHT_ENDPOINT:-http://localhost:8889}"
STATE_FILE="${STATE_FILE:-$HOME/.hindsight-sync-state}"

# Hard wall-clock cap per run. If exceeded, stop cleanly and resume next tick.
# Prevents runs from overlapping or stampeding.
WALL_BUDGET_SECONDS="${WALL_BUDGET_SECONDS:-90}"

# HTTP timeout for the ingest call. MUST be >= 120s.
# The embedder may invoke your configured LLM backend;
# use a generous timeout to avoid spurious failures on a cold backend.
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"

# Top-level vault directories to include in sync.
# Project/ is excluded (own git repo / separate boundary).
# Security/private/ is excluded (potentially sensitive).
INCLUDE_DIRS=(Architecture Operations Research Meta Strategy)

# ── Helper: portable file mtime ───────────────────────────────────────────────
# GNU find supports -newermt "@epoch" natively; BSD find (macOS) may not.
# We use a stat-based helper as a fallback for per-file mtime comparison.

file_mtime() {
  # Try GNU stat first, then BSD stat (macOS).
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

# ── Main ──────────────────────────────────────────────────────────────────────

START_EPOCH=$(date +%s)
SYNCED=0
BUDGET_HIT=0

echo "[vault-hindsight-sync] started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── Step 1: Determine last sync time ─────────────────────────────────────────
# mtime-incremental: only re-ingest files changed since the last successful run.
# On the very first run (no state file), LAST_SYNC=0 triggers a full pass.

if [[ -f "$STATE_FILE" ]]; then
  LAST_SYNC=$(cat "$STATE_FILE")
  echo "[vault-hindsight-sync] last sync epoch: $LAST_SYNC"
else
  LAST_SYNC=0
  echo "[vault-hindsight-sync] no state file found — performing full initial pass"
fi

# ── Step 2: Build list of changed files ──────────────────────────────────────

CHANGED_FILES=()

for dir in "${INCLUDE_DIRS[@]}"; do
  target="$VAULT_ROOT/$dir"
  if [[ ! -d "$target" ]]; then
    echo "[vault-hindsight-sync] warning: included dir not found, skipping: $target"
    continue
  fi

  # Collect markdown files changed since LAST_SYNC.
  # GNU find supports -newermt "@$LAST_SYNC" but BSD find (macOS) may not.
  # We collect all .md files and compare mtime per-file via file_mtime().
  while IFS= read -r -d '' f; do
    # Skip Obsidian workspace files and sync bookkeeping
    case "$f" in
      */.obsidian/*|*.syncstate|*/.hindsight-sync-state) continue ;;
    esac

    mtime=$(file_mtime "$f")
    if (( mtime > LAST_SYNC )); then
      CHANGED_FILES+=("$f")
    fi
  done < <(find "$target" -type f -name '*.md' -print0)
done

TOTAL_CHANGED=${#CHANGED_FILES[@]}
echo "[vault-hindsight-sync] found $TOTAL_CHANGED changed file(s) to sync"

# ── Step 3: Ingest changed files ──────────────────────────────────────────────

for file in "${CHANGED_FILES[@]}"; do
  # Check wall budget before each file
  elapsed=$(( $(date +%s) - START_EPOCH ))
  if (( elapsed >= WALL_BUDGET_SECONDS )); then
    echo "[vault-hindsight-sync] wall budget reached (${elapsed}s >= ${WALL_BUDGET_SECONDS}s), stopping — will resume next run"
    BUDGET_HIT=1
    break
  fi

  # ── TODO(binding): replace the dry-run echo below with your real Hindsight
  # retain/ingest API call.
  #
  # The shape below is an EXAMPLE — adjust the endpoint path and payload
  # to match your Hindsight version's actual API:
  #
  #   curl -fsS --max-time "$TIMEOUT_SECONDS" \
  #     -X POST "$HINDSIGHT_ENDPOINT/retain" \
  #     -H 'Content-Type: application/json' \
  #     --data "$(jq -n \
  #       --arg path "$file" \
  #       --rawfile content "$file" \
  #       '{path: $path, text: $content}')"
  #
  # TODO(binding): remove the dry-run echo once the real call above is wired.
  echo "[dry-run] would ingest: $file"

  (( SYNCED++ )) || true
done

# ── Step 4: Advance state file ────────────────────────────────────────────────
# Write the run-start epoch so files modified mid-run are caught next tick.
#
# TODO(decision): on BUDGET_HIT you have two strategies:
#   A) Advance anyway (default below): ensures forward progress even when
#      the run was truncated. Risk: files modified during the truncated run
#      window may be missed if their mtime falls before START_EPOCH.
#   B) Do NOT advance on budget hit: guarantees we never skip a file, but
#      may re-ingest already-synced files on the next run after a truncation.
#
# Default: advance regardless (strategy A).
# For strategy B: replace the line below with:
#   if (( BUDGET_HIT == 0 )); then echo "$START_EPOCH" > "$STATE_FILE"; fi

echo "$START_EPOCH" > "$STATE_FILE"
echo "[vault-hindsight-sync] state file updated to epoch $START_EPOCH"

# ── Summary ───────────────────────────────────────────────────────────────────

ELAPSED=$(( $(date +%s) - START_EPOCH ))
if (( BUDGET_HIT )); then
  echo "[vault-hindsight-sync] DONE (budget hit) — synced $SYNCED/$TOTAL_CHANGED file(s) in ${ELAPSED}s"
else
  echo "[vault-hindsight-sync] DONE — synced $SYNCED/$TOTAL_CHANGED file(s) in ${ELAPSED}s"
fi

# ── PORTABILITY / SAFETY NOTES ───────────────────────────────────────────────
# - No secrets in this script. Read endpoint/token from env vars or the
#   config file (config/hindsight/hindsight.example.yaml).
# - Uses $HOME and env vars throughout — no absolute personal paths.
# - Dry-run by default: safe to run with no Hindsight instance present.
#   The real ingest call is commented out; remove dry-run after wiring.
# - Requires: jq (real ingest call JSON payload), curl
# - macOS / GNU portability: file_mtime() handles both stat dialects.
#   GNU find -newermt is not used; per-file mtime comparison is portable.
# - Reference: docs/memory.md (three-layer architecture, timeout rationale)
# - Reference: config/hindsight/hindsight.example.yaml (full config options)
