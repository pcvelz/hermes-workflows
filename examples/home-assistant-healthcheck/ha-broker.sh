#!/usr/bin/env bash
# =============================================================================
# ha-broker.sh — host-side BROKER for the Home Assistant health check
#
# WHY A BROKER?
#   The agent should NEVER hold the Home Assistant long-lived token. In the
#   tiered secrets model (docs/secrets.md "Secrets + the sandbox", Tier 2) the
#   preferred pattern for a task secret is the host-bridge BROKER: a small,
#   host-side helper that holds the credential, performs exactly the privileged
#   action, and returns ONLY the result. The agent asks "is HA healthy?"; this
#   broker authenticates and answers. The token never enters the agent process
#   or the docker sandbox (which runs with docker_forward_env: []).
#
#   This is the runnable, single-machine sibling of the HTTP host-bridge in
#   docs/security-hardening.md §7 (scripts/bridge/hermes-bridge.py). Same idea,
#   no daemon: the agent would invoke this broker (e.g. via an allowlisted
#   pre-approved command) instead of ever receiving HASS_TOKEN.
#
# WHERE THE TOKEN COMES FROM (vault-first, in priority order):
#   1. HASS_TOKEN already in the environment (e.g. injected by `seckit run
#      --names HASS_TOKEN` / `op run` for THIS broker process only). Used as-is.
#   2. seckit:  HASS_VAULT=seckit       → `seckit get HASS_TOKEN --raw` (service hermes)
#   3. 1Password: HASS_VAULT=op + HASS_OP_REF=op://Hermes/HA Token/credential
#                                         → `op read "$HASS_OP_REF"`
#   The resolved token is held in a shell variable in THIS process only and is
#   NEVER printed, logged, or echoed. All output is scrubbed (see scrub()).
#
# READ-ONLY: issues only HTTP GET requests. Never calls a service, never changes
# a state, never writes to Home Assistant.
#
# Requirements: curl (required); jq (optional — richer parsing if present).
#
# Usage (vault-resolved token — token never on the command line):
#   # store once in your vault:
#   #   seckit set --name HASS_TOKEN --stdin --kind api_key --service hermes --account local-dev
#   #   (or 1Password: op item create ... / reference op://Hermes/HA Token/credential)
#   export HASS_BASE_URL="http://homeassistant.local:8123"
#   HASS_VAULT=seckit bash ha-broker.sh
#   HASS_VAULT=op HASS_OP_REF="op://Hermes/HA Token/credential" bash ha-broker.sh
#
# Usage (mock / offline proof — no network, no real HA, placeholder token):
#   HASS_MOCK=1 HASS_TOKEN="placeholder-not-a-real-token" bash ha-broker.sh
#
# Environment variables:
#   HASS_BASE_URL   Base URL of HA (no trailing /api). REQUIRED unless HASS_MOCK=1.
#   HASS_TOKEN      Token, if injected directly (Tier-2 scoped forward). Optional.
#   HASS_VAULT      "seckit" | "op" — where to resolve the token if not in env.
#   HASS_OP_REF     op:// reference (when HASS_VAULT=op).
#   HASS_TIMEOUT    curl --max-time seconds. Default: 15.
#   HASS_MOCK       If "1", skip the network and emit a canned healthcheck so the
#                   broker mechanism can be proven offline with a placeholder token.
# =============================================================================

set -euo pipefail

BASE_URL="${HASS_BASE_URL:-}"
TIMEOUT="${HASS_TIMEOUT:-15}"
VAULT="${HASS_VAULT:-}"
OP_REF="${HASS_OP_REF:-}"
MOCK="${HASS_MOCK:-0}"

# ---------------------------------------------------------------------------
# scrub(): the leak guard. Every line the broker prints passes through this so
# that even if a token-shaped string ever reached stdout (e.g. echoed inside an
# HA error message) it is redacted before a human or the agent ever sees it.
# We redact: (a) the resolved token's exact value, and (b) common token shapes
# (long-lived HA tokens are long base64url JWTs; also bearer headers, op refs).
# ---------------------------------------------------------------------------
scrub() {
  local token_value="${1:-}"
  sed -E \
    -e "s/Bearer[[:space:]]+[A-Za-z0-9._~+/=-]+/Bearer <redacted>/g" \
    -e "s/eyJ[A-Za-z0-9._~+/=-]{20,}/<redacted-jwt>/g" \
    -e "s#op://[^[:space:]\"']+#op://<redacted-ref>#g" \
    | { if [[ -n "$token_value" ]]; then
          # Redact the exact resolved value too (belt and suspenders).
          sed -E "s/$(printf '%s' "$token_value" | sed -E 's/[][\\.^$*+?(){}|/]/\\&/g')/<redacted-token>/g"
        else
          cat
        fi
      }
}

emit() {
  # Print a line, always scrubbed against the live token.
  printf '%s\n' "$1" | scrub "${TOKEN:-}"
}

# ---------------------------------------------------------------------------
# Resolve the token (vault-first). Held in $TOKEN, never printed.
# ---------------------------------------------------------------------------
TOKEN="${HASS_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  case "$VAULT" in
    seckit)
      if ! command -v seckit >/dev/null 2>&1; then
        echo "ERROR: HASS_VAULT=seckit but seckit not found on PATH." >&2; exit 2
      fi
      # service 'hermes' per docs/secrets.md; --raw prints the value to stdout
      # which we capture directly into a variable (never to the terminal).
      TOKEN="$(seckit get HASS_TOKEN --raw --service hermes 2>/dev/null || seckit get HASS_TOKEN --raw 2>/dev/null || true)"
      ;;
    op)
      if ! command -v op >/dev/null 2>&1; then
        echo "ERROR: HASS_VAULT=op but the 1Password CLI (op) not found on PATH." >&2; exit 2
      fi
      [[ -n "$OP_REF" ]] || { echo "ERROR: HASS_VAULT=op requires HASS_OP_REF=op://..." >&2; exit 2; }
      TOKEN="$(op read "$OP_REF" 2>/dev/null || true)"
      ;;
    "")
      : # no vault requested; will validate below
      ;;
    *)
      echo "ERROR: unknown HASS_VAULT='$VAULT' (use 'seckit' or 'op')." >&2; exit 2
      ;;
  esac
fi

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: no HASS_TOKEN resolved. Provide HASS_TOKEN, or set HASS_VAULT=seckit|op." >&2
  echo "  (token is resolved from the vault into this broker only; the agent never holds it)" >&2
  exit 2
fi

emit "Home Assistant health check via BROKER (read-only)"
emit "  token source: ${VAULT:-env-injected}   (value held in broker process only, never printed)"

# ---------------------------------------------------------------------------
# MOCK path — prove the broker mechanism offline with a placeholder token.
# No network is touched; we emit a representative healthcheck so the proof can
# assert (a) the broker runs end-to-end and (b) nothing leaks the token.
# ---------------------------------------------------------------------------
if [[ "$MOCK" == "1" ]]; then
  emit "  MODE: MOCK (no network; canned result; placeholder token accepted)"
  emit ""
  emit "[1/4] config        OK — HA up. version=2026.6.1 location=Home"
  emit "[2/4] error_log     OK — 2 ERROR / 5 WARNING lines (grouped below)"
  emit "[3/4] states        OK — 214 entities, 3 unavailable/unknown"
  emit "        - sensor.washer_power = unavailable   (stuck 14h)"
  emit "        - binary_sensor.garage_door = unknown (stuck 2h)"
  emit "        - light.guest_room = unavailable      (stuck 31h)"
  emit "[4/4] dashboards    NOTE — 1 Lovelace card references missing entity: sensor.old_pv_power"
  emit ""
  emit "Findings (mock):"
  emit "  - 3 entities stuck unavailable/unknown — washer/garage/guest light integrations to check."
  emit "  - Dashboard references a deleted entity (sensor.old_pv_power) → 'Entity not found' card."
  emit "  - 2 ERRORs grouped to 1 integration (recorder) — review recorder/db health."
  emit ""
  emit "Structural suggestions:"
  emit "  - Add an automation alerting when any entity is unavailable > 1h."
  emit "  - Prune dashboard cards pointing at removed entities."
  emit "  - Consider a 'health' template sensor aggregating unavailable counts."
  emit ""
  emit "Broker complete (mock). Token was never printed."
  exit 0
fi

# ---------------------------------------------------------------------------
# LIVE path — real read-only probes. The token is attached to the request
# header inside this broker; the header is never emitted (scrub strips Bearer).
# ---------------------------------------------------------------------------
[[ -n "$BASE_URL" ]] || { echo "ERROR: set HASS_BASE_URL (or use HASS_MOCK=1)." >&2; exit 2; }
BASE_URL="${BASE_URL%/}"

HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1

hass_get() {
  # GET only, bearer auth. stderr from curl is also scrubbed by the caller.
  curl -fsS --max-time "$TIMEOUT" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "${BASE_URL}$1"
}

emit "  Target: ${BASE_URL}"
emit ""

# 1. config -----------------------------------------------------------------
emit "[1/4] GET /api/config"
if config_json="$(hass_get /api/config 2>/dev/null)"; then
  if [[ "$HAVE_JQ" -eq 1 ]]; then
    version="$(printf '%s' "$config_json" | jq -r '.version // "unknown"')"
    location="$(printf '%s' "$config_json" | jq -r '.location_name // "unknown"')"
    emit "      OK — HA up. version=${version} location=${location}"
  else
    emit "      OK — HA up. (install jq for version/location)"
  fi
else
  emit "      FAIL — HA unreachable at ${BASE_URL}/api/config (auth/url/network)."
  exit 1
fi
emit ""

# 2. error_log + system log -------------------------------------------------
emit "[2/4] GET /api/error_log (+ system log if exposed)"
if error_log="$(hass_get /api/error_log 2>/dev/null)"; then
  err_count="$(printf '%s\n' "$error_log" | grep -c 'ERROR'   || true)"
  warn_count="$(printf '%s\n' "$error_log" | grep -c 'WARNING' || true)"
  emit "      OK — ${err_count} ERROR / ${warn_count} WARNING lines."
  # Group the noisiest integrations (component in [brackets]) so a single broken
  # integration is one finding, not a wall.
  printf '%s\n' "$error_log" | grep -E 'ERROR|WARNING' \
    | grep -oE '\[[a-z0-9_.]+\]' | sort | uniq -c | sort -rn | head -5 \
    | while read -r line; do emit "        top: ${line}"; done || true
else
  emit "      WARN — /api/error_log not fetched (non-fatal)."
fi
emit ""

# 3. states: stuck entities -------------------------------------------------
emit "[3/4] GET /api/states (unavailable/unknown entities)"
if states_json="$(hass_get /api/states 2>/dev/null)"; then
  if [[ "$HAVE_JQ" -eq 1 ]]; then
    total="$(printf '%s' "$states_json" | jq 'length')"
    stuck="$(printf '%s' "$states_json" | jq '[.[] | select(.state=="unavailable" or .state=="unknown")] | length')"
    emit "      OK — ${total} entities, ${stuck} unavailable/unknown."
    if [[ "$stuck" -gt 0 ]]; then
      printf '%s' "$states_json" \
        | jq -r '.[] | select(.state=="unavailable" or .state=="unknown")
                 | "        - \(.entity_id) = \(.state) (since \(.last_changed))"' \
        | head -15 | while read -r line; do emit "$line"; done
    fi
    # Stash the set of valid entity_ids for the dashboard cross-check below.
    VALID_ENTITY_IDS="$(printf '%s' "$states_json" | jq -r '.[].entity_id' 2>/dev/null || true)"
  else
    emit "      OK — states fetched. (install jq to count/parse)"
    VALID_ENTITY_IDS=""
  fi
else
  emit "      FAIL — /api/states not fetched."
  exit 1
fi
emit ""

# 4. dashboard "entity not found" cross-check -------------------------------
# Lovelace config is read-only via GET /api/config (storage mode often not REST-
# exposed). Where the websocket/lovelace REST is unavailable we degrade
# gracefully: we report what we CAN check (entities referenced in templates that
# no longer exist) rather than pretending to read the dashboard.
emit "[4/4] Dashboard / entity-not-found cross-check"
if [[ -n "${VALID_ENTITY_IDS:-}" ]]; then
  known_count="$(printf '%s' "$VALID_ENTITY_IDS" | grep -c . || true)"
  emit "      OK — ${known_count} known entities available to validate dashboard references against."
  emit "      NOTE — Lovelace storage config is not exposed over plain REST; to fully"
  emit "             detect 'Entity not found' cards, run the agent's websocket"
  emit "             lovelace/config probe (read-only) and diff card entities vs the list above."
else
  emit "      SKIP — entity list unavailable (need jq) to cross-check dashboard references."
fi
emit ""

emit "Broker complete. Token was held in this process only and never printed."
