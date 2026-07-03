#!/usr/bin/env bash
# =============================================================================
# run.sh — entrypoint for the Gmail processing example
#
# Orchestrates the example end-to-end:
#   1. Verify prerequisites (a credential source; GMAIL_USER_ID for live runs)
#   2. Run gmail-broker.sh  (read-only retrieve + subject-glob gate via the broker)
#
# POC / design-target honest note: the dispatcher and the agent-side processing
# loop are design targets; this script runs the part that is actually runnable
# today (the host-side broker). Nothing here sends, modifies, or deletes mail —
# it only RETRIEVES.
#
# Mode A — MOCK (default-friendly, no network, no real account):
#   GMAIL_MOCK=1 bash examples/gmail-processing/run.sh
#
# Mode B — LIVE (broker resolves the credential from your vault — preferred):
#   export GMAIL_USER_ID="your-composio-user-id"
#   export GMAIL_QUERY="subject:invoice newer_than:7d"
#   GMAIL_VAULT=seckit bash examples/gmail-processing/run.sh
#   GMAIL_VAULT=op GMAIL_OP_REF="op://Hermes/Composio Gmail/credential" \
#     bash examples/gmail-processing/run.sh
#
# Mode B — LIVE (direct credential injection, fallback):
#   export GMAIL_USER_ID="your-composio-user-id"
#   export GMAIL_API_KEY="ak_…"   # never commit a real key
#   bash examples/gmail-processing/run.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve the directory this script lives in so we can call siblings by
# absolute path regardless of the caller's CWD.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BROKER_SCRIPT="${SCRIPT_DIR}/gmail-broker.sh"

MOCK="${GMAIL_MOCK:-0}"

# ---------------------------------------------------------------------------
# banner
# ---------------------------------------------------------------------------
echo "=================================================================="
echo "  Gmail processing example — hermes-workflows POC"
echo "=================================================================="
echo

# ---------------------------------------------------------------------------
# Step 0: Verify the broker exists
# ---------------------------------------------------------------------------
if [[ ! -f "$BROKER_SCRIPT" ]]; then
  echo "ERROR: required script not found: $BROKER_SCRIPT" >&2
  echo "  Run this script from the repo root:" >&2
  echo "    bash examples/gmail-processing/run.sh" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Mode A: MOCK — skip all prerequisite checks, run broker in mock mode.
# This is the "prove it offline" path: no network, no real Composio account,
# placeholder credential. It leads because it is the safe default to demo with.
# ---------------------------------------------------------------------------
if [[ "$MOCK" == "1" ]]; then
  echo "[prereqs] GMAIL_MOCK=1 — skipping network prerequisites."
  echo "          Using placeholder credential; no real Gmail/Composio account required."
  echo
  # Provide a placeholder credential so the broker's credential-check passes.
  export GMAIL_API_KEY="${GMAIL_API_KEY:-placeholder-not-a-real-key}"

  echo "----------------------------------------------------------------"
  echo "  gmail-broker.sh (MOCK: canned messages, read-only)"
  echo "----------------------------------------------------------------"
  GMAIL_MOCK=1 bash "$BROKER_SCRIPT"
  echo
  echo "=================================================================="
  echo "  Mock run complete."
  echo "  To run against a real account, set GMAIL_USER_ID + a credential"
  echo "  (see examples/gmail-processing/README.md)."
  echo "=================================================================="
  exit 0
fi

# ---------------------------------------------------------------------------
# Mode B (live): Verify prerequisites
# ---------------------------------------------------------------------------
echo "----------------------------------------------------------------"
echo "  Step 1 — checking prerequisites"
echo "----------------------------------------------------------------"
echo

PREREQ_FAIL=0

# GMAIL_USER_ID is mandatory for live runs (scopes the Composio connected account).
if [[ -z "${GMAIL_USER_ID:-}" ]]; then
  echo "ERROR: GMAIL_USER_ID is not set." >&2
  echo >&2
  echo "  Set it to your Composio connected-account user id:" >&2
  echo "    export GMAIL_USER_ID=\"your-composio-user-id\"" >&2
  echo >&2
  PREREQ_FAIL=1
else
  echo "  GMAIL_USER_ID = ${GMAIL_USER_ID}"
fi

# Credential: must be available via vault or directly in env. We detect
# availability here so we can give a clear error before running the broker.
VAULT="${GMAIL_VAULT:-}"
OP_REF="${GMAIL_OP_REF:-}"
CRED_SOURCE=""

if [[ -n "${GMAIL_API_KEY:-}" ]]; then
  CRED_SOURCE="env (GMAIL_API_KEY)"
elif [[ "$VAULT" == "seckit" ]]; then
  if ! command -v seckit >/dev/null 2>&1; then
    echo "ERROR: GMAIL_VAULT=seckit but 'seckit' is not on PATH." >&2
    echo "  Install Secrets-Kit or switch to GMAIL_VAULT=op, or set GMAIL_API_KEY directly." >&2
    PREREQ_FAIL=1
  else
    CRED_SOURCE="seckit vault"
  fi
elif [[ "$VAULT" == "op" ]]; then
  if ! command -v op >/dev/null 2>&1; then
    echo "ERROR: GMAIL_VAULT=op but the 1Password CLI ('op') is not on PATH." >&2
    echo "  Install the 1Password CLI or switch to GMAIL_VAULT=seckit, or set GMAIL_API_KEY directly." >&2
    PREREQ_FAIL=1
  elif [[ -z "$OP_REF" ]]; then
    echo "ERROR: GMAIL_VAULT=op requires GMAIL_OP_REF=op://Vault/Item/field." >&2
    PREREQ_FAIL=1
  else
    CRED_SOURCE="1Password (${OP_REF})"
  fi
else
  echo "ERROR: no credential source configured." >&2
  echo >&2
  echo "  You need a Composio API key (it fronts the Gmail OAuth grant). Provide it one of:" >&2
  echo >&2
  echo "  Preferred — broker resolves the credential from your vault (never printed):" >&2
  echo "    GMAIL_VAULT=seckit bash examples/gmail-processing/run.sh" >&2
  echo "    GMAIL_VAULT=op GMAIL_OP_REF=\"op://Hermes/Composio Gmail/credential\" bash ..." >&2
  echo >&2
  echo "  Fallback — inject the credential directly (scrubbed from all output):" >&2
  echo "    export GMAIL_API_KEY=\"ak_…\"" >&2
  echo "    bash examples/gmail-processing/run.sh" >&2
  echo >&2
  echo "  Offline proof (no real account or credential needed):" >&2
  echo "    GMAIL_MOCK=1 bash examples/gmail-processing/run.sh" >&2
  echo >&2
  echo "  See examples/gmail-processing/SECURE-SECRETS.md for the Composio + vault walkthrough." >&2
  PREREQ_FAIL=1
fi

if [[ "$PREREQ_FAIL" -ne 0 ]]; then
  echo >&2
  echo "Fix the error(s) above and re-run." >&2
  exit 2
fi

echo "  Credential source: ${CRED_SOURCE} (value held in broker only — never printed here)"
echo "  Query: ${GMAIL_QUERY:-subject:invoice newer_than:7d}"
echo "  Subject glob: ${GMAIL_SUBJECT_GLOB:-*invoice*}"
echo
echo "  Prerequisites OK."
echo

# ---------------------------------------------------------------------------
# Step 2: Retrieve + gate via broker (gmail-broker.sh)
# ---------------------------------------------------------------------------
echo "----------------------------------------------------------------"
echo "  Step 2 — retrieve + gate broker (gmail-broker.sh)"
echo "----------------------------------------------------------------"
echo
echo "  Running gmail-broker.sh (read-only; credential held in broker process only) ..."
echo

if bash "$BROKER_SCRIPT"; then
  BROKER_EXIT=0
else
  BROKER_EXIT=$?
fi

echo

if [[ "$BROKER_EXIT" -eq 0 ]]; then
  echo "=================================================================="
  echo "  Retrieve complete."
  echo "  Review the matched messages above. The agent would now process"
  echo "  each (read-only) per examples/gmail-processing/processing-skill.md."
  echo "=================================================================="
  exit 0
else
  echo "=================================================================="
  echo "  Gmail broker exited with code ${BROKER_EXIT}."
  echo "  Review the output above for error details."
  echo "=================================================================="
  exit "$BROKER_EXIT"
fi
