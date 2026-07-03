#!/usr/bin/env bash
# kanban-backup.example.sh
# =========================
# SQLite online backup + retention skeleton for the Hermes kanban (and optionally
# state) database.
#
# WHY `sqlite3 .backup` INSTEAD OF `cp`
# --------------------------------------
# The Hermes gateway holds the kanban DB open continuously. SQLite's WAL (Write-Ahead
# Logging) mode means a plain `cp` can capture the DB mid-transaction, producing a
# corrupt or inconsistent snapshot. The `sqlite3 .backup` command uses the SQLite
# Online Backup API: it takes a page-level, consistent snapshot of a LIVE database,
# handling concurrent writes gracefully without locking out the gateway.
#
# Reference: the maintainer's kanban DB lives at $HERMES_HOME/kanban.db.
# This script is safe to run from launchd (com.hermes-workflows.backup.plist.example)
# OR from the in-gateway cron (cron/jobs.json.example, job id 'kanban-db-backup').
# Enable EXACTLY ONE — see cron/README.md for the backup-choice discussion.
#
# EXTENDING TO MULTIPLE DATABASES
# --------------------------------
# To also back up state.db (per-profile) or a Gitea DB, convert DB_PATH into
# an array and loop over it — left as TODO at the bottom of this script.

set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIG — all values env-overridable, with sane defaults
# ---------------------------------------------------------------------------

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Path to the SQLite database to back up.
# TODO: if you have multiple DBs (e.g. state.db per profile), extend into an
#       array and loop — see the TODO block at the end of this script.
DB_PATH="${HERMES_KANBAN_DB:-$HERMES_HOME/kanban.db}"

# Directory where timestamped backup files are written.
BACKUP_DIR="${HERMES_BACKUP_DIR:-$HERMES_HOME/backups}"

# How many days of backups to retain; older files are pruned.
RETENTION_DAYS="${HERMES_BACKUP_RETENTION_DAYS:-14}"

# Timestamp for this backup run.
TS=$(date +%Y%m%d-%H%M%S)

# ---------------------------------------------------------------------------
# PREFLIGHT CHECKS
# ---------------------------------------------------------------------------

# Require sqlite3.
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "[kanban-backup] ERROR: sqlite3 not found on PATH. Install via Homebrew: brew install sqlite" >&2
    exit 1
fi

# Require the source DB to exist.
if [[ ! -f "$DB_PATH" ]]; then
    echo "[kanban-backup] ERROR: DB not found: $DB_PATH" >&2
    echo "[kanban-backup] Set HERMES_KANBAN_DB to the correct path." >&2
    exit 1
fi

# Create the backup directory if it doesn't exist.
mkdir -p "$BACKUP_DIR"

# ---------------------------------------------------------------------------
# ONLINE BACKUP
# ---------------------------------------------------------------------------

OUT="$BACKUP_DIR/$(basename "$DB_PATH" .db)-$TS.db"

echo "[kanban-backup] Starting online backup: $DB_PATH -> $OUT"

# `.timeout 5000`: wait up to 5 s for any active write lock to clear before
# starting the backup, to avoid a "database is locked" error.
# The gateway keeps the DB open but the backup API handles concurrent reads/writes.
sqlite3 "$DB_PATH" \
    ".timeout 5000" \
    ".backup '$OUT'"

# ---------------------------------------------------------------------------
# INTEGRITY CHECK
# ---------------------------------------------------------------------------

echo "[kanban-backup] Running integrity check on backup..."
INTEGRITY=$(sqlite3 "$OUT" 'PRAGMA integrity_check;')

if [[ "$INTEGRITY" != "ok" ]]; then
    echo "[kanban-backup] ERROR: integrity check failed: $INTEGRITY" >&2
    echo "[kanban-backup] Removing corrupt backup: $OUT" >&2
    rm -f "$OUT"
    exit 1
fi

# ---------------------------------------------------------------------------
# COMPRESS
# ---------------------------------------------------------------------------

gzip -f "$OUT"
COMPRESSED="$OUT.gz"
COMPRESSED_SIZE=$(du -sh "$COMPRESSED" | cut -f1)

echo "[kanban-backup] Compressed: $COMPRESSED ($COMPRESSED_SIZE)"

# ---------------------------------------------------------------------------
# RETENTION PRUNE
# ---------------------------------------------------------------------------

# Delete backups older than RETENTION_DAYS.
# `find -mtime +N` matches files last modified MORE than N*24h ago.
PRUNED_COUNT=0
while IFS= read -r -d '' old_file; do
    echo "[kanban-backup] Pruning old backup: $old_file"
    rm -f "$old_file"
    (( PRUNED_COUNT++ )) || true
done < <(find "$BACKUP_DIR" -name '*.db.gz' -type f -mtime +"$RETENTION_DAYS" -print0)

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------

RETAINED_COUNT=$(find "$BACKUP_DIR" -name '*.db.gz' -type f | wc -l | tr -d ' ')

echo "[kanban-backup] Done."
echo "[kanban-backup]   Backup:   $COMPRESSED ($COMPRESSED_SIZE)"
echo "[kanban-backup]   Pruned:   $PRUNED_COUNT file(s) older than ${RETENTION_DAYS} days"
echo "[kanban-backup]   Retained: $RETAINED_COUNT backup(s) in $BACKUP_DIR"

# ---------------------------------------------------------------------------
# TODO: EXTEND TO MULTIPLE DATABASES
# ---------------------------------------------------------------------------
# To also back up state.db, Gitea, or the Obsidian vault DB, replace the
# single-DB logic above with an array loop:
#
# declare -a DBS=(
#     "$HERMES_HOME/kanban.db"
#     "$HERMES_HOME/profiles/coding/state.db"
#     # "$HERMES_HOME/gitea/data/gitea.db"   # if using Gitea
# )
# for DB_PATH in "${DBS[@]}"; do
#     [[ -f "$DB_PATH" ]] || continue
#     OUT="$BACKUP_DIR/$(basename "$DB_PATH" .db)-$TS.db"
#     sqlite3 "$DB_PATH" ".timeout 5000" ".backup '$OUT'"
#     sqlite3 "$OUT" 'PRAGMA integrity_check;' | grep -q '^ok$' || { rm -f "$OUT"; exit 1; }
#     gzip -f "$OUT"
# done
