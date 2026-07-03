#!/usr/bin/env bash
# =============================================================================
# run.sh — entrypoint for the Home Assistant healthcheck example
#
# Orchestrates the example end-to-end:
#   1. Verify prerequisites (HASS_BASE_URL set; token available via broker or env)
#   2. Run check-hass-connectivity.sh  (human-readable connectivity probe)
#   3. Run ha-broker.sh                (read-only health-check via the broker)
#
# POC / design-target honest note: the dispatcher and HA adapter are design
# targets; this script runs the parts that are actually runnable today (the
# shell helpers).  Nothing here writes to Home Assistant.
#
# Required environment variables
# ───────────────────────────────
#   HASS_BASE_URL   Base URL of your HA instance, e.g. http://homeassistant.local:8123
#                   (no trailing /api, no trailing slash).  REQUIRED.
#
# Token (one of the following — broker is preferred):
#   HASS_VAULT=seckit          Broker reads token from the seckit vault (preferred).
#   HASS_VAULT=op +
#     HASS_OP_REF=op://...     Broker reads token via the 1Password CLI (preferred).
#   HASS_TOKEN=<value>         Direct token injection into this process (fallback).
#                              The broker still scrubs it from all output.
#   HASS_MOCK=1                Offline proof mode — no network, no real HA needed.
#                              Sets a placeholder token automatically.
#
# Offline proof (no HA, no real token):
#   HASS_MOCK=1 bash examples/home-assistant-healthcheck/run.sh
#
# Broker (vault-resolved token):
#   export HASS_BASE_URL="http://homeassistant.local:8123"
#   HASS_VAULT=seckit bash examples/home-assistant-healthcheck/run.sh
#   HASS_VAULT=op HASS_OP_REF="op://Hermes/HA Token/credential" \
#     bash examples/home-assistant-healthcheck/run.sh
#
# Direct token (fallback, less preferred):
#   export HASS_BASE_URL="http://homeassistant.local:8123"
#   export HASS_TOKEN="your-long-lived-token"   # never commit a real token
#   bash examples/home-assistant-healthcheck/run.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve the directory this script lives in so we can call siblings by
# absolute path regardless of the caller's CWD.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONNECTIVITY_SCRIPT="${SCRIPT_DIR}/check-hass-connectivity.sh"
BROKER_SCRIPT="${SCRIPT_DIR}/ha-broker.sh"

MOCK="${HASS_MOCK:-0}"

# ---------------------------------------------------------------------------
# banner
# ---------------------------------------------------------------------------
echo "=================================================================="
echo "  Home Assistant healthcheck example — hermes-workflows POC"
echo "=================================================================="
echo

# ---------------------------------------------------------------------------
# Step 0: Verify script siblings exist
# ---------------------------------------------------------------------------
for f in "$CONNECTIVITY_SCRIPT" "$BROKER_SCRIPT"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: required script not found: $f" >&2
    echo "  Run this script from the repo root:" >&2
    echo "    bash examples/home-assistant-healthcheck/run.sh" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Step 1: Mock mode — skip all prerequisite checks, run broker in mock mode
# ---------------------------------------------------------------------------
if [[ "$MOCK" == "1" ]]; then
  echo "[prereqs] HASS_MOCK=1 — skipping network prerequisites."
  echo "          Using placeholder token; no real HA instance required."
  echo
  # Provide a placeholder token so the broker's token-check passes.
  export HASS_TOKEN="${HASS_TOKEN:-placeholder-not-a-real-token}"
  export HASS_BASE_URL="${HASS_BASE_URL:-http://homeassistant.local:8123}"

  echo "----------------------------------------------------------------"
  echo "  Step 2 — connectivity check (MOCK: skipped — no network)"
  echo "----------------------------------------------------------------"
  echo "  (mock mode: skipping check-hass-connectivity.sh — no network touched)"
  echo

  echo "----------------------------------------------------------------"
  echo "  Step 3 — ha-broker.sh (MOCK: canned healthcheck)"
  echo "----------------------------------------------------------------"
  bash "$BROKER_SCRIPT"
  echo
  echo "=================================================================="
  echo "  Mock run complete."
  echo "  To run against a real HA instance, set HASS_BASE_URL + a token"
  echo "  (see examples/home-assistant-healthcheck/README.md)."
  echo "=================================================================="
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1 (live): Verify prerequisites
# ---------------------------------------------------------------------------
echo "----------------------------------------------------------------"
echo "  Step 1 — checking prerequisites"
echo "----------------------------------------------------------------"
echo

PREREQ_FAIL=0

# HASS_BASE_URL is mandatory for live runs.
if [[ -z "${HASS_BASE_URL:-}" ]]; then
  echo "ERROR: HASS_BASE_URL is not set." >&2
  echo >&2
  echo "  Set it to the base URL of your Home Assistant instance:" >&2
  echo "    export HASS_BASE_URL=\"http://homeassistant.local:8123\"" >&2
  echo >&2
  PREREQ_FAIL=1
else
  echo "  HASS_BASE_URL = ${HASS_BASE_URL}"
fi

# Token: must be available via vault or directly in env.
# We detect availability here so we can give a clear error before running the
# broker (which also validates, but its error is less targeted).
VAULT="${HASS_VAULT:-}"
OP_REF="${HASS_OP_REF:-}"
TOKEN_SOURCE=""

if [[ -n "${HASS_TOKEN:-}" ]]; then
  TOKEN_SOURCE="env (HASS_TOKEN)"
elif [[ "$VAULT" == "seckit" ]]; then
  if ! command -v seckit >/dev/null 2>&1; then
    echo "ERROR: HASS_VAULT=seckit but 'seckit' is not on PATH." >&2
    echo "  Install Secrets-Kit or switch to HASS_VAULT=op, or set HASS_TOKEN directly." >&2
    PREREQ_FAIL=1
  else
    TOKEN_SOURCE="seckit vault"
  fi
elif [[ "$VAULT" == "op" ]]; then
  if ! command -v op >/dev/null 2>&1; then
    echo "ERROR: HASS_VAULT=op but the 1Password CLI ('op') is not on PATH." >&2
    echo "  Install the 1Password CLI or switch to HASS_VAULT=seckit, or set HASS_TOKEN directly." >&2
    PREREQ_FAIL=1
  elif [[ -z "$OP_REF" ]]; then
    echo "ERROR: HASS_VAULT=op requires HASS_OP_REF=op://Vault/Item/field." >&2
    PREREQ_FAIL=1
  else
    TOKEN_SOURCE="1Password (${OP_REF})"
  fi
else
  # No vault and no HASS_TOKEN.
  echo "ERROR: no token source configured." >&2
  echo >&2
  echo "  You need a Home Assistant long-lived access token.  Provide it one of:" >&2
  echo >&2
  echo "  Preferred — broker resolves the token from your vault (token never printed):" >&2
  echo "    HASS_VAULT=seckit bash examples/home-assistant-healthcheck/run.sh" >&2
  echo "    HASS_VAULT=op HASS_OP_REF=\"op://Hermes/HA Token/credential\" bash ..." >&2
  echo >&2
  echo "  Fallback — inject the token directly (scrubbed from all output):" >&2
  echo "    export HASS_TOKEN=\"your-long-lived-access-token\"" >&2
  echo "    bash examples/home-assistant-healthcheck/run.sh" >&2
  echo >&2
  echo "  Offline proof (no real HA or token needed):" >&2
  echo "    HASS_MOCK=1 bash examples/home-assistant-healthcheck/run.sh" >&2
  echo >&2
  echo "  Create a long-lived token in HA: Profile → Security → Long-lived access tokens." >&2
  echo "  See examples/home-assistant-healthcheck/SECURE-SECRETS.md for the vault walkthrough." >&2
  PREREQ_FAIL=1
fi

if [[ "$PREREQ_FAIL" -ne 0 ]]; then
  echo >&2
  echo "Fix the error(s) above and re-run." >&2
  exit 2
fi

echo "  Token source: ${TOKEN_SOURCE} (value held in broker only — never printed here)"
echo
echo "  Prerequisites OK."
echo

# ---------------------------------------------------------------------------
# Step 2: Connectivity probe (check-hass-connectivity.sh)
# Only runs when HASS_TOKEN is available in env (broker path skips this because
# the token should not be exported into child processes outside the broker).
# ---------------------------------------------------------------------------
echo "----------------------------------------------------------------"
echo "  Step 2 — connectivity probe (check-hass-connectivity.sh)"
echo "----------------------------------------------------------------"
echo

if [[ -n "${HASS_TOKEN:-}" ]]; then
  # Token is in env — we can run the connectivity helper directly.
  echo "  Running connectivity check against ${HASS_BASE_URL} ..."
  echo
  if bash "$CONNECTIVITY_SCRIPT"; then
    echo
    echo "  Connectivity probe: PASSED"
  else
    EXIT_CODE=$?
    echo
    echo "ERROR: connectivity probe failed (exit ${EXIT_CODE})." >&2
    echo "  Check that HASS_BASE_URL is correct and the token is valid." >&2
    echo "  See examples/home-assistant-healthcheck/README.md for troubleshooting." >&2
    exit "$EXIT_CODE"
  fi
else
  # Vault path — the connectivity script needs HASS_TOKEN in env, which we do
  # not have here (the broker holds it).  Skip and let the broker do its own
  # connectivity check in Step 3.
  echo "  Token is vault-resolved (HASS_VAULT=${VAULT})."
  echo "  Connectivity check requires HASS_TOKEN in env; skipping to avoid"
  echo "  extracting the token from the vault twice."
  echo "  The broker (Step 3) performs equivalent GET requests and will"
  echo "  report any connectivity failure clearly."
fi

echo

# ---------------------------------------------------------------------------
# Step 3: Health-check via broker (ha-broker.sh)
# ---------------------------------------------------------------------------
echo "----------------------------------------------------------------"
echo "  Step 3 — health-check broker (ha-broker.sh)"
echo "----------------------------------------------------------------"
echo

echo "  Running ha-broker.sh (read-only; token held in broker process only) ..."
echo

if bash "$BROKER_SCRIPT"; then
  BROKER_EXIT=0
else
  BROKER_EXIT=$?
fi

echo

if [[ "$BROKER_EXIT" -eq 0 ]]; then
  echo "=================================================================="
  echo "  Health-check complete."
  echo "  Review the findings above."
  echo "  See examples/home-assistant-healthcheck/README.md for next steps."
  echo "=================================================================="
  exit 0
else
  echo "=================================================================="
  echo "  Health-check broker exited with code ${BROKER_EXIT}."
  echo "  Review the output above for error details."
  echo "=================================================================="
  exit "$BROKER_EXIT"
fi
