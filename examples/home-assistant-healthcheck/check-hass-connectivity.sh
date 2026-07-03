#!/usr/bin/env bash
# =============================================================================
# check-hass-connectivity.sh
#
# POC / design-target helper. A small, READ-ONLY connectivity check a HUMAN can
# run to confirm a standalone Hermes would be able to reach their Home Assistant
# instance. It makes the same GET requests the health-check skill would:
#   1. GET /api/config       — is HA up? what version?
#   2. GET /api/error_log    — how many ERROR/WARNING lines recently?
#   3. GET /api/states       — how many entities are unavailable/unknown?
#
# It is NON-DESTRUCTIVE: GET requests only. It never calls a service, never
# changes a state, never writes to Home Assistant.
#
# Requirements: curl (required), jq (optional — falls back to grep/wc if absent).
# No secrets are stored: the token is read from the environment only.
#
# Usage:
#   export HASS_BASE_URL="http://homeassistant.local:8123"
#   export HASS_TOKEN="…"     # long-lived access token — never commit it
#   bash examples/home-assistant-healthcheck/check-hass-connectivity.sh
#
# Environment variables:
#   HASS_BASE_URL   Base URL of the HA instance (no trailing /api). REQUIRED.
#   HASS_TOKEN      Long-lived access token (bearer). REQUIRED.
#   HASS_TIMEOUT    curl --max-time in seconds. Default: 15.
# =============================================================================

set -euo pipefail

BASE_URL="${HASS_BASE_URL:-}"
TOKEN="${HASS_TOKEN:-}"
TIMEOUT="${HASS_TIMEOUT:-15}"

if [[ -z "$BASE_URL" || -z "$TOKEN" ]]; then
  echo "ERROR: set HASS_BASE_URL and HASS_TOKEN in your environment first." >&2
  echo "  export HASS_BASE_URL=\"http://homeassistant.local:8123\"" >&2
  echo "  export HASS_TOKEN=\"…\"   # long-lived access token (never commit)" >&2
  exit 2
fi

# Strip any trailing slash so we can append /api cleanly.
BASE_URL="${BASE_URL%/}"

HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then HAVE_JQ=1; fi

# curl wrapper: GET only, bearer auth, fail loudly on HTTP errors.
hass_get() {
  local path="$1"
  curl -fsS --max-time "$TIMEOUT" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "${BASE_URL}${path}"
}

echo "Home Assistant connectivity check (read-only)"
echo "  Target: ${BASE_URL}"
echo

# ---------------------------------------------------------------------------
# 1. /api/config — is HA up?
# ---------------------------------------------------------------------------
echo "[1/3] GET /api/config"
if config_json="$(hass_get /api/config)"; then
  if [[ "$HAVE_JQ" -eq 1 ]]; then
    version="$(printf '%s' "$config_json" | jq -r '.version // "unknown"')"
    location="$(printf '%s' "$config_json" | jq -r '.location_name // "unknown"')"
    echo "      OK — HA is up. version=${version} location=${location}"
  else
    echo "      OK — HA is up. (install jq for parsed version/location)"
  fi
else
  echo "      FAIL — could not reach ${BASE_URL}/api/config (auth? URL? network?)" >&2
  exit 1
fi
echo

# ---------------------------------------------------------------------------
# 2. /api/error_log — recent ERROR/WARNING lines (plain text response)
# ---------------------------------------------------------------------------
echo "[2/3] GET /api/error_log"
if error_log="$(hass_get /api/error_log)"; then
  err_count="$(printf '%s\n' "$error_log" | grep -c 'ERROR'   || true)"
  warn_count="$(printf '%s\n' "$error_log" | grep -c 'WARNING' || true)"
  echo "      OK — ${err_count} ERROR / ${warn_count} WARNING lines in the log."
else
  echo "      WARN — could not fetch /api/error_log (non-fatal)." >&2
fi
echo

# ---------------------------------------------------------------------------
# 3. /api/states — entities stuck on unavailable/unknown
# ---------------------------------------------------------------------------
echo "[3/3] GET /api/states"
if states_json="$(hass_get /api/states)"; then
  if [[ "$HAVE_JQ" -eq 1 ]]; then
    total="$(printf '%s' "$states_json" | jq 'length')"
    stuck="$(printf '%s' "$states_json" \
      | jq '[.[] | select(.state == "unavailable" or .state == "unknown")] | length')"
    echo "      OK — ${total} entities total, ${stuck} unavailable/unknown."
    if [[ "$stuck" -gt 0 ]]; then
      echo "      Stuck entities (first 10):"
      printf '%s' "$states_json" \
        | jq -r '.[] | select(.state == "unavailable" or .state == "unknown")
                 | "        - \(.entity_id) = \(.state)"' \
        | head -10
    fi
  else
    echo "      OK — fetched states. (install jq to count unavailable/unknown entities)"
  fi
else
  echo "      FAIL — could not fetch /api/states." >&2
  exit 1
fi
echo

echo "Connectivity check complete. A standalone Hermes health-check job could"
echo "use this same token + URL to run unattended (see jobs.json.example)."
